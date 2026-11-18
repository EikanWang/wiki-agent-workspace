"""Reviews → Monitors: per-repository GitHub search rules for pull requests and issues, new/updated since last seen."""

from __future__ import annotations

from backend.app import gh_monitors, github_hub
from backend.app.errors import ApiError
from backend.app.models import LoginSession


def as_github(h, login):
    with h.ctx.db.write() as s:
        for row in s.query(LoginSession).all():
            row.identity = f"github:{login}"


def node(n, kind="PullRequest", created="2026-11-01T00:00:00Z", updated="2026-11-08T00:00:00Z"):
    return {"__typename": kind, "number": n, "title": f"Item {n}", "url": f"https://github.com/pytorch/pytorch/{'pull' if kind == 'PullRequest' else 'issues'}/{n}",
            "state": "OPEN", "isDraft": False, "createdAt": created, "updatedAt": updated, "author": {"login": "someone"},
            "repository": {"nameWithOwner": "pytorch/pytorch"}, "labels": {"nodes": [{"name": "module: xpu", "color": "ff0000"}]},
            "comments": {"totalCount": 3}}


async def test_monitor_rules_per_repository_list_prs_and_issues_and_mark_what_is_new(running_app):
    gh_monitors.reset_cache()
    github_hub.reset_cache()
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    calls: list = []
    found = [node(1), node(2, "Issue")]

    def graphql(q, v):
        calls.append(v)
        if "bad:" in v["q"]:
            raise ApiError(422, "GH_ERROR", "invalid search")
        return {"search": {"issueCount": len(found), "nodes": found}}

    h.ctx.extra["gh_graphql"] = graphql
    assert (await h.api.get("/api/v1/github/monitors")).json() == {"rules": []}
    # a rule is a GitHub search within its repository
    for bad, code in [({"repo": "pytorch", "query": "is:open"}, "INVALID_REPO"), ({"repo": "pytorch/pytorch", "query": "repo:a/b is:open"}, "INVALID_QUERY"),
                      ({"repo": "pytorch/pytorch", "query": "   "}, "INVALID_QUERY")]:
        r = await h.api.post("/api/v1/github/monitors", json=bad)
        assert r.status_code == 400 and r.json()["error"]["code"] == code, r.text
    r = await h.api.post("/api/v1/github/monitors", json={"repo": "pytorch/pytorch", "query": 'is:open  label:"module: xpu"', "name": "XPU"})
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["query"] == 'is:open label:"module: xpu"' and rule["name"] == "XPU" and rule["unseen"] is None
    other = (await h.api.post("/api/v1/github/monitors", json={"repo": "intel/torch-xpu-ops", "query": "is:issue is:open"})).json()
    assert other["name"] == "is:issue is:open"

    d = (await h.api.get(f"/api/v1/github/monitors/{rule['id']}/items")).json()
    assert calls[-1]["q"] == 'repo:pytorch/pytorch is:open label:"module: xpu" sort:updated-desc'
    assert [(x["kind"], x["number"], x["new"], x["updated"]) for x in d["items"]] == [("pr", 1, False, False), ("issue", 2, False, False)]
    assert d["items"][0]["labels"] == [{"name": "module: xpu", "color": "ff0000"}] and d["unseen"] == 0
    # cached (GitHub's search quota) unless refreshed
    await h.api.get(f"/api/v1/github/monitors/{rule['id']}/items")
    assert len(calls) == 1
    # seen; later one is updated and a new one appears
    assert (await h.api.post(f"/api/v1/github/monitors/{rule['id']}/seen")).json()["seen_at"] > 0
    found[:] = [node(3, created="2099-01-01T00:00:00Z", updated="2099-01-01T00:00:00Z"), node(1, updated="2099-01-02T00:00:00Z"), node(2, "Issue")]
    d = (await h.api.get(f"/api/v1/github/monitors/{rule['id']}/items?refresh=true")).json()
    assert [(x["number"], x["new"], x["updated"]) for x in d["items"]] == [(3, True, False), (1, False, True), (2, False, False)]
    assert d["unseen"] == 2
    rules = (await h.api.get("/api/v1/github/monitors")).json()["rules"]
    assert [(r["repo"], r["unseen"]) for r in rules] == [("pytorch/pytorch", 2), ("intel/torch-xpu-ops", None)]
    # edit, a GitHub error is reported, delete
    r = await h.api.patch(f"/api/v1/github/monitors/{other['id']}", json={"query": "is:issue bad:x"})
    assert r.json()["query"] == "is:issue bad:x" and r.json()["name"] == "is:issue is:open"
    r = await h.api.get(f"/api/v1/github/monitors/{other['id']}/items")
    assert r.status_code == 422
    assert (await h.api.delete(f"/api/v1/github/monitors/{other['id']}")).status_code == 200
    assert (await h.api.get(f"/api/v1/github/monitors/{other['id']}/items")).status_code == 404
    # independent of the review queue and its prefs
    assert "github_monitors" not in str((await h.api.get("/api/v1/github/prefs")).json())


async def test_monitors_are_owner_only(running_app):
    h = await running_app()
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    as_github(h, "someone-else")
    assert (await h.api.get("/api/v1/github/monitors")).status_code == 403
