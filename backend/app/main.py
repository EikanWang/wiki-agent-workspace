"""FastAPI application factory and entry points."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import annotations, commits, diffs, documents, locks, previews, reviews, sessions, workspaces
from .agents.jobs import JobManager
from .agents.registry import ProviderRegistry
from .auth import AuthManager
from .config import Settings, ensure_state_dir, load_settings
from .db import Database
from .deps import AppContext, get_ctx, require_auth
from .errors import ApiError, api_error_handler, unauthorized, unexpected_error_handler
from .events import EventBus
from .gitinfo import git_info
from .idempotency import IdempotencyMiddleware
from .terminal import manager as terminal
from .terminal.tmux import Tmux

log = logging.getLogger("waw")


class TokenLogger(logging.Filter):
    """Defensive: never let the access token or cookie values reach log lines."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self.secrets:
            if s in msg:
                record.msg = msg.replace(s, "[redacted]")
                record.args = ()
                msg = record.msg
        return True


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("alembic").setLevel(logging.WARNING)
    flt = TokenLogger([settings.access_token, settings.server_secret])
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "waw"):
        logging.getLogger(name).addFilter(flt)
    # uvicorn access logs would include query strings; the API never carries tokens in URLs.


# ---------------------------------------------------------------------- auth/bootstrap routes

core = APIRouter()


class LoginIn(BaseModel):
    token: str


@core.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response, ctx: AppContext = Depends(get_ctx)):
    client_host = request.client.host if request.client else None
    ctx.auth.check_origin(request.headers, client_host, required=False)
    if not ctx.auth.verify_access_token(body.token):
        raise unauthorized("invalid access token")
    csrf, expires = ctx.auth.create_login(response, request)
    return {"csrf_token": csrf, "expires_at": expires}


@core.post("/auth/logout")
def logout(request: Request, response: Response, ctx: AppContext = Depends(get_ctx)):
    ctx.auth.authenticate_request(request, response, mutating=True)
    ctx.auth.logout(request, response)
    return {"ok": True}


@core.get("/health")
def health():
    return {"status": "ok"}


@core.get("/bootstrap")
async def bootstrap(request: Request, ctx: AppContext = Depends(require_auth)):
    assert ctx.providers and ctx.terminals and ctx.documents
    caps = await ctx.providers.summary()
    git = ctx.documents.git_summary(await asyncio.to_thread(git_info, ctx.documents.root))
    return {
        "workspace": {"id": ctx.workspace_id, "root": str(ctx.documents.root), "name": next((w["name"] for w in ctx.workspaces.list() if w["id"] == ctx.workspace_id), ctx.documents.root.name) if ctx.workspaces else ctx.documents.root.name, "git": git},
        "workspaces": ctx.workspaces.list() if ctx.workspaces else [],
        "default_provider": ctx.providers.default_provider(),
        "providers": caps,
        "terminals": ctx.terminals.list(),
        "terminal_info": ctx.terminals.info(),
        "settings": {
            "codex_sandbox": ctx.settings.provider("codex").sandbox,
            "opencode_mode": ctx.settings.provider("opencode").mode,
            "max_document_bytes": ctx.settings.max_document_bytes,
            "watcher_error": ctx.watcher.error if ctx.watcher else None,
        },
        "watcher": ctx.watcher.status() if ctx.watcher else None,
        "csrf_token": ctx.auth.csrf_token_for_request(request),
        "default_provider_warning": (None if ctx.providers.default_provider() else
                                     "No agent provider detected (opencode/codex). Reader and terminal work; set agents.default_provider in the config file to enable discussions."),
    }


@core.get("/events")
async def global_events(request: Request, ctx: AppContext = Depends(require_auth)):
    assert ctx.events
    q = ctx.events.subscribe("global")

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {ev.get('type', 'message')}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
        finally:
            ctx.events.unsubscribe("global", q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@core.get("/export")
def export_state(ctx: AppContext = Depends(require_auth)):
    """Backup: a tar.gz of the SQLite database and app-managed snapshots (no tokens)."""
    import io
    import tarfile

    buf = io.BytesIO()
    with ctx.db.write():  # hold the write lock for a consistent copy
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            db_copy = ctx.settings.state_dir / "backup.sqlite3"
            import sqlite3

            src = sqlite3.connect(str(ctx.settings.db_path))
            dst = sqlite3.connect(str(db_copy))
            src.backup(dst)
            dst.close()
            src.close()
            tar.add(db_copy, arcname="workspace.sqlite3")
            db_copy.unlink(missing_ok=True)
            if ctx.settings.snapshots_dir.exists():
                tar.add(ctx.settings.snapshots_dir, arcname="snapshots")
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/gzip",
                             headers={"Content-Disposition": 'attachment; filename="wiki-agent-workspace-backup.tar.gz"'})


