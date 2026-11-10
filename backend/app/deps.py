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


def is_hub_owner(ctx: AppContext, request: Request) -> bool:
    """Signed in with GitHub as the account the server's gh CLI uses (the GitHub review hub's owner)."""
    from .github_hub import _hooks

    ident = (ctx.auth.identity_for_request(request) or "").lower()
    if not ident.startswith("github:"):
        return False
    owner = _hooks(ctx)[1]()
    return bool(owner) and ident == f"github:{owner}".lower()


def _guard_pull_requests(ctx: AppContext, request: Request) -> None:
    """Pull requests (from private repositories too) are the owner's: refuse any other login touching one."""
    from .documents import is_pr_path
    from .models import Document, Thread

    path = request.query_params.get("path") or ""
    pp = request.path_params
    touches = is_pr_path(path)
    if not touches and ("doc_id" in pp or "thread_id" in pp):
        with ctx.db.read() as s:
            doc = s.get(Document, pp["doc_id"]) if "doc_id" in pp else None
            if doc is None and "thread_id" in pp:
                t = s.get(Thread, pp["thread_id"])
                doc = s.get(Document, t.document_id) if t else None
            touches = bool(doc and is_pr_path(doc.path))
    if touches and not is_hub_owner(ctx, request):
        from .errors import ApiError

        raise ApiError(403, "NOT_GITHUB_OWNER", "pull requests are only shown to the server's owner signed in with GitHub")


def require_auth(request: Request, response: Response, ctx: AppContext = Depends(get_ctx)) -> AppContext:
    ctx.auth.authenticate_request(request, response, mutating=False)
    _guard_pull_requests(ctx, request)
    return scoped(ctx, request)


def require_auth_mutating(request: Request, response: Response, ctx: AppContext = Depends(get_ctx)) -> AppContext:
    ctx.auth.authenticate_request(request, response, mutating=True)
    _guard_pull_requests(ctx, request)
    return scoped(ctx, request)
