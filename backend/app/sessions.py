"""Agent session records and routes (DESIGN §7, §13)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from .agents.base import ProviderError
from .db import iso, new_id
from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError, bad_request, not_found, SESSION_NOT_FOUND
from .models import AgentSession, Document

router = APIRouter()


def session_dict(s: AgentSession) -> dict[str, Any]:
    return {
        "id": s.id, "provider": s.provider, "native_id": s.native_id, "workspace_id": s.workspace_id,
        "display_name": s.display_name, "status": s.status, "origin": s.origin,
        "short_id": (s.native_id[-8:] if s.native_id else None),
        "created_at": iso(s.created_at), "updated_at": iso(s.updated_at),
    }


class SessionService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def get(self, session_id: str) -> AgentSession:
        with self.ctx.db.read() as s:
            row = s.get(AgentSession, session_id)
        if row is None:
            raise not_found("agent session not found", SESSION_NOT_FOUND)
        return row

    def create_local(self, provider: str, display_name: str | None, native_id: str | None = None,
                     status: str = "uninitialized", origin: str = "app") -> AgentSession:
        assert self.ctx.providers is not None
        self.ctx.providers.get(provider)  # raises if unknown
        with self.ctx.db.write() as s:
            if native_id:
                existing = s.query(AgentSession).filter_by(provider=provider, native_id=native_id, workspace_id=self.ctx.workspace_id).one_or_none()
                if existing:
                    if display_name and not existing.display_name:
                        existing.display_name = display_name
                    existing.status = status
                    return existing
            row = AgentSession(id=new_id(), provider=provider, native_id=native_id, workspace_id=self.ctx.workspace_id,
                               display_name=display_name, status=status, origin=origin)
            s.add(row)
            s.flush()
            return row

    async def create_for_provider(self, provider: str, display_name: str | None) -> AgentSession:
        """Create a session with the provider when it supports eager ids; otherwise a local
        `uninitialized` record bound on the first turn."""
        assert self.ctx.providers is not None
        adapter = self.ctx.providers.get(provider)
        caps = await self.ctx.providers.capabilities(provider)
        if not caps.available:
            raise ApiError(503, "PROVIDER_UNAVAILABLE", caps.error or f"{provider} is unavailable")
        native_id: str | None = None
        if caps.eager_session_ids:
            try:
                native_id = await adapter.create_session(self.ctx.settings.workspace_root, display_name)
            except ProviderError as exc:
                raise ApiError(502, exc.code, exc.message)
        return self.create_local(provider, display_name, native_id, status="ready" if native_id else "uninitialized")

    async def link_existing(self, provider: str, native_id: str, display_name: str | None) -> AgentSession:
        """Link a provider-native session. Only confirm when the provider confirms the id."""
        assert self.ctx.providers is not None
        adapter = self.ctx.providers.get(provider)
        caps = await self.ctx.providers.capabilities(provider)
        if not caps.available:
            raise ApiError(503, "PROVIDER_UNAVAILABLE", caps.error or f"{provider} is unavailable")
        exists = await adapter.session_exists(self.ctx.settings.workspace_root, native_id)
        if exists is False:
            raise ApiError(404, SESSION_NOT_FOUND, f"{provider} does not report a session with id {native_id}")
        status = "availability_unknown"  # externally created: readiness unknown until a turn succeeds
        if exists is None:
            # the provider cannot list/confirm sessions: say so explicitly rather than claim success
            raise ApiError(409, "PROVIDER_CANNOT_CONFIRM",
                           f"{provider} cannot confirm session ids in this mode; the id was not linked. "
                           "You can still use the terminal, or start a new app session.")
        return self.create_local(provider, display_name, native_id, status=status, origin="linked")

    async def list_all(self, provider: str | None) -> dict[str, Any]:
        assert self.ctx.providers is not None
        with self.ctx.db.read() as s:
            q = s.query(AgentSession).filter_by(workspace_id=self.ctx.workspace_id)
            if provider:
                q = q.filter_by(provider=provider)
            rows = q.order_by(AgentSession.updated_at.desc()).all()
        local = [session_dict(r) for r in rows]
        known = {(r.provider, r.native_id) for r in rows if r.native_id}
        provider_sessions: list[dict[str, Any]] = []
        caps_out: dict[str, Any] = {}
        errors: dict[str, str] = {}
        names = [provider] if provider else list(self.ctx.providers.adapters.keys())
        for name in names:
            try:
                caps = await self.ctx.providers.capabilities(name)
            except ProviderError as exc:
                errors[name] = exc.message
                continue
            caps_out[name] = caps.as_dict()
            if not caps.available or not caps.can_list_sessions:
                if caps.error:
                    errors[name] = caps.error
                continue
            try:
                sessions = await self.ctx.providers.get(name).list_sessions(self.ctx.settings.workspace_root)
            except ProviderError as exc:
                errors[name] = exc.message
                continue
            for ns in sessions:
                d = ns.as_dict()
                d["provider"] = name
                d["linked"] = (name, ns.native_id) in known
                provider_sessions.append(d)
        return {"sessions": local, "provider_sessions": provider_sessions, "capabilities": caps_out, "errors": errors}


class SessionCreateIn(BaseModel):
    provider: str
    workspace_id: str | None = None
    display_name: str | None = None
    native_id: str | None = None  # when set: link an existing provider session


@router.get("/agent-sessions")
async def list_sessions(provider: str | None = Query(None), workspace: str | None = Query(None), ctx: AppContext = Depends(require_auth)):
    return await SessionService(ctx).list_all(provider)


@router.post("/agent-sessions", status_code=201)
async def create_session(body: SessionCreateIn, ctx: AppContext = Depends(require_auth_mutating)):
    svc = SessionService(ctx)
    if body.workspace_id and body.workspace_id != ctx.workspace_id:
        raise bad_request("unknown workspace", "WORKSPACE_NOT_FOUND")
    try:
        if body.native_id:
            row = await svc.link_existing(body.provider, body.native_id, body.display_name)
        else:
            row = await svc.create_for_provider(body.provider, body.display_name)
    except ProviderError as exc:
        raise ApiError(502, exc.code, exc.message)
    return session_dict(row)


@router.get("/agent-sessions/{session_id}")
def get_session(session_id: str, ctx: AppContext = Depends(require_auth)):
    return session_dict(SessionService(ctx).get(session_id))


class DefaultSessionIn(BaseModel):
    session_id: str | None


@router.put("/documents/{doc_id}/default-session")
def put_default_session(doc_id: str, body: DefaultSessionIn, ctx: AppContext = Depends(require_auth_mutating)):
    with ctx.db.write() as s:
        doc = s.get(Document, doc_id)
        if doc is None:
            raise not_found("document not found", "DOCUMENT_NOT_FOUND")
        if body.session_id is not None:
            sess = s.get(AgentSession, body.session_id)
            if sess is None:
                raise not_found("agent session not found", SESSION_NOT_FOUND)
        doc.default_session_id = body.session_id
    return {"document_id": doc_id, "default_session_id": body.session_id}
