"""Agent dashboard: the agent sessions of the chosen folders, their current activity and health, on demand.

Health is derived from facts the app has: the provider is available; the session's native id is still known to the
provider (`deep` check: one session listing per provider and folder); a running job is really alive in this server
process and has produced events recently (else: stuck / orphaned); the last job's outcome; recent failures.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query

from .agents.base import ProviderError
from .db import iso
from .deps import AppContext, require_auth
from .models import AgentSession, Document, Job, JobEvent, Thread, Workspace

router = APIRouter()

ACTIVE = ("queued", "running", "waiting_provider_permission")
STUCK_AFTER = timedelta(minutes=10)  # a running job without any event for this long
RECENT = timedelta(hours=24)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _about(doc: Document | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    p = doc.path
    kind = ("pr" if p.startswith(".waw-github/") else "chat" if p.startswith(".waw-chat/") else "dev" if p.startswith(".waw-dev/")
            else "document")
    return {"kind": kind, "path": p, "document_id": doc.id, "workspace_id": doc.workspace_id}


@router.get("/agent-dashboard/sessions")
def dashboard_sessions(ctx: AppContext = Depends(require_auth)):
    """Every agent session (all folders) for choosing what to monitor: id, provider, name, folder, last use."""
    with ctx.db.read() as s:
        names = {w.id: (w.name or Path(w.root_path).name) for w in s.query(Workspace).all()}
        rows = s.query(AgentSession).filter(AgentSession.origin != "preview").order_by(AgentSession.updated_at.desc()).all()
        out = []
        for r in rows:
            last = s.query(Job.created_at).filter_by(session_id=r.id).order_by(Job.created_at.desc()).first()
            out.append({"id": r.id, "provider": r.provider, "display_name": r.display_name, "short_id": r.native_id[-8:] if r.native_id else None,
                        "workspace_id": r.workspace_id, "workspace_name": names.get(r.workspace_id, "?"), "origin": r.origin,
                        "last_used": iso(last[0]) if last else iso(r.updated_at)})
    return {"sessions": out}


@router.get("/agent-dashboard")
async def dashboard(ids: str = Query("", max_length=40_000), deep: bool = Query(False), ctx: AppContext = Depends(require_auth)):
    """Status and health of the given sessions (comma-separated ids). `deep`: also ask each provider whether the
    session still exists (slower: lists the provider's sessions once per folder)."""
    assert ctx.providers is not None and ctx.jobs is not None
    wanted = [x for x in ids.split(",") if x][:500]
    now = datetime.now(timezone.utc)
    alive = set(ctx.jobs._handles) | set(ctx.jobs._tasks)
    caps = await ctx.providers.summary()
    with ctx.db.read() as s:
        names = {w.id: (w.name or Path(w.root_path).name) for w in s.query(Workspace).all()}
        roots = {w.id: w.root_path for w in s.query(Workspace).all()}
        rows = [r for r in (s.get(AgentSession, i) for i in wanted) if r is not None]
        items: list[dict[str, Any]] = []
        for r in rows:
            jobs = s.query(Job).filter_by(session_id=r.id).order_by(Job.created_at.desc()).limit(20).all()
            active = next((j for j in jobs if j.status in ACTIVE), None)
            finished = [j for j in jobs if j.status not in ACTIVE]
            last = finished[0] if finished else None
            last_event = None
            if active is not None:
                ev = s.query(JobEvent).filter_by(job_id=active.id).order_by(JobEvent.seq.desc()).first()
                last_event = _aware(ev.created_at) if ev else _aware(active.started_at or active.created_at)
            t = s.query(Thread).filter_by(last_session_id=r.id).order_by(Thread.updated_at.desc()).first()
            doc = s.get(Document, t.document_id) if t else None
            recent = [j for j in finished if _aware(j.finished_at or j.created_at) and now - _aware(j.finished_at or j.created_at) < RECENT]
            items.append({
                "session": {"id": r.id, "provider": r.provider, "display_name": r.display_name, "native_id": r.native_id,
                            "short_id": r.native_id[-8:] if r.native_id else None, "model": r.model, "variant": r.variant,
                            "status": r.status, "origin": r.origin, "workspace_id": r.workspace_id,
                            "workspace_name": names.get(r.workspace_id, "?"), "workspace_root": roots.get(r.workspace_id)},
                "active": None if active is None else {
                    "job_id": active.id, "kind": active.kind, "status": active.status, "thread_id": active.thread_id,
                    "started_at": iso(active.started_at), "created_at": iso(active.created_at),
                    "seconds": int((now - _aware(active.started_at or active.created_at)).total_seconds()),
                    "last_event_at": iso(last_event), "idle_seconds": int((now - last_event).total_seconds()) if last_event else None,
                    "alive": active.id in alive,
                    "waiting": s.query(Job).filter(Job.session_id == r.id, Job.status == "queued", Job.id != active.id).count()},
                "last": None if last is None else {
                    "job_id": last.id, "status": last.status, "finished_at": iso(last.finished_at), "error_code": last.error_code,
                    "error_message": (last.error_message or "")[:500] or None,
                    "seconds": int((_aware(last.finished_at) - _aware(last.started_at)).total_seconds()) if last.finished_at and last.started_at else None},
                "recent": {"total": len(recent), "failed": sum(1 for j in recent if j.status in ("failed", "interrupted")),
                           "history": [j.status for j in jobs[:10]]},
                "about": _about(doc),
                "last_activity": iso(_aware((active.started_at if active else None) or (last.finished_at if last else None) or r.updated_at)),
            })
    # does the provider still know the session? one listing per (provider, folder)
    exists: dict[str, bool | None] = {}
    if deep:
        listings: dict[tuple[str, str], set[str] | None] = {}

        async def listing(provider: str, root: str) -> set[str] | None:
            try:
                ns = await asyncio.wait_for(ctx.providers.get(provider).list_sessions(Path(root)), 60)
                return {n.native_id for n in ns}
            except (ProviderError, TimeoutError, OSError):
                return None

        keys = {(it["session"]["provider"], it["session"]["workspace_root"]) for it in items if it["session"]["native_id"] and it["session"]["workspace_root"]}
        results = await asyncio.gather(*(listing(p, root) for p, root in keys))
        listings = dict(zip(keys, results, strict=True))
        for it in items:
            se = it["session"]
            got = listings.get((se["provider"], se["workspace_root"])) if se["native_id"] else None
            exists[se["id"]] = None if got is None else se["native_id"] in got
    for it in items:
        it["health"] = _health(it, caps.get(it["session"]["provider"]) or {}, exists.get(it["session"]["id"]), deep)
    return {"items": items, "checked_at": iso(now), "deep": deep, "providers": {k: {"available": v.get("available"), "version": v.get("version"),
                                                                                       "error": v.get("error")} for k, v in caps.items()}}


def _health(it: dict[str, Any], cap: dict[str, Any], exists: bool | None, deep: bool) -> dict[str, Any]:
    """{level: ok|warning|error|unknown, state: running|waiting|queued|idle|never, problems: [...]}"""
    problems: list[str] = []
    level = "ok"

    rank = {"ok": 0, "unknown": 1, "warning": 2, "error": 3}

    def bad(msg: str, lvl: str = "error") -> None:
        nonlocal level
        problems.append(msg)
        if rank[lvl] > rank[level]:
            level = lvl

    se, act, last = it["session"], it["active"], it["last"]
    if not cap.get("available"):
        bad(f"{se['provider']} is not available: {cap.get('error') or 'not installed / not reachable'}")
    if se["status"] in ("missing", "unavailable"):
        bad(f"the session is {se['status']} at the provider")
    if deep and se["native_id"] and exists is False:
        bad("the provider no longer has this session (deleted or from another machine)")
    state = "never" if act is None and last is None else "idle"
    if act is not None:
        state = {"running": "running", "waiting_provider_permission": "waiting", "queued": "queued"}[act["status"]]
        if not act["alive"]:
            bad("a job is marked active but nothing runs it in this server (interrupted by a restart?)")
        elif act["status"] == "running" and (act["idle_seconds"] or 0) > STUCK_AFTER.total_seconds():
            bad(f"no output for {act['idle_seconds'] // 60} min: the agent may be stuck", "warning")
        if act["status"] == "waiting_provider_permission":
            bad("waiting for your permission decision", "warning")
    if last is not None and last["status"] in ("failed", "interrupted") and act is None:
        bad(f"the last turn {last['status']}" + (f": {last['error_code']}" if last.get("error_code") else ""), "warning")
    r = it["recent"]
    if r["total"] >= 3 and r["failed"] / r["total"] >= 0.5:
        bad(f"{r['failed']} of the last {r['total']} turns (24 h) failed", "warning")
    if deep and se["native_id"] and exists is None and cap.get("available"):
        problems.append("could not confirm the session with the provider")
        if level == "ok":
            level = "unknown"
    return {"level": level, "state": state, "problems": problems, "confirmed": exists}
