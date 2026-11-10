"""GitHub review hub: owner-only access, the review queue from a fake GitHub GraphQL API, caching, errors."""

from __future__ import annotations

from backend.app import github_hub
from backend.app.errors import ApiError
from backend.app.models import LoginSession


def pr(n, repo="pytorch/pytorch", *, draft=False, ci="SUCCESS", team=None, user="EikanWang"):
    reqs = [{"requestedReviewer": {"__typename": "User", "login": user}}] if user else []
    if team:
        reqs.append({"requestedReviewer": {"__typename": "Team", "slug": team, "name": team}})
    return {"number": n, "title": f"PR {n}", "url": f"https://github.com/{repo}/pull/{n}", "isDraft": draft,
            "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-08T00:00:00Z", "additions": 10, "deletions": 2,
            "changedFiles": 1, "reviewDecision": "REVIEW_REQUIRED", "author": {"login": "someone"},
            "repository": {"nameWithOwner": repo, "isPrivate": repo.endswith("private")},
            "labels": {"nodes": [{"name": "xpu", "color": "ff0000"}]},
            "reviewRequests": {"nodes": reqs},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": ci} if ci else None}}]}}


def fake_github(pages, calls):
    def graphql(query, variables):
        calls.append(variables)
        i = 0 if variables.get("after") is None else int(variables["after"])
        nodes, more = pages[i], i + 1 < len(pages)
        return {"rateLimit": {"remaining": 4999}, "search": {"issueCount": sum(len(p) for p in pages),
                "pageInfo": {"hasNextPage": more, "endCursor": str(i + 1) if more else None}, "nodes": nodes}}
    return graphql


def as_github(h, login):
    with h.ctx.db.write() as s:
        for row in s.query(LoginSession).all():
            row.identity = f"github:{login}"


async def test_review_queue_owner_only_paginated_and_cached(running_app):
    github_hub.reset_cache()
    calls: list = []
    h = await running_app()
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    h.ctx.extra["gh_graphql"] = fake_github([[pr(1), pr(2, draft=True, ci="FAILURE")], [pr(3, "intel/torch-xpu-ops", team="xpu-reviewers", user=None)]], calls)
    api = h.api
    # token login: no access to the queue (it can include private repos)
    st = (await api.get("/api/v1/github/hub")).json()
    assert st == {"available": False, "owner": "EikanWang", "reason": "not_owner"}
    assert (await api.get("/api/v1/github/reviews")).json()["error"]["code"] == "NOT_GITHUB_OWNER"
    as_github(h, "someone-else")
    assert (await api.get("/api/v1/github/reviews")).status_code == 403
    # the owner, signed in with GitHub (login compared case-insensitively)
    as_github(h, "eikanwang")
    assert (await api.get("/api/v1/github/hub")).json()["available"] is True
    r = (await api.get("/api/v1/github/reviews")).json()
    assert r["total"] == 3 and len(r["items"]) == 3 and r["truncated"] is False and r["refreshing"] is False
    assert len(calls) == 2 and calls[0]["q"].startswith("is:pr is:open")  # two pages (warmed by /github/hub)
    by = {i["number"]: i for i in r["items"]}
    assert by[1]["requested"] == "you" and by[1]["ci"] == "SUCCESS" and by[1]["labels"][0]["name"] == "xpu"
    assert by[2]["draft"] is True and by[2]["ci"] == "FAILURE"
    assert by[3]["requested"] == "team" and by[3]["teams"] == ["xpu-reviewers"] and by[3]["repo"] == "intel/torch-xpu-ops"
    # cached: no new GitHub call; a refresh right after is also served from the cache (search quota)
    assert (await api.get("/api/v1/github/reviews")).json()["refreshing"] is False
    assert (await api.get("/api/v1/github/reviews?refresh=true")).json()["refreshing"] is False
    assert len(calls) == 2
    # an old result is returned at once while a fresh one loads in the background
    import asyncio

    owner = "EikanWang"
    ts, data = github_hub._cache[owner]
    github_hub._cache[owner] = (ts - 1000, data)
    r = (await api.get("/api/v1/github/reviews")).json()
    assert r["refreshing"] is True and len(r["items"]) == 3 and r["age_seconds"] >= 1000
    await asyncio.wait_for(github_hub._tasks[owner], 5)
    r = (await api.get("/api/v1/github/reviews")).json()
    assert r["refreshing"] is False and r["age_seconds"] < 5 and len(calls) == 4


def test_saml_protected_prs_are_hidden_with_an_explanation():
    def graphql(q, v):
        return {"search": {"issueCount": 2, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [pr(1), None]},
                "_errors": [{"type": "FORBIDDEN", "message": "Resource protected by organization SAML enforcement."}]}

    d = github_hub.fetch_queue(graphql, "EikanWang")
    assert len(d["items"]) == 1 and d["hidden"] == 1 and "SAML" in d["note"] and d["truncated"] is False


def test_busy_github_is_retried_with_smaller_pages():
    calls: list = []

    def graphql(q, v):
        calls.append(v["n"])
        if v["n"] > 20:
            raise ApiError(502, "GH_BUSY", "GitHub took too long")
        return {"search": {"issueCount": 1, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [pr(1)]}}

    d = github_hub.fetch_queue(graphql, "EikanWang")
    assert len(d["items"]) == 1 and calls == [40, 20]


async def test_review_queue_errors_are_reported(running_app):
    github_hub.reset_cache()
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"

    def limited(q, v):
        raise ApiError(429, "GH_RATE_LIMITED", "GitHub's rate limit is reached; try again in a minute")

    h.ctx.extra["gh_graphql"] = limited
    r = await h.api.get("/api/v1/github/reviews")
    assert r.status_code == 429 and r.json()["error"]["code"] == "GH_RATE_LIMITED"
    # gh missing / logged out: the hub reports it, nothing to show
    h.ctx.extra["gh_owner"] = lambda: ""
    assert (await h.api.get("/api/v1/github/hub")).json() == {"available": False, "owner": None, "reason": "gh_unavailable"}
    assert (await h.api.get("/api/v1/github/reviews")).json()["error"]["code"] == "GH_UNAVAILABLE"


def test_gh_errors_are_classified(monkeypatch):
    import subprocess

    class P:
        def __init__(self, err):
            self.returncode, self.stdout, self.stderr = 1, "", err

    monkeypatch.setattr(github_hub.shutil, "which", lambda n: "/usr/bin/gh")
    for err, code in [("API rate limit exceeded for user", "GH_RATE_LIMITED"), ("HTTP 401: Bad credentials", "GH_AUTH"), ("<html>... oauth ...</html> HTTP 502", "GH_BUSY"),
                      ("dial tcp: could not resolve host api.github.com", "GH_UNREACHABLE"), ("something else", "GH_ERROR")]:
        monkeypatch.setattr(subprocess, "run", lambda *a, _e=err, **k: P(_e))
        try:
            github_hub._run_gh(["api", "x"])
        except ApiError as e:
            assert e.code == code, (err, e.code)
        else:
            raise AssertionError("no error")
