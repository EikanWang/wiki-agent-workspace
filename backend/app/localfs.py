"""Local files: browse and edit any folder or file on the server like a file manager (no wiki features).

Absolute paths of the server's OS user — the same rights the terminal already gives. The opened folders are kept in
kv_settings `local_roots`. Writes are conditional on the content hash the editor loaded (409 FILE_CHANGED), written
atomically, keeping the file's mode.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError, bad_request, conflict, not_found
from .kv import kv_get, kv_put

router = APIRouter()
MAX_TEXT = 10 * 1024 * 1024
MAX_RAW = 200 * 1024 * 1024
MAX_LIST = 5000
KEY = "local_roots"
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
               ".svg": "image/svg+xml", ".bmp": "image/bmp", ".ico": "image/x-icon", ".pdf": "application/pdf"}


def _abs(path: str) -> Path:
    if not path or "\0" in path:
        raise bad_request("give a path", "INVALID_PATH")
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise bad_request("give an absolute path on the server", "INVALID_PATH")
    return Path(os.path.normpath(p))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _roots(ctx: AppContext) -> list[dict[str, Any]]:
    v = kv_get(ctx, KEY, [])
    out = []
    for r in v if isinstance(v, list) else []:
        if isinstance(r, str):
            p = Path(r)
            out.append({"path": r, "name": p.name or r, "exists": p.is_dir()})
    return out


class RootsIn(BaseModel):
    roots: list[str] = Field(default_factory=list, max_length=100)


@router.get("/local/roots")
def get_roots(ctx: AppContext = Depends(require_auth)):
    return {"roots": _roots(ctx), "home": str(Path.home())}


@router.put("/local/roots")
def put_roots(body: RootsIn, ctx: AppContext = Depends(require_auth_mutating)):
    clean: list[str] = []
    for r in body.roots:
        p = _abs(r)
        if not p.is_dir():
            raise not_found(f"{p} is not a folder", "FOLDER_NOT_FOUND")
        if str(p) not in clean:
            clean.append(str(p))
    kv_put(ctx, KEY, clean)
    return {"roots": _roots(ctx), "home": str(Path.home())}


@router.get("/local/list")
def list_dir(path: str = Query(...), hidden: bool = Query(True), ctx: AppContext = Depends(require_auth)):
    p = _abs(path)
    if not p.is_dir():
        raise not_found(f"{p} is not a folder", "FOLDER_NOT_FOUND")
    items: list[dict[str, Any]] = []
    try:
        entries = sorted(os.scandir(p), key=lambda e: e.name.lower())
    except PermissionError:
        raise ApiError(403, "PERMISSION_DENIED", f"cannot list {p}")
    for e in entries:
        if not hidden and e.name.startswith("."):
            continue
        try:
            is_dir = e.is_dir()
            st = e.stat() if not is_dir else None
        except OSError:
            continue
        items.append({"name": e.name, "path": str(p / e.name), "dir": is_dir, "size": st.st_size if st else 0,
                      "mtime": st.st_mtime if st else None, "link": e.is_symlink()})
        if len(items) >= MAX_LIST:
            break
    items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "items": items, "truncated": len(items) >= MAX_LIST}


@router.get("/local/file")
def read_file(path: str = Query(...), ctx: AppContext = Depends(require_auth)):
    p = _abs(path)
    if not p.is_file():
        raise not_found(f"{p} is not a file", "FILE_NOT_FOUND")
    size = p.stat().st_size
    if size > MAX_TEXT:
        return {"path": str(p), "size": size, "binary": True, "too_large": True, "text": None, "sha256": None,
                "writable": os.access(p, os.W_OK)}
    try:
        data = p.read_bytes()
    except PermissionError:
        raise ApiError(403, "PERMISSION_DENIED", f"cannot read {p}")
    text = None
    binary = b"\0" in data[:8000]
    if not binary:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            binary = True
    return {"path": str(p), "size": size, "binary": binary, "too_large": False, "text": text, "sha256": _sha(data),
            "writable": os.access(p, os.W_OK)}


class WriteIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    text: str = Field(max_length=MAX_TEXT)
    base_sha256: str | None = Field(None, max_length=64)
    create: bool = False


@router.put("/local/file")
def write_file(body: WriteIn, ctx: AppContext = Depends(require_auth_mutating)):
    p = _abs(body.path)
    exists = p.is_file()
    if body.create:
        if p.exists():
            raise conflict("ALREADY_EXISTS", f"{p} already exists")
    else:
        if not exists:
            raise conflict("FILE_CHANGED", f"{p} was deleted on disk", current_sha256=None)
        cur = _sha(p.read_bytes())
        if cur != body.base_sha256:
            raise conflict("FILE_CHANGED", f"{p.name} changed on disk since it was opened", current_sha256=cur)
    if p.is_dir():
        raise bad_request("that is a folder", "INVALID_PATH")
    data = body.text.encode("utf-8")
    mode = (p.stat().st_mode & 0o7777) if exists else None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.waw-tmp")
        tmp.write_bytes(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, p)
    except PermissionError:
        raise ApiError(403, "PERMISSION_DENIED", f"cannot write {p}")
    return {"path": str(p), "sha256": _sha(data), "size": len(data)}


class PathIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    new_path: str | None = Field(None, max_length=4096)


@router.post("/local/mkdir")
def mkdir(body: PathIn, ctx: AppContext = Depends(require_auth_mutating)):
    p = _abs(body.path)
    if p.exists():
        raise conflict("ALREADY_EXISTS", f"{p} already exists")
    try:
        p.mkdir(parents=True)
    except PermissionError:
        raise ApiError(403, "PERMISSION_DENIED", f"cannot create {p}")
    return {"path": str(p)}


@router.post("/local/rename")
def rename(body: PathIn, ctx: AppContext = Depends(require_auth_mutating)):
    a = _abs(body.path)
    b = _abs(body.new_path or "")
    if not a.exists() and not a.is_symlink():
        raise not_found(f"{a} not found", "FILE_NOT_FOUND")
    if b.exists():
        raise conflict("ALREADY_EXISTS", f"{b} already exists")
    try:
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(a), str(b))
    except PermissionError:
        raise ApiError(403, "PERMISSION_DENIED", f"cannot move {a}")
    return {"path": str(b)}


@router.post("/local/delete")
def delete(body: PathIn, ctx: AppContext = Depends(require_auth_mutating)):
    p = _abs(body.path)
    if str(p) in ("/", str(Path.home())):
        raise bad_request("refusing to delete this folder", "INVALID_PATH")
    try:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            p.unlink()
        else:
            raise not_found(f"{p} not found", "FILE_NOT_FOUND")
    except PermissionError:
        raise ApiError(403, "PERMISSION_DENIED", f"cannot delete {p}")
    return {"deleted": str(p)}


class StatIn(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=200)


@router.post("/local/stat")
def stat(body: StatIn, ctx: AppContext = Depends(require_auth)):
    """Current content hashes of open files (None = gone or too large): clean tabs follow changes on disk."""
    out: dict[str, str | None] = {}
    for x in body.paths:
        p = _abs(x)
        try:
            out[x] = _sha(p.read_bytes()) if p.is_file() and p.stat().st_size <= MAX_TEXT else None
        except OSError:
            out[x] = None
    return {"sha256": out}


@router.get("/local/raw")
def raw(path: str = Query(...), ctx: AppContext = Depends(require_auth)):
    p = _abs(path)
    if not p.is_file():
        raise not_found(f"{p} is not a file", "FILE_NOT_FOUND")
    if p.stat().st_size > MAX_RAW:
        raise ApiError(413, "FILE_TOO_LARGE", "file too large")
    ctype = IMAGE_TYPES.get(p.suffix.lower())
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if ctype == "image/svg+xml":
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    if not ctype:
        headers["Content-Disposition"] = f'attachment; filename="{p.name}"'
    return Response(content=p.read_bytes(), media_type=ctype or "application/octet-stream", headers=headers)
