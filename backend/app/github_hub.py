"""GitHub review hub: the pull requests waiting for the server owner's review, next to the wikis.

Data comes from GitHub through the server user's own `gh` CLI login (GraphQL), so no extra credentials are needed.
Only the *owner* may see it: a browser signed in with "Continue with GitHub" as the account `gh` is logged in as.
Everyone else (token logins, other GitHub accounts) gets 403 — the queue can include private repositories.

The queue is fetched with a single paginated GraphQL search (with reviews, checks and size inline) and cached for
CACHE_SECONDS, because GitHub's search API allows only 30 requests per minute.
"""

from __future__ import annotations

import asyncio
import json
import re
import logging
import shutil
import subprocess
import time
from typing import Any, Callable

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError

log = logging.getLogger("waw.github")

CACHE_SECONDS = 120
PAGE_SIZE = 40  # GitHub answers 502 (timeout) for 100 PRs with reviews/checks/labels inline
MAX_PRS = 500
OWNER_CACHE_SECONDS = 600

router = APIRouter()

REVIEW_QUERY = """
query($q: String!, $after: String, $n: Int!) {
  rateLimit { remaining resetAt }
  search(query: $q, type: ISSUE, first: $n, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url isDraft createdAt updatedAt additions deletions changedFiles reviewDecision
        author { login }
        repository { nameWithOwner isPrivate }
        labels(first: 6) { nodes { name color } }
        reviewRequests(first: 20) { nodes { requestedReviewer { __typename ... on User { login } ... on Team { slug name } } } }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
  }
}
"""


# ---------------------------------------------------------------------- gh access (replaceable in tests)

def _run_gh(args: list[str], timeout: float = 60, *, partial_json: bool = False) -> str:
    """Run gh. partial_json: GraphQL answers with data *and* errors make gh exit 1; return that JSON anyway."""
    gh = shutil.which("gh")
    if not gh:
        raise ApiError(503, "GH_UNAVAILABLE", "the GitHub CLI (gh) is not installed on the server")
    try:
        p = subprocess.run([gh, *args], capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise ApiError(504, "GH_TIMEOUT", "GitHub did not answer in time")
    if p.returncode != 0 and partial_json and p.stdout.strip().startswith("{") and '"data"' in p.stdout:
        return p.stdout
    if p.returncode != 0:
        err = (p.stderr or p.stdout).strip()
        low = err.lower()
        if "http 502" in low or "http 504" in low or "invalid character '<'" in low:  # GitHub's HTML error page
            raise ApiError(502, "GH_BUSY", "GitHub took too long to answer (HTTP 502)", {"detail": err[-300:]})
        if "rate limit" in low:
            raise ApiError(429, "GH_RATE_LIMITED", "GitHub's rate limit is reached; try again in a minute", {"detail": err[-300:]})
        if "http 401" in low or "bad credentials" in low or "gh auth login" in low or "not logged" in low:
            raise ApiError(503, "GH_AUTH", "the server's gh CLI is not logged in to GitHub (run `gh auth login` in a terminal)", {"detail": err[-300:]})
        if "could not resolve host" in low or "timeout" in low or "connection" in low:
            raise ApiError(503, "GH_UNREACHABLE", "the server cannot reach GitHub (network or proxy)", {"detail": err[-300:]})
        raise ApiError(502, "GH_ERROR", f"gh failed: {err[-300:]}")
    return p.stdout


def gh_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is None:
            continue
        args += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]  # -F: typed (Int), -f: string
    data = json.loads(_run_gh(args, partial_json=True))
    errors = data.get("errors") or []
    if errors and not data.get("data"):
        raise ApiError(502, "GH_ERROR", "; ".join(e.get("message", "") for e in errors)[:300])
    out = data["data"]
    # partial answers: e.g. PRs in an organization that enforces SAML SSO the token is not authorized for come back
    # as null nodes with a FORBIDDEN error — keep the rest, and say how many are hidden and why
    out["_errors"] = [{"type": e.get("type"), "message": e.get("message", "")[:200]} for e in errors]
    return out


_owner: tuple[float, str] | None = None


def gh_owner() -> str:
    """The GitHub login the server's gh CLI uses (cached); "" if gh is missing or logged out."""
    global _owner
    if _owner and time.time() - _owner[0] < OWNER_CACHE_SECONDS:
        return _owner[1]
    try:
        login = _run_gh(["api", "user", "--jq", ".login"], timeout=20).strip()
    except ApiError:
        login = ""
    _owner = (time.time(), login)
    return login


