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
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from .deps import AppContext, require_auth, require_auth_mutating
from .errors import ApiError
from .kv import kv_get, kv_put

log = logging.getLogger("waw.github")

CACHE_SECONDS = 120
PAGE_SIZE = 40  # GitHub answers 502 (timeout) for 100 PRs with reviews/checks/labels inline
MAX_PRS = 500
OWNER_CACHE_SECONDS = 600

router = APIRouter()

REVIEW_QUERY = """
query($q: String!, $after: String, $n: Int!, $me: String!) {
  rateLimit { remaining resetAt }
  search(query: $q, type: ISSUE, first: $n, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url state isDraft createdAt updatedAt additions deletions changedFiles reviewDecision
        author { login }
        headRefOid
        mine: reviews(author: $me, last: 10) { nodes { state submittedAt commit { oid } } }
        others: reviews(last: 10) { nodes { submittedAt author { __typename login } } }
        comments(last: 10) { nodes { createdAt author { __typename login } } }
        timelineItems(itemTypes: [REVIEW_REQUESTED_EVENT], last: 8) { nodes { ... on ReviewRequestedEvent {
          createdAt actor { login ... on User { company } } requestedReviewer { __typename ... on User { login } ... on Team { slug } } } } }
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

class RateGuard:
    """Stay below GitHub's API limits (after ai_tools/github_item.py secure_github_actions): the remaining quota is
    read from `gh api rate_limit` (free) every minute; at <= LOW_WATER requests left, calls wait for the reset when it
    is near, otherwise fail with a clear message instead of being blocked by GitHub. Mutations are spaced >= 1 s
    apart (GitHub's guidance against secondary rate limits)."""

    LOW_WATER = 100
    REFRESH_SECONDS = 60
    MAX_WAIT = 90
    MUTATION_GAP = 1.0

    def __init__(self) -> None:
        import threading

        self.lock = threading.Lock()
        self.limits: dict[str, dict[str, int]] = {}
        self.checked = 0.0
        self.last_mutation = 0.0

    def _refresh(self) -> None:
        gh = shutil.which("gh")
        if not gh:
            return
        try:
            p = subprocess.run([gh, "api", "rate_limit"], capture_output=True, text=True, timeout=20, check=False)
            res = json.loads(p.stdout).get("resources", {}) if p.returncode == 0 else {}
        except (OSError, ValueError, subprocess.SubprocessError):
            res = {}
        self.limits = {k: {"remaining": int(v.get("remaining", 0)), "reset": int(v.get("reset", 0)), "limit": int(v.get("limit", 0))}
                       for k, v in res.items() if isinstance(v, dict)}
        self.checked = time.time()

    def before(self, resource: str, mutation: bool = False) -> None:
        with self.lock:
            if time.time() - self.checked > self.REFRESH_SECONDS:
                self._refresh()
            lim = self.limits.get(resource)
            if lim and lim["remaining"] <= self.LOW_WATER:
                wait = lim["reset"] - time.time() + 2
                if 0 < wait <= self.MAX_WAIT:
                    log.warning("GitHub %s quota low (%d left): waiting %.0f s for the reset", resource, lim["remaining"], wait)
                    time.sleep(wait)
                    self._refresh()
                elif wait > self.MAX_WAIT:
                    at = time.strftime("%H:%M", time.localtime(lim["reset"]))
                    raise ApiError(429, "GH_RATE_LIMITED", f"GitHub API quota almost used up ({lim['remaining']} of {lim['limit']} left); it resets at {at}")
            if lim:
                lim["remaining"] -= 1
            if mutation:
                gap = self.MUTATION_GAP - (time.time() - self.last_mutation)
                if gap > 0:
                    time.sleep(gap)
                self.last_mutation = time.time()

    def status(self) -> dict[str, Any]:
        return {k: v for k, v in self.limits.items() if k in ("core", "graphql", "search")}


RATE = RateGuard()


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
    RATE.before("graphql", mutation=query.lstrip().startswith("mutation"))
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
        "state": pr.get("state") or "OPEN", **_requester(pr, owner), **_approval(pr, owner),
    }


# Not people: their comments are no news for a reviewer (CI, merge and CLA bots; after ai_tools ignored_authors)
BOT_LOGINS = {"pytorchmergebot", "pytorch-bot", "facebook-github-bot", "linux-foundation-easycla", "github-actions", "dependabot",
              "codecov", "netlify", "vercel", "copilot-pull-request-reviewer", "copilot-swe-agent"}


def is_bot(author: dict[str, Any] | None) -> bool:
    a = author or {}
    login = (a.get("login") or "").lower()
    return a.get("__typename") == "Bot" or not login or login.endswith("[bot]") or login in BOT_LOGINS


