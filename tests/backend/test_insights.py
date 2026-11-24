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
from backend.app.insights import registry, scheduler, store
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
    # a request's size: the agent's limit (smaller) for a task on the agent
    from backend.app.insights import llm
    assert s["max_chars"] == 1_500_000 and s["agent_max_chars"] == 400_000
    assert llm.request_chars(h.ctx, "highlight") == 400_000 and llm.request_chars(h.ctx, "daily") == 1_500_000
    r = (await api.put("/api/v1/insights/settings", json={"agent_max_chars": 250_000})).json()
    assert r["agent_max_chars"] == 250_000 and llm.request_chars(h.ctx, "highlight") == 250_000
    assert (await api.put("/api/v1/insights/settings", json={"agent_max_chars": 5})).status_code in (400, 422)
    outs = []
    h.ctx.extra["insights_llm"] = lambda task, prompt, as_json: outs.append(len(prompt)) or "part"
    await llm.summarize(h.ctx, "highlight", ["x" * 100_000] * 6, "ins", "combine")  # 600k: split in parts for the agent
    assert len(outs) == 4 and max(outs) <= 250_000 and outs[-1] < 100  # three parts, then their combination
    h.ctx.extra.pop("insights_llm")
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
    # an item of an earlier day (here for its comments) comes with the start of its description only
    from backend.app.insights import github as igh
    it = {"number": 1, "title": "t", "url": "u", "body": "q" * 3000, "labels": [], "assignees": [], "reviews": [], "comments": [], "review_comments": []}
    a, b = igh.window(DAY)
    assert "q" * 3000 in igh.text_of({**it, "created_at": f"{DAY}T05:00:00Z"}, a, b)
    assert igh.text_of({**it, "created_at": "2026-10-01T00:00:00Z"}, a, b).count("q") == 500
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
    assert r["kind"] == "dates" and r["label"] == "2026-10-16 – 2026-11-05"
    rid = r["id"]
    for _ in range(200):
        rep = (await api.get(f"/api/v1/insights/reports/acme/proj/{rid}")).json()
        if rep["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert rep["status"] == "done", rep
    assert rep["text"].startswith("Report of acme/proj from 2026-10-16 to 2026-11-05:") and "[report]" in rep["text"]
    days = [r for r in store.index(h.ctx, "acme/proj") if r["daily"]]
    assert len(days) == 21 and model.count("daily") == 3 and model.count("rollup") == 4  # calendar weeks: Oct 16–18, 19–25, Oct 26–Nov 1, Nov 2–5; empty days need no model
    assert "No notable activity" in store.body(store.daily(h.ctx, "acme/proj", "2026-10-20")["path"])
    main_fetch = [q for q in gh.queries if q.startswith("repo:acme/proj updated:>=2026-10-16") and "label" not in q and "in:title" not in q]
    assert len(main_fetch) == 1  # all missing days in one GitHub search
    final = next(p for t, p in model.calls if t == "report")
    assert "## RFCs ::" in final and "RFC-0042" in final and "[RFC] New device API" in final
    assert "Commits on the default branch: 2 by 1 authors" in final and "Change 1 (#901)" in final
    assert "## XPU watch ::" in final and "[Hot](https://x/1)" in final and "GPU_TYPE" in final and "devices" in final
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
    # several chosen reports in one chat (compared)
    c = await api.post("/api/v1/insights/reports/acme/proj/chat", json={"ids": [r2["id"], rid]})
    assert c.status_code == 201, c.text
    await api.wait_job(c.json()["job_id"])
    with h.ctx.db.read() as s:
        from backend.app.models import Job
        prompt = s.get(Job, c.json()["job_id"]).prompt
    assert prompt.count("<<<REPORT ") == 2 and "Compare them" in prompt, prompt[:2000]
    assert (await api.post("/api/v1/insights/reports/acme/proj/chat", json={"ids": ["nope"]})).status_code == 404
    # a discussion about a selected part, with your question and its own reference folders; the repository's apply too
    own, shared = tmp_path / "own-refs", tmp_path / "repo-refs"
    own.mkdir()
    shared.mkdir()
    r = await api.put("/api/v1/agent-context?scope=insights:acme/proj", json={"dirs": [str(shared)]})
    assert r.status_code == 200 and r.json()["scope"] == "insights:acme/proj", r.text
    c = await api.post(f"/api/v1/insights/reports/acme/proj/{rid}/chat", json={"quote": "Change 1 (#901)\nsecond line", "question": "Why did this land?", "dirs": [str(own)]})
    assert c.status_code == 201, c.text
    await api.wait_job(c.json()["job_id"])
    with h.ctx.db.read() as s:
        prompt = s.get(Job, c.json()["job_id"]).prompt
    assert "> Change 1 (#901)\n> second line" in prompt and "Why did this land?" in prompt and "three things" not in prompt
    assert str(own) in prompt and str(shared) in prompt and prompt.index(str(own)) < prompt.index(str(shared))
    chat_path = c.json()["path"]
    assert "Change 1 (#901)" in (await api.get(f"/api/v1/documents/open?path={chat_path}")).json()["text"].split("\n", 1)[0]
    opened = (await api.get(f"/api/v1/documents/open?path={chat_path}")).json()
    assert opened["insights_repo"] == "acme/proj"
    scope = f"chat:{c.json()['workspace_id']}:{chat_path}"
    assert (await api.get("/api/v1/agent-context", params={"scope": scope})).json()["dirs"] == [str(own.resolve())]
    assert (await api.put("/api/v1/agent-context", params={"scope": scope}, json={"dirs": []})).json()["dirs"] == []
    assert (await api.get("/api/v1/agent-context", params={"scope": "chat:x:../etc/passwd"})).status_code == 400
    bad = await api.post(f"/api/v1/insights/reports/acme/proj/{rid}/chat", json={"quote": "x", "dirs": [str(tmp_path / "missing")]})
    assert bad.status_code == 404 and bad.json()["error"]["code"] == "FOLDER_NOT_FOUND"
    # a daily summary: a selected part, or all of it
    day = store.daily(h.ctx, "acme/proj", "2026-10-20")["path"]
    c = await api.post("/api/v1/insights/file/chat", json={"path": day, "quote": "No notable activity", "question": "Is that right?", "dirs": [str(own)]})
    assert c.status_code == 201, c.text
    await api.wait_job(c.json()["job_id"])
    with h.ctx.db.read() as s:
        prompt = s.get(Job, c.json()["job_id"]).prompt
    assert "> No notable activity" in prompt and "Is that right?" in prompt and "daily summary" in prompt and str(own) in prompt and str(shared) in prompt
    assert "daily summary 2026-10-20" in (await api.get(f"/api/v1/documents/open?path={c.json()['path']}")).json()["text"].split("\n", 1)[0]
    c = await api.post("/api/v1/insights/file/chat", json={"path": day})
    assert c.status_code == 201, c.text
    await api.wait_job(c.json()["job_id"])
    assert (await api.post("/api/v1/insights/file/chat", json={"path": "/etc/passwd"})).status_code == 400
    # discussions next to the material: those about one report share a document, kept out of Chats
    key = f"report:{rid}"
    assert (await api.get("/api/v1/insights/repos/acme/proj/discussion", params={"key": key})).json()["discussion"] is None
    d1 = await api.post(f"/api/v1/insights/reports/acme/proj/{rid}/chat", json={"discussion": key})
    assert d1.status_code == 201, d1.text
    await api.wait_job(d1.json()["job_id"])
    d2 = await api.post(f"/api/v1/insights/reports/acme/proj/{rid}/chat", json={"discussion": key, "quote": "Change 1 (#901)", "question": "And this?", "dirs": [str(own)]})
    assert d2.status_code == 201, d2.text
    await api.wait_job(d2.json()["job_id"])
    assert d2.json()["document_id"] == d1.json()["document_id"] and d2.json()["thread_id"] != d1.json()["thread_id"]
    found = (await api.get("/api/v1/insights/repos/acme/proj/discussion", params={"key": key})).json()["discussion"]
    assert found["document_id"] == d1.json()["document_id"]
    threads = (await api.get(f"/api/v1/documents/{found['document_id']}/threads")).json()["threads"]
    assert len(threads) == 2
    with h.ctx.db.read() as s:
        prompt = s.get(Job, d2.json()["job_id"]).prompt
    assert "> Change 1 (#901)" in prompt and "And this?" in prompt and "<<<REPORT " in prompt and str(own) in prompt
    listed = {x["path"] for x in (await api.get("/api/v1/chats")).json()["chats"]}
    assert found["path"] not in listed and chat_path in listed
    other = await api.post("/api/v1/insights/file/chat", json={"path": day, "discussion": f"file:{day}"})
    assert other.status_code == 201 and other.json()["document_id"] != found["document_id"]
    await api.wait_job(other.json()["job_id"])
    assert (await api.delete(f"/api/v1/insights/reports/acme/proj/{rid}")).status_code == 200
    assert (await api.get(f"/api/v1/insights/reports/acme/proj/{rid}")).status_code == 404
    for bad in ({"repo": "acme/proj", "period": "custom", "since": "2026-11-05", "until": "2026-11-01"}, {"repo": "x", "period": "week"},
                {"repo": "acme/proj", "period": "fortnight"}, {"repo": "acme/proj", "period": "month", "value": "2026-13"},
                {"repo": "acme/proj", "period": "quarter", "value": "2026-Q5"}, {"repo": "acme/proj", "period": "year", "value": "2999"}):
        assert (await api.post("/api/v1/insights/reports", json=bad)).status_code in (400, 422), bad


async def test_trend_chat_works_in_the_data_folder(running_app, tmp_path):
    from backend.app.models import Job

    h, _, _model = await make(running_app, tmp_path, proj_nodes())
    api = h.api
    await pl.daily_summary(h.ctx, "acme/proj", DAY, None)
    c = await api.post("/api/v1/insights/repos/acme/proj/chats", json={"title": "Last two weeks"})
    assert c.status_code == 201, c.text
    c = c.json()
    assert c["repo"] == "acme/proj" and c["path"].startswith(".waw-chat/")
    d = (await api.get(f"/api/v1/documents/open?path={c['path']}")).json()
    r = await api.post(f"/api/v1/documents/{d['document_id']}/threads", json={"whole_document": True, "version_sha256": d["sha256"],
                       "question": "Summarize Nov 1 to Nov 3"})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    tid = r.json()["thread_id"]
    r2 = await api.post(f"/api/v1/threads/{tid}/messages", json={"text": "And the RFCs?"})
    await api.wait_job(r2.json()["job_id"])
    with h.ctx.db.read() as s:
        jobs = s.query(Job).filter(Job.id.in_([r.json()["job_id"], r2.json()["job_id"]])).order_by(Job.created_at).all()
        prompts, ctxs = [j.prompt for j in jobs], [json.loads(j.context_json) for j in jobs]
    data = str(tmp_path / "insights")
    assert all(x["_cwd"] == data for x in ctxs)  # the agent works in the insights data folder
    assert "repository acme/proj" in prompts[0] and data in prompts[0] and f"Daily summaries: {DAY} … {DAY} (1 days)" in prompts[0]
    assert "chatting with the user about their wiki" not in prompts[0]
    assert "[user] Summarize Nov 1 to Nov 3" in prompts[1] and "User message:\n\nAnd the RFCs?" in prompts[1]
    # listed in the repository's Trend page, not in Chats; no distill / update into the wiki
    chats = (await api.get("/api/v1/insights/repos/acme/proj/chats")).json()["chats"]
    assert [(x["title"], x["messages"]) for x in chats] == [("Last two weeks", 4)]
    assert (await api.get("/api/v1/insights/repos/acme/other/chats")).json()["chats"] == []
    assert c["path"] not in [x["path"] for x in (await api.get("/api/v1/chats")).json()["chats"]]
    bad = await api.post(f"/api/v1/threads/{tid}/messages", json={"text": "-", "chat_action": "update"})
    assert bad.status_code == 400
    assert (await api.delete(f"/api/v1/chats?path={c['path']}")).status_code == 200  # archived
    assert (await api.get("/api/v1/insights/repos/acme/proj/chats")).json()["chats"] == []


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


async def test_days_end_by_githubs_clock(monkeypatch):
    from backend.app import gh_limits

    lim = gh_limits.LIMITER
    monkeypatch.setattr(lim, "skew", 0.0)
    monkeypatch.setattr(lim, "skew_at", 0.0)
    monkeypatch.setattr(lim, "refresher", None)
    assert await scheduler._github_now() is None  # GitHub's time unknown: no day is decided
    host = dt.datetime(2026, 11, 18, 0, 30, tzinfo=dt.timezone.utc).timestamp()  # this host: 8 h fast
    monkeypatch.setattr(lim, "clock", lambda: host)
    lim.observe({"date": "Tue, 17 Nov 2026 16:30:00 GMT"})
    assert lim.skew == -8 * 3600
    assert pl.today() == dt.date(2026, 11, 17)  # the 17th is not over yet
    assert (await scheduler._github_now()).hour == 16


def test_last_finished_periods():
    lf = scheduler.last_finished
    assert lf("week", dt.date(2026, 11, 15)) == "2026-11-15"  # a Sunday: that week has ended
    assert lf("week", dt.date(2026, 11, 16)) == "2026-11-15"
    assert pl.calendar_period("week", lf("week", dt.date(2026, 11, 20)))[:2] == ("2026-11-09", "2026-11-15")
    assert lf("month", dt.date(2026, 10, 31)) == "2026-10" and lf("month", dt.date(2026, 11, 3)) == "2026-10"
    assert lf("quarter", dt.date(2026, 11, 3)) == "2026-Q3" and lf("quarter", dt.date(2026, 12, 31)) == "2026-Q4"
    assert lf("quarter", dt.date(2026, 2, 1)) == "2025-Q4"
    assert lf("year", dt.date(2026, 11, 3)) == "2025" and lf("year", dt.date(2026, 12, 31)) == "2026"


async def test_scheduled_reports_after_the_daily_run_once_per_period(running_app, tmp_path):
    h, _, _ = await make(running_app, tmp_path, proj_nodes())
    assert st.load(h.ctx)["repos"][0]["reports"] == {"week": False, "month": False, "quarter": False, "year": False}
    try:
        st.save(h.ctx, {"repos": [{"repo": "acme/proj", "reports": {"daily": True}}]})
    except Exception as e:  # noqa: BLE001
        assert getattr(e, "code", "") == "INVALID_PERIOD"
    else:
        raise AssertionError("a bad period was accepted")
    st.save(h.ctx, {"repos": [{"repo": "acme/proj", "highlights": False, "schedule": {"enabled": True, "time": "05:35"},
                               "reports": {"week": True, "month": True}}]})
    late = dt.datetime(2026, 11, 3, 22, 0, tzinfo=dt.timezone.utc)  # 06:00 UTC+8 on Nov 4: the day summary of Nov 2 is due

    async def settle():
        for _ in range(1200):  # (up to a minute: slower when the whole suite runs)
            if not [j for j in pl.jobs("acme/proj") if j.get("scheduled") and j["status"] == "running"]:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("still running")
    assert await scheduler.tick(h.ctx, late) == ["acme/proj"]  # the daily run first …
    assert scheduler.plan["reports_due"] == []  # the reports wait until the day summaries reach their last day
    assert await scheduler.tick(h.ctx, late) == []  # … one thing at a time
    await settle()
    assert await scheduler.tick(h.ctx, late) == ["acme/proj week"]  # Oct 26 – Nov 1 (the daily run is past Nov 1 now)
    assert scheduler.plan["reports_due"] == ["acme/proj week 2026-11-01", "acme/proj month 2026-10"]
    await settle()
    assert await scheduler.tick(h.ctx, late) == ["acme/proj month"]  # October
    await settle()
    assert await scheduler.tick(h.ctx, late) == [] and scheduler.plan["reports_due"] == []  # each period once
    metas = sorted((tmp_path / "insights" / "reports" / "acme" / "proj").glob("*.json"))
    got = sorted((json.loads(f.read_text())["kind"], json.loads(f.read_text())["since"], json.loads(f.read_text())["scheduled"]) for f in metas)
    assert got == [("monthly", "2026-10-01", True), ("weekly", "2026-10-26", True)], got
    # a daily run that is behind holds the report back; without a daily run the report does not wait for one
    st.save(h.ctx, {"repos": [{"repo": "acme/proj", "schedule": {"enabled": False, "time": "05:35"}, "reports": {"week": True}}]})
    nxt = late + dt.timedelta(days=7)  # Nov 11: the week of Nov 2 – 8 has ended
    assert await scheduler.tick(h.ctx, nxt) == ["acme/proj week"]
    await settle()
    st.save(h.ctx, {"repos": [{"repo": "acme/proj", "schedule": {"enabled": True, "time": "23:59"}, "reports": {"week": True}}]})
    assert await scheduler.tick(h.ctx, nxt + dt.timedelta(days=7)) == []  # before the repository's time of day


async def test_a_task_set_to_the_agent_runs_an_agent_turn(running_app, tmp_path, monkeypatch):
    from backend.app import review_priority
    from backend.app.insights import llm

    h, _, _ = await make(running_app, tmp_path, {})
    h.ctx.extra.pop("insights_llm")
    seen = []

    async def fake_turn(ctx, choice, prompt, title):
        seen.append((choice["provider"], title, prompt))
        return "from the agent"
    monkeypatch.setattr(review_priority, "run_turn", fake_turn)
    st.save(h.ctx, {"llm": {"report": {"mode": "agent", "provider": "codex"}}})
    assert await llm.complete(h.ctx, "report", "write it") == "from the agent"
    assert seen == [("codex", "Insights: report", "write it")]
    assert st.load(h.ctx)["llm"]["daily"]["mode"] == "api"  # the other tasks are unchanged


def test_calendar_periods(monkeypatch):
    monkeypatch.setattr(pl, "today", lambda: dt.date(2026, 11, 13))
    cp = pl.calendar_period
    assert cp("day", "2026-11-12") == ("2026-11-12", "2026-11-12", "2026-11-12", "daily")
    assert cp("week", "2026-11-04") == ("2026-11-02", "2026-11-08", "Week 45, 2026 (Nov 2 – Nov 8)", "weekly")
    assert cp("week", "2026-11-13") == ("2026-11-09", "2026-11-13", "Week 46, 2026 (Nov 9 – Nov 15) (so far)", "weekly")
    assert cp("month", "2026-02") == ("2026-02-01", "2026-02-28", "February 2026", "monthly")
    assert cp("month", "2026-11") == ("2026-11-01", "2026-11-13", "November 2026 (so far)", "monthly")
    assert cp("quarter", "2026-Q1") == ("2026-01-01", "2026-03-31", "Q1 2026", "quarterly")
    assert cp("quarter", "2026-Q3") == ("2026-07-01", "2026-09-30", "Q3 2026", "quarterly")
    assert cp("quarter", "2026-Q4")[1] == "2026-11-13"
    assert cp("year", "2025") == ("2025-01-01", "2025-12-31", "2025", "yearly")
    assert cp("year", None) == ("2026-01-01", "2026-11-13", "2026 (so far)", "yearly")
    assert cp("custom", None, "2026-10-14", "2026-11-13")[2:] == ("2026-10-14 – 2026-11-13", "dates")
    assert pl.weeks(pl.days_of("2026-10-14", "2026-10-27")) == [pl.days_of("2026-10-14", "2026-10-18"), pl.days_of("2026-10-19", "2026-10-25"),
                                                                ["2026-10-26", "2026-10-27"]]


async def test_long_periods_go_through_month_summaries(running_app, tmp_path):
    h, _, model = await make(running_app, tmp_path, proj_nodes())
    text, n = await pl.issue_material(h.ctx, "acme/proj", "2026-08-01", "2026-11-05", None)  # 97 days
    assert n == 97
    assert [x.split(" (")[0] for x in text.split("--- Month ")[1:]] == ["2026-08", "2026-09", "2026-10", "2026-11"]
    month_calls = [p for t, p in model.calls if t == "rollup" and "weekly summaries" in p]
    assert len(month_calls) == 4
    r0 = model.count("rollup")
    await pl.issue_material(h.ctx, "acme/proj", "2026-08-01", "2026-11-05", None)
    assert model.count("rollup") == r0  # finished weeks and months reused


async def test_the_schedule_runs_one_repository_at_a_time_and_waits_for_quota(running_app, tmp_path):
    from backend.app import gh_limits

    h, gh, _ = await make(running_app, tmp_path, proj_nodes())
    st.save(h.ctx, {"repos": [{"repo": "acme/proj", "highlights": True, "schedule": {"enabled": True, "time": "05:00"}},
                              {"repo": "acme/other", "schedule": {"enabled": True, "time": "05:30"}}]})
    late = dt.datetime(2026, 11, 4, 1, 0, tzinfo=dt.timezone.utc)  # 09:00 UTC+8: both due (for Nov 3)
    L = gh_limits.LIMITER
    L.buckets[L.key("graphql")] = gh_limits.Bucket(5000, 1000, 4000, 2e9, 2e9, "headers")
    L.checked = 2e9
    try:
        assert await scheduler.tick(h.ctx, late) == []  # 20% left: not started
        assert "1000 of 5000 graphql" in scheduler.plan["deferred"] and scheduler.plan["due"] == ["acme/proj", "acme/other"]
        L.buckets[L.key("graphql")] = gh_limits.Bucket(5000, 4000, 1000, 2e9, 2e9, "headers")
        n0 = len(gh.queries)
        assert await scheduler.tick(h.ctx, late) == ["acme/proj"]  # the earliest first …
        assert await scheduler.tick(h.ctx, late) == []  # … and nothing else while it runs
        for _ in range(200):
            if not [j for j in pl.jobs() if j.get("scheduled") and j["status"] == "running"]:
                break
            await asyncio.sleep(0.05)
        main = [q for q in gh.queries[n0:] if q.startswith("repo:acme/proj ")]
        assert len(main) == 1  # the day's summary and its XPU highlights: one search
        assert await scheduler.tick(h.ctx, late) == ["acme/other"]  # then the next one
    finally:
        L.buckets.clear()
        L.checked = 0.0


async def test_highlights_per_day_and_flat_without_duplicates(running_app, tmp_path):
    h, _, _ = await make(running_app, tmp_path, {})
    hl = tmp_path / "insights" / "highlight" / "acme" / "proj"
    hl.mkdir(parents=True)
    item = lambda u, s: {"title": u, "url": u, "xpu_relevance_score": s, "xpu_relevance_justification": "j"}  # noqa: E731
    (hl / "highlight_acme_proj_2026-11-10_02-05-02.json").write_text(json.dumps([
        {"File Information": "Highlights of acme/proj from 2026-11-08_02:05:02 to 2026-11-10_02:05:02"}, item("a", 80), item("b", 40)]))
    (hl / "highlight_acme_proj_2026-11-11_02-05-02.json").write_text(json.dumps([
        {"File Information": "Highlights of acme/proj from 2026-11-09_02:05:02 to 2026-11-11_02:05:02"}, item("a", 85), item("c", 90)]))
    (hl / "highlight_acme_proj_2026-11-12_23-00-00.json").write_text(json.dumps([  # an app run of one day
        {"File Information": "Highlights of acme/proj from 2026-11-12_00:00:00 to 2026-11-12_23:59:59"}, item("c", 91)]))
    base = "/api/v1/insights/repos/acme/proj/highlights"
    days = (await h.api.get(base, params={"since": "2026-11-09", "until": "2026-11-12", "view": "day"})).json()["days"]
    assert [d["day"] for d in days] == ["2026-11-12", "2026-11-10", "2026-11-09"]  # a run ending at 02:05 reports the day before
    assert [(x["url"], x["seen_before"]) for x in days[1]["items"]] == [("c", False), ("a", True)]
    assert [(x["url"], x["seen_before"]) for x in days[0]["items"]] == [("c", True)]
    flat = (await h.api.get(base, params={"since": "2026-11-09", "until": "2026-11-12"})).json()["items"]
    assert [(x["url"], x["xpu_relevance_score"], x["days"]) for x in flat] == [
        ("c", 91, ["2026-11-12", "2026-11-10"]), ("a", 85, ["2026-11-10", "2026-11-09"]), ("b", 40, ["2026-11-09"])]
    # discuss the highlights shown: a day (only the new ones), or every item of the dates above a score
    from backend.app.models import Job

    async def prompt_of(body):
        c = await h.api.post(f"{base}/chat", json={"since": "2026-11-09", "until": "2026-11-12", **body})
        assert c.status_code == 201, c.text
        await h.api.wait_job(c.json()["job_id"])
        with h.ctx.db.read() as s:
            return s.get(Job, c.json()["job_id"]).prompt

    p = await prompt_of({"day": "2026-11-10", "only_new": True, "quote": "c", "question": "Why is c urgent?"})
    assert "XPU highlights of acme/proj for 2026-11-10" in p and "- [90] [c](c)" in p and "[a](a)" not in p and "Why is c urgent?" in p
    p = await prompt_of({"min_score": 50})
    assert "for 2026-11-09 – 2026-11-12" in p and "- [91] [c](c)" in p and "(on 2026-11-10, 2026-11-09)" in p and "[b](b)" not in p
    assert "Give me the three things" in p and "Why: j" in p
    r = await h.api.post(f"{base}/chat", json={"since": "2026-11-09", "until": "2026-11-12", "min_score": 99})
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_HIGHLIGHTS"


async def test_new_things_are_marked_remembered_and_counted(running_app, tmp_path):
    h, _, model = await make(running_app, tmp_path, proj_nodes())
    # an earlier day already flagged something as new: later days and reports are told, so it is not flagged again
    store.write_summary(h.ctx, "acme/proj", "2026-11-01", "2026-11-01",
                        "# Day\n\n## 🆕 What's new\n- 🆕 **Ring buffer API** (new feature): x\n- 🆕 **Ring buffer API**: again\n")
    base = model.__call__

    def answer(task, prompt, as_json):
        out = base(task, prompt, as_json)
        if task == "terms":
            return json.dumps({"terms": ["Old widget", "Ring buffer API", {"name": "Device mesh", "what": "a mesh of devices"}]})
        if task == "check" and "a report (" not in prompt:
            return "{}"
        if task == "check":  # "Device mesh v2" extends the known "Device mesh"
            import re
            m = re.search(r"Candidate (\d+): Device mesh v2\n.*?\[(\d+)\] Device mesh:", prompt, re.S)
            return json.dumps({"results": [{"id": int(m[1]), "verdict": "extension", "match": int(m[2]), "confidence": 80}] if m else []})
        extra = "\n- 🆕 **Old widget**: seen before\n- 🆕 **`Ring  buffer` API** again" if task == "report" else ""
        return out if task == "highlight" else out + "\n\n## 🆕 What's new\n- 🆕 **Device mesh v2** (new concept): y\n- 🆕 **XPU graphs**: z" + extra
    h.ctx.extra["insights_llm"] = answer
    await pl.daily_summary(h.ctx, "acme/proj", DAY)
    prompt = next(p for t, p in model.calls if t == "daily")
    assert "## 🆕 What's new" in prompt and 'with "🆕 "' in prompt
    # day to day: against everything before the day (the summary of 2026-11-01 and what it flagged new)
    assert "baseline: everything before 2026-11-03 (1 day(s) with a summary)" in prompt and "Known in the baseline: Ring buffer API" in prompt
    assert prompt.index("everything before") < prompt.index("Below is the detailed information") < prompt.index("Number: 10\n")
    rows = {r["since"]: r for r in store.index(h.ctx, "acme/proj")}
    assert rows["2026-11-01"]["new"] == 1 and rows[DAY]["new"] == 2
    assert store.flagged_new(h.ctx, "acme/proj", "2026-11-04") == ["Device mesh v2", "XPU graphs", "Ring buffer API"]
    assert store.flagged_new(h.ctx, "acme/proj", "2026-11-01") == []  # only days before
    # a report has the section and is compared with its baseline: what the baseline's daily summaries are about
    async def report(**kw):
        meta = pl.new_report(h.ctx, "acme/proj", DAY, DAY, {"issues": True}, "", DAY, None, **kw)
        for _ in range(200):
            rep = pl.report(h.ctx, "acme/proj", meta["id"])
            if rep["status"] != "running":
                return rep, [p for t, p in model.calls if t == "report"][-1].split("Material:")[0]
            await asyncio.sleep(0.05)
    rep, ins = await report(baseline={"mode": "after", "date": "2026-11-01"})
    assert rep["status"] == "done" and rep["new"] == 1, rep  # what the baseline had is unmarked, its bullets in What's new dropped
    assert "➕ **Device mesh v2** *(extends Device mesh)*" in rep["text"]  # an extension: not counted as new
    assert rep["check"]["counts"] == {"extension": 1, "unchecked": 1} and rep["check"]["items"][0]["match"] == "Device mesh"
    assert any(f.startswith("New-things check (model, against 3 registry entries): 0 repeat") for f in rep["facts"])
    assert "Old widget" not in rep["text"] and "Ring  buffer" not in rep["text"]
    assert any(f.startswith("Marks of new things removed (already in the baseline): 2") for f in rep["facts"])
    assert "## 🆕 What's new ::" in ins and "baseline: from 2026-11-01 until the period (2026-11-01 … 2026-11-02) (1 day(s) with a summary)" in ins
    known = ins.split("Known in the baseline: ")[1]
    assert "Ring buffer API" in known and "Old widget" in known and "Device mesh v2" not in known  # the report's own day is material
    assert rep["baseline"]["mode"] == "after" and rep["baseline"]["days"] == 1
    assert any(f.startswith("New compared with: from 2026-11-01") for f in rep["facts"])
    n_terms = model.count("terms")
    assert n_terms == 1
    # the default (the repository's: the period before) — the day before has no summary; the terms are kept per file
    rep, ins = await report()
    assert rep["baseline"]["mode"] == "previous" and "everything before the period (up to 2026-11-02) (1 day(s) with a summary)" in ins
    rep, ins = await report(baseline={"mode": "before", "date": "2026-11-03"})
    assert "everything before 2026-11-03" in ins and "Old widget" in ins and model.count("terms") == n_terms
    # a daily summary knows what the day before was about (its terms) and flagged new
    store.write_summary(h.ctx, "acme/proj", "2026-11-02", "2026-11-02", "# Day\n\n- 🆕 **Old gadget**: x\n")
    await pl.daily_summary(h.ctx, "acme/proj", DAY, force=True)
    prompt = [p for t, p in model.calls if t == "daily"][-1].split("Below is the detailed information")[0]
    assert "everything before 2026-11-03 (2 day(s) with a summary)" in prompt
    assert "Old gadget" in prompt and "Old widget" in prompt and model.count("terms") == n_terms + 1  # only the day before listed now
    # the new things can be marked read: the lists count the unread ones
    api = h.api
    day = next(r for r in (await api.get("/api/v1/insights/repos/acme/proj")).json()["daily"] if r["since"] == DAY)
    assert day["new"] == 2 and day["unread"] == 2 and "new_names" not in day
    r = await api.put("/api/v1/insights/read", json={"repo": "acme/proj", "key": f"f:{day['name']}", "names": ["device  mesh v2"]})
    assert r.status_code == 200 and r.json()["read_new"] == ["device mesh v2"]
    ov = (await api.get("/api/v1/insights/repos/acme/proj")).json()
    assert next(r for r in ov["daily"] if r["since"] == DAY)["unread"] == 1
    assert (await api.get(f"/api/v1/insights/file?path={day['path']}")).json()["read_new"] == ["device mesh v2"]
    rep_row = next(r for r in ov["reports"] if r["id"] == rep["id"])
    assert rep_row["new"] == 1 and rep_row["unread"] == 1  # "Device mesh v2" is an extension there
    await api.put("/api/v1/insights/read", json={"repo": "acme/proj", "key": f"r:{rep['id']}", "names": ["Device mesh v2", "XPU graphs"]})
    ov = (await api.get("/api/v1/insights/repos/acme/proj")).json()
    assert next(r for r in ov["reports"] if r["id"] == rep["id"])["unread"] == 0
    assert (await api.get(f"/api/v1/insights/reports/acme/proj/{rep['id']}")).json()["read_new"] == ["device mesh v2", "xpu graphs"]
    await api.put("/api/v1/insights/read", json={"repo": "acme/proj", "key": f"r:{rep['id']}", "names": ["XPU graphs"], "read": False})
    ov = (await api.get("/api/v1/insights/repos/acme/proj")).json()
    assert next(r for r in ov["reports"] if r["id"] == rep["id"])["unread"] == 1
    assert (await api.put("/api/v1/insights/read", json={"repo": "acme/proj", "key": "x:../y", "names": []})).status_code in (400, 422)
    # select all, mark read: every new thing of several reports and summaries at once (the server knows their names)
    keys = [f"r:{rep['id']}", f"f:{day['name']}", "r:no-such-report"]
    r = await api.put("/api/v1/insights/read-all", json={"repo": "acme/proj", "keys": keys})
    assert r.status_code == 200 and r.json()["marked"] == 2, r.text
    ov = (await api.get("/api/v1/insights/repos/acme/proj")).json()
    assert next(r for r in ov["reports"] if r["id"] == rep["id"])["unread"] == 0 and next(r for r in ov["daily"] if r["since"] == DAY)["unread"] == 0
    await api.put("/api/v1/insights/read-all", json={"repo": "acme/proj", "keys": keys, "read": False})
    ov = (await api.get("/api/v1/insights/repos/acme/proj")).json()
    assert next(r for r in ov["reports"] if r["id"] == rep["id"])["unread"] == 1 and next(r for r in ov["daily"] if r["since"] == DAY)["unread"] == 2
    assert (await api.put("/api/v1/insights/read-all", json={"repo": "acme/proj", "keys": ["f:../x"]})).status_code == 400


def test_baseline_windows():
    w = pl.baseline_window
    for kind in ("daily", "weekly", "monthly", "quarterly", "yearly", "dates"):  # day to day, month to month …: all before
        assert w({"mode": "previous"}, "2026-11-09", "2026-11-15", kind) == ("0001-01-01", "2026-11-08", "everything before the period (up to 2026-11-08)")
    assert w({"mode": "week"}, "2026-11-09", "2026-11-15", "weekly")[:2] == ("2026-11-02", "2026-11-08")
    assert w({"mode": "quarter"}, "2026-10-01", "2026-12-31")[:2] == ("2026-07-01", "2026-09-30")
    assert w({"mode": "year"}, "2026-01-01", "2026-12-31")[:2] == ("2025-01-01", "2025-12-31")
    assert w({"mode": "day"}, "2026-11-03", "2026-11-03")[:2] == ("2026-11-02", "2026-11-02")
    assert w({"mode": "month"}, "2026-03-31", "2026-03-31")[:2] == ("2026-02-28", "2026-03-30")
    assert w({"mode": "week"}, "2026-11-09", "2026-11-30", "monthly")[:2] == ("2026-11-02", "2026-11-08")
    assert w({"mode": "before", "date": "2026-06-01"}, "2026-11-09", "2026-11-15")[:2] == ("0001-01-01", "2026-05-31")
    assert w({"mode": "before", "date": "2026-12-01"}, "2026-11-09", "2026-11-15")[1:] == ("2026-11-08", "everything before 2026-11-09")
    assert w({"mode": "after", "date": "2026-06-01"}, "2026-11-09", "2026-11-15")[:2] == ("2026-06-01", "2026-11-08")
    assert w({"mode": "after", "date": "2026-11-10"}, "2026-11-09", "2026-11-15") is None


def test_unmark_what_was_seen_before():
    text = ("# R\n\n- 🆕 **Aa** grew, 🆕 **`Bb`** too\n\n## 🆕 What's new\n- 🆕 **Aa**: x\n- 🆕 **Cc**: y\n- 🆕 **Aa** and 🆕 **Cc**\n\n## Other\n- 🆕 **Aa** z")
    out, gone = store.unmark(text, {"aa", "bb"})
    assert gone == ["Aa", "`Bb`"]
    assert out == ("# R\n\n- **Aa** grew, **`Bb`** too\n\n## 🆕 What's new\n- 🆕 **Cc**: y\n- **Aa** and 🆕 **Cc**\n\n## Other\n- **Aa** z")
    assert store.unmark(text, set()) == (text, [])
    sub = "## 🆕 What's new\n- 🆕 **Aa** additions:\n  - `x`\n  - 🆕 **Cc** inside\n- 🆕 **Dd**: w\n"
    assert store.unmark(sub, {"aa"})[0] == "## 🆕 What's new\n  - 🆕 **Cc** inside\n- 🆕 **Dd**: w\n"
    # looser: case, backticks, punctuation, "()" and a trailing note do not count
    k = store.match_key
    assert k("`torch.export.load(weights_only=)`") == k("torch.export.load weights_only") == "torch export load weights only"
    assert k("`torch.assert_async`") == k("torch._assert_async") and k("RVV FusedAdam()") == k("rvv fusedadam")
    assert k("Ring buffer API (new feature)") == k("ring-buffer API") and k("()") == ""
    assert store.unmark("- 🆕 **()**", {""}) == ("- 🆕 **()**", [])


async def test_baseline_settings(running_app, tmp_path):
    h, _, _ = await make(running_app, tmp_path, proj_nodes())
    assert st.repo_cfg(h.ctx, "pytorch/pytorch")["new_baseline"] == {"mode": "previous", "date": None}
    st.save(h.ctx, {"repos": [{"repo": "acme/proj", "new_baseline": {"mode": "before", "date": "2026-01-01"}}]})
    assert st.repo_cfg(h.ctx, "acme/proj")["new_baseline"] == {"mode": "before", "date": "2026-01-01"}
    r = await h.api.put("/api/v1/insights/settings", json={"repos": [{"repo": "acme/proj", "new_baseline": {"mode": "after"}}]})
    assert r.status_code == 400
    r = await h.api.post("/api/v1/insights/reports", json={"repo": "acme/proj", "period": "day", "value": DAY, "baseline": {"mode": "after", "date": "x"}})
    assert r.status_code == 400


def test_registry_merges_strictly():
    t = lambda name, *aliases, what="": {"name": name, "aliases": list(aliases), "what": what}  # noqa: E731
    reg = registry.build([
        ("2026-07-01", [t("weights_only=True for object collectives", "c10d", "weights-only unpickler"), t("safetensors")]),
        ("2026-07-02", [t("`Weights_only=True` for object collectives()"), t("c10d", "c10d library"),
                        t("weights-only unpickler", "pickle guard"), t("pickle guard"), t("SafeTensors loading", "safetensors")]),
        ("2026-07-03", [t("c10d library", what="the distributed backend library")]),
    ])
    names = {e["name"]: e for e in reg}
    w = names["weights_only=True for object collectives"]
    assert w["days"] == 2 and w["last"] == "2026-07-02"  # its main name again (case, backticks, () do not count)
    assert "c10d" in names  # a short other spelling ("c10d") does not merge
    assert "weights-only unpickler" in w["aliases"] and "pickle guard" in names  # its name is a specific other spelling: merged; alias to alias: not
    assert names["safetensors"]["aliases"] == ["SafeTensors loading"]  # its other spelling is an entry's main name
    assert names["c10d"]["what"] == "the distributed backend library" and names["c10d"]["days"] == 2
    near = registry.nearest(reg, [registry._toks("the distributed backend")])
    assert reg[near[0][0]]["name"] == "c10d"
    assert registry.entries_of(["Plain name", {"name": " A  b ", "aliases": ["A  b", "x"], "refs": [1, "2"]}, {"x": 1}]) == [
        {"name": "Plain name", "aliases": [], "kind": "", "area": "", "what": "", "refs": []},
        {"name": "A b", "aliases": ["x"], "kind": "", "area": "", "what": "", "refs": [1]}]


async def test_new_things_check():
    reg = registry.build([("2026-09-01", [{"name": "FSDP2 per-param mixed precision", "what": "a policy per parameter"},
                                           {"name": "CUDA graphs", "what": "capture and replay"}])])
    text = ("# R\n\n- 🆕 **FSDP2 per-parameter mixed-precision policy** landed; 🆕 **Breakable CUDA graphs** too\n\n"
            "## 🆕 What's new\n- 🆕 **FSDP2 per-parameter mixed-precision policy**: x\n- 🆕 **Breakable CUDA graphs**: y\n"
            "- 🆕 **Breakable graphs**: same\n- 🆕 **torch.float8_e5m3fnu**: a dtype\n- 🆕 **Low-confidence thing**: z\n")
    seen = []

    def model(task, prompt, as_json):
        seen.append((task, prompt))
        fsdp = next(i for i, e in enumerate(reg) if e["name"].startswith("FSDP2"))
        cuda = 1 - fsdp
        return json.dumps({"results": [
            {"id": 1, "verdict": "repeat", "match": fsdp, "confidence": 92, "why": "same"},
            {"id": 2, "verdict": "extension", "match": cuda, "confidence": 70, "why": "new part"},
            {"id": 3, "verdict": "duplicate", "same_as": 2, "confidence": 90},
            {"id": 4, "verdict": "new", "confidence": 85},
            {"id": 5, "verdict": "repeat", "match": fsdp, "confidence": 40}]})
    ctx = type("Ctx", (), {"extra": {"insights_llm": model}})()
    out, chk = await registry.check(ctx, "acme/proj", text, reg, "a report", "everything before")
    assert len(seen) == 1 and seen[0][0] == "check" and "All candidates of this text: 1. FSDP2" in seen[0][1]
    assert "[0] FSDP2 per-param mixed precision" in seen[0][1] or "[1] FSDP2 per-param mixed precision" in seen[0][1]
    assert chk["counts"] == {"repeat": 1, "extension": 1, "duplicate": 1, "new": 2} and chk["entries"] == 2
    assert out == ("# R\n\n- **FSDP2 per-parameter mixed-precision policy** landed; ➕ **Breakable CUDA graphs** *(extends CUDA graphs)* too\n\n"
                   "## 🆕 What's new\n- ➕ **Breakable CUDA graphs**: y\n"
                   "- 🆕 **torch.float8_e5m3fnu**: a dtype\n- 🆕 **Low-confidence thing**: z\n")
    assert store.new_names(out) == ["torch.float8_e5m3fnu", "Low-confidence thing"]  # an extension is not counted as new
    assert "1 extension (marked ➕, not counted as new), 2 new" in registry.summary(chk)
    # a failed or unreadable answer leaves the text as it was
    ctx.extra["insights_llm"] = lambda task, prompt, as_json: "no json"
    assert (await registry.check(ctx, "acme/proj", text, reg, "a report", "x"))[0] == text