# ---------------------------------------------------------------------- app factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    _configure_logging(settings)
    if not settings.access_token or not settings.server_secret:
        raise SystemExit("Access token/server secret missing. Run `scripts/setup.sh` first (or set WAW_ACCESS_TOKEN).")
    ensure_state_dir(settings)
    db = Database(settings.db_path)
    db.migrate()
    auth = AuthManager(settings, db)
    ctx = AppContext(settings=settings, db=db, auth=auth)
    bus = EventBus()
    ctx.events = bus
    reg = workspaces.WorkspaceRegistry(settings, db, bus)
    reg.load()
    ctx.workspaces = reg
    # the unscoped context points at the primary root (routes get a per-request scoped copy, see deps.scoped)
    primary = reg.primary
    ctx.documents, ctx.locks, ctx.watcher = primary.docs, primary.locks, primary.watcher
    ctx.workspace_id, ctx.workspace_root = primary.id, primary.root
    ctx.providers = ProviderRegistry(settings)
    ctx.jobs = JobManager(db, ctx.providers, bus, primary.root, settings.provider("codex").sandbox)
    ctx.jobs.registry = reg
    ctx.jobs.locks = primary.locks
    ctx.jobs.documents = primary.docs
    tmux = Tmux(settings)
    ctx.terminals = terminal.TerminalManager(db, tmux, bus, primary.root)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        n = ctx.jobs.mark_interrupted()
        if n:
            log.info("marked %d unfinished jobs as interrupted", n)
        rec = ctx.terminals.reconcile()
        log.info("terminal reconcile: %s", rec)
        try:
            caps = await ctx.providers.refresh()
            for name, c in caps.items():
                log.info("provider %s: available=%s version=%s %s", name, c.available, c.version, c.error or "")
        except Exception as exc:  # pragma: no cover
            log.warning("provider detection failed: %s", exc)
        reg.start_all()
        try:
            yield
        finally:
            await reg.stop_all()
            n = await ctx.jobs.shutdown()
            if n:
                log.info("marked %d running jobs as interrupted at shutdown", n)
            db.checkpoint()
            db.engine.dispose()

    app = FastAPI(title="Wiki Agent Workspace", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=400, content={"error": {"code": "VALIDATION_ERROR", "message": "malformed request",
                                                                "details": {"errors": json.loads(json.dumps(exc.errors(), default=str))}}})

    api = APIRouter(prefix="/api/v1")
    api.include_router(core)
    api.include_router(documents.router)
    api.include_router(annotations.router)
    api.include_router(sessions.router)
    api.include_router(previews.router)
    api.include_router(diffs.router)
    api.include_router(locks.router)
    api.include_router(commits.router)
    api.include_router(reviews.router)
    api.include_router(workspaces.router)
    api.include_router(terminal.router)
    app.include_router(api)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        # /raw may be embedded by the app's own PDF viewer; everything else refuses framing
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN" if request.url.path == "/api/v1/raw" else "DENY")
        if not request.url.path.startswith("/api/"):
            resp.headers.setdefault("Content-Security-Policy",
                                    "default-src 'self'; img-src 'self' data: blob: https: http:; style-src 'self' 'unsafe-inline'; "
                                    "script-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
        return resp

    app.add_middleware(IdempotencyMiddleware, db=db)

    _mount_frontend(app, settings)
    return app


def _frontend_dist(settings: Settings) -> Path | None:
    if settings.frontend_dist and settings.frontend_dist.exists():
        return settings.frontend_dist
    cand = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    return cand if cand.exists() else None


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    dist = _frontend_dist(settings)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str, request: Request):
        if full_path.startswith("api/"):
            raise ApiError(404, "NOT_FOUND", "unknown API route")
        if dist is None:
            return JSONResponse(status_code=503, content={"error": {"code": "FRONTEND_NOT_BUILT", "message": "frontend not built; run scripts/setup.sh (npm run build in frontend/)", "details": {}}})
        target = (dist / full_path).resolve() if full_path else dist / "index.html"
        if full_path and dist.resolve() in target.parents and target.is_file():
            return FileResponse(target)
        return FileResponse(dist / "index.html")


def run() -> None:  # pragma: no cover - process entry point
    import uvicorn

    settings = load_settings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.bind, port=settings.port, log_level=settings.log_level.lower(), proxy_headers=bool(settings.trusted_proxies),
                forwarded_allow_ips=",".join(settings.trusted_proxies) if settings.trusted_proxies else None, ws_ping_interval=20, ws_ping_timeout=20)


app: FastAPI | None = None

if __name__ == "__main__":  # pragma: no cover
    run()
