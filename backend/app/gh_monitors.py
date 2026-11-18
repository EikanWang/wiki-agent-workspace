"""Monitors: pull requests and issues of a repository picked by saved GitHub search rules (Reviews → Monitors).

Each rule belongs to one repository (``owner/name``) and is a GitHub search query (``is:open label:bug -author:@me``,
``is:pr review-requested:@me``, ``is:issue updated:>@today-7d`` …). They are independent of the review queue: nothing
here changes the queue, its deferred PRs or the Reviews badge. Each rule remembers when it was last seen, so what is
new or updated since is marked. Rules: kv ``github_monitors``.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .db import new_id
from .deps import AppContext
from .errors import ApiError, not_found
from .github_hub import _hooks, require_owner, require_owner_mutating
from .kv import kv_get, kv_put

router = APIRouter()

KEY = "github_monitors"
TTL = 120.0  # s: a rule's results are reused this long (GitHub's search quota)
LIMIT = 50
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_SCOPE = re.compile(r"(?:^|\s)-?(?:repo|org|user):", re.I)
_cache: dict[str, tuple[float, str, dict[str, Any]]] = {}  # rule id -> (time, query, result)

MONITOR_QUERY = """query MonitorItems($q: String!, $n: Int!) {
  search(query: $q, type: ISSUE, first: $n) {
    issueCount
    nodes {
      __typename
      ... on PullRequest { number title url state isDraft createdAt updatedAt author { login } repository { nameWithOwner }
        labels(first: 10) { nodes { name color } } comments { totalCount } }
      ... on Issue { number title url state createdAt updatedAt author { login } repository { nameWithOwner }
        labels(first: 10) { nodes { name color } } comments { totalCount } }
    }
  }
}"""


def reset_cache() -> None:
    _cache.clear()


def load(ctx: AppContext) -> list[dict[str, Any]]:
    rules = kv_get(ctx, KEY, [])
    return rules if isinstance(rules, list) else []


def search_query(rule: dict[str, Any]) -> str:
    q = f"repo:{rule['repo']} {rule['query'].strip()}"
    if not re.search(r"(?:^|\s)sort:", q):
        q += " sort:updated-desc"
    return q


def _check(repo: str, query: str) -> tuple[str, str]:
    repo, query = repo.strip(), " ".join(query.split())
    if not _REPO.match(repo):
        raise ApiError(400, "INVALID_REPO", f"{repo!r}: use owner/name")
    if not query:
        raise ApiError(400, "INVALID_QUERY", "write the rule as a GitHub search, e.g. is:open label:bug")
    if _SCOPE.search(query):
        raise ApiError(400, "INVALID_QUERY", "a rule searches its own repository: leave out repo:, org: and user:")
    return repo, query


def _item(n: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "pr" if n.get("__typename") == "PullRequest" else "issue", "repo": (n.get("repository") or {}).get("nameWithOwner"),
            "number": n["number"], "title": n.get("title") or "", "url": n.get("url"), "state": n.get("state"), "draft": bool(n.get("isDraft")),
            "author": (n.get("author") or {}).get("login"), "labels": [{"name": x["name"], "color": x.get("color")} for x in (n.get("labels") or {}).get("nodes") or [] if x],
            "comments": (n.get("comments") or {}).get("totalCount", 0), "created_at": n.get("createdAt"), "updated_at": n.get("updatedAt")}


def _ts(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def mark(result: dict[str, Any], seen_at: float | None) -> dict[str, Any]:
    """New (created) / updated since the rule was last seen; never seen: nothing is marked (the first look is the base)."""
    items = []
    for x in result["items"]:
        new = seen_at is not None and _ts(x["created_at"]) > seen_at
        upd = seen_at is not None and not new and _ts(x["updated_at"]) > seen_at
        items.append({**x, "new": new, "updated": upd})
    return {**result, "items": items, "unseen": sum(1 for x in items if x["new"] or x["updated"])}


def _public(rule: dict[str, Any]) -> dict[str, Any]:
    hit = _cache.get(rule["id"])
    unseen = mark(hit[2], rule.get("seen_at"))["unseen"] if hit and hit[1] == search_query(rule) else None
    return {**rule, "unseen": unseen}


class RuleIn(BaseModel):
    repo: str = Field(min_length=3, max_length=200)
    query: str = Field(min_length=1, max_length=300)
    name: str | None = Field(None, max_length=80)


class RuleEditIn(BaseModel):
    query: str | None = Field(None, min_length=1, max_length=300)
    name: str | None = Field(None, max_length=80)


@router.get("/github/monitors")
def list_rules(auth: tuple[AppContext, str] = Depends(require_owner)):
    """The rules, grouped by repository in the order they were added (with how many items are unseen, when known)."""
    ctx, _ = auth
    return {"rules": [_public(r) for r in load(ctx)]}


@router.post("/github/monitors", status_code=201)
def add_rule(body: RuleIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    ctx, _ = auth
    repo, query = _check(body.repo, body.query)
    rule = {"id": new_id(), "repo": repo, "query": query, "name": (body.name or "").strip() or query, "seen_at": None}
    kv_put(ctx, KEY, [*load(ctx), rule])
    return _public(rule)


@router.patch("/github/monitors/{rule_id}")
def edit_rule(rule_id: str, body: RuleEditIn, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    ctx, _ = auth
    rules = load(ctx)
    rule = next((r for r in rules if r["id"] == rule_id), None)
    if rule is None:
        raise not_found("monitor rule not found", "MONITOR_NOT_FOUND")
    if body.query is not None:
        _, rule["query"] = _check(rule["repo"], body.query)
    if body.name is not None:
        rule["name"] = body.name.strip() or rule["query"]
    kv_put(ctx, KEY, rules)
    return _public(rule)


@router.delete("/github/monitors/{rule_id}")
def delete_rule(rule_id: str, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    ctx, _ = auth
    rules = load(ctx)
    if not any(r["id"] == rule_id for r in rules):
        raise not_found("monitor rule not found", "MONITOR_NOT_FOUND")
    kv_put(ctx, KEY, [r for r in rules if r["id"] != rule_id])
    _cache.pop(rule_id, None)
    return {"deleted": rule_id}


@router.get("/github/monitors/{rule_id}/items")
async def rule_items(rule_id: str, refresh: bool = Query(False), auth: tuple[AppContext, str] = Depends(require_owner)):
    """The rule's pull requests and issues (most recently updated first unless the rule sorts), each marked new or
    updated since the rule was last seen."""
    ctx, _ = auth
    rule = next((r for r in load(ctx) if r["id"] == rule_id), None)
    if rule is None:
        raise not_found("monitor rule not found", "MONITOR_NOT_FOUND")
    q = search_query(rule)
    hit = _cache.get(rule_id)
    if refresh or hit is None or hit[1] != q or time.time() - hit[0] > TTL:
        try:
            d = await asyncio.to_thread(_hooks(ctx)[0], MONITOR_QUERY, {"q": q, "n": LIMIT})
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - GitHub's answer (bad query syntax, network): shown with the rule
            raise ApiError(502, "GH_SEARCH_FAILED", f"GitHub search failed: {exc}") from exc
        s = d.get("search") or {}
        items = [_item(n) for n in s.get("nodes") or [] if n and n.get("number")]
        hit = (time.time(), q, {"query": q, "total": s.get("issueCount", len(items)), "items": items, "fetched_at": time.time()})
        _cache[rule_id] = hit
    return {**mark(hit[2], rule.get("seen_at")), "seen_at": rule.get("seen_at")}


@router.post("/github/monitors/{rule_id}/seen")
def mark_seen(rule_id: str, auth: tuple[AppContext, str] = Depends(require_owner_mutating)):
    """Everything the rule shows now counts as seen (later changes are marked)."""
    ctx, _ = auth
    rules = load(ctx)
    rule = next((r for r in rules if r["id"] == rule_id), None)
    if rule is None:
        raise not_found("monitor rule not found", "MONITOR_NOT_FOUND")
    rule["seen_at"] = datetime.now(timezone.utc).timestamp()
    kv_put(ctx, KEY, rules)
    return _public(rule)
