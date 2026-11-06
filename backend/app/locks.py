"""Read-only locks on files and folders (user-declared).

A lock forbids every write the app performs or drives on the covered paths:
- preview Apply, relink, delete-history → 423 PATH_LOCKED
- app-driven agent turns: the agent is told the file is locked; Codex runs with the `read-only` sandbox;
  OpenCode CLI cannot be sandboxed by the app, so if the turn nevertheless changed a locked file the change is
  reported as a `LOCK_VIOLATION` event (never reverted silently).
The user's terminal is never restricted (DESIGN §2.2); locks are an application-level guard.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .db import Database, iso, new_id
from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError, not_found
from .models import Lock

PATH_LOCKED = "PATH_LOCKED"


class LockService:
    def __init__(self, db: Database, workspace_id: str):
        self.db = db
        self.workspace_id = workspace_id

    def list(self) -> list[dict[str, Any]]:
        with self.db.read() as s:
            rows = s.query(Lock).filter_by(workspace_id=self.workspace_id).order_by(Lock.path).all()
            return [self._dict(r) for r in rows]

    @staticmethod
    def _dict(r: Lock) -> dict[str, Any]:
        return {"id": r.id, "path": r.path, "is_dir": r.is_dir, "reason": r.reason, "created_at": iso(r.created_at)}

    def covering(self, rel: str) -> dict[str, Any] | None:
        """The most specific lock covering `rel` (a file or folder path), or None."""
        with self.db.read() as s:
            rows = s.query(Lock).filter_by(workspace_id=self.workspace_id).all()
        best: Lock | None = None
        for r in rows:
            if r.path == rel or (r.is_dir and (r.path == "" or rel.startswith(r.path + "/"))):
                if best is None or len(r.path) > len(best.path):
                    best = r
        return self._dict(best) if best else None

    def covered_paths(self, paths: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for p in paths:
            c = self.covering(p)
            if c:
                out[p] = c
        return out

    def assert_writable(self, rel: str, action: str) -> None:
        c = self.covering(rel)
        if c:
            raise ApiError(423, PATH_LOCKED, f"{rel or 'the workspace'} is locked ({'folder ' + c['path'] if c['is_dir'] and c['path'] != rel else 'file'}"
                           f"{' — ' + c['reason'] if c.get('reason') else ''}); {action} is not allowed. Unlock it first.",
                           {"lock": c})

    def add(self, rel: str, is_dir: bool, reason: str | None) -> dict[str, Any]:
        with self.db.write() as s:
            existing = s.query(Lock).filter_by(workspace_id=self.workspace_id, path=rel).one_or_none()
            if existing:
                existing.reason = reason
                existing.is_dir = is_dir
                return self._dict(existing)
            row = Lock(id=new_id(), workspace_id=self.workspace_id, path=rel, is_dir=is_dir, reason=reason)
            s.add(row)
            s.flush()
            return self._dict(row)

    def remove(self, rel: str) -> dict[str, Any]:
        with self.db.write() as s:
            row = s.query(Lock).filter_by(workspace_id=self.workspace_id, path=rel).one_or_none()
            if row is None:
                raise not_found("no lock on that path", "LOCK_NOT_FOUND")
            s.delete(row)
        return {"path": rel, "locked": False}


class LockIn(BaseModel):
    path: str = ""
    reason: str | None = Field(None, max_length=500)


router = APIRouter()


def _svc(ctx: AppContext) -> LockService:
    assert ctx.locks is not None
    return ctx.locks


@router.get("/locks")
def list_locks(ctx: AppContext = Depends(require_auth)):
    return {"locks": _svc(ctx).list()}


@router.get("/locks/check")
def check_lock(path: str = Query(""), ctx: AppContext = Depends(require_auth)):
    assert ctx.documents
    rel = ctx.documents.normalize_rel(path) if path else ""
    return {"path": rel, "lock": _svc(ctx).covering(rel)}


@router.post("/locks", status_code=201)
def add_lock(body: LockIn, ctx: AppContext = Depends(require_auth_mutating)):
    assert ctx.documents
    rel = ctx.documents.normalize_rel(body.path) if body.path else ""
    if rel:
        _, p = ctx.documents.resolve(rel)
        is_dir = p.is_dir()
    else:
        is_dir = True
    return _svc(ctx).add(rel, is_dir, body.reason)


@router.delete("/locks")
def remove_lock(path: str = Query(""), ctx: AppContext = Depends(require_auth_mutating)):
    assert ctx.documents
    rel = ctx.documents.normalize_rel(path) if path else ""
    return _svc(ctx).remove(rel)
