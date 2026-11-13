"""The commit history of a period: from the repository's connected checkout (PR Review ⎇; only the app's own refs are
fetched) or an app-owned blobless clone (repos/<owner>/<repo>.git), brought up to date first."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Any

from ..errors import ApiError
from . import settings as st

log = logging.getLogger("waw.insights")
_locks: dict[str, asyncio.Lock] = {}


def _git(args: list[str], timeout: float = 600) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, "", str(exc)


def _prepare(ctx: Any, repo: str) -> tuple[str, str, str]:
    """(git dir, ref to read, note): up to date when possible."""
    from ..local_repos import default_branch, find_remote, get_link

    link = get_link(ctx, repo)
    url = ctx.extra.get("insights_remote", {}).get(repo) or f"https://github.com/{repo}.git"
    if link:
        path = link["path"]
        src = link.get("remote") or find_remote(path, repo) or url
        branch = default_branch(path, src)
        rc, _, err = _git(["-C", path, "fetch", "--no-tags", "--quiet", src, f"+refs/heads/{branch}:refs/waw/base/{branch}"])
        ref = f"refs/waw/base/{branch}"
        note = "" if rc == 0 else f"could not update {path} ({err.strip()[-200:]}); its local history is used"
        if rc != 0 and _git(["-C", path, "rev-parse", "--verify", "--quiet", ref])[0] != 0:
            ref = branch if _git(["-C", path, "rev-parse", "--verify", "--quiet", branch])[0] == 0 else "HEAD"
        return path, ref, note
    clone = st.data_dir(ctx) / "repos" / f"{repo}.git"
    if not (clone / "HEAD").is_file():
        clone.parent.mkdir(parents=True, exist_ok=True)
        rc, _, err = _git(["clone", "--bare", "--filter=blob:none", "--quiet", url, str(clone)], timeout=3600)
        if rc != 0:
            raise ApiError(502, "CLONE_FAILED", f"could not clone {repo}: {err.strip()[-300:]}")
    branch = default_branch(str(clone), "origin")
    rc, _, err = _git(["-C", str(clone), "fetch", "--no-tags", "--quiet", "--filter=blob:none", "origin",
                       f"+refs/heads/{branch}:refs/heads/{branch}"], timeout=1800)
    note = "" if rc == 0 else f"could not update the clone ({err.strip()[-200:]}); its earlier history is used"
    return str(clone), f"refs/heads/{branch}", note


async def log_of(ctx: Any, repo: str, since: str, until: str) -> dict[str, Any]:
    """{commits: [(hash, date, author, subject)], areas: [(count, area)], authors, ref, note}"""
    lock = _locks.setdefault(repo.lower(), asyncio.Lock())
    async with lock:
        path, ref, note = await asyncio.to_thread(_prepare, ctx, repo)

    def run() -> dict[str, Any]:
        base = ["-C", path, "log", ref, f"--since={since}T00:00:00Z", f"--until={until}T23:59:59Z", "--no-merges", "--date=short"]
        rc, out, err = _git(base + ["--format=%h%x09%ad%x09%an%x09%s"], timeout=900)
        if rc != 0:
            raise ApiError(502, "GIT_FAILED", f"git log failed: {err.strip()[-300:]}")
        commits = [ln.split("\t", 3) for ln in out.splitlines() if ln.count("\t") >= 3]
        rc, out, _ = _git(base + ["--format=", "--name-only"], timeout=900)
        areas: dict[str, int] = {}
        for f in out.splitlines() if rc == 0 else []:
            f = f.strip()
            if not f:
                continue
            parts = f.split("/")
            a = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else "(top level)")
            areas[a] = areas.get(a, 0) + 1
        return {"commits": commits, "areas": sorted(((n, a) for a, n in areas.items()), reverse=True)[:40],
                "authors": len({c[2] for c in commits}), "ref": ref, "path": path, "note": note}
    return await asyncio.to_thread(run)


def text_of(repo: str, data: dict[str, Any], limit: int = 3000) -> str:
    areas = "\n".join(f"{n:6d}  {a}" for n, a in data["areas"])
    rows = "\n".join("\t".join(c) for c in data["commits"][:limit])
    more = f"\n(only the newest {limit} of {len(data['commits'])} commits are listed)" if len(data["commits"]) > limit else ""
    return f"Files changed per area (commit-file count):\n{areas}\n\nCommits (hash, date, author, subject; newest first):\n{rows}{more}"


def path_note(p: Path) -> str:
    return str(p)
