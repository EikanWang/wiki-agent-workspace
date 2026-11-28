"""Local files: browse and edit any folder or file on the server — or on a machine of the Machines view — like a file
manager (no wiki features).

Absolute paths of the server's OS user — the same rights the terminal already gives; `<machine>:/path` is a path on
that machine, reached over ssh as its machine user (remotefs). The opened folders are kept in kv_settings
`local_roots`. Writes are conditional on the content hash the editor loaded (409 FILE_CHANGED), written atomically,
keeping the file's mode.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError, bad_request, conflict, not_found
from .kv import kv_get, kv_put
from .remotefs import MAX_REMOTE_RAW, Remote, gather_roots, split

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


def _stored(ctx: AppContext) -> list[str]:
    v = kv_get(ctx, KEY, [])
    return [r for r in v if isinstance(r, str)] if isinstance(v, list) else []


async def _roots(ctx: AppContext) -> list[dict[str, Any]]:
    stored = _stored(ctx)
    remote = await gather_roots(ctx, stored)
    out = []
    for r in stored:
        s = split(r)
        if s:
            name = s[1].rstrip("/").rsplit("/", 1)[-1] or s[1]
            out.append({"path": r, "name": name, "machine": s[0], **remote.get(r, {"exists": False})})
        else:
            p = Path(r)
            out.append({"path": r, "name": p.name or r, "machine": None, "exists": p.is_dir()})
    return out


async def _where(ctx: AppContext, path: str) -> tuple[Remote | None, str]:
    """(the machine's file system or None for the server, the absolute path there)."""
    s = split(path)
    if s is None:
        return None, str(_abs(path))
    fs = Remote(ctx, s[0])
    return fs, await fs.abs(s[1])


class RootsIn(BaseModel):
    roots: list[str] = Field(default_factory=list, max_length=100)


@router.get("/local/roots")
async def get_roots(ctx: AppContext = Depends(require_auth)):
    return {"roots": await _roots(ctx), "home": str(Path.home())}


@router.put("/local/roots")
async def put_roots(body: RootsIn, ctx: AppContext = Depends(require_auth_mutating)):
    """The opened folders. A folder newly added must be there; the ones kept are not checked again (closing a folder
    works while a machine is unreachable)."""
    before = set(_stored(ctx))
    clean: list[str] = []
    for r in body.roots:
        if r in before:
            key = r
        else:
            fs, p = await _where(ctx, r)
            key = fs.name(p) if fs else p
            there = (await fs.dirs_exist([p]))[0] if fs else Path(p).is_dir()
            if not there:
                raise not_found(f"{key} is not a folder", "FOLDER_NOT_FOUND")
        if key not in clean:
            clean.append(key)
    kv_put(ctx, KEY, clean)
    return {"roots": await _roots(ctx), "home": str(Path.home())}


@router.get("/local/list")
async def list_dir(path: str = Query(...), hidden: bool = Query(True), ctx: AppContext = Depends(require_auth)):
    fs, p = await _where(ctx, path)
    if fs:
        return await fs.list_dir(p, hidden, MAX_LIST)
    return await run_in_threadpool(_list_local, Path(p), hidden)


def _list_local(p: Path, hidden: bool) -> dict[str, Any]:
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


def _text_of(path: str, size: int, data: bytes | None, writable: bool) -> dict[str, Any]:
    if data is None:
        return {"path": path, "size": size, "binary": True, "too_large": True, "text": None, "sha256": None, "writable": writable}
    text = None
    binary = b"\0" in data[:8000]
    if not binary:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            binary = True
    return {"path": path, "size": size, "binary": binary, "too_large": False, "text": text, "sha256": _sha(data), "writable": writable}


@router.get("/local/file")
async def read_file(path: str = Query(...), ctx: AppContext = Depends(require_auth)):
    fs, p = await _where(ctx, path)
    if fs:
        size, writable, data = await fs.read_bytes(p, MAX_TEXT)
        return _text_of(fs.name(p), size, data, writable)
    return await run_in_threadpool(_read_local, Path(p))


def _read_local(p: Path) -> dict[str, Any]:
    if not p.is_file():
        raise not_found(f"{p} is not a file", "FILE_NOT_FOUND")
    size = p.stat().st_size
    if size > MAX_TEXT:
        return _text_of(str(p), size, None, os.access(p, os.W_OK))
    try:
        data = p.read_bytes()
    except PermissionError:
        raise ApiError(403, "PERMISSION_DENIED", f"cannot read {p}")
    return _text_of(str(p), size, data, os.access(p, os.W_OK))


class WriteIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    text: str = Field(max_length=MAX_TEXT)
    base_sha256: str | None = Field(None, max_length=64)
    create: bool = False


@router.put("/local/file")
async def write_file(body: WriteIn, ctx: AppContext = Depends(require_auth_mutating)):
    fs, p = await _where(ctx, body.path)
    if fs:
        data = body.text.encode("utf-8")
        await fs.write(p, data, body.base_sha256, body.create)
        return {"path": fs.name(p), "sha256": _sha(data), "size": len(data)}
    return await run_in_threadpool(_write_local, Path(p), body)


def _write_local(p: Path, body: WriteIn) -> dict[str, Any]:
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
async def mkdir(body: PathIn, ctx: AppContext = Depends(require_auth_mutating)):
    fs, ps = await _where(ctx, body.path)
    if fs:
        await fs.mkdir(ps)
        return {"path": fs.name(ps)}
    p = Path(ps)
    if p.exists():
        raise conflict("ALREADY_EXISTS", f"{p} already exists")
    try:
        p.mkdir(parents=True)
    except PermissionError:
        raise ApiError(403, "PERMISSION_DENIED", f"cannot create {p}")
    return {"path": str(p)}


@router.post("/local/rename")
async def rename(body: PathIn, ctx: AppContext = Depends(require_auth_mutating)):
    fa, pa = await _where(ctx, body.path)
    fb, pb = await _where(ctx, body.new_path or "")
    if (fa.machine if fa else None) != (fb.machine if fb else None):
        raise bad_request("moving between machines is not possible here: both paths must be on the same machine", "INVALID_PATH")
    if fa:
        await fa.rename(pa, pb)
        return {"path": fa.name(pb)}
    return await run_in_threadpool(_rename_local, Path(pa), Path(pb))


def _rename_local(a: Path, b: Path) -> dict[str, Any]:
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
async def delete(body: PathIn, ctx: AppContext = Depends(require_auth_mutating)):
    fs, ps = await _where(ctx, body.path)
    if fs:
        await fs.delete(ps)
        return {"deleted": fs.name(ps)}
    return await run_in_threadpool(_delete_local, Path(ps))


def _delete_local(p: Path) -> dict[str, Any]:
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
async def stat(body: StatIn, ctx: AppContext = Depends(require_auth)):
    """Current content hashes of open files (None = gone or too large): clean tabs follow changes on disk. Files of a
    machine that cannot be reached now are left out (unknown, not gone)."""
    out: dict[str, str | None] = {}
    by: dict[str, list[str]] = {}
    local: list[str] = []
    for x in body.paths:
        s = split(x)
        if s:
            by.setdefault(s[0], []).append(x)
        else:
            local.append(x)

    async def one(machine: str, keys: list[str]) -> None:
        try:
            fs = Remote(ctx, machine)
            paths = [await fs.abs(split(k)[1]) for k in keys]  # type: ignore[index]
            out.update(zip(keys, await fs.shas(paths, MAX_TEXT), strict=True))
        except ApiError:
            pass

    await asyncio.gather(*(one(m, ks) for m, ks in by.items()))
    out.update(await run_in_threadpool(_stat_local, local))
    return {"sha256": out}


def _stat_local(paths: list[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for x in paths:
        p = _abs(x)
        try:
            out[x] = _sha(p.read_bytes()) if p.is_file() and p.stat().st_size <= MAX_TEXT else None
        except OSError:
            out[x] = None
    return out


async def _bytes_of(ctx: AppContext, path: str, limit: int) -> tuple[str, bytes]:
    """(file name, content) of a file anywhere (Local files on a machine: fetched over ssh)."""
    fs, p = await _where(ctx, path)
    name = p.rstrip("/").rsplit("/", 1)[-1]
    if fs:
        size, _, data = await fs.read_bytes(p, limit)
        if data is None:
            raise ApiError(413, "FILE_TOO_LARGE", f"{name} is {size} bytes; files of a machine are shown up to {limit} bytes")
        return name, data
    lp = Path(p)
    if not lp.is_file():
        raise not_found(f"{lp} is not a file", "FILE_NOT_FOUND")
    if lp.stat().st_size > limit:
        raise ApiError(413, "FILE_TOO_LARGE", "file too large")
    return name, await run_in_threadpool(lp.read_bytes)


@router.get("/local/sheet")
async def sheet(path: str = Query(...), ctx: AppContext = Depends(require_auth)):
    """An .xlsx workbook's sheets as tables of cell values (first 2000 rows / 200 columns of each)."""
    import tempfile

    from .sheets import read_xlsx

    if Path(path).suffix.lower() not in (".xlsx", ".xlsm"):
        raise bad_request("only .xlsx / .xlsm workbooks can be shown as tables", "NOT_A_WORKBOOK")
    _, data = await _bytes_of(ctx, path, MAX_REMOTE_RAW)

    def read() -> list[Any]:
        with tempfile.NamedTemporaryFile(suffix=Path(path).suffix) as f:
            f.write(data)
            f.flush()
            return read_xlsx(f.name)
    try:
        return {"sheets": await run_in_threadpool(read)}
    except Exception as e:  # noqa: BLE001 - a damaged or unusual workbook
        raise bad_request(f"could not read the workbook: {e}", "BAD_WORKBOOK") from e


@router.get("/local/raw")
async def raw(path: str = Query(...), ctx: AppContext = Depends(require_auth)):
    name, data = await _bytes_of(ctx, path, MAX_REMOTE_RAW if split(path) else MAX_RAW)
    ctype = IMAGE_TYPES.get(Path(name).suffix.lower())
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if ctype == "image/svg+xml":
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    if not ctype:
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return Response(content=data, media_type=ctype or "application/octet-stream", headers=headers)
