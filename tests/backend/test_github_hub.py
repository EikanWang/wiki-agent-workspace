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


def fake_pr_detail(calls, gh=None):
    """Fake GitHub GraphQL for one PR. `gh` (optional dict) holds threads/conversation/viewed state and records
    mutations, so review features can be checked end to end."""
    gh = gh if gh is not None else {}
    gh.setdefault("viewed", {})
    gh.setdefault("threads", [])
    gh.setdefault("conversation", {"reviews": [], "comments": [], "pending": []})
    gh.setdefault("mutations", [])

    def graphql(q, v):
        calls.append(v)
        if q.lstrip().startswith("mutation"):
            name = q.split()[1].split("(")[0]
            gh["mutations"].append((name, v))
            if gh.get("refuse") == name:
                return {name and "x": None, "_errors": [{"message": "Line could not be resolved"}]}
            key = {"MarkViewed": "markFileAsViewed", "MarkUnviewed": "unmarkFileAsViewed", "Resolve": "resolveReviewThread",
                   "Unresolve": "unresolveReviewThread", "AddReview": "addPullRequestReview", "AddThread": "addPullRequestReviewThread",
                   "Submit": "submitPullRequestReview", "DeleteReview": "deletePullRequestReview"}[name]
            if name in ("MarkViewed", "MarkUnviewed"):
                gh["viewed"][v["path"]] = "VIEWED" if name == "MarkViewed" else "UNVIEWED"
            res = {"AddReview": {"pullRequestReview": {"id": "PRR_new"}}, "Submit": {"pullRequestReview": {"url": "https://github.com/pytorch/pytorch/pull/7#pullrequestreview-1"}},
                   "Resolve": {"thread": {"id": v.get("id"), "isResolved": True}}, "Unresolve": {"thread": {"id": v.get("id"), "isResolved": False}}}.get(name, {"clientMutationId": None, "thread": {"id": "T"}})
            return {key: res}
        if "query PrFiles" in q:
            return {"repository": {"pullRequest": {"files": {"pageInfo": {"hasNextPage": False}, "nodes": [{"path": k, "viewerViewedState": x} for k, x in gh["viewed"].items()]}}}}
        if "query PrThreads" in q:
            return {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": gh["threads"]}}}}
        if "query PrConversation" in q:
            c = gh["conversation"]
            return {"repository": {"pullRequest": {"reviews": {"totalCount": len(c["reviews"]), "nodes": c["reviews"]},
                                                    "comments": {"totalCount": len(c["comments"]), "nodes": c["comments"]},
                                                    "pending": {"nodes": c["pending"]}}}}
        if "query PrDetail" in q:
            return {"repository": {"nameWithOwner": "pytorch/pytorch", "isPrivate": False, "pullRequest": {
                "id": "PR_node7", "number": 7, "title": "[xpu] Faster GEMM", "url": "https://github.com/pytorch/pytorch/pull/7", "body": "Speeds up **GEMM**.",
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


async def test_pr_review_threads_conversation_viewed_drafts_and_publish(running_app, settings):
    github_hub.reset_cache()
    calls: list = []
    gh = {"viewed": {"a.cpp": "DISMISSED", "b.md": "UNVIEWED"},
          "threads": [
              {"id": "PRRT_t1", "isResolved": False, "isOutdated": False, "path": "a.cpp", "line": 1, "originalLine": 1, "startLine": None,
               "diffSide": "RIGHT", "startDiffSide": None, "subjectType": "LINE", "resolvedBy": None,
               "comments": {"totalCount": 2, "nodes": [{"databaseId": 11, "author": {"login": "bob"}, "body": "why?", "createdAt": "2026-11-07T00:00:00Z", "url": "u", "diffHunk": "@@ -1 +1 @@\n-x\n+y"},
                                                        {"databaseId": 12, "author": {"login": "alice"}, "body": "because", "createdAt": "2026-11-07T01:00:00Z", "url": "u2", "diffHunk": "@@"}]}},
              {"id": "PRRT_t2", "isResolved": True, "isOutdated": True, "path": "a.cpp", "line": None, "originalLine": 5, "startLine": None,
               "diffSide": "RIGHT", "startDiffSide": None, "subjectType": "LINE", "resolvedBy": {"login": "bob"},
               "comments": {"totalCount": 1, "nodes": [{"databaseId": 13, "author": {"login": "bob"}, "body": "old", "createdAt": "2026-11-06T00:00:00Z", "url": "u3", "diffHunk": "@@"}]}},
              {"id": "PRRT_t3", "isResolved": False, "isOutdated": False, "path": "b.md", "line": None, "originalLine": None, "startLine": None,
               "diffSide": "RIGHT", "startDiffSide": None, "subjectType": "FILE", "resolvedBy": None,
               "comments": {"totalCount": 1, "nodes": [{"databaseId": 14, "author": {"login": "carol"}, "body": "rename this file", "createdAt": "2026-11-08T00:00:00Z", "url": "u4", "diffHunk": None}]}}],
          "conversation": {"reviews": [{"databaseId": 21, "author": {"login": "bob"}, "state": "CHANGES_REQUESTED", "body": "Needs tests", "submittedAt": "2026-11-07T02:00:00Z", "url": "r",
                                        "comments": {"totalCount": 1, "nodes": [{"databaseId": 11, "path": "a.cpp", "line": 1, "originalLine": 1, "body": "why?", "diffHunk": "@@ -1 +1 @@\n-x\n+y", "outdated": False, "url": "u", "replyTo": None}]}}],
                           "comments": [{"databaseId": 31, "author": {"login": "pytorch-bot"}, "body": "CI started", "createdAt": "2026-11-06T00:00:00Z", "url": "c"}],
                           "pending": []}}
    sent: list = []
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    h.ctx.extra["gh_graphql"] = fake_pr_detail(calls, gh)
    h.ctx.extra["gh_pr_files"] = lambda o, r, n: [{"filename": "a.cpp", "status": "modified", "additions": 1, "deletions": 1, "patch": "@@ -1 +1 @@\n-x\n+y"},
                                                  {"filename": "b.md", "status": "added", "additions": 1, "deletions": 0, "patch": "@@ -0,0 +1 @@\n+hi"}]

    def rest(method, path, body=None):
        sent.append((method, path, body))
        if path.endswith("/replies"):
            return {"id": 15, "body": body["body"], "user": {"login": "EikanWang"}}
        if path.endswith("/issues/7/comments"):
            return {"id": 32, "body": body["body"], "user": {"login": "EikanWang"}, "html_url": "c2", "created_at": "2026-11-09T00:00:00Z"}
        raise AssertionError(path)

    h.ctx.extra["gh_rest"] = rest
    api = h.api
    base = "/api/v1/github/pr/pytorch/pytorch/7"
    files = {f["path"]: f for f in (await api.get(base)).json()["files"]}
    assert files["a.cpp"]["viewed"] == "DISMISSED" and files["b.md"]["viewed"] == "UNVIEWED"  # viewed state from GitHub
    c = (await api.get(f"{base}/comments")).json()
    t = {x["id"]: x for x in c["threads"]}
    assert [m["author"] for m in t["PRRT_t1"]["comments"]] == ["bob", "alice"] and t["PRRT_t1"]["resolved"] is False and t["PRRT_t1"]["diff_hunk"].startswith("@@")
    assert t["PRRT_t2"]["resolved"] is True and t["PRRT_t2"]["outdated"] is True and t["PRRT_t2"]["resolved_by"] == "bob"
    assert t["PRRT_t3"]["subject"] == "FILE" and t["PRRT_t3"]["path"] == "b.md"
    assert [x["kind"] for x in c["conversation"]] == ["comment", "review"] and c["conversation"][1]["state"] == "CHANGES_REQUESTED"
    # a review's line comments are part of it in the conversation (like GitHub), with their code
    lc = c["conversation"][1]["comments"]
    assert lc == [{"id": 11, "path": "a.cpp", "line": 1, "outdated": False, "body": "why?", "url": "u", "reply": False, "reply_to": None, "author": "bob", "created_at": "2026-11-07T02:00:00Z", "hunk": "@@ -1 +1 @@\n-x\n+y"}]
    assert c["pending_on_github"] is None and c["drafts"] == []
    # the agent gets the discussion with the PR
    disc = (settings.snapshots_dir / "github" / "pytorch" / "pytorch" / "7.discussion.md").read_text()
    assert "bob (review: changes requested): Needs tests" in disc and "b.md [" not in disc and "rename this file" in disc and "[resolved, outdated]" in disc
    # viewed, resolve, general comment, reply — straight to GitHub
    assert (await api.put(f"{base}/viewed", json={"path": "a.cpp", "viewed": True})).json()["viewed"] == "VIEWED"
    assert ("MarkViewed", {"pr": "PR_node7", "path": "a.cpp"}) in gh["mutations"]
    assert (await api.put(f"{base}/viewed", json={"path": "a.cpp", "viewed": False})).json()["viewed"] == "UNVIEWED"
    assert (await api.post(f"{base}/threads/PRRT_t1/resolve", json={"resolved": True})).json()["resolved"] is True
    assert (await api.post(f"{base}/threads/PRRT_t2/resolve", json={"resolved": False})).json()["resolved"] is False
    assert (await api.post(f"{base}/threads/bad%20id/resolve", json={"resolved": True})).status_code == 400
    cm = (await api.post(f"{base}/conversation", json={"body": "LGTM overall"})).json()
    assert cm["author"] == "EikanWang" and sent[-1][1] == "repos/pytorch/pytorch/issues/7/comments"
    assert (await api.post(f"{base}/comments/11/reply", json={"body": "agreed"})).status_code == 201
    # drafts: line, range from the old side, whole file
    d1 = (await api.post(f"{base}/drafts", json={"path": "a.cpp", "side": "RIGHT", "line": 1, "line_text": "y", "body": "nit: name"})).json()
    await api.post(f"{base}/drafts", json={"path": "a.cpp", "side": "RIGHT", "line": 1, "start_side": "LEFT", "start_line": 1, "body": "range"})
    fd = (await api.post(f"{base}/drafts", json={"path": "b.md", "subject": "file", "body": "add a license header"})).json()
    assert fd["subject"] == "file" and fd["line"] == 0
    assert (await api.post(f"{base}/drafts", json={"path": "a.cpp", "side": "MIDDLE", "line": 1, "body": "x"})).status_code == 400
    assert (await api.patch(f"{base}/drafts/{d1['id']}", json={"body": "nit: rename"})).json()["body"] == "nit: rename"
    assert (await api.post(f"{base}/review", json={"event": "WHATEVER"})).status_code == 400
    # GitHub refusing a comment: the unfinished review is deleted there, drafts stay here
    gh["refuse"] = "AddThread"
    r = await api.post(f"{base}/review", json={"event": "COMMENT"})
    assert r.status_code == 502 and "could not be resolved" in r.json()["error"]["message"] and "(comment on a.cpp line 1)" in r.json()["error"]["message"]
    assert gh["mutations"][-1][0] == "DeleteReview"
    assert len((await api.get(f"{base}/comments")).json()["drafts"]) == 3
    gh.pop("refuse")
    gh["mutations"].clear()
    r = (await api.post(f"{base}/review", json={"event": "REQUEST_CHANGES", "body": "Please fix"})).json()
    assert r["published"] == 3 and r["url"].endswith("pullrequestreview-1")
    names = [m[0] for m in gh["mutations"]]
    assert names == ["AddReview", "AddThread", "AddThread", "AddThread", "Submit"]
    assert gh["mutations"][0][1]["commit"] == "abc"  # the diff the user saw
    th = [m[1] for m in gh["mutations"] if m[0] == "AddThread"]
    assert th[0] == {"review": "PRR_new", "path": "a.cpp", "body": "nit: rename", "line": 1, "side": "RIGHT", "subject": "LINE"}
    assert th[1]["startLine"] == 1 and th[1]["startSide"] == "LEFT"
    assert th[2] == {"review": "PRR_new", "path": "b.md", "body": "add a license header", "subject": "FILE"}
    assert gh["mutations"][-1][1] == {"review": "PRR_new", "event": "REQUEST_CHANGES", "body": "Please fix"}
    assert (await api.get(f"{base}/comments")).json()["drafts"] == []
    # a pending review started on github.com is reused (and announced)
    gh["conversation"]["pending"] = [{"id": "PRR_web", "comments": {"totalCount": 2}}]
    assert (await api.get(f"{base}/comments")).json()["pending_on_github"] == 2
    gh["mutations"].clear()
    r = (await api.post(f"{base}/review", json={"event": "COMMENT"})).json()
    assert r["from_github"] == 2 and [m[0] for m in gh["mutations"]] == ["Submit"] and gh["mutations"][0][1]["review"] == "PRR_web"
    # owner only
    from backend.app.models import LoginSession

    with h.ctx.db.write() as s:
        for row in s.query(LoginSession).all():
            row.identity = "token"
    assert (await api.get(f"{base}/comments")).status_code == 403
    assert (await api.put(f"{base}/viewed", json={"path": "a.cpp", "viewed": True})).status_code == 403
    assert (await api.post(f"{base}/review", json={"event": "APPROVE"})).status_code == 403


def requested_by(p, login, *, company=None, email=None, owner="EikanWang"):
    p["timelineItems"] = {"nodes": [{"actor": {"login": login, "company": company, "email": email},
                                     "requestedReviewer": {"__typename": "User", "login": owner}}]}
    return p


async def test_review_prefs_priority_hidden_and_watched_repos(running_app):
    import asyncio

    github_hub.reset_cache()
    calls: list = []
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    closed = pr(5)
    closed["state"] = "MERGED"
    queue = [requested_by(pr(1), "metaguy", company="@Meta"), requested_by(pr(2), "fbdev", email="x@fb.com"),
             requested_by(pr(3), "chosen"), requested_by(pr(4, "intel/torch-xpu-ops"), "other", company="Intel"), closed]
    watched = [pr(1), pr(9, "intel/torch-xpu-ops", user=None)]  # #1 is already in the queue: not repeated

    def graphql(q, v):
        calls.append(v["q"])
        nodes = watched if "repo:" in v["q"] else queue
        return {"search": {"issueCount": len(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}

    h.ctx.extra["gh_graphql"] = graphql
    api = h.api
    r = (await api.get("/api/v1/github/reviews")).json()
    by = {i["number"]: i for i in r["items"]}
    assert 5 not in by  # closed / merged meanwhile: dropped
    assert by[1]["priority"] and by[2]["priority"] and not by[3]["priority"] and not by[4]["priority"]
    assert by[1]["requested_by"] == "metaguy" and by[1]["requested_by_company"] == "@Meta"
    assert r["prefs"] == {"hidden_repos": [], "watched_repos": [], "priority_requesters": [], "priority_meta": True} and r["watched"] == []
    # priority people, no Meta rule; hide a repository; watch one
    assert (await api.put("/api/v1/github/prefs", json={"priority_requesters": ["chosen"], "priority_meta": False})).status_code == 200
    r = (await api.get("/api/v1/github/reviews")).json()
    assert [i["number"] for i in r["items"] if i["priority"]] == [3]
    assert (await api.put("/api/v1/github/prefs", json={"hidden_repos": ["Intel/Torch-XPU-Ops"]})).status_code == 200
    r = (await api.get("/api/v1/github/reviews")).json()
    assert sorted(i["number"] for i in r["items"]) == [1, 2, 3] and r["hidden_count"] == 1
    assert (await api.put("/api/v1/github/prefs", json={"hidden_repos": [], "watched_repos": ["intel/torch-xpu-ops", "pytorch/pytorch"]})).status_code == 200
    await asyncio.wait_for(github_hub._tasks["EikanWang"], 5)
    r = (await api.get("/api/v1/github/reviews")).json()
    assert [(i["number"], i["requested"], i["watched"]) for i in r["watched"]] == [(9, None, True)]
    assert any("repo:intel/torch-xpu-ops" in q for q in calls) and any("repo:pytorch/pytorch" in q for q in calls)
    # stored in the database (kv_settings), validated
    assert (await api.put("/api/v1/github/prefs", json={"watched_repos": ["no-slash"]})).json()["error"]["code"] == "INVALID_REPO"
    assert (await api.put("/api/v1/github/prefs", json={"priority_requesters": ["bad login!"]})).json()["error"]["code"] == "INVALID_LOGIN"
    github_hub.reset_cache()
    assert (await api.get("/api/v1/github/reviews")).json()["prefs"]["watched_repos"] == ["intel/torch-xpu-ops", "pytorch/pytorch"]
    # only the owner
    with h.ctx.db.write() as s:
        for row in s.query(LoginSession).all():
            row.identity = "token"
    assert (await api.put("/api/v1/github/prefs", json={"hidden_repos": []})).status_code == 403


async def test_extra_context_folders_reach_the_agent_prompt(running_app, workspace, tmp_path):
    h = await running_app()
    api = h.api
    ws = (await api.get("/api/v1/workspaces")).json()["workspaces"][0]["id"]
    ref = tmp_path / "refs"
    ref.mkdir()
    assert (await api.get(f"/api/v1/agent-context?scope=ws:{ws}")).json() == {"scope": f"ws:{ws}", "dirs": []}
    r = await api.put(f"/api/v1/agent-context?scope=ws:{ws}", json={"dirs": [str(ref), str(ref)]})
    assert r.status_code == 200 and r.json()["dirs"] == [str(ref.resolve())]
    assert (await api.put(f"/api/v1/agent-context?scope=ws:{ws}", json={"dirs": ["relative/x"]})).json()["error"]["code"] == "INVALID_PATH"
    assert (await api.put(f"/api/v1/agent-context?scope=ws:{ws}", json={"dirs": [str(tmp_path / "nope")]})).json()["error"]["code"] == "FOLDER_NOT_FOUND"
    assert (await api.get("/api/v1/agent-context?scope=bad")).json()["error"]["code"] == "INVALID_SCOPE"
    # a repository's folders: only the GitHub owner may set them
    github_hub.reset_cache()
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    assert (await api.put("/api/v1/agent-context?scope=repo:pytorch/pytorch", json={"dirs": [str(ref)]})).status_code == 403
    # a question about a document names the folders
    (workspace / "ctx.md").write_text("# Ctx\n\nSome text here.\n")
    d = (await api.get("/api/v1/documents/open?path=ctx.md")).json()
    r = await api.post(f"/api/v1/documents/{d['document_id']}/threads", json={"whole_document": True, "version_sha256": d["sha256"], "question": "Q?"})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    from backend.app.models import Job

    with h.ctx.db.read() as s:
        prompt = s.query(Job).order_by(Job.created_at.desc()).first().prompt
    assert "Additional reference folders the user added for more accurate answers" in prompt and str(ref.resolve()) in prompt


def test_rate_guard_waits_or_refuses_near_the_limit(monkeypatch):
    import time as _t

    g = github_hub.RateGuard()
    slept: list = []
    monkeypatch.setattr(github_hub.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(g, "_refresh", lambda: setattr(g, "checked", _t.time()))
    g.checked = _t.time()
    g.limits = {"graphql": {"remaining": 5000, "reset": int(_t.time()) + 3000, "limit": 5000}}
    g.before("graphql")
    assert slept == [] and g.limits["graphql"]["remaining"] == 4999
    g.limits["graphql"] = {"remaining": 50, "reset": int(_t.time()) + 30, "limit": 5000}
    g.before("graphql")  # reset is near: wait for it
    assert slept and 25 < slept[0] <= 33
    g.limits["graphql"] = {"remaining": 50, "reset": int(_t.time()) + 1800, "limit": 5000}
    try:
        g.before("graphql")
    except ApiError as e:
        assert e.status == 429 and e.code == "GH_RATE_LIMITED"
    else:
        raise AssertionError("not refused")
    slept.clear()
    g.limits = {}
    g.before("core", mutation=True)
    g.before("core", mutation=True)  # mutations are spaced out
    assert slept and slept[-1] > 0.5


async def test_pr_contributors_roles_affiliation_and_details(running_app):
    github_hub.reset_cache()
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    meta = {"login": "mdev", "name": "M Dev", "company": None, "location": None, "bio": None, "websiteUrl": None, "twitterUsername": None,
            "url": "https://github.com/mdev", "avatarUrl": None, "createdAt": None, "organizations": {"nodes": [{"login": "meta-pytorch", "name": None}]},
            "followers": {"totalCount": 1}}
    calls: list = []

    def graphql(q, v):
        calls.append(q)
        assert "email" not in q.split("authors")[0].split("author")[1].split("}")[0]  # needs a token scope gh lacks
        return {"repository": {"pullRequest": {"author": meta, "commits": {"totalCount": 3, "nodes": [
            {"commit": {"authors": {"nodes": [{"name": "M Dev", "email": "m@fb.com", "user": meta}, {"name": "X", "email": "x@intel.com", "user": None}]}}},
            {"commit": {"authors": {"nodes": [{"name": "M Dev", "email": "123+mdev@users.noreply.github.com", "user": meta}]}}},
            {"commit": {"authors": {"nodes": [{"name": "Other", "email": "o@nvidia.com", "user": None}]}}}]}}}}

    rest_calls: list = []
    h.ctx.extra["gh_graphql"] = graphql
    h.ctx.extra["gh_rest"] = lambda m, p, b=None: rest_calls.append(p) or {"email": "public@meta.com", "blog": "m.dev", "hireable": True}
    r = (await h.api.get("/api/v1/github/pr/pytorch/pytorch/5/contributors")).json()
    by = {(c["login"] or c["name"]): c for c in r["contributors"]}
    assert list(by) == ["mdev", "Other", "X"] and r["commits_total"] == 3
    assert by["mdev"]["roles"] == ["author", "commit author"] and by["mdev"]["commits"] == 2
    assert by["mdev"]["commit_emails"] == ["m@fb.com"] and by["mdev"]["affiliation"] == ["Meta"]  # noreply hidden
    assert by["X"]["roles"] == ["co-author"] and by["X"]["affiliation"] == ["Intel"] and by["Other"]["affiliation"] == ["NVIDIA"]
    assert rest_calls == [] and by["mdev"]["public_email"] is None
    r = (await h.api.get("/api/v1/github/pr/pytorch/pytorch/5/contributors?details=true")).json()
    mdev = r["contributors"][0]
    assert mdev["public_email"] == "public@meta.com" and mdev["website"] == "m.dev" and rest_calls == ["users/mdev"] and len(calls) == 1  # cached


async def test_agent_review_suggestions_apply_discard_and_publish(running_app, workspace, fake_env):
    github_hub.reset_cache()
    calls: list = []
    gh: dict = {}
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    h.ctx.extra["gh_graphql"] = fake_pr_detail(calls, gh)
    h.ctx.extra["gh_pr_files"] = lambda o, r, n: [
        {"filename": "aten/gemm.cpp", "status": "modified", "additions": 3, "deletions": 1, "patch": "@@ -1,2 +1,4 @@\n-old\n+fast\n+tiled\n+done"}]
    api = h.api
    assert (await api.get("/api/v1/github/pr/pytorch/pytorch/7")).status_code == 200
    ws = (await api.get("/api/v1/workspaces")).json()["workspaces"][0]["id"]
    await api.put("/api/v1/github/repo-wiki", json={"repo": "pytorch/pytorch", "workspace_id": ws})
    d = (await api.get("/api/v1/documents/open?path=.waw-github/pytorch/pytorch/7.md")).json()
    r = await api.post(f"/api/v1/documents/{d['document_id']}/threads", json={"whole_document": True, "version_sha256": d["sha256"],
                       "question": "Review this pull request.", "pr_review": True})
    assert r.status_code == 201, r.text
    thread_id = r.json()["thread_id"]
    await api.wait_job(r.json()["job_id"])
    from backend.app.models import Job, Message

    with h.ctx.db.read() as s:
        prompt = s.query(Job).order_by(Job.created_at.desc()).first().prompt
    assert "fenced block tagged waw-review" in prompt and "          1 |+fast" in prompt and "    1       |-old" in prompt
    base = "/api/v1/github/pr/pytorch/pytorch/7"
    c = (await api.get(f"{base}/comments")).json()
    assert c["agent_reviews"][0]["state"] == "done" and c["agent_reviews"][0]["added"] == 2 and "not/in/pr.cpp" in c["agent_reviews"][0]["problem"]
    ds = {x["subject"]: x for x in c["drafts"]}
    line, filec = ds["line"], ds["file"]
    assert line["origin"] == "agent" and line["decision"] == "proposed" and line["thread_id"] == thread_id
    assert (line["path"], line["side"], line["line"], line["line_text"]) == ("aten/gemm.cpp", "RIGHT", 1, "+fast")
    assert filec["body"] == "Line 9999: Agent: consider a test."
    # imported once; the discussion shows a note instead of the JSON
    assert len((await api.get(f"{base}/comments")).json()["drafts"]) == 2
    with h.ctx.db.read() as s:
        msg = s.query(Message).filter_by(thread_id=thread_id, role="agent").one().content
    assert "waw-review" not in msg and "2 suggested comment(s) were added to the diff" in msg
    # nothing undecided is published: apply one, discard the other
    assert (await api.patch(f"{base}/drafts/{line['id']}", json={"decision": "maybe"})).status_code == 400
    assert (await api.patch(f"{base}/drafts/{line['id']}", json={"decision": "accepted"})).json()["decision"] == "accepted"
    assert (await api.patch(f"{base}/drafts/{filec['id']}", json={"decision": "discarded"})).json()["decision"] == "discarded"
    r = (await api.post(f"{base}/review", json={"event": "COMMENT"})).json()
    assert r["published"] == 1 and r["undecided"] == 0
    threads = [v for n, v in gh["mutations"] if n == "AddThread"]
    assert len(threads) == 1 and threads[0]["body"] == "Agent: is this guard needed?" and threads[0]["line"] == 1
    assert (await api.get(f"{base}/comments")).json()["drafts"] == []


def test_review_block_parsing_and_diff_lines():
    items, problem = github_hub.parse_review_block("x\n```waw-review\n[{\"path\": \"a\", \"line\": 2, \"body\": \"b\"}]\n```")
    assert problem is None and items[0]["path"] == "a"
    assert github_hub.parse_review_block("no block")[1] and github_hub.parse_review_block("```waw-review\nnot json\n```")[1]
    m = github_hub.diff_line_map("@@ -3,3 +3,3 @@\n ctx\n-gone\n+new\n tail")
    assert m == {("RIGHT", 3): " ctx", ("LEFT", 3): " ctx", ("LEFT", 4): "-gone", ("RIGHT", 4): "+new", ("RIGHT", 5): " tail", ("LEFT", 5): " tail"}
    drafts, bad = github_hub.agent_drafts([{"path": "f", "side": "RIGHT", "line": 5, "start_line": 3, "body": "range"},
                                           {"path": "f", "side": "LEFT", "line": 4, "start_line": 9, "body": "bad start"}],
                                          [{"path": "f", "unified": "@@ -3,3 +3,3 @@\n ctx\n-gone\n+new\n tail"}])
    assert drafts[0]["start_line"] == 3 and drafts[0]["line_text"] == " ctx\n+new\n tail" and drafts[1]["start_line"] is None and not bad


async def test_local_checkout_fetches_the_pr_and_is_the_agents_reference(running_app, tmp_path):
    import asyncio
    import subprocess

    def g(cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    up = tmp_path / "upstream"
    up.mkdir()
    g(up, "init", "-q", "-b", "main")
    g(up, "config", "user.email", "t@e")
    g(up, "config", "user.name", "T")
    (up / "gemm.cpp").write_text("old\n")
    g(up, "add", "-A")
    g(up, "commit", "-qm", "base")
    local = tmp_path / "checkout"
    g(tmp_path, "clone", "-q", str(up), str(local))
    (up / "gemm.cpp").write_text("old\nnewer main\n")
    g(up, "commit", "-qam", "main moves on")
    g(up, "checkout", "-qb", "pr7")
    (up / "gemm.cpp").write_text("old\nnewer main\nfast\n")
    g(up, "commit", "-qam", "the PR")
    pr_sha = g(up, "rev-parse", "HEAD")
    g(up, "update-ref", "refs/pull/7/head", pr_sha)
    g(up, "checkout", "-q", "main")

    github_hub.reset_cache()
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    h.ctx.extra["gh_graphql"] = fake_pr_detail([])
    h.ctx.extra["gh_pr_files"] = lambda o, r, n: [{"filename": "gemm.cpp", "status": "modified", "additions": 1, "deletions": 0, "patch": "@@ -1,2 +1,3 @@\n old\n newer main\n+fast"}]
    api = h.api
    assert (await api.put("/api/v1/github/local-repo", json={"repo": "pytorch/pytorch", "path": "relative"})).json()["error"]["code"] == "INVALID_PATH"
    assert (await api.put("/api/v1/github/local-repo", json={"repo": "pytorch/pytorch", "path": str(tmp_path)})).json()["error"]["code"] == "NOT_A_GIT_REPO"
    r = (await api.put("/api/v1/github/local-repo", json={"repo": "pytorch/pytorch", "path": str(local), "remote": str(up)})).json()
    assert r["link"] == {"path": str(local), "update": True, "remote": str(up)}
    assert (await api.get("/api/v1/github/local-repo?repo=pytorch/pytorch")).json()["link"]["path"] == str(local)
    base = "/api/v1/github/pr/pytorch/pytorch/7"
    assert (await api.post(f"{base}/local-sync")).json()["state"] == "running"
    for _ in range(100):
        st = (await api.get(f"{base}/local-sync")).json()
        if st["state"] != "running":
            break
        await asyncio.sleep(0.1)
    assert st["state"] == "done" and st["sha"] == pr_sha and st["ref"] == "refs/waw/pr/7", st
    assert st["base_updated"] is True and "main fast-forwarded" in st["notes"]  # update is on by default; clean, on main
    assert (local / "gemm.cpp").read_text() == "old\nnewer main\n"  # the PR is never checked out
    assert g(local, "show", "refs/waw/pr/7:gemm.cpp").endswith("fast")
    # the agent is told about the checkout and the PR ref
    ws = (await api.get("/api/v1/workspaces")).json()["workspaces"][0]["id"]
    await api.put("/api/v1/github/repo-wiki", json={"repo": "pytorch/pytorch", "workspace_id": ws})
    assert (await api.get(base)).status_code == 200  # writes the PR copy
    d = (await api.get("/api/v1/documents/open?path=.waw-github/pytorch/pytorch/7.md")).json()
    r = await api.post(f"/api/v1/documents/{d['document_id']}/threads", json={"whole_document": True, "version_sha256": d["sha256"], "question": "ok?"})
    await api.wait_job(r.json()["job_id"])
    from backend.app.models import Job

    with h.ctx.db.read() as s:
        prompt = s.query(Job).order_by(Job.created_at.desc()).first().prompt
    assert f"checked out locally at {local}" in prompt and f"git -C {local} show refs/waw/pr/7:<path>" in prompt and "read-only" in prompt
    # with update off and local changes, nothing in the working tree moves
    (up / "gemm.cpp").write_text("old\nnewer main\nmain again\n")
    g(up, "commit", "-qam", "main again")
    (local / "gemm.cpp").write_text("my local edit\n")
    await api.put("/api/v1/github/local-repo", json={"repo": "pytorch/pytorch", "path": str(local), "remote": str(up)})
    await api.post(f"{base}/local-sync")
    for _ in range(100):
        st = (await api.get(f"{base}/local-sync")).json()
        if st["state"] != "running":
            break
        await asyncio.sleep(0.1)
    assert st["state"] == "done" and st["base_updated"] is False and "local changes" in st["notes"][0]
    assert (local / "gemm.cpp").read_text() == "my local edit\n"
    await api.put("/api/v1/github/local-repo", json={"repo": "pytorch/pytorch", "path": str(local), "remote": str(up), "update": False})
    await api.post(f"{base}/local-sync")
    for _ in range(100):
        st = (await api.get(f"{base}/local-sync")).json()
        if st["state"] != "running":
            break
        await asyncio.sleep(0.1)
    assert st["state"] == "done" and st["base_updated"] is None and st["notes"] == []
    # summarize the repository's changes over a period from the checkout's git log: a chat in the repo's wiki
    assert (await api.post("/api/v1/github/repo-summary", json={"repo": "pytorch/pytorch", "period": "fortnight"})).json()["error"]["code"] == "INVALID_PERIOD"
    r = await api.post("/api/v1/github/repo-summary", json={"repo": "pytorch/pytorch", "period": "week", "focus": "XPU"})
    assert r.status_code == 201, r.text
    summ = r.json()
    assert summ["path"].startswith(".waw-chat/pytorch-pytorch-changes-week-") and summ["commits"] == 3 and summ["workspace_id"] == ws
    await api.wait_job(summ["job_id"])
    with h.ctx.db.read() as s:
        prompt = s.query(Job).order_by(Job.created_at.desc()).first().prompt
    assert "Summarize the key changes of pytorch/pytorch in the week" in prompt and "Focus: XPU" in prompt
    assert "Material: the git history of pytorch/pytorch" in prompt and "\tmain again" in prompt and "\tthe PR" not in prompt
    assert "(top level)" in prompt and f"git -C {local} show --stat <hash>" in prompt
    chats = (await api.get("/api/v1/chats")).json()["chats"]
    assert any(c["path"] == summ["path"] and c["title"].startswith("pytorch/pytorch changes · week") for c in chats)
    assert (await api.delete("/api/v1/github/local-repo?repo=pytorch/pytorch")).json()["link"] is None
    assert (await api.get(f"{base}/local-sync")).json() == {"state": "none"}


async def test_mention_candidates_participants_first_then_repository(running_app):
    github_hub.reset_cache()
    calls: list = []
    gh: dict = {"conversation": {"reviews": [], "comments": [{"databaseId": 1, "author": {"login": "carol"}, "body": "x", "createdAt": "2026-11-01T00:00:00Z", "url": None},
                                                            {"databaseId": 2, "author": {"login": "pytorch-bot[bot]"}, "body": "y", "createdAt": "2026-11-01T00:00:00Z", "url": None}],
                                 "pending": []}}
    base_graphql = fake_pr_detail(calls, gh)

    def graphql(q, v):
        if "query Mentionable" in q:
            calls.append(("mention", v["q"]))
            return {"repository": {"mentionableUsers": {"nodes": [{"login": "albanD", "name": "Alban"}, {"login": "alice", "name": "Alice"}]}}}
        return base_graphql(q, v)

    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    h.ctx.extra["gh_graphql"] = graphql
    h.ctx.extra["gh_pr_files"] = lambda o, r, n: []
    api = h.api
    base = "/api/v1/github/pr/pytorch/pytorch/7"
    await api.get(base)
    await api.get(f"{base}/comments")
    r = (await api.get(f"{base}/mentions?q=")).json()["candidates"]
    assert [(c["login"], c["source"]) for c in r] == [("alice", "author"), ("bob", "reviewer"), ("EikanWang", "requested"), ("carol", "commenter")]
    r = (await api.get(f"{base}/mentions?q=al")).json()["candidates"]
    assert [c["login"] for c in r] == ["alice", "albanD"]  # the author first, then the repository's users (deduplicated)
    assert (await api.get(f"{base}/mentions?q=al")).json()["candidates"] == r and calls.count(("mention", "al")) == 1  # cached
    assert (await api.get(f"{base}/mentions?q=a%20b")).json()["candidates"] == []
