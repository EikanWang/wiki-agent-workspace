"""Local files on a machine of the Machines view: the same operations as localfs, run over ssh (machines.conn's shared
connection) with POSIX tools and GNU find. A path on a machine is written `<machine>:/absolute/path` (like scp);
`<machine>:~/…` starts at its home folder. Every command is one `sh -c <script> _ <args…>` (arguments quoted, never
pasted into the script).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import posixpath
import re
from typing import Any

from .errors import ApiError, bad_request, conflict, not_found

REMOTE_RE = re.compile(r"^([A-Za-z0-9._-]+):(/.*|~|~/.*)$")
MAX_REMOTE_RAW = 50 * 1024 * 1024  # images, downloads and workbooks fetched from a machine


def split(path: str) -> tuple[str, str] | None:
    """(machine, path there) for `machine:/path`, else None (a path on the server)."""
    m = REMOTE_RE.match(path or "")
    return (m.group(1), m.group(2)) if m else None


def join(machine: str, path: str) -> str:
    return f"{machine}:{path}"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Remote:
    """One machine's file system."""

    def __init__(self, ctx: Any, machine: str):
        from .development.targets import target_for

        reg = ctx.extra["machines"]
        h = reg.host(machine)  # 404 MACHINE_NOT_FOUND
        if h.kind == "bastion":
            raise bad_request(f"{machine} is a bastion: it is only passed through; open folders on the machines behind it", "MACHINE_IS_BASTION")
        self.machine = machine
        self.t = target_for(reg.dev, machine)

    async def run(self, script: str, *args: str, timeout: float = 60, stdin: bytes | None = None) -> tuple[int, str, str]:
        from .development.targets import TargetError

        try:
            return await self.t.run(["sh", "-c", script, "_", *args], timeout=timeout, stdin=stdin)
        except TargetError as exc:
            raise ApiError(502, "MACHINE_UNREACHABLE", f"{self.machine}: {exc.message}") from exc

    def fail(self, err: str, what: str) -> ApiError:
        err = err.strip()[-300:]
        if "ermission denied" in err:
            return ApiError(403, "PERMISSION_DENIED", f"cannot {what} on {self.machine}")
        return ApiError(400, "REMOTE_FAILED", f"cannot {what} on {self.machine}: {err or 'failed'}")

    async def abs(self, path: str) -> str:
        """An absolute, normalized path there (~ expanded)."""
        from .development.targets import TargetError

        if "\0" in path:
            raise bad_request("give a path", "INVALID_PATH")
        try:
            p = await self.t.expand(path)
        except TargetError as exc:
            raise ApiError(502 if exc.code == "DEV_HOST_UNREACHABLE" else 400, "MACHINE_UNREACHABLE" if exc.code == "DEV_HOST_UNREACHABLE" else "INVALID_PATH",
                           f"{self.machine}: {exc.message}") from exc
        return posixpath.normpath(p) if p != "/" else "/"

    def name(self, p: str) -> str:
        return join(self.machine, p)

    # ---------------------------------------------------------------- folders
    async def dirs_exist(self, paths: list[str]) -> list[bool]:
        rc, out, err = await self.run('for p; do if [ -d "$p" ]; then echo 1; else echo 0; fi; done', *paths, timeout=20)
        if rc != 0:
            raise self.fail(err, "check its folders")
        got = out.split()
        return [i < len(got) and got[i] == "1" for i in range(len(paths))]

    async def list_dir(self, p: str, hidden: bool, max_list: int) -> dict[str, Any]:
        script = ('[ -d "$1" ] || { echo "@@notdir"; exit 0; }; [ -r "$1" ] && [ -x "$1" ] || { echo "@@denied"; exit 0; }; echo "@@ok"; '
                  "find \"$1\" -mindepth 1 -maxdepth 1 -printf '%Y\\t%y\\t%s\\t%T@\\t%f\\0' 2>/dev/null")
        rc, out, err = await self.run(script, p, timeout=60)
        if rc != 0:
            raise self.fail(err, f"list {p}")
        head, _, rest = out.partition("\n")
        if head == "@@notdir":
            raise not_found(f"{self.name(p)} is not a folder", "FOLDER_NOT_FOUND")
        if head == "@@denied":
            raise ApiError(403, "PERMISSION_DENIED", f"cannot list {self.name(p)}")
        items: list[dict[str, Any]] = []
        for rec in rest.split("\0"):
            parts = rec.split("\t", 4)
            if len(parts) != 5 or not parts[4] or (not hidden and parts[4].startswith(".")):
                continue
            is_dir = parts[0] == "d"
            try:
                size, mtime = (0 if is_dir else int(parts[2])), (None if is_dir else float(parts[3]))
            except ValueError:
                size, mtime = 0, None
            items.append({"name": parts[4], "path": self.name(posixpath.join(p, parts[4])), "dir": is_dir, "size": size,
                          "mtime": mtime, "link": parts[1] == "l"})
        items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
        truncated = len(items) > max_list
        parent = posixpath.dirname(p) if p != "/" else None
        return {"path": self.name(p), "parent": self.name(parent) if parent else None, "items": items[:max_list], "truncated": truncated}

    # ---------------------------------------------------------------- files
    async def read_bytes(self, p: str, limit: int) -> tuple[int, bool, bytes | None]:
        """(size, writable, content or None when larger than `limit`)."""
        script = ('[ -f "$1" ] || exit 3; [ -r "$1" ] || exit 5; s=$(wc -c < "$1" | tr -d " "); '
                  'if [ -w "$1" ]; then w=1; else w=0; fi; echo "$s $w"; [ "$s" -le "$2" ] || exit 0; base64 < "$1"')
        rc, out, err = await self.run(script, p, str(limit), timeout=180)
        if rc == 3:
            raise not_found(f"{self.name(p)} is not a file", "FILE_NOT_FOUND")
        if rc == 5:
            raise ApiError(403, "PERMISSION_DENIED", f"cannot read {self.name(p)}")
        if rc != 0:
            raise self.fail(err, f"read {p}")
        head, _, body = out.partition("\n")
        try:
            size_s, w = head.split()
            size = int(size_s)
        except ValueError as exc:
            raise self.fail(head, f"read {p}") from exc
        return size, w == "1", (base64.b64decode(body) if size <= limit else None)

    async def write(self, p: str, data: bytes, base_sha256: str | None, create: bool) -> None:
        script = r'''p=$1; base=$2; create=$3
if [ "$create" = 1 ]; then
  if [ -e "$p" ] || [ -L "$p" ]; then exit 3; fi
else
  [ -f "$p" ] || exit 4
  cur=$(sha256sum < "$p" | cut -d" " -f1)
  [ "$cur" = "$base" ] || { printf %s "$cur"; exit 5; }
fi
[ -d "$p" ] && exit 6
d=$(dirname -- "$p"); mkdir -p -- "$d" || exit 7
m=$(stat -c %a -- "$p" 2>/dev/null)
t="$d/.$(basename -- "$p").waw-tmp"
cat > "$t" || { rm -f -- "$t"; exit 7; }
if [ -n "$m" ]; then chmod "$m" "$t"; fi
mv -f -- "$t" "$p" || { rm -f -- "$t"; exit 7; }'''
        rc, out, err = await self.run(script, p, base_sha256 or "", "1" if create else "0", timeout=180, stdin=data)
        if rc == 3:
            raise conflict("ALREADY_EXISTS", f"{self.name(p)} already exists")
        if rc == 4:
            raise conflict("FILE_CHANGED", f"{self.name(p)} was deleted on disk", current_sha256=None)
        if rc == 5:
            raise conflict("FILE_CHANGED", f"{posixpath.basename(p)} changed on disk since it was opened", current_sha256=out.strip() or None)
        if rc == 6:
            raise bad_request("that is a folder", "INVALID_PATH")
        if rc != 0:
            raise self.fail(err, f"write {p}")

    async def mkdir(self, p: str) -> None:
        rc, _, err = await self.run('if [ -e "$1" ] || [ -L "$1" ]; then exit 3; fi; mkdir -p -- "$1"', p, timeout=30)
        if rc == 3:
            raise conflict("ALREADY_EXISTS", f"{self.name(p)} already exists")
        if rc != 0:
            raise self.fail(err, f"create {p}")

    async def rename(self, a: str, b: str) -> None:
        script = ('if [ ! -e "$1" ] && [ ! -L "$1" ]; then exit 4; fi; if [ -e "$2" ] || [ -L "$2" ]; then exit 3; fi; '
                  'mkdir -p -- "$(dirname -- "$2")" && mv -- "$1" "$2"')
        rc, _, err = await self.run(script, a, b, timeout=120)
        if rc == 4:
            raise not_found(f"{self.name(a)} not found", "FILE_NOT_FOUND")
        if rc == 3:
            raise conflict("ALREADY_EXISTS", f"{self.name(b)} already exists")
        if rc != 0:
            raise self.fail(err, f"move {a}")

    async def delete(self, p: str) -> None:
        if p in ("/", await self.abs("~")):
            raise bad_request("refusing to delete this folder", "INVALID_PATH")
        script = ('if [ -d "$1" ] && [ ! -L "$1" ]; then rm -rf -- "$1"; elif [ -e "$1" ] || [ -L "$1" ]; then rm -f -- "$1"; '
                  'else exit 4; fi')
        rc, _, err = await self.run(script, p, timeout=300)
        if rc == 4:
            raise not_found(f"{self.name(p)} not found", "FILE_NOT_FOUND")
        if rc != 0:
            raise self.fail(err, f"delete {p}")

    async def shas(self, paths: list[str], max_size: int) -> list[str | None]:
        script = ('max=$1; shift; for p; do if [ -f "$p" ] && [ "$(wc -c < "$p" | tr -d " ")" -le "$max" ]; then '
                  'h=$(sha256sum < "$p" 2>/dev/null | cut -d" " -f1); echo "${h:--}"; else echo -; fi; done')
        rc, out, err = await self.run(script, str(max_size), *paths, timeout=60)
        if rc != 0:
            raise self.fail(err, "check its files")
        got = out.split()
        return [None if i >= len(got) or got[i] == "-" else got[i] for i in range(len(paths))]


async def gather_roots(ctx: Any, stored: list[str]) -> dict[str, dict[str, Any]]:
    """Whether each folder on a machine is there: one ssh call per machine, all at once (an unreachable machine shows
    its folders as unavailable with the reason; it does not hold up the others)."""
    by: dict[str, list[tuple[str, str]]] = {}
    for r in stored:
        s = split(r)
        if s:
            by.setdefault(s[0], []).append((r, s[1]))
    out: dict[str, dict[str, Any]] = {}

    async def one(machine: str, rows: list[tuple[str, str]]) -> None:
        try:
            ok = await asyncio.wait_for(Remote(ctx, machine).dirs_exist([p for _, p in rows]), timeout=30)
            for (r, _), e in zip(rows, ok, strict=True):
                out[r] = {"exists": e}
        except (ApiError, TimeoutError) as exc:
            why = exc.message if isinstance(exc, ApiError) else f"{machine} did not answer"
            for r, _ in rows:
                out[r] = {"exists": False, "error": why}

    await asyncio.gather(*(one(m, rows) for m, rows in by.items()))
    return out