def _approval(pr: dict[str, Any], owner: str) -> dict[str, Any]:
    """Your latest review, and whether anything happened since you approved: new commits, a review or comment from
    a person (bots and GitHub Actions do not count), or a new request for your review."""
    mine = [n for n in (pr.get("mine") or {}).get("nodes") or [] if n and n.get("state") != "PENDING"]  # not a draft on github.com
    if not mine:
        return {"my_review": None, "approved_no_news": False}
    # your standing verdict: the latest approve / request changes / dismissal (a later plain comment does not undo it)
    verdicts = [n for n in mine if n.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")]
    last = verdicts[-1] if verdicts else mine[-1]
    state, at = last.get("state"), last.get("submittedAt") or ""
    if state != "APPROVED" or not at:
        return {"my_review": state, "approved_no_news": False}
    new_commits = bool(pr.get("headRefOid")) and ((last.get("commit") or {}).get("oid") or "") != pr.get("headRefOid")
    me = owner.lower()

    def news(at2: str | None, author: dict[str, Any] | None) -> bool:
        return bool(at2 and at2 > at and not is_bot(author) and (author or {}).get("login", "").lower() != me)

    talk = any(news(n.get("submittedAt"), n.get("author")) for n in (pr.get("others") or {}).get("nodes") or [] if n) or \
        any(news(n.get("createdAt"), n.get("author")) for n in (pr.get("comments") or {}).get("nodes") or [] if n)
    re_requested = any((n or {}).get("createdAt", "") > at and ((n or {}).get("requestedReviewer") or {}).get("login", "").lower() == me
                       for n in (pr.get("timelineItems") or {}).get("nodes") or [])
    return {"my_review": state, "approved_no_news": not (new_commits or talk or re_requested),
            "since_approval": [x for x, on in (("new commits", new_commits), ("new comments", talk), ("review requested again", re_requested)) if on]}


# Who counts as a "PyTorch Meta maintainer" for prioritising review requests (after ai_tools HARDWARE_VENDORS META)
META_COMPANY = ("meta", "facebook")
META_EMAIL = ("@meta.com", "@fb.com")


def _requester(pr: dict[str, Any], owner: str) -> dict[str, Any]:
    """Who asked the owner for this review (latest request), and whether it is a priority requester."""
    by, company, meta = None, None, False
    for n in (pr.get("timelineItems") or {}).get("nodes") or []:
        rv = (n or {}).get("requestedReviewer") or {}
        if rv.get("__typename") == "User" and (rv.get("login") or "").lower() == owner.lower():
            a = n.get("actor") or {}
            by, company = a.get("login") or by, a.get("company") or None
            email = (a.get("email") or "").lower()
            meta = any(m in (company or "").lower() for m in META_COMPANY) or email.endswith(META_EMAIL)
    return {"requested_by": by, "requested_by_company": company, "requested_by_meta": meta}


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
            d = graphql(REVIEW_QUERY, {"q": "is:pr is:open archived:false review-requested:@me", "after": after, "n": size, "me": owner})
        except ApiError as exc:
            if exc.code != "GH_BUSY" or size <= 10:
                raise
            size //= 2  # GitHub timed out building the page: ask for less
            continue
        s = d["search"]
        total = s["issueCount"]
        rate = d.get("rateLimit")
        items += [x for x in (normalize(n, owner) for n in s["nodes"] if n and n.get("number"))
                  if x["state"] == "OPEN"]  # the search index lags: drop PRs closed / merged meanwhile
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


WATCH_PRS = 40  # newest open PRs fetched per watched repository


def fetch_watched(graphql: Callable[[str, dict], dict], owner: str, repos: list[str], skip: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Open PRs of the repositories the owner added to Reviews (beyond those requesting their review)."""
    items: list[dict[str, Any]] = []
    problems: list[str] = []
    for repo in repos:
        try:
            d = graphql(REVIEW_QUERY, {"q": f"is:pr is:open archived:false repo:{repo} sort:updated-desc", "after": None, "n": WATCH_PRS, "me": owner})
        except ApiError as exc:
            problems.append(f"{repo}: {exc.message}")
            continue
        for n in d["search"]["nodes"]:
            if n and n.get("number"):
                x = normalize(n, owner)
                key = f"{x['repo']}#{x['number']}".lower()
                if x["state"] == "OPEN" and key not in skip:
                    skip.add(key)
                    x["requested"] = None  # nobody asked the owner: shown because the repository is watched
                    items.append(x)
    return items, problems


_watched: dict[str, list[str]] = {}  # owner -> watched repositories (latest prefs, read by a running refresh)


async def _fetch(graphql: Callable[[str, dict], dict], owner: str) -> dict[str, Any]:
    data = await asyncio.to_thread(fetch_queue, graphql, owner)
    watched = _watched.get(owner) or []
    if watched:
        skip = {f"{p['repo']}#{p['number']}".lower() for p in data["items"]}
        data["watched"], data["watch_problems"] = await asyncio.to_thread(fetch_watched, graphql, owner, watched, skip)
    data["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data["owner"] = owner
    _cache[owner] = (time.time(), data)
    _errors.pop(owner, None)
    log.info("review queue for %s: %d PRs (%d hidden)", owner, len(data["items"]), data.get("hidden", 0))
    return data


def _start_refresh(graphql: Callable[[str, dict], dict], owner: str, watched: list[str] | None = None) -> asyncio.Task:
    """One background fetch per owner at a time (a full queue takes ~10 s per 40 PRs)."""
    if watched is not None:
        _watched[owner] = list(watched)
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
        _start_refresh(_hooks(ctx)[0], st["owner"], load_prefs(ctx)["watched_repos"])
    return st


@router.get("/github/reviews")
async def review_queue(refresh: bool = Query(False), auth: tuple[AppContext, str] = Depends(require_owner)):
    """Open PRs requesting the owner's review. Stale-while-revalidate: a cached result older than CACHE_SECONDS (or
    refresh=true) is returned at once with `refreshing: true` while a new fetch runs; poll until it is false."""
    ctx, owner = auth
    graphql, _ = _hooks(ctx)
    prefs = load_prefs(ctx)
    hit = _cache.get(owner)
    if hit is None:
        task = _start_refresh(graphql, owner, prefs["watched_repos"])
        await asyncio.shield(task)  # first load: wait for it
        hit = _cache.get(owner)
        if hit is None:
            err = _errors.get(owner) or {"code": "GH_ERROR", "message": "the review queue could not be loaded"}
            status = {"GH_RATE_LIMITED": 429, "GH_UNAVAILABLE": 503, "GH_AUTH": 503, "GH_UNREACHABLE": 503, "GH_TIMEOUT": 504}.get(err["code"], 502)
            raise ApiError(status, err["code"], err["message"])
    age = time.time() - hit[0]
    running = owner in _tasks and not _tasks[owner].done()
    if (refresh and age >= 10) or age >= CACHE_SECONDS:  # < 10 s: rapid ↻ clicks do not burn the search quota
        _start_refresh(graphql, owner, prefs["watched_repos"])
        running = True
    data = apply_prefs(hit[1], prefs)
    return {**data, "cached": True, "age_seconds": round(age), "refreshing": running, "refresh_error": _errors.get(owner),
            "prefs": prefs, "rate": RATE.status()}


def reset_cache() -> None:
    _cache.clear()
    _errors.clear()
    _tasks.clear()
    _pr_cache.clear()
    _people_cache.clear()
    _user_cache.clear()
    _mention_cache.clear()
    _participants.clear()


# ---------------------------------------------------------------------- a pull request (tab)

PR_QUERY = """
query PrDetail($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner isPrivate
    pullRequest(number: $number) {
      id number title url body state isDraft createdAt updatedAt mergeable additions deletions changedFiles
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
FILES_QUERY = """
query PrFiles($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    files(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { path viewerViewedState } }
  } }
}
"""
THREADS_QUERY = """
query PrThreads($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    reviewThreads(first: 50, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id isResolved isOutdated path line originalLine startLine originalStartLine diffSide startDiffSide subjectType
        resolvedBy { login }
        comments(first: 60) { totalCount nodes { databaseId author { login } body createdAt url diffHunk } }
      }
    }
  } }
}
"""
CONVERSATION_QUERY = """
query PrConversation($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    reviews(last: 100) { totalCount nodes { databaseId author { login } state body submittedAt url
      comments(first: 30) { totalCount nodes { databaseId author { login } createdAt path line originalLine body diffHunk outdated url replyTo { databaseId } } } } }
    comments(last: 100) { totalCount nodes { databaseId author { login } body createdAt url } }
    pending: reviews(states: PENDING, first: 5) { nodes { id comments { totalCount } } }
  } }
}
"""
M_VIEWED = "mutation MarkViewed($pr: ID!, $path: String!) { markFileAsViewed(input: {pullRequestId: $pr, path: $path}) { clientMutationId } }"
M_UNVIEWED = "mutation MarkUnviewed($pr: ID!, $path: String!) { unmarkFileAsViewed(input: {pullRequestId: $pr, path: $path}) { clientMutationId } }"
M_RESOLVE = "mutation Resolve($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { id isResolved } } }"
M_UNRESOLVE = "mutation Unresolve($id: ID!) { unresolveReviewThread(input: {threadId: $id}) { thread { id isResolved } } }"
M_ADD_REVIEW = "mutation AddReview($pr: ID!, $commit: GitObjectID) { addPullRequestReview(input: {pullRequestId: $pr, commitOID: $commit}) { pullRequestReview { id } } }"
M_ADD_THREAD = """mutation AddThread($review: ID!, $path: String!, $body: String!, $line: Int, $side: DiffSide, $startLine: Int,
  $startSide: DiffSide, $subject: PullRequestReviewThreadSubjectType) {
  addPullRequestReviewThread(input: {pullRequestReviewId: $review, path: $path, body: $body, line: $line, side: $side,
    startLine: $startLine, startSide: $startSide, subjectType: $subject}) { thread { id } } }"""
M_SUBMIT = "mutation Submit($review: ID!, $event: PullRequestReviewEvent!, $body: String) { submitPullRequestReview(input: {pullRequestReviewId: $review, event: $event, body: $body}) { pullRequestReview { url } } }"
M_DELETE_REVIEW = "mutation DeleteReview($review: ID!) { deletePullRequestReview(input: {pullRequestReviewId: $review}) { clientMutationId } }"


def _mutate(graphql: Callable[[str, dict], dict], q: str, v: dict[str, Any], key: str) -> dict[str, Any]:
    """Run a mutation; GitHub reports failures as a null result plus errors."""
    d = graphql(q, v)
    res = d.get(key)
    if not res:
        msg = "; ".join(e.get("message", "") for e in d.get("_errors") or []) or "GitHub did not accept the change"
        raise ApiError(502, "GH_ERROR", f"GitHub refused: {msg[:400]}")
    return res


def fetch_viewed(graphql: Callable[[str, dict], dict], owner: str, repo: str, number: int) -> dict[str, str]:
    """path -> VIEWED | UNVIEWED | DISMISSED (viewed, but changed since) for the signed-in gh account."""
    out: dict[str, str] = {}
    after = None
    for _ in range(40):
        try:
            d = graphql(FILES_QUERY, {"owner": owner, "name": repo, "number": number, "after": after})
        except ApiError:
            return out  # optional: the diff still works without it
        f = (((d.get("repository") or {}).get("pullRequest") or {}).get("files")) or {}
        for n in f.get("nodes") or []:
            if n:
                out[n["path"]] = n.get("viewerViewedState") or "UNVIEWED"
        if not (f.get("pageInfo") or {}).get("hasNextPage"):
            break
        after = f["pageInfo"]["endCursor"]
    return out


def fetch_threads(graphql: Callable[[str, dict], dict], owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    after = None
    for _ in range(20):  # 1000 threads
        d = graphql(THREADS_QUERY, {"owner": owner, "name": repo, "number": number, "after": after})
        t = (((d.get("repository") or {}).get("pullRequest") or {}).get("reviewThreads")) or {}
        for n in t.get("nodes") or []:
            if not n:
                continue
            cs = (n.get("comments") or {}).get("nodes") or []
            threads.append({
                "id": n["id"], "resolved": bool(n.get("isResolved")), "outdated": bool(n.get("isOutdated")),
                "resolved_by": (n.get("resolvedBy") or {}).get("login"), "path": n.get("path"),
                "line": n.get("line"), "original_line": n.get("originalLine"), "start_line": n.get("startLine"),
                "side": n.get("diffSide") or "RIGHT", "start_side": n.get("startDiffSide"),
                "subject": (n.get("subjectType") or "LINE").upper(), "total": (n.get("comments") or {}).get("totalCount", len(cs)),
                "diff_hunk": (cs[0].get("diffHunk") if cs else None),
                "comments": [{"id": c.get("databaseId"), "author": (c.get("author") or {}).get("login"), "body": c.get("body") or "",
                              "created_at": c.get("createdAt"), "url": c.get("url")} for c in cs if c],
            })
        if not (t.get("pageInfo") or {}).get("hasNextPage"):
            break
        after = t["pageInfo"]["endCursor"]
    return threads


def fetch_conversation(graphql: Callable[[str, dict], dict], owner: str, repo: str, number: int) -> dict[str, Any]:
    d = graphql(CONVERSATION_QUERY, {"owner": owner, "name": repo, "number": number})
    p = ((d.get("repository") or {}).get("pullRequest")) or {}
    items: list[dict[str, Any]] = []
    for r in (p.get("reviews") or {}).get("nodes") or []:
        if not r or r.get("state") == "PENDING":
            continue
        rc = (r.get("comments") or {}).get("nodes") or []
        r_author = (r.get("author") or {}).get("login")
        items.append({"kind": "review", "id": r.get("databaseId"), "author": (r.get("author") or {}).get("login"), "state": r.get("state"),
                      "body": r.get("body") or "", "created_at": r.get("submittedAt"), "url": r.get("url"),
                      "line_comments": (r.get("comments") or {}).get("totalCount", 0),
                      # shown inline under the review, with the code they are about — as on GitHub's Conversation tab
                      "comments": [{"id": c.get("databaseId"), "path": c.get("path"), "line": c.get("line") or c.get("originalLine"),
                                    "outdated": bool(c.get("outdated")), "body": c.get("body") or "", "url": c.get("url"),
                                    "reply": bool(c.get("replyTo")), "reply_to": (c.get("replyTo") or {}).get("databaseId"),
                                    "author": (c.get("author") or {}).get("login") or r_author, "created_at": c.get("createdAt") or r.get("submittedAt"), "hunk": "\n".join((c.get("diffHunk") or "").splitlines()[-6:])}
                                   for c in rc if c]})
    items.extend({"kind": "comment", "id": c.get("databaseId"), "author": (c.get("author") or {}).get("login"), "body": c.get("body") or "",
                  "created_at": c.get("createdAt"), "url": c.get("url")} for c in (p.get("comments") or {}).get("nodes") or [] if c)
    items.sort(key=lambda x: x.get("created_at") or "")
    pend = [n for n in (p.get("pending") or {}).get("nodes") or [] if n]
    return {"items": items, "pending_review_id": pend[0]["id"] if pend else None,
            "pending_comments": (pend[0].get("comments") or {}).get("totalCount", 0) if pend else 0,
            "older_hidden": max(0, (p.get("comments") or {}).get("totalCount", 0) - 100) + max(0, (p.get("reviews") or {}).get("totalCount", 0) - 100)}


def discussion_markdown(threads: list[dict[str, Any]], conv: dict[str, Any], limit: int = 120_000) -> str:
    """The review discussion as text for the agent (conversation, then threads by file)."""
    out = ["## Review discussion on GitHub", ""]
    for it in conv["items"]:
        what = f"review: {it['state'].lower().replace('_', ' ')}" if it["kind"] == "review" else "comment"
        if it["kind"] == "review" and not it["body"].strip():
            out.append(f"- {it['author']} ({what}, {it.get('line_comments', 0)} line comments)")
        else:
            out.append(f"- {it['author']} ({what}): {it['body'].strip()[:3000]}")
    out += ["", "### Review threads", ""]
    for t in threads:
        where = f"{t['path']}" + ("" if t["subject"] == "FILE" else f" line {t['line'] or t['original_line']} ({'old' if t['side'] == 'LEFT' else 'new'})")
        flags = ", ".join(x for x in ("resolved" if t["resolved"] else "", "outdated" if t["outdated"] else "") if x)
        out.append(f"#### {where}{f' [{flags}]' if flags else ''}")
        if t.get("diff_hunk"):
            out += ["```diff", "\n".join(t["diff_hunk"].splitlines()[-8:]), "```"]
        out += [f"- {c['author']}: {c['body'].strip()[:2000]}" for c in t["comments"]]
        out.append("")
    text = "\n".join(out) + "\n"
    return text if len(text) <= limit else text[:limit] + "\n(truncated)\n"


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
        raise ApiError(404, "PR_NOT_FOUND", f"{owner}/{repo}#{number} is not a pull request visible to the server's gh account (it may be an issue)")
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
        "ci": ci, "checks": checks, "node_id": p.get("id"),
    }
    files = files_fn(owner, repo, number)
    viewed = fetch_viewed(graphql, owner, repo, number)
    return {"pr": pr, "files": [
        {"path": f["filename"], "previous_path": f.get("previous_filename"), "status": f["status"], "additions": f.get("additions", 0),
         "deletions": f.get("deletions", 0), "unified": (f"--- a/{f.get('previous_filename') or f['filename']}\n+++ b/{f['filename']}\n{f['patch']}\n" if f.get("patch") else ""),
         "binary_or_large": not f.get("patch"), "viewed": viewed.get(f["filename"], "UNVIEWED")}
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
        hit = None
        graphql = _hooks(ctx)[0]
        files_fn = ctx.extra.get("gh_pr_files", _gh_files)
        data = await asyncio.to_thread(fetch_pr, owner, repo, number, graphql, files_fn)
        _pr_cache[key] = (time.time(), data)
        hit = None
    # the copy the agent reads; discussions attach to it like to a document of the repo's wiki (also written when
    # the PR came into the cache another way, e.g. a local sync, before its tab was opened)
    snap = ctx.settings.snapshots_dir / "github" / owner.lower() / repo.lower()
    if hit is None or not (snap / f"{number}.md").is_file():
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


# ---------------------------------------------------------------------- review comments: read, draft, publish

def gh_rest(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """`gh api` REST call; JSON body on stdin. Lists are fetched with --paginate."""
    gh = shutil.which("gh")
    if not gh:
        raise ApiError(503, "GH_UNAVAILABLE", "the GitHub CLI (gh) is not installed on the server")
    RATE.before("core", mutation=method != "GET")
    args = [gh, "api", "-X", method, path]
    if method == "GET":
        args.insert(2, "--paginate")
    if body is not None:
        args += ["--input", "-"]
    try:
        p = subprocess.run(args, input=json.dumps(body) if body is not None else None, capture_output=True, text=True, timeout=90, check=False)
    except subprocess.TimeoutExpired:
        raise ApiError(504, "GH_TIMEOUT", "GitHub did not answer in time")
    if p.returncode != 0:
        out = (p.stdout or "").strip()
        msg = (p.stderr or out).strip()
        try:  # GitHub's JSON error: {"message": ..., "errors": [...]}
            j = json.loads(out)
            detail = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in j.get("errors") or [])
            msg = j.get("message", msg) + (f": {detail}" if detail else "")
        except (ValueError, AttributeError):
            pass
        if "rate limit" in msg.lower():
            raise ApiError(429, "GH_RATE_LIMITED", "GitHub's rate limit is reached; try again in a minute")
        raise ApiError(502, "GH_ERROR", f"GitHub refused: {msg[-400:]}")
    out = p.stdout.strip()
    if not out:
        return None
    if method == "GET":
        # --paginate prints one JSON document per page: [..][..] — decode them all and join lists
        dec = json.JSONDecoder()
        docs: list[Any] = []
        i = 0
        while i < len(out):
            obj, i = dec.raw_decode(out, i)
            docs.append(obj)
            while i < len(out) and out[i].isspace():
                i += 1
        if all(isinstance(d, list) for d in docs):
            return [x for d in docs for x in d]
        return docs[0] if len(docs) == 1 else docs
    return json.loads(out)


def _rest(ctx: AppContext) -> Callable[..., Any]:
    return ctx.extra.get("gh_rest", gh_rest)


async def require_owner_mutating(request: Request, ctx: AppContext = Depends(require_auth_mutating)) -> tuple[AppContext, str]:
    st = await hub_status(ctx, request)
    if not st["available"]:
        raise ApiError(403, "NOT_GITHUB_OWNER", "only the server's owner signed in with GitHub can do this")
    return ctx, st["owner"]


def _comment_dict(c: dict[str, Any]) -> dict[str, Any]:
    return {"id": c["id"], "path": c.get("path"), "line": c.get("line"), "side": c.get("side") or "RIGHT",
            "start_line": c.get("start_line"), "start_side": c.get("start_side"), "original_line": c.get("original_line"),
            "outdated": c.get("line") is None, "body": c.get("body") or "", "author": (c.get("user") or {}).get("login"),
            "created_at": c.get("created_at"), "in_reply_to_id": c.get("in_reply_to_id"), "url": c.get("html_url"),
            "diff_hunk": c.get("diff_hunk")}


def _draft_dict(d: Any) -> dict[str, Any]:
    return {"id": d.id, "path": d.path, "subject": d.subject or "line", "side": d.side, "line": d.line, "start_side": d.start_side, "start_line": d.start_line,
            "line_text": d.line_text, "body": d.body, "created_at": d.created_at.isoformat() if d.created_at else None,
            "origin": d.origin or "user", "decision": d.decision or "accepted", "thread_id": d.thread_id}


@router.get("/github/pr/{owner}/{repo}/{number}/comments")
async def pr_comments(owner: str, repo: str, number: int, auth: tuple[AppContext, str] = Depends(require_owner)):
    """Everything reviewers said: review threads (resolved/outdated, line or file), the conversation (review
    summaries and general comments), your unpublished drafts, and your pending review started on github.com."""
    from .models import GithubPrDraft

    ctx, _ = auth
    _check_names(owner, repo)
    graphql = _hooks(ctx)[0]
    agent_reviews = await asyncio.to_thread(import_agent_reviews, ctx, owner, repo, number)
    threads, conv = await asyncio.gather(asyncio.to_thread(fetch_threads, graphql, owner, repo, number),
                                         asyncio.to_thread(fetch_conversation, graphql, owner, repo, number))
    with ctx.db.read() as s:
        drafts = s.query(GithubPrDraft).filter_by(repo=f"{owner}/{repo}".lower(), number=number).order_by(GithubPrDraft.created_at).all()
        dd = [_draft_dict(d) for d in drafts]
    _remember_participants(f"{owner}/{repo}#{number}".lower(), [(c.get("author"), "commenter") for c in conv["items"]]
                           + [(c.get("author"), "commenter") for t in threads for c in t.get("comments") or []])
    # the agent reads the review discussion next to the PR copy
    snap = ctx.settings.snapshots_dir / "github" / owner.lower() / repo.lower()
    snap.mkdir(parents=True, exist_ok=True)
    (snap / f"{number}.discussion.md").write_text(discussion_markdown(threads, conv), encoding="utf-8")
    return {"threads": threads, "conversation": conv["items"], "older_hidden": conv["older_hidden"], "drafts": dd,
            "pending_on_github": conv["pending_comments"] if conv["pending_review_id"] else None, "agent_reviews": agent_reviews}


def _pr_node(ctx: AppContext, owner: str, repo: str, number: int) -> dict[str, Any]:
    key = f"{owner}/{repo}#{number}".lower()
    hit = _pr_cache.get(key)
    if hit:
        return hit[1]["pr"]
    data = fetch_pr(owner, repo, number, _hooks(ctx)[0], ctx.extra.get("gh_pr_files", _gh_files))
    _pr_cache[key] = (time.time(), data)
    return data["pr"]


class ViewedIn(BaseModel):
    path: str
    viewed: bool


@router.put("/github/pr/{owner}/{repo}/{number}/viewed")
async def set_viewed(owner: str, repo: str, number: int, body: ViewedIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    """Mark a file viewed / not viewed on GitHub (shared with github.com)."""
    ctx, _ = auth
    _check_names(owner, repo)
    pr = await asyncio.to_thread(_pr_node, ctx, owner, repo, number)
    graphql = _hooks(ctx)[0]
    q, key = (M_VIEWED, "markFileAsViewed") if body.viewed else (M_UNVIEWED, "unmarkFileAsViewed")
    await asyncio.to_thread(_mutate, graphql, q, {"pr": pr["node_id"], "path": body.path}, key)
    hit = _pr_cache.get(f"{owner}/{repo}#{number}".lower())
    if hit:
        for f in hit[1]["files"]:
            if f["path"] == body.path:
                f["viewed"] = "VIEWED" if body.viewed else "UNVIEWED"
    return {"path": body.path, "viewed": "VIEWED" if body.viewed else "UNVIEWED"}


class ResolveIn(BaseModel):
    resolved: bool


@router.post("/github/pr/{owner}/{repo}/{number}/threads/{thread_id}/resolve")
async def resolve_thread(owner: str, repo: str, number: int, thread_id: str, body: ResolveIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    ctx, _ = auth
    _check_names(owner, repo)
    if not re.fullmatch(r"[A-Za-z0-9_=-]{4,100}", thread_id):
        raise ApiError(400, "INVALID_THREAD", "invalid thread id")
    q, key = (M_RESOLVE, "resolveReviewThread") if body.resolved else (M_UNRESOLVE, "unresolveReviewThread")
    res = await asyncio.to_thread(_mutate, _hooks(ctx)[0], q, {"id": thread_id}, key)
    return {"id": thread_id, "resolved": bool(((res or {}).get("thread") or {}).get("isResolved", body.resolved))}


class ConversationIn(BaseModel):
    body: str


@router.post("/github/pr/{owner}/{repo}/{number}/conversation", status_code=201)
async def add_conversation_comment(owner: str, repo: str, number: int, body: ConversationIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    """A general comment on the pull request (posted immediately)."""
    ctx, _ = auth
    _check_names(owner, repo)
    if not body.body.strip():
        raise ApiError(400, "INVALID_COMMENT", "a comment needs text")
    res = await asyncio.to_thread(_rest(ctx), "POST", f"repos/{owner}/{repo}/issues/{number}/comments", {"body": body.body.strip()})
    return {"kind": "comment", "id": (res or {}).get("id"), "author": ((res or {}).get("user") or {}).get("login"),
            "body": (res or {}).get("body", body.body.strip()), "created_at": (res or {}).get("created_at"), "url": (res or {}).get("html_url")}


class DraftIn(BaseModel):
    path: str
    subject: str = "line"  # line | file (a comment on the whole file)
    side: str = "RIGHT"
    line: int = 0
    start_side: str | None = None
    start_line: int | None = None
    line_text: str = ""
    body: str


@router.post("/github/pr/{owner}/{repo}/{number}/drafts", status_code=201)
def add_draft(owner: str, repo: str, number: int, body: DraftIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    from .db import new_id
    from .models import GithubPrDraft

    ctx, _ = auth
    _check_names(owner, repo)
    if body.subject not in ("line", "file"):
        raise ApiError(400, "INVALID_COMMENT", "subject must be line or file")
    if body.subject == "file":
        body.line, body.side, body.start_line, body.start_side = 0, "RIGHT", None, None
        if not body.body.strip():
            raise ApiError(400, "INVALID_COMMENT", "a comment needs text")
    elif body.side not in ("LEFT", "RIGHT") or (body.start_side not in (None, "LEFT", "RIGHT")) or not body.body.strip() or body.line < 1:
        raise ApiError(400, "INVALID_COMMENT", "a comment needs text, a line and a side (LEFT/RIGHT)")
    if body.start_line is not None and (body.start_line < 1 or (body.start_side == body.side and body.start_line >= body.line)):
        body.start_line = body.start_side = None  # a "range" of one line
    with ctx.db.write() as s:
        d = GithubPrDraft(id=new_id(), repo=f"{owner}/{repo}".lower(), number=number, path=body.path, subject=body.subject, side=body.side, line=body.line,
                          start_side=body.start_side if body.start_line else None, start_line=body.start_line,
                          line_text=body.line_text[:4000], body=body.body.strip())
        s.add(d)
        s.flush()
        return _draft_dict(d)


class DraftEditIn(BaseModel):
    body: str | None = None
    decision: str | None = None  # accepted (apply) | proposed | discarded — for agent suggestions


@router.patch("/github/pr/{owner}/{repo}/{number}/drafts/{draft_id}")
def edit_draft(owner: str, repo: str, number: int, draft_id: str, body: DraftEditIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    from .models import GithubPrDraft

    ctx, _ = auth
    with ctx.db.write() as s:
        d = s.get(GithubPrDraft, draft_id)
        if d is None or d.repo != f"{owner}/{repo}".lower() or d.number != number:
            raise ApiError(404, "DRAFT_NOT_FOUND", "draft comment not found")
        if body.body is not None:
            if not body.body.strip():
                raise ApiError(400, "INVALID_COMMENT", "a comment needs text")
            d.body = body.body.strip()
        if body.decision is not None:
            if body.decision not in ("accepted", "proposed", "discarded"):
                raise ApiError(400, "INVALID_DECISION", "decision must be accepted, proposed or discarded")
            d.decision = body.decision
        return _draft_dict(d)


@router.delete("/github/pr/{owner}/{repo}/{number}/drafts/{draft_id}")
def delete_draft(owner: str, repo: str, number: int, draft_id: str, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    from .models import GithubPrDraft

    ctx, _ = auth
    with ctx.db.write() as s:
        d = s.get(GithubPrDraft, draft_id)
        if d is None or d.repo != f"{owner}/{repo}".lower() or d.number != number:
            raise ApiError(404, "DRAFT_NOT_FOUND", "draft comment not found")
        s.delete(d)
    return {"deleted": draft_id}


class SubmitIn(BaseModel):
    event: str = "COMMENT"  # COMMENT | APPROVE | REQUEST_CHANGES
    body: str = ""


@router.post("/github/pr/{owner}/{repo}/{number}/review")
async def submit_review(owner: str, repo: str, number: int, body: SubmitIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    """Publish your drafts (line and file comments) and a summary to GitHub as one review, as the server's gh account.
    A pending review you started on github.com is reused, so its comments are submitted too."""
    from .models import GithubPrDraft

    ctx, owner_login = auth
    _check_names(owner, repo)
    if body.event not in ("COMMENT", "APPROVE", "REQUEST_CHANGES"):
        raise ApiError(400, "INVALID_EVENT", "event must be COMMENT, APPROVE or REQUEST_CHANGES")
    with ctx.db.read() as s:
        drafts = s.query(GithubPrDraft).filter_by(repo=f"{owner}/{repo}".lower(), number=number).order_by(GithubPrDraft.created_at).all()
        everything = [_draft_dict(d) for d in drafts]
    dd = [d for d in everything if d["decision"] == "accepted"]  # agent suggestions only once you chose Apply
    discarded = [d["id"] for d in everything if d["decision"] == "discarded"]
    undecided = sum(1 for d in everything if d["decision"] == "proposed")
    graphql = _hooks(ctx)[0]
    conv = await asyncio.to_thread(fetch_conversation, graphql, owner, repo, number)
    pending, pending_n = conv["pending_review_id"], conv["pending_comments"]
    if not dd and not pending_n and not body.body.strip() and body.event != "APPROVE":
        raise ApiError(400, "EMPTY_REVIEW", "write a comment or a summary first")
    pr = await asyncio.to_thread(_pr_node, ctx, owner, repo, number)

    def publish() -> str | None:
        created = False
        review = pending
        if review is None:
            review = _mutate(graphql, M_ADD_REVIEW, {"pr": pr["node_id"], "commit": pr.get("head_sha")}, "addPullRequestReview")["pullRequestReview"]["id"]
            created = True
        try:
            for d in dd:
                v: dict[str, Any] = {"review": review, "path": d["path"], "body": d["body"]}
                if d["subject"] == "file":
                    v["subject"] = "FILE"
                else:
                    v.update({"line": d["line"], "side": d["side"], "subject": "LINE"})
                    if d["start_line"]:
                        v.update({"startLine": d["start_line"], "startSide": d["start_side"] or d["side"]})
                try:
                    _mutate(graphql, M_ADD_THREAD, v, "addPullRequestReviewThread")
                except ApiError as exc:
                    where = d["path"] if d["subject"] == "file" else f"{d['path']} line {d['line']}"
                    raise ApiError(exc.status, exc.code, f"{exc.message} (comment on {where})")
            res = _mutate(graphql, M_SUBMIT, {"review": review, "event": body.event, "body": body.body.strip() or None}, "submitPullRequestReview")
            return (res.get("pullRequestReview") or {}).get("url")
        except ApiError:
            if created:  # leave nothing half-done on GitHub; the drafts stay here
                try:
                    _mutate(graphql, M_DELETE_REVIEW, {"review": review}, "deletePullRequestReview")
                except ApiError:
                    log.warning("could not delete the unfinished review on %s/%s#%d", owner, repo, number)
            raise

    url = await asyncio.to_thread(publish)
    with ctx.db.write() as s:
        s.query(GithubPrDraft).filter(GithubPrDraft.id.in_([d["id"] for d in dd] + discarded)).delete(synchronize_session=False)
    _pr_cache.pop(f"{owner}/{repo}#{number}".lower(), None)
    _cache.pop(owner_login, None)  # the review queue changes too
    log.info("published review on %s/%s#%d (%s, %d comments, %d from github.com)", owner, repo, number, body.event, len(dd), pending_n)
    return {"published": len(dd), "from_github": pending_n, "event": body.event, "url": url, "undecided": undecided}


class ReplyIn(BaseModel):
    body: str


@router.post("/github/pr/{owner}/{repo}/{number}/comments/{comment_id}/reply", status_code=201)
async def reply_comment(owner: str, repo: str, number: int, comment_id: int, body: ReplyIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    """Reply to an existing review thread on GitHub (published immediately)."""
    ctx, _ = auth
    _check_names(owner, repo)
    if not body.body.strip():
        raise ApiError(400, "INVALID_COMMENT", "a reply needs text")
    res = await asyncio.to_thread(_rest(ctx), "POST", f"repos/{owner}/{repo}/pulls/{number}/comments/{comment_id}/replies", {"body": body.body.strip()})
    return _comment_dict(res or {"id": 0})


# ---------------------------------------------------------------------- hub preferences (kv_settings)


DEFAULT_PREFS: dict[str, Any] = {"hidden_repos": [], "watched_repos": [], "priority_requesters": [], "priority_meta": True, "show_approved": False,
                                 "starred": [], "unstarred": []}


def load_prefs(ctx: AppContext) -> dict[str, Any]:
    return {**DEFAULT_PREFS, **kv_get(ctx, "github_hub_prefs", {})}


def pr_key(p: dict[str, Any]) -> str:
    return f"{p['repo']}#{p['number']}".lower()


def is_priority(p: dict[str, Any], prefs: dict[str, Any]) -> bool:
    """★ by hand wins both ways: starred PRs are priority, un-starred ones never are; otherwise the rules."""
    k = pr_key(p)
    if k in {x.lower() for x in prefs.get("starred") or []}:
        return True
    if k in {x.lower() for x in prefs.get("unstarred") or []}:
        return False
    by = (p.get("requested_by") or "").lower()
    if by and by in {x.lower() for x in prefs.get("priority_requesters") or []}:
        return True
    return bool(prefs.get("priority_meta", True) and p.get("requested_by_meta"))


def apply_prefs(data: dict[str, Any], prefs: dict[str, Any]) -> dict[str, Any]:
    """Hide removed repositories and mark priority requests (PyTorch Meta maintainers / chosen people)."""
    hidden = {r.lower() for r in prefs.get("hidden_repos") or []}
    visible = [p for p in data.get("items", []) if p["repo"].lower() not in hidden]
    quiet = [p for p in visible if p.get("approved_no_news")] if not prefs.get("show_approved") else []
    items = [{**p, "priority": is_priority(p, prefs)} for p in visible if p not in quiet]
    watching = {r.lower() for r in prefs.get("watched_repos") or []}  # a repository just unwatched disappears at once
    watched = [{**p, "priority": False, "watched": True} for p in data.get("watched", [])
               if p["repo"].lower() not in hidden and p["repo"].lower() in watching and (prefs.get("show_approved") or not p.get("approved_no_news"))]
    return {**data, "items": items, "watched": watched, "hidden_count": len(data.get("items", [])) - len(visible),
            "approved_hidden": len(quiet)}


class PrefsIn(BaseModel):
    hidden_repos: list[str] | None = None
    watched_repos: list[str] | None = None
    priority_requesters: list[str] | None = None
    priority_meta: bool | None = None
    show_approved: bool | None = None  # also list PRs you approved where nothing happened since
    star: str | None = None    # "owner/repo#n": make it a priority PR
    unstar: str | None = None  # "owner/repo#n": never priority (even when the rules say so)


@router.put("/github/prefs")
async def put_prefs(body: PrefsIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    """Repositories shown / hidden / watched in Reviews, and who counts as a priority requester."""
    ctx, owner = auth
    prefs = load_prefs(ctx)
    for k in ("hidden_repos", "watched_repos", "priority_requesters"):
        v = getattr(body, k)
        if v is not None:
            clean = []
            for x in v:
                x = x.strip()
                if k != "priority_requesters":
                    parts = x.split("/")
                    if len(parts) != 2:
                        raise ApiError(400, "INVALID_REPO", f"{x!r}: use owner/name")
                    _check_names(*parts)
                elif not NAME.match(x):
                    raise ApiError(400, "INVALID_LOGIN", f"{x!r} is not a GitHub login")
                if x.lower() not in {c.lower() for c in clean}:
                    clean.append(x)
            prefs[k] = clean
    if body.priority_meta is not None:
        prefs["priority_meta"] = body.priority_meta
    if body.show_approved is not None:
        prefs["show_approved"] = body.show_approved
    for k, on in ((body.star, "starred"), (body.unstar, "unstarred")):
        if k is None:
            continue
        m = re.fullmatch(r"([\w.-]+)/([\w.-]+)#(\d+)", k.strip())
        if not m:
            raise ApiError(400, "INVALID_PR", f"{k!r}: use owner/repo#number")
        key = k.strip().lower()
        off = "unstarred" if on == "starred" else "starred"
        prefs[off] = [x for x in prefs.get(off) or [] if x.lower() != key]
        prefs[on] = [x for x in prefs.get(on) or [] if x.lower() != key] + [key]
        prefs[on] = prefs[on][-500:]
    kv_put(ctx, "github_hub_prefs", prefs)
    if body.watched_repos is not None:  # watched PRs are fetched with the queue: refresh it in the background
        _start_refresh(_hooks(ctx)[0], owner, prefs["watched_repos"])
    return prefs


# ---------------------------------------------------------------------- extra context folders for the agent

@router.get("/agent-context")
def get_agent_context(scope: str = Query(..., max_length=300), ctx: AppContext = Depends(require_auth)):
    """Extra folders the agent should read for a repository's PRs (scope repo:owner/name) or a wiki folder (ws:<id>)."""
    scope = _check_scope(ctx, scope)
    return {"scope": scope, "dirs": kv_get(ctx, f"context:{scope}", [])}


class ContextIn(BaseModel):
    dirs: list[str]


def _check_scope(ctx: AppContext, scope: str) -> str:
    kind, _, val = scope.strip().partition(":")
    if kind == "repo" and len(val.split("/")) == 2:
        _check_names(*val.split("/"))
        return f"repo:{val.lower()}"
    if kind == "ws" and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", val):
        return f"ws:{val}"
    raise ApiError(400, "INVALID_SCOPE", "scope must be repo:owner/name or ws:<folder id>")


@router.put("/agent-context")
async def put_agent_context(body: ContextIn, request: Request, scope: str = Query(..., max_length=300),
                            ctx: AppContext = Depends(require_auth_mutating)):
    scope = _check_scope(ctx, scope)
    if scope.startswith("repo:") and not (await hub_status(ctx, request))["available"]:
        raise ApiError(403, "NOT_GITHUB_OWNER", "only the server's owner signed in with GitHub can change this")
    clean: list[str] = []
    for d in body.dirs[:30]:
        p = Path(d.strip()).expanduser()
        if not p.is_absolute():
            raise ApiError(400, "INVALID_PATH", f"{d!r}: give an absolute path on the server")
        if not p.is_dir():
            raise ApiError(404, "FOLDER_NOT_FOUND", f"{p} is not a directory on the server")
        if str(p.resolve()) not in clean:
            clean.append(str(p.resolve()))
    kv_put(ctx, f"context:{scope}", clean)
    return {"scope": scope, "dirs": clean}



# ---------------------------------------------------------------------- PR contributors

# `email` would need the user:email scope (gh's token lacks it): the public email comes from REST /users/{login}
USER_FIELDS = """login name company location bio websiteUrl twitterUsername url avatarUrl createdAt
  organizations(first: 20) { nodes { login name } } followers { totalCount }"""

PEOPLE_QUERY = f"""
query PrPeople($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{ pullRequest(number: $number) {{
    author {{ login ... on User {{ {USER_FIELDS} }} }}
    commits(first: 100) {{ totalCount nodes {{ commit {{ authors(first: 6) {{ nodes {{ name email user {{ {USER_FIELDS} }} }} }} }} }} }}
  }} }}
}}"""

# affiliation guessed from public profile data and commit emails (after ai_tools/github_item.py HARDWARE_VENDORS)
VENDORS: dict[str, dict[str, tuple[str, ...]]] = {
    "Intel": {"email": ("@intel.com", "@habana.ai"), "company": ("intel",), "org": ("intel",)},
    "Meta": {"email": ("@meta.com", "@fb.com"), "company": ("meta", "facebook"), "org": ("facebook", "meta", "facebookresearch", "meta-pytorch")},
    "NVIDIA": {"email": ("@nvidia.com",), "company": ("nvidia",), "org": ("nvidia",)},
    "AMD": {"email": ("@amd.com",), "company": ("amd",), "org": ("rocm", "amd")},
    "Google": {"email": ("@google.com",), "company": ("google",), "org": ("google",)},
    "Microsoft": {"email": ("@microsoft.com",), "company": ("microsoft",), "org": ("microsoft",)},
}


def affiliation(emails: list[str], company: str | None, orgs: list[str]) -> list[str]:
    out: list[str] = []
    comp = (company or "").lower()
    lorgs = {o.lower() for o in orgs}
    for v, pat in VENDORS.items():
        if (any(e.lower().endswith(pat["email"]) for e in emails if e) or any(p in comp for p in pat["company"])
                or any(o in lorgs for o in pat["org"])):
            out.append(v)
    return out


def fetch_people(graphql: Callable[[str, dict], dict], owner: str, repo: str, number: int) -> dict[str, Any]:
    d = graphql(PEOPLE_QUERY, {"owner": owner, "name": repo, "number": number})
    p = ((d.get("repository") or {}).get("pullRequest")) or {}
    people: dict[str, dict[str, Any]] = {}

    def person(u: dict[str, Any] | None, name: str | None = None, email: str | None = None) -> dict[str, Any]:
        login = (u or {}).get("login")
        key = (login or f"~{email or name}").lower()
        x = people.get(key)
        if x is None:
            u = u or {}
            x = people[key] = {
                "login": login, "name": u.get("name") or name, "url": u.get("url"), "avatar": u.get("avatarUrl"),
                "public_email": u.get("email") or None, "company": u.get("company"), "location": u.get("location"), "bio": u.get("bio"),
                "website": u.get("websiteUrl"), "twitter": u.get("twitterUsername"), "joined": u.get("createdAt"),
                "followers": (u.get("followers") or {}).get("totalCount"),
                "orgs": [{"login": o["login"], "name": o.get("name")} for o in (u.get("organizations") or {}).get("nodes") or [] if o],
                "roles": [], "commits": 0, "commit_emails": [], "commit_names": [],
            }
        if email and email not in x["commit_emails"] and not email.endswith("@users.noreply.github.com"):
            x["commit_emails"].append(email)
        if name and name not in x["commit_names"]:
            x["commit_names"].append(name)
        return x

    if p.get("author"):
        person(p["author"])["roles"].append("author")
    for n in (p.get("commits") or {}).get("nodes") or []:
        authors = (((n or {}).get("commit") or {}).get("authors") or {}).get("nodes") or []
        for i, a in enumerate(a for a in authors if a):
            x = person(a.get("user"), a.get("name"), a.get("email"))
            role = "commit author" if i == 0 else "co-author"
            if role not in x["roles"]:
                x["roles"].append(role)
            if i == 0:
                x["commits"] += 1
    for x in people.values():
        x["affiliation"] = affiliation([x["public_email"] or "", *x["commit_emails"]], x["company"], [o["login"] for o in x["orgs"]])
    order = sorted(people.values(), key=lambda x: ("author" not in x["roles"], -x["commits"], (x["login"] or x["name"] or "").lower()))
    return {"contributors": order, "commits_total": (p.get("commits") or {}).get("totalCount", 0),
            "commits_listed": min(100, (p.get("commits") or {}).get("totalCount", 0))}


_people_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_user_cache: dict[str, tuple[float, dict[str, Any]]] = {}
MAX_DETAILS = 30


def user_details(rest: Callable[..., Any], login: str) -> dict[str, Any]:
    """Public profile fields only REST returns without extra token scopes (public email, blog, hireable, …)."""
    hit = _user_cache.get(login.lower())
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    try:
        u = rest("GET", f"users/{login}") or {}
        if isinstance(u, list):
            u = u[0] if u else {}
    except ApiError as exc:
        return {"error": exc.message}
    d = {"public_email": u.get("email"), "website": u.get("blog") or None, "hireable": u.get("hireable"), "twitter": u.get("twitter_username"),
         "public_repos": u.get("public_repos"), "type": u.get("type")}
    _user_cache[login.lower()] = (time.time(), d)
    return d


@router.get("/github/pr/{owner}/{repo}/{number}/contributors")
async def pr_contributors(owner: str, repo: str, number: int, refresh: bool = Query(False), details: bool = Query(False),
                          auth: tuple[AppContext, str] = Depends(require_owner)):
    """Who contributed to the PR (author, commit authors, co-authors) with their public GitHub profile: name, commit
    emails, company, organizations, location, … and a guessed affiliation. details=true adds the public email and
    other REST-only profile fields (one request per person, cached)."""
    ctx, _ = auth
    _check_names(owner, repo)
    key = f"{owner}/{repo}#{number}".lower()
    hit = _people_cache.get(key)
    if hit and not refresh and time.time() - hit[0] < 600:
        data = hit[1]
    else:
        data = await asyncio.to_thread(fetch_people, _hooks(ctx)[0], owner, repo, number)
        _people_cache[key] = (time.time(), data)
        _remember_participants(key, [(c["login"], "contributor") for c in data["contributors"]])
    if details:
        rest = _rest(ctx)
        people = [dict(x) for x in data["contributors"]]
        for x in people[:MAX_DETAILS]:
            if x["login"]:
                extra = await asyncio.to_thread(user_details, rest, x["login"])
                x.update({k: v for k, v in extra.items() if v is not None or k not in x})
                x["affiliation"] = affiliation([x.get("public_email") or "", *x["commit_emails"]], x["company"], [o["login"] for o in x["orgs"]])
        data = {**data, "contributors": people, "details": True}
    return data



# ---------------------------------------------------------------------- agent review → suggested pending comments

REVIEW_BLOCK = "waw-review"
_BLOCK_RE = re.compile(r"```" + REVIEW_BLOCK + r"[ \t]*\n(.*?)\n```", re.S)


def review_instructions() -> str:
    return (
        "This is a code review request. In the pull request above each diff line starts with its line numbers: old file, "
        "new file, then `|` and the diff line (deleted lines have only an old number, added lines only a new one). "
        "Review the pull request as a careful reviewer and write your line comments in a "
        f"fenced block tagged {REVIEW_BLOCK} at the end of your answer: a JSON array of objects "
        '{"path": "<file in the PR>", "side": "RIGHT" (new code; "LEFT" for a deleted line), "line": <the line number of that side '
        '(the numbers in front of the diff line)>, "start_line": <optional first line of a range>, "body": "<the comment, Markdown; a ```suggestion block may propose replacement code>"}. '
        "Only comment on lines that appear in the diff; be specific, one point per comment, and skip nitpicks unless asked. "
        "Before the block, give a short overall summary. The comments are shown to the user as suggestions in the diff; "
        "nothing is posted to GitHub unless the user applies them.\n"
        f"Example:\n```{REVIEW_BLOCK}\n" '[{"path": "src/a.cpp", "side": "RIGHT", "line": 42, "body": "This can overflow for n > 2^31."}]' "\n```"
    )


def numbered_diffs(copy_text: str) -> str:
    """The PR copy with old/new line numbers in front of every diff line (```diff blocks), so the agent's comments
    point at the right lines: `  OLD   NEW |<diff line>`."""
    out: list[str] = []
    in_diff = False
    old = new = 0
    for ln in copy_text.split("\n"):
        if ln.startswith("```diff"):
            in_diff, old, new = True, 0, 0
            out.append(ln)
            continue
        if in_diff and ln.startswith("```"):
            in_diff = False
            out.append(ln)
            continue
        if not in_diff:
            out.append(ln)
            continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", ln)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            out.append(f"{'':>5} {'':>5} |{ln}")
        elif ln.startswith(("--- ", "+++ ")) or (not old and not new):
            out.append(f"{'':>5} {'':>5} |{ln}")
        elif ln.startswith("+"):
            out.append(f"{'':>5} {new:>5} |{ln}")
            new += 1
        elif ln.startswith("-"):
            out.append(f"{old:>5} {'':>5} |{ln}")
            old += 1
        else:
            out.append(f"{old:>5} {new:>5} |{ln}")
            old += 1
            new += 1
    return "\n".join(out)


def parse_review_block(text: str) -> tuple[list[dict[str, Any]], str | None]:
    """The agent's comments, or ([], problem)."""
    blocks = _BLOCK_RE.findall(text or "")
    if not blocks:
        return [], f"the answer has no {REVIEW_BLOCK} block"
    try:
        items = json.loads(blocks[-1])
    except ValueError as exc:
        return [], f"the {REVIEW_BLOCK} block is not valid JSON ({exc})"
    if isinstance(items, dict):
        items = items.get("comments") or []
    if not isinstance(items, list):
        return [], f"the {REVIEW_BLOCK} block is not a list"
    return [x for x in items if isinstance(x, dict)], None


def diff_line_map(unified: str) -> dict[tuple[str, int], str]:
    """(side, line) → text, for every line of a unified diff that GitHub accepts a comment on."""
    out: dict[tuple[str, int], str] = {}
    old = new = 0
    for ln in (unified or "").split("\n"):
        if ln.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", ln)
            if m:
                old, new = int(m.group(1)), int(m.group(2))
            continue
        if ln.startswith(("--- ", "+++ ")) or (not old and not new):
            continue
        if ln.startswith("+"):
            out[("RIGHT", new)] = ln
            new += 1
        elif ln.startswith("-"):
            out[("LEFT", old)] = ln
            old += 1
        elif ln.startswith(" ") or ln == "":
            out[("RIGHT", new)] = ln
            out[("LEFT", old)] = ln
            old += 1
            new += 1
    return out


def agent_drafts(items: list[dict[str, Any]], files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Validated drafts for the agent's comments: on a diff line when possible, else on the file (line in the text)."""
    by_path = {f["path"]: f for f in files}
    out: list[dict[str, Any]] = []
    problems: list[str] = []
    for x in items[:100]:
        path = str(x.get("path") or "").strip().lstrip("./")
        body = str(x.get("body") or "").strip()
        if not body:
            continue
        f = by_path.get(path)
        if f is None:
            problems.append(f"{path or '?'}: not a file of this PR")
            continue
        side = "LEFT" if str(x.get("side") or "").upper() == "LEFT" else "RIGHT"
        try:
            line = int(x.get("line") or 0)
            start = int(x["start_line"]) if x.get("start_line") not in (None, "", 0) else None
        except (TypeError, ValueError):
            line, start = 0, None
        lines = diff_line_map(f.get("unified") or "")
        if line and (side, line) in lines:
            if start is not None and (start >= line or any((side, n) not in lines for n in range(start, line))):
                start = None
            text = "\n".join(lines[(side, n)] for n in range(start or line, line + 1))
            out.append({"path": path, "subject": "line", "side": side, "line": line, "start_side": side if start else None,
                        "start_line": start, "line_text": text, "body": body})
        else:
            where = f"Line {line}: " if line else ""
            out.append({"path": path, "subject": "file", "side": "RIGHT", "line": 0, "start_side": None, "start_line": None,
                        "line_text": "", "body": where + body})
    return out, problems


def _review_key(owner: str, repo: str, number: int) -> str:
    return f"agent_review:{owner}/{repo}#{number}".lower()


def register_agent_review(ctx: AppContext, doc_path: str, job_id: str, thread_id: str, message_id: str) -> None:
    """Called when an agent review starts (from the PR tab's discussion document .waw-github/<owner>/<repo>/<n>.md)."""
    parts = doc_path.split("/")
    owner, repo, number = parts[1], parts[2], int(parts[3].removesuffix(".md"))
    key = _review_key(owner, repo, number)
    runs = kv_get(ctx, key, [])
    runs.append({"job_id": job_id, "thread_id": thread_id, "message_id": message_id, "state": "running", "added": 0, "problem": None,
                 "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    kv_put(ctx, key, runs[-10:])


def import_agent_reviews(ctx: AppContext, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    """Turn finished agent reviews into proposed pending comments (once), and report their state."""
    from .db import new_id
    from .models import GithubPrDraft, Message

    key = _review_key(owner, repo, number)
    runs = kv_get(ctx, key, [])
    changed = False
    for r in runs:
        if r["state"] != "running":
            continue
        with ctx.db.read() as s:
            m = s.get(Message, r["message_id"])
            status, content = (m.status, m.content or "") if m else ("failed", "")
        if status in ("queued", "running", "streaming"):
            continue
        changed = True
        if status != "completed":
            r.update(state="failed", problem=f"the agent's turn ended: {status}")
            continue
        items, problem = parse_review_block(content)
        files: list[dict[str, Any]] = []
        if items:
            try:
                hit = _pr_cache.get(f"{owner}/{repo}#{number}".lower())
                data = hit[1] if hit else fetch_pr(owner, repo, number, _hooks(ctx)[0], ctx.extra.get("gh_pr_files", _gh_files))
                files = data["files"]
            except ApiError as exc:
                r.update(state="failed", problem=exc.message)
                continue
        drafts, bad = agent_drafts(items, files)
        with ctx.db.write() as s:
            for d in drafts:
                s.add(GithubPrDraft(id=new_id(), repo=f"{owner}/{repo}".lower(), number=number, origin="agent", decision="proposed",
                                    thread_id=r["thread_id"], **{**d, "line_text": d["line_text"][:4000]}))
            m = s.get(Message, r["message_id"])
            if m is not None and drafts:  # the JSON is for the app: the discussion shows what became of it
                note = f"*{len(drafts)} suggested comment(s) were added to the diff (Files) — choose Apply or Discard for each.*"
                m.content = _BLOCK_RE.sub(note, m.content or "")
        r.update(state="done", added=len(drafts), problem=problem or ("; ".join(bad[:5]) if bad else None))
    if changed:
        kv_put(ctx, key, runs)
    return runs



# ---------------------------------------------------------------------- local checkout of a repository

class LocalRepoIn(BaseModel):
    repo: str
    path: str
    update: bool = True  # bring the base branch up to date before reviewing (fetch; fast-forward when clean)
    remote: str | None = None  # where to fetch from (a remote name or URL); default: the checkout's GitHub remote


def _repo_arg(repo: str) -> str:
    parts = repo.strip().split("/")
    if len(parts) != 2:
        raise ApiError(400, "INVALID_REPO", "use owner/name")
    _check_names(*parts)
    return "/".join(parts)


@router.get("/github/local-repo")
def get_local_repo(repo: str = Query(...), auth: tuple[AppContext, str] = Depends(require_owner)):
    """The local git checkout connected to a repository (the agent's default reference for its PRs), if any."""
    from .local_repos import get_link

    ctx, _ = auth
    repo = _repo_arg(repo)
    return {"repo": repo, "link": get_link(ctx, repo)}


@router.put("/github/local-repo")
def put_local_repo(body: LocalRepoIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    from .local_repos import check_checkout, find_remote

    ctx, _ = auth
    repo = _repo_arg(body.repo)
    top = check_checkout(body.path.strip())
    remote = (body.remote or "").strip() or find_remote(top, repo)
    if remote and remote.startswith("-"):
        raise ApiError(400, "INVALID_REMOTE", "not a remote name or URL")
    link = {"path": top, "update": body.update, "remote": remote}
    kv_put(ctx, f"local_repo:{repo.lower()}", link)
    return {"repo": repo, "link": link,
            "note": None if remote else f"no remote of this checkout points at github.com/{repo}; PRs are fetched from https://github.com/{repo}.git"}


@router.delete("/github/local-repo")
def delete_local_repo(repo: str = Query(...), auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    ctx, _ = auth
    repo = _repo_arg(repo)
    kv_put(ctx, f"local_repo:{repo.lower()}", None)
    return {"repo": repo, "link": None}


@router.post("/github/pr/{owner}/{repo}/{number}/local-sync")
async def local_sync(owner: str, repo: str, number: int, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    """Fetch the PR into the connected checkout (in the background); poll with GET."""
    from .local_repos import start_sync

    ctx, _ = auth
    _check_names(owner, repo)
    pr = await asyncio.to_thread(_pr_node, ctx, owner, repo, number)
    return start_sync(ctx, f"{owner}/{repo}", number, pr.get("base"))


@router.get("/github/pr/{owner}/{repo}/{number}/local-sync")
def local_sync_state(owner: str, repo: str, number: int, auth: tuple[AppContext, str] = Depends(require_owner)):
    from .local_repos import get_link, sync_state

    ctx, _ = auth
    _check_names(owner, repo)
    if get_link(ctx, f"{owner}/{repo}") is None:
        return {"state": "none"}
    return sync_state(f"{owner}/{repo}", number) or {"state": "idle"}


class RepoSummaryIn(BaseModel):
    repo: str
    period: str  # day | week | month | quarter | year
    end: str | None = None  # last day of the window (YYYY-MM-DD); default today
    focus: str = ""


@router.post("/github/repo-summary", status_code=201)
async def repo_summary(body: RepoSummaryIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    """Summarize a repository's key changes over a day / week / month / quarter / year from the connected checkout's
    git log: starts a chat in the repository's wiki (to continue, distill into a page, …) with the log as material."""
    from dataclasses import replace

    from .annotations import ThreadCreateIn, ThreadService
    from .chats import new_chat
    from .local_repos import change_log, get_link, period_range, summary_brief

    ctx, _ = auth
    repo = _repo_arg(body.repo)
    period_range(body.period, body.end)  # validate before any git work
    link = get_link(ctx, repo)
    if link is None:
        raise ApiError(400, "NO_LOCAL_REPO", f"connect {repo} to a local git checkout first (⎇ in one of its PR tabs)")
    wiki = repo_wiki(ctx, repo)
    rt = ctx.workspaces.runtimes.get(wiki) if (wiki and ctx.workspaces is not None) else None
    if rt is None:
        raise ApiError(400, "NO_REPO_WIKI", f"choose the agent wiki for {repo} first (in one of its PR tabs)")
    log_ = await asyncio.to_thread(change_log, link, repo, body.period, body.end)
    title = f"{repo} changes · {log_['label']}"
    chat = new_chat(rt.docs, title)
    sctx = replace(ctx, documents=rt.docs, locks=rt.locks, watcher=rt.watcher, workspace_id=rt.id, workspace_root=rt.root)
    opened = rt.docs.open_document(chat["path"])
    question = (f"Summarize the key changes of {repo} in the {log_['label']} ({log_['commits']} commits): group them by area, "
                "lead with what matters most (new features and APIs, behaviour changes, performance, deprecations and removals, "
                "notable fixes, build / CI changes), cite PR numbers or commit hashes, and point out what touches topics of "
                "this wiki." + (f" Focus: {body.focus.strip()}" if body.focus.strip() else ""))
    r = await ThreadService(sctx).create_thread(chat["document_id"], ThreadCreateIn(
        version_sha256=opened["sha256"], question=question, whole_document=True, chat_brief=summary_brief(repo, log_, body.focus)))
    return {**chat, "thread_id": r["thread_id"], "job_id": r["job_id"], "commits": log_["commits"], "since": log_["since"], "until": log_["until"]}


# ---------------------------------------------------------------------- @-mention candidates

MENTION_QUERY = """
query Mentionable($owner: String!, $name: String!, $q: String!) {
  repository(owner: $owner, name: $name) { mentionableUsers(query: $q, first: 8) { nodes { login name } } }
}"""
_mention_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_participants: dict[str, list[dict[str, Any]]] = {}


_RANK = {"author": 0, "reviewer": 1, "requested": 2, "contributor": 3, "commenter": 4}


def _remember_participants(key: str, people: list[tuple[str | None, str]]) -> None:
    """Add people to a PR's participants; a person keeps their most relevant role (author before reviewer …)."""
    out = {p["login"].lower(): p for p in _participants.get(key, [])}
    for login, role in people:
        if not login or login.endswith("[bot]"):
            continue
        cur = out.get(login.lower())
        if cur is None:
            out[login.lower()] = {"login": login, "name": None, "source": role}
        elif _RANK.get(role, 9) < _RANK.get(cur["source"], 9):
            cur["source"] = role
    _participants[key] = list(out.values())


@router.get("/github/pr/{owner}/{repo}/{number}/mentions")
async def mention_candidates(owner: str, repo: str, number: int, q: str = Query("", max_length=40),
                             auth: tuple[AppContext, str] = Depends(require_owner)):
    """People to @-mention: the PR's participants first (author, reviewers, requested reviewers, commenters), then
    users GitHub says can be mentioned in the repository (searched by login / name)."""
    ctx, _ = auth
    _check_names(owner, repo)
    key = f"{owner}/{repo}#{number}".lower()
    q = q.strip().lstrip("@")
    if not re.fullmatch(r"[\w.-]*", q):
        return {"candidates": []}
    hit = _pr_cache.get(key)
    if hit:
        p = hit[1]["pr"]
        _remember_participants(key, [(p.get("author"), "author")] + [(r.get("author"), "reviewer") for r in p.get("reviews") or []]
                               + [(x, "requested") for x in p.get("requested") or []])
    ql = q.lower()
    local = sorted((c for c in _participants.get(key, []) if c["login"].lower().startswith(ql)), key=lambda c: _RANK.get(c["source"], 9))[:8]
    remote: list[dict[str, Any]] = []
    if q:
        mk = f"{owner}/{repo}:{ql}".lower()
        cached = _mention_cache.get(mk)
        if cached and time.time() - cached[0] < 600:
            remote = cached[1]
        else:
            try:
                d = await asyncio.to_thread(_hooks(ctx)[0], MENTION_QUERY, {"owner": owner, "name": repo, "q": q})
                remote = [{"login": n["login"], "name": n.get("name"), "source": "repository"}
                          for n in (((d.get("repository") or {}).get("mentionableUsers") or {}).get("nodes") or []) if n]
                _mention_cache[mk] = (time.time(), remote)
            except ApiError:
                remote = []
    seen = {c["login"].lower() for c in local}
    return {"candidates": local + [c for c in remote if c["login"].lower() not in seen][: max(0, 10 - len(local))]}


# ---------------------------------------------------------------------- reviewers

REVIEWER_QUERY = """
query ReviewerCandidates($owner: String!, $name: String!, $number: Int!, $q: String!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) { suggestedReviewers { isAuthor isCommenter reviewer { login name } } }
    assignableUsers(query: $q, first: 10) { nodes { login name } }
  }
}"""


@router.get("/github/pr/{owner}/{repo}/{number}/reviewer-candidates")
async def reviewer_candidates(owner: str, repo: str, number: int, q: str = Query("", max_length=40),
                              auth: tuple[AppContext, str] = Depends(require_owner)):
    """People to request a review from: GitHub's suggested reviewers (who changed these files) first, then the
    repository's users that can be assigned, matching `q`."""
    ctx, owner_login = auth
    _check_names(owner, repo)
    q = q.strip().lstrip("@")
    if not re.fullmatch(r"[\w.-]*", q):
        return {"candidates": []}
    d = await asyncio.to_thread(_hooks(ctx)[0], REVIEWER_QUERY, {"owner": owner, "name": repo, "number": number, "q": q})
    r = d.get("repository") or {}
    pr = (_pr_cache.get(f"{owner}/{repo}#{number}".lower()) or (0, {"pr": {}}))[1]["pr"]
    taken = {(x or "").lower() for x in pr.get("requested") or []} | {(pr.get("author") or "").lower()}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    ql = q.lower()
    for s in (r.get("pullRequest") or {}).get("suggestedReviewers") or []:
        u = (s or {}).get("reviewer") or {}
        login = u.get("login") or ""
        if login and login.lower() not in seen and (not ql or ql in login.lower() or ql in (u.get("name") or "").lower()):
            seen.add(login.lower())
            out.append({"login": login, "name": u.get("name"), "source": "suggested" + (" (commented)" if s.get("isCommenter") else ""),
                        "requested": login.lower() in taken})
    for u in (r.get("assignableUsers") or {}).get("nodes") or []:
        login = (u or {}).get("login") or ""
        if login and login.lower() not in seen:
            seen.add(login.lower())
            out.append({"login": login, "name": u.get("name"), "source": "repository", "requested": login.lower() in taken})
    return {"candidates": [c for c in out if c["login"].lower() != owner_login.lower()][:12]}


class ReviewersIn(BaseModel):
    add: list[str] = []
    remove: list[str] = []


@router.post("/github/pr/{owner}/{repo}/{number}/reviewers")
async def change_reviewers(owner: str, repo: str, number: int, body: ReviewersIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    """Request reviews from people (or cancel pending requests), on GitHub at once, as the server's gh account."""
    ctx, _ = auth
    _check_names(owner, repo)
    for x in body.add + body.remove:
        if not NAME.match(x):
            raise ApiError(400, "INVALID_LOGIN", f"{x!r} is not a GitHub login")
    if not body.add and not body.remove:
        raise ApiError(400, "INVALID_REVIEWERS", "nobody to add or remove")
    rest = _rest(ctx)
    path = f"repos/{owner}/{repo}/pulls/{number}/requested_reviewers"
    if body.add:
        await asyncio.to_thread(rest, "POST", path, {"reviewers": body.add})
    if body.remove:
        await asyncio.to_thread(rest, "DELETE", path, {"reviewers": body.remove})
    _pr_cache.pop(f"{owner}/{repo}#{number}".lower(), None)
    pr = await asyncio.to_thread(_pr_node, ctx, owner, repo, number)
    return {"requested": pr.get("requested") or [], "reviews": pr.get("reviews") or []}


# ---------------------------------------------------------------------- CI failures (logs for the agent)

FAILED = ("failure", "timed_out", "startup_failure", "action_required")
MAX_LOGS = 8
_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z ")
_ERR = re.compile(r"(##\[error\]|\berror\b|Error:|FAILED|FAIL:|Traceback|AssertionError|RuntimeError|Exception|exit code [1-9]|Segmentation fault|undefined reference|fatal:)", re.I)


def gh_job_log(owner: str, repo: str, job_id: int) -> str:
    """The raw log of a GitHub Actions job (gh follows the redirect to the log file)."""
    gh = shutil.which("gh")
    if not gh:
        raise ApiError(503, "GH_UNAVAILABLE", "the GitHub CLI (gh) is not installed on the server")
    RATE.before("core")
    try:
        p = subprocess.run([gh, "api", f"repos/{owner}/{repo}/actions/jobs/{job_id}/logs"], capture_output=True, timeout=180, check=False)
    except subprocess.TimeoutExpired:
        raise ApiError(504, "GH_TIMEOUT", "GitHub did not send the log in time")
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip()
        if "410" in err or "404" in err:
            raise ApiError(410, "LOG_GONE", "the log is no longer on GitHub (expired or deleted)")
        raise ApiError(502, "GH_ERROR", f"no log: {err[-300:]}")
    return p.stdout[:30_000_000].decode("utf-8", "replace")


def log_excerpt(text: str, max_chars: int = 6000) -> tuple[str, int]:
    """What the agent should read first (timestamps stripped); (excerpt, total lines). GitHub marks the failing
    step with `##[error]` lines: the excerpt is the output leading up to them; without such marks, the lines
    around error-looking lines plus the end of the log."""
    lines = [_TS.sub("", ln) for ln in text.splitlines()]
    marks = [i for i, ln in enumerate(lines) if ln.startswith("##[error]")]
    keep: set[int] = set()
    if marks:
        keep.update(range(max(0, marks[0] - 80), min(len(lines), marks[-1] + 2)))
    else:
        hits = [i for i, ln in enumerate(lines) if _ERR.search(ln)]
        for i in hits[-25:]:
            keep.update(range(max(0, i - 3), min(len(lines), i + 4)))
        keep.update(range(max(0, len(lines) - 40), len(lines)))
    out: list[str] = []
    prev = -2
    for i in sorted(keep):
        if i != prev + 1:
            out.append("…")
        out.append(lines[i][:400])
        prev = i
    text_out = "\n".join(out)
    return (text_out if len(text_out) <= max_chars else "…" + text_out[-max_chars:]), len(lines)


class CiIn(BaseModel):
    checks: list[str] | None = None  # only these check names (default: every failing one)


@router.post("/github/pr/{owner}/{repo}/{number}/ci-failures")
async def ci_failures(owner: str, repo: str, number: int, body: CiIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    """Download the logs of the PR's failing CI jobs (GitHub Actions) to the server for the agent, with excerpts
    around the errors; other failing checks (external CI) are listed with their links."""
    ctx, _ = auth
    _check_names(owner, repo)
    pr = await asyncio.to_thread(_pr_node, ctx, owner, repo, number)
    sha = pr.get("head_sha")
    if not sha:
        raise ApiError(409, "NO_HEAD", "the PR's head commit is unknown")
    rest = _rest(ctx)
    runs = await asyncio.to_thread(rest, "GET", f"repos/{owner}/{repo}/commits/{sha}/check-runs?filter=latest&per_page=100")
    pages = runs if isinstance(runs, list) else [runs]
    check_runs = [c for p in pages if isinstance(p, dict) for c in p.get("check_runs") or []]  # one {check_runs: […]} per page
    failed = [c for c in check_runs if (c.get("conclusion") or "") in FAILED]
    wanted = {x.lower() for x in body.checks} if body.checks else None
    if wanted is not None:
        failed = [c for c in failed if (c.get("name") or "").lower() in wanted]
    external = [c for c in pr.get("checks") or [] if c.get("state") in ("FAILURE", "ERROR") and not any(c.get("name") == f.get("name") for f in check_runs)]
    if wanted is not None:
        external = [c for c in external if (c.get("name") or "").lower() in wanted]
    snap = ctx.settings.snapshots_dir / "github" / owner.lower() / repo.lower() / f"{number}.ci"
    snap.mkdir(parents=True, exist_ok=True)
    fetch = ctx.extra.get("gh_job_log", gh_job_log)
    out: list[dict[str, Any]] = []
    for c in failed[:MAX_LOGS]:
        item = {"name": c.get("name"), "conclusion": c.get("conclusion"), "url": c.get("html_url") or c.get("details_url"),
                "app": ((c.get("app") or {}).get("slug")), "log_path": None, "lines": 0, "excerpt": None, "error": None}
        if item["app"] in (None, "github-actions"):
            try:
                text = await asyncio.to_thread(fetch, owner, repo, int(c["id"]))
                f = snap / f"{c['id']}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', c.get('name') or 'job')[:80]}.log"
                f.write_text(text, encoding="utf-8")
                item["log_path"] = str(f)
                item["excerpt"], item["lines"] = log_excerpt(text)
            except ApiError as exc:
                item["error"] = exc.message
        else:
            item["error"] = f"runs on {item['app']}: open the link for its log"
        out.append(item)
    out += [{"name": c.get("name"), "conclusion": c.get("state", "").lower(), "url": c.get("url"), "app": "status", "log_path": None,
             "lines": 0, "excerpt": None, "error": "an external status: open the link for its log"} for c in external]
    return {"head_sha": sha, "failures": out, "total_failed": len(failed) + len(external), "logs_dir": str(snap)}



# ---------------------------------------------------------------------- search, PR state, issues

@router.get("/github/search")
async def search_prs(q: str = Query(..., min_length=1, max_length=300), state: str = Query("open"),
                     auth: tuple[AppContext, str] = Depends(require_owner)):
    """Search pull requests on GitHub (GitHub's search syntax: words, repo:owner/name, author:, label:, …).
    `owner/repo#123` or a PR link finds that PR. state: open (default; merged and closed PRs are left out),
    closed (closed or merged) or all."""
    ctx, owner = auth
    if state not in ("open", "closed", "all"):
        raise ApiError(400, "INVALID_STATE", "state must be open, closed or all")
    text = q.strip()
    m = re.fullmatch(r"(?:https://github\.com/)?([\w.-]+)/([\w.-]+)(?:#|/pull/)(\d+)/?", text)
    query = f"is:pr repo:{m.group(1)}/{m.group(2)} {m.group(3)}" if m else ("" if re.search(r"\bis:(pr|issue)\b", text) else "is:pr ") + text
    if not re.search(r"\b(is|state):(open|closed|merged|unmerged)\b", text):
        query += {"open": " is:open", "closed": " is:closed", "all": ""}[state]
    d = await asyncio.to_thread(_hooks(ctx)[0], REVIEW_QUERY, {"q": query + " archived:false", "after": None, "n": 30, "me": owner})
    s = d.get("search") or {}
    items = [normalize(n, owner) for n in s.get("nodes") or [] if n and n.get("number")]
    if m:
        items = [x for x in items if x["number"] == int(m.group(3))] or items
    prefs = load_prefs(ctx)
    if state == "open":  # the search index lags behind merges: never show a merged / closed PR here
        items = [x for x in items if x["state"] == "OPEN"]
    elif state == "closed":
        items = [x for x in items if x["state"] != "OPEN"]
    return {"query": query, "total": s.get("issueCount", 0), "items": [{**x, "priority": is_priority(x, prefs)} for x in items]}


M_READY = "mutation MarkReady($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) { pullRequest { isDraft } } }"
M_DRAFT = "mutation ToDraft($id: ID!) { convertPullRequestToDraft(input: {pullRequestId: $id}) { pullRequest { isDraft } } }"


class PrStateIn(BaseModel):
    action: str  # close | reopen | ready | draft


@router.post("/github/pr/{owner}/{repo}/{number}/state")
async def pr_state(owner: str, repo: str, number: int, body: PrStateIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    """Close / reopen a pull request, mark it ready for review or convert it to a draft — on GitHub at once."""
    ctx, owner_login = auth
    _check_names(owner, repo)
    if body.action not in ("close", "reopen", "ready", "draft"):
        raise ApiError(400, "INVALID_ACTION", "action must be close, reopen, ready or draft")
    if body.action in ("close", "reopen"):
        await asyncio.to_thread(_rest(ctx), "PATCH", f"repos/{owner}/{repo}/pulls/{number}", {"state": "closed" if body.action == "close" else "open"})
    else:
        pr = await asyncio.to_thread(_pr_node, ctx, owner, repo, number)
        q, key = (M_READY, "markPullRequestReadyForReview") if body.action == "ready" else (M_DRAFT, "convertPullRequestToDraft")
        await asyncio.to_thread(_mutate, _hooks(ctx)[0], q, {"id": pr["node_id"]}, key)
    _pr_cache.pop(f"{owner}/{repo}#{number}".lower(), None)
    _cache.pop(owner_login, None)
    pr = await asyncio.to_thread(_pr_node, ctx, owner, repo, number)
    return {"state": pr.get("state"), "draft": pr.get("draft")}


ISSUE_QUERY = """
query Issue($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { nameWithOwner issue(number: $number) {
    number title url body state stateReason createdAt updatedAt author { login }
    labels(first: 20) { nodes { name color } } assignees(first: 10) { nodes { login } }
    comments(last: 100) { totalCount nodes { databaseId author { login } body createdAt url } }
  } }
}"""


@router.get("/github/issue/{owner}/{repo}/{number}")
async def get_issue(owner: str, repo: str, number: int, auth: tuple[AppContext, str] = Depends(require_owner)):
    """An issue (for links like #123 that are not pull requests): details and comments."""
    ctx, _ = auth
    _check_names(owner, repo)
    d = await asyncio.to_thread(_hooks(ctx)[0], ISSUE_QUERY, {"owner": owner, "name": repo, "number": number})
    r = d.get("repository") or {}
    i = r.get("issue")
    if not i:
        raise ApiError(404, "ISSUE_NOT_FOUND", f"{owner}/{repo}#{number} is neither a pull request nor an issue visible to the server's gh account")
    comments = (i.get("comments") or {}).get("nodes") or []
    return {"repo": r.get("nameWithOwner"), "number": i["number"], "title": i["title"], "url": i["url"], "body": i.get("body") or "",
            "state": i.get("state"), "state_reason": i.get("stateReason"), "author": (i.get("author") or {}).get("login"),
            "created_at": i.get("createdAt"), "labels": [{"name": x["name"], "color": x.get("color")} for x in (i.get("labels") or {}).get("nodes") or []],
            "assignees": [x["login"] for x in (i.get("assignees") or {}).get("nodes") or [] if x],
            "comments": [{"id": c.get("databaseId"), "author": (c.get("author") or {}).get("login"), "body": c.get("body") or "",
                          "created_at": c.get("createdAt"), "url": c.get("url")} for c in comments if c],
            "older_hidden": max(0, (i.get("comments") or {}).get("totalCount", 0) - len(comments))}


class IssueStateIn(BaseModel):
    action: str  # close | reopen
    reason: str = "completed"  # completed | not_planned (for close)


@router.post("/github/issue/{owner}/{repo}/{number}/state")
async def issue_state(owner: str, repo: str, number: int, body: IssueStateIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    ctx, _ = auth
    _check_names(owner, repo)
    if body.action not in ("close", "reopen") or body.reason not in ("completed", "not_planned"):
        raise ApiError(400, "INVALID_ACTION", "action must be close or reopen; reason completed or not_planned")
    patch = {"state": "closed", "state_reason": body.reason} if body.action == "close" else {"state": "open"}
    res = await asyncio.to_thread(_rest(ctx), "PATCH", f"repos/{owner}/{repo}/issues/{number}", patch)
    return {"state": ((res or {}).get("state") or patch["state"]).upper(), "state_reason": (res or {}).get("state_reason")}


class IssueCommentIn(BaseModel):
    body: str


@router.post("/github/issue/{owner}/{repo}/{number}/comments", status_code=201)
async def issue_comment(owner: str, repo: str, number: int, body: IssueCommentIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    ctx, _ = auth
    _check_names(owner, repo)
    if not body.body.strip():
        raise ApiError(400, "INVALID_COMMENT", "a comment needs text")
    res = await asyncio.to_thread(_rest(ctx), "POST", f"repos/{owner}/{repo}/issues/{number}/comments", {"body": body.body.strip()})
    return {"id": (res or {}).get("id"), "body": body.body.strip()}


# ---------------------------------------------------------------------- triggering CI

CIFLOW = re.compile(r"^ciflow/[\w./-]+$")


@router.get("/github/pr/{owner}/{repo}/{number}/workflows")
async def pr_workflows(owner: str, repo: str, number: int, auth: tuple[AppContext, str] = Depends(require_owner)):
    """GitHub Actions runs of the PR's head commit (to re-run / cancel), the repository's workflows that can be
    started by hand (workflow_dispatch), and the `ciflow/*` labels the repository uses to trigger CI (PyTorch)."""
    ctx, _ = auth
    _check_names(owner, repo)
    pr = await asyncio.to_thread(_pr_node, ctx, owner, repo, number)
    rest = _rest(ctx)
    sha = pr.get("head_sha")
    runs_raw = await asyncio.to_thread(rest, "GET", f"repos/{owner}/{repo}/actions/runs?head_sha={sha}&per_page=100") if sha else []
    pages = runs_raw if isinstance(runs_raw, list) else [runs_raw]
    runs = [r for p in pages if isinstance(p, dict) for r in p.get("workflow_runs") or []]
    latest: dict[str, dict[str, Any]] = {}
    for r in runs:  # the newest attempt per workflow
        k = r.get("name") or str(r.get("workflow_id"))
        if k not in latest or (r.get("run_number", 0), r.get("run_attempt", 0)) > (latest[k].get("run_number", 0), latest[k].get("run_attempt", 0)):
            latest[k] = r
    try:
        wf_raw = await asyncio.to_thread(rest, "GET", f"repos/{owner}/{repo}/actions/workflows?per_page=100")
        wpages = wf_raw if isinstance(wf_raw, list) else [wf_raw]
        workflows = [{"id": w["id"], "name": w.get("name"), "path": w.get("path")} for p in wpages if isinstance(p, dict)
                     for w in p.get("workflows") or [] if w.get("state") == "active"]
    except ApiError:
        workflows = []
    try:
        lab_raw = await asyncio.to_thread(rest, "GET", f"repos/{owner}/{repo}/labels?per_page=100")
        ciflow = sorted({lb["name"] for lb in (lab_raw if isinstance(lab_raw, list) else []) if isinstance(lb, dict) and CIFLOW.match(lb.get("name", ""))})
    except ApiError:
        ciflow = []
    on = [lb["name"] for lb in pr.get("labels") or [] if CIFLOW.match(lb.get("name", ""))]
    return {"head_sha": sha, "head": pr.get("head"), "runs": [
        {"id": r["id"], "name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion"), "url": r.get("html_url"),
         "attempt": r.get("run_attempt"), "event": r.get("event")} for r in sorted(latest.values(), key=lambda r: r.get("name") or "")],
        "workflows": workflows, "ciflow_labels": ciflow, "ciflow_on": on}


class CiTriggerIn(BaseModel):
    action: str  # rerun_failed | rerun | cancel | dispatch | label | unlabel
    run_id: int | None = None
    workflow_id: int | None = None
    ref: str | None = None
    label: str | None = None


@router.post("/github/pr/{owner}/{repo}/{number}/ci")
async def trigger_ci(owner: str, repo: str, number: int, body: CiTriggerIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    """Trigger CI on GitHub: re-run a run's failed jobs or the whole run, cancel a run, start a workflow by hand,
    or add / remove a `ciflow/*` label (PyTorch starts the matching workflows when such a label is added)."""
    ctx, _ = auth
    _check_names(owner, repo)
    rest = _rest(ctx)
    base = f"repos/{owner}/{repo}"
    a = body.action
    if a in ("rerun_failed", "rerun", "cancel"):
        if not body.run_id:
            raise ApiError(400, "INVALID_RUN", "run_id is required")
        path = {"rerun_failed": "rerun-failed-jobs", "rerun": "rerun", "cancel": "cancel"}[a]
        await asyncio.to_thread(rest, "POST", f"{base}/actions/runs/{body.run_id}/{path}", {})
    elif a == "dispatch":
        if not body.workflow_id:
            raise ApiError(400, "INVALID_WORKFLOW", "workflow_id is required")
        pr = await asyncio.to_thread(_pr_node, ctx, owner, repo, number)
        ref = (body.ref or pr.get("head") or "").strip()
        if not ref or not re.fullmatch(r"[\w./-]+", ref):
            raise ApiError(400, "INVALID_REF", "a branch name is required")
        try:
            await asyncio.to_thread(rest, "POST", f"{base}/actions/workflows/{body.workflow_id}/dispatches", {"ref": ref})
        except ApiError as exc:
            raise ApiError(exc.status, exc.code, f"{exc.message} (only workflows with a workflow_dispatch trigger can be started by hand, "
                           f"on a branch of {owner}/{repo} — a PR from a fork has no such branch)")
    elif a in ("label", "unlabel"):
        lb = (body.label or "").strip()
        if not CIFLOW.match(lb):
            raise ApiError(400, "INVALID_LABEL", "only ciflow/* labels")
        if a == "label":
            await asyncio.to_thread(rest, "POST", f"{base}/issues/{number}/labels", {"labels": [lb]})
        else:
            from urllib.parse import quote

            await asyncio.to_thread(rest, "DELETE", f"{base}/issues/{number}/labels/{quote(lb, safe='')}")
    else:
        raise ApiError(400, "INVALID_ACTION", "action must be rerun_failed, rerun, cancel, dispatch, label or unlabel")
    _pr_cache.pop(f"{owner}/{repo}#{number}".lower(), None)
    return {"ok": True, "action": a}


# ---------------------------------------------------------------------- whole file content (expand context in diffs)

_content_cache: dict[tuple[str, str, str], str] = {}
MAX_EXPAND_BYTES = 5 * 1024 * 1024


def _file_at(ctx: AppContext, repo: str, number: int, sha: str, path: str) -> str:
    """The file at the PR's head commit: from the connected local checkout when it has the PR fetched, else GitHub."""
    key = (repo.lower(), sha, path)
    if key in _content_cache:
        return _content_cache[key]
    text: str | None = None
    from .local_repos import get_link, sync_state

    link = get_link(ctx, repo)
    st = sync_state(repo, number) or {}
    if link and st.get("state") == "done" and st.get("sha") == sha:
        try:
            p = subprocess.run(["git", "-C", link["path"], "show", f"{sha}:{path}"], capture_output=True, timeout=60, check=False)
            if p.returncode == 0 and b"\0" not in p.stdout[:8000] and len(p.stdout) <= MAX_EXPAND_BYTES:
                text = p.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired):
            text = None
    if text is None:
        import base64
        from urllib.parse import quote

        rest = _rest(ctx)
        meta = rest("GET", f"repos/{repo}/contents/{quote(path)}?ref={sha}")
        if isinstance(meta, list):
            raise ApiError(400, "NOT_A_FILE", f"{path} is a folder")
        if not isinstance(meta, dict):
            raise ApiError(404, "FILE_NOT_FOUND", f"{path} not found at {sha[:12]}")
        if (meta.get("size") or 0) > MAX_EXPAND_BYTES:
            raise ApiError(413, "FILE_TOO_LARGE", f"{path} is too large to expand here")
        raw = meta.get("content") or ""
        if not raw and meta.get("sha"):  # > 1 MB: the contents API leaves it out; the blob has it
            blob = rest("GET", f"repos/{repo}/git/blobs/{meta['sha']}")
            raw = (blob or {}).get("content") or ""
        data = base64.b64decode(raw)
        if b"\0" in data[:8000]:
            raise ApiError(415, "BINARY_FILE", f"{path} is binary")
        text = data.decode("utf-8", "replace")
    if len(_content_cache) > 200:
        _content_cache.clear()
    _content_cache[key] = text
    return text


@router.get("/github/pr/{owner}/{repo}/{number}/file")
async def pr_file_content(owner: str, repo: str, number: int, path: str = Query(..., min_length=1, max_length=4096),
                          auth: tuple[AppContext, str] = Depends(require_owner)):
    """A changed file's content at the PR's head, so the diff can show more context lines (as on GitHub)."""
    ctx, _ = auth
    _check_names(owner, repo)
    if path.startswith("/") or ".." in path.split("/"):
        raise ApiError(400, "INVALID_PATH", "invalid path")
    hit = _pr_cache.get(f"{owner}/{repo}#{number}".lower())
    data = hit[1] if hit else await asyncio.to_thread(fetch_pr, owner, repo, number, _hooks(ctx)[0], ctx.extra.get("gh_pr_files", _gh_files))
    sha = data["pr"].get("head_sha")
    if not sha:
        raise ApiError(409, "NO_HEAD", "the PR's head commit is unknown")
    if not any(f["path"] == path for f in data["files"]):
        raise ApiError(404, "FILE_NOT_IN_PR", f"{path} is not a file of this pull request")
    text = await asyncio.to_thread(_file_at, ctx, f"{owner}/{repo}", number, sha, path)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return {"path": path, "sha": sha, "lines": lines}
