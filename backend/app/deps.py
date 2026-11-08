"""FastAPI dependencies and the application context object."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, Request, Response

from .auth import AuthManager
from .config import Settings
from .db import Database

if TYPE_CHECKING:  # pragma: no cover
    from .agents.jobs import JobManager
    from .agents.registry import ProviderRegistry
    from .documents import DocumentService
    from .events import EventBus
    from .locks import LockService
    from .office import OfficeRenderer
    from .terminal.manager import TerminalManager
    from .watcher import WorkspaceWatcher
    from .workspaces import WorkspaceRegistry


@dataclass
class AppContext:
    settings: Settings
    db: Database
    auth: AuthManager
    workspace_id: str = ""
    documents: "DocumentService | None" = None
    providers: "ProviderRegistry | None" = None
    jobs: "JobManager | None" = None
    terminals: "TerminalManager | None" = None
    office: "OfficeRenderer | None" = None
    events: "EventBus | None" = None
    watcher: "WorkspaceWatcher | None" = None
    locks: "LockService | None" = None
    workspace_root: Path | None = None
    workspaces: "WorkspaceRegistry | None" = None
    extra: dict[str, Any] = field(default_factory=dict)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def scoped(ctx: AppContext, request: Request) -> AppContext:
    """A shallow copy of the context bound to one root folder.

    The root is taken from the object the route addresses (document / thread / preview / review / session id),
    so a request can never read a file of one root through another root's path; otherwise from the `X-Workspace`
    header or `?workspace=` query parameter; otherwise the first (primary) root."""
    reg = ctx.workspaces
    if reg is None:
        return ctx
    rt = reg.runtime_for_request(request)
    return replace(ctx, documents=rt.docs, locks=rt.locks, watcher=rt.watcher, workspace_id=rt.id, workspace_root=rt.root)


def require_auth(request: Request, response: Response, ctx: AppContext = Depends(get_ctx)) -> AppContext:
    ctx.auth.authenticate_request(request, response, mutating=False)
    return scoped(ctx, request)


def require_auth_mutating(request: Request, response: Response, ctx: AppContext = Depends(get_ctx)) -> AppContext:
    ctx.auth.authenticate_request(request, response, mutating=True)
    return scoped(ctx, request)
