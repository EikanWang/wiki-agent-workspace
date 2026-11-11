"""Agent session records and routes (DESIGN §7, §13)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

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
        "model": s.model, "variant": s.variant,
        "short_id": (s.native_id[-8:] if s.native_id else None),
        "created_at": iso(s.created_at), "updated_at": iso(s.updated_at),
    }


def clean_model(model: str | None, variant: str | None) -> tuple[str | None, str | None]:
    """Normalise ("" -> None) and validate a model id / variant; they become CLI arguments."""
    model = (model or "").strip() or None
    if model and (model.startswith("-") or any(c in model for c in " \t\n")):
        raise bad_request("invalid model id", "INVALID_MODEL")
    variant = (variant or "").strip() or None
    if variant and (variant.startswith("-") or not variant.replace("-", "").replace("_", "").isalnum()):
        raise bad_request("invalid variant", "INVALID_MODEL")
    return model, variant


def inherited_model(source: AgentSession | None, provider: str) -> tuple[str | None, str | None]:
    """Model/variant a new `provider` session takes over from `source` (model ids are provider-specific)."""
    if source is None or source.provider != provider:
        return None, None
    return source.model, source.variant


class ModelChoiceIn(BaseModel):
    """A model / reasoning level chosen for one discussion (empty values: the provider's default)."""
    provider: str | None = Field(None, max_length=40)
    model: str | None = Field(None, max_length=300)
    variant: str | None = Field(None, max_length=40)


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

    async def create_for_provider(self, provider: str, display_name: str | None, *,
                                  inherit_from: AgentSession | None = None) -> AgentSession:
        """Create a session with the provider when it supports eager ids; otherwise a local
        `uninitialized` record bound on the first turn. With `inherit_from` (the session the user is
        currently working with), the new session starts on the same model/variant when the provider matches."""
        row = await self._create_for_provider(provider, display_name)
        model, variant = inherited_model(inherit_from, provider)
        if model or variant:
            row = self.set_model(row.id, model, variant)
        return row

    def set_model(self, session_id: str, model: str | None, variant: str | None) -> AgentSession:
        model, variant = clean_model(model, variant)
        with self.ctx.db.write() as s:
            row = s.get(AgentSession, session_id)
            if row is None:
                raise not_found("agent session not found", SESSION_NOT_FOUND)
            row.model = model
            row.variant = variant
            return row

    async def session_for_model(self, base: AgentSession | None, choice: "ModelChoiceIn", display_name: str | None) -> AgentSession:
        """A session that runs with the chosen model: `base` when it already does, else a new one (other discussions
        of `base` keep their model)."""
        assert self.ctx.providers is not None
        model, variant = clean_model(choice.model, choice.variant)
        provider = choice.provider or (base.provider if base else None) or self.ctx.providers.default_provider()
        if not provider:
            raise ApiError(503, "PROVIDER_UNAVAILABLE", "no default agent provider configured")
        self.ctx.providers.get(provider)
        if base is not None and base.provider == provider and base.model == model and base.variant == variant:
            return base
        row = await self._create_for_provider(provider, display_name or (base.display_name if base else None))
        return self.set_model(row.id, model, variant)

    async def _create_for_provider(self, provider: str, display_name: str | None) -> AgentSession:
        assert self.ctx.providers is not None
        adapter = self.ctx.providers.get(provider)
        caps = await self.ctx.providers.capabilities(provider)
        if not caps.available:
            raise ApiError(503, "PROVIDER_UNAVAILABLE", caps.error or f"{provider} is unavailable")
        native_id: str | None = None
        if caps.eager_session_ids:
            try:
                native_id = await adapter.create_session((self.ctx.workspace_root or self.ctx.settings.workspace_root), display_name)
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
        exists = await adapter.session_exists((self.ctx.workspace_root or self.ctx.settings.workspace_root), native_id)
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
                sessions = await self.ctx.providers.get(name).list_sessions(self.ctx.workspace_root or self.ctx.settings.workspace_root)
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
    model: str | None = None
    variant: str | None = None
    inherit_from: str | None = None  # session id: copy its model/variant (same provider) unless model/variant are given


class SessionModelIn(BaseModel):
    model: str | None = None   # None/"" = provider default
    variant: str | None = None


@router.get("/providers/{provider}/models")
async def list_models(provider: str, ctx: AppContext = Depends(require_auth)):
    assert ctx.providers is not None
    try:
        adapter = ctx.providers.get(provider)
    except ProviderError as exc:
        raise ApiError(404, exc.code, exc.message)
    caps = await ctx.providers.capabilities(provider)
    models: list[dict[str, Any]] = []
    default: dict[str, str | None] = {"model": None, "variant": None}
    if caps.available:
        models, default = await asyncio.gather(adapter.list_models(),
                                               adapter.default_model(ctx.workspace_root or ctx.settings.workspace_root))
    if default.get("model"):
        # flag the provider's configured default in the list (and offer it even when the list lacks it)
        models = [dict(m, default=True) if m.get("id") == default["model"] else {k: v for k, v in m.items() if k != "default"}
                  for m in models]
        if not any(m.get("id") == default["model"] for m in models):
            models.insert(0, {"id": default["model"], "label": default["model"], "default": True})
    return {"provider": provider, "models": models, "variants": adapter.variants(), "can_select_model": caps.can_select_model,
            "default_model": default.get("model"), "default_variant": default.get("variant"),
            "note": None if models else "The provider did not return a model list; type a model id manually."}


@router.put("/agent-sessions/{session_id}/model")
def set_session_model(session_id: str, body: SessionModelIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Change the model used for future app-driven turns in this session. Past messages are unchanged."""
    return session_dict(SessionService(ctx).set_model(session_id, body.model, body.variant))


@router.get("/agent-sessions")
async def list_sessions(provider: str | None = Query(None), workspace: str | None = Query(None), ctx: AppContext = Depends(require_auth)):
    return await SessionService(ctx).list_all(provider)


@router.post("/agent-sessions", status_code=201)
async def create_session(body: SessionCreateIn, ctx: AppContext = Depends(require_auth_mutating)):
    svc = SessionService(ctx)
    if body.workspace_id and body.workspace_id != ctx.workspace_id:
        raise bad_request("unknown workspace", "WORKSPACE_NOT_FOUND")
    clean_model(body.model, body.variant)  # reject bad ids before creating anything
    source = svc.get(body.inherit_from) if body.inherit_from else None
    try:
        if body.native_id:
            row = await svc.link_existing(body.provider, body.native_id, body.display_name)
        else:
            row = await svc.create_for_provider(body.provider, body.display_name)
    except ProviderError as exc:
        raise ApiError(502, exc.code, exc.message)
    # an explicit model/variant wins; otherwise take over the one of the session the user came from
    model, variant = (body.model, body.variant) if (body.model or body.variant) else inherited_model(source, body.provider)
    if model or variant:
        row = svc.set_model(row.id, model, variant)
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
