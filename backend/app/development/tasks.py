"""Build / test / custom command runs in a development workspace.

A run is `sh -c <command>` in the worktree on the workspace's host (through `ssh -tt` for SSH hosts, so the remote
command ends with the connection). Output goes to a log file in the state dir (stdout and stderr interleaved); the UI
polls it by byte offset. Runs still marked running at startup were cut off by a restart: they become `interrupted`.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

from ..db import iso, new_id, utcnow
from ..deps import AppContext
from ..errors import ApiError, bad_request, conflict, not_found
from ..models import DevTaskRun
from .service import DevService, settings_of

MAX_LOG_READ = 256 * 1024
MAX_PARALLEL = 4


def run_dict(r: DevTaskRun) -> dict[str, Any]:
    return {"id": r.id, "dev_id": r.dev_id, "kind": r.kind, "command": r.command, "status": r.status, "exit_code": r.exit_code,
            "started_at": iso(r.started_at), "finished_at": iso(r.finished_at)}


class TaskRunner:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.dev_of: dict[str, str] = {}
        self.logs = ctx.settings.state_dir / "dev" / "logs"

    def mark_interrupted(self) -> int:
        with self.ctx.db.write() as s:
            rows = s.query(DevTaskRun).filter_by(status="running").all()
            for r in rows:
                r.status = "interrupted"
                r.finished_at = utcnow()
            return len(rows)

    def running_count(self, dev_id: str) -> int:
        return sum(1 for d in self.dev_of.values() if d == dev_id)

    def get(self, run_id: str) -> DevTaskRun:
        with self.ctx.db.read() as s:
            r = s.get(DevTaskRun, run_id)
        if r is None:
            raise not_found("task run not found", "TASK_NOT_FOUND")
        return r

    def list(self, dev_id: str, limit: int = 30) -> list[dict[str, Any]]:
        with self.ctx.db.read() as s:
            rows = s.query(DevTaskRun).filter_by(dev_id=dev_id).order_by(DevTaskRun.started_at.desc()).limit(limit).all()
        return [run_dict(r) for r in rows]

    async def start(self, dev_id: str, kind: str, command: str | None) -> dict[str, Any]:
        svc = DevService(self.ctx)
        row, target = svc._ready(dev_id)
        if kind not in ("build", "test", "custom"):
            raise bad_request("kind must be build, test or custom", "INVALID_TASK")
        cfg = settings_of(row)
        cmd = (command or "").strip() or (cfg.get(f"{kind}_command") if kind != "custom" else "") or ""
        if not cmd:
            raise bad_request(f"no {kind} command is configured for this workspace; set it in the workspace settings", "NO_COMMAND")
        if len(cmd) > 8000 or "\0" in cmd:
            raise bad_request("command too long", "INVALID_TASK")
        if self.running_count(dev_id) >= MAX_PARALLEL:
            raise conflict("TOO_MANY_TASKS", f"{MAX_PARALLEL} commands are already running in this workspace")
        run_id = new_id()
        self.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = self.logs / f"{run_id}.log"
        log_path.touch(mode=0o600)
        with self.ctx.db.write() as s:
            s.add(DevTaskRun(id=run_id, dev_id=dev_id, kind=kind, command=cmd, status="running", log_path=str(log_path)))
        env = {"TERM": "dumb", "CLICOLOR": "0", "PYTHONUNBUFFERED": "1"}
        script = cmd
        py = cfg.get("python") or ""
        if "/" in py:  # the configured interpreter's folder (a virtual environment / conda env) first on PATH
            import shlex

            script = f'PATH={shlex.quote(py.rsplit("/", 1)[0])}:"$PATH"; export PATH; {cmd}'
        argv, lcwd, lenv = target.spawn_argv(["sh", "-c", script], row.worktree_path, env, ssh_opts=None if target.is_local else ["-tt"])
        fh = open(log_path, "ab", buffering=0)  # noqa: SIM115 - closed when the process ends
        try:
            proc = await asyncio.create_subprocess_exec(*argv, cwd=lcwd, env=lenv, stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=fh, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except (OSError, ValueError) as exc:
            fh.close()
            self._finish(run_id, "failed", None, f"could not start: {exc}")
            raise ApiError(400, "TASK_START_FAILED", f"could not start the command: {exc}")
        self.procs[run_id] = proc
        self.dev_of[run_id] = dev_id
        self._event(dev_id, run_id, "running")

        async def wait() -> None:
            try:
                rc = await proc.wait()
            finally:
                fh.close()
            cancelled = getattr(proc, "_waw_cancelled", False)
            self._finish(run_id, "cancelled" if cancelled else ("passed" if rc == 0 else "failed"), rc, None)
            self.procs.pop(run_id, None)
            self.dev_of.pop(run_id, None)

        asyncio.get_running_loop().create_task(wait())
        return run_dict(self.get(run_id))

    def _finish(self, run_id: str, status: str, rc: int | None, note: str | None) -> None:
        dev_id = None
        with self.ctx.db.write() as s:
            r = s.get(DevTaskRun, run_id)
            if r is not None:
                r.status = status
                r.exit_code = rc
                r.finished_at = utcnow()
                dev_id = r.dev_id
                if note:
                    with open(r.log_path, "a", encoding="utf-8") as fh:
                        fh.write(f"\n[{note}]\n")
        if dev_id:
            self._event(dev_id, run_id, status)

    def _event(self, dev_id: str, run_id: str, status: str) -> None:
        if self.ctx.events:
            self.ctx.events.publish("global", {"type": "dev_task", "dev_id": dev_id, "run_id": run_id, "status": status})

    async def cancel(self, run_id: str) -> dict[str, Any]:
        proc = self.procs.get(run_id)
        if proc is None or proc.returncode is not None:
            return run_dict(self.get(run_id))
        proc._waw_cancelled = True  # type: ignore[attr-defined]
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=5)
        except TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.sleep(0)
        return run_dict(self.get(run_id))

    def read_log(self, run_id: str, offset: int) -> dict[str, Any]:
        r = self.get(run_id)
        p = Path(r.log_path)
        size = p.stat().st_size if p.exists() else 0
        offset = max(0, min(offset, size))
        data = b""
        if p.exists():
            with p.open("rb") as fh:
                fh.seek(offset)
                data = fh.read(MAX_LOG_READ)
        text = data.decode("utf-8", "replace").replace("\r\n", "\n")
        return {"text": text, "offset": offset + len(data), "size": size, "run": run_dict(r)}

    def tail(self, run_id: str, max_chars: int = 30_000) -> str:
        r = self.get(run_id)
        p = Path(r.log_path)
        if not p.exists():
            return ""
        data = p.read_bytes()[-max_chars * 2:]
        return data.decode("utf-8", "replace").replace("\r\n", "\n")[-max_chars:]

    async def shutdown(self) -> None:
        for run_id in list(self.procs):
            await self.cancel(run_id)
