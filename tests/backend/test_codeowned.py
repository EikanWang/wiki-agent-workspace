"""PRs touching files you own in CODEOWNERS: GitHub's matching, the incremental scan, and the review list."""

from __future__ import annotations

import asyncio

import pytest
from test_github_hub import _who, as_github, pr

from backend.app import codeowned, github_hub, maintainers
from backend.app.errors import ApiError
from backend.app.kv import kv_put

CODEOWNERS = """# owners
* @fallback
/aten/src/ATen/xpu/ @EikanWang @gujinghui
/aten/src/ATen/native/mkldnn/xpu/ @EikanWang @gujinghui  # oneDNN on XPU
/aten/src/ATen/native/mkldnn/xpu/special.cpp
/test/test_scaled_matmul_cuda.py @drisspg
*.md @docs-team @pytorch/docs
"""


@pytest.mark.parametrize("pattern,path,hit", [
    ("*.js", "a/b/c.js", True),            # no slash: at any depth
    ("docs/*", "docs/a.md", True),
    ("docs/*", "docs/x/a.md", False),      # * does not cross folders (GitHub's docs/* example)
    ("apps/", "x/apps/y.py", True),        # a folder anywhere
    ("/docs/", "docs/q/r", True),
    ("/docs/", "x/docs/q", False),         # anchored
    ("/build/logs", "build/logs/x/y", True),  # a plain name owns the folder's contents
    ("/torch/xpu/", "torch/xpu_x/a", False),
    ("**/logs", "a/b/logs/c", True),
    ("/a/**/b.py", "a/b.py", True),
    ("/a/**/b.py", "a/x/y/b.py", True),
    ("*", "anything/at/all", True),
])
def test_patterns_match_like_github(pattern, path, hit):
    assert bool(codeowned._pattern_regex(pattern).fullmatch(path)) is hit


def test_the_last_matching_rule_decides():
    rules = codeowned.parse_rules(CODEOWNERS)
    assert codeowned.owners_of(rules, "aten/src/ATen/native/mkldnn/xpu/ScaledBlas.cpp") == {"@eikanwang", "@gujinghui"}
    assert codeowned.owners_of(rules, "aten/src/ATen/native/mkldnn/xpu/special.cpp") == set()  # a rule without owners
    assert codeowned.owners_of(rules, "aten/src/ATen/native/mkldnn/xpu/README.md") == {"@docs-team", "@pytorch/docs"}  # later rule
    assert codeowned.owners_of(rules, "setup.py") == {"@fallback"}
    files = ["aten/src/ATen/native/mkldnn/xpu/ScaledBlas.cpp", "test/test_scaled_matmul_cuda.py", "aten/src/ATen/xpu/X.h"]
    assert codeowned.owned_paths(rules, files, "eikanwang") == [files[0], files[2]]
    assert codeowned.names(rules, "EikanWang") and not codeowned.names(rules, "nobody")


def node(n, updated, files, author="dev", draft=False, more_files=None):
    return {"number": n, "updatedAt": updated, "isDraft": draft, "author": {"login": author},
            "files": {"pageInfo": {"hasNextPage": bool(more_files), "endCursor": "f1" if more_files else None},
                      "nodes": [{"path": f} for f in files]}}


XPU = "aten/src/ATen/native/mkldnn/xpu/ScaledBlas.cpp"


