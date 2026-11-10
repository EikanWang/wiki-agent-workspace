"""Multi-root windows: several wiki folders side by side, each fully isolated (VS Code "add folder to workspace").

Every root has its own `WorkspaceRuntime` — DocumentService (paths, documents, versions, Recent), LockService and a
filesystem watcher — and a row in `workspaces`. All per-wiki data (documents, discussions, agent sessions, locks,
reviews, viewed marks) is keyed by that row's id, so roots never see each other's state.

Roots come from the configuration (`[workspace] root` = the primary root, plus `[[workspaces]]` entries) and from the
UI ("Add folder"), which persists them in the database. Removing a folder from the window only deactivates it; its
data stays and comes back when the same folder is added again.

Named workspaces (VS Code "workspaces", table `named_workspaces`) are named sets of folders; exactly one is current
and decides which folders the window shows. "Default" is created from the folders open before named workspaces
existed and always contains the folders defined in config.toml. Switching keeps the runtimes (and file watchers) of
folders both workspaces share, stops the others and starts the new ones. Terminals belong to a named workspace.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .db import Database, new_id, utcnow
from .deps import AppContext, require_auth, require_auth_mutating
from .documents import DocumentService
from .errors import ApiError, bad_request, conflict, not_found
from .events import EventBus
from .gitinfo import git_info
from .locks import LockService
from .models import AgentSession, Document, NamedWorkspace, Preview, Review, ReviewComment, Thread, Workspace
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
        self.current: str = ""  # id of the current named workspace
        self.on_switch = None  # callback(registry) after the folders of the window changed (main.py rewires defaults)

    # ------------------------------------------------------------------ loading
    def _config_roots(self) -> list[tuple[Path, str | None]]:
        roots: list[tuple[Path, str | None]] = [(Path(self.settings.workspace_root), None)]
        roots.extend((Path(str(w["root"])).expanduser(), w.get("name") or None)
                     for w in getattr(self.settings, "workspaces", []) or [] if w.get("root"))
        return roots

    def load(self) -> None:
        with self.db.read() as s:
            named = s.query(NamedWorkspace).all()
        if not named:
            # first start with named workspaces: "Default" = what the window showed so far
            for root, name in self._config_roots():
                self._open(root, name, configured=True)
            with self.db.read() as s:
                rows = s.query(Workspace).filter(Workspace.active.is_(True)).order_by(Workspace.position, Workspace.created_at).all()
            for r in rows:
                if r.id not in self.runtimes:
                    self._open_row(r)
            with self.db.write() as s:
                d = NamedWorkspace(id=new_id(), name="Default", is_default=True, current=True, last_opened_at=utcnow(),
                                   folders=json.dumps(self.order + [m["id"] for m in self.missing]))
                s.add(d)
                self.current = d.id
            return
        cur = next((n for n in named if n.current), None) or next((n for n in named if n.is_default), None) or named[0]
        self._load_named(cur)

    def _open_row(self, r: Workspace) -> WorkspaceRuntime | None:
        if not Path(r.root_path).is_dir():
            self.missing.append({"id": r.id, "name": r.name or Path(r.root_path).name, "root": r.root_path})
            log.warning("workspace folder %s no longer exists; skipped (remove it in the UI or restore the folder)", r.root_path)
            return None
        return self._open(Path(r.root_path), r.name, configured=False)

    def _load_named(self, nw: NamedWorkspace, keep: dict[str, WorkspaceRuntime] | None = None) -> None:
        """Make `nw` the window's folders. `keep`: runtimes already running that may be reused."""
        self.runtimes, self.order, self.missing = dict(keep or {}), [], []
        reuse = dict(self.runtimes)
        self.runtimes = {}
        ids: list[str] = json.loads(nw.folders or "[]")
        with self.db.read() as s:
            rows = {r.id: r for r in s.query(Workspace).filter(Workspace.id.in_(ids)).all()} if ids else {}

        def add(rt: WorkspaceRuntime) -> None:
            if rt.id not in self.runtimes:
                self.runtimes[rt.id] = rt
                self.order.append(rt.id)

        if nw.is_default:  # the folders of config.toml are part of Default — unless the user closed them
            closed = set(json.loads(nw.closed_roots or "[]"))
            for root, name in self._config_roots():
                if str(root.resolve()) in closed:
                    continue
                r = next((x for x in reuse.values() if x.root == root.resolve()), None)
                if r is not None:
                    r.configured = True
                    add(r)
                else:
                    self._open(root, name, configured=True)
        for i in ids:
            if i in self.runtimes:
                continue
            if i in reuse:
                reuse[i].configured = reuse[i].configured and bool(nw.is_default)
                add(reuse[i])
            elif i in rows:
                self._open_row(rows[i])
        if not self.order:
            # a window needs a folder: fall back to the primary folder of config.toml
            self._open(Path(self.settings.workspace_root), None, configured=False)
        for rt in self.runtimes.values():
            if not nw.is_default:
                rt.configured = False
        with self.db.write() as s:
            for row in s.query(Workspace).all():
                row.active = row.id in self.runtimes
                if row.id in self.order:
                    row.position = self.order.index(row.id)
        self.current = nw.id

    def _save_folders(self) -> None:
        """Remember the window's folders (in tree order, unavailable ones too) in the current named workspace."""
        with self.db.write() as s:
            nw = s.get(NamedWorkspace, self.current)
            if nw is not None:
                nw.folders = json.dumps(self.order + [m["id"] for m in self.missing])

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
        with self.db.write() as s:  # adding a closed config.toml folder again reopens it for good
            nw = s.get(NamedWorkspace, self.current)
            if nw is not None:
                closed = [c for c in json.loads(nw.closed_roots or "[]") if c != str(rt.root)]
                nw.closed_roots = json.dumps(closed)
        self._save_folders()
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
            self._save_folders()
            return {"id": workspace_id, "removed": True}
        rt = self.get(workspace_id)
        if len(self.order) <= 1:
            raise conflict("LAST_WORKSPACE", "the window needs at least one folder")
        was_primary = self.order[0] == workspace_id
        await rt.watcher.stop()
        del self.runtimes[workspace_id]
        self.order.remove(workspace_id)
        with self.db.write() as s:
            row = s.get(Workspace, workspace_id)
            if row is not None:
                row.active = False
            if rt.configured:  # a config.toml folder: remember it is closed, so it does not come back at startup
                nw = s.get(NamedWorkspace, self.current)
                if nw is not None:
                    closed = set(json.loads(nw.closed_roots or "[]")) | {str(rt.root)}
                    nw.closed_roots = json.dumps(sorted(closed))
        self._save_folders()
        if was_primary and self.on_switch:
            self.on_switch(self)  # process-wide defaults (jobs, terminals) follow the new first folder
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

    # ------------------------------------------------------------------ named workspaces
    def named_list(self) -> dict[str, Any]:
        with self.db.read() as s:
            rows = s.query(NamedWorkspace).order_by(NamedWorkspace.is_default.desc(), NamedWorkspace.created_at).all()
            folders = {w.id: w for w in s.query(Workspace).all()}
        out = []
        for n in rows:
            ids = json.loads(n.folders or "[]")
            out.append({
                "id": n.id, "name": n.name, "current": n.id == self.current, "is_default": n.is_default,
                "folders": [{"id": i, "name": (folders[i].name or Path(folders[i].root_path).name), "root": folders[i].root_path}
                            for i in ids if i in folders],
                "last_opened_at": n.last_opened_at.isoformat() if n.last_opened_at else None,
            })
        return {"workspaces": out, "current": self.current}

    def _folder_row(self, root: str) -> str:
        """Id of the folder row for an absolute server path (created if new), without opening it."""
        p = Path(root).expanduser()
        if not p.is_absolute():
            raise bad_request("give an absolute path on the server (e.g. /home/me/other-wiki)", "INVALID_PATH")
        if not p.is_dir():
            raise not_found(f"{p} is not a directory on the server", "FOLDER_NOT_FOUND")
        resolved = str(p.resolve())
        with self.db.write() as s:
            row = s.query(Workspace).filter_by(root_path=resolved).one_or_none()
            if row is None:
                row = Workspace(id=new_id(), root_path=resolved, active=False, name=p.resolve().name)
                s.add(row)
            return row.id

    async def switch(self, named_id: str) -> dict[str, Any]:
        with self.db.read() as s:
            nw = s.get(NamedWorkspace, named_id)
        if nw is None:
            raise not_found("unknown workspace", "NAMED_WORKSPACE_NOT_FOUND")
        if named_id != self.current:
            target = set(json.loads(nw.folders or "[]"))
            if nw.is_default:
                target |= {rt.id for rt in self.runtimes.values() if rt.root in {r.resolve() for r, _ in self._config_roots()}}
            keep = {i: rt for i, rt in self.runtimes.items() if i in target}
            for i, rt in list(self.runtimes.items()):
                if i not in keep:
                    await rt.watcher.stop()
            self._load_named(nw, keep)
            for i in self.order:
                if i not in keep:
                    self.runtimes[i].watcher.start()
            with self.db.write() as s:
                for row in s.query(NamedWorkspace).all():
                    row.current = row.id == named_id
                    if row.id == named_id:
                        row.last_opened_at = utcnow()
            self._save_folders()
            if self.on_switch:
                self.on_switch(self)
            log.info("switched to workspace %s (%d folders)", nw.name, len(self.order))
            self.bus.publish("global", {"type": "named_workspace_changed", "id": named_id})
            self.bus.publish("global", {"type": "workspaces_changed"})
        return self.named_list()

    async def create_named(self, name: str, root: str | None, copy_current: bool) -> dict[str, Any]:
        """New named workspace with a first folder (or the current window's folders) and switch to it."""
        ids: list[str] = []
        if copy_current:
            ids = list(self.order) + [m["id"] for m in self.missing]
        if root:
            fid = self._folder_row(root)
            if fid not in ids:
                ids.append(fid)
        if not ids:
            raise bad_request("choose a folder for the new workspace", "NO_FOLDER")
        with self.db.write() as s:
            if s.query(NamedWorkspace).filter_by(name=name).first() is not None:
                raise conflict("NAME_TAKEN", f"a workspace named {name!r} already exists")
            nw = NamedWorkspace(id=new_id(), name=name, folders=json.dumps(ids))
            s.add(nw)
            nid = nw.id
        return await self.switch(nid)

    def rename_named(self, named_id: str, name: str) -> dict[str, Any]:
        with self.db.write() as s:
            nw = s.get(NamedWorkspace, named_id)
            if nw is None:
                raise not_found("unknown workspace", "NAMED_WORKSPACE_NOT_FOUND")
            other = s.query(NamedWorkspace).filter(NamedWorkspace.name == name, NamedWorkspace.id != named_id).first()
            if other is not None:
                raise conflict("NAME_TAKEN", f"a workspace named {name!r} already exists")
            nw.name = name
        self.bus.publish("global", {"type": "named_workspaces_changed"})
        return self.named_list()

    def delete_named(self, named_id: str) -> dict[str, Any]:
        """Delete a named workspace (not the current one, not Default). Its folders and their data are untouched;
        its terminal tabs move to Default (their shells keep running)."""
        from .models import Terminal

        with self.db.write() as s:
            nw = s.get(NamedWorkspace, named_id)
            if nw is None:
                raise not_found("unknown workspace", "NAMED_WORKSPACE_NOT_FOUND")
            if nw.is_default:
                raise conflict("DEFAULT_WORKSPACE", "the Default workspace cannot be deleted")
            if named_id == self.current:
                raise conflict("CURRENT_WORKSPACE", "switch to another workspace before deleting this one")
            for t in s.query(Terminal).filter_by(named_workspace_id=named_id).all():
                t.named_workspace_id = None
            s.delete(nw)
        self.bus.publish("global", {"type": "named_workspaces_changed"})
        return self.named_list()

    def current_is_default(self) -> bool:
        with self.db.read() as s:
            nw = s.get(NamedWorkspace, self.current)
            return bool(nw and nw.is_default)


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


class NamedCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    root: str | None = Field(None, max_length=4096)  # first folder (absolute server path)
    copy_current: bool = False  # start with the folders of the current window


class NamedRenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.get("/named-workspaces")
def list_named(ctx: AppContext = Depends(require_auth)):
    return _reg(ctx).named_list()


@router.post("/named-workspaces", status_code=201)
async def create_named(body: NamedCreateIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await _reg(ctx).create_named(body.name.strip(), body.root, body.copy_current)


@router.post("/named-workspaces/{named_id}/open")
async def open_named(named_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    return await _reg(ctx).switch(named_id)


@router.patch("/named-workspaces/{named_id}")
def rename_named(named_id: str, body: NamedRenameIn, ctx: AppContext = Depends(require_auth_mutating)):
    return _reg(ctx).rename_named(named_id, body.name.strip())


@router.delete("/named-workspaces/{named_id}")
def delete_named(named_id: str, ctx: AppContext = Depends(require_auth_mutating)):
    return _reg(ctx).delete_named(named_id)


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


class MkdirIn(BaseModel):
    parent: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=255)
    git: bool = False


@router.post("/fs/mkdir", status_code=201)
def make_server_dir(body: MkdirIn, ctx: AppContext = Depends(require_auth_mutating)):
    """"New folder" in the folder picker: create a directory on the server (optionally a Git repository), e.g. for a
    new wiki. Like the picker itself this needs nothing the logged-in user's shell could not do already."""
    import subprocess

    name = body.name.strip()
    if not name or name in (".", "..") or "/" in name or "\x00" in name:
        raise bad_request("a folder name, without slashes", "INVALID_NAME")
    parent = Path(body.parent).expanduser()
    if not parent.is_absolute():
        raise bad_request("absolute path required", "INVALID_PATH")
    if not parent.is_dir():
        raise not_found(f"{parent} is not a directory on the server", "FOLDER_NOT_FOUND")
    target = parent / name
    if target.exists() or target.is_symlink():
        raise conflict("ALREADY_EXISTS", f"{target} already exists", path=str(target))
    try:
        target.mkdir()
    except PermissionError:
        raise ApiError(403, "PERMISSION_DENIED", f"cannot create a folder in {parent}")
    git_error = None
    if body.git:
        p = subprocess.run(["git", "init", "-q"], cwd=target, capture_output=True, text=True, timeout=30, check=False)
        if p.returncode != 0:
            git_error = (p.stderr or p.stdout).strip()[:300] or "git init failed"
        else:
            (target / ".gitignore").write_text(".waw-tmp-*\n")  # the app's atomic-write temporaries
    return {"path": str(target.resolve()), "is_git": (target / ".git").exists(), "git_error": git_error}


# ---------------------------------------------------------------------- clone a repository (add a project by URL)

_clones: dict[str, dict[str, Any]] = {}
_URL = __import__("re").compile(r"^(https?://|ssh://|git://|file://)[^\s]+$|^[\w.-]+@[\w.-]+:[^\s]+$")


def _repo_name(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


class CloneIn(BaseModel):
    url: str = Field(min_length=3, max_length=2000)
    parent: str = Field(min_length=1, max_length=4096)
    name: str | None = Field(None, max_length=255)
    shallow: bool = False


@router.post("/fs/clone", status_code=202)
def clone_repository(body: CloneIn, ctx: AppContext = Depends(require_auth_mutating)):
    """Clone a Git repository (https / ssh / git@host:path) into a new folder on the server, in the background.
    Uses the server user's own Git credentials (e.g. `gh` for GitHub). Poll GET /fs/clone/{id}; when it is done,
    add the folder with POST /workspaces."""
    import subprocess
    import threading
    import time as _time

    from .db import new_id

    url = body.url.strip()
    if not _URL.match(url) or url.startswith("-"):
        raise bad_request("give a Git URL, e.g. https://github.com/owner/repo.git or git@github.com:owner/repo.git", "INVALID_URL")
    name = (body.name or "").strip() or _repo_name(url)
    if not name or name in (".", "..") or "/" in name or name.startswith("-"):
        raise bad_request("invalid folder name", "INVALID_NAME")
    parent = Path(body.parent).expanduser()
    if not parent.is_absolute() or not parent.is_dir():
        raise not_found(f"{parent} is not a directory on the server", "FOLDER_NOT_FOUND")
    target = parent / name
    if target.exists():
        raise conflict("ALREADY_EXISTS", f"{target} already exists", path=str(target))
    if any(c["target"] == str(target) and c["state"] == "running" for c in _clones.values()):
        raise conflict("ALREADY_CLONING", f"{target} is being cloned already")
    cid = new_id()
    job: dict[str, Any] = {"id": cid, "url": url, "target": str(target), "state": "running", "progress": "Starting…", "error": None,
                           "started": _time.time()}
    _clones[cid] = job
    args = ["git", "clone", "--progress"] + (["--depth", "1"] if body.shallow else []) + ["--", url, str(target)]

    def run() -> None:
        import os
        import re as _re

        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")}
        try:
            p = subprocess.Popen(args, cwd=str(parent), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True, bufsize=1)
            tail: list[str] = []
            buf = ""
            assert p.stderr is not None
            while True:
                ch = p.stderr.read(1)
                if not ch:
                    break
                if ch in "\r\n":
                    if buf.strip():
                        job["progress"] = buf.strip()[:200]
                        tail = (tail + [buf.strip()])[-6:]
                    buf = ""
                else:
                    buf += ch
            rc = p.wait()
            if rc == 0 and target.is_dir():
                job.update(state="done", progress="Cloned", path=str(target.resolve()))
                log.info("cloned %s into %s", url, target)
            else:
                msg = " ".join(ln for ln in tail if not _re.match(r"^(Cloning into|remote: (Enumerating|Counting|Compressing))", ln))
                job.update(state="failed", error=(msg or f"git clone failed (exit {rc})")[-400:])
                import shutil as _sh

                if target.exists():
                    _sh.rmtree(target, ignore_errors=True)  # no half-cloned folder left behind
        except Exception as exc:  # pragma: no cover
            job.update(state="failed", error=f"{exc.__class__.__name__}: {exc}")

    threading.Thread(target=run, daemon=True, name=f"clone-{cid[:8]}").start()
    return job


@router.get("/fs/clone/{clone_id}")
def clone_status(clone_id: str, ctx: AppContext = Depends(require_auth)):
    job = _clones.get(clone_id)
    if job is None:
        raise not_found("unknown clone", "CLONE_NOT_FOUND")
    return job

