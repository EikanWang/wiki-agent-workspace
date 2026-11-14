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

def in_scope(rel: str, scope: str | None) -> bool:
    """`rel` is the file `scope` names or inside the folder it names (no scope: everything)."""
    if not scope:
        return True
    scope = scope.strip("/")
    return rel == scope or rel.startswith(scope + "/")


def valid_branch(name: str) -> bool:
    return bool(name) and not name.startswith("-") and bool(re.fullmatch(r"[\w./-]+", name)) and ".." not in name \
        and not name.endswith((".lock", "/", ".")) and "//" not in name


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's empty tree: the "parent" of a root commit


def _unquote(p: str) -> str:
    """A path as git prints it in diff headers (C-quoted when it has special characters)."""
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        try:
            return p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return p[1:-1]
    return p


def split_diff(text: str) -> list[dict[str, Any]]:
    """`git diff` output → one entry per file: path, previous path, status, +/- counts, its unified diff."""
    files: list[dict[str, Any]] = []
    chunks = re.split(r"(?m)^(?=diff --git )", text)
    for ch in chunks:
        if not ch.startswith("diff --git "):
            continue
        lines = ch.split("\n")
        old = new = None
        status = "modified"
        binary = False
        body_at = len(lines)
        for i, ln in enumerate(lines[1:], 1):
            if ln.startswith("@@"):
                body_at = i
                break
            if ln.startswith(("--- ", "+++ ")):
                v = ln[4:].rstrip("\t")  # git ends a path with spaces with a tab
                v = None if v == "/dev/null" else _unquote(v)[2:]  # "a/…" / "b/…"
                if ln[0] == "-":
                    old = v
                else:
                    new = v
            elif ln.startswith("new file mode"):
                status = "added"
            elif ln.startswith("deleted file mode"):
                status = "deleted"
            elif ln.startswith("rename from "):
                old, status = _unquote(ln[12:]), "renamed"
            elif ln.startswith("rename to "):
                new = _unquote(ln[10:])
            elif ln.startswith("copy to "):
                new, status = _unquote(ln[8:]), "added"
            elif ln.startswith("Binary files ") or ln.startswith("GIT binary patch"):
                binary = True
        if old is None and new is None:  # no ---/+++ (binary, mode change, pure rename): take them from the first line
            m = re.match(r'diff --git (?:"?a/)(.+?)"? (?:"?b/)(.+?)"?$', lines[0])
            if m:
                old, new = m.group(1), m.group(2)
        path = new or old or ""
        body = lines[body_at:]
        if body and body[-1] == "":
            body = body[:-1]
        adds = sum(1 for ln in body if ln.startswith("+"))
        dels = sum(1 for ln in body if ln.startswith("-"))
        files.append({"path": path, "previous_path": old if status == "renamed" else None, "status": status,
                      "additions": adds, "deletions": dels, "binary": binary,
                      "unified": "\n".join(body) + ("\n" if body else "")})
    return files

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
    from .. import gh_limits

    res = await gh_limits.run_gh_async(["pr", "view", str(number), "--repo", repo, "--json",
                                        "headRefName,headRepository,headRepositoryOwner,baseRefName,isCrossRepository,maintainerCanModify,title,url,state"],
                                       timeout=60, dedupe=True)
    if res.rc != 0:
        if gh_limits.classify(None, res.err, {}) in ("primary", "secondary"):
            gh_limits.raise_for(res)
        raise ApiError(502, "GH_FAILED", f"gh pr view failed: {res.err.strip()[:300]}")
    out = res.out
    d = json.loads(out)
    owner = (d.get("headRepositoryOwner") or {}).get("login")
    name = (d.get("headRepository") or {}).get("name")
    d["head_repo"] = f"{owner}/{name}" if owner and name else repo
    return d


async def github_remotes(t: Target, path: str) -> dict[str, str]:
    """owner/name → remote name, for the checkout's GitHub remotes, from the configured URLs (not rewritten by
    url.<base>.insteadOf, which `git remote -v` / `get-url` apply)."""
    _, out, _ = await t.git(path, ["config", "--get-regexp", r"^remote\..*\.url$"])
    found: dict[str, str] = {}
    for ln in out.splitlines():
        key, _, url = ln.partition(" ")
        name = key.removeprefix("remote.").removesuffix(".url")
        repo = parse_github_repo(url)
        if repo and (repo not in found or name == "origin"):
            found[repo] = name
    return found