class FakeGithub:
    def __init__(self, prs, oid="o1", text=CODEOWNERS):
        self.prs, self.oid, self.text, self.calls, self.more, self.fail_at = prs, oid, text, [], {}, None

    def __call__(self, q, v):
        if "query Codeowners" in q:
            self.calls.append("codeowners")
            return {"repository": {"a": None, "b": {"oid": self.oid, "text": self.text, "isTruncated": False}, "c": None}}
        if "query CodeownedFiles" in q:
            self.calls.append(("files", v["number"]))
            return {"repository": {"pullRequest": {"files": {"pageInfo": {"hasNextPage": False}, "nodes": [{"path": p} for p in self.more[v["number"]]]}}}}
        assert "query CodeownedScan" in q
        start = int(v.get("after") or 0)
        self.calls.append(("page", start))
        if self.fail_at == start:
            raise ApiError(429, "GH_RATE_LIMITED", "rate limited")
        nodes = sorted(self.prs, key=lambda x: x["updatedAt"], reverse=True)[start:start + v["n"]]
        more = start + v["n"] < len(self.prs)
        return {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": more, "endCursor": str(start + v["n"]) if more else None}, "nodes": nodes}}}


async def test_full_then_incremental_scan_with_resume(running_app, monkeypatch):
    ctx = (await running_app()).ctx
    monkeypatch.setattr(codeowned, "PAGE_SIZE", 2)
    gh = FakeGithub([node(1, "2026-11-01T00:00:00Z", [XPU], draft=True),         # a draft touching my files
                     node(2, "2026-11-02T00:00:00Z", ["torch/nn/x.py"]),
                     node(3, "2026-11-03T00:00:00Z", [XPU], author="EikanWang"),  # my own PR
                     node(4, "2026-11-04T00:00:00Z", ["a.py"], more_files=True),  # mine is on its second files page
                     node(5, "2026-11-05T00:00:00Z", ["aten/src/ATen/native/mkldnn/xpu/special.cpp"])])  # no owner
    gh.more[4] = ["aten/src/ATen/xpu/Y.h"]
    t0 = 1_800_000_000.0
    # the first scan is stopped by the rate limit on its third page: the progress so far is kept
    gh.fail_at = 4
    with pytest.raises(ApiError):
        codeowned.scan(ctx, gh, "EikanWang", "pytorch/pytorch", now=t0)
    st = codeowned.load(ctx, "pytorch/pytorch")
    assert st["resume"]["after"] == "4" and set(st["prs"]) == {"4"} and st["error"] == "rate limited"
    # ... and goes on where it stopped
    gh.fail_at, gh.calls = None, []
    assert codeowned.scan(ctx, gh, "EikanWang", "pytorch/pytorch", now=t0 + 60) is True
    assert gh.calls == ["codeowners", ("page", 4)]
    st = codeowned.load(ctx, "pytorch/pytorch")
    assert sorted(st["prs"]) == ["1", "4"] and st["prs"]["1"]["draft"] is True and st["prs"]["4"]["paths"] == ["aten/src/ATen/xpu/Y.h"]
    assert st["resume"] is None and st["since"] == codeowned._iso(t0)  # the scan's start: later scans look from there
    # incremental: only PRs updated since then (one page, stops at the first older PR)
    gh.prs.append(node(6, codeowned._iso(t0 + 100), [XPU]))
    gh.prs[1] = node(2, codeowned._iso(t0 + 50), ["torch/nn/x.py", "aten/src/ATen/xpu/new.cpp"])  # #2 now touches my files
    gh.calls = []
    assert codeowned.scan(ctx, gh, "EikanWang", "pytorch/pytorch", now=t0 + 400) is True
    assert gh.calls == ["codeowners", ("page", 0), ("page", 2)]  # page 2 holds #5 (older): the scan stops there
    assert sorted(codeowned.load(ctx, "pytorch/pytorch")["prs"]) == ["1", "2", "4", "6"]
    gh.calls = []
    assert codeowned.scan(ctx, gh, "EikanWang", "pytorch/pytorch", now=t0 + 800) is False
    assert gh.calls == ["codeowners", ("page", 0)]
    # CODEOWNERS changed: everything is checked again
    gh.oid, gh.text, gh.calls = "o2", "/aten/src/ATen/xpu/ @EikanWang\n", []
    assert codeowned.scan(ctx, gh, "EikanWang", "pytorch/pytorch", now=t0 + 1200) is True
    assert ("page", 4) in gh.calls and sorted(codeowned.load(ctx, "pytorch/pytorch")["prs"]) == ["2", "4"]
    # not a code owner any more: nothing listed
    gh.oid, gh.text = "o3", "* @someone\n"
    assert codeowned.scan(ctx, gh, "EikanWang", "pytorch/pytorch", now=t0 + 1600) is True
    assert codeowned.load(ctx, "pytorch/pytorch")["prs"] == {}


async def test_code_owned_prs_join_the_review_list(running_app):
    github_hub.reset_cache()
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    h.ctx.extra["codeowned_scan"] = True
    # maintainers (read once a day) know you own paths of pytorch/pytorch: that repository is checked
    kv_put(h.ctx, maintainers.KEY, {"pytorch/pytorch": {"fetched": 9e12, "sources": ["CODEOWNERS"], "people": maintainers.parse_codeowners(CODEOWNERS)}})
    scan = FakeGithub([node(201752, "2026-11-16T15:00:00Z", [XPU, "test/test_scaled_matmul_cuda.py"], draft=True),
                       node(7, "2026-11-15T00:00:00Z", [XPU]), node(1, "2026-11-14T00:00:00Z", [XPU])])
    owned_calls: list = []
    merged = {7}

    def graphql(q, v):
        if "query CodeownedPrs" in q:
            owned_calls.append(q)
            out = {}
            for n in (201752, 7):
                if f"p{n}:" in q:
                    p = pr(n, draft=n == 201752, user=None)
                    p["state"] = "MERGED" if n in merged else "OPEN"
                    out[f"p{n}"] = p
            return {"repository": out}
        if "query Codeowners" in q or "query Codeowned" in q:
            return scan(q, v)
        return {"search": {"issueCount": 1, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [pr(1)]}}

    h.ctx.extra["gh_graphql"] = graphql
    api = h.api
    r = (await api.get("/api/v1/github/reviews")).json()
    assert [i["number"] for i in r["items"]] == [1]  # the first answer: only what GitHub requested
    for t in list(codeowned._running.values()):
        await asyncio.wait_for(t, 5)  # the scan found PRs: the list refreshes by itself
    await asyncio.wait_for(github_hub._tasks["EikanWang"], 5)
    r = (await api.get("/api/v1/github/reviews")).json()
    by = {i["number"]: i for i in r["items"]}
    assert set(by) == {1, 201752}  # #7 is merged: dropped (and forgotten)
    assert by[201752]["requested"] == "codeowner" and by[201752]["draft"] is True and by[201752]["owned_paths"] == [XPU]
    assert by[1]["requested"] == "you" and by[1]["owned_paths"] == [XPU]  # requested and yours
    assert "7" not in codeowned.load(h.ctx, "pytorch/pytorch")["prs"]
    assert r["codeowned"][0]["repo"] == "pytorch/pytorch" and r["codeowned"][0]["prs"] == 2 and r["hidden_count"] == 0
    # turned off in Reviews ⚙: gone at once
    assert (await api.put("/api/v1/github/prefs", json={"codeowned": False})).status_code == 200
    r = (await api.get("/api/v1/github/reviews")).json()
    assert [i["number"] for i in r["items"]] == [1] and r["codeowned"] == [] and r["hidden_count"] == 0


async def test_a_bot_cc_on_a_pr_touching_your_files_stays_in_the_list(running_app):
    # pytorch-bot cc's you on #5 (touches your files) and #9 (does not): only #9 is "cc'd by bot"
    github_hub.reset_cache()
    h = await running_app()
    as_github(h, "EikanWang")
    h.ctx.extra["gh_owner"] = lambda: "EikanWang"
    h.ctx.extra["codeowned_scan"] = True
    kv_put(h.ctx, maintainers.KEY, {"pytorch/pytorch": {"fetched": 9e12, "sources": ["CODEOWNERS"], "people": maintainers.parse_codeowners(CODEOWNERS)}})
    scan = FakeGithub([node(5, "2026-11-16T00:00:00Z", [XPU]), node(9, "2026-11-15T00:00:00Z", ["torch/_inductor/decomposition.py"])])
    bot = _who([("dev", "x"), ("pytorch-bot", "x\n\ncc @voznesenskym @EikanWang")])
    owned_calls: list = []

    def graphql(q, v):
        if "MentionWho" in q:
            return {"repository": {f"p{n}": bot for n in (5, 9) if f"pullRequest(number: {n})" in q}}
        if "query CodeownedPrs" in q:
            owned_calls.append(q)
            return {"repository": {}}
        if "query Codeowners" in q or "query Codeowned" in q:
            return scan(q, v)
        nodes = [pr(5, user=None), pr(9, user=None)] if "mentions:@me" in (v.get("q") or "") else [pr(1)]
        return {"search": {"issueCount": len(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}

    h.ctx.extra["gh_graphql"] = graphql
    r = (await h.api.get("/api/v1/github/reviews")).json()
    assert sorted(p["number"] for p in r["bot_cc"]) == [5, 9]  # before the scan: not known to be yours yet
    for t in list(codeowned._running.values()):
        await asyncio.wait_for(t, 5)
    await asyncio.wait_for(github_hub._tasks["EikanWang"], 5)
    r = (await h.api.get("/api/v1/github/reviews")).json()
    by = {p["number"]: p for p in r["items"]}
    assert set(by) == {1, 5} and [p["number"] for p in r["bot_cc"]] == [9]
    assert by[5]["requested"] == "codeowner" and by[5]["owned_paths"] == [XPU] and by[5]["mention_by"] == "bot"
    assert owned_calls == []  # already read by the mentions search: not fetched again
