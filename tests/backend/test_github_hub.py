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


def fake_pr_detail(calls):
    def graphql(q, v):
        calls.append(v)
        if "pullRequest(number" in q:
            return {"repository": {"nameWithOwner": "pytorch/pytorch", "isPrivate": False, "pullRequest": {
                "number": 7, "title": "[xpu] Faster GEMM", "url": "https://github.com/pytorch/pytorch/pull/7", "body": "Speeds up **GEMM**.",
                "state": "OPEN", "isDraft": False, "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-08T00:00:00Z", "mergeable": "MERGEABLE",
                "additions": 3, "deletions": 1, "changedFiles": 2, "baseRefName": "main", "headRefName": "gemm", "reviewDecision": "REVIEW_REQUIRED",
                "author": {"login": "alice"}, "labels": {"nodes": [{"name": "module: xpu", "color": "0e8a16"}]},
                "latestReviews": {"nodes": [{"author": {"login": "bob"}, "state": "APPROVED", "submittedAt": "2026-11-07T00:00:00Z", "url": "u"}]},
                "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": "User", "login": "EikanWang"}}]},
                "commits": {"nodes": [{"commit": {"oid": "abc", "statusCheckRollup": {"state": "FAILURE", "contexts": {"nodes": [
                    {"__typename": "CheckRun", "name": "lint", "conclusion": "SUCCESS", "status": "COMPLETED", "detailsUrl": "x"},
                    {"__typename": "CheckRun", "name": "xpu-test", "conclusion": "FAILURE", "status": "COMPLETED", "detailsUrl": "y"}]}}}}]},
            }}}
        raise AssertionError(q[:60])
    return graphql


async def test_pull_request_tab_wiki_mapping_and_discussion(running_app, workspace, fake_env):
    github_hub.reset_cache()
    calls: list = []
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    h.ctx.extra["gh_graphql"] = fake_pr_detail(calls)
    h.ctx.extra["gh_pr_files"] = lambda o, r, n: [
        {"filename": "aten/gemm.cpp", "status": "modified", "additions": 3, "deletions": 1, "patch": "@@ -1,2 +1,4 @@\n-old\n+fast\n+tiled\n+done"},
        {"filename": "model.bin", "status": "added", "additions": 0, "deletions": 0, "patch": None}]
    api = h.api
    r = (await api.get("/api/v1/github/pr/pytorch/pytorch/7")).json()
    assert r["pr"]["title"] == "[xpu] Faster GEMM" and r["pr"]["ci"] == "FAILURE" and r["pr"]["checks"][0]["name"] == "xpu-test"  # failures first
    assert r["pr"]["reviews"][0]["state"] == "APPROVED" and r["files"][0]["unified"].startswith("--- a/aten/gemm.cpp\n+++ b/aten/gemm.cpp\n@@")
    assert r["files"][1]["binary_or_large"] is True and r["wiki_workspace_id"] is None
    assert r["doc_path"] == ".waw-github/pytorch/pytorch/7.md"
    # choose the wiki for pytorch/pytorch once
    ws = (await api.get("/api/v1/workspaces")).json()["workspaces"][0]["id"]
    assert (await api.put("/api/v1/github/repo-wiki", json={"repo": "pytorch/pytorch", "workspace_id": ws})).status_code == 200
    assert (await api.get("/api/v1/github/pr/pytorch/pytorch/7")).json()["wiki_workspace_id"] == ws
    assert (await api.get("/api/v1/github/repo-wikis")).json()["repos"] == [{"repo": "pytorch/pytorch", "workspace_id": ws}]
    # the PR is a read-only document of that wiki (never a file in it, never in the file tree)
    d = (await api.get("/api/v1/documents/open?path=.waw-github/pytorch/pytorch/7.md")).json()
    assert d["format"] == "markdown" and d["editable"] is False and "+tiled" in d["text"] and "Speeds up **GEMM**." in d["text"]
    assert not (workspace / ".waw-github").exists()
    assert (await api.put(f"/api/v1/documents/{d['document_id']}/content", json={"text": "x", "base_sha256": d["sha256"]})).json()["error"]["code"] == "NOT_EDITABLE"
    # ask about the whole PR, then about selected diff lines; the agent gets the PR and works in the wiki
    r = await api.post(f"/api/v1/documents/{d['document_id']}/threads", json={"whole_document": True, "version_sha256": d["sha256"], "question": "Is this safe?"})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    r = await api.post(f"/api/v1/documents/{d['document_id']}/threads", json={"version_sha256": d["sha256"], "question": "why tiled?",
                       "quote": "+fast\n+tiled", "target": {"kind": "text", "label": "aten/gemm.cpp"}})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    from backend.app.models import Job

    with h.ctx.db.read() as s:
        prompts = [j.prompt for j in s.query(Job).order_by(Job.created_at).all()]
    assert "review the GitHub pull request pytorch/pytorch#7: [xpu] Faster GEMM" in prompts[0] and "+tiled" in prompts[0]
    assert f"working directory is the user's wiki ({workspace.resolve()})" in prompts[0] and "never comment on, approve, push" in prompts[0]
    assert "lines of aten/gemm.cpp" in prompts[1] and "<<<SELECTED_PASSAGE\n+fast\n+tiled" in prompts[1]
    recent = (await api.get("/api/v1/recent")).json()["items"]
    assert any(i["path"] == ".waw-github/pytorch/pytorch/7.md" for i in recent)
    # a token login can reach none of it: the PR document, its discussions, Recent, the mapping
    as_github(h, "")
    with h.ctx.db.write() as s:
        from backend.app.models import LoginSession

        for row in s.query(LoginSession).all():
            row.identity = "token"
    assert (await api.get("/api/v1/documents/open?path=.waw-github/pytorch/pytorch/7.md")).status_code == 403
    assert (await api.get(f"/api/v1/documents/{d['document_id']}/threads")).status_code == 403
    assert not any(i["path"].startswith(".waw-github/") for i in (await api.get("/api/v1/recent")).json()["items"])
    assert (await api.put("/api/v1/github/repo-wiki", json={"repo": "pytorch/pytorch", "workspace_id": ws})).status_code == 403
    assert (await api.get("/api/v1/github/pr/pytorch/pytorch/7")).status_code == 403
    assert (await api.get("/api/v1/github/pr/..%2Fx/y/1")).status_code in (400, 403, 404)