async def set_pr_state(repo: str, number: int, action: str, comment: str | None) -> None:
    """`gh pr close|reopen` (the server's gh login). Module-level so tests can replace it."""
    import shutil

    gh = shutil.which("gh")
    if not gh:
        raise ApiError(409, "GH_UNAVAILABLE", "the GitHub CLI (gh) is not installed on the server")
    from .. import gh_limits

    def reconcile() -> str | None:  # unclear outcome: does GitHub show the PR in the wanted state now?
        r = gh_limits.run_gh(["pr", "view", str(number), "--repo", repo, "--json", "state"], timeout=60)
        try:
            state = json.loads(r.out).get("state") if r.rc == 0 else None
        except ValueError:
            state = None
        return "ok" if state == ("CLOSED" if action == "close" else "OPEN") else None
    res = await gh_limits.run_gh_async(["pr", action, str(number), "--repo", repo, *(["--comment", comment] if comment else [])],
                                       timeout=90, mutation=True, reconcile=reconcile)
    if res.rc != 0:
        if gh_limits.classify(None, res.err, {}) in ("primary", "secondary"):
            gh_limits.raise_for(res)
        raise ApiError(502, "GH_FAILED", f"gh pr {action} failed: {(res.err or res.out).strip()[:300]}")


async def list_my_prs(repo: str, state: str = "open") -> list[dict[str, Any]]:
    """Your pull requests in `repo` (author @me, the server's gh login), newest first. Module-level so tests can
    replace it."""
    import shutil

    gh = shutil.which("gh")
    if not gh:
        raise ApiError(409, "GH_UNAVAILABLE", "the GitHub CLI (gh) is not installed on the server (run gh auth login after installing it)")
    from .. import gh_limits

    res = await gh_limits.run_gh_async(["pr", "list", "--repo", repo, "--author", "@me", "--state", state, "--limit", "100", "--json",
                                        "number,title,url,isDraft,state,headRefName,baseRefName,updatedAt,reviewDecision,statusCheckRollup"],
                                       timeout=90, dedupe=True)
    if res.rc != 0:
        if gh_limits.classify(None, res.err, {}) in ("primary", "secondary"):
            gh_limits.raise_for(res)
        raise ApiError(502, "GH_FAILED", f"gh pr list failed: {res.err.strip()[:300]}")
    out = res.out
    items = json.loads(out or "[]")
    for p in items:
        checks = p.pop("statusCheckRollup", None) or []
        states = {(c.get("conclusion") or c.get("state") or c.get("status") or "").upper() for c in checks}
        p["ci"] = (None if not checks else "FAILURE" if states & {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}
                   else "PENDING" if states & {"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", ""} else "SUCCESS")
    return sorted(items, key=lambda p: p.get("updatedAt") or "", reverse=True)


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
        if body.mode not in ("branch", "pr", "folder"):
            raise bad_request("mode must be branch, pr or folder", "INVALID_MODE")
        source = (body.source_repo or "").strip()
        if not source or source.startswith("-"):
            raise bad_request("give the path of a local Git checkout of the repository (on the chosen host)", "INVALID_PATH")
        if body.mode == "folder":
            br = (body.branch or "").strip()
            if br and not valid_branch(br):
                raise bad_request("give a valid branch name (letters, digits, - _ . /)", "INVALID_BRANCH")
            if body.github_repo and not _REPO.match(body.github_repo):
                raise bad_request("repository must be owner/name", "INVALID_PR")
        elif body.mode == "pr":
            if not body.github_repo or not _REPO.match(body.github_repo) or not body.pr_number or body.pr_number < 1:
                raise bad_request("an existing pull request needs its repository (owner/name) and number", "INVALID_PR")
        else:
            br = (body.branch or "").strip()
            if not valid_branch(br):
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
        name = (body.name or "").strip() or (f"{body.github_repo}#{body.pr_number}" if body.mode == "pr"
                                              else source_abs.rstrip("/").rsplit("/", 1)[-1] or source_abs if body.mode == "folder"
                                              else body.branch.strip())
        # the agent conversation: a hidden document of the reference wiki folder (or the primary folder)
        rt = self.ctx.workspaces.get(conv_ws) if self.ctx.workspaces else None
        docs = rt.docs if rt else self.ctx.documents
        assert docs is not None
        norm = dev_doc_path(dev_id)
        stub = dev_snapshot_path(docs.snapshots_dir, norm)
        assert stub is not None
        stub.parent.mkdir(parents=True, exist_ok=True)
        what = {"pr": "its Git worktree (existing pull request)", "branch": "its Git worktree (new branch)",
                "folder": f"the folder {source_abs}"}[body.mode]
        stub.write_text(f"# {name}\n\nPR Development workspace — the agent works in {what} with the wiki folder as reference.\n",
                        encoding="utf-8")
        doc = docs.get_or_create_document(norm)
        with self.ctx.db.write() as s:
            d = s.get(Document, doc.id)
            if d is not None:
                d.hidden = True  # not in Recent / chats lists; it is shown inside PR Development
            s.add(DevWorkspace(
                id=dev_id, name=name[:200], host=host, source_repo=source_abs, worktree_path="", mode=body.mode,
                branch=(body.branch or "").strip() if body.mode != "pr" else f"waw/pr-{body.pr_number}",
                base_ref=(body.base or "").strip() or None, github_repo=body.github_repo or None, pr_number=body.pr_number if body.mode != "branch" else None,
                wiki_workspace_id=body.wiki_workspace_id or None, document_id=doc.id,
                settings_json=json.dumps(clean_settings(body.settings or {})), status="creating"))
        self._start(dev_id, self._provision_folder(dev_id, body.init_git) if body.mode == "folder" else self._provision(dev_id))
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
        await self._finish(dev_id, t, wt, fields)

    async def _provision_folder(self, dev_id: str, init_git: bool) -> None:
        """Work in the given folder itself: a Git checkout (its top folder), or a folder made a repository (git init).
        A requested branch is created there (or switched to) from the current one, which becomes its base."""
        row = self.get(dev_id)
        t = self.target(row)
        path = row.source_repo
        if not await t.is_dir(path):
            raise ApiError(400, "FOLDER_NOT_FOUND", f"{path} is not a folder on {t.label()}")
        rc, top, err = await t.git(path, ["rev-parse", "--show-toplevel"], timeout=60)
        if rc != 0:
            if not init_git:
                raise ApiError(400, "NOT_A_GIT_REPO", f"{path} is not in a Git repository on {t.label()}")
            rc, _, err = await t.git(path, ["init", "-q"], timeout=60)
            if rc != 0:
                raise ApiError(400, "GIT_INIT_FAILED", f"git init failed: {err.strip()[:300]}")
            top = path
        wt = top.strip()
        _, cur, _ = await t.git(wt, ["symbolic-ref", "--quiet", "--short", "HEAD"])
        current = cur.strip()
        fields: dict[str, Any] = {"source_repo": wt, "worktree_path": wt, "branch": current or "HEAD"}
        if row.branch and row.branch != current:
            exists = (await t.git(wt, ["rev-parse", "--verify", "--quiet", f"refs/heads/{row.branch}"]))[0] == 0
            rc, _, err = await t.git(wt, ["switch", *([] if exists else ["-c"]), row.branch], timeout=120)
            if rc != 0:
                raise ApiError(400, "GIT_SWITCH_FAILED", f"could not {'switch to' if exists else 'create'} branch {row.branch}: {err.strip()[-400:]}")
            fields["branch"] = row.branch
            if not exists and current:
                fields["base_ref"] = current
        github_repo = row.github_repo or await self.repo_of_folder(wt, t)
        _, remotes, _ = await t.git(wt, ["remote"])
        fields.update(github_repo=github_repo, push_remote="origin" if "origin" in remotes.split() else None,
                      push_branch=fields["branch"] if fields["branch"] != "HEAD" else None)
        pr = row.pr_number
        if pr:  # opened for one of the repository's pull requests: switch the folder to it before it is ready
            self._update(dev_id, **{k: v for k, v in fields.items()}, pr_number=None)
            try:
                done = await self.checkout_pr(dev_id, pr, provisioning=True)
                fields = {"error": done["warning"]} if done.get("warning") else {}
            except ApiError as exc:
                fields["error"] = f"Could not check out pull request #{pr}: {exc.message}"
        await self._finish(dev_id, t, wt, fields)

    async def _finish(self, dev_id: str, t: Target, wt: str, fields: dict[str, Any]) -> None:
        row = self.get(dev_id)
        _, ls, _ = await t.git(wt, ["ls-files", "--cached", "--others", "--exclude-standard", "--", "CMakeLists.txt", "pyproject.toml", "setup.py", "setup.cfg", "pytest.ini",
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
        if remove_worktree and row.worktree_path and row.mode != "folder":  # a folder opened directly is yours: never removed
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
        row = await self._follow_branch(row, t)
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
        else:
            c, n, _ = await t.git(wt, ["rev-list", "--count", "@{upstream}..HEAD"])  # a checkout's own upstream
            if c == 0 and n.strip().isdigit():
                unpushed = int(n.strip())
            elif ahead is not None:
                unpushed = ahead  # never pushed: every commit of the branch
        _, name, _ = await t.git(wt, ["config", "user.name"])
        _, email, _ = await t.git(wt, ["config", "user.email"])
        return {"branch": row.branch, "head": head.strip(), "subject": subject.strip(), "files": sorted(files, key=lambda f: f["path"]),
                "mode": row.mode, "base_ref": row.base_ref,
                "commits_on_branch": ahead, "unpushed": unpushed, "push_remote": row.push_remote, "push_branch": row.push_branch,
                "identity": {"name": name.strip(), "email": email.strip(), "configured": bool(name.strip() and email.strip())}}

    async def diff(self, dev_id: str, path: str | None = None, max_bytes: int = 400_000) -> dict[str, Any]:
        row, t = self._ready(dev_id)
        has_head = (await t.git(row.worktree_path, ["rev-parse", "--verify", "--quiet", "HEAD"]))[0] == 0
        args = ["diff", "--no-color", "--no-ext-diff", "HEAD" if has_head else EMPTY_TREE]
        if path:
            args += ["--", path]
        rc, out, err = await t.git(row.worktree_path, args)
        if rc != 0:
            raise ApiError(400, "GIT_ERROR", f"git diff failed: {err.strip()[:300]}")
        st = await self.status(dev_id)
        untracked = [f["path"] for f in st["files"] if f["status"] == "untracked" and in_scope(f["path"], path)]
        for rel in untracked[:50]:  # show new files like `git diff` would after `git add -N`
            c, text, _ = await t.run(["git", "-C", row.worktree_path, "diff", "--no-color", "--no-index", "--", "/dev/null", rel], timeout=60)
            if c in (0, 1):
                out += text
        return {"diff": out[:max_bytes], "truncated": len(out) > max_bytes, "files": st["files"]}

    # ------------------------------------------------------------------ compare (the Changes view)
    async def _commit_of(self, t: Target, wt: str, ref: str | None) -> str | None:
        """The full sha `ref` names (a commit), or None. Refuses option-like or odd refs (they reach git's argv)."""
        if not ref or ref.startswith("-") or len(ref) > 250 or any(c in ref for c in " \t\r\n\0"):
            return None
        rc, out, _ = await t.git(wt, ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}"])
        return out.strip() if rc == 0 and out.strip() else None

    async def _merge_base(self, row: DevWorkspace, t: Target) -> str:
        if not row.base_ref:
            raise conflict("NO_BASE", "this workspace has no base branch to compare with")
        rc, out, err = await t.git(row.worktree_path, ["merge-base", "--end-of-options", row.base_ref, "HEAD"])
        if rc != 0 or not out.strip():
            raise ApiError(400, "GIT_ERROR", f"cannot find where the branch started from {row.base_ref}: {err.strip()[:200]}")
        return out.strip()

    async def compare(self, dev_id: str, *, base: str = "HEAD", target: str = "worktree", commit: str | None = None,
                      path: str | None = None, max_bytes: int = 3_000_000) -> dict[str, Any]:
        """Changed files between two states, each with its own unified diff. `base`: HEAD, `branch` (where the branch
        started from its base), a commit or a branch name; `target`: `worktree` (uncommitted files included), HEAD or a
        commit; `commit`: just that commit (its parent → it)."""
        row, t = self._ready(dev_id)
        wt = row.worktree_path
        if commit:
            sha = await self._commit_of(t, wt, commit)
            if not sha:
                raise bad_request(f"unknown commit {commit[:60]}", "INVALID_REF")
            base_sha = await self._commit_of(t, wt, f"{sha}^") or EMPTY_TREE
            target_sha: str | None = sha
        else:
            base_sha = await self._merge_base(row, t) if base == "branch" else await self._commit_of(t, wt, base)
            if not base_sha and base == "HEAD":
                base_sha = EMPTY_TREE  # a repository without commits yet: everything is new
            if not base_sha:
                raise bad_request(f"unknown base {base[:60]}", "INVALID_REF")
            target_sha = None if target == "worktree" else await self._commit_of(t, wt, target)
            if target != "worktree" and not target_sha:
                raise bad_request(f"unknown target {target[:60]}", "INVALID_REF")
        if path and (path.startswith(("-", "/")) or ".." in path.split("/")):
            raise bad_request("invalid path", "INVALID_PATH")
        path = path.strip("/") if path else None
        args = ["diff", "--no-color", "--no-ext-diff", "-M", base_sha, *([target_sha] if target_sha else [])]
        if path:
            args += ["--", path]
        rc, out, err = await t.git(wt, args, timeout=180)
        if rc != 0:
            raise ApiError(400, "GIT_ERROR", f"git diff failed: {err.strip()[:300]}")
        files = split_diff(out)
        st = await self.status(dev_id) if target_sha is None else None
        if st is not None:
            for rel in [f["path"] for f in st["files"] if f["status"] == "untracked" and in_scope(f["path"], path)][:500]:
                c, text, _ = await t.git(wt, ["diff", "--no-color", "--no-index", "--", "/dev/null", rel], timeout=60)
                if c in (0, 1):
                    for f in split_diff(text):
                        f["status"] = "untracked"
                        files.append(f)
        size = 0
        for f in files:
            size += len(f["unified"])
            if size > max_bytes or len(f["unified"]) > 600_000:
                f["unified"], f["too_large"] = "", True
        files.sort(key=lambda f: f["path"])
        return {"path": path, "base": base if not commit else f"{commit}^", "base_sha": base_sha, "target": commit or target,
                "target_sha": target_sha, "worktree": target_sha is None, "files": files,
                "summary": {"files": len(files), "additions": sum(f["additions"] for f in files),
                            "deletions": sum(f["deletions"] for f in files)}}

    async def refs(self, dev_id: str) -> dict[str, Any]:
        """What the Changes view can compare: the branch's own commits, recent history, and branches."""
        row, t = self._ready(dev_id)
        wt = row.worktree_path
        fmt = "--format=%H%x00%h%x00%s%x00%an%x00%cI"

        def parse(text: str) -> list[dict[str, str]]:
            out = []
            for ln in text.splitlines():
                p = ln.split("\0")
                if len(p) == 5:
                    out.append({"sha": p[0], "short": p[1], "subject": p[2], "author": p[3], "date": p[4]})
            return out

        mine: list[dict[str, str]] = []
        if row.base_ref:
            c, text, _ = await t.git(wt, ["log", "--max-count=100", fmt, f"{row.base_ref}..HEAD"])
            mine = parse(text) if c == 0 else []
        _, text, _ = await t.git(wt, ["log", "--max-count=50", fmt, "HEAD"])
        _, br, _ = await t.git(wt, ["for-each-ref", "--count=300", "--sort=-committerdate", "--format=%(refname:short)", "refs/heads", "refs/remotes"])
        base = (row.base_ref or "").removeprefix("refs/waw/base/")
        return {"branch_commits": mine, "commits": parse(text), "branches": [b for b in br.split() if not b.endswith("/HEAD")],
                "base_ref": row.base_ref, "base_label": base or None}

    async def file_at(self, dev_id: str, path: str, ref: str = "worktree", max_bytes: int = 4_000_000) -> dict[str, Any]:
        """The lines of a file in the worktree or at a commit (to show more context around a change)."""
        row, t = self._ready(dev_id)
        wt = row.worktree_path
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise bad_request("invalid path", "INVALID_PATH")
        if ref == "worktree":
            rc, out, err = await t.run(["cat", "--", path], cwd=wt, timeout=60)
        else:
            sha = await self._commit_of(t, wt, ref)
            if not sha:
                raise bad_request(f"unknown commit {ref[:60]}", "INVALID_REF")
            rc, out, err = await t.git(wt, ["show", f"{sha}:{path}"], timeout=60)
        if rc != 0:
            raise not_found(f"{path} not found: {err.strip()[:200]}", "FILE_NOT_FOUND")
        if len(out) > max_bytes:
            raise ApiError(413, "FILE_TOO_LARGE", "the file is too large to show")
        return {"lines": out.split("\n")[:-1] if out.endswith("\n") else out.split("\n")}

    # ------------------------------------------------------------------ your pull requests
    def known_repos(self) -> list[dict[str, Any]]:
        """GitHub repositories you work on here, each with a local checkout to develop in (on this server) if known:
        from the development workspaces and the checkouts connected in PR Review."""
        from ..kv import kv_get
        from ..models import KvSetting

        out: dict[str, dict[str, Any]] = {}
        for r in self.list():
            if not r.get("github_repo"):
                continue
            k = r["github_repo"].lower()
            cur = out.setdefault(k, {"repo": r["github_repo"], "source": None, "workspaces": 0})
            cur["workspaces"] += 1
            if r.get("host") == LOCAL and not cur["source"] and r.get("source_repo"):
                cur["source"] = r["source_repo"]
        with self.ctx.db.read() as s:
            keys = [k.key for k in s.query(KvSetting.key).filter(KvSetting.key.like("local_repo:%"))]
        for key in keys:
            repo = key.removeprefix("local_repo:")
            link = kv_get(self.ctx, key, None) or {}
            cur = out.setdefault(repo.lower(), {"repo": repo, "source": None, "workspaces": 0})
            if not cur["source"] and link.get("path"):
                cur["source"] = link["path"]
        return sorted(out.values(), key=lambda x: (-x["workspaces"], x["repo"].lower()))

    async def repo_of_folder(self, path: str, t: Target | None = None) -> str | None:
        """The GitHub repository of a checkout (its origin, else any GitHub remote)."""
        remotes = await github_remotes(t or self.target(LOCAL), path)
        return next((r for r, name in remotes.items() if name == "origin"), None) or next(iter(remotes), None)

    async def my_prs(self, repo: str | None, source: str | None, state: str) -> dict[str, Any]:
        if not repo and source:
            source = await self.target(LOCAL).expand(source)
            repo = await self.repo_of_folder(source)
            if not repo:
                raise bad_request(f"{source} has no GitHub remote", "NO_GITHUB_REPO")
        if not repo or not _REPO.match(repo):
            raise bad_request("give the repository as owner/name (or a local checkout of it)", "INVALID_REPO")
        prs = await list_my_prs(repo, state)
        rows = [r for r in self.list() if (r.get("github_repo") or "").lower() == repo.lower() and r.get("pr_number")]
        for p in prs:
            p["workspaces"] = [{"id": r["id"], "name": r["name"], "mode": r["mode"]} for r in rows if r["pr_number"] == p["number"]]
        known = next((k for k in self.known_repos() if k["repo"].lower() == repo.lower()), None)
        return {"repo": repo, "state": state, "source": source or (known or {}).get("source"), "prs": prs}

    async def checkout_pr(self, dev_id: str, number: int, provisioning: bool = False) -> dict[str, Any]:
        """Put one of the repository's pull requests in this workspace's own checkout (a folder opened directly, or a
        new-branch worktree): its head on a local branch, compared with its base, pushes going back to the PR's
        branch. Your uncommitted changes go along (git refuses when they would be overwritten)."""
        row, t = (self.get(dev_id), self.target(self.get(dev_id))) if provisioning else self._ready(dev_id)
        if row.mode == "pr":
            raise conflict("PR_WORKSPACE", "this workspace belongs to a pull request: open the other one in its own workspace")
        if not row.github_repo:
            raise conflict("NO_GITHUB_REPO", "this checkout has no GitHub remote")
        wt = row.worktree_path
        meta = await fetch_pr_meta(row.github_repo, number)
        origin_repo = {k.lower(): v for k, v in (await github_remotes(t, wt)).items()}
        repo = row.github_repo
        fetch_src = origin_repo.get(repo.lower()) or f"https://github.com/{repo}.git"
        base = meta.get("baseRefName") or "main"
        rc, _, err = await t.git(wt, ["fetch", "--no-tags", "--quiet", fetch_src, f"+refs/pull/{number}/head:refs/waw/pr/{number}",
                                      f"+refs/heads/{base}:refs/waw/base/{base}"], timeout=900)
        if rc != 0:
            raise ApiError(502, "GIT_FETCH_FAILED", f"could not fetch the pull request: {err.strip()[-400:]}")
        head = meta.get("headRefName") or f"pr-{number}"
        local = head if valid_branch(head) else f"pr-{number}"
        exists = (await t.git(wt, ["rev-parse", "--verify", "--quiet", f"refs/heads/{local}"]))[0] == 0
        if exists:  # a local branch of that name: use it if it is the PR's (or behind it), else a branch of its own
            rc, _, _ = await t.git(wt, ["merge-base", "--is-ancestor", local, f"refs/waw/pr/{number}"])
            rc2, _, _ = await t.git(wt, ["merge-base", "--is-ancestor", f"refs/waw/pr/{number}", local])
            if rc != 0 and rc2 != 0:
                local, exists = f"pr-{number}", (await t.git(wt, ["rev-parse", "--verify", "--quiet", f"refs/heads/pr-{number}"]))[0] == 0
        if exists:
            rc, out, err = await t.git(wt, ["switch", local], timeout=180)
            if rc == 0:  # bring it up to the PR's head when it is behind
                await t.git(wt, ["merge", "--ff-only", "--quiet", f"refs/waw/pr/{number}"], timeout=180)
        else:
            rc, out, err = await t.git(wt, ["switch", "-c", local, f"refs/waw/pr/{number}"], timeout=180)
        if rc != 0:
            raise ApiError(409, "GIT_SWITCH_FAILED", f"git switch failed: {(err or out).strip()[-500:]}")
        head_repo = meta.get("head_repo") or repo
        push_remote = origin_repo.get(head_repo.lower()) or f"https://github.com/{head_repo}.git"
        await t.git(wt, ["update-ref", f"refs/waw/pushed/{local}", f"refs/waw/pr/{number}"])
        warn = None
        if meta.get("isCrossRepository") and not meta.get("maintainerCanModify"):
            warn = "This PR comes from a fork that does not allow edits by maintainers: pushing to its branch will be refused."
        self._update(dev_id, branch=local, pr_number=number, base_ref=f"refs/waw/base/{base}", push_remote=push_remote,
                     push_branch=head, error=warn)
        return {"branch": local, "pr_number": number, "title": meta.get("title"), "push_branch": head, "warning": warn}

    # ------------------------------------------------------------------ branches
    async def _follow_branch(self, row: DevWorkspace, t: Target) -> DevWorkspace:
        """A folder opened directly (or a worktree) may switch branches outside the app: follow it."""
        if row.mode == "pr":
            return row
        _, cur, _ = await t.git(row.worktree_path, ["symbolic-ref", "--quiet", "--short", "HEAD"])
        cur = cur.strip() or "HEAD"
        if cur != row.branch:
            self._update(row.id, branch=cur, push_branch=cur if cur != "HEAD" else None,
                         **({"pr_number": None, "push_remote": self._default_remote(row)} if row.pr_number else {}))
            row = self.get(row.id)
        return row

    def _default_remote(self, row: DevWorkspace) -> str | None:
        return "origin" if row.push_remote else row.push_remote  # a PR's remote may be a fork's URL: back to origin

    async def branches(self, dev_id: str) -> dict[str, Any]:
        row, t = self._ready(dev_id)
        row = await self._follow_branch(row, t)
        _, out, _ = await t.git(row.worktree_path, ["for-each-ref", "--sort=-committerdate", "--format=%(refname:short)", "refs/heads"])
        return {"current": row.branch, "branches": [b for b in out.split() if b], "base_ref": row.base_ref, "mode": row.mode,
                "can_switch": row.mode != "pr"}

    async def branch(self, dev_id: str, name: str, create: bool, start: str | None) -> dict[str, Any]:
        """Create a branch (from the current one, or `start`) and switch to it, or switch to an existing one. The
        uncommitted changes go along (git refuses when they would be overwritten)."""
        row, t = self._ready(dev_id)
        if row.mode == "pr":
            raise conflict("PR_WORKSPACE", "this workspace belongs to a pull request: its branch cannot be changed")
        name = name.strip()
        if not valid_branch(name):
            raise bad_request("give a valid branch name (letters, digits, - _ . /)", "INVALID_BRANCH")
        wt = row.worktree_path
        row = await self._follow_branch(row, t)
        exists = (await t.git(wt, ["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"]))[0] == 0
        if create and exists:
            raise conflict("BRANCH_EXISTS", f"a branch {name} already exists: switch to it instead")
        if not create and not exists:
            raise not_found(f"no branch {name}", "BRANCH_NOT_FOUND")
        start_sha = None
        if create and start:
            start_sha = await self._commit_of(t, wt, start)
            if not start_sha:
                raise bad_request(f"unknown start {start[:60]}", "INVALID_REF")
        prev = row.branch
        args = ["switch", "-c", name, *([start_sha] if start_sha else [])] if create else ["switch", name]
        rc, out, err = await t.git(wt, args, timeout=180)
        if rc != 0:
            raise ApiError(409, "GIT_SWITCH_FAILED", f"git switch failed: {(err or out).strip()[-500:]}")
        fields: dict[str, Any] = {"branch": name, "push_branch": name}
        if row.pr_number:  # off the pull request's branch
            fields.update(pr_number=None, push_remote=self._default_remote(row), error=None)
        if create and row.mode == "folder":
            fields["base_ref"] = start or (prev if prev != "HEAD" else None)  # compared against where it started
        self._update(dev_id, **fields)
        return {"branch": name, "created": create, "previous": prev}

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
        rc, out, err = await t.git(row.worktree_path, ["commit", "-q", "-F", "-", "--", *sel], stdin=msg.encode("utf-8"))
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
        from .. import gh_limits

        head = row.push_branch or row.branch

        async def existing() -> tuple[int, str, str] | None:  # a PR for this branch on GitHub already (made by an unclear attempt)
            rc2, out2, _ = await t.run(["gh", "pr", "list", "--repo", row.github_repo, "--head", head, "--state", "open", "--json", "url",
                                        "--jq", ".[0].url // empty"], cwd=row.worktree_path, timeout=60)
            return (0, out2.strip(), "") if rc2 == 0 and "/pull/" in out2 else None
        rc, out, err = await gh_limits.run_with(lambda: t.run(args, cwd=row.worktree_path, timeout=120), args[1:], mutation=True, reconcile=existing)
        if rc != 0:
            m0 = re.search(r"https://github\.com/\S+/pull/\d+", err or out)
            if "already exists" in (err or out) and m0:  # never a second PR for the branch: take the one GitHub has
                out, rc = m0.group(0), 0
            else:
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
        from .. import gh_limits

        cargs = ["gh", "pr", "checks", str(row.pr_number), "--repo", row.github_repo, "--json", "name,state,link,workflow"]
        try:
            rc, out, err = await gh_limits.run_with(lambda: t.run(cargs, timeout=60), cargs[1:])
        except gh_limits.RateLimited as exc:
            return {"checks": [], "note": exc.message}
        if rc not in (0, 8) or not out.strip().startswith("["):  # 8 = some checks pending
            return {"checks": [], "note": (err or out).strip()[:300] or "gh pr checks failed"}
        return {"checks": json.loads(out), "note": None}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def shell_join(cmd: list[str]) -> str:
    return shlex.join(cmd)
