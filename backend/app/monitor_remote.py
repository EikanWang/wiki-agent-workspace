"""Agent monitor on machines (machines.registry): sessions of folders on a machine reached over SSH.

A remote folder is written ``<machine>:<absolute path>`` wherever the monitor keeps folders (added folders, the
checked ones). On the machine:

* OpenCode — its own CLI (the machine's `opencode`, <work_dir>/bin first on PATH): `opencode session list` in each
  folder, `opencode export <id>` into a temporary file there (through a pipe it stops early on large sessions).
* Codex / Claude Code — a small Python helper (`python3 -`, sent on stdin) lists the transcripts and returns the
  changed ones; they are parsed here by the same readers as local ones. One ssh call per machine and refresh.

Everything goes through the machine's shared ssh connection (machines.conn).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

log = logging.getLogger("waw.monitor")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
FRESH = 8.0       # a machine's listing younger than this is used as is; older: refreshed in the background
MAX_AGE = 600.0   # older than this: wait for a new one
CLAUDE_BIN = "claude"


def split_folder(f: str) -> tuple[str | None, str]:
    """``box:/path`` -> ("box", "/path"); a local path -> (None, path)."""
    if not f.startswith("/") and ":" in f:
        m, _, p = f.partition(":")
        if NAME_RE.match(m) and m != "local" and p.startswith("/"):
            return m, p
    return None, f


def remote_folder(machine: str, path: str) -> str:
    return f"{machine}:{path}"


HELPER = r'''
import json, os, shutil, sys
from pathlib import Path
op, arg = sys.argv[1], json.loads(sys.argv[2])
home = Path.home()
cx = Path(os.environ.get("CODEX_HOME") or home / ".codex")
cl = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")

def under(d, folders):
    if not d:
        return False
    d = os.path.normpath(d)
    return any(d == f or d.startswith(f.rstrip("/") + "/") for f in folders)

def codex_list(folders):
    root = cx / "sessions"
    if not root.exists():
        return []
    titles = {}
    idx = cx / "session_index.jsonl"
    if idx.exists():
        for line in idx.read_text("utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line); titles[d["id"]] = d.get("thread_name") or ""
            except (ValueError, KeyError):
                pass
    out = []
    for f in sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:1000]:
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                d = json.loads(fh.readline())
        except (OSError, ValueError):
            continue
        p = d.get("payload") or {}
        sid = p.get("id") or p.get("session_id")
        if sid and under(p.get("cwd"), folders):
            out.append({"id": sid, "title": titles.get(sid) or None, "directory": p.get("cwd"), "updated": f.stat().st_mtime})
    return out

def claude_list(folders):
    root = cl / "projects"
    if not root.exists():
        return []
    out = []
    for f in sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:1000]:
        cwd = title = first = None
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 60:
                        break
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    cwd = cwd or d.get("cwd")
                    if d.get("type") in ("summary", "ai-title"):
                        title = title or d.get("summary") or d.get("title")
                    if d.get("type") == "user" and not first and not d.get("isMeta"):
                        c = (d.get("message") or {}).get("content")
                        if isinstance(c, str) and not c.startswith("<"):
                            first = c[:80]
        except OSError:
            continue
        if under(cwd, folders):
            out.append({"id": f.stem, "title": title or first, "directory": cwd, "updated": f.stat().st_mtime})
    return out

def find(provider, sid):
    if provider == "codex":
        root = cx / "sessions"
        hits = sorted(root.rglob("rollout-*" + sid + ".jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if root.exists() else []
    else:
        root = cl / "projects"
        hits = list(root.glob("*/" + sid + ".jsonl")) if root.exists() else []
    return hits[0] if hits else None

if op == "list":
    out = {"codex": codex_list(arg["folders"]), "claude": claude_list(arg["folders"])}
elif op == "read":
    out = {}
    for r in arg:
        f = find(r["provider"], r["id"])
        k = r["provider"] + ":" + r["id"]
        if f is None:
            out[k] = {"found": False}
            continue
        m = f.stat().st_mtime
        if r.get("known") == m:
            out[k] = {"found": True, "mtime": m, "unchanged": True}
        else:
            out[k] = {"found": True, "mtime": m, "text": f.read_text("utf-8", errors="replace")}
elif op == "delete":
    f = find("claude", arg["id"])
    if f is None:
        out = {"deleted": False}
    else:
        f.unlink()
        side = f.with_suffix("")
        if side.is_dir():
            shutil.rmtree(side, ignore_errors=True)
        out = {"deleted": True}
else:
    out = {"error": "unknown op"}
sys.stdout.write("@@json " + json.dumps(out))
'''

_OC_LIST = r'''for d in "$HOME" "$@"; do
  printf '\n@@dir %s\n' "$d"
  if cd "$d" 2>/dev/null; then PWD="$d" "$OC" session list --format json -n 1000 2>/dev/null; fi
done
exit 0
'''

_OC_EXPORT = r'''f=$(mktemp) || exit 1
cd "$HOME" 2>/dev/null; PWD="$HOME" "$OC" export "$1" > "$f" 2>/dev/null; rc=$?
cat "$f"; rm -f "$f"; exit $rc
'''


class RemoteError(Exception):
    pass


def registry(ctx: Any) -> Any:
    reg = (getattr(ctx, "extra", None) or {}).get("machines")
    if reg is None:
        raise RemoteError("machines are not available on this server")
    return reg


def target(ctx: Any, machine: str) -> Any:
    from .development.targets import TargetError, target_for

    try:
        return target_for(registry(ctx).dev, machine)
    except TargetError as exc:
        raise RemoteError(exc.message) from exc


def bin_of(ctx: Any, machine: str, provider: str) -> str:
    h = target(ctx, machine).host
    return {"opencode": h.opencode, "codex": h.codex}.get(provider) or CLAUDE_BIN


async def _run(ctx: Any, machine: str, args: list[str], *, timeout: float = 120, stdin: bytes | None = None,
               env: dict[str, str] | None = None) -> tuple[int, str, str]:
    from .development.targets import TargetError

    try:
        return await target(ctx, machine).run(args, timeout=timeout, stdin=stdin, env=env)
    except TargetError as exc:
        raise RemoteError(exc.message) from exc


async def helper(ctx: Any, machine: str, op: str, arg: Any, timeout: float = 120) -> Any:
    rc, out, err = await _run(ctx, machine, ["python3", "-", op, json.dumps(arg)], stdin=HELPER.encode(), timeout=timeout)
    i = out.find("@@json ")
    if rc != 0 or i < 0:
        why = "python3 is not installed there" if rc == 127 else (err.strip()[-300:] or f"exit {rc}")
        raise RemoteError(f"cannot read the Codex / Claude Code sessions on {machine}: {why}")
    return json.loads(out[i + 7:])


async def opencode_list(ctx: Any, machine: str, folders: list[str]) -> dict[str, dict[str, Any]]:
    rc, out, err = await _run(ctx, machine, ["sh", "-c", _OC_LIST, "_", *folders], env={"OC": bin_of(ctx, machine, "opencode")})
    if rc != 0:
        raise RemoteError(f"cannot list the OpenCode sessions on {machine}: {err.strip()[-300:] or f'exit {rc}'}")
    found: dict[str, dict[str, Any]] = {}
    for seg in out.split("\n@@dir ")[1:]:
        body = seg.partition("\n")[2]
        start = body.find("[")
        if start < 0:
            continue
        try:
            rows = json.loads(body[start:].strip())
        except json.JSONDecodeError:
            continue
        found.update({x["id"]: x for x in rows if isinstance(x, dict) and x.get("id")})
    return found


async def opencode_export(ctx: Any, machine: str, sid: str) -> tuple[int, str]:
    rc, out, _ = await _run(ctx, machine, ["sh", "-c", _OC_EXPORT, "_", sid], env={"OC": bin_of(ctx, machine, "opencode")}, timeout=180)
    return rc, out


# ---------------------------------------------------------------------- a machine's listing (shared, cached)

_lists: dict[str, tuple[float, dict[str, Any]]] = {}
_running: dict[str, asyncio.Task] = {}


async def _list_now(ctx: Any, machine: str, folders: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"opencode": {}, "codex": [], "claude": [], "errors": []}
    oc, other = await asyncio.gather(opencode_list(ctx, machine, folders), helper(ctx, machine, "list", {"folders": folders}),
                                     return_exceptions=True)
    for r in (oc, other):
        if isinstance(r, BaseException) and not isinstance(r, RemoteError):
            raise r
    if isinstance(oc, RemoteError):
        out["errors"].append(str(oc))
    else:
        out["opencode"] = oc
    if isinstance(other, RemoteError):
        out["errors"].append(str(other))
    else:
        out["codex"], out["claude"] = other.get("codex") or [], other.get("claude") or []
    return out


async def listing(ctx: Any, machine: str, folders: list[str]) -> dict[str, Any]:
    """The sessions of `folders` on `machine` (stale-while-refresh, one run at a time per machine and folders)."""
    key = machine + "\n" + "\n".join(sorted(folders))
    hit = _lists.get(key)
    age = time.time() - hit[0] if hit else None
    task = _running.get(key)
    if task is None and (age is None or age >= FRESH):
        async def refresh() -> dict[str, Any]:
            try:
                got = await _list_now(ctx, machine, folders)
            except RemoteError as exc:
                got = {"opencode": {}, "codex": [], "claude": [], "errors": [str(exc)]}
            try:
                if got["errors"] and hit and not (got["opencode"] or got["codex"] or got["claude"]):
                    got = {**hit[1], "errors": got["errors"]}  # unreachable: keep what was listed last
                _lists[key] = (time.time(), got)
                return got
            finally:
                _running.pop(key, None)

        task = _running[key] = asyncio.ensure_future(refresh())
    if hit and age is not None and age < MAX_AGE:
        return hit[1]
    return await asyncio.shield(task)


def forget() -> None:
    _lists.clear()
