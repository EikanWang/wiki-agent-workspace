"""Commit workflow: pick changed files, draft a commit message from the real diff with the agent, commit with the
user's Git identity, and optionally push / open a pull request.

Everything here runs plain `git` (and `gh` for pull requests) as the server's OS user with argument arrays only.
Nothing is ever committed, pushed or force-updated without an explicit request from the UI; history is never
rewritten (no amend, no reset, no force-push).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError, bad_request, conflict
from .gitinfo import git_info, git_run
from .locks import PATH_LOCKED
from .sessions import SessionService

router = APIRouter()

MAX_DIFF_FOR_PROMPT = 60_000  # characters of diff handed to the agent for the draft


def _git(root: Path, args: list[str], timeout: float = 60) -> tuple[int, str, str]:
    code, out, err = git_run(root, args, timeout=timeout)
    return code, out.decode("utf-8", "replace"), err


class CommitService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        assert ctx.documents
        self.docs = ctx.documents
        self.root = self.docs.root

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        info = git_info(self.root)
        if not info.is_repo:
            raise ApiError(409, "NOT_A_GIT_REPO", "the workspace is not a Git repository")
        code, out, err = _git(self.root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        if code != 0:
            raise ApiError(400, "GIT_ERROR", f"git status failed: {err[:300]}")
        files: list[dict[str, Any]] = []
        parts = out.split("\0")
        i = 0
        while i < len(parts):
            entry = parts[i]
            i += 1
            if len(entry) < 4:
                continue
            x, y, rel = entry[0], entry[1], entry[3:]
            if x == "R" or x == "C":  # renamed/copied: next part is the original path
                i += 1
            kind = ("untracked" if entry.startswith("??") else "deleted" if "D" in (x, y) else "added" if "A" in (x, y)
                    else "renamed" if "R" in (x, y) else "modified")
            lock = self.ctx.locks.covering(rel) if self.ctx.locks is not None else None
            files.append({"path": rel, "status": kind, "staged": x not in (" ", "?"), "unstaged": y not in (" ", "?"),
                          "is_markdown": rel.lower().endswith((".md", ".markdown")), "lock": lock})
        files.sort(key=lambda f: f["path"])
        code, out, _ = _git(self.root, ["config", "user.name"])
        name = out.strip() if code == 0 else ""
        code, out, _ = _git(self.root, ["config", "user.email"])
        email = out.strip() if code == 0 else ""
        code, out, _ = _git(self.root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        upstream = out.strip() if code == 0 else None
        ahead = behind = None
        if upstream:
            code, out, _ = _git(self.root, ["rev-list", "--left-right", "--count", f"{upstream}...HEAD"])
            if code == 0 and out.strip():
                b, a = out.split()
                ahead, behind = int(a), int(b)
        code, out, _ = _git(self.root, ["remote", "get-url", "origin"])
        origin = out.strip() if code == 0 else None
        code, out, _ = _git(self.root, ["log", "--max-count=10", "--format=%h%x00%s%x00%cr%x00%an"])
        recent = []
        if code == 0:
            for line in out.splitlines():
                p = line.split("\0")
                if len(p) == 4:
                    recent.append({"short": p[0], "subject": p[1], "when": p[2], "author": p[3]})
        gh = shutil.which("gh")
        gh_ready = False
        if gh and origin and "github.com" in origin:
            try:
                gh_ready = subprocess.run([gh, "auth", "status"], capture_output=True, timeout=15, check=False).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                gh_ready = False
        return {
            "branch": info.branch, "head": info.commit, "upstream": upstream, "ahead": ahead, "behind": behind,
            "origin": origin, "origin_is_github": bool(origin and "github.com" in origin), "gh_available": gh_ready,
            "identity": {"name": name, "email": email, "configured": bool(name and email)},
            "files": files, "recent_commits": recent,
        }

    # ------------------------------------------------------------------ helpers
    def _check_paths(self, paths: list[str]) -> list[str]:
        if not paths:
            raise bad_request("select at least one file", "NO_FILES")
        rels: list[str] = []
        for p in paths:
            rel = self.docs.normalize_rel(p)
            if not rel:
                raise bad_request("cannot commit the workspace root as a path", "INVALID_PATH")
            if self.ctx.locks is not None:
                lock = self.ctx.locks.covering(rel)
                if lock:
                    raise ApiError(423, PATH_LOCKED, f"{rel} is locked; unlock it before committing", {"lock": lock})
            rels.append(rel)
        return rels

    def _diff_for(self, rels: list[str]) -> str:
        """Unified diff of the selected files vs HEAD, including untracked files as additions."""
        code, out, _ = _git(self.root, ["diff", "HEAD", "--", *rels], timeout=120)
        if code != 0:
            # no HEAD yet (empty repository): compare against the empty tree
            code, out, _ = _git(self.root, ["diff", "4b825dc642cb6eb9a060e54bf8d69288fbee4904", "--", *rels], timeout=120)
        text = out if code == 0 else ""
        code2, untracked, _ = _git(self.root, ["ls-files", "--others", "--exclude-standard", "-z", "--", *rels])
        if code2 == 0:
            for raw in untracked.split("\0"):
                if not raw:
                    continue
                p = self.root / raw
                if p.is_file():
                    try:
                        body = p.read_text("utf-8")
                    except (OSError, UnicodeDecodeError):
                        body = "<binary or non-UTF-8 content>\n"
                    lines = body.splitlines()
                    text += f"diff --git a/{raw} b/{raw}\nnew file mode 100644\n--- /dev/null\n+++ b/{raw}\n@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{ln}\n" for ln in lines)
        return text

    # ------------------------------------------------------------------ draft message (agent)
    async def draft(self, body: "DraftIn") -> dict[str, Any]:
        rels = self._check_paths(body.paths)
        diff = self._diff_for(rels)
        if not diff.strip():
            raise conflict("NOTHING_TO_COMMIT", "the selected files have no changes against HEAD")
        truncated = len(diff) > MAX_DIFF_FOR_PROMPT
        shown = diff[:MAX_DIFF_FOR_PROMPT]
        assert self.ctx.providers and self.ctx.jobs
        svc = SessionService(self.ctx)
        if body.session_id:
            sess = svc.get(body.session_id)
        else:
            provider = self.ctx.providers.default_provider()
            if not provider:
                raise ApiError(503, "PROVIDER_UNAVAILABLE", "no default agent provider configured")
            sess = await svc.create_for_provider(provider, "Commit messages")
            with self.ctx.db.write() as s:
                r = s.get(type(sess), sess.id)
                if r:
                    r.origin = "commit"
        prompt = (
            "Write a Git commit message for the following changes to a Markdown wiki / repository.\n"
            "Rules: first line is a concise imperative subject of at most 72 characters; then a blank line; then a short "
            "body (bullet points are fine) explaining WHAT changed and WHY, based only on the diff below. Do not invent "
            "reasons that are not visible in the diff. Output ONLY the commit message, no code fences, no preamble.\n\n"
            f"Files ({len(rels)}): " + ", ".join(rels) + "\n\n"
            + ("[diff truncated to the first part]\n" if truncated else "")
            + "```diff\n" + shown + "\n```"
        )
        job_id = self.ctx.jobs.enqueue_turn(session_id=sess.id, thread_id=None, prompt=prompt,
                                            context={"commit_draft": True, "paths": rels}, agent_message_id=None, kind="commit_draft")
        return {"job_id": job_id, "session_id": sess.id, "files": rels, "diff_chars": len(diff), "truncated": truncated}

    # ------------------------------------------------------------------ commit
    def commit(self, body: "CommitIn") -> dict[str, Any]:
        rels = self._check_paths(body.paths)
        message = body.message.strip()
        if not message:
            raise bad_request("commit message is empty", "EMPTY_MESSAGE")
        if message.startswith("-"):
            raise bad_request("commit message cannot start with '-'", "INVALID_MESSAGE")
        st = self.status()
        if not st["identity"]["configured"]:
            raise ApiError(409, "GIT_IDENTITY_MISSING",
                           "Git user.name/user.email are not configured for this user; run `git config --global user.name …` and "
                           "`git config --global user.email …` in the terminal")
        changed = {f["path"] for f in st["files"]}
        missing = [r for r in rels if r not in changed]
        if missing:
            raise conflict("NOT_CHANGED", f"not changed in the working tree: {', '.join(missing)}")
        # Stage exactly the selected files (handles deletions too), then commit only what is staged.
        code, _, err = _git(self.root, ["add", "-A", "--", *rels])
        if code != 0:
            raise ApiError(400, "GIT_ERROR", f"git add failed: {err[:300]}")
        code, out, err = self._commit_with_message(message, rels)
        if code != 0:
            raise ApiError(400, "GIT_ERROR", f"git commit failed: {(err or out)[:500]}")
        code, sha, _ = _git(self.root, ["rev-parse", "HEAD"])
        head = sha.strip()
        code, subj, _ = _git(self.root, ["log", "-1", "--format=%s"])
        if self.ctx.events:
            self.ctx.events.publish("global", {"type": "tree_changed"})
            self.ctx.events.publish("global", {"type": "commit", "sha": head, "subject": subj.strip(), "files": rels})
        return {"sha": head, "short": head[:7], "subject": subj.strip(), "files": rels, "branch": st["branch"]}

    def _commit_with_message(self, message: str, rels: list[str]) -> tuple[int, str, str]:
        try:
            p = subprocess.run(["git", "commit", "--only", "-F", "-", "--", *rels], cwd=str(self.root), input=message.encode("utf-8"),
                               capture_output=True, timeout=120, check=False)
        except subprocess.TimeoutExpired:
            return 124, "", "git commit timed out"
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    # ------------------------------------------------------------------ push / PR
    def push(self, body: "PushIn") -> dict[str, Any]:
        st = self.status()
        branch = st["branch"]
        if not branch or branch == "(detached)":
            raise conflict("DETACHED_HEAD", "cannot push from a detached HEAD; check out a branch in the terminal")
        if not st["origin"]:
            raise conflict("NO_REMOTE", "the repository has no 'origin' remote")
        remote = body.remote or "origin"
        if remote.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.\-/]+", remote):
            raise bad_request("invalid remote name", "INVALID_REMOTE")
        args = ["push", "--porcelain"]
        if not st["upstream"]:
            args += ["--set-upstream"]
        args += [remote, f"HEAD:refs/heads/{branch}"]
        code, out, err = _git(self.root, args, timeout=300)
        if code != 0:
            combined = (out + "\n" + err).strip()
            low = combined.lower()
            if "rejected" in low or "non-fast-forward" in low or "fetch first" in low:
                raise conflict("PUSH_REJECTED", "the remote branch has commits you do not have; pull/rebase in the terminal first (the app never force-pushes)",
                               detail=combined[-600:])
            if "authentication" in low or "permission denied" in low or "could not read username" in low or "403" in low:
                raise ApiError(409, "PUSH_AUTH", "git push was refused for authentication reasons; fix your credentials/SSH key in the terminal", {"detail": combined[-600:]})
            raise ApiError(400, "GIT_ERROR", f"git push failed: {combined[-600:]}")
        st2 = self.status()
        return {"remote": remote, "branch": branch, "upstream": st2["upstream"], "ahead": st2["ahead"], "output": (out or err)[-800:]}

    def pull_request(self, body: "PrIn") -> dict[str, Any]:
        st = self.status()
        gh = shutil.which("gh")
        if not gh or not st["gh_available"]:
            raise ApiError(409, "GH_UNAVAILABLE", "GitHub CLI (gh) is not installed or not logged in; run `gh auth login` in the terminal")
        if not st["origin_is_github"]:
            raise conflict("NOT_GITHUB", "origin is not a GitHub repository")
        branch = st["branch"]
        if not branch or branch == "(detached)":
            raise conflict("DETACHED_HEAD", "check out a branch first")
        base = body.base or ""
        if base and (base.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.\-/]+", base)):
            raise bad_request("invalid base branch", "INVALID_BRANCH")
        if base and base == branch:
            raise conflict("SAME_BRANCH", f"you are on '{branch}'; create and push a feature branch (e.g. in the terminal: git switch -c my-change) before opening a PR into it")
        title = body.title.strip()
        if not title or title.startswith("-"):
            raise bad_request("PR title is required", "INVALID_TITLE")
        args = [gh, "pr", "create", "--title", title, "--body", body.body or "", "--head", branch]
        if base:
            args += ["--base", base]
        if body.draft:
            args.append("--draft")
        try:
            p = subprocess.run(args, cwd=str(self.root), capture_output=True, text=True, timeout=120, check=False)
        except subprocess.TimeoutExpired:
            raise ApiError(504, "GH_TIMEOUT", "gh pr create timed out")
        if p.returncode != 0:
            msg = (p.stderr or p.stdout).strip()
            if "already exists" in msg:
                raise conflict("PR_EXISTS", msg[-500:])
            raise ApiError(400, "GH_ERROR", f"gh pr create failed: {msg[-600:]}")
        url = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""
        return {"url": url, "head": branch, "base": base or None, "draft": body.draft}

# ---------------------------------------------------------------------- schemas


class DraftIn(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=400)
    session_id: str | None = None


class CommitIn(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=400)
    message: str = Field(min_length=1, max_length=20000)


class PushIn(BaseModel):
    remote: str | None = None


class PrIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str | None = Field(None, max_length=20000)
    base: str | None = None
    draft: bool = False


# ---------------------------------------------------------------------- routes


@router.get("/git/status")
def git_status(ctx: AppContext = Depends(require_auth)):
    return CommitService(ctx).status()


@router.post("/git/draft-message", status_code=202)
async def draft_message(body: DraftIn, ctx: AppContext = Depends(require_auth_mutating)):
    return await CommitService(ctx).draft(body)


@router.post("/git/commit", status_code=201)
def commit(body: CommitIn, ctx: AppContext = Depends(require_auth_mutating)):
    return CommitService(ctx).commit(body)


@router.post("/git/push")
def push(body: PushIn, ctx: AppContext = Depends(require_auth_mutating)):
    return CommitService(ctx).push(body)


@router.post("/git/pull-request", status_code=201)
def pull_request(body: PrIn, ctx: AppContext = Depends(require_auth_mutating)):
    return CommitService(ctx).pull_request(body)
