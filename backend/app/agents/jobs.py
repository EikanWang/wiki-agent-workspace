"""Job manager: runs app-driven agent turns, serializes jobs per agent session, persists
events with monotonically increasing `seq`, and fans them out to SSE subscribers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

from ..db import Database, iso, new_id, utcnow
from ..errors import ApiError, conflict, not_found
from ..events import EventBus
from ..models import AgentSession, Job, JobEvent, Message
from .base import AgentAdapter, AgentEvent, ProviderError, TurnContext, TurnHandle
from .registry import ProviderRegistry

log = logging.getLogger("waw.jobs")

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class JobManager:
    def __init__(self, db: Database, providers: ProviderRegistry, bus: EventBus, workspace: Path, codex_sandbox: str):
        self.db = db
        self.providers = providers
        self.bus = bus
        self.workspace = workspace
        self.codex_sandbox = codex_sandbox
        self._locks: dict[str, asyncio.Lock] = {}
        self._handles: dict[str, TurnHandle] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_requested: set[str] = set()
        self._pending_permissions: dict[str, dict[str, Any]] = {}
        self._on_complete: dict[str, Any] = {}
        self._shutting_down = False

    # ------------------------------------------------------------------ startup
    def mark_interrupted(self) -> int:
        """Jobs that were not finished when the server last stopped are `interrupted`."""
        n = 0
        with self.db.write() as s:
            rows = s.query(Job).filter(Job.status.in_(["queued", "running", "waiting_provider_permission"])).all()
            for j in rows:
                j.status = "interrupted"
                j.error_code = "INTERRUPTED"
                j.error_message = "server restarted while the job was in progress"
                j.finished_at = utcnow()
                if j.agent_message_id:
                    m = s.get(Message, j.agent_message_id)
                    if m and m.status not in TERMINAL:
                        m.status = "interrupted"
                self._append_event_locked(s, j.id, "failed", {"code": "INTERRUPTED", "message": j.error_message, "status": "interrupted"})
                n += 1
        return n

    async def shutdown(self, timeout: float = 5.0) -> int:
        """Server stop: terminate app-owned child processes and settle their jobs as `interrupted`.

        The user did not cancel these turns, so they are not recorded as `cancelled`; nothing is
        re-run automatically on restart (DESIGN §16). Returns the number of interrupted jobs."""
        self._shutting_down = True
        tasks = list(self._tasks.values())
        affected = set(self._tasks.keys())
        for job_id, handle in list(self._handles.items()):
            self._cancel_requested.add(job_id)
            try:
                await handle.cancel()
            except Exception:  # pragma: no cover - best effort
                log.debug("cancel during shutdown failed for %s", job_id, exc_info=True)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for t in pending:
                t.cancel()
        n = 0
        with self.db.write() as s:
            for job_id in affected:
                j = s.get(Job, job_id)
                if j is None or j.status in ("completed", "failed", "interrupted"):
                    continue
                j.status = "interrupted"
                j.error_code = "INTERRUPTED"
                j.error_message = "server stopped while the job was in progress"
                j.finished_at = j.finished_at or utcnow()
                if j.agent_message_id:
                    m = s.get(Message, j.agent_message_id)
                    if m and m.status not in ("completed", "failed"):
                        m.status = "interrupted"
                n += 1
        return n

    # ------------------------------------------------------------------ events
    def _append_event_locked(self, s, job_id: str, etype: str, payload: dict[str, Any]) -> dict[str, Any]:
        last = s.query(JobEvent.seq).filter_by(job_id=job_id).order_by(JobEvent.seq.desc()).first()
        seq = (last[0] if last else 0) + 1
        ev = JobEvent(job_id=job_id, seq=seq, type=etype, payload_json=json.dumps(payload, ensure_ascii=False))
        s.add(ev)
        s.flush()
        return {"seq": seq, "type": etype, "payload": payload, "created_at": iso(ev.created_at), "job_id": job_id}

    def _emit(self, job_id: str, etype: str, payload: dict[str, Any]) -> None:
        with self.db.write() as s:
            ev = self._append_event_locked(s, job_id, etype, payload)
        self.bus.publish(f"job:{job_id}", ev)
        self.bus.publish("global", {"type": "job_event", "job_id": job_id, "event_type": etype, "seq": ev["seq"],
                                    "status": payload.get("status"), "thread_id": payload.get("thread_id")})

    async def stream(self, job_id: str, after: int) -> AsyncIterator[dict[str, Any]]:
        q = self.bus.subscribe(f"job:{job_id}")
        try:
            with self.db.read() as s:
                job = s.get(Job, job_id)
                if job is None:
                    raise not_found("job not found", "JOB_NOT_FOUND")
                rows = s.query(JobEvent).filter(JobEvent.job_id == job_id, JobEvent.seq > after).order_by(JobEvent.seq).all()
            last = after
            for r in rows:
                last = r.seq
                yield {"seq": r.seq, "type": r.type, "payload": json.loads(r.payload_json), "created_at": iso(r.created_at), "job_id": job_id}
            if rows and rows[-1].type in ("completed", "failed", "cancelled"):
                return
            with self.db.read() as s:
                job = s.get(Job, job_id)
                if job and job.status in TERMINAL:
                    # replay anything that landed between the query and the subscription
                    with self.db.read() as s2:
                        rows = s2.query(JobEvent).filter(JobEvent.job_id == job_id, JobEvent.seq > last).order_by(JobEvent.seq).all()
                    for r in rows:
                        yield {"seq": r.seq, "type": r.type, "payload": json.loads(r.payload_json), "created_at": iso(r.created_at), "job_id": job_id}
                    return
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except TimeoutError:
                    yield {"seq": last, "type": "heartbeat", "payload": {}, "job_id": job_id}
                    continue
                if ev["seq"] <= last:
                    continue
                last = ev["seq"]
                yield ev
                if ev["type"] in ("completed", "failed", "cancelled"):
                    return
        finally:
            self.bus.unsubscribe(f"job:{job_id}", q)

    # ------------------------------------------------------------------ enqueue
    def enqueue_turn(self, *, session_id: str, thread_id: str | None, prompt: str, context: dict[str, Any],
                     agent_message_id: str | None, kind: str = "turn", cwd: Path | None = None,
                     on_complete=None) -> str:
        job_id = new_id()
        ctx = dict(context)
        if cwd is not None:
            ctx["_cwd"] = str(cwd)
        with self.db.write() as s:
            s.add(Job(id=job_id, session_id=session_id, thread_id=thread_id, kind=kind, status="queued", prompt=prompt,
                      context_json=json.dumps(ctx, ensure_ascii=False), agent_message_id=agent_message_id))
        if on_complete is not None:
            self._on_complete[job_id] = on_complete
        self._emit(job_id, "queued", {"status": "queued", "thread_id": thread_id, "session_id": session_id})
        self._tasks[job_id] = asyncio.get_running_loop().create_task(self._run(job_id))
        return job_id

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = self._locks[session_id] = asyncio.Lock()
        return lock

    # ------------------------------------------------------------------ run
    async def _run(self, job_id: str) -> None:
        with self.db.read() as s:
            job = s.get(Job, job_id)
            assert job is not None
            s.get(AgentSession, job.session_id)
            session_id = job.session_id
        lock = self._lock_for(session_id)
        if lock.locked():
            log.info("job %s waiting for session %s", job_id, session_id)
            self._emit(job_id, "waiting", {"status": "queued", "message": "waiting for session: another job is using this agent session"})
        async with lock:
            log.info("job %s acquired session %s (cancel requested=%s)", job_id, session_id, job_id in self._cancel_requested)
            try:
                if job_id in self._cancel_requested:
                    self._cancel_requested.discard(job_id)
                    with self.db.read() as s:
                        cur = s.get(Job, job_id)
                    if cur is not None and cur.status not in TERMINAL:  # not already finished by cancel()
                        self._finish(job_id, "cancelled", "CANCELLED", "cancelled before it started", "")
                        self._emit(job_id, "cancelled", {"status": "cancelled"})
                else:
                    await self._run_locked(job_id)
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("job %s crashed", job_id)
                self._finish(job_id, "failed", "INTERNAL_ERROR", str(exc)[:500], "")
                self._emit(job_id, "failed", {"status": "failed", "code": "INTERNAL_ERROR", "message": "unexpected error running the job"})
            finally:
                log.info("job %s released session %s", job_id, session_id)
                self._handles.pop(job_id, None)
                self._pending_permissions.pop(job_id, None)
                self._tasks.pop(job_id, None)
                cb = self._on_complete.pop(job_id, None)
                if cb is not None:
                    try:
                        await cb(job_id)
                    except Exception:  # pragma: no cover
                        log.exception("on_complete for %s failed", job_id)

    async def _run_locked(self, job_id: str) -> None:
        with self.db.write() as s:
            job = s.get(Job, job_id)
            assert job is not None
            session = s.get(AgentSession, job.session_id)
            assert session is not None
            job.status = "running"
            job.started_at = utcnow()
            if job.agent_message_id:
                m = s.get(Message, job.agent_message_id)
                if m:
                    m.status = "running"
            provider = session.provider
            native_id = session.native_id
            display_name = session.display_name
            prompt = job.prompt
            context = json.loads(job.context_json or "{}")
            thread_id = job.thread_id
            kind = job.kind
        self._emit(job_id, "running", {"status": "running", "provider": provider, "native_session_id": native_id})

        try:
            adapter: AgentAdapter = self.providers.get(provider)
            caps = await self.providers.capabilities(provider)
            if not caps.available:
                raise ProviderError("PROVIDER_UNAVAILABLE", caps.error or f"{provider} is not available")
            cwd = Path(context["_cwd"]) if context.get("_cwd") else None
            turn = TurnContext(prompt=prompt, workspace=self.workspace, native_session_id=native_id,
                               title=display_name or (context.get("document_path") and f"Wiki: {context['document_path']}"),
                               cwd=cwd, sandbox=self.codex_sandbox if provider == "codex" else None)
            handle = await adapter.resume_turn(turn)
        except ProviderError as exc:
            self._finish(job_id, "failed", exc.code, exc.message, "")
            self._emit(job_id, "failed", {"status": "failed", "code": exc.code, "message": exc.message, "thread_id": thread_id})
            return

        self._handles[job_id] = handle
        texts: dict[str, str] = {}
        order: list[str] = []
        final: AgentEvent | None = None
        events = handle.events()
        try:
            async for ev in events:
                if ev.type == "started":
                    nid = ev.payload.get("native_session_id")
                    if nid:
                        self._bind_native_id(job_id, nid)
                    self._emit(job_id, "started", {"status": "running", "native_session_id": nid})
                elif ev.type == "delta":
                    pid = ev.payload.get("part_id", "t")
                    if pid not in texts:
                        order.append(pid)
                        texts[pid] = ""
                    if ev.payload.get("replace"):
                        texts[pid] = ev.payload.get("text", "")
                    else:
                        texts[pid] += ev.payload.get("text", "")
                    self._emit(job_id, "delta", {"part_id": pid, "text": ev.payload.get("text", ""), "replace": bool(ev.payload.get("replace"))})
                elif ev.type == "tool_activity":
                    self._emit(job_id, "tool_activity", ev.payload)
                elif ev.type == "notice":
                    self._emit(job_id, "notice", ev.payload)
                elif ev.type == "permission_request":
                    req = dict(ev.payload)
                    req["job_id"] = job_id
                    self._pending_permissions[job_id] = req
                    with self.db.write() as s:
                        j = s.get(Job, job_id)
                        if j:
                            j.status = "waiting_provider_permission"
                            j.pending_permission_json = json.dumps(req, ensure_ascii=False)
                    payload = {**req, "status": "waiting_provider_permission"}
                    if not req.get("relay_supported"):
                        payload["message"] = ("The provider asked for a permission that this mode cannot relay. "
                                              "Answer it in the provider's own terminal, or use the terminal tab to continue.")
                    self._emit(job_id, "permission_request", payload)
                elif ev.type in ("completed", "failed", "cancelled"):
                    final = ev
                    break
        except Exception as exc:  # adapter bug or transport failure
            log.exception("adapter failure in job %s", job_id)
            final = AgentEvent("failed", {"code": "PROVIDER_FAILED", "message": f"adapter error: {exc.__class__.__name__}"})
        finally:
            # We leave the loop on the terminal event; close the generator now so the adapter runs its
            # cleanup (process wait, transport close) deterministically instead of at garbage collection.
            try:
                await events.aclose()
            except Exception:  # pragma: no cover - adapter cleanup must never mask the result
                log.debug("closing adapter event stream for job %s failed", job_id, exc_info=True)

        text = final.payload.get("text") if final and final.type == "completed" else None
        if text is None:
            text = "".join(texts[p] for p in order)
        if final is None:
            final = AgentEvent("failed", {"code": "PROVIDER_FAILED", "message": "the provider ended without a result"})
        if final.type == "completed":
            self._finish(job_id, "completed", None, None, text)
            self._emit(job_id, "completed", {"status": "completed", "text": text, "thread_id": thread_id, "kind": kind})
        elif final.type == "cancelled":
            self._finish(job_id, "cancelled", "CANCELLED", "cancelled by user", text)
            self._emit(job_id, "cancelled", {"status": "cancelled", "text": text, "thread_id": thread_id})
        else:
            code = final.payload.get("code", "PROVIDER_FAILED")
            msg = final.payload.get("message", "provider failed")
            self._finish(job_id, "failed", code, msg, text)
            self._emit(job_id, "failed", {"status": "failed", "code": code, "message": msg, "text": text, "thread_id": thread_id,
                                         "hint": self._hint_for(code, provider)})
            self._mark_session_from_failure(job_id, code)

    @staticmethod
    def _hint_for(code: str, provider: str) -> str:
        if code == "PROVIDER_AUTH_REQUIRED":
            return f"Log in to {provider} in the terminal tab (same OS user), then retry."
        if code == "SESSION_NOT_FOUND":
            return "The provider no longer has this session. Link another session or start a new one."
        if code == "SESSION_BUSY":
            return "The session is busy (possibly an interactive TUI). Finish/exit it, or explicitly choose a new session."
        if code == "PROVIDER_UNAVAILABLE":
            return "Check the provider installation/configuration; the reader and terminal remain usable."
        if code == "PERMISSION_RELAY_UNSUPPORTED":
            return "Respond in the provider's terminal, or switch OpenCode to server mode to relay permissions."
        return "Retry explicitly, or continue in the terminal."

    def _bind_native_id(self, job_id: str, native_id: str) -> None:
        with self.db.write() as s:
            job = s.get(Job, job_id)
            if not job:
                return
            sess = s.get(AgentSession, job.session_id)
            if not sess:
                return
            if sess.native_id is None:
                dup = s.query(AgentSession).filter_by(provider=sess.provider, native_id=native_id, workspace_id=sess.workspace_id).one_or_none()
                if dup is not None and dup.id != sess.id:
                    # extremely unlikely: provider reused an id we already track; keep both consistent
                    sess.native_id = None
                    sess.status = "unavailable"
                    return
                sess.native_id = native_id
            sess.status = "ready"

    def _mark_session_from_failure(self, job_id: str, code: str) -> None:
        if code not in ("SESSION_NOT_FOUND",):
            return
        with self.db.write() as s:
            job = s.get(Job, job_id)
            if job:
                sess = s.get(AgentSession, job.session_id)
                if sess:
                    sess.status = "missing"

    def _finish(self, job_id: str, status: str, code: str | None, message: str | None, text: str) -> None:
        with self.db.write() as s:
            job = s.get(Job, job_id)
            if not job:
                return
            job.status = status
            job.error_code = code
            job.error_message = message
            job.finished_at = utcnow()
            job.pending_permission_json = None
            if job.agent_message_id:
                m = s.get(Message, job.agent_message_id)
                if m:
                    m.status = status
                    m.content = text if status == "completed" or text else (message or "")

    # ------------------------------------------------------------------ cancel / permissions
    async def cancel(self, job_id: str) -> dict[str, Any]:
        with self.db.read() as s:
            job = s.get(Job, job_id)
        if job is None:
            raise not_found("job not found", "JOB_NOT_FOUND")
        if job.status in TERMINAL:
            return {"job_id": job_id, "status": job.status}
        self._cancel_requested.add(job_id)
        handle = self._handles.get(job_id)
        if handle is not None:
            await handle.cancel()
            return {"job_id": job_id, "status": "cancelling"}
        if job.status == "queued":
            # Still waiting for the session lock: finish immediately; _run() will skip it.
            self._finish(job_id, "cancelled", "CANCELLED", "cancelled before it started", "")
            self._emit(job_id, "cancelled", {"status": "cancelled", "thread_id": job.thread_id})
            return {"job_id": job_id, "status": "cancelled"}
        return {"job_id": job_id, "status": job.status}

    async def permission_decision(self, job_id: str, request_id: str, decision: str) -> dict[str, Any]:
        pending = self._pending_permissions.get(job_id)
        if pending is None or pending.get("request_id") != request_id:
            raise conflict("PERMISSION_STALE", "no pending permission request with that id for this job")
        if not pending.get("relay_supported"):
            raise ApiError(409, "PERMISSION_RELAY_UNSUPPORTED", "this provider mode cannot relay permission decisions; answer in the terminal")
        options = pending.get("options") or []
        if options and decision not in options:
            raise ApiError(400, "INVALID_DECISION", f"decision must be one of {options}")
        handle = self._handles.get(job_id)
        if handle is None:
            raise conflict("PERMISSION_STALE", "job is no longer running")
        try:
            await handle.respond_permission(request_id, decision)
        except ProviderError as exc:
            if exc.code == "PERMISSION_STALE":
                raise conflict("PERMISSION_STALE", exc.message)
            raise ApiError(502, exc.code, exc.message)
        self._pending_permissions.pop(job_id, None)
        with self.db.write() as s:
            j = s.get(Job, job_id)
            if j and j.status == "waiting_provider_permission":
                j.status = "running"
                j.pending_permission_json = None
        self._emit(job_id, "running", {"status": "running", "message": f"permission {request_id}: {decision}"})
        return {"job_id": job_id, "status": "running"}

    def get(self, job_id: str) -> dict[str, Any]:
        with self.db.read() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise not_found("job not found", "JOB_NOT_FOUND")
            return job_dict(job)

    def active_count(self) -> int:
        return len(self._tasks)


def job_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id, "session_id": job.session_id, "thread_id": job.thread_id, "kind": job.kind, "status": job.status,
        "error_code": job.error_code, "error_message": job.error_message, "agent_message_id": job.agent_message_id,
        "pending_permission": json.loads(job.pending_permission_json) if job.pending_permission_json else None,
        "created_at": iso(job.created_at), "started_at": iso(job.started_at), "finished_at": iso(job.finished_at),
    }