def _hooks(ctx: AppContext) -> tuple[Callable[[str, dict], dict], Callable[[], str]]:
    return ctx.extra.get("gh_graphql", gh_graphql), ctx.extra.get("gh_owner", gh_owner)


# ---------------------------------------------------------------------- access

async def hub_status(ctx: AppContext, request: Request) -> dict[str, Any]:
    _, owner_fn = _hooks(ctx)
    owner = await asyncio.to_thread(owner_fn)
    ident = ctx.auth.identity_for_request(request) or ""
    if not owner:
        return {"available": False, "owner": None, "reason": "gh_unavailable"}
    if ident.lower() != f"github:{owner}".lower():
        return {"available": False, "owner": owner, "reason": "not_owner"}
    return {"available": True, "owner": owner, "reason": None}


async def require_owner(request: Request, ctx: AppContext = Depends(require_auth)) -> tuple[AppContext, str]:
    """(context, owner login) — or 403/503. ctx.extra is shared by all requests, so nothing is stored there."""
    st = await hub_status(ctx, request)
    if not st["available"]:
        if st["reason"] == "gh_unavailable":
            raise ApiError(503, "GH_UNAVAILABLE", "GitHub is not available on this server (gh missing or logged out)")
        raise ApiError(403, "NOT_GITHUB_OWNER", f"only {st['owner']} signed in with GitHub can see the review queue")
    return ctx, st["owner"]


# ---------------------------------------------------------------------- queue

def _ci(pr: dict[str, Any]) -> str | None:
    nodes = (pr.get("commits") or {}).get("nodes") or []
    roll = ((nodes[0] or {}).get("commit") or {}).get("statusCheckRollup") if nodes else None
    return (roll or {}).get("state")  # SUCCESS | FAILURE | ERROR | PENDING | EXPECTED | None


def normalize(pr: dict[str, Any], owner: str) -> dict[str, Any]:
    requested_user = False
    teams: list[str] = []
    for n in (pr.get("reviewRequests") or {}).get("nodes") or []:
        r = (n or {}).get("requestedReviewer") or {}
        if r.get("__typename") == "User" and (r.get("login") or "").lower() == owner.lower():
            requested_user = True
        elif r.get("__typename") == "Team":
            teams.append(r.get("slug") or r.get("name") or "")
    return {
        "repo": pr["repository"]["nameWithOwner"], "private": bool(pr["repository"].get("isPrivate")),
        "number": pr["number"], "title": pr["title"], "url": pr["url"], "draft": bool(pr.get("isDraft")),
        "author": (pr.get("author") or {}).get("login"), "created_at": pr.get("createdAt"), "updated_at": pr.get("updatedAt"),
        "additions": pr.get("additions", 0), "deletions": pr.get("deletions", 0), "changed_files": pr.get("changedFiles", 0),
        "review_decision": pr.get("reviewDecision"), "ci": _ci(pr),
        "labels": [{"name": lb["name"], "color": lb.get("color")} for lb in (pr.get("labels") or {}).get("nodes") or []],
        "requested": "you" if requested_user else ("team" if teams else "you"), "teams": teams,
    }


def fetch_queue(graphql: Callable[[str, dict], dict], owner: str) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    hidden = 0
    problems: list[dict[str, Any]] = []
    after = None
    total = 0
    rate = None
    size = PAGE_SIZE
    while len(items) < MAX_PRS:
        try:
            d = graphql(REVIEW_QUERY, {"q": "is:pr is:open archived:false review-requested:@me", "after": after, "n": size})
        except ApiError as exc:
            if exc.code != "GH_BUSY" or size <= 10:
                raise
            size //= 2  # GitHub timed out building the page: ask for less
            continue
        s = d["search"]
        total = s["issueCount"]
        rate = d.get("rateLimit")
        items += [normalize(n, owner) for n in s["nodes"] if n and n.get("number")]
        hidden += sum(1 for n in s["nodes"] if not n or not n.get("number"))
        for e in d.get("_errors") or []:
            if e not in problems:
                problems.append(e)
        if not s["pageInfo"]["hasNextPage"]:
            break
        after = s["pageInfo"]["endCursor"]
    saml = any("SAML" in (e.get("message") or "") for e in problems)
    note = None
    if hidden:
        note = (f"{hidden} PR(s) are hidden: they are in an organization that requires SAML single sign-on for this token. "
                "Authorize it once: run `gh auth refresh -h github.com` in a terminal, or on github.com → Settings → "
                "Applications → Authorized OAuth Apps → GitHub CLI → Grant for that organization."
                if saml else f"{hidden} PR(s) could not be read: {problems[0]['message'] if problems else 'unknown reason'}")
    return {"items": items, "total": total, "hidden": hidden, "note": note,
            "truncated": len(items) + hidden < total, "rate_limit": rate}


