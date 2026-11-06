"""Multi-root windows: several wiki folders side by side, each fully isolated (VS Code "add folder to workspace").

Every root has its own `WorkspaceRuntime` — DocumentService (paths, documents, versions, Recent), LockService and a
filesystem watcher — and a row in `workspaces`. All per-wiki data (documents, discussions, agent sessions, locks,
reviews, viewed marks) is keyed by that row's id, so roots never see each other's state.

Roots come from the configuration (`[workspace] root` = the primary root, plus `[[workspaces]]` entries) and from the
UI ("Add folder"), which persists them in the database. Removing a folder from the window only deactivates it; its
data stays and comes back when the same folder is added again.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .db import Database, utcnow
from .deps import AppContext, require_auth, require_auth_mutating
from .documents import DocumentService
from .errors import ApiError, bad_request, conflict, not_found
from .events import EventBus
from .gitinfo import git_info
from .locks import LockService
from .models import AgentSession, Document, Preview, Review, ReviewComment, Thread, Workspace
from .watcher import WorkspaceWatcher

log = logging.getLogger("waw.workspaces")

HEADER = "x-workspace"


class WorkspaceRuntime:
    def __init__(self, db: Database, bus: EventBus, root: Path, snapshots_dir: Path, max_bytes: int, *, configured: bool):
        self.docs = DocumentService(db, root, snapshots_dir, max_bytes)
        self.id = self.docs.workspace_id
        self.root = self.docs.root
        self.locks = LockService(db, self.id)
        self.docs.locks = self.locks
        self.watcher = WorkspaceWatcher(self.root, self.docs, bus)
        self.configured = configured  # defined in config.toml (cannot be removed from the UI)


class WorkspaceRegistry:
    def __init__(self, settings, db: Database, bus: EventBus):
        self.settings = settings
        self.db = db
        self.bus = bus
        self.runtimes: dict[str, WorkspaceRuntime] = {}
        self.order: list[str] = []
        self.missing: list[dict[str, Any]] = []  # persisted roots whose folder no longer exists

    # ------------------------------------------------------------------ loading
    def load(self) -> None:
        roots: list[tuple[Path, str | None, bool]] = [(Path(self.settings.workspace_root), None, True)]
        roots.extend((Path(str(w["root"])).expanduser(), w.get("name") or None, True)
                     for w in getattr(self.settings, "workspaces", []) or [] if w.get("root"))
        for root, name, configured in roots:
            self._open(root, name, configured=configured)
        # folders added from the UI earlier
        with self.db.read() as s:
            rows = s.query(Workspace).filter(Workspace.active.is_(True)).order_by(Workspace.position, Workspace.created_at).all()
        for r in rows:
            if r.id in self.runtimes:
                continue
            if not Path(r.root_path).is_dir():
                self.missing.append({"id": r.id, "name": r.name or Path(r.root_path).name, "root": r.root_path})
                log.warning("workspace folder %s no longer exists; skipped (remove it in the UI or restore the folder)", r.root_path)
                continue
            self._open(Path(r.root_path), r.name, configured=False)

    def _open(self, root: Path, name: str | None, *, configured: bool) -> WorkspaceRuntime:
        resolved = root.resolve()
        for rt in self.runtimes.values():
            if rt.root == resolved:
                if configured:
                    rt.configured = True
                return rt
        rt = WorkspaceRuntime(self.db, self.bus, resolved, self.settings.snapshots_dir, self.settings.max_document_bytes, configured=configured)
        with self.db.write() as s:
            row = s.get(Workspace, rt.id)
            if row is not None:
                row.active = True
                if name:
                    row.name = name
                if row.position == 0 and self.order:
                    row.position = len(self.order)
        self.runtimes[rt.id] = rt
        self.order.append(rt.id)
        return rt

    # ------------------------------------------------------------------ access
    @property
    def primary(self) -> WorkspaceRuntime:
        return self.runtimes[self.order[0]]

    def get(self, workspace_id: str) -> WorkspaceRuntime:
        rt = self.runtimes.get(workspace_id)
        if rt is None:
            with self.db.read() as s:
                known = s.get(Workspace, workspace_id)
            if known is not None:
                raise ApiError(409, "WORKSPACE_NOT_OPEN", f"the folder {known.root_path} is not open in this window; add it again to use it",
                               {"workspace_id": workspace_id, "root": known.root_path})
            raise not_found("unknown workspace", "WORKSPACE_NOT_FOUND")
        return rt

    def for_root_path(self, path: Path) -> WorkspaceRuntime | None:
        p = path.resolve()
        best = None
        for rt in self.runtimes.values():
            if p == rt.root or rt.root in p.parents:
                if best is None or len(str(rt.root)) > len(str(best.root)):
                    best = rt
        return best

    def runtime_for_request(self, request: Request) -> WorkspaceRuntime:
        ws_id = self._workspace_of_object(request.path_params)
        if ws_id is None:
            ws_id = request.headers.get(HEADER) or request.query_params.get("workspace") or None
        return self.get(ws_id) if ws_id else self.primary

    def _workspace_of_object(self, params: dict[str, Any]) -> str | None:
        """Workspace of the object addressed by the URL, if any (object truth beats the header)."""
        with self.db.read() as s:
            if "doc_id" in params:
                d = s.get(Document, params["doc_id"])
                return d.workspace_id if d else None
            if "thread_id" in params:
                t = s.get(Thread, params["thread_id"])
                d = s.get(Document, t.document_id) if t else None
                return d.workspace_id if d else None
            if "preview_id" in params:
                p = s.get(Preview, params["preview_id"])
                d = s.get(Document, p.document_id) if p else None
                return d.workspace_id if d else None
            if "review_id" in params:
                r = s.get(Review, params["review_id"])
                return r.workspace_id if r else None
            if "comment_id" in params:
                c = s.get(ReviewComment, params["comment_id"])
                r = s.get(Review, c.review_id) if c else None
                return r.workspace_id if r else None
            if "session_id" in params:
                a = s.get(AgentSession, params["session_id"])
                return a.workspace_id if a else None
        return None

    # ------------------------------------------------------------------ lifecycle
    def start_all(self) -> None:
        for rid in self.order:
            self.runtimes[rid].watcher.start()

    async def stop_all(self) -> None:
        for rid in list(self.order):
            await self.runtimes[rid].watcher.stop()

    async def add(self, root: str, name: str | None) -> WorkspaceRuntime:
        p = Path(root).expanduser()
        if not p.is_absolute():
            raise bad_request("give an absolute path on the server (e.g. /home/me/other-wiki)", "INVALID_PATH")
        if not p.is_dir():
            raise not_found(f"{p} is not a directory on the server", "FOLDER_NOT_FOUND")
        existing = self.for_root_path(p)
        if existing is not None and existing.root == p.resolve():
            raise conflict("ALREADY_OPEN", f"{p} is already open in this window", workspace_id=existing.id)
        rt = self._open(p, name, configured=False)
        with self.db.write() as s:
            row = s.get(Workspace, rt.id)
            if row is not None:
                row.position = len(self.order) - 1
                row.name = name or row.name or rt.root.name
        rt.watcher.start()
        self.missing = [m for m in self.missing if m["id"] != rt.id]
        self.bus.publish("global", {"type": "workspaces_changed"})
        return rt

    async def remove(self, workspace_id: str) -> dict[str, Any]:
        missing = next((m for m in self.missing if m["id"] == workspace_id), None)
        if missing is not None:
            with self.db.write() as s:
                row = s.get(Workspace, workspace_id)
                if row is not None:
                    row.active = False
            self.missing.remove(missing)
            return {"id": workspace_id, "removed": True}
        rt = self.get(workspace_id)
        if rt.configured:
            raise conflict("CONFIGURED_WORKSPACE", "this folder is defined in config.toml; remove it there")
        if len(self.order) <= 1:
            raise conflict("LAST_WORKSPACE", "the window needs at least one folder")
        await rt.watcher.stop()
        del self.runtimes[workspace_id]
        self.order.remove(workspace_id)
        with self.db.write() as s:
            row = s.get(Workspace, workspace_id)
            if row is not None:
                row.active = False
        self.bus.publish("global", {"type": "workspaces_changed"})
        return {"id": workspace_id, "removed": True, "data_kept": True}

    def rename(self, workspace_id: str, name: str) -> dict[str, Any]:
        rt = self.get(workspace_id)
        with self.db.write() as s:
            row = s.get(Workspace, rt.id)
            if row is not None:
                row.name = name
        self.bus.publish("global", {"type": "workspaces_changed"})
        return self.describe(rt)

    def describe(self, rt: WorkspaceRuntime) -> dict[str, Any]:
        with self.db.read() as s:
            row = s.get(Workspace, rt.id)
        info = git_info(rt.root)
        return {
            "id": rt.id, "name": (row.name if row and row.name else rt.root.name), "root": str(rt.root),
            "primary": rt.id == self.order[0], "configured": rt.configured, "available": True,
            "git": rt.docs.git_summary(info), "watcher": rt.watcher.status(),
        }

    def list(self) -> list[dict[str, Any]]:
        out = [self.describe(self.runtimes[i]) for i in self.order]
        out += [{**m, "primary": False, "configured": False, "available": False, "git": None, "watcher": None} for m in self.missing]
        return out


# ---------------------------------------------------------------------- routes

router = APIRouter()


def _reg(ctx: AppContext) -> WorkspaceRegistry:
    assert ctx.workspaces is not None
    return ctx.workspaces


class AddFolderIn(BaseModel):
    root: str = Field(min_length=1, max_length=4096)
    name: str | None = Field(None, max_length=200)


class RenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@router.get("/workspaces")
def list_workspaces(ctx: AppContext = Depends(require_auth)):
    return {"workspaces": _reg(ctx).list(), "active": ctx.workspace_id}


@router.post("/workspaces", status_code=201)
async def add_workspace(body: AddFolderIn, ctx: AppContext = Depends(require_auth_mutating)):
    reg = _reg(ctx)
    return reg.describe(await reg.add(body.root, body.name))


@router.patch("/workspaces/{workspace_id}")
def rename_workspace(workspace_id: str, body: RenameIn, ctx: AppContext = Depends(require_auth_mutating)):
    return _reg(ctx).rename(workspace_id, body.name.strip())


@router.delete("/workspaces/{workspace_id}")
async def remove_workspace(workspace_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    return await _reg(ctx).remove(workspace_id)


@router.get("/fs/dirs")
def list_server_dirs(path: str = Query("~"), hidden: bool = Query(False), ctx: AppContext = Depends(require_auth)):
    """Folder picker for "Add folder": subdirectories of an absolute server path. The authenticated user already
    has a shell on this host, so this exposes nothing new; it only lists directory names."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise bad_request("absolute path required", "INVALID_PATH")
    try:
        p = p.resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        raise not_found(f"{path} does not exist", "FOLDER_NOT_FOUND")
    if not p.is_dir():
        raise bad_request(f"{p} is not a directory", "NOT_A_DIRECTORY")
    entries = []
    try:
        for e in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if not hidden and e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    entries.append({"name": e.name, "path": str(e), "is_git": (e / ".git").exists()})
            except OSError:
                continue
            if len(entries) >= 500:
                break
    except PermissionError:
        raise ApiError(403, "PERMISSION_DENIED", f"cannot list {p}")
    reg = _reg(ctx)
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "is_git": (p / ".git").exists(),
            "open_as": next((rt.id for rt in reg.runtimes.values() if rt.root == p), None), "entries": entries, "listed_at": utcnow().isoformat()}
