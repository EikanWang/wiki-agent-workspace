"""Build / test / custom command runs in a development workspace.

A run is `sh -c <command>` in the worktree on the workspace's host. Output goes to a log file in the state dir (stdout
and stderr interleaved); the UI polls it by byte offset.

* On this server, and on machines without tmux (through `ssh -tt`, so the remote command ends with the connection),
  the command is a child of the app: runs still marked running at startup were cut off by a restart (`interrupted`).
* On a machine with tmux (machines.rtmux), the command runs in a tmux session of its own there and writes its log in a
  run folder under the machine's work_dir (`cmd.sh`, `log`, `pid`, then `rc` with the exit code). The app follows that
  log over ssh into the local copy; when the connection drops it follows again from where the copy ends, and after a
  restart it picks up the runs still going. Cancel signals the command's whole process group there. The run folder is
  removed once the exit code is read.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
from pathlib import Path
from typing import Any

from ..db import iso, new_id, utcnow
from ..deps import AppContext
from ..errors import ApiError, bad_request, conflict, not_found
from ..models import DevTaskRun
from .service import DevService, settings_of

log = logging.getLogger("waw.dev.tasks")
MAX_LOG_READ = 256 * 1024
MAX_PARALLEL = 4
RUN_SESSION_PREFIX = "wawrun-"
LOST = 3  # the follow script's exit code when the command is gone without an exit code

# runs in the tmux session: the command in a process group of its own (setsid), its exit code written last
_WRAP = r'''d=$1; wt=$2
cd "$wt" 2>"$d/log" || { echo 1 > "$d/rc.tmp"; mv -f "$d/rc.tmp" "$d/rc"; exit 0; }
export TERM=dumb CLICOLOR=0 PYTHONUNBUFFERED=1
if command -v setsid >/dev/null 2>&1; then setsid sh "$d/cmd.sh" >"$d/log" 2>&1 </dev/null &
else sh "$d/cmd.sh" >"$d/log" 2>&1 </dev/null &
fi
echo $! > "$d/pid"
wait $!
echo $? > "$d/rc.tmp"; mv -f "$d/rc.tmp" "$d/rc"
'''

# prints the log from byte `off` on as it grows; exits 0 once the exit code is there, LOST when the command is gone
_FOLLOW = r'''d=$1; off=$2; gone=0
while :; do
  fin=; [ -f "$d/rc" ] && fin=1
  sz=$(wc -c < "$d/log" 2>/dev/null); sz=$((${sz:-0} + 0))
  if [ "$sz" -gt "$off" ]; then tail -c +$((off + 1)) "$d/log" | head -c $((sz - off)); off=$sz; fi
  [ -n "$fin" ] && exit 0
  p=$(cat "$d/pid" 2>/dev/null)
  if [ ! -d "$d" ] || { [ -n "$p" ] && ! kill -0 "$p" 2>/dev/null; }; then
    gone=$((gone + 1)); [ "$gone" -ge 3 ] && exit 3
  fi
  sleep 1
done
'''

_CANCEL = r'''d=$1; p=$(cat "$d/pid" 2>/dev/null)
[ -n "$p" ] || exit 0
kill -TERM -- "-$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null
i=0; while [ ! -f "$d/rc" ] && [ "$i" -lt 5 ]; do sleep 1; i=$((i + 1)); done
[ -f "$d/rc" ] || kill -KILL -- "-$p" 2>/dev/null || kill -KILL "$p" 2>/dev/null
exit 0
'''


def run_dict(r: DevTaskRun) -> dict[str, Any]:
    return {"id": r.id, "dev_id": r.dev_id, "kind": r.kind, "command": r.command, "status": r.status, "exit_code": r.exit_code,
            "machine": r.machine, "in_remote_tmux": bool(r.remote_dir),
            "started_at": iso(r.started_at), "finished_at": iso(r.finished_at)}


class TaskRunner:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.dev_of: dict[str, str] = {}
        self.logs = ctx.settings.state_dir / "dev" / "logs"
        self.followers: dict[str, asyncio.subprocess.Process] = {}  # run id -> the local ssh following a remote log
        self.follow_tasks: dict[str, asyncio.Task] = {}
        self.cancelled: set[str] = set()
        self.stopping = False

    def mark_interrupted(self) -> int:
        """Runs that were children of the app died with it; runs in tmux on a machine go on (see resume)."""
        with self.ctx.db.write() as s:
            rows = s.query(DevTaskRun).filter(DevTaskRun.status == "running", DevTaskRun.remote_dir.is_(None)).all()
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
        env = {"TERM": "dumb", "CLICOLOR": "0", "PYTHONUNBUFFERED": "1"}
        script = cmd
        py = cfg.get("python") or ""
        if "/" in py:  # the configured interpreter's folder (a virtual environment / conda env) first on PATH
            script = f'PATH={shlex.quote(py.rsplit("/", 1)[0])}:"$PATH"; export PATH; {cmd}'
        if not target.is_local and await self._remote_tmux_ready(target):
            return await self._start_remote(run_id, dev_id, kind, cmd, script, target, row.worktree_path, log_path)
        with self.ctx.db.write() as s:
            s.add(DevTaskRun(id=run_id, dev_id=dev_id, kind=kind, command=cmd, status="running", log_path=str(log_path)))
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

    # ------------------------------------------------------------------ runs in tmux on a machine
    def _rtmux(self, target):
        from ..machines import rtmux

        assert self.ctx.terminals is not None
        return rtmux.RemoteTmux(target, rtmux.remote_socket(self.ctx.terminals.tmux.socket), self.ctx.settings.tmux_history_limit,
                                target.host.work_dir)

    async def _remote_tmux_ready(self, target) -> bool:
        from .targets import TargetError

        try:
            return await asyncio.to_thread(lambda: self._rtmux(target).available())
        except TargetError:
            return False

    async def _start_remote(self, run_id: str, dev_id: str, kind: str, cmd: str, script: str, target, worktree: str,
                            log_path: Path) -> dict[str, Any]:
        from .targets import TargetError

        try:
            base = await target.expand(target.host.work_dir)
            rdir = f"{base}/runs/{run_id}"
            await target.mkdir(rdir)
            await target.write_text(f"{rdir}/cmd.sh", script + "\n", 0o700)
            await target.write_text(f"{rdir}/wrap.sh", _WRAP, 0o700)
            with self.ctx.db.write() as s:
                s.add(DevTaskRun(id=run_id, dev_id=dev_id, kind=kind, command=cmd, status="running", log_path=str(log_path),
                                 machine=target.name, remote_dir=rdir))
            rt = self._rtmux(target)
            await asyncio.to_thread(rt.new_session, RUN_SESSION_PREFIX + run_id[:8], worktree,
                                    shlex.join(["sh", f"{rdir}/wrap.sh", rdir, worktree]))
        except TargetError as exc:
            with self.ctx.db.read() as s:
                started = s.get(DevTaskRun, run_id) is not None
            if started:
                self._finish(run_id, "failed", None, f"could not start on {target.name}: {exc.message}")
            raise ApiError(502 if exc.code == "DEV_HOST_UNREACHABLE" else 400, "TASK_START_FAILED", f"could not start the command on {target.name}: {exc.message}")
        self.dev_of[run_id] = dev_id
        self._event(dev_id, run_id, "running")
        self._follow_in_background(run_id, dev_id, target, rdir, log_path)
        return run_dict(self.get(run_id))

    def _follow_in_background(self, run_id: str, dev_id: str, target, rdir: str, log_path: Path) -> None:
        self.dev_of[run_id] = dev_id
        task = asyncio.get_running_loop().create_task(self._follow(run_id, target, rdir, log_path))
        self.follow_tasks[run_id] = task
        task.add_done_callback(lambda _t: (self.follow_tasks.pop(run_id, None), self.dev_of.pop(run_id, None)))

    async def _follow(self, run_id: str, target, rdir: str, log_path: Path) -> None:
        """Copy the remote log into the local one until the exit code appears; follow again (from the copy's size)
        after a dropped connection."""
        from .targets import TargetError

        delay = 2.0
        while not self.stopping:
            off = log_path.stat().st_size if log_path.exists() else 0
            argv, _, _ = target.spawn_argv(["sh", "-c", _FOLLOW, "_", rdir, str(off)], None, None)
            with open(log_path, "ab", buffering=0) as fh:
                try:
                    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=fh,
                                                                stderr=asyncio.subprocess.PIPE)
                except OSError as exc:
                    self._finish(run_id, "failed", None, f"could not follow the command on {target.name}: {exc}")
                    return
                self.followers[run_id] = proc
                _, err = await proc.communicate()
                self.followers.pop(run_id, None)
            if self.stopping:
                return
            if proc.returncode == 0:
                try:
                    rc_code, out, _ = await target.run(["cat", f"{rdir}/rc"], timeout=30)
                    rc = int(out.strip()) if rc_code == 0 and out.strip().lstrip("-").isdigit() else None
                    await target.run(["rm", "-rf", rdir], timeout=60)
                except TargetError:
                    await asyncio.sleep(delay)
                    continue
                cancelled = run_id in self.cancelled
                self.cancelled.discard(run_id)
                self._finish(run_id, "cancelled" if cancelled else ("passed" if rc == 0 else "failed"), rc, None)
                return
            if proc.returncode == LOST:
                self._finish(run_id, "interrupted", None, f"the command stopped on {target.name} without an exit code "
                             "(the machine restarted, or its tmux was stopped)")
                return
            log.info("following run %s on %s: connection lost (%s); again in %.0f s", run_id, target.name,
                     err.decode("utf-8", "replace").strip()[-200:], delay)
            await asyncio.sleep(delay)
            delay = min(30.0, delay * 2)

    def resume(self) -> int:
        """After a restart: follow again the runs still going in tmux on a machine. Call from the event loop."""
        from .targets import TargetError, target_for

        with self.ctx.db.read() as s:
            rows = s.query(DevTaskRun).filter(DevTaskRun.status == "running", DevTaskRun.remote_dir.is_not(None)).all()
        n = 0
        for r in rows:
            try:
                t = target_for(self.ctx.extra["machines"].dev, r.machine)
            except TargetError as exc:
                self._finish(r.id, "interrupted", None, f"cannot follow the command any more: {exc.message}")
                continue
            self._follow_in_background(r.id, r.dev_id, t, r.remote_dir, Path(r.log_path))
            n += 1
        return n

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
        r = self.get(run_id)
        if r.remote_dir and r.status == "running":
            return await self._cancel_remote(r)
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

    async def _cancel_remote(self, r: DevTaskRun) -> dict[str, Any]:
        from .targets import TargetError, target_for

        self.cancelled.add(r.id)
        try:
            t = target_for(self.ctx.extra["machines"].dev, r.machine)
            await t.run(["sh", "-c", _CANCEL, "_", r.remote_dir], timeout=30)
        except TargetError as exc:
            if exc.code == "DEV_HOST_UNKNOWN":  # the machine was removed: nothing to stop any more from here
                task = self.follow_tasks.get(r.id)
                if task:
                    task.cancel()
                self._finish(r.id, "cancelled", None, exc.message)
                return run_dict(self.get(r.id))
            self.cancelled.discard(r.id)
            raise ApiError(502, exc.code, f"could not stop the command on {r.machine}: {exc.message}")
        task = self.follow_tasks.get(r.id)
        if task is not None:  # the follower sees the exit code within a second or two
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                pass
        return run_dict(self.get(r.id))

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
        """Stop the app's own children; runs in tmux on a machine keep going (followed again after the restart)."""
        self.stopping = True
        for proc in list(self.followers.values()):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        for task in list(self.follow_tasks.values()):
            task.cancel()
        for run_id in list(self.procs):
            await self.cancel(run_id)
