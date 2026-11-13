"""PR Review priority: levels, hard rules OR the agent (most urgent wins), set by hand, migration, validation, and the
agent's work — a small diff read in full, a large one summarized file by file (batched, split, capped, cached)."""

from __future__ import annotations

import asyncio

import pytest
from test_github_hub import as_github, pr, requested_by

from backend.app import github_hub
from backend.app import review_priority as rp
from backend.app.errors import ApiError


def prefs(**kw):
    return rp.normalize({**github_hub.DEFAULT_PREFS, "priority_meta": False, "priority_agent": {"enabled": True}, **kw})


def item(n=1, **kw):
    base = {"repo": "pytorch/pytorch", "number": n, "title": f"PR {n}", "author": "alice", "requested_by": "bob", "labels": [{"name": "xpu"}],
            "additions": 10, "deletions": 2, "ci": "SUCCESS", "draft": False, "review_decision": "REVIEW_REQUIRED", "my_activity": "todo",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-11-08T00:00:00Z"}
    return {**base, **kw}


def rule(level, *conds, enabled=True):
    return {"id": f"r-{level}", "enabled": enabled, "level": level, "conditions": [dict(zip(("field", "op", "value"), c, strict=True)) for c in conds]}


def test_hard_rules_or_agent_the_most_urgent_wins_hand_beats_both():
    p = prefs(priority_rules=[rule("P1", ("label", "in", ["xpu"])), rule("P3", ("author", "in", ["alice"]))])
    it = item()
    # hard rules alone: the most urgent matching rule (P1 beats P3)
    lv = rp.final_level(it, p, None)
    assert (lv["level"], lv["source"]) == ("P1", "rule") and "label in xpu" in lv["reasons"][0] and lv["priority"] is True
    # the agent more urgent → the agent; less urgent → the rule
    assert rp.final_level(it, p, {"level": "P0", "reason": "crash fix", "matched": "crashes"})["level"] == "P0"
    lv = rp.final_level(it, p, {"level": "P2", "reason": "meh"})
    assert lv["level"] == "P1" and any(r.startswith("agent:") for r in lv["reasons"])
    # no rule: the agent; the agent says none and no rule: the default
    q = prefs()
    assert rp.final_level(it, q, {"level": "P1", "reason": "x"})["source"] == "agent"
    lv = rp.final_level(it, q, {"level": None, "reason": "no fit", "at": "t"})
    assert (lv["level"], lv["source"]) == ("P2", "default") and "none of your descriptions fits" in lv["reasons"][0]
    # set by hand beats both
    p["priority_manual"] = {"pytorch/pytorch#1": "P3"}
    lv = rp.final_level(it, p, {"level": "P0"})
    assert (lv["level"], lv["source"]) == ("P3", "manual") and lv["reasons"][0] == "set by hand"
    # a disabled rule does nothing
    assert rp.hard_level(it, prefs(priority_rules=[rule("P0", ("label", "in", ["xpu"]), enabled=False)])) == (None, None)


@pytest.mark.parametrize(("cond", "kw", "hit"), [
    (("requested_by", "in", ["Bob"]), {}, True), (("requested_by", "not_in", ["bob"]), {}, False),
    (("requested_by_meta", "is", True), {"requested_by_meta": True}, True), (("author", "in", ["carol"]), {}, False),
    (("repo", "in", ["PyTorch/PyTorch"]), {}, True), (("label", "in", ["XPU", "cuda"]), {}, True),
    (("title", "contains", "fix"), {"title": "Fix a crash"}, True), (("title", "not_contains", "fix"), {"title": "Fix"}, False),
    (("size", ">=", 100), {}, False), (("size", "<=", 12), {}, True), (("age_days", ">=", 30), {}, True), (("idle_days", "<=", 1), {}, False),
    (("ci", "in", ["failing"]), {"ci": "FAILURE"}, True), (("ci", "in", ["none"]), {"ci": None}, True), (("ci", "in", ["running"]), {}, False),
    (("draft", "is", True), {"draft": True}, True), (("review_decision", "in", ["CHANGES_REQUESTED"]), {}, False),
    (("my_activity", "in", ["news", "todo"]), {}, True),
])
def test_every_rule_field(cond, kw, hit):
    c = rp.clean_rules([rule("P0", cond)], ["P0", "P1", "P2", "P3"])[0]["conditions"][0]
    assert rp._match(item(**kw), c) is hit


def test_validation_and_migration():
    ids = ["P0", "P1"]
    for bad in ([rule("P9", ("label", "in", ["x"]))], [rule("P0", ("nope", "in", ["x"]))], [rule("P0", ("size", "in", ["x"]))],
                [rule("P0", ("ci", "in", ["green"]))], [rule("P0")], [rule("P0", ("size", ">=", "big"))]):
        with pytest.raises(ApiError):
            rp.clean_rules(bad, ids)
    for bad in ([], [{"id": "a b"}], [{"id": "P0"}, {"id": "p0"}], [{"id": "P0", "color": "red"}]):
        with pytest.raises(ApiError):
            rp.clean_levels(bad)
    with pytest.raises(ApiError):
        rp.clean_categories([{"key": "todo"}, {"key": "news"}])
    assert [c["key"] for c in rp.clean_categories([{"key": "quiet"}, {"key": "todo", "title": "Fresh"}, {"key": "news"}])] == ["quiet", "todo", "news"]
    # the older settings become hard rules and levels set by hand
    m = rp.normalize({**github_hub.DEFAULT_PREFS, "priority_requesters": ["chosen"], "starred": ["a/b#1"], "unstarred": ["a/b#2"]})
    assert [r["id"] for r in m["priority_rules"]] == ["meta", "people"] and m["priority_rules"][1]["conditions"][0]["value"] == ["chosen"]
    assert m["priority_manual"] == {"a/b#1": "P1", "a/b#2": "P2"} and [c["key"] for c in m["categories"]] == ["todo", "news", "quiet"]


def test_files_to_summarize_parts_and_batches():
    files = [{"path": f"src/f{i}.cpp", "unified": "x" * 100, "additions": i, "deletions": 0} for i in range(70)] + \
        [{"path": "package-lock.json", "unified": "y", "additions": 900}, {"path": "img.png", "unified": "", "binary_or_large": True},
         {"path": "third_party/lib.c", "unified": "z", "additions": 5}]
    todo, named = rp.plan_files(files)
    assert len(todo) == rp.MAX_FILES and todo[0]["path"] == "src/f69.cpp"  # the biggest changes
    notes = {f["path"]: f["note"] for f in named}
    assert notes["package-lock.json"].startswith("lock") and notes["img.png"].startswith("binary") and notes["third_party/lib.c"].startswith("lock")
    assert notes["src/f0.cpp"].startswith("not summarized")
    big = "".join(f"@@ -{i},1 +{i},1 @@\n" + "+" + "a" * 9_000 + "\n" for i in range(5))
    parts = rp.split_patch(big)
    assert len(parts) > 1 and all(len(x) <= rp.BATCH_CHARS for x in parts) and "".join(parts) == big
    assert len(rp.split_patch("q" * 60_000)) == 3  # one huge hunk: cut
    b = rp.batches([("a", "a", "x" * 20_000), ("b", "b", "x" * 4_000), ("c", "c", "x" * 3_000), ("d", "d", "x" * 1_000)])
    assert [[i[0] for i in g] for g in b] == [["a", "b"], ["c", "d"]]


def _queue_graphql(prs: dict[int, dict]):
    """search → the queue; PrDetail → one PR (title, body); PrFiles → viewed marks."""
    def graphql(q, v):
        if "search(" in q:
            nodes = [n["node"] for n in prs.values()]
            return {"search": {"issueCount": len(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}
        if "query PrFiles" in q:
            return {"repository": {"pullRequest": {"files": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}}
        if "query PrDetail" in q:
            n = prs[v["number"]]
            return {"repository": {"nameWithOwner": "pytorch/pytorch", "isPrivate": False, "pullRequest": {
                "id": f"PR_{v['number']}", "number": v["number"], "title": n["node"]["title"], "url": "u", "body": n["body"], "state": "OPEN",
                "isDraft": False, "createdAt": "2026-11-01T00:00:00Z", "updatedAt": n["node"]["updatedAt"], "mergeable": "MERGEABLE",
                "additions": 10, "deletions": 2, "changedFiles": 1, "baseRefName": "main", "headRefName": "h", "reviewDecision": "REVIEW_REQUIRED",
                "author": {"login": "someone"}, "labels": {"nodes": []}, "latestReviews": {"nodes": []}, "reviewRequests": {"nodes": []},
                "commits": {"nodes": [{"commit": {"oid": "abc", "statusCheckRollup": None}}]}}}}
        raise AssertionError(q[:60])
    return graphql


async def _settle(api, timeout=20.0):
    for _ in range(int(timeout / 0.1)):
        st = (await api.get("/api/v1/github/priority/status")).json()
        if not st["queued"] and not st["running"]:
            return st
        await asyncio.sleep(0.1)
    raise AssertionError("the agent is still classifying")


async def test_the_agent_decides_levels_from_your_descriptions(running_app, fake_env):
    github_hub.reset_cache()
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    small = pr(1)
    small["title"] = "Fix a crash prio-hint:P0"
    big = requested_by(pr(2), "metaguy", company="Meta")  # a hard rule gives P1; the agent says P3: P1 stays
    big["title"] = "Refactor prio-hint:P3"
    none = pr(3)
    none["title"] = "Docs prio-hint:none"
    prs = {1: {"node": small, "body": "Fixes the **allocator** crash."}, 2: {"node": big, "body": "Big refactor"}, 3: {"node": none, "body": ""}}
    files = {1: [{"filename": "c10/alloc.cpp", "status": "modified", "additions": 3, "deletions": 1, "patch": "@@ -1 +1 @@\n-old\n+new"}],
             2: [{"filename": f"aten/f{i}.cpp", "status": "modified", "additions": 50 + i, "deletions": 0,
                  "patch": "@@ -1 +1,40 @@\n" + "".join(f"+line {j} of f{i}\n" for j in range(400))} for i in range(3)]
             + [{"filename": "yarn.lock", "status": "modified", "additions": 900, "deletions": 0, "patch": "@@ x\n+y"}],
             3: [{"filename": "README.md", "status": "modified", "additions": 1, "deletions": 0, "patch": "@@ -1 +1 @@\n+doc"}]}
    h.ctx.extra["gh_graphql"] = _queue_graphql(prs)
    h.ctx.extra["gh_pr_files"] = lambda o, r, n: files[n]
    api = h.api
    # off by default: nothing runs, levels from rules only
    r = (await api.get("/api/v1/github/reviews")).json()
    by = {i["number"]: i for i in r["items"]}
    assert by[2]["level"] == "P1" and by[2]["level_source"] == "rule" and by[1]["level"] == "P2" and r["priority_agent"]["queued"] == 0
    assert (await api.post("/api/v1/github/priority/classify", json={"all": True})).json()["error"]["code"] == "PRIORITY_AGENT_OFF"
    # describe the levels, turn the agent on
    levels = [dict(lv) for lv in r["prefs"]["priority_levels"]]
    levels[0]["descriptions"] = ["Crashes in the XPU runtime", "Security fixes"]
    levels[3]["descriptions"] = ["Docs-only changes"]
    p = (await api.put("/api/v1/github/prefs", json={"priority_levels": levels, "priority_agent": {
        "enabled": True, "summary": {"provider": "opencode", "model": "fake/alpha"}, "decision": {"provider": "opencode"}}})).json()
    assert p["priority_agent"]["summary"]["model"] == "fake/alpha"
    await api.get("/api/v1/github/reviews")
    await _settle(api)
    by = {i["number"]: i for i in (await api.get("/api/v1/github/reviews")).json()["items"]}
    assert (by[1]["level"], by[1]["level_source"]) == ("P0", "agent") and "fake decision P0" in by[1]["level_reasons"][0]
    assert (by[2]["level"], by[2]["level_source"]) == ("P1", "rule") and any("agent:" in x and "P3" in x for x in by[2]["level_reasons"])
    assert (by[3]["level"], by[3]["level_source"]) == ("P2", "default")
    # what the agent read: the small PR in full, the big one through file summaries (the lock file only named)
    dec = (fake_env / "priority_decision_prompts.log").read_text().split("\n\0\n")
    d1 = next(x for x in dec if "#1 Fix a crash" in x)
    assert "Crashes in the XPU runtime" in d1 and "Fixes the **allocator** crash." in d1 and "```diff" in d1 and "+new" in d1
    d2 = next(x for x in dec if "#2 Refactor" in x)
    assert "Summaries of the changes, file by file:" in d2 and "- aten/f2.cpp: summary of aten/f2.cpp" in d2
    assert "yarn.lock: (lock / vendored / generated)" in d2 and "+line 1 of f0" not in d2
    summ = (fake_env / "priority_summary_prompts.log").read_text().split("\n\0\n")
    assert sum(1 for x in summ if x.startswith("WAW-PRIORITY-SUMMARY")) == 1 and "yarn.lock" not in summ[0]
    res = (await api.get("/api/v1/github/priority/pr/pytorch/pytorch/2")).json()["result"]
    assert res["mode"] == "summaries" and [f["path"] for f in res["files"]][:3] == ["aten/f2.cpp", "aten/f1.cpp", "aten/f0.cpp"]
    # nothing changed: no new turns; a description edit: decisions again, file summaries from the cache
    n_dec = len(dec)
    await api.get("/api/v1/github/reviews")
    await _settle(api)
    assert len((fake_env / "priority_decision_prompts.log").read_text().split("\n\0\n")) == n_dec
    levels[1]["descriptions"] = ["Performance work"]
    await api.put("/api/v1/github/prefs", json={"priority_levels": levels})
    await api.get("/api/v1/github/reviews")
    await _settle(api)
    assert len((fake_env / "priority_decision_prompts.log").read_text().split("\n\0\n")) > n_dec
    assert sum(1 for x in (fake_env / "priority_summary_prompts.log").read_text().split("\n\0\n") if x.startswith("WAW-PRIORITY-SUMMARY")) == 1
    # set by hand (beats everything), back to rules / agent; re-classify one PR
    p = (await api.put("/api/v1/github/prefs", json={"set_level": {"pr": "pytorch/pytorch#1", "level": "P3"}})).json()
    assert p["priority_manual"]["pytorch/pytorch#1"] == "P3"
    assert {i["number"]: i for i in (await api.get("/api/v1/github/reviews")).json()["items"]}[1]["level_source"] == "manual"
    await api.put("/api/v1/github/prefs", json={"set_level": {"pr": "pytorch/pytorch#1", "level": None}})
    assert (await api.put("/api/v1/github/prefs", json={"set_level": {"pr": "pytorch/pytorch#1", "level": "P7"}})).json()["error"]["code"] == "INVALID_PRIORITY"
    assert (await api.post("/api/v1/github/priority/classify", json={"pr": "pytorch/pytorch#1"})).json()["queued"] >= 0
    await _settle(api)
    assert (await api.post("/api/v1/github/priority/classify", json={"pr": "x/y#9"})).json()["error"]["code"] == "PR_NOT_IN_LIST"
    # the agent's sessions are hidden helpers; Codex would run read-only
    from backend.app.models import AgentSession

    with h.ctx.db.read() as s:
        assert s.query(AgentSession).filter_by(origin="priority").count() >= 4
    # reset: default levels, no descriptions, agent off
    p = (await api.put("/api/v1/github/prefs", json={"reset_priority": True})).json()
    assert p["priority_agent"]["enabled"] is False and all(not lv["descriptions"] for lv in p["priority_levels"])
