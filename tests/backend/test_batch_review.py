"""Agent review of several PRs at once: queue, cap, wiki for repositories without one, skips, cancel, failures."""

from __future__ import annotations

import asyncio
import time

from backend.app import batch_review, github_hub

from test_github_hub import as_github, fake_pr_detail


def multi_pr_detail(calls):
    """fake_pr_detail, but the PR (number, repository) is the one asked for."""
    base = fake_pr_detail(calls)

    def graphql(q, v):
        d = base(q, v)
        if "query PrDetail" in q:
            r = d["repository"]
            r["nameWithOwner"] = f"{v['owner']}/{v['name']}"
            r["pullRequest"] = {**r["pullRequest"], "number": v["number"], "id": f"PR_{v['number']}", "title": f"PR {v['number']}",
                                "url": f"https://github.com/{v['owner']}/{v['name']}/pull/{v['number']}"}
        return d
    return graphql


async def _setup(running_app):
    github_hub.reset_cache()
    calls: list = []
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    h.ctx.extra["gh_graphql"] = multi_pr_detail(calls)
    h.ctx.extra["gh_pr_files"] = lambda o, r, n: [
        {"filename": "aten/gemm.cpp", "status": "modified", "additions": 3, "deletions": 1, "patch": "@@ -1,2 +1,4 @@\n-old\n+fast\n+tiled\n+done"}]
    ws = (await h.api.get("/api/v1/workspaces")).json()["workspaces"][0]["id"]
    return h, ws


async def _drain(timeout: float = 60) -> None:
    end = time.monotonic() + timeout
    while (batch_review._queue or batch_review._running) and time.monotonic() < end:
        await asyncio.sleep(0.1)
    assert not batch_review._queue and not batch_review._running, batch_review.status()


async def test_batch_review_runs_each_pr_like_the_tab_button(running_app, workspace, fake_env):
    h, ws = await _setup(running_app)
    api = h.api
    prs = [{"repo": "pytorch/pytorch", "number": 7}, {"repo": "pytorch/pytorch", "number": 8},
           {"repo": "intel/torch-xpu-ops", "number": 3}, {"repo": "pytorch/pytorch", "number": 7}]  # a duplicate is ignored
    r = await api.post("/api/v1/github/batch-review", json={"prs": prs, "workspace_id": ws})
    assert r.status_code == 202, r.text
    assert r.json()["queued"] == ["pytorch/pytorch#7", "pytorch/pytorch#8", "intel/torch-xpu-ops#3"] and r.json()["skipped"] == []
    await _drain()
    assert batch_review.status()["failed"] == {}
    # each one is a normal agent review: waiting for you with its suggestions, brought in when the PR is opened
    states = github_hub.agent_review_states(h.ctx)
    for key in ("pytorch/pytorch#7", "pytorch/pytorch#8", "intel/torch-xpu-ops#3"):
        assert states[key]["state"] == "waiting", (key, states)
    c = (await api.get("/api/v1/github/pr/intel/torch-xpu-ops/3/comments")).json()
    assert c["agent_reviews"][0]["state"] == "done" and {d["origin"] for d in c["drafts"]} == {"agent"}
    # repositories without a wiki got the given one, as if chosen in a PR tab
    got = {w["repo"]: w["workspace_id"] for w in (await api.get("/api/v1/github/repo-wikis")).json()["repos"]}
    assert got.get("intel/torch-xpu-ops") == ws and got.get("pytorch/pytorch") == ws
    # the review prompt is the PR review one (waw-review block, the PR's diff)
    from backend.app.models import Job

    with h.ctx.db.read() as s:
        prompts = [j.prompt for j in s.query(Job).all()]
    assert sum("fenced block tagged waw-review" in p for p in prompts) == 3
    assert batch_review.status()["started"] == batch_review.status()["finished"] == 3


