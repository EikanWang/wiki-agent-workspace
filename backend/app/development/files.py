"""Files of a development workspace for the web editor: the worktree (read/write) and the reference wiki (read-only).

Everything runs on the workspace's host (Target): plain Python for `local`, POSIX tools through ssh otherwise (GNU
find, sha256sum, git). Paths are relative to a root and never contain `..`; writes are conditional on the content
hash the editor loaded (409 FILE_CHANGED otherwise) and never go through a symlink that leaves the worktree.
"""

from __future__ import annotations

import hashlib
import os
import shlex
from pathlib import Path
from typing import Any

from ..deps import AppContext
from ..errors import ApiError, bad_request, conflict, not_found
from ..models import DevWorkspace
from .service import DevService

MAX_FILE = 5 * 1024 * 1024
MAX_LIST = 5000
MAX_FILES = 50_000
SKIP_DIRS = {".git"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def norm_rel(rel: str) -> str:
    rel = (rel or "").replace("\\", "/").strip()
    if rel.startswith("/") or "\0" in rel:
        raise bad_request("path must be relative to the root", "INVALID_PATH")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise bad_request("path traversal is not allowed", "INVALID_PATH")
    if parts and parts[0] in SKIP_DIRS:
        raise bad_request("the .git folder is not editable here", "INVALID_PATH")
    return "/".join(parts)


class DevFiles:
    def __init__(self, ctx: AppContext, dev_id: str):
        self.ctx = ctx
        svc = DevService(ctx)
        self.row: DevWorkspace
        self.row, self.t = svc._ready(dev_id)
        self.svc = svc

    # ------------------------------------------------------------------ roots
    async def roots(self) -> dict[str, dict[str, Any]]:
        out = {"wt": {"path": self.row.worktree_path, "name": f"{self.row.branch}", "readonly": False}}
        wiki = await self.wiki_path()
        if wiki:
            out["wiki"] = {"path": wiki, "name": "wiki (reference)", "readonly": True}
        return out

    async def wiki_path(self) -> str | None:
        if not self.row.wiki_workspace_id:
            return None
        if self.t.is_local:
            root = self.svc.wiki_root(self.row)
            return str(root) if root else None
        from .agent import remote_wiki_dir

        return await remote_wiki_dir(self.ctx, self.row)

    async def root(self, name: str, *, write: bool = False) -> str:
        roots = await self.roots()
        r = roots.get(name)
        if r is None:
            raise not_found(f"unknown root {name}", "ROOT_NOT_FOUND")
        if write and r["readonly"]:
            raise ApiError(403, "READ_ONLY", "the reference wiki is read-only here")
        return r["path"]

    @staticmethod
    def join(root: str, rel: str) -> str:
        return root.rstrip("/") + ("/" + rel if rel else "")

    # ------------------------------------------------------------------ listing
    async def list_dir(self, root_name: str, rel: str) -> list[dict[str, Any]]:
        root = await self.root(root_name)
        rel = norm_rel(rel)
        path = self.join(root, rel)
        items: list[dict[str, Any]] = []
        if self.t.is_local:
            p = Path(path)
            if not p.is_dir():
                raise not_found(f"{rel or '/'} is not a folder", "FOLDER_NOT_FOUND")
            for e in sorted(os.scandir(p), key=lambda e: e.name):
                if e.name in SKIP_DIRS:
                    continue
                try:
                    is_dir = e.is_dir()
                    size = 0 if is_dir else e.stat().st_size
                except OSError:
                    continue
                items.append({"name": e.name, "dir": is_dir, "size": size, "link": e.is_symlink()})
                if len(items) >= MAX_LIST:
                    break
        else:
            rc, out, err = await self.t.run(["find", path, "-mindepth", "1", "-maxdepth", "1", "-printf", "%Y\t%s\t%y\t%f\n"], timeout=60)
            if rc != 0:
                raise not_found(f"{rel or '/'}: {err.strip()[:200]}", "FOLDER_NOT_FOUND")
            for line in out.splitlines():
                parts = line.split("\t", 3)
                if len(parts) != 4 or parts[3] in SKIP_DIRS:
                    continue
                items.append({"name": parts[3], "dir": parts[0] == "d", "size": int(parts[1]) if parts[0] != "d" else 0, "link": parts[2] == "l"})
            items.sort(key=lambda x: x["name"])
            items = items[:MAX_LIST]
        items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
        return items

    async def all_files(self, root_name: str) -> list[str]:
        """Every file of a root (for quick open): tracked + untracked-not-ignored for the worktree."""
        root = await self.root(root_name)
        if root_name == "wt":
            rc, out, _ = await self.t.git(root, ["ls-files", "-co", "--exclude-standard", "-z"], timeout=120)
            files = [f for f in out.split("\0") if f] if rc == 0 else []
        else:
            rc, out, _ = await self.t.run(["find", root, "-type", "f", "-not", "-path", "*/.git/*", "-printf", "%P\n"], timeout=120)
            files = [f for f in out.splitlines() if f] if rc == 0 else []
        return sorted(files)[:MAX_FILES]

    # ------------------------------------------------------------------ read / write
    async def read_bytes(self, root_name: str, rel: str, max_size: int = 0) -> tuple[str, bytes]:
        """(absolute path, content) of a file of a root (any content, up to MAX_FILE or `max_size`)."""
        limit = max_size or MAX_FILE
        root = await self.root(root_name)
        rel = norm_rel(rel)
        if not rel:
            raise bad_request("give a file", "INVALID_PATH")
        path = self.join(root, rel)
        if self.t.is_local:
            p = Path(path)
            if not p.is_file():
                raise not_found(f"{rel} not found", "FILE_NOT_FOUND")
            size = p.stat().st_size
            if size > limit:
                raise ApiError(413, "FILE_TOO_LARGE", f"{rel} is {size} bytes; the limit is {limit}")
            data = p.read_bytes()
        else:
            q = shlex.quote(path)
            rc, out, err = await self.t.run(["sh", "-c", f"test -f {q} || exit 3; s=$(wc -c < {q}); [ \"$s\" -le {limit} ] || exit 4; base64 < {q}"],
                                            timeout=120)
            if rc == 3:
                raise not_found(f"{rel} not found", "FILE_NOT_FOUND")
            if rc == 4:
                raise ApiError(413, "FILE_TOO_LARGE", f"{rel} is larger than {limit} bytes")
            if rc != 0:
                raise ApiError(400, "READ_FAILED", err.strip()[:300])
            import base64

            data = base64.b64decode(out)
        return path, data

    async def read(self, root_name: str, rel: str) -> dict[str, Any]:
        rel = norm_rel(rel)
        path, data = await self.read_bytes(root_name, rel)
        binary = b"\0" in data[:8000]
        text = None
        if not binary:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                binary = True
        return {"root": root_name, "path": rel, "abs_path": path, "sha256": sha(data), "size": len(data), "binary": binary,
                "text": text, "readonly": root_name != "wt"}

    async def current_sha(self, path: str) -> str | None:
        if self.t.is_local:
            p = Path(path)
            return sha(p.read_bytes()) if p.is_file() else None
        rc, out, _ = await self.t.run(["sh", "-c", f"test -f {shlex.quote(path)} && sha256sum {shlex.quote(path)}"], timeout=60)
        return out.split()[0] if rc == 0 and out.strip() else None

    async def _check_inside(self, root: str, path: str) -> None:
        """Writes never go through a symlink that leaves the worktree."""
        if self.t.is_local:
            real = os.path.realpath(path)
            rroot = os.path.realpath(root)
            if real != rroot and not real.startswith(rroot + os.sep):
                raise ApiError(403, "PATH_ESCAPES", "this path leads outside the worktree")
            return
        rc, out, _ = await self.t.run(["realpath", "-m", path, root], timeout=30)
        lines = out.splitlines()
        if rc != 0 or len(lines) != 2 or not (lines[0] == lines[1] or lines[0].startswith(lines[1] + "/")):
            raise ApiError(403, "PATH_ESCAPES", "this path leads outside the worktree")

    async def write(self, rel: str, text: str, base_sha256: str | None, *, create: bool = False) -> dict[str, Any]:
        root = await self.root("wt", write=True)
        rel = norm_rel(rel)
        if not rel:
            raise bad_request("give a file", "INVALID_PATH")
        path = self.join(root, rel)
        await self._check_inside(root, path)
        cur = await self.current_sha(path)
        if create:
            if cur is not None:
                raise conflict("ALREADY_EXISTS", f"{rel} already exists")
        elif cur != base_sha256:
            raise conflict("FILE_CHANGED", f"{rel} changed on disk since it was opened (agent, terminal, git)", current_sha256=cur)
        data = text.encode("utf-8")
        if len(data) > MAX_FILE:
            raise ApiError(413, "FILE_TOO_LARGE", "the file is too large for the editor")
        mode = 0o644
        if self.t.is_local:
            p = Path(path)
            if p.exists():
                mode = p.stat().st_mode & 0o7777
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{p.name}.waw-tmp")
            tmp.write_bytes(data)
            os.chmod(tmp, mode)
            os.replace(tmp, p)
        else:
            q = shlex.quote(path)
            script = (f"mkdir -p \"$(dirname {q})\" && m=$(stat -c %a {q} 2>/dev/null || echo 644) && cat > {q}.waw-tmp && "
                      f"chmod \"$m\" {q}.waw-tmp && mv -f {q}.waw-tmp {q}")
            rc, _, err = await self.t.run(["sh", "-c", script], timeout=120, stdin=data)
            if rc != 0:
                raise ApiError(400, "WRITE_FAILED", err.strip()[:300])
        return {"path": rel, "sha256": sha(data), "size": len(data)}

    async def mkdir(self, rel: str) -> dict[str, Any]:
        root = await self.root("wt", write=True)
        rel = norm_rel(rel)
        path = self.join(root, rel)
        await self._check_inside(root, path)
        rc, _, err = await self.t.run(["mkdir", "-p", path], timeout=30)
        if rc != 0:
            raise ApiError(400, "MKDIR_FAILED", err.strip()[:300])
        return {"path": rel}

    async def rename(self, rel: str, new_rel: str) -> dict[str, Any]:
        root = await self.root("wt", write=True)
        a, b = norm_rel(rel), norm_rel(new_rel)
        if not a or not b:
            raise bad_request("give both paths", "INVALID_PATH")
        pa, pb = self.join(root, a), self.join(root, b)
        await self._check_inside(root, pa)
        await self._check_inside(root, pb)
        rc, _, err = await self.t.run(["sh", "-c", f"test -e {shlex.quote(pb)} && exit 3; mkdir -p \"$(dirname {shlex.quote(pb)})\" && mv -- {shlex.quote(pa)} {shlex.quote(pb)}"],
                                      timeout=60)
        if rc == 3:
            raise conflict("ALREADY_EXISTS", f"{b} already exists")
        if rc != 0:
            raise ApiError(400, "RENAME_FAILED", err.strip()[:300])
        return {"path": b}

    async def delete(self, rel: str) -> dict[str, Any]:
        root = await self.root("wt", write=True)
        rel = norm_rel(rel)
        if not rel:
            raise bad_request("refusing to delete the worktree", "INVALID_PATH")
        path = self.join(root, rel)
        await self._check_inside(root, path)
        rc, _, err = await self.t.run(["rm", "-rf", "--", path], timeout=120)
        if rc != 0:
            raise ApiError(400, "DELETE_FAILED", err.strip()[:300])
        return {"deleted": rel}

    async def stat_many(self, root_name: str, paths: list[str]) -> dict[str, str | None]:
        """Current content hashes (None = gone): the editor reloads tabs the agent or a command changed."""
        root = await self.root(root_name)
        rels = [norm_rel(p) for p in paths[:200]]
        if self.t.is_local:
            out: dict[str, str | None] = {}
            for r in rels:
                p = Path(self.join(root, r))
                try:
                    out[r] = sha(p.read_bytes()) if p.is_file() and p.stat().st_size <= MAX_FILE else None
                except OSError:
                    out[r] = None
            return out
        script = "; ".join(f"(sha256sum {shlex.quote(self.join(root, r))} 2>/dev/null || echo '- {shlex.quote(r)}') | head -1" for r in rels)
        _rc, text, _ = await self.t.run(["sh", "-c", script or "true"], timeout=120)
        result: dict[str, str | None] = {}
        lines = text.splitlines()
        for r, line in zip(rels, lines, strict=False):
            h = line.split()[0] if line.strip() else "-"
            result[r] = None if h == "-" else h
        return result

    # ------------------------------------------------------------------ search
    async def search(self, root_name: str, query: str, *, regex: bool, case: bool, glob: str | None, limit: int = 2000) -> dict[str, Any]:
        root = await self.root(root_name)
        if not query or len(query) > 1000 or "\0" in query:
            raise bad_request("give a search text", "INVALID_QUERY")
        flags = ["-n", "-I"]
        if not regex:
            flags.append("-F")
        else:
            flags.append("-E")
        if not case:
            flags.append("-i")
        if root_name == "wt":
            args = ["git", "-C", root, "grep", "--no-color", *flags, "--untracked", "-e", query]
            if glob:
                args += ["--", glob]
        else:
            args = ["grep", "-r", "--color=never", *flags, "--exclude-dir=.git", "-e", query, "."]
            if glob:
                args.insert(2, f"--include={glob}")
            args = ["sh", "-c", f"cd {shlex.quote(root)} && exec " + shlex.join(args)]
        rc, out, err = await self.t.run(args, timeout=120)
        if rc not in (0, 1):
            raise bad_request(f"search failed: {err.strip()[:300]}", "SEARCH_FAILED")
        hits: list[dict[str, Any]] = []
        for line in out.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3 or not parts[1].isdigit():
                continue
            hits.append({"path": parts[0].removeprefix("./"), "line": int(parts[1]), "text": parts[2][:400]})
            if len(hits) >= limit:
                break
        return {"hits": hits, "truncated": len(hits) >= limit}
