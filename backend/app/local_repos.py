"""A GitHub repository connected to a local git checkout (e.g. pytorch/pytorch → ~/src/pytorch).

The checkout is the agent's default reference for that repository's PRs: when a PR tab opens, the PR's head is
fetched into the checkout as `refs/waw/pr/<n>` (never checked out — the user's working tree is not touched), and the
agent is told where the checkout is and how to read the PR's code there (`git show`, `git diff`). Optionally (on by
default) the base branch is brought up to date first: fetched, and fast-forwarded when the checkout is on that
branch with no local changes; otherwise only fetched.

Settings in kv_settings `local_repo:<owner>/<name>` = {path, update, remote}; the last sync of each PR in memory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .deps import AppContext
from .errors import ApiError
from .kv import kv_get

log = logging.getLogger("waw.local_repos")

SYNC_TIMEOUT = 600  # pytorch-sized fetches can take minutes the first time
_sync: dict[str, dict[str, Any]] = {}
_tasks: dict[str, asyncio.Task] = {}


def _key(repo: str) -> str:
    return f"local_repo:{repo.lower()}"


def get_link(ctx: AppContext, repo: str) -> dict[str, Any] | None:
    v = kv_get(ctx, _key(repo), None)
    return v if isinstance(v, dict) and v.get("path") else None


def git(path: str, args: list[str], timeout: float = 60) -> tuple[int, str, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}
    try:
        p = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=timeout, check=False, env=env)
    except FileNotFoundError:
        return 127, "", "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0]} timed out after {int(timeout)} s"
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def find_remote(path: str, repo: str) -> str | None:
    """The checkout's remote that points at github.com/<repo> (any URL form), if any."""
    code, out, _ = git(path, ["remote", "-v"])
    if code != 0:
        return None
    want = repo.lower()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            m = re.search(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$", parts[1])
            if m and m.group(1).lower() == want:
                return parts[0]
    return None


def check_checkout(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ApiError(400, "INVALID_PATH", "give the absolute path of the local checkout on the server")
    if not p.is_dir():
        raise ApiError(404, "FOLDER_NOT_FOUND", f"{p} is not a directory on the server")
    code, top, err = git(str(p), ["rev-parse", "--show-toplevel"])
    if code != 0:
        raise ApiError(400, "NOT_A_GIT_REPO", f"{p} is not a git checkout ({err or 'git rev-parse failed'})")
    return top


def sync_pr(link: dict[str, Any], repo: str, number: int, base: str | None) -> dict[str, Any]:
    """Fetch the PR head (and update the base branch when enabled). Never changes the checked-out branch or files,
    except a fast-forward of the base branch when it is checked out and clean."""
    path = link["path"]
    src = link.get("remote") or find_remote(path, repo) or f"https://github.com/{repo}.git"
    ref = f"refs/waw/pr/{number}"
    notes: list[str] = []
    updated = None
    if link.get("update", True) and base and re.fullmatch(r"[\w./-]+", base):
        tracking = f"refs/waw/base/{base}"
        code, _, err = git(path, ["fetch", "--no-tags", "--quiet", src, f"+refs/heads/{base}:{tracking}"], SYNC_TIMEOUT)
        if code != 0:
            notes.append(f"could not update {base}: {err[-300:]}")
        else:
            _, branch, _ = git(path, ["symbolic-ref", "--quiet", "--short", "HEAD"])
            _, dirty, _ = git(path, ["status", "--porcelain", "--untracked-files=no"])
            if branch == base and not dirty:
                code, _, err = git(path, ["merge", "--ff-only", "--quiet", tracking], 120)
                updated = code == 0
                notes.append(f"{base} fast-forwarded" if updated else f"{base} not fast-forwarded: {err[-200:]}")
            else:
                why = f"on {branch or 'a detached HEAD'}" if branch != base else "local changes"
                notes.append(f"{base} fetched as {tracking}; the working tree was left alone ({why})")
                updated = False
    code, _, err = git(path, ["fetch", "--no-tags", "--quiet", src, f"+refs/pull/{number}/head:{ref}"], SYNC_TIMEOUT)
    if code != 0:
        return {"state": "failed", "message": f"could not fetch the PR: {err[-300:]}", "notes": notes, "at": time.time()}
    _, sha, _ = git(path, ["rev-parse", ref])
    merge_base = None
    if base:
        for b in (f"refs/waw/base/{base}", base, f"origin/{base}"):
            c, mb, _ = git(path, ["merge-base", b, ref])
            if c == 0 and mb:
                merge_base = mb
                break
    return {"state": "done", "ref": ref, "sha": sha, "merge_base": merge_base, "base": base, "base_updated": updated,
            "notes": notes, "path": path, "at": time.time()}


def start_sync(ctx: AppContext, repo: str, number: int, base: str | None) -> dict[str, Any]:
    link = get_link(ctx, repo)
    key = f"{repo.lower()}#{number}"
    if link is None:
        return {"state": "none"}
    t = _tasks.get(key)
    if t is None or t.done():
        _sync[key] = {"state": "running", "at": time.time(), "update": link.get("update", True)}

        async def run() -> None:
            try:
                _sync[key] = await asyncio.to_thread(sync_pr, link, repo, number, base)
            except Exception as exc:  # never leave an unretrieved task exception
                _sync[key] = {"state": "failed", "message": f"{exc.__class__.__name__}: {exc}", "at": time.time()}
                log.exception("local sync failed for %s", key)
        _tasks[key] = asyncio.get_running_loop().create_task(run())
    return _sync[key]


def sync_state(repo: str, number: int) -> dict[str, Any] | None:
    return _sync.get(f"{repo.lower()}#{number}")


def local_repo_prompt(ctx: AppContext, repo: str, number: int) -> str | None:
    """For the agent's prompt: the connected checkout and how to read the PR there."""
    link = get_link(ctx, repo)
    if link is None:
        return None
    path = link["path"]
    s = sync_state(repo, number) or {}
    text = (f"The repository {repo} is checked out locally at {path} — your default reference for this PR: read the "
            "surrounding code, callers, tests and history there for an accurate review. Treat it as read-only: do not "
            "edit files, check out branches, commit or push in it.")
    if s.get("state") == "done":
        text += (f" The PR's head commit {s['sha'][:12]} is fetched there as `{s['ref']}` (not checked out): read a file "
                 f"of the PR with `git -C {path} show {s['ref']}:<path>`")
        if s.get("merge_base"):
            text += f", and see the whole change with `git -C {path} diff {s['merge_base'][:12]} {s['ref']}`"
        text += f". The working tree shows {'the base branch' if s.get('base_updated') else 'whatever is checked out there'}."
    return text


# ---------------------------------------------------------------------- change summaries from the git log

PERIODS = {"day": 1, "week": 7, "month": 31, "quarter": 92, "year": 366}
MAX_SUBJECTS = 1500


def period_range(period: str, end: str | None) -> tuple[str, str, str]:
    """(since, until, label) — a rolling window ending on `end` (YYYY-MM-DD, default today), inclusive."""
    import datetime as dt

    if period not in PERIODS:
        raise ApiError(400, "INVALID_PERIOD", "period must be day, week, month, quarter or year")
    try:
        e = dt.date.fromisoformat(end) if end else dt.date.today()
    except ValueError:
        raise ApiError(400, "INVALID_DATE", "end must be a date (YYYY-MM-DD)")
    if period == "day":
        s = e
    elif period == "week":
        s = e - dt.timedelta(days=6)
    else:
        months = {"month": 1, "quarter": 3, "year": 12}[period]
        y, m = e.year, e.month - months
        while m < 1:
            m += 12
            y -= 1
        import calendar

        s = dt.date(y, m, min(e.day, calendar.monthrange(y, m)[1])) + dt.timedelta(days=1)
    label = {"day": f"{e}", "week": f"week {s} – {e}", "month": f"month {s} – {e}", "quarter": f"quarter {s} – {e}",
             "year": f"year {s} – {e}"}[period]
    return s.isoformat(), e.isoformat(), label


def default_branch(path: str, src: str) -> str:
    code, out, _ = git(path, ["ls-remote", "--symref", src, "HEAD"], 60)
    if code == 0:
        m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", out)
        if m:
            return m.group(1)
    for b in ("main", "master"):
        if git(path, ["rev-parse", "--verify", "--quiet", f"refs/heads/{b}"])[0] == 0:
            return b
    return "main"


def change_log(link: dict[str, Any], repo: str, period: str, end: str | None) -> dict[str, Any]:
    """The material for a summary: the default branch's commits in the window (brought up to date first when the
    link's update option is on), per-area counts and the subjects."""
    path = link["path"]
    since, until, label = period_range(period, end)
    src = link.get("remote") or find_remote(path, repo) or f"https://github.com/{repo}.git"
    branch = default_branch(path, src)
    ref = f"refs/waw/base/{branch}"
    note = None
    if link.get("update", True):
        code, _, err = git(path, ["fetch", "--no-tags", "--quiet", src, f"+refs/heads/{branch}:{ref}"], SYNC_TIMEOUT)
        if code != 0:
            note = f"could not update {branch} ({err[-200:]}); using the local history"
    if git(path, ["rev-parse", "--verify", "--quiet", ref])[0] != 0:
        ref = branch if git(path, ["rev-parse", "--verify", "--quiet", branch])[0] == 0 else "HEAD"
    window = [f"--since={since}T00:00:00", f"--until={until}T23:59:59", "--no-merges"]
    code, subjects, err = git(path, ["log", ref, *window, "--date=short", "--format=%h%x09%ad%x09%an%x09%s"], 300)
    if code != 0:
        raise ApiError(502, "GIT_FAILED", f"git log failed: {err[-300:]}")
    lines = subjects.splitlines()
    _, names, _ = git(path, ["log", ref, *window, "--format=", "--name-only"], 300)
    areas: dict[str, int] = {}
    for n in names.splitlines():
        if n.strip():
            parts = n.split("/")
            a = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else "(top level)")
            areas[a] = areas.get(a, 0) + 1
    top = sorted(areas.items(), key=lambda kv: -kv[1])[:40]
    return {"since": since, "until": until, "label": label, "ref": ref, "branch": branch, "commits": len(lines),
            "subjects": lines[:MAX_SUBJECTS], "areas": top, "note": note, "path": path}


def summary_brief(repo: str, log_: dict[str, Any], focus: str) -> str:
    path, ref = log_["path"], log_["ref"]
    window = f"--since={log_['since']}T00:00:00 --until={log_['until']}T23:59:59 --no-merges"
    head = (f"Material: the git history of {repo} for the {log_['label']} ({log_['since']} to {log_['until']}), from the local "
            f"checkout at {path} (branch {log_['branch']}, ref `{ref}`): {log_['commits']} commits (merges excluded).")
    if log_.get("note"):
        head += f" Note: {log_['note']}."
    areas = "\n".join(f"{n:6d}  {a}" for a, n in log_["areas"])
    more = ""
    if log_["commits"] > len(log_["subjects"]):
        more = f"\n(only the newest {len(log_['subjects'])} subjects are listed; run `git -C {path} log {ref} {window} --format='%h %s'` for all)"
    return (head + "\n\nFiles changed per area (commit-file count):\n<<<AREAS\n" + areas + "\nAREAS>>>\n\n"
            "Commits (hash, date, author, subject; newest first):\n<<<COMMITS\n" + "\n".join(log_["subjects"]) + more + "\nCOMMITS>>>\n\n"
            f"Read any commit with `git -C {path} show --stat <hash>` / `git -C {path} show <hash>`; the checkout is read-only for you."
            + (f"\n\nThe user's focus: {focus}" if focus.strip() else ""))