async def test_batch_review_cap_skip_running_and_cancel(running_app, workspace, fake_env, monkeypatch):
    h, ws = await _setup(running_app)
    api = h.api
    peak = {"n": 0}
    real = batch_review._wait_job

    async def watched(ctx, job_id):
        peak["n"] = max(peak["n"], sum(1 for v in batch_review._running.values() if v == "reviewing"))
        await asyncio.sleep(0.3)  # keep the slot a little so the cap is observable
        await real(ctx, job_id)
    monkeypatch.setattr(batch_review, "_wait_job", watched)
    prs = [{"repo": "pytorch/pytorch", "number": n} for n in range(10, 16)]
    r = (await api.post("/api/v1/github/batch-review", json={"prs": prs, "workspace_id": ws})).json()
    assert len(r["queued"]) == 6
    assert len(batch_review._running) + len(batch_review._queue) == 6 and len(batch_review._running) <= batch_review.CONCURRENCY
    # queued again while waiting / running: skipped
    again = (await api.post("/api/v1/github/batch-review", json={"prs": prs[:1] + prs[-1:], "workspace_id": ws})).json()
    assert again["queued"] == [] and {s["reason"] for s in again["skipped"]} == {"already queued"}
    # take the last one off the queue before it starts
    c = (await api.post("/api/v1/github/batch-review/cancel", json={"prs": prs[-1:]})).json()
    assert c["removed"] == ["pytorch/pytorch#15"]
    await _drain()
    assert peak["n"] <= batch_review.CONCURRENCY and peak["n"] >= 1
    states = github_hub.agent_review_states(h.ctx)
    assert all(states[f"pytorch/pytorch#{n}"]["state"] == "waiting" for n in range(10, 15)) and "pytorch/pytorch#15" not in states


async def test_batch_review_failures_are_reported_per_pr(running_app, workspace, fake_env):
    h, ws = await _setup(running_app)
    api = h.api
    graphql = h.ctx.extra["gh_graphql"]

    def broken(q, v):
        if "query PrDetail" in q and v["number"] == 99:
            raise github_hub.ApiError(404, "GH_NOT_FOUND", "no such pull request")
        return graphql(q, v)
    h.ctx.extra["gh_graphql"] = broken
    # a PR that cannot be loaded fails with the reason; the others go on
    r = await api.post("/api/v1/github/batch-review", json={"prs": [{"repo": "pytorch/pytorch", "number": 99}, {"repo": "pytorch/pytorch", "number": 7}],
                                                            "workspace_id": ws})
    assert r.status_code == 202
    await _drain()
    failed = batch_review.status()["failed"]
    assert list(failed) == ["pytorch/pytorch#99"] and "no such pull request" in failed["pytorch/pytorch#99"]["error"]
    assert github_hub.agent_review_states(h.ctx)["pytorch/pytorch#7"]["state"] == "waiting"
    # GitHub answers again; selected again: the failure is cleared and the review runs
    h.ctx.extra["gh_graphql"] = graphql
    await api.post("/api/v1/github/batch-review", json={"prs": [{"repo": "pytorch/pytorch", "number": 99}], "workspace_id": ws})
    await _drain()
    assert batch_review.status()["failed"] == {} and github_hub.agent_review_states(h.ctx)["pytorch/pytorch#99"]["state"] == "waiting"
    # a workspace that is not open is refused; bad names are refused; only the owner may start
    assert (await api.post("/api/v1/github/batch-review", json={"prs": [{"repo": "a/b", "number": 1}], "workspace_id": "nope"})).status_code == 400
    assert (await api.post("/api/v1/github/batch-review", json={"prs": [{"repo": "a/b/c", "number": 1}]})).status_code == 400
    assert (await api.post("/api/v1/github/batch-review", json={"prs": []})).status_code == 400
    as_github(h, "someone-else")
    assert (await api.post("/api/v1/github/batch-review", json={"prs": [{"repo": "a/b", "number": 1}]})).status_code == 403


async def test_batch_review_without_wiki_fails_cleanly(running_app, workspace, fake_env):
    h, _ = await _setup(running_app)
    item = {"owner": "pytorch", "repo": "pytorch", "number": 7, "question": "q", "workspace_id": None, "login": "EikanWang"}
    try:
        await batch_review.start_review(h.ctx, item)
    except github_hub.ApiError as e:
        assert e.code == "NO_REPO_WIKI" and "pytorch/pytorch" in e.message
    else:
        raise AssertionError("expected NO_REPO_WIKI")