_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_tasks: dict[str, asyncio.Task] = {}
_errors: dict[str, dict[str, Any]] = {}


async def _fetch(graphql: Callable[[str, dict], dict], owner: str) -> dict[str, Any]:
    data = await asyncio.to_thread(fetch_queue, graphql, owner)
    data["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data["owner"] = owner
    _cache[owner] = (time.time(), data)
    _errors.pop(owner, None)
    log.info("review queue for %s: %d PRs (%d hidden)", owner, len(data["items"]), data.get("hidden", 0))
    return data


def _start_refresh(graphql: Callable[[str, dict], dict], owner: str) -> asyncio.Task:
    """One background fetch per owner at a time (a full queue takes ~10 s per 40 PRs)."""
    t = _tasks.get(owner)
    if t is None or t.done():
        async def run() -> None:
            try:
                await _fetch(graphql, owner)
            except ApiError as exc:
                _errors[owner] = {"code": exc.code, "message": exc.message}
                log.warning("review queue refresh failed: %s", exc.message)
            except Exception as exc:  # never leave a task with an unretrieved exception
                _errors[owner] = {"code": "GH_ERROR", "message": f"{exc.__class__.__name__}: {exc}"}
                log.exception("review queue refresh failed")
        t = _tasks[owner] = asyncio.get_running_loop().create_task(run())
    return t


@router.get("/github/hub")
async def github_hub(request: Request, ctx: AppContext = Depends(require_auth)):
    """For the UI: is the Reviews area available to this browser? For the owner it also warms the queue."""
    st = await hub_status(ctx, request)
    if st["available"] and st["owner"] not in _cache:
        _start_refresh(_hooks(ctx)[0], st["owner"])
    return st


@router.get("/github/reviews")
async def review_queue(refresh: bool = Query(False), auth: tuple[AppContext, str] = Depends(require_owner)):
    """Open PRs requesting the owner's review. Stale-while-revalidate: a cached result older than CACHE_SECONDS (or
    refresh=true) is returned at once with `refreshing: true` while a new fetch runs; poll until it is false."""
    ctx, owner = auth
    graphql, _ = _hooks(ctx)
    hit = _cache.get(owner)
    if hit is None:
        task = _start_refresh(graphql, owner)
        await asyncio.shield(task)  # first load: wait for it
        hit = _cache.get(owner)
        if hit is None:
            err = _errors.get(owner) or {"code": "GH_ERROR", "message": "the review queue could not be loaded"}
            status = {"GH_RATE_LIMITED": 429, "GH_UNAVAILABLE": 503, "GH_AUTH": 503, "GH_UNREACHABLE": 503, "GH_TIMEOUT": 504}.get(err["code"], 502)
            raise ApiError(status, err["code"], err["message"])
    age = time.time() - hit[0]
    running = owner in _tasks and not _tasks[owner].done()
    if (refresh and age >= 10) or age >= CACHE_SECONDS:  # < 10 s: rapid ↻ clicks do not burn the search quota
        _start_refresh(graphql, owner)
        running = True
    return {**hit[1], "cached": True, "age_seconds": round(age), "refreshing": running, "refresh_error": _errors.get(owner)}


def reset_cache() -> None:
    _cache.clear()
    _errors.clear()
    _tasks.clear()
    _pr_cache.clear()


# ---------------------------------------------------------------------- a pull request (tab)

PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner isPrivate
    pullRequest(number: $number) {
      number title url body state isDraft createdAt updatedAt mergeable additions deletions changedFiles
      baseRefName headRefName reviewDecision
      author { login }
      labels(first: 20) { nodes { name color } }
      latestReviews(first: 30) { nodes { author { login } state submittedAt url } }
      reviewRequests(first: 30) { nodes { requestedReviewer { __typename ... on User { login } ... on Team { slug } } } }
      commits(last: 1) { nodes { commit { oid statusCheckRollup { state contexts(first: 80) { nodes {
        __typename
        ... on CheckRun { name conclusion status detailsUrl }
        ... on StatusContext { context state targetUrl }
      } } } } } }
    }
  }
}
"""
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
MAX_PATCH_CHARS = 400_000  # per PR copy given to the agent / shown (GitHub omits patches of huge files anyway)
PR_CACHE_SECONDS = 60


def _gh_files(owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    out = _run_gh(["api", "--paginate", f"repos/{owner}/{repo}/pulls/{number}/files?per_page=100",
                   "--jq", ".[] | {filename, status, additions, deletions, patch, previous_filename}"], timeout=120)
    return [json.loads(ln) for ln in out.splitlines() if ln.strip()]


def _checks(pr: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    nodes = (pr.get("commits") or {}).get("nodes") or []
    roll = (((nodes[0] or {}).get("commit") or {}).get("statusCheckRollup") or {}) if nodes else {}
    out = []
    for c in (roll.get("contexts") or {}).get("nodes") or []:
        if not c:
            continue
        if c.get("__typename") == "CheckRun":
            state = (c.get("conclusion") or c.get("status") or "").upper()
            out.append({"name": c.get("name"), "state": state, "url": c.get("detailsUrl")})
        else:
            out.append({"name": c.get("context"), "state": (c.get("state") or "").upper(), "url": c.get("targetUrl")})
    order = {"FAILURE": 0, "ERROR": 0, "TIMED_OUT": 0, "CANCELLED": 1, "ACTION_REQUIRED": 1, "PENDING": 2, "IN_PROGRESS": 2, "QUEUED": 2}
    out.sort(key=lambda c: (order.get(c["state"], 3), c["name"] or ""))
    return roll.get("state"), out


def pr_markdown(pr: dict[str, Any], files: list[dict[str, Any]]) -> str:
    """The copy of a pull request the agent reads (and that discussions are attached to)."""
    lines = [f"# {pr['repo']}#{pr['number']}: {pr['title']}", "",
             f"- URL: {pr['url']}", f"- Author: {pr['author']}", f"- Branches: {pr['head']} → {pr['base']}",
             f"- State: {pr['state']}{' (draft)' if pr['draft'] else ''}; review decision: {pr['review_decision'] or 'none'}",
             f"- Size: {pr['changed_files']} files, +{pr['additions']} −{pr['deletions']}", "",
             "## Description", "", (pr["body"] or "(no description)").strip(), "", "## Changed files", ""]
    budget = MAX_PATCH_CHARS
    for f in files:
        lines.append(f"### {f['filename']} ({f['status']}, +{f['additions']} −{f['deletions']})")
        patch = f.get("patch")
        if not patch:
            lines += ["", "(no textual diff: binary or too large)", ""]
            continue
        if len(patch) > budget:
            lines += ["", f"(diff omitted: the PR copy is limited to {MAX_PATCH_CHARS} characters of diff)", ""]
            continue
        budget -= len(patch)
        fence = "````" if "```" in patch else "```"
        lines += ["", f"{fence}diff", patch, fence, ""]
    return "\n".join(lines) + "\n"


def fetch_pr(owner: str, repo: str, number: int, graphql: Callable[[str, dict], dict], files_fn: Callable[[str, str, int], list]) -> dict[str, Any]:
    d = graphql(PR_QUERY, {"owner": owner, "name": repo, "number": number})
    r = d.get("repository") or {}
    p = r.get("pullRequest")
    if not p:
        raise ApiError(404, "PR_NOT_FOUND", f"{owner}/{repo}#{number} not found (or not visible to the server's gh account)")
    ci, checks = _checks(p)
    pr = {
        "repo": r["nameWithOwner"], "private": bool(r.get("isPrivate")), "number": p["number"], "title": p["title"], "url": p["url"],
        "body": p.get("body") or "", "state": p.get("state"), "draft": bool(p.get("isDraft")), "author": (p.get("author") or {}).get("login"),
        "created_at": p.get("createdAt"), "updated_at": p.get("updatedAt"), "mergeable": p.get("mergeable"),
        "additions": p.get("additions", 0), "deletions": p.get("deletions", 0), "changed_files": p.get("changedFiles", 0),
        "base": p.get("baseRefName"), "head": p.get("headRefName"), "review_decision": p.get("reviewDecision"),
        "labels": [{"name": lb["name"], "color": lb.get("color")} for lb in (p.get("labels") or {}).get("nodes") or []],
        "reviews": [{"author": (v.get("author") or {}).get("login"), "state": v.get("state"), "submitted_at": v.get("submittedAt"), "url": v.get("url")}
                    for v in (p.get("latestReviews") or {}).get("nodes") or [] if v],
        "requested": [(n.get("requestedReviewer") or {}).get("login") or (n.get("requestedReviewer") or {}).get("slug")
                      for n in (p.get("reviewRequests") or {}).get("nodes") or [] if n],
        "head_sha": ((((p.get("commits") or {}).get("nodes") or [{}])[0] or {}).get("commit") or {}).get("oid"),
        "ci": ci, "checks": checks,
    }
    files = files_fn(owner, repo, number)
    return {"pr": pr, "files": [
        {"path": f["filename"], "previous_path": f.get("previous_filename"), "status": f["status"], "additions": f.get("additions", 0),
         "deletions": f.get("deletions", 0), "unified": (f"--- a/{f.get('previous_filename') or f['filename']}\n+++ b/{f['filename']}\n{f['patch']}\n" if f.get("patch") else ""),
         "binary_or_large": not f.get("patch")}
        for f in files]}


_pr_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _check_names(owner: str, repo: str) -> None:
    if not NAME.match(owner) or not NAME.match(repo):
        raise ApiError(400, "INVALID_REPO", "invalid repository name")


def repo_wiki(ctx: AppContext, repo: str) -> str | None:
    from .models import GithubRepoWiki

    with ctx.db.read() as s:
        row = s.get(GithubRepoWiki, repo.lower())
        return row.workspace_id if row else None


@router.get("/github/pr/{owner}/{repo}/{number}")
async def get_pull_request(owner: str, repo: str, number: int, refresh: bool = Query(False),
                           auth: tuple[AppContext, str] = Depends(require_owner)):
    """A pull request for its tab: details, checks, reviews, changed files; writes the copy the agent discusses."""
    ctx, _ = auth
    _check_names(owner, repo)
    key = f"{owner}/{repo}#{number}".lower()
    hit = _pr_cache.get(key)
    if hit and not refresh and time.time() - hit[0] < PR_CACHE_SECONDS:
        data = hit[1]
    else:
        graphql = _hooks(ctx)[0]
        files_fn = ctx.extra.get("gh_pr_files", _gh_files)
        data = await asyncio.to_thread(fetch_pr, owner, repo, number, graphql, files_fn)
        _pr_cache[key] = (time.time(), data)
        # the copy the agent reads; discussions attach to it like to a document of the repo's wiki
        snap = ctx.settings.snapshots_dir / "github" / owner.lower() / repo.lower()
        snap.mkdir(parents=True, exist_ok=True)
        raw = [{"filename": f["path"], "status": f["status"], "additions": f["additions"], "deletions": f["deletions"],
                "patch": f["unified"].split("\n", 2)[2].rstrip("\n") if f["unified"] else None} for f in data["files"]]
        text = pr_markdown(data["pr"], raw)
        (snap / f"{number}.md").write_text(text, encoding="utf-8")
    wiki = repo_wiki(ctx, data["pr"]["repo"])
    if wiki and ctx.workspaces is not None and wiki not in ctx.workspaces.runtimes:
        wiki = None  # that folder is not open in this window (any more): ask again
    return {**data, "wiki_workspace_id": wiki, "doc_path": f".waw-github/{owner.lower()}/{repo.lower()}/{number}.md"}


class RepoWikiIn(BaseModel):
    repo: str
    workspace_id: str


@router.put("/github/repo-wiki")
async def set_repo_wiki(body: RepoWikiIn, request: Request, ctx: AppContext = Depends(require_auth_mutating)):
    """Which wiki folder the agent uses for this repository's pull requests (asked once per repository)."""
    from .models import GithubRepoWiki

    if not (await hub_status(ctx, request))["available"]:
        raise ApiError(403, "NOT_GITHUB_OWNER", "only the server's owner signed in with GitHub can change this")
    parts = body.repo.split("/")
    if len(parts) != 2:
        raise ApiError(400, "INVALID_REPO", "repo must be owner/name")
    _check_names(*parts)
    if ctx.workspaces is not None and body.workspace_id not in ctx.workspaces.runtimes:
        raise ApiError(400, "WORKSPACE_NOT_OPEN", "choose a folder that is open in this window")
    with ctx.db.write() as s:
        row = s.get(GithubRepoWiki, body.repo.lower())
        if row is None:
            s.add(GithubRepoWiki(repo=body.repo.lower(), workspace_id=body.workspace_id))
        else:
            row.workspace_id = body.workspace_id
    return {"repo": body.repo.lower(), "workspace_id": body.workspace_id}


@router.get("/github/repo-wikis")
def list_repo_wikis(auth: tuple[AppContext, str] = Depends(require_owner)):
    from .models import GithubRepoWiki

    ctx, _ = auth
    with ctx.db.read() as s:
        return {"repos": [{"repo": r.repo, "workspace_id": r.workspace_id} for r in s.query(GithubRepoWiki).order_by(GithubRepoWiki.repo).all()]}
