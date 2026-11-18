"""Work on a machine as a whole (setup: machines.bootstrap), run by the app with a log in the state dir.

One job at a time per machine. Jobs are app-side (they drive ssh step by step), so one still marked running at startup
was cut off by a restart (`interrupted`); running it again picks up where it stopped (installed tools are skipped).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..db import iso, new_id, utcnow
from ..errors import conflict, not_found
from ..models import MachineJob
from .envfwd import mask

log = logging.getLogger("waw.machines.jobs")
MAX_LOG_READ = 256 * 1024


class JobLog:
    """Lines of a job's log; forwarded values are masked before they are written."""

    def __init__(self, path: Path, secrets: list[str] | None = None):
        self.path = path
        self.secrets = list(secrets or [])

    def write(self, text: str) -> None:
        if not text:
            return
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(mask(text, self.secrets))

    def line(self, text: str) -> None:
        self.write(text.rstrip("\n") + "\n")

    def step(self, text: str) -> None:
        self.line(f"\n== {text}")


def job_dict(j: MachineJob) -> dict[str, Any]:
    return {"id": j.id, "machine": j.machine, "kind": j.kind, "title": j.title, "status": j.status, "error": j.error,
            "params": json.loads(j.params_json or "{}"), "result": json.loads(j.result_json) if j.result_json else None,
            "started_at": iso(j.started_at), "finished_at": iso(j.finished_at)}


class MachineJobs:
    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.logs = ctx.settings.state_dir / "machines" / "logs"
        self.tasks: dict[str, asyncio.Task] = {}
        self.stopping = False

    def mark_interrupted(self) -> int:
        with self.ctx.db.write() as s:
            rows = s.query(MachineJob).filter(MachineJob.status == "running").all()
            for r in rows:
                r.status, r.finished_at = "interrupted", utcnow()
                r.error = r.error or "the server restarted while this ran; run it again to continue"
            return len(rows)

    def get(self, job_id: str) -> MachineJob:
        with self.ctx.db.read() as s:
            j = s.get(MachineJob, job_id)
        if j is None:
            raise not_found("machine job not found", "MACHINE_JOB_NOT_FOUND")
        return j

    def list(self, machine: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.ctx.db.read() as s:
            rows = (s.query(MachineJob).filter_by(machine=machine).order_by(MachineJob.started_at.desc()).limit(limit).all())
        return [job_dict(r) for r in rows]

    def running(self, machine: str) -> MachineJob | None:
        with self.ctx.db.read() as s:
            return s.query(MachineJob).filter_by(machine=machine, status="running").first()

    def start(self, machine: str, kind: str, title: str, params: dict[str, Any],
              work: Callable[[JobLog], Awaitable[dict[str, Any] | None]], secrets: list[str] | None = None) -> dict[str, Any]:
        cur = self.running(machine)
        if cur is not None and cur.id in self.tasks:
            raise conflict("MACHINE_BUSY", f"{machine} is already running “{cur.title}”")
        job_id = new_id()
        self.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.logs / f"{job_id}.log"
        path.touch(mode=0o600)
        with self.ctx.db.write() as s:
            if cur is not None:  # left over from a crash of this process' task: not running any more
                old = s.get(MachineJob, cur.id)
                if old is not None and old.status == "running":
                    old.status, old.finished_at = "interrupted", utcnow()
            s.add(MachineJob(id=job_id, machine=machine, kind=kind, title=title, status="running",
                             params_json=json.dumps(params), log_path=str(path), started_at=utcnow()))
        jl = JobLog(path, secrets)
        self._event(machine, job_id, "running")

        async def run() -> None:
            status, error, result = "passed", None, None
            try:
                result = await work(jl)
            except asyncio.CancelledError:
                if self.stopping:
                    status, error = "interrupted", "the server stopped while this ran; run it again to continue"
                else:
                    status, error = "cancelled", "cancelled"
                jl.line(f"\n[{error}]")
            except JobFailed as exc:
                status, error = "failed", exc.message
                jl.line(f"\n[failed: {exc.message}]")
            except Exception as exc:
                log.exception("machine job %s failed", job_id)
                status, error = "failed", f"{exc.__class__.__name__}: {exc}"
                jl.line(f"\n[failed: {error}]")
            finally:
                self.tasks.pop(job_id, None)
            with self.ctx.db.write() as s:
                j = s.get(MachineJob, job_id)
                if j is not None:
                    j.status, j.error, j.finished_at = status, error, utcnow()
                    j.result_json = json.dumps(result) if result is not None else None
            self._event(machine, job_id, status)

        self.tasks[job_id] = asyncio.get_running_loop().create_task(run())
        return job_dict(self.get(job_id))

    async def cancel(self, job_id: str) -> dict[str, Any]:
        task = self.tasks.get(job_id)
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=15)
            except (TimeoutError, asyncio.CancelledError):
                pass
        return job_dict(self.get(job_id))

    def read_log(self, job_id: str, offset: int) -> dict[str, Any]:
        j = self.get(job_id)
        p = Path(j.log_path)
        size = p.stat().st_size if p.exists() else 0
        offset = max(0, min(offset, size))
        data = b""
        if p.exists():
            with p.open("rb") as fh:
                fh.seek(offset)
                data = fh.read(MAX_LOG_READ)
        return {"text": data.decode("utf-8", "replace"), "offset": offset + len(data), "size": size, "job": job_dict(j)}

    async def shutdown(self) -> None:
        self.stopping = True
        for t in list(self.tasks.values()):
            t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    def _event(self, machine: str, job_id: str, status: str) -> None:
        if self.ctx.events:
            self.ctx.events.publish("global", {"type": "machine_job", "machine": machine, "job_id": job_id, "status": status})


class JobFailed(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
