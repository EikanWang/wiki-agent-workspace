"""FastAPI dependencies and the application context object."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    from .terminal.manager import TerminalManager
    from .watcher import WorkspaceWatcher


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
    events: "EventBus | None" = None
    watcher: "WorkspaceWatcher | None" = None
    locks: "LockService | None" = None
    extra: dict[str, Any] = field(default_factory=dict)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def require_auth(request: Request, response: Response, ctx: AppContext = Depends(get_ctx)) -> AppContext:
    ctx.auth.authenticate_request(request, response, mutating=False)
    return ctx


def require_auth_mutating(request: Request, response: Response, ctx: AppContext = Depends(get_ctx)) -> AppContext:
    ctx.auth.authenticate_request(request, response, mutating=True)
    return ctx
