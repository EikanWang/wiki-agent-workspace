"""Development workspaces: an app-owned Git worktree per piece of work, on the app server or an SSH host.

* New work (`mode=branch`): `git worktree add -b <branch> <path> <base>` from the chosen checkout.
* An existing pull request (`mode=pr`): the PR head is fetched as `refs/waw/pr/<n>` and checked out in the worktree
  on the local branch `waw/pr-<n>`; pushes go back to the PR's head branch (its fork when the PR comes from one —
  GitHub then needs "allow edits by maintainers").

The user's own checkout is never switched, reset or committed to. History is never rewritten (no force-push).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shlex
from pathlib import Path
from typing import Any

from ..db import iso, new_id, utcnow
from ..deps import AppContext
from ..errors import ApiError, bad_request, conflict, not_found
from ..models import DevWorkspace, Document
from . import DEV_PREFIX
from .targets import LOCAL, Target, TargetError, target_for

log = logging.getLogger("waw.development")

DEV_NOT_FOUND = "DEV_WORKSPACE_NOT_FOUND"
_ID = re.compile(r"^[0-9a-f-]{36}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_tasks: dict[str, asyncio.Task] = {}


def dev_snapshot_path(snapshots_dir: Path, norm: str) -> Path | None:
    name = norm[len(DEV_PREFIX):]
    if not name.endswith(".md") or not _ID.match(name[:-3]):
        return None
    return snapshots_dir / "dev" / name


def dev_doc_path(dev_id: str) -> str:
    return f"{DEV_PREFIX}{dev_id}.md"


def parse_github_repo(url: str) -> str | None:
    m = re.search(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url.strip())
    return m.group(1) if m else None


def settings_of(row: DevWorkspace) -> dict[str, Any]:
    try:
        v = json.loads(row.settings_json or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


# ---------------------------------------------------------------------- language / task defaults

SETTING_KEYS = ("editor", "python", "python_lsp", "compile_commands_dir", "clangd", "build_command", "test_command", "debug_program", "debug_args")
EDITORS = ("web", "vscode")  # the editor of a workspace: the app's own web editor (Monaco + language servers) or code-server


def detect_settings(files: set[str]) -> dict[str, Any]:
    """Sensible C++ / Python defaults from the files at the top of the checkout (all editable later)."""
    out: dict[str, Any] = {"languages": []}
    build, test = [], []
    if "CMakeLists.txt" in files:
        out["languages"].append("cpp")
        out["compile_commands_dir"] = "build"
        build.append("cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON && cmake --build build")
        test.append("ctest --test-dir build --output-on-failure")
    elif "compile_commands.json" in files or "Makefile" in files:
        out["languages"].append("cpp")
        out["compile_commands_dir"] = "."
        if "Makefile" in files:
            build.append("make -j")
    if files & {"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "requirements.txt"}:
        out["languages"].append("python")
        out["python"] = "python3"
        test.append("python3 -m pytest -q")
    out["build_command"] = " && ".join(build) or None
    out["test_command"] = " && ".join(test) or None
    return out


def clean_settings(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in SETTING_KEYS:
        v = raw.get(k)
        if v is None or v == "":
            continue
        if k == "debug_args":
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise bad_request("debug_args must be a list of strings", "INVALID_SETTINGS")
            out[k] = [x[:1000] for x in v][:50]
            continue
        if not isinstance(v, str) or len(v) > 4000 or "\0" in v:
            raise bad_request(f"invalid {k}", "INVALID_SETTINGS")
        if k == "editor" and v not in EDITORS:
            raise bad_request("editor must be web or vscode", "INVALID_SETTINGS")
        out[k] = v.strip()
    return out


# ---------------------------------------------------------------------- serialisation


def dev_dict(row: DevWorkspace, ctx: AppContext) -> dict[str, Any]:
    editor = ctx.extra.get("dev_editor")
    tasks = ctx.extra.get("dev_tasks")
    return {
        "id": row.id, "name": row.name, "host": row.host, "source_repo": row.source_repo, "worktree_path": row.worktree_path,
        "mode": row.mode, "branch": row.branch, "base_ref": row.base_ref, "github_repo": row.github_repo,
        "pr_number": row.pr_number, "push_remote": row.push_remote, "push_branch": row.push_branch,
        "wiki_workspace_id": row.wiki_workspace_id, "document_id": row.document_id, "document_path": dev_doc_path(row.id),
        "settings": {"editor": "web", **settings_of(row)}, "status": row.status, "error": row.error,
        "editor": editor.status(row.id) if editor else {"state": "stopped"},
        "running_tasks": tasks.running_count(row.id) if tasks else 0,
        "created_at": iso(row.created_at), "updated_at": iso(row.updated_at),
    }


# ---------------------------------------------------------------------- PR metadata (gh on the app server)


async def fetch_pr_meta(repo: str, number: int) -> dict[str, Any]:
    """headRefName, headRepository, baseRefName, isCrossRepository, maintainerCanModify, title, url, state.
    Module-level so tests can replace it."""
    import shutil

    gh = shutil.which("gh")
    if not gh:
        raise ApiError(409, "GH_UNAVAILABLE", "the GitHub CLI (gh) is not installed on the server; it is needed to open an existing pull request")
    proc = await asyncio.create_subprocess_exec(
        gh, "pr", "view", str(number), "--repo", repo, "--json",
        "headRefName,headRepository,headRepositoryOwner,baseRefName,isCrossRepository,maintainerCanModify,title,url,state",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    except TimeoutError:
        proc.kill()
        raise ApiError(504, "GH_TIMEOUT", "gh pr view timed out")
    if proc.returncode != 0:
        raise ApiError(502, "GH_FAILED", f"gh pr view failed: {err.decode('utf-8', 'replace').strip()[:300]}")
    d = json.loads(out)
    owner = (d.get("headRepositoryOwner") or {}).get("login")
    name = (d.get("headRepository") or {}).get("name")
    d["head_repo"] = f"{owner}/{name}" if owner and name else repo
    return d


# ---------------------------------------------------------------------- the service


class DevService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.dev = ctx.settings.development

    # ------------------------------------------------------------------ lookups
    def get(self, dev_id: str) -> DevWorkspace:
        with self.ctx.db.read() as s:
            row = s.get(DevWorkspace, dev_id)
        if row is None:
            raise not_found("development workspace not found", DEV_NOT_FOUND)
        return row

    def for_document(self, doc: Document) -> DevWorkspace | None:
        with self.ctx.db.read() as s:
            return s.query(DevWorkspace).filter_by(document_id=doc.id).one_or_none()

    def list(self, include_archived: bool = False) -> list[dict[str, Any]]:
        with self.ctx.db.read() as s:
            q = s.query(DevWorkspace)
            if not include_archived:
                q = q.filter(DevWorkspace.status != "archived")
            rows = q.order_by(DevWorkspace.updated_at.desc()).all()
        return [dev_dict(r, self.ctx) for r in rows]

    def target(self, row_or_host: DevWorkspace | str) -> Target:
        host = row_or_host if isinstance(row_or_host, str) else row_or_host.host
        try:
            return target_for(self.dev, host)
        except TargetError as exc:
            raise ApiError(400, exc.code, exc.message)

    def hosts(self) -> list[dict[str, Any]]:
        return [{"name": LOCAL, "label": "This server", "ssh": None}] + [
            {"name": h.name, "label": h.name, "ssh": h.ssh} for h in self.dev.hosts]

    def wiki_root(self, row: DevWorkspace) -> Path | None:
        if not row.wiki_workspace_id or self.ctx.workspaces is None:
            return None
        rt = self.ctx.workspaces.runtimes.get(row.wiki_workspace_id)
        return rt.root if rt else None

    async def worktrees_dir(self, target: Target) -> str:
        if target.is_local:
            d = self.dev.worktrees_dir or (self.ctx.settings.state_dir / "dev" / "worktrees")
            Path(d).mkdir(parents=True, exist_ok=True, mode=0o700)
            return str(Path(d).resolve())
        host = self.dev.host(target.name)
        assert host is not None
        base = await target.expand(host.work_dir)
        d = f"{base}/worktrees"
        await target.mkdir(d)
        return d

    def _publish(self, row_id: str) -> None:
        if self.ctx.events:
            self.ctx.events.publish("global", {"type": "dev_changed", "dev_id": row_id})

    def _update(self, dev_id: str, **fields: Any) -> DevWorkspace:
        with self.ctx.db.write() as s:
            row = s.get(DevWorkspace, dev_id)
            if row is None:
                raise not_found("development workspace not found", DEV_NOT_FOUND)
            for k, v in fields.items():
                setattr(row, k, v)
            row.updated_at = utcnow()
        self._publish(dev_id)
        return row

    # ------------------------------------------------------------------ create
    async def create(self, body: Any) -> dict[str, Any]:
        host = body.host or LOCAL
        target = self.target(host)
        if body.mode not in ("branch", "pr"):
            raise bad_request("mode must be branch or pr", "INVALID_MODE")
        source = (body.source_repo or "").strip()
        if not source or source.startswith("-"):
            raise bad_request("give the path of a local Git checkout of the repository (on the chosen host)", "INVALID_PATH")
        if body.mode == "pr":
            if not body.github_repo or not _REPO.match(body.github_repo) or not body.pr_number or body.pr_number < 1:
                raise bad_request("an existing pull request needs its repository (owner/name) and number", "INVALID_PR")
        else:
            br = (body.branch or "").strip()
            if not br or br.startswith("-") or not re.fullmatch(r"[\w./-]+", br) or ".." in br or br.endswith((".lock", "/", ".")):
                raise bad_request("give a valid new branch name (letters, digits, - _ . /)", "INVALID_BRANCH")
            if body.github_repo and not _REPO.match(body.github_repo):
                raise bad_request("repository must be owner/name", "INVALID_PR")
            base = (body.base or "").strip()
            if base and (base.startswith("-") or not re.fullmatch(r"[\w./@^~-]+", base) or ".." in base):
                raise bad_request("give the base as a branch, tag or commit", "INVALID_BRANCH")
        if body.wiki_workspace_id and self.ctx.workspaces is not None:
            self.ctx.workspaces.get(body.wiki_workspace_id)  # 404/409 when not open
        conv_ws = body.wiki_workspace_id or (self.ctx.workspaces.primary.id if self.ctx.workspaces else self.ctx.workspace_id)
        try:
            source_abs = await target.expand(source)
        except TargetError as exc:
            raise ApiError(400, exc.code, exc.message)
        dev_id = new_id()
        name = (body.name or "").strip() or (f"{body.github_repo}#{body.pr_number}" if body.mode == "pr" else body.branch.strip())
        # the agent conversation: a hidden document of the reference wiki folder (or the primary folder)
        rt = self.ctx.workspaces.get(conv_ws) if self.ctx.workspaces else None
        docs = rt.docs if rt else self.ctx.documents
        assert docs is not None
        norm = dev_doc_path(dev_id)
        stub = dev_snapshot_path(docs.snapshots_dir, norm)
        assert stub is not None
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(f"# {name}\n\nPR Development workspace — the agent works in its Git worktree "
                        f"({'existing pull request' if body.mode == 'pr' else 'new branch'}) with the wiki folder as reference.\n",
                        encoding="utf-8")
        doc = docs.get_or_create_document(norm)
        with self.ctx.db.write() as s:
            d = s.get(Document, doc.id)
            if d is not None:
                d.hidden = True  # not in Recent / chats lists; it is shown inside PR Development
            s.add(DevWorkspace(
                id=dev_id, name=name[:200], host=host, source_repo=source_abs, worktree_path="", mode=body.mode,
                branch=(body.branch or "").strip() if body.mode == "branch" else f"waw/pr-{body.pr_number}",
                base_ref=(body.base or "").strip() or None, github_repo=body.github_repo or None, pr_number=body.pr_number if body.mode == "pr" else None,
                wiki_workspace_id=body.wiki_workspace_id or None, document_id=doc.id,
                settings_json=json.dumps(clean_settings(body.settings or {})), status="creating"))
        self._start(dev_id, self._provision(dev_id))
        return dev_dict(self.get(dev_id), self.ctx)

    def _start(self, key: str, coro: Any) -> None:
        async def run() -> None:
            try:
                await coro
            except ApiError as exc:
                self._update(key, status="failed", error=exc.message)
            except TargetError as exc:
                self._update(key, status="failed", error=exc.message)
            except Exception as exc:  # never leave an unretrieved task exception
                log.exception("provisioning %s failed", key)
                self._update(key, status="failed", error=f"{exc.__class__.__name__}: {exc}")
        _tasks[key] = asyncio.get_running_loop().create_task(run())

    async def _provision(self, dev_id: str) -> None:
        row = self.get(dev_id)
        t = self.target(row)
        rc, top, err = await t.git(row.source_repo, ["rev-parse", "--show-toplevel"], timeout=60)
        if rc != 0:
            raise ApiError(400, "NOT_A_GIT_REPO", f"{row.source_repo} is not a Git checkout on {t.label()}: {err.strip()[:300]}")
        src = top.strip()
        repo_name = src.rstrip("/").rsplit("/", 1)[-1] or "repo"
        wt = f"{await self.worktrees_dir(t)}/{re.sub(r'[^A-Za-z0-9._-]', '-', repo_name)}-{dev_id[:8]}"
        github_repo = row.github_repo
        _, remotes, _ = await t.git(src, ["remote", "-v"])
        origin_repo: dict[str, str] = {}
        for line in remotes.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                r = parse_github_repo(parts[1])
                if r:
                    origin_repo.setdefault(r.lower(), parts[0])
        fields: dict[str, Any] = {"source_repo": src, "worktree_path": wt}
        if row.mode == "pr":
            assert github_repo and row.pr_number
            meta = await fetch_pr_meta(github_repo, row.pr_number)
            n = row.pr_number
            fetch_src = origin_repo.get(github_repo.lower()) or f"https://github.com/{github_repo}.git"
            base = meta.get("baseRefName") or row.base_ref or "main"
            rc, _, err = await t.git(src, ["fetch", "--no-tags", "--quiet", fetch_src, f"+refs/pull/{n}/head:refs/waw/pr/{n}",
                                           f"+refs/heads/{base}:refs/waw/base/{base}"], timeout=900)
            if rc != 0:
                raise ApiError(502, "GIT_FETCH_FAILED", f"could not fetch the pull request: {err.strip()[-400:]}")
            rc, _, err = await t.git(src, ["worktree", "add", "-B", row.branch, wt, f"refs/waw/pr/{n}"], timeout=600)
            if rc != 0:
                raise ApiError(400, "WORKTREE_FAILED", f"git worktree add failed: {err.strip()[-400:]}")
            head_repo = meta.get("head_repo") or github_repo
            if meta.get("isCrossRepository"):
                push_remote = origin_repo.get(head_repo.lower()) or f"https://github.com/{head_repo}.git"
            else:
                push_remote = origin_repo.get(github_repo.lower()) or f"https://github.com/{github_repo}.git"
            fields.update(base_ref=f"refs/waw/base/{base}", push_remote=push_remote, push_branch=meta.get("headRefName"))
            if row.name == f"{github_repo}#{n}" and meta.get("title"):
                fields["name"] = f"#{n} {meta['title']}"[:200]
            await t.git(wt, ["update-ref", f"refs/waw/pushed/{row.branch}", f"refs/waw/pr/{n}"])
            if meta.get("isCrossRepository") and not meta.get("maintainerCanModify"):
                fields["error"] = ("This PR comes from a fork that does not allow edits by maintainers: you can develop and "
                                   "test here, but pushing to its branch will be refused.")
        else:
            base = row.base_ref
            if not base:
                _, cur, _ = await t.git(src, ["symbolic-ref", "--quiet", "--short", "HEAD"])
                base = cur.strip() or "HEAD"
            rc, _, _ = await t.git(src, ["rev-parse", "--verify", "--quiet", f"refs/heads/{row.branch}"])
            args = ["worktree", "add", wt, row.branch] if rc == 0 else ["worktree", "add", "-b", row.branch, wt, base]
            rc, _, err = await t.git(src, args, timeout=600)
            if rc != 0:
                raise ApiError(400, "WORKTREE_FAILED", f"git worktree add failed: {err.strip()[-400:]}")
            if not github_repo:
                rc, url, _ = await t.git(src, ["remote", "get-url", "origin"])
                github_repo = parse_github_repo(url) if rc == 0 else None
            remote = (origin_repo.get(github_repo.lower()) if github_repo else None) or ("origin" if "origin" in remotes.split() else None)
            fields.update(base_ref=base, github_repo=github_repo, push_remote=remote, push_branch=row.branch)
        rc, ls, _ = await t.git(wt, ["ls-files", "--", "CMakeLists.txt", "pyproject.toml", "setup.py", "setup.cfg", "pytest.ini",
                                     "tox.ini", "requirements.txt", "Makefile", "compile_commands.json"])
        detected = detect_settings({x.strip() for x in ls.splitlines() if x.strip()})
        merged = {**{k: v for k, v in detected.items() if v}, **settings_of(row)}
        fields["settings_json"] = json.dumps(merged)
        self._update(dev_id, **fields)
        if not t.is_local and row.wiki_workspace_id:
            from .agent import sync_wiki_snapshot

            try:
                await sync_wiki_snapshot(self.ctx, self.get(dev_id))
            except (ApiError, TargetError) as exc:
                fields["error"] = f"The wiki snapshot could not be copied to {t.name}: {getattr(exc, 'message', exc)}"
        self._update(dev_id, status="ready", error=fields.get("error"))

    async def wait_ready(self, dev_id: str, timeout: float = 120) -> None:
        """Tests / callers that need provisioning finished."""
        task = _tasks.get(dev_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout)

    # ------------------------------------------------------------------ settings / archive
    def update(self, dev_id: str, body: Any) -> dict[str, Any]:
        row = self.get(dev_id)
        fields: dict[str, Any] = {}
        if body.name is not None and body.name.strip():
            fields["name"] = body.name.strip()[:200]
        if body.settings is not None:
            fields["settings_json"] = json.dumps({**settings_of(row), **clean_settings(body.settings),
                                                  **{k: None for k in SETTING_KEYS if body.settings.get(k) == ""}})
        if body.wiki_workspace_id is not None:
            if body.wiki_workspace_id and self.ctx.workspaces is not None:
                self.ctx.workspaces.get(body.wiki_workspace_id)
            fields["wiki_workspace_id"] = body.wiki_workspace_id or None
        row = self._update(dev_id, **fields)
        return dev_dict(self.get(dev_id), self.ctx)

    async def archive(self, dev_id: str, remove_worktree: bool, force: bool) -> dict[str, Any]:
        row = self.get(dev_id)
        editor = self.ctx.extra.get("dev_editor")
        if editor:
            await editor.stop(dev_id)
        removed = False
        if remove_worktree and row.worktree_path:
            t = self.target(row)
            if not force:
                st = await self.status(dev_id)
                if st["files"]:
                    raise conflict("WORKTREE_DIRTY", f"the worktree has {len(st['files'])} uncommitted change(s); commit them or remove anyway")
                if st.get("unpushed"):
                    raise conflict("WORKTREE_UNPUSHED", f"{st['unpushed']} commit(s) are not pushed; push them or remove anyway")
            rc, _, err = await t.git(row.source_repo, ["worktree", "remove", *(["--force"] if force else []), row.worktree_path])
            if rc != 0 and await t.is_dir(row.worktree_path):
                raise ApiError(400, "WORKTREE_REMOVE_FAILED", f"git worktree remove failed: {err.strip()[:300]}")
            removed = True
        self._update(dev_id, status="archived")
        return {"archived": dev_id, "worktree_removed": removed}

    # ------------------------------------------------------------------ git
    def _ready(self, dev_id: str) -> tuple[DevWorkspace, Target]:
        row = self.get(dev_id)
        if row.status != "ready" or not row.worktree_path:
            raise conflict("DEV_NOT_READY", f"the development workspace is {row.status}" + (f": {row.error}" if row.error else ""))
        return row, self.target(row)

    async def status(self, dev_id: str) -> dict[str, Any]:
        row, t = self._ready(dev_id)
        wt = row.worktree_path
        rc, out, err = await t.git(wt, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        if rc != 0:
            raise ApiError(400, "GIT_ERROR", f"git status failed: {err.strip()[:300]}")
        files: list[dict[str, Any]] = []
        parts = out.split("\0")
        i = 0
        while i < len(parts):
            e = parts[i]
            i += 1
            if len(e) < 4:
                continue
            x, y, rel = e[0], e[1], e[3:]
            if x in "RC":
                i += 1
            kind = ("untracked" if e.startswith("??") else "deleted" if "D" in (x, y) else "added" if "A" in (x, y)
                    else "renamed" if "R" in (x, y) else "modified")
            files.append({"path": rel, "status": kind})
        _, head, _ = await t.git(wt, ["rev-parse", "--short", "HEAD"])
        _, subject, _ = await t.git(wt, ["log", "-1", "--format=%s"])
        ahead = None
        if row.base_ref:
            c, n, _ = await t.git(wt, ["rev-list", "--count", f"{row.base_ref}..HEAD"])
            ahead = int(n.strip()) if c == 0 and n.strip().isdigit() else None
        unpushed = None
        c, n, _ = await t.git(wt, ["rev-list", "--count", f"refs/waw/pushed/{row.branch}..HEAD"])
        if c == 0 and n.strip().isdigit():
            unpushed = int(n.strip())
        elif ahead is not None:
            unpushed = ahead  # never pushed: every commit of the branch
        _, name, _ = await t.git(wt, ["config", "user.name"])
        _, email, _ = await t.git(wt, ["config", "user.email"])
        return {"branch": row.branch, "head": head.strip(), "subject": subject.strip(), "files": sorted(files, key=lambda f: f["path"]),
                "commits_on_branch": ahead, "unpushed": unpushed, "push_remote": row.push_remote, "push_branch": row.push_branch,
                "identity": {"name": name.strip(), "email": email.strip(), "configured": bool(name.strip() and email.strip())}}

    async def diff(self, dev_id: str, path: str | None = None, max_bytes: int = 400_000) -> dict[str, Any]:
        row, t = self._ready(dev_id)
        args = ["diff", "--no-color", "--no-ext-diff", "HEAD"]
        if path:
            args += ["--", path]
        rc, out, err = await t.git(row.worktree_path, args)
        if rc != 0:
            raise ApiError(400, "GIT_ERROR", f"git diff failed: {err.strip()[:300]}")
        st = await self.status(dev_id)
        untracked = [f["path"] for f in st["files"] if f["status"] == "untracked" and (not path or f["path"] == path)]
        for rel in untracked[:50]:  # show new files like `git diff` would after `git add -N`
            c, text, _ = await t.run(["git", "-C", row.worktree_path, "diff", "--no-color", "--no-index", "--", "/dev/null", rel], timeout=60)
            if c in (0, 1):
                out += text
        return {"diff": out[:max_bytes], "truncated": len(out) > max_bytes, "files": st["files"]}

    async def commit(self, dev_id: str, message: str, paths: list[str] | None) -> dict[str, Any]:
        row, t = self._ready(dev_id)
        msg = message.strip()
        if not msg:
            raise bad_request("a commit message is required", "INVALID_MESSAGE")
        st = await self.status(dev_id)
        if not st["identity"]["configured"]:
            raise conflict("GIT_IDENTITY_MISSING", "set git user.name and user.email on the host first (e.g. in the terminal)")
        known = {f["path"] for f in st["files"]}
        sel = [p for p in (paths or list(known))]
        bad = [p for p in sel if p not in known]
        if bad:
            raise bad_request(f"not a changed file: {bad[0]}", "INVALID_PATH")
        if not sel:
            raise conflict("NOTHING_TO_COMMIT", "there are no changes to commit")
        rc, _, err = await t.git(row.worktree_path, ["add", "-A", "--", *sel])
        if rc != 0:
            raise ApiError(400, "GIT_ERROR", f"git add failed: {err.strip()[:300]}")
        rc, out, err = await t.git(row.worktree_path, ["commit", "-q", "-m", msg, "--", *sel])
        if rc != 0:
            raise ApiError(400, "GIT_COMMIT_FAILED", f"git commit failed: {(err or out).strip()[:500]}")
        _, head, _ = await t.git(row.worktree_path, ["rev-parse", "--short", "HEAD"])
        self._update(dev_id)
        return {"commit": head.strip(), "files": len(sel)}

    async def push(self, dev_id: str) -> dict[str, Any]:
        row, t = self._ready(dev_id)
        if not row.push_remote or not row.push_branch:
            raise conflict("NO_PUSH_TARGET", "this workspace has no remote to push to (add an `origin` remote to the checkout)")
        rc, out, err = await t.git(row.worktree_path, ["push", "--porcelain", row.push_remote, f"HEAD:refs/heads/{row.push_branch}"], timeout=600)
        if rc != 0:
            text = (err or out).strip()
            code = "PUSH_REJECTED" if "rejected" in text or "non-fast-forward" in text else (
                "PUSH_AUTH" if any(k in text.lower() for k in ("permission", "denied", "authentication", "403")) else "PUSH_FAILED")
            raise ApiError(409 if code == "PUSH_REJECTED" else 400, code, f"git push failed: {text[-500:]}")
        await t.git(row.worktree_path, ["update-ref", f"refs/waw/pushed/{row.branch}", "HEAD"])
        self._update(dev_id)
        return {"pushed": True, "remote": row.push_remote, "branch": row.push_branch}

    async def create_pr(self, dev_id: str, title: str, body: str, draft: bool) -> dict[str, Any]:
        row, t = self._ready(dev_id)
        if row.pr_number:
            raise conflict("PR_EXISTS", f"this workspace already belongs to pull request #{row.pr_number}")
        if not row.github_repo:
            raise conflict("NO_GITHUB_REPO", "the repository is not on GitHub (no github.com remote); push and open the PR there")
        if not title.strip():
            raise bad_request("a title is required", "INVALID_TITLE")
        st = await self.status(dev_id)
        if st.get("unpushed"):
            await self.push(dev_id)
        base = (row.base_ref or "main").removeprefix("refs/heads/").removeprefix("origin/")
        args = ["gh", "pr", "create", "--repo", row.github_repo, "--base", base, "--head", row.push_branch or row.branch,
                "--title", title.strip(), "--body", body or ""]
        if draft:
            args.append("--draft")
        rc, out, err = await t.run(args, cwd=row.worktree_path, timeout=120)
        if rc != 0:
            raise ApiError(400, "PR_CREATE_FAILED", f"gh pr create failed: {(err or out).strip()[:500]}")
        url = next((ln.strip() for ln in out.splitlines() if "/pull/" in ln), out.strip())
        m = re.search(r"/pull/(\d+)", url)
        if m:
            self._update(dev_id, pr_number=int(m.group(1)))
        return {"url": url, "number": int(m.group(1)) if m else None}

    async def checks(self, dev_id: str) -> dict[str, Any]:
        row, t = self._ready(dev_id)
        if not row.pr_number or not row.github_repo:
            return {"checks": [], "note": "no pull request yet"}
        rc, out, err = await t.run(["gh", "pr", "checks", str(row.pr_number), "--repo", row.github_repo, "--json", "name,state,link,workflow"],
                                   timeout=60)
        if rc not in (0, 8) or not out.strip().startswith("["):  # 8 = some checks pending
            return {"checks": [], "note": (err or out).strip()[:300] or "gh pr checks failed"}
        return {"checks": json.loads(out), "note": None}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def shell_join(cmd: list[str]) -> str:
    return shlex.join(cmd)
