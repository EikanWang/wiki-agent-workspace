"""The skills an agent PR review gets, per repository, and editing them (Reviews ▸ settings ▸ Skills).

A review turn of `owner/name` runs in the repository's agent wiki (else the wiki of the request) and gets the skills of
the PR's connected local checkout, the wiki, every open folder and every connected folder (`skills.discover`, first
definition of a name wins), listed in its prompt. Skills are discovered anew for every turn, so a saved SKILL.md is
what the next review reads; the list is read again after a save to show what that review will see.

Only SKILL.md files of that list can be read or written here, and a write must name the version it replaces.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .errors import ApiError
from .github_hub import require_owner, require_owner_mutating

router = APIRouter()

MAX_SKILL = 512 * 1024
_REVIEWISH = re.compile(r"review|code quality|lint|style guide|convention", re.I)
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def known_repos(ctx: Any) -> list[str]:
    """Repositories with an agent wiki or a connected local checkout."""
    from .models import GithubRepoWiki, KvSetting

    out: set[str] = set()
    with ctx.db.read() as s:
        out.update(r.repo for r in s.query(GithubRepoWiki).all())
        out.update(k.key.split(":", 1)[1].lower() for k in s.query(KvSetting).filter(KvSetting.key.like("local_repo:%")).all())
    return sorted(x for x in out if _REPO.match(x))


def review_folders(ctx: Any, repo: str | None) -> tuple[list[Path], str | None, str | None]:
    """(folders in the order a review turn of `repo` sees them, its wiki id, its local checkout)."""
    from .github_hub import repo_wiki
    from .local_repos import get_link

    wiki = repo_wiki(ctx, repo) if repo else None
    root = ctx.workspace_root
    if wiki and ctx.workspaces is not None and wiki in ctx.workspaces.runtimes:
        root = ctx.workspaces.runtimes[wiki].root
    link = get_link(ctx, repo) if repo else None
    checkout = str(link["path"]) if link else None
    assert ctx.jobs is not None
    folders = ctx.jobs.skill_folders(root, None, {"_skill_folders": [checkout] if checkout else []})
    return folders, wiki, checkout


def _git_owner(path: Path) -> str | None:
    """The top of the git checkout that tracks this file, if any."""
    from .local_repos import git

    rc, top, _ = git(str(path.parent), ["rev-parse", "--show-toplevel"], timeout=10)
    if rc != 0 or not top.strip():
        return None
    rc, _, _ = git(str(path.parent), ["ls-files", "--error-unmatch", path.name], timeout=10)
    return top.strip() if rc == 0 else None


def _listing(ctx: Any, repo: str | None) -> dict[str, Any]:
    from . import skills as sk
    from .models import KvSetting

    folders, wiki, checkout = review_folders(ctx, repo)
    found = sk.discover(folders)
    checkouts: dict[str, str] = {}  # connected local checkout → its repository
    with ctx.db.read() as s:
        for k in s.query(KvSetting).filter(KvSetting.key.like("local_repo:%")).all():
            try:
                p = json.loads(k.value).get("path")
            except (ValueError, AttributeError):
                continue
            if p:
                checkouts[str(Path(p).resolve())] = k.key.split(":", 1)[1]
    out = []
    for s_ in found:
        p = Path(s_["path"])
        try:
            head = p.read_text("utf-8", "replace")[:4000]
        except OSError:
            head = ""
        top = _git_owner(p)
        out.append({**s_, "review": bool(_REVIEWISH.search(f"{s_['name']} {s_['description']}")),
                    "has_frontmatter": bool(sk._FRONT.match(head)), "tracked_by": top,
                    "checkout_of": checkouts.get(str(Path(top).resolve())) if top else None})
    return {"repo": repo, "wiki": wiki, "checkout": checkout, "folders": [str(f) for f in folders], "skills": out,
            "repos": known_repos(ctx)}


def _allowed(ctx: Any, path: str) -> Path:
    """The SKILL.md must be one some review gets (any known repository, or none)."""
    from . import skills as sk

    p = Path(path)
    if p.name != "SKILL.md" or not p.is_absolute():
        raise ApiError(400, "NOT_A_SKILL", "only a skill's SKILL.md can be edited here")
    for repo in [None, *known_repos(ctx)]:
        if any(s["path"] == str(p) for s in sk.discover(review_folders(ctx, repo)[0])):
            return p
    raise ApiError(404, "SKILL_NOT_FOUND", f"{p} is not a skill an agent review gets")


@router.get("/github/review-skills")
def list_review_skills(repo: str | None = Query(None, max_length=200), auth: tuple[Any, str] = Depends(require_owner)):
    """The skills an agent review of `repo` gets (default: the first known repository), review-related ones flagged."""
    ctx, _ = auth
    if repo and not _REPO.match(repo):
        raise ApiError(400, "BAD_REPO", "repo must be owner/name")
    repos = known_repos(ctx)
    return _listing(ctx, (repo or (repos[0] if repos else None) or "").lower() or None)


@router.get("/github/review-skills/file")
def read_review_skill(path: str = Query(..., max_length=4096), auth: tuple[Any, str] = Depends(require_owner)):
    ctx, _ = auth
    p = _allowed(ctx, path)
    data = p.read_bytes()
    if len(data) > MAX_SKILL:
        raise ApiError(413, "FILE_TOO_LARGE", "SKILL.md is too large to edit here")
    import os

    return {"path": str(p), "text": data.decode("utf-8", "replace"), "sha256": _sha(data), "writable": os.access(p, os.W_OK)}


class SkillWriteIn(BaseModel):
    path: str = Field(max_length=4096)
    text: str = Field(max_length=MAX_SKILL)
    base_sha256: str = Field(min_length=64, max_length=64)
    repo: str | None = Field(None, max_length=200)  # the list to answer with


@router.put("/github/review-skills/file")
def write_review_skill(body: SkillWriteIn, auth: tuple[Any, str] = Depends(require_owner_mutating)):
    """Save a SKILL.md (only over the version it was read as), then list the skills again: the next review uses them."""
    from . import skills as sk

    ctx, _ = auth
    p = _allowed(ctx, body.path)
    cur = _sha(p.read_bytes())
    if cur != body.base_sha256:
        raise ApiError(409, "FILE_CHANGED", f"{p} changed on disk since it was opened; reload it", {"current_sha256": cur})
    if not sk._FRONT.match(body.text):
        raise ApiError(400, "NO_FRONTMATTER", "keep the --- name: / description: --- header at the top: the agent picks a "
                       "skill by its name and description")
    data = body.text.encode("utf-8")
    tmp = p.with_name(".SKILL.md.waw-tmp")
    try:
        tmp.write_bytes(data)
        tmp.chmod(p.stat().st_mode & 0o777)
        tmp.replace(p)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise ApiError(403, "PERMISSION_DENIED", f"cannot write {p}")
    repo = body.repo.lower() if body.repo and _REPO.match(body.repo) else None
    return {"sha256": _sha(data), **_listing(ctx, repo)}
