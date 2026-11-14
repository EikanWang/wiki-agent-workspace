"""Issues and pull requests active in a period, from GitHub (the server's gh login, rate-limit aware), kept as local
copies: items/<owner>/<repo>/<n>.json and items/<owner>/<repo>/days/<YYYY-MM-DD>.json (the numbers active that day,
written once the day is over and fetched) — a past day already fetched is never fetched again.

Active on a day: created that day, or a comment / review / review comment by a person (not a bot) that day.
Left out: titles starting with "DISABLED", the "Performance Dashboard" issue."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from . import settings as st

log = logging.getLogger("waw.insights")

IGNORED = {"pytorchmergebot", "pytorch-bot[bot]", "facebook-github-bot", "linux-foundation-easycla[bot]", "linux-foundation-easycla",
           "pytorch-bot"}
VENDORS: dict[str, dict[str, list[str]]] = {
    "Intel": {"logins": ["EikanWang", "etaf", "xytintel", "chuanqi129", "ZhiweiYan-96", "guangyey", "liangan1", "ZhaoqiongZ",
                         "zhangxiaoli73", "dvrogozh"], "company": ["intel", "habana"]},
    "NVIDIA": {"logins": [], "company": ["nvidia"]},
    "AMD": {"logins": [], "company": ["amd"]},
    "META": {"logins": [], "company": ["meta", "facebook"]},
    "Google": {"logins": [], "company": ["google"]},
}
INTEL_LOGINS = VENDORS["Intel"]["logins"]

_USER = "login ... on User { company }"
_COMMENTS = f"comments(last: 50) {{ nodes {{ createdAt body author {{ {_USER} }} }} }}"
_COMMON = (f"number title url body state createdAt updatedAt author {{ {_USER} }} labels(first: 20) {{ nodes {{ name }} }} "
           f"assignees(first: 10) {{ nodes {{ login }} }} {_COMMENTS}")
QUERY = f"""
query InsightsItems($q: String!, $after: String) {{
  rateLimit {{ remaining resetAt }}
  search(query: $q, type: ISSUE, first: 25, after: $after) {{
    issueCount
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      __typename
      ... on Issue {{ {_COMMON} }}
      ... on PullRequest {{ {_COMMON} mergedAt
        reviews(last: 20) {{ nodes {{ submittedAt body author {{ {_USER} }} comments(first: 10) {{ nodes {{ createdAt body author {{ login }} }} }} }} }} }}
    }}
  }}
}}
"""


def is_bot(login: str | None) -> bool:
    from ..github_hub import BOT_LOGINS

    x = (login or "").lower()
    return not x or x in {b.lower() for b in IGNORED} or x in BOT_LOGINS or x.endswith("[bot]")


def vendors_of(login: str | None, company: str | None) -> set[str]:
    out = set()
    for v, d in VENDORS.items():
        if (login or "") in d["logins"] or any(p in (company or "").lower() for p in d["company"]):
            out.add(v)
    return out


def normalize(n: dict[str, Any]) -> dict[str, Any]:
    def person(a: dict[str, Any] | None) -> tuple[str | None, str | None]:
        a = a or {}
        return a.get("login"), a.get("company")

    who, company = person(n.get("author"))
    comments = []
    for c in (n.get("comments") or {}).get("nodes") or []:
        lg, co = person(c.get("author"))
        comments.append({"author": lg, "company": co, "created_at": c.get("createdAt"), "body": c.get("body") or ""})
    reviews, review_comments = [], []
    for r in (n.get("reviews") or {}).get("nodes") or []:
        lg, co = person(r.get("author"))
        reviews.append({"author": lg, "company": co, "created_at": r.get("submittedAt"), "body": r.get("body") or ""})
        review_comments += [{"author": (c.get("author") or {}).get("login"), "company": None, "created_at": c.get("createdAt"),
                             "body": c.get("body") or ""} for c in (r.get("comments") or {}).get("nodes") or []]
    return {
        "number": n["number"], "kind": "pr" if n.get("__typename") == "PullRequest" else "issue", "title": n.get("title") or "",
        "url": n.get("url"), "body": n.get("body") or "", "author": who, "author_company": company, "state": n.get("state"),
        "created_at": n.get("createdAt"), "updated_at": n.get("updatedAt"), "merged_at": n.get("mergedAt"),
        "labels": [x["name"] for x in (n.get("labels") or {}).get("nodes") or []],
        "assignees": [x["login"] for x in (n.get("assignees") or {}).get("nodes") or []],
        "comments": comments, "reviews": reviews, "review_comments": review_comments,
    }


def _in(ts: str | None, a: str, b: str) -> bool:
    return bool(ts) and a <= ts <= b


def window(day: str) -> tuple[str, str]:
    return f"{day}T00:00:00Z", f"{day}T23:59:59Z"


def skipped(it: dict[str, Any]) -> bool:
    return it["title"].startswith("DISABLED") or "Performance Dashboard" in it["title"]


def active(it: dict[str, Any], a: str, b: str) -> bool:
    """Created in [a, b], or a person (not a bot) commented / reviewed then."""
    if skipped(it):
        return False
    if _in(it.get("created_at"), a, b):
        return True
    return any(_in(c.get("created_at"), a, b) and not is_bot(c.get("author")) for c in it["comments"] + it["reviews"] + it["review_comments"])


def vendor_involvement(it: dict[str, Any]) -> set[str]:
    out = vendors_of(it.get("author"), it.get("author_company"))
    for c in it["comments"] + it["reviews"] + it["review_comments"]:
        if not is_bot(c.get("author")):
            out |= vendors_of(c.get("author"), c.get("company"))
    return out


def text_of(it: dict[str, Any], a: str, b: str, body_chars: int = 4000, comment_chars: int = 1500) -> str:
    """One item for a summary, with the comments made in [a, b] (the daily-summary format)."""
    reviewers = sorted({r["author"] for r in it["reviews"] if r.get("author") and not is_bot(r["author"])})
    comments = [c for c in it["comments"] if _in(c.get("created_at"), a, b) and not is_bot(c.get("author"))]
    rcs = [c for c in it["review_comments"] + [r for r in it["reviews"] if r.get("body")] if _in(c.get("created_at"), a, b) and not is_bot(c.get("author"))]
    lines = [f"Number: {it['number']}", f"Title: {it['title']}", f"URL: {it['url']}", f"Description: {it['body'][:body_chars]}",
             f"Submitter: {it.get('author')}", f"Tags: {', '.join(it['labels'])}", f"Assignees: {', '.join(it['assignees'])}",
             f"Reviewers: {', '.join(reviewers)}", f"Created At: {it.get('created_at')}", f"State: {it.get('state')}",
             f"Comments: {len(comments)}", f"Review Comments: {len(rcs)}",
             f"Vendor Involvement: {', '.join(sorted(vendor_involvement(it))) or 'None'}"]
    lines += [f"- Comment by {c['author']} (Created at {c['created_at']}): {c['body'][:comment_chars]}" for c in comments]
    lines += [f"- Review Comment by {c['author']} (Created at {c['created_at']}): {c['body'][:comment_chars]}" for c in rcs]
    return "---\n" + "\n".join(lines) + "\n---"


# ---------------------------------------------------------------------- the local copies

def _dir(ctx: Any, repo: str) -> Path:
    p = st.data_dir(ctx) / "items" / repo
    (p / "days").mkdir(parents=True, exist_ok=True)
    return p


def _write(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)


def _load_item(ctx: Any, repo: str, n: int) -> dict[str, Any] | None:
    try:
        return json.loads((_dir(ctx, repo) / f"{n}.json").read_text())
    except (OSError, ValueError):
        return None


def _search_all(graphql: Any, q: str, lo: str, hi: str, out: dict[int, dict[str, Any]], depth: int = 0) -> None:
    """Every result of `q created:lo..hi` (GitHub stops a search at 1000: split the creation window in halves)."""
    full = f"{q} created:{lo}..{hi}"
    after = None
    first = True
    while True:
        d = graphql(QUERY, {"q": full, "after": after})
        s = d.get("search") or {}
        if first and s.get("issueCount", 0) > 1000 and depth < 12 and lo < hi:
            a, b = dt.date.fromisoformat(lo), dt.date.fromisoformat(hi)
            mid = a + (b - a) // 2
            _search_all(graphql, q, lo, mid.isoformat(), out, depth + 1)
            _search_all(graphql, q, (mid + dt.timedelta(days=1)).isoformat(), hi, out, depth + 1)
            return
        first = False
        for n in s.get("nodes") or []:
            if n and n.get("number"):
                out[n["number"]] = normalize(n)
        page = s.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return
        after = page.get("endCursor")


async def fetch_range(ctx: Any, repo: str, since: str, until: str, extra: str = "") -> list[dict[str, Any]]:
    """Items of `repo` active between the days since..until (inclusive), from GitHub (search: updated since `since`,
    created up to `until`), saved as local copies; past days are remembered as fetched."""
    from ..github_hub import _hooks

    graphql = _hooks(ctx)[0]
    found: dict[int, dict[str, Any]] = {}
    q = f"repo:{repo} updated:>={since} {extra}".strip()
    await asyncio.to_thread(_search_all, graphql, q, "2008-01-01", until, found)
    d = _dir(ctx, repo)
    a, b = f"{since}T00:00:00Z", f"{until}T23:59:59Z"
    items = [it for it in found.values() if active(it, a, b)]
    for it in found.values():
        _write(d / f"{it['number']}.json", it)
    if not extra:
        today = dt.datetime.now(dt.timezone.utc).date()
        day = dt.date.fromisoformat(since)
        while day <= dt.date.fromisoformat(until):
            if day < today:  # over: what was active then is final
                w = window(day.isoformat())
                _write(d / "days" / f"{day.isoformat()}.json", sorted(it["number"] for it in items if active(it, *w)))
            day += dt.timedelta(days=1)
    return items


def _local_range(ctx: Any, repo: str, since: str, until: str) -> list[dict[str, Any]] | None:
    """The items active in since..until from the local copies, when every day was fetched after it ended."""
    import datetime as _dt

    d, out = _dt.date.fromisoformat(since), {}
    while d <= _dt.date.fromisoformat(until):
        f = _dir(ctx, repo) / "days" / f"{d.isoformat()}.json"
        if not f.is_file():
            return None
        for n in json.loads(f.read_text()):
            it = _load_item(ctx, repo, n)
            if it is None:
                return None
            out[n] = it
        d += _dt.timedelta(days=1)
    return list(out.values())


async def items_of_range(ctx: Any, repo: str, since: str, until: str) -> list[dict[str, Any]]:
    """Items active in since..until: from the local copies when the days were fetched, else one search for the range."""
    got = _local_range(ctx, repo, since, until)
    if got is not None:
        return got
    a, b = f"{since}T00:00:00Z", f"{until}T23:59:59Z"
    return [it for it in await fetch_range(ctx, repo, since, until) if active(it, a, b)]


async def items_of_day(ctx: Any, repo: str, day: str) -> list[dict[str, Any]]:
    """Items active on a day: from the local copies when the day was fetched after it ended, else from GitHub."""
    f = _dir(ctx, repo) / "days" / f"{day}.json"
    if f.is_file():
        nums = json.loads(f.read_text())
        got = [x for n in nums if (x := _load_item(ctx, repo, n))]
        if len(got) == len(nums):
            return got
    w = window(day)
    return [it for it in await fetch_range(ctx, repo, day, day) if active(it, *w)]


async def rfcs(ctx: Any, cfg: dict[str, Any], since: str, until: str) -> list[dict[str, Any]]:
    """RFCs active in the period: the RFC repositories' issues and PRs, and the repository's issues / PRs labelled or
    titled as RFCs (the repository's RFC settings)."""
    rule = cfg.get("rfc") or {}
    a, b = f"{since}T00:00:00Z", f"{until}T23:59:59Z"
    out: dict[str, dict[str, Any]] = {}
    for r in rule.get("repos") or []:
        for it in await fetch_range(ctx, r, since, until, extra=" "):
            out[it["url"]] = {**it, "repo": r}
    pats = [p.lower() for p in rule.get("title_patterns") or []]
    for lb in rule.get("labels") or []:
        for it in await fetch_range(ctx, cfg["repo"], since, until, extra=f'label:"{lb}"'):
            out[it["url"]] = {**it, "repo": cfg["repo"]}
    if pats:
        for it in await fetch_range(ctx, cfg["repo"], since, until, extra="RFC in:title"):
            if any(p in it["title"].lower() for p in pats):
                out[it["url"]] = {**it, "repo": cfg["repo"]}
    return sorted((x for x in out.values() if active(x, a, b)), key=lambda x: x.get("updated_at") or "", reverse=True)


def rfc_text(it: dict[str, Any], a: str, b: str) -> str:
    comments = [c for c in it["comments"] + it["reviews"] if _in(c.get("created_at"), a, b) and not is_bot(c.get("author"))]
    state = it.get("state") + (" (merged)" if it.get("merged_at") else "")
    lines = [f"RFC: [{it['kind'].upper()} {it['repo']}#{it['number']}]({it['url']}) {it['title']}", f"State: {state}; by {it.get('author')}; "
             f"created {it.get('created_at')}; labels: {', '.join(it['labels']) or 'none'}", f"Description: {it['body'][:3000]}"]
    lines += [f"- {c['author']} ({c['created_at'][:10]}): {c['body'][:800]}" for c in comments[-15:]]
    return "\n".join(lines)


def diff_of(ctx: Any, repo: str, number: int, limit: int = 60_000) -> str:
    """The PR's diff for scoring: one REST page (the first 100 files; enough for a judgement, and a huge PR does not
    spend many requests)."""
    from .. import gh_limits

    if "gh_pr_files" in ctx.extra:  # tests
        try:
            files = ctx.extra["gh_pr_files"](*repo.split("/", 1), number)
        except Exception:  # noqa: BLE001
            files = []
    else:
        try:
            r = gh_limits.run_gh(["api", f"repos/{repo}/pulls/{number}/files?per_page=100"], timeout=60)
            files = json.loads(r.out) if r.rc == 0 else []
        except (ValueError, gh_limits.ApiError) as exc:
            log.info("diff of %s#%d: %s", repo, number, exc)
            files = []
    out = "".join(f"--- a/{f['filename']}\n+++ b/{f['filename']}\n{f.get('patch') or '(binary or large)'}\n" for f in files if isinstance(f, dict))
    return out[:limit]


def mentions(text: str) -> int:
    return len(re.findall(r"@[\w-]+", text or ""))
