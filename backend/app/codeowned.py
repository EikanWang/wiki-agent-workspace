"""Open PRs that touch files you own in CODEOWNERS — listed in Reviews even when GitHub did not request your review.

GitHub requests code owners only in some cases: never while a PR is a draft, and not the person who marks it ready
or who already reviewed it. So a PR touching your files can be open for weeks without ever reaching
`review-requested:@me`. Here every open PR of a repository is checked against its CODEOWNERS (the default branch's
copy, which GitHub uses for PRs into it), the way GitHub does it: the *last* matching rule decides a file's owners.

Which repositories: those of your review list (requests and watched repositories) whose CODEOWNERS names you
(`maintainers` already reads it once a day). Teams (`@org/team`) are not expanded: that needs org membership lookups
the server's gh login cannot make.

Scanning is incremental, in the background and at background priority. The first scan of a repository walks all its
open PRs, newest update first (pytorch/pytorch: ~4000 PRs, a few minutes). Later scans stop at the first PR not
updated since the previous scan started, so they usually take one request. The progress is saved after every page,
so a scan stopped by the rate limit or a restart goes on where it stopped. A full rescan runs when CODEOWNERS
changes, when the gh login changes, and once a week (it also catches PRs a moving list skipped).

State per repository in the kv table, key `codeowned:<owner>/<name>`; `codeowned_repos` lists them.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
import time
from typing import Any, Callable

from .errors import ApiError
from .kv import kv_get, kv_put

log = logging.getLogger("waw.codeowned")

PREFIX = "codeowned:"
INDEX = "codeowned_repos"
CODEOWNERS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")  # the places GitHub looks, in its order
SCAN_SECONDS = 300          # an incremental scan at most this often per repository
FULL_SECONDS = 7 * 86400    # a full rescan at least this often
MARGIN_SECONDS = 120        # incremental scans look this much further back (clock skew, slow index)
PAGE_SIZE = 40
MAX_PATHS = 20              # owned paths kept per PR (the count is kept too)
MAX_FILE_PAGES = 30         # GitHub lists at most 3000 files of a PR

# ---------------------------------------------------------------------- CODEOWNERS rules


def _pattern_regex(pat: str) -> re.Pattern[str] | None:
    """A CODEOWNERS path pattern as a regex over repository paths (no leading slash).

    Like .gitignore, with GitHub's differences: no `!` negation and no `[]` ranges. A pattern with a slash at the
    start or in the middle is anchored to the top folder, otherwise it matches at any depth; a trailing slash (or a
    plain name) owns everything below; `*` does not cross `/` (so `docs/*` owns docs/a.md but not docs/x/a.md);
    `**` crosses folders.
    """
    pat = pat.replace("\\#", "#").replace("\\ ", " ")
    if not pat or pat == "/":
        return None
    anchored = pat.startswith("/") or "/" in pat.rstrip("/")
    dir_only = pat.endswith("/")
    parts = pat.strip("/").split("/")
    rx = ""
    for i, seg in enumerate(parts):
        last = i == len(parts) - 1
        if seg == "**":
            rx += ".*" if last else "(?:[^/]+/)*"
            continue
        rx += "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in seg)
        if not last:
            rx += "/"
    tail = parts[-1]
    if dir_only:
        rx += "/.*"
    elif tail != "**" and not any(c in tail for c in "*?"):
        rx += "(?:/.*)?"  # a file, or a folder and all inside it
    return re.compile(("" if anchored else "(?:.*/)?") + rx)


def parse_rules(text: str) -> list[tuple[str, re.Pattern[str], set[str]]]:
    """[(pattern, regex, owners in lower case)] in file order. A rule without owners takes ownership away."""
    rules = []
    for line in text.splitlines():
        toks = line.split()
        if not toks or toks[0].startswith("#"):
            continue
        owners: list[str] = []
        for t in toks[1:]:
            if t.startswith("#"):  # an inline comment
                break
            owners.append(t.lower())
        rx = _pattern_regex(toks[0])
        if rx is not None:
            rules.append((toks[0], rx, set(owners)))
    return rules


def owners_of(rules: list[tuple[str, re.Pattern[str], set[str]]], path: str) -> set[str]:
    """The owners of a file: those of the last matching rule (GitHub's precedence)."""
    for _, rx, owners in reversed(rules):
        if rx.fullmatch(path):
            return owners
    return set()


def names(rules: list[tuple[str, re.Pattern[str], set[str]]], login: str) -> bool:
    me = f"@{login.lower()}"
    return any(me in owners for _, _, owners in rules)


def owned_paths(rules: list[tuple[str, re.Pattern[str], set[str]]], paths: list[str], login: str) -> list[str]:
    me = f"@{login.lower()}"
    return [p for p in paths if me in owners_of(rules, p)]


# ---------------------------------------------------------------------- GitHub queries

CODEOWNERS_QUERY = """
query Codeowners($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    a: object(expression: "HEAD:.github/CODEOWNERS") { ... on Blob { oid text isTruncated } }
    b: object(expression: "HEAD:CODEOWNERS") { ... on Blob { oid text isTruncated } }
    c: object(expression: "HEAD:docs/CODEOWNERS") { ... on Blob { oid text isTruncated } }
  }
}
"""

SCAN_QUERY = """
query CodeownedScan($owner: String!, $name: String!, $n: Int!, $after: String) {
  rateLimit { remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: $n, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number updatedAt isDraft author { login }
        files(first: 100) { pageInfo { hasNextPage endCursor } nodes { path } }
      }
    }
  }
}
"""

FILES_QUERY = """
query CodeownedFiles($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) { files(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { path } } }
  }
}
"""


def fetch_codeowners(graphql: Callable[[str, dict], dict], repo: str) -> dict[str, Any] | None:
    """{"path", "oid", "text"} of the CODEOWNERS GitHub uses (the first of its three places), or None."""
    owner, name = repo.split("/")
    r = (graphql(CODEOWNERS_QUERY, {"owner": owner, "name": name}) or {}).get("repository") or {}
    for key, path in zip("abc", CODEOWNERS, strict=True):
        blob = r.get(key)
        if blob and blob.get("text") is not None:
            return {"path": path, "oid": blob.get("oid") or "", "text": blob["text"], "truncated": bool(blob.get("isTruncated"))}
    return None


def _all_files(graphql: Callable[[str, dict], dict], repo: str, node: dict[str, Any]) -> list[str]:
    files = node.get("files") or {}
    paths = [f["path"] for f in files.get("nodes") or [] if f and f.get("path")]
    info, pages = files.get("pageInfo") or {}, 1
    owner, name = repo.split("/")
    while info.get("hasNextPage") and pages < MAX_FILE_PAGES:
        d = graphql(FILES_QUERY, {"owner": owner, "name": name, "number": node["number"], "after": info.get("endCursor")})
        files = (((d.get("repository") or {}).get("pullRequest") or {}).get("files")) or {}
        paths += [f["path"] for f in files.get("nodes") or [] if f and f.get("path")]
        info, pages = files.get("pageInfo") or {}, pages + 1
    return paths


# ---------------------------------------------------------------------- state


def _key(repo: str) -> str:
    return PREFIX + repo.lower()


def load(ctx: Any, repo: str) -> dict[str, Any]:
    st = kv_get(ctx, _key(repo), {}) or {}
    st.setdefault("prs", {})
    return st


def _save(ctx: Any, repo: str, st: dict[str, Any]) -> None:
    kv_put(ctx, _key(repo), st)
    index = kv_get(ctx, INDEX, []) or []
    if repo.lower() not in [r.lower() for r in index]:
        kv_put(ctx, INDEX, [*index, repo])


def scanned_repos(ctx: Any) -> list[str]:
    """The repositories scanned so far (their matches stay listed while they are not in the review list)."""
    return list(kv_get(ctx, INDEX, []) or [])


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def owned(ctx: Any, repos: list[str]) -> dict[str, dict[str, dict[str, Any]]]:
    """{repo: {"123": {"paths", "count", "updated_at"}}} — the matches found so far."""
    return {r: load(ctx, r)["prs"] for r in repos}


def forget(ctx: Any, repo: str, numbers: list[int]) -> None:
    """PRs found closed / merged meanwhile: no longer listed."""
    if not numbers:
        return
    st = load(ctx, repo)
    for n in numbers:
        st["prs"].pop(str(n), None)
    _save(ctx, repo, st)


# ---------------------------------------------------------------------- scanning


def scan(ctx: Any, graphql: Callable[[str, dict], dict], login: str, repo: str, now: float | None = None) -> bool:
    """Bring a repository's matches up to date; True when the set of matched PRs changed."""
    now = time.time() if now is None else now
    st = load(ctx, repo)
    before = set(st["prs"])
    st["checked"] = now
    co = fetch_codeowners(graphql, repo)
    if co is None or co.get("truncated"):
        st.update(prs={}, resume=None, error="no CODEOWNERS on the default branch" if co is None else "CODEOWNERS is too large to read",
                  codeowners=None)
        _save(ctx, repo, st)
        return bool(before)
    rules = parse_rules(co["text"])
    if not names(rules, login):
        st.update(prs={}, resume=None, error=None, codeowners={"path": co["path"], "oid": co["oid"]}, mine=0)
        _save(ctx, repo, st)
        return bool(before)
    me = login.lower()
    full = (st.get("login") != me or (st.get("codeowners") or {}).get("oid") != co["oid"] or not st.get("since")
            or now - (st.get("full_at") or 0) > FULL_SECONDS)
    resume = st.get("resume") or {}
    if resume and resume.get("oid") == co["oid"] and resume.get("login") == me and bool(resume.get("full")) == full:
        started, after = resume["started"], resume.get("after")  # go on where the last scan stopped
    else:
        started, after = _iso(now), None
        resume = {"started": started, "oid": co["oid"], "login": me, "full": full, "after": None, "seen": 0}
    since = None if full else _iso(_epoch(st["since"]) - MARGIN_SECONDS)
    st.update(login=me, codeowners={"path": co["path"], "oid": co["oid"]}, error=None,
              mine=sum(1 for _, _, o in rules if f"@{me}" in o))
    owner, name = repo.split("/")
    size = PAGE_SIZE
    while True:
        try:
            d = graphql(SCAN_QUERY, {"owner": owner, "name": name, "n": size, "after": after})
        except ApiError as exc:
            if exc.code == "GH_BUSY" and size > 10:
                size //= 2  # GitHub timed out building the page: ask for less
                continue
            st["resume"] = resume
            st["error"] = str(exc.message)[:200]
            _save(ctx, repo, st)
            raise
        conn = ((d.get("repository") or {}).get("pullRequests")) or {}
        done = False
        for node in conn.get("nodes") or []:
            if not node or not node.get("number"):
                continue
            if since and (node.get("updatedAt") or "") < since:
                done = True  # older than the last scan: nothing changed further down
                break
            k = str(node["number"])
            mine = owned_paths(rules, _all_files(graphql, repo, node), me) if ((node.get("author") or {}).get("login") or "").lower() != me else []
            if mine:
                st["prs"][k] = {"paths": mine[:MAX_PATHS], "count": len(mine), "updated_at": node.get("updatedAt"), "draft": bool(node.get("isDraft"))}
            else:
                st["prs"].pop(k, None)
            resume["seen"] = resume.get("seen", 0) + 1
        info = conn.get("pageInfo") or {}
        if done or not info.get("hasNextPage"):
            break
        after = resume["after"] = info.get("endCursor")
        st["resume"] = resume
        _save(ctx, repo, st)  # progress survives a rate limit stop or a restart
    st.update(since=started, resume=None, scanned=resume.get("seen", 0), scanned_at=now)
    if full:
        st["full_at"] = now
    _save(ctx, repo, st)
    log.info("code-owned PRs of %s for %s: %d (%s scan, %d PRs checked)", repo, login, len(st["prs"]), "full" if full else "incremental",
             resume.get("seen", 0))
    return set(st["prs"]) != before


def _epoch(iso: str) -> float:
    return float(calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")))


_running: dict[str, asyncio.Task] = {}


def enabled(ctx: Any) -> bool:
    """Scans need the real GitHub, or a test that asks for them."""
    return "gh_graphql" not in ctx.extra or bool(ctx.extra.get("codeowned_scan"))


def candidates(ctx: Any, login: str, repos: set[str], hidden: set[str]) -> list[str]:
    """The repositories whose CODEOWNERS names you (from the daily maintainers lists) — not hidden ones."""
    from . import maintainers

    cache = maintainers._all(ctx)
    out = []
    for r in sorted(repos):
        if r.lower() in hidden:
            continue
        p = ((cache.get(r.lower()) or {}).get("people") or {}).get(login.lower()) or {}
        if any(x.get("role") == "codeowner" for x in p.get("roles") or []):
            out.append(r)
    return out


def scan_soon(ctx: Any, graphql: Callable[[str, dict], dict], login: str, repos: list[str],
              on_change: Callable[[], None] | None = None) -> None:
    """Scan the repositories not scanned for SCAN_SECONDS, one after the other, in the background; on_change() when
    a repository's matched PRs changed (the review list then refreshes)."""
    if not enabled(ctx) or not repos:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    key = login.lower()
    t = _running.get(key)
    if t is not None and not t.done():
        return
    due = [r for r in repos if time.time() - (load(ctx, r).get("checked") or 0) >= SCAN_SECONDS]
    if not due:
        return

    async def run() -> None:
        from . import gh_limits

        changed = False
        for repo in due:
            try:
                changed |= await asyncio.to_thread(gh_limits.in_priority, gh_limits.BACKGROUND, False, "codeowned", scan, ctx, graphql, login, repo)
            except Exception as exc:  # noqa: BLE001 - saved with the state; tried again after SCAN_SECONDS
                log.info("code-owned PRs of %s: %s", repo, getattr(exc, "message", exc))
        if changed and on_change is not None:
            on_change()

    _running[key] = loop.create_task(run())


def status(ctx: Any, repos: list[str]) -> list[dict[str, Any]]:
    out = []
    for r in repos:
        st = load(ctx, r)
        out.append({"repo": r, "prs": len(st["prs"]), "rules": st.get("mine"), "source": (st.get("codeowners") or {}).get("path"),
                    "scanned_at": st.get("scanned_at"), "scanned": st.get("scanned"), "error": st.get("error"),
                    "in_progress": bool(st.get("resume")) or (not st.get("since") and not st.get("error")),
                    "running": any(not t.done() for t in _running.values())})
    return out


def reset() -> None:
    _running.clear()
