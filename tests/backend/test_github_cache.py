"""The local PR Review cache: a tab's first load from the cache (no GitHub call before the answer), the same fetch in
the background with an event when something changed, every per-load side effect kept, actions mark it stale,
prefetch from the review list (rate-limit aware), folder / permissions / cleanup / clear / move."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time

from test_github_hub import as_github, fake_pr_detail, pr

from backend.app import github_cache as gc
from backend.app import github_hub

FILES = [{"filename": "aten/gemm.cpp", "status": "modified", "additions": 3, "deletions": 1, "patch": "@@ -1,2 +1,4 @@\n-old\n+fast\n+tiled\n+done"}]
BASE = "/api/v1/github/pr/pytorch/pytorch/7"


async def setup(running_app, gh=None):
    github_hub.reset_cache()
    calls: list = []
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    h.ctx.extra["gh_graphql"] = fake_pr_detail(calls, gh if gh is not None else {})
    h.ctx.extra["gh_pr_files"] = lambda o, r, n: FILES
    events: list = []
    orig = h.ctx.events.publish
    h.ctx.events.publish = lambda topic, ev: (events.append(ev), orig(topic, ev))[1]
    return h, calls, events


async def wait_event(events, part, n=1, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        got = [e for e in events if e.get("type") == "github_cache" and e["part"] == part]
        if len(got) >= n:
            return got[n - 1]
        await asyncio.sleep(0.05)
    raise AssertionError(f"no github_cache event for {part}")


async def test_first_load_from_the_cache_then_the_same_fetch_in_the_background(running_app):
    gh: dict = {}
    h, calls, events = await setup(running_app, gh)
    api = h.api
    first = (await api.get(BASE, params={"cached": "true"})).json()  # nothing cached yet: from GitHub, as before
    assert first["cache"]["source"] == "github" and first["pr"]["title"] == "[xpu] Faster GEMM"
    c1 = (await api.get(f"{BASE}/comments", params={"cached": "true"})).json()
    assert c1["cache"]["source"] == "github"
    token = first["cache"]["token"]
    assert len(token) == 20 and "pytorch" not in token
    github_hub._pr_cache.clear()  # another session later (or a restart): not in memory any more
    n = len(calls)
    again = (await api.get(BASE, params={"cached": "true"})).json()
    assert again["cache"]["source"] == "cache" and again["pr"] == first["pr"] and again["files"] == first["files"]
    assert again["doc_path"] == first["doc_path"] and "wiki_workspace_id" in again
    ev = await wait_event(events, "pr")
    assert ev["token"] == token and ev["changed"] is False and len(calls) > n  # checked on GitHub right after
    assert "repo" not in json.dumps(ev) and "pytorch" not in json.dumps(ev)
    # the tab's normal reload: in memory now (fetched in this process)
    assert (await api.get(BASE)).json()["cache"]["source"] == "memory"


async def test_a_change_on_github_reaches_the_tab_and_the_agent_copy_stays_consistent(running_app, workspace):
    gh: dict = {}
    h, _calls, events = await setup(running_app, gh)
    api = h.api
    ws = (await api.get("/api/v1/workspaces")).json()["workspaces"][0]["id"]
    await api.put("/api/v1/github/repo-wiki", json={"repo": "pytorch/pytorch", "workspace_id": ws})
    await api.get(BASE)
    snap = h.ctx.settings.snapshots_dir / "github" / "pytorch" / "pytorch" / "7.md"
    before = snap.read_text()
    # GitHub changes (a new file in the PR); the tab opens from the cache — the agent's copy is left as it is
    FILES.append({"filename": "c10/new.cpp", "status": "added", "additions": 1, "deletions": 0, "patch": "@@ -0,0 +1 @@\n+x"})
    try:
        github_hub._pr_cache.clear()
        r = (await api.get(BASE, params={"cached": "true"})).json()
        assert r["cache"]["source"] == "cache" and len(r["files"]) == 1 and snap.read_text() == before
        ev = await wait_event(events, "pr")
        assert ev["changed"] is True and snap.read_text() == before  # the background never rewrites the copy
        # the tab reloads (normal load): the new files, and the copy written now — then the tab re-opens it
        r = (await api.get(BASE)).json()
        assert len(r["files"]) == 2 and "c10/new.cpp" in snap.read_text()
        d = (await api.get("/api/v1/documents/open", params={"path": r["doc_path"]})).json()
        t = await api.post(f"/api/v1/documents/{d['document_id']}/threads", json={"whole_document": True, "version_sha256": d["sha256"], "question": "ok?"})
        assert t.status_code == 201, t.text  # no version conflict
        # unchanged data: a load leaves the copy (and its version) alone
        mtime = snap.stat().st_mtime_ns
        await api.get(BASE)
        assert snap.stat().st_mtime_ns == mtime
    finally:
        FILES.pop()


async def test_cached_comments_keep_every_side_effect_and_actions_mark_the_cache_stale(running_app):
    gh: dict = {}
    h, calls, events = await setup(running_app, gh)
    api = h.api
    await api.get(BASE)
    await api.get(f"{BASE}/comments")
    disc = h.ctx.settings.snapshots_dir / "github" / "pytorch" / "pytorch" / "7.discussion.md"
    disc.unlink()
    # a draft written here is live from the database even when the comments come from the cache
    assert (await api.post(f"{BASE}/drafts", json={"path": "aten/gemm.cpp", "side": "RIGHT", "line": 1, "body": "mine"})).status_code in (200, 201)
    c = (await api.get(f"{BASE}/comments", params={"cached": "true"})).json()
    assert c["cache"]["source"] == "cache" and [d["body"] for d in c["drafts"]] == ["mine"]
    assert disc.is_file()  # the agent's copy of the discussion is still written
    await wait_event(events, "comments")
    # an action that changes the PR marks the cached copy stale: the next first load goes to GitHub, before answering
    rest: list = []
    h.ctx.extra["gh_rest"] = lambda m, p, b=None: rest.append((m, p)) or {}
    assert (await api.post(f"{BASE}/state", json={"action": "close"})).status_code == 200
    github_hub._pr_cache.clear()
    n = len(calls)
    r = (await api.get(BASE, params={"cached": "true"})).json()
    assert r["cache"]["source"] == "github" and len(calls) > n
    # viewed marks you set are mirrored in the cached copy
    await api.put(f"{BASE}/viewed", json={"path": "aten/gemm.cpp", "viewed": True})
    hit = gc.get(h.ctx, "pytorch/pytorch#7", "pr")
    assert hit and hit[0]["files"][0]["viewed"] == "VIEWED"
    # ?refresh=true is always GitHub
    n = len(calls)
    assert (await api.get(BASE, params={"refresh": "true", "cached": "true"})).json()["cache"]["source"] == "github" and len(calls) > n


async def test_prefetch_from_the_review_list_rate_limit_aware(running_app):
    github_hub.reset_cache()
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    detail = fake_pr_detail([], {})
    queue = [pr(7)]

    def graphql(q, v):
        if "search(" in q:
            return {"search": {"issueCount": 1, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": queue}}
        return detail(q, v)
    h.ctx.extra["gh_graphql"] = graphql
    h.ctx.extra["gh_pr_files"] = lambda o, r, n: FILES
    api = h.api
    await api.get("/api/v1/github/reviews")
    assert gc.get(h.ctx, "pytorch/pytorch#7", "pr") is None  # tests: off unless asked
    h.ctx.extra["gh_cache_prefetch"] = True
    old_status = github_hub.RATE.status
    github_hub.RATE.status = lambda: {"graphql": {"remaining": 50, "reset": 0, "limit": 5000}}
    try:
        await api.get("/api/v1/github/reviews")
        await asyncio.sleep(0.3)
        assert gc.get(h.ctx, "pytorch/pytorch#7", "pr") is None  # quota low: nothing fetched
    finally:
        github_hub.RATE.status = old_status
    await api.get("/api/v1/github/reviews")
    for _ in range(100):
        if gc.get(h.ctx, "pytorch/pytorch#7", "pr") and gc.get(h.ctx, "pytorch/pytorch#7", "comments"):
            break
        await asyncio.sleep(0.05)
    assert gc.get(h.ctx, "pytorch/pytorch#7", "pr")[0]["pr"]["number"] == 7
    # opening it now: from the cache at once
    github_hub._pr_cache.clear()
    assert (await api.get(BASE, params={"cached": "true"})).json()["cache"]["source"] == "cache"
    # prefetch off in the settings
    assert (await api.put("/api/v1/github/cache", json={"prefetch": False})).json()["prefetch"] is False


async def test_folder_permissions_cleanup_clear_and_moving(running_app, tmp_path, workspace):
    gh: dict = {}
    h, _calls, _events = await setup(running_app, gh)
    api = h.api
    await api.get(BASE)
    info = (await api.get("/api/v1/github/cache")).json()
    root = gc.root(h.ctx)
    assert info["dir"] == str(h.ctx.settings.state_dir / "github-cache") and info["prs"] == 1 and info["bytes"] > 0
    f = root / "pytorch" / "pytorch" / "7" / "pr.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert not list(root.rglob("*.tmp"))
    # file contents at a commit are kept and served from the cache
    gc.file_put(h.ctx, "pytorch/pytorch", "abc", "a.txt", "hello\n")
    assert gc.file_get(h.ctx, "pytorch/pytorch", "abc", "a.txt") == "hello\n"
    # closed PRs go after a week; then the oldest opened go first over the cap
    m = json.loads((f.parent / "meta.json").read_text())
    m.update(state="MERGED", fetched={"pr": time.time() - 8 * 86400})
    (f.parent / "meta.json").write_text(json.dumps(m))
    assert gc.cleanup(h.ctx)["removed"] == 1 and (await api.get("/api/v1/github/cache")).json()["prs"] == 0
    github_hub._pr_cache.clear()
    await api.get(BASE)
    (await api.put("/api/v1/github/cache", json={"cap_mb": 0}))
    # move the folder: the cached PR goes along; the old folder is gone; a folder inside a wiki is refused
    new = tmp_path / "elsewhere" / "cache"
    r = (await api.put("/api/v1/github/cache", json={"dir": str(new)})).json()
    assert r["dir"] == str(new) and r["prs"] == 1 and not root.exists() and (new / "pytorch" / "pytorch" / "7" / "pr.json").is_file()
    assert stat.S_IMODE(os.stat(new).st_mode) == 0o700
    assert (await api.put("/api/v1/github/cache", json={"dir": str(workspace / "cache")})).json()["error"]["code"] == "INVALID_PATH"
    assert (await api.put("/api/v1/github/cache", json={"dir": "relative/x"})).json()["error"]["code"] == "INVALID_PATH"
    github_hub._pr_cache.clear()
    assert (await api.get(BASE, params={"cached": "true"})).json()["cache"]["source"] == "cache"
    # clear
    assert (await api.post("/api/v1/github/cache/clear")).json()["prs"] == 0
    github_hub._pr_cache.clear()
    assert (await api.get(BASE, params={"cached": "true"})).json()["cache"]["source"] == "github"
    # a token login never sees it
    as_github(h, "someone-else")
    assert (await api.get("/api/v1/github/cache")).status_code == 403


async def test_issues_are_cached_like_pull_requests(running_app):
    h, _, events = await setup(running_app)
    state = {"title": "XPU crash", "comments": [], "n": 0}

    def graphql(q, v):
        assert "query Issue" in q
        state["n"] += 1
        return {"repository": {"nameWithOwner": "pytorch/pytorch", "issue": {
            "number": v["number"], "title": state["title"], "url": "https://github.com/pytorch/pytorch/issues/55", "body": "b", "state": "OPEN",
            "stateReason": None, "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-02T00:00:00Z", "author": {"login": "alice"},
            "labels": {"nodes": []}, "assignees": {"nodes": []},
            "comments": {"totalCount": len(state["comments"]), "nodes": state["comments"]}}}}
    h.ctx.extra["gh_graphql"] = graphql
    posted = []
    h.ctx.extra["gh_rest"] = lambda m, p, b=None: (posted.append((m, p)), {"id": 9})[1]
    url = "/api/v1/github/issue/pytorch/pytorch/55"
    api = h.api
    first = (await api.get(url, params={"cached": "true"})).json()  # nothing cached: GitHub
    assert first["cache"]["source"] == "github" and first["title"] == "XPU crash" and state["n"] == 1
    again = (await api.get(url, params={"cached": "true"})).json()  # the local copy, checked on GitHub right after
    assert again["cache"]["source"] == "cache" and again["title"] == "XPU crash"
    ev = await wait_event(events, "issue")
    assert ev["token"] == first["cache"]["token"] and ev["changed"] is False and state["n"] == 2
    state["title"] = "XPU crash on resize"  # changed on GitHub: the check says so, the next first load shows it
    (await api.get(url, params={"cached": "true"})).json()
    assert (await wait_event(events, "issue", 2))["changed"] is True
    assert (await api.get(url, params={"cached": "true"})).json()["title"] == "XPU crash on resize"
    # your comment: the cached copy is not used again until fetched
    assert (await api.post(f"{url}/comments", json={"body": "looking"})).status_code == 201
    assert (await api.get(url, params={"cached": "true"})).json()["cache"]["source"] == "github"
    assert gc.usage(h.ctx)["issues"] == 1 and gc.usage(h.ctx)["prs"] == 0
