"""Repo Insights: settings, the summary / highlight history read by its first lines, daily summaries from GitHub (the
fixed "active on the day" rule, bots, local copies), reports of any period (weekly blocks, commits, RFCs, highlights,
reused building blocks), XPU highlights, the scheduler and the chat."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import subprocess
from pathlib import Path

from backend.app.insights import pipeline as pl
from backend.app.insights import scheduler, store
from backend.app.insights import settings as st


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def node(n, title, created, comments=(), kind="Issue", labels=(), body="", author="alice", company=None):
    return {"__typename": kind, "number": n, "title": title, "url": f"https://github.com/acme/proj/{'pull' if kind == 'PullRequest' else 'issues'}/{n}",
            "body": body, "state": "OPEN", "createdAt": created, "updatedAt": max([created, *[c[1] for c in comments]]),
            "author": {"login": author, "company": company}, "labels": {"nodes": [{"name": x} for x in labels]}, "assignees": {"nodes": []},
            "comments": {"nodes": [{"createdAt": t, "body": b, "author": {"login": who}} for who, t, b in comments]},
            **({"mergedAt": None, "reviews": {"nodes": []}} if kind == "PullRequest" else {})}


class FakeGitHub:
    def __init__(self, nodes_by_repo):
        self.nodes = nodes_by_repo
        self.queries: list[str] = []

    def __call__(self, q, v):
        self.queries.append(v["q"])
        import re

        repo = re.search(r"repo:(\S+)", v["q"]).group(1)
        since = re.search(r"updated:>=(\S+)", v["q"]).group(1)
        lo, hi = re.search(r"created:(\S+)\.\.(\S+)", v["q"]).groups()
        nodes = [n for n in self.nodes.get(repo, []) if n["updatedAt"][:10] >= since and lo <= n["createdAt"][:10] <= hi]
        if "label:" in v["q"]:
            lb = re.search(r'label:"([^"]+)"', v["q"]).group(1)
            nodes = [n for n in nodes if lb in [x["name"] for x in n["labels"]["nodes"]]]
        if "in:title" in v["q"]:
            nodes = [n for n in nodes if "rfc" in n["title"].lower()]
        return {"search": {"issueCount": len(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}


class FakeModel:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, task, prompt, as_json):
        self.calls.append((task, prompt))
        if task == "highlight" and as_json:
            score = 95 if "Title: Add CUDA-only" in prompt else 10
            return json.dumps({"score": score, "justification": f"scored {score}"})
        if task == "highlight":
            return "One sentence."
        return f"[{task}] " + " ".join(sorted(set(__import__("re").findall(r"#(\d+)", prompt))))[:300]

    def count(self, task):
        return sum(1 for t, _ in self.calls if t == task)


DAY = "2026-11-03"


async def make(running_app, tmp_path, nodes):
    h = await running_app()
    gh = FakeGitHub(nodes)
    model = FakeModel()
    h.ctx.extra["gh_graphql"] = gh
    h.ctx.extra["insights_llm"] = model
    h.ctx.extra["gh_pr_files"] = lambda o, r, n: [{"filename": "aten/src/ATen/native/native_functions.yaml" if n == 30 else "a.py", "patch": "@@ x\n+y"}]
    st.save(h.ctx, {"data_dir": str(tmp_path / "insights")})
    return h, gh, model


def proj_nodes():
    return {"acme/proj": [
        node(10, "New issue opened that day", f"{DAY}T05:00:00Z"),                                                     # created in the day: kept
        node(11, "Old issue discussed", "2026-10-01T00:00:00Z", [("bob", f"{DAY}T09:00:00Z", "a comment")]),           # commented: kept
        node(12, "Only a bot", "2026-10-01T00:00:00Z", [("pytorchmergebot", f"{DAY}T09:00:00Z", "merged")]),          # bot only: left out
        node(13, "DISABLED test_foo", f"{DAY}T01:00:00Z"),                                                             # DISABLED: left out
        node(14, "Another day", "2026-11-04T01:00:00Z"),
        node(20, "[RFC] New device API", "2026-11-02T00:00:00Z", [("carol", f"{DAY}T10:00:00Z", "+1")], labels=["RFC"]),
        node(30, "Add CUDA-only branch in shared code", f"{DAY}T02:00:00Z", kind="PullRequest"),
        node(31, "Docs typo", f"{DAY}T03:00:00Z", kind="PullRequest"),
        node(32, "Fix xpu kernel", f"{DAY}T04:00:00Z", kind="PullRequest"),
    ], "acme/rfcs": [node(5, "RFC-0042: something", "2026-11-03T00:00:00Z")]}


async def test_settings_defaults_validation_and_secrets(running_app, tmp_path):
    h, _, _ = await make(running_app, tmp_path, {})
    api = h.api
    s = (await api.get("/api/v1/insights/settings")).json()
    assert [r["repo"] for r in s["repos"]][:2] == ["pytorch/pytorch", "sgl-project/sglang"] and len(s["repos"]) == 7
    py = s["repos"][0]
    assert py["highlights"] is True and py["schedule"] == {"enabled": False, "time": "05:35"} and py["rfc"]["repos"] == ["pytorch/rfcs"]
    assert s["llm"]["daily"]["mode"] == "api" and s["presets"][0]["id"] == "xpu" and "GPU_TYPE" in s["presets"][0]["text"]
    assert s["endpoint"] == {"base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash", "has_key": False}
    r = (await api.put("/api/v1/insights/settings", json={"endpoint": {"api_key": "sk-secret"}, "llm": {"highlight": {"mode": "agent", "provider": "opencode"}}})).json()
    assert r["endpoint"]["has_key"] is True and "sk-secret" not in json.dumps(r) and r["llm"]["highlight"]["mode"] == "agent"
    assert (h.ctx.settings.state_dir / "insights-secrets.json").stat().st_mode & 0o077 == 0
    for bad in ({"repos": [{"repo": "nope"}]}, {"repos": [{"repo": "a/b", "schedule": {"time": "25:00"}}]}, {"data_dir": "rel"}, {"llm": {"daily": {"mode": "x"}}}):
        assert (await api.put("/api/v1/insights/settings", json=bad)).status_code == 400, bad


async def test_history_is_read_by_its_first_lines(running_app, tmp_path):
    h, _, _ = await make(running_app, tmp_path, {})
    d = tmp_path / "insights" / "summary" / "acme" / "proj"
    d.mkdir(parents=True)
    (d / "summary_acme_proj_0D_2026-11-01_05-35-01.md").write_text("Summary of acme/proj from 2026-11-01 00-00-00 to 2026-11-01 23-59-59:\n\n# Old\n")
    (d / "summary_acme_proj_2026-10-31_23-59-02.md").write_text("Summary of acme/proj from 2026-10-31 00-00-00 to 2026-10-31 23-59-59:\n\nolder\n")
    (d / "notes.md").write_text("no header")
    hl = tmp_path / "insights" / "highlight" / "acme" / "proj"
    hl.mkdir(parents=True)
    (hl / "highlight_acme_proj_2026-11-02_02-05-02.json").write_text(json.dumps([
        {"File Information": "Highlights of acme/proj from 2026-10-31_02:05:02 to 2026-11-02_02:05:02"},
        {"title": "A", "url": "u1", "xpu_relevance_score": -1, "xpu_relevance_justification": {"score": -1, "justification": '{"score": 77, "justification": "x"}'}},
        {"title": "B", "url": "u2", "xpu_relevance_score": 40, "xpu_relevance_justification": "y"}]))
    (hl / "highlight_acme_proj_2026-11-03_02-05-02.json").write_text(json.dumps([
        {"File Information": "Highlights of acme/proj from 2026-11-01_02:05:02 to 2026-11-03_02:05:02"},
        {"title": "B again", "url": "u2", "xpu_relevance_score": 45, "xpu_relevance_justification": "newer"}]))
    ov = (await h.api.get("/api/v1/insights/repos/acme/proj")).json()
    assert [r["since"] for r in ov["daily"]] == ["2026-11-01", "2026-10-31"] and ov["first_day"] == "2026-10-31"
    assert store.body(store.daily(h.ctx, "acme/proj", "2026-11-01")["path"]) == "# Old"
    items = (await h.api.get("/api/v1/insights/repos/acme/proj/highlights", params={"since": "2026-11-01", "until": "2026-11-02"})).json()["items"]
    assert [(x["url"], x["xpu_relevance_score"]) for x in items] == [("u1", 77), ("u2", 45)]  # the -1 rescued; the newer file wins
    f = (await h.api.get("/api/v1/insights/file", params={"path": ov["daily"][0]["path"]})).json()
    assert f["text"].startswith("Summary of acme/proj")
    assert (await h.api.get("/api/v1/insights/file", params={"path": "/etc/passwd"})).status_code == 400


async def test_daily_summary_from_github_active_rule_and_local_copies(running_app, tmp_path):
    h, gh, model = await make(running_app, tmp_path, proj_nodes())
    text = await pl.daily_summary(h.ctx, "acme/proj", DAY)
    prompt = next(p for t, p in model.calls if t == "daily")
    assert "Number: 10\n" in prompt and "Number: 11\n" in prompt  # created that day (ai_tools dropped these) / commented
    assert "Number: 12\n" not in prompt and "Number: 13\n" not in prompt and "Number: 14\n" not in prompt
    assert "- Comment by bob" in prompt and text.startswith("[daily]")
    f = store.daily(h.ctx, "acme/proj", DAY)
    assert f["name"] == f"summary_acme_proj_0D_{DAY}_23-59-59.md" and Path(f["path"]).read_text().startswith(f"Summary of acme/proj from {DAY} 00-00-00 to {DAY} 23-59-59:")
    # again: the file; another day already fetched: the local copies (no GitHub)
    n = len(gh.queries)
    await pl.daily_summary(h.ctx, "acme/proj", DAY)
    assert len(gh.queries) == n and model.count("daily") == 1
    await pl.daily_summary(h.ctx, "acme/proj", DAY, force=True)
    assert len(gh.queries) == n  # the day is over and was fetched after it ended


async def test_reports_of_any_period_with_blocks_commits_rfcs_highlights(running_app, tmp_path):
    h, gh, model = await make(running_app, tmp_path, proj_nodes())
    # a "GitHub" repository for the commits, cloned by the app (blobless) and updated before each report
    up = tmp_path / "up"
    up.mkdir()
    git(up, "init", "-q", "-b", "main")
    for i, d in enumerate(["2026-10-20", "2026-11-03"]):
        (up / f"f{i}.py").write_text(str(i))
        git(up, "add", "-A")
        subprocess.run(["git", "-c", "user.name=Dev", "-c", "user.email=d@e", "commit", "-q", "-m", f"Change {i} (#{900 + i})"], cwd=up, check=True,
                       env={"GIT_AUTHOR_DATE": f"{d}T12:00:00Z", "GIT_COMMITTER_DATE": f"{d}T12:00:00Z", "PATH": "/usr/bin:/bin"})
    h.ctx.extra["insights_remote"] = {"acme/proj": str(up)}
    st.save(h.ctx, {"repos": [{"repo": "acme/proj", "highlights": True, "rfc": {"repos": ["acme/rfcs"], "labels": ["RFC"], "title_patterns": ["[RFC]"]}}]})
    hl = tmp_path / "insights" / "highlight" / "acme" / "proj"
    hl.mkdir(parents=True)
    (hl / "h.json").write_text(json.dumps([{"File Information": "Highlights of acme/proj from 2026-11-01_00:00:00 to 2026-11-05_00:00:00"},
                                           {"title": "Hot", "url": "https://x/1", "xpu_relevance_score": 90, "xpu_relevance_justification": "matters"}]))
    api = h.api
    # a 21-day custom range: daily summaries made for the missing days (one fetch), rolled up week by week
    r = (await api.post("/api/v1/insights/reports", json={"repo": "acme/proj", "period": "custom", "since": "2026-10-16", "until": "2026-11-05",
                                                          "focus": "devices", "preset": "xpu"})).json()
    rid = r["id"]
    for _ in range(200):
        rep = (await api.get(f"/api/v1/insights/reports/acme/proj/{rid}")).json()
        if rep["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert rep["status"] == "done", rep
    assert rep["text"].startswith("Report of acme/proj from 2026-10-16 to 2026-11-05:") and "[report]" in rep["text"]
    days = [r for r in store.index(h.ctx, "acme/proj") if r["daily"]]
    assert len(days) == 21 and model.count("daily") == 3 and model.count("rollup") == 3  # empty days need no model
    assert "No notable activity" in store.body(store.daily(h.ctx, "acme/proj", "2026-10-20")["path"])
    main_fetch = [q for q in gh.queries if q.startswith("repo:acme/proj updated:>=2026-10-16") and "label" not in q and "in:title" not in q]
    assert len(main_fetch) == 1  # all missing days in one GitHub search
    final = next(p for t, p in model.calls if t == "report")
    assert "## RFCs" in final and "RFC-0042" in final and "[RFC] New device API" in final
    assert "Commits on the default branch: 2 by 1 authors" in final and "Change 1 (#901)" in final
    assert "## XPU watch" in final and "[Hot](https://x/1)" in final and "GPU_TYPE" in final and "devices" in final
    assert any(f.startswith("Days with a summary") for f in rep["facts"])
    # the same period again: the summaries and the weekly blocks are reused (no daily / rollup work), the clone updated
    (up / "f2.py").write_text("2")
    git(up, "add", "-A")
    subprocess.run(["git", "-c", "user.name=Dev", "-c", "user.email=d@e", "commit", "-q", "-m", "Late (#999)"], cwd=up, check=True,
                   env={"GIT_AUTHOR_DATE": "2026-11-04T12:00:00Z", "GIT_COMMITTER_DATE": "2026-11-04T12:00:00Z", "PATH": "/usr/bin:/bin"})
    d0, r0 = model.count("daily"), model.count("rollup")
    r2 = (await api.post("/api/v1/insights/reports", json={"repo": "acme/proj", "period": "custom", "since": "2026-10-16", "until": "2026-11-05"})).json()
    for _ in range(200):
        rep2 = (await api.get(f"/api/v1/insights/reports/acme/proj/{r2['id']}")).json()
        if rep2["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert rep2["status"] == "done" and model.count("daily") == d0 and model.count("rollup") == r0
    assert "Late (#999)" in [p for t, p in model.calls if t == "report"][-1]
    assert {x["id"] for x in (await api.get("/api/v1/insights/repos/acme/proj")).json()["reports"]} >= {rid, r2["id"]}
    # continue in a chat; delete
    c = await api.post(f"/api/v1/insights/reports/acme/proj/{rid}/chat", json={})
    assert c.status_code == 201, c.text
    await api.wait_job(c.json()["job_id"])
    assert (await api.delete(f"/api/v1/insights/reports/acme/proj/{rid}")).status_code == 200
    assert (await api.get(f"/api/v1/insights/reports/acme/proj/{rid}")).status_code == 404
    for bad in ({"repo": "acme/proj", "period": "custom", "since": "2026-11-05", "until": "2026-11-01"}, {"repo": "x", "period": "week"},
                {"repo": "acme/proj", "period": "fortnight"}):
        assert (await api.post("/api/v1/insights/reports", json=bad)).status_code in (400, 422), bad


async def test_xpu_highlights_scored_and_written(running_app, tmp_path):
    h, _, model = await make(running_app, tmp_path, proj_nodes())
    res = await pl.highlights(h.ctx, "acme/proj", DAY, DAY)
    assert res["scored"] >= 4
    f = sorted((tmp_path / "insights" / "highlight" / "acme" / "proj").glob("*.json"))[-1]
    data = json.loads(f.read_text())
    assert data[0] == {"File Information": f"Highlights of acme/proj from {DAY}_00:00:00 to {DAY}_23:59:59"}
    by = {x["url"].rsplit("/", 1)[1]: x for x in data[1:]}
    assert by["30"]["xpu_relevance_score"] == 95 and by["30"]["xpu_relevance_justification"] == "scored 95"  # by the score
    assert "32" in by and by["32"]["xpu_relevance_score"] == 10  # by the rules (xpu in the title)
    assert "31" not in by  # neither
    assert by["30"]["body"] == "One sentence." or by["30"]["body"]
    assert any("Diff: --- a/aten/src/ATen/native/native_functions.yaml" in p for t, p in model.calls if t == "highlight")


async def test_scheduler_runs_the_last_complete_day_once(running_app, tmp_path):
    h, _, _model = await make(running_app, tmp_path, proj_nodes())
    st.save(h.ctx, {"repos": [{"repo": "acme/proj", "highlights": False, "schedule": {"enabled": True, "time": "05:35"}}]})
    early = dt.datetime(2026, 11, 3, 21, 0, tzinfo=dt.timezone.utc)   # 05:00 UTC+8 on the 4th: not yet
    assert await scheduler.tick(h.ctx, early) == []
    late = dt.datetime(2026, 11, 3, 22, 0, tzinfo=dt.timezone.utc)    # 06:00 UTC+8: the 2nd (the last complete UTC day)
    assert await scheduler.tick(h.ctx, late) == ["acme/proj"]
    for _ in range(100):
        if not [j for j in pl.jobs("acme/proj") if j["status"] == "running"]:
            break
        await asyncio.sleep(0.05)
    assert store.daily(h.ctx, "acme/proj", "2026-11-02") is not None
    assert await scheduler.tick(h.ctx, late) == []  # done for the day
    st.save(h.ctx, {"repos": [{"repo": "acme/proj", "schedule": {"enabled": False, "time": "05:35"}}]})
    assert await scheduler.tick(h.ctx, late + dt.timedelta(days=1)) == []  # off
