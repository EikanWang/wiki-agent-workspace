"""Read-only Git helpers. The app never commits, pushes, resets or rewrites history."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GitInfo:
    is_repo: bool = False
    branch: str | None = None
    commit: str | None = None
    toplevel: str | None = None
    remote_state: str | None = None  # ahead/behind summary when determinable
    statuses: dict[str, str] = field(default_factory=dict)  # relative path -> untracked|modified|clean|...
    error: str | None = None


def _run(args: list[str], cwd: Path, timeout: float = 10) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, "git executable not found"
    except subprocess.TimeoutExpired:
        return 124, "git timed out"
    return p.returncode, (p.stdout if p.returncode == 0 else p.stderr).strip()


def git_info(root: Path) -> GitInfo:
    info = GitInfo()
    code, out = _run(["rev-parse", "--show-toplevel"], root)
    if code != 0:
        if code == 127:
            info.error = out
        return info
    info.is_repo = True
    info.toplevel = out
    code, out = _run(["rev-parse", "--abbrev-ref", "HEAD"], root)
    if code == 0:
        info.branch = out if out != "HEAD" else "(detached)"
    code, out = _run(["rev-parse", "HEAD"], root)
    if code == 0:
        info.commit = out
    code, out = _run(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--branch", "."], root)
    if code == 0:
        entries = out.split("\0")
        for e in entries:
            if not e:
                continue
            if e.startswith("## "):
                head = e[3:]
                if "[" in head:
                    info.remote_state = head[head.index("[") + 1:].rstrip("]")
                elif "..." in head:
                    info.remote_state = "up to date"
                continue
            xy, path = e[:2], e[3:]
            # paths from `status .` are relative to the repo toplevel; convert to root-relative
            abs_path = Path(info.toplevel) / path
            try:
                rel = abs_path.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                try:
                    rel = Path(info.toplevel, path).relative_to(root).as_posix()
                except ValueError:
                    continue
            if xy == "??":
                info.statuses[rel] = "untracked"
            elif "D" in xy:
                info.statuses[rel] = "deleted"
            elif xy.strip():
                info.statuses[rel] = "modified"
    return info


async def git_info_async(root: Path) -> GitInfo:
    return await asyncio.to_thread(git_info, root)


def git_show(root: Path, ref: str, rel_path: str) -> tuple[bool, str]:
    """Return (ok, content_or_error) for `git show ref:path`."""
    if ref.startswith("-"):
        return False, "invalid ref"
    code, out = _run(["cat-file", "-e", f"{ref}:{rel_path}"], root)
    if code != 0:
        return False, out or "ref/path not found"
    try:
        p = subprocess.run(["git", "show", f"{ref}:{rel_path}"], cwd=str(root), capture_output=True, timeout=10, check=False)
    except subprocess.TimeoutExpired:
        return False, "git timed out"
    if p.returncode != 0:
        return False, p.stderr.decode("utf-8", "replace").strip()
    try:
        return True, p.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return False, "content is not valid UTF-8"
