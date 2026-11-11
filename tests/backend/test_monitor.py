"""Agent monitor: OpenCode / Codex / Claude Code sessions found in folders or given by id, their status (needs you,
working, error, idle), the conversation, continuing it, opening it in a terminal."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from conftest import FAKES


@pytest.fixture
def homes(tmp_path, monkeypatch, settings):
    claude = tmp_path / "claude-home"
    (claude / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    from backend.app.config import ProviderConfig

    settings.providers["claude"] = ProviderConfig(bin=str(FAKES / "fake-claude"))
    return {"claude": claude, "codex": tmp_path / "codex-home"}


def write_codex(home: Path, sid: str, cwd: str, events: list[dict]) -> Path:
    d = home / "sessions" / "2026" / "11" / "11"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-2026-11-11T00-00-00-{sid}.jsonl"
    rows = [{"timestamp": "2026-11-11T00:00:00Z", "type": "session_meta", "payload": {"id": sid, "cwd": cwd}}, *events]
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return f


def write_claude(home: Path, sid: str, cwd: str, entries: list[dict]) -> Path:
    d = home / "projects" / cwd.replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text("".join(json.dumps({"cwd": cwd, "timestamp": "2026-11-11T00:00:00Z", **e}) + "\n" for e in entries))
    return f


def ev(t: str, **p) -> dict:
    return {"timestamp": "2026-11-11T00:00:01Z", "type": "event_msg", "payload": {"type": t, **p}}


async def test_monitor_discover_status_send(running_app, settings, homes, tmp_path, fake_env, workspace):
    h = await running_app(settings)
    api = h.api
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    # Codex: one finished, one waiting for an approval
    write_codex(homes["codex"], "cx-done", str(proj), [ev("task_started"), {"timestamp": "x", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "All done."}]}}, ev("task_complete")])
    write_codex(homes["codex"], "cx-wait", str(proj / "sub"), [ev("item_completed", item={"type": "UserMessage", "text": "delete the build dir"}), ev("task_started"), ev("exec_approval_request", call_id="c1")])
    # Claude: one asking a question at the end of its turn, one in another folder
    write_claude(homes["claude"], "cl-q", str(proj), [{"type": "user", "message": {"role": "user", "content": "plan it"}},
                                                    {"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "Should I also update the tests?"}]}}])
    write_claude(homes["claude"], "cl-other", str(tmp_path / "elsewhere"), [{"type": "user", "message": {"role": "user", "content": "hi"}}])
    # OpenCode: a session made in the wiki folder (via a fake run)
    import subprocess

    subprocess.run([str(FAKES / "fake-opencode"), "run", "--dir", str(workspace), "--", "hello"], cwd=workspace, check=True, capture_output=True)
    oc_id = json.loads((fake_env / "opencode_sessions.json").read_text())[-1]["id"]

    assert (await api.put("/api/v1/monitor/folders", json={"folders": [str(proj)]})).json()["folders"][0]["path"] == str(proj)
    found = (await api.get("/api/v1/monitor/discover", params={"folders": f"{proj}\n{workspace}"})).json()["sessions"]
    keys = {(x["provider"], x["id"]) for x in found}
    assert {("codex", "cx-done"), ("codex", "cx-wait"), ("claude", "cl-q"), ("opencode", oc_id)} <= keys and ("claude", "cl-other") not in keys
    # watch them, plus one by id that does not exist
    sessions = [{"provider": p, "id": i} for p, i in [("codex", "cx-done"), ("codex", "cx-wait"), ("claude", "cl-q"), ("opencode", oc_id)]]
    await api.put("/api/v1/monitor/watched", json={"sessions": sessions})
    r = (await api.post("/api/v1/monitor/watched", json={"provider": "claude", "id": "nope-123", "label": "mine"})).json()
    assert r["found"] is False
    st = (await api.get("/api/v1/monitor/status")).json()
    by = {(i["provider"], i["id"]): i for i in st["items"]}
    assert by[("codex", "cx-done")]["status"]["state"] == "idle"
    assert by[("codex", "cx-wait")]["status"]["state"] == "needs_input" and "approval" in by[("codex", "cx-wait")]["status"]["reason"]
    assert by[("claude", "cl-q")]["status"]["state"] == "needs_input"
    assert by[("claude", "nope-123")]["status"]["state"] == "missing" and by[("claude", "nope-123")]["label"] == "mine"
    assert by[("opencode", oc_id)]["status"]["state"] == "idle" and by[("opencode", oc_id)]["directory"] == str(workspace)
    assert st["alarms"] == 2
    # the conversation, and continuing it (OpenCode, Claude)
    d = (await api.get(f"/api/v1/monitor/session/opencode/{oc_id}")).json()
    assert [m["role"] for m in d["messages"]] == ["user", "assistant"] and d["messages"][1]["text"] == "echo: hello"
    for provider, sid, expect in (("opencode", oc_id, "echo: more please"), ("claude", "cl-q", "claude echo: more please")):
        r = await api.post(f"/api/v1/monitor/session/{provider}/{sid}/send", json={"text": "more please"})
        assert r.status_code == 202, r.text
        for _ in range(100):
            d = (await api.get(f"/api/v1/monitor/session/{provider}/{sid}")).json()
            if d["send"] and d["send"]["state"] != "running":
                break
            await asyncio.sleep(0.05)
        assert d["send"]["state"] == "done", d
        assert d["messages"][-1]["text"] == expect
    assert (await api.post("/api/v1/monitor/session/claude/nope-123/send", json={"text": "x"})).status_code == 404
    # codex: the error of the last turn
    write_codex(homes["codex"], "cx-err", str(proj), [ev("task_started"), ev("task_complete", error={"message": "401 Unauthorized"})])
    d = (await api.get("/api/v1/monitor/session/codex/cx-err")).json()
    assert d["status"]["state"] == "error" and "401" in d["status"]["reason"]


async def test_opencode_question_and_tool_wait(running_app, settings, homes, fake_env):
    h = await running_app(settings)
    now = int(time.time() * 1000)
    old = now - 10 * 60 * 1000
    for sid, tool, start, upd in (("ses_q", "question", now, now), ("ses_perm", "bash", old, old), ("ses_run", "bash", now, now)):
        (fake_env / f"opencode_export_{sid}.json").write_text(json.dumps({"info": {"id": sid, "directory": "/tmp", "time": {"updated": upd}}, "messages": [
            {"info": {"role": "user", "time": {"created": start}}, "parts": [{"type": "text", "text": "go"}]},
            {"info": {"role": "assistant", "time": {"created": start}}, "parts": [{"type": "tool", "tool": tool, "state": {"status": "running", "time": {"start": start}, "input": {}}}]}]}))
    out = {}
    for sid in ("ses_q", "ses_perm", "ses_run"):
        out[sid] = (await h.api.get(f"/api/v1/monitor/session/opencode/{sid}")).json()["status"]
    assert out["ses_q"]["state"] == "needs_input" and "question" in out["ses_q"]["reason"]
    assert out["ses_perm"]["state"] == "needs_input" and "permission" in out["ses_perm"]["reason"]
    assert out["ses_run"]["state"] == "working"


@pytest.mark.skipif(__import__("shutil").which("tmux") is None, reason="tmux not installed")
async def test_open_session_in_terminal(running_app, settings, homes, tmp_path):
    h = await running_app(settings)
    write_claude(homes["claude"], "cl-t", str(tmp_path), [{"type": "user", "message": {"role": "user", "content": "hi"}}])
    t = (await h.api.post("/api/v1/monitor/session/claude/cl-t/terminal")).json()
    assert t["cwd"] == str(tmp_path) and t["label"].startswith("claude")
    await h.api.delete(f"/api/v1/terminals/{t['id']}")


async def test_recent_sessions_of_checked_folders_are_monitored(running_app, settings, homes, tmp_path, fake_env):
    import os

    h = await running_app(settings)
    api = h.api
    proj = tmp_path / "auto"
    proj.mkdir()
    write_codex(homes["codex"], "cx-new", str(proj), [ev("task_started"), ev("task_complete")])
    old = write_codex(homes["codex"], "cx-old", str(proj), [ev("task_started"), ev("task_complete")])
    t = time.time() - 20 * 86400
    os.utime(old, (t, t))
    await api.put("/api/v1/monitor/folders", json={"folders": [str(proj)]})
    st = (await api.get("/api/v1/monitor/status")).json()
    ids = {i["id"]: i for i in st["items"]}
    assert "cx-new" in ids and ids["cx-new"]["auto"] is True and "cx-old" not in ids  # nothing watched explicitly
    # a wider window takes the older one; excluding removes one; unchecking the folder stops it
    await api.put("/api/v1/monitor/auto", json={"folders": [str(proj)], "days": 30, "exclude": ["codex:cx-new"]})
    ids = {i["id"] for i in (await api.get("/api/v1/monitor/status")).json()["items"]}
    assert "cx-old" in ids and "cx-new" not in ids
    d = (await api.get("/api/v1/monitor/discover", params={"folders": str(proj)})).json()["sessions"]
    assert {x["id"]: (x["auto"], x["excluded"]) for x in d} == {"cx-old": (True, False), "cx-new": (False, True)}
    await api.put("/api/v1/monitor/auto", json={"folders": [], "days": 30, "exclude": []})
    assert all(not i["auto"] for i in (await api.get("/api/v1/monitor/status")).json()["items"])


async def test_rename_and_delete_sessions(running_app, settings, homes, tmp_path, fake_env):
    import subprocess

    h = await running_app(settings)
    api = h.api
    proj = tmp_path / "del"
    proj.mkdir()
    write_codex(homes["codex"], "cx-del", str(proj), [ev("task_started"), ev("task_complete")])
    cl = write_claude(homes["claude"], "cl-del", str(proj), [{"type": "user", "message": {"role": "user", "content": "hi"}}])
    subprocess.run([str(FAKES / "fake-opencode"), "run", "--dir", str(proj), "--", "hello"], cwd=proj, check=True, capture_output=True)
    oc = json.loads((fake_env / "opencode_sessions.json").read_text())[-1]["id"]
    await api.put("/api/v1/monitor/watched", json={"sessions": [{"provider": "codex", "id": "cx-del"}, {"provider": "claude", "id": "cl-del"}, {"provider": "opencode", "id": oc}]})
    # rename: the name shown everywhere (status, discovery)
    assert (await api.put(f"/api/v1/monitor/session/opencode/{oc}/label", json={"label": "my run"})).json()["label"] == "my run"
    st = {i["id"]: i for i in (await api.get("/api/v1/monitor/status")).json()["items"]}
    assert st[oc]["label"] == "my run"
    d = (await api.get("/api/v1/monitor/discover", params={"folders": str(proj)})).json()["sessions"]
    assert next(x for x in d if x["id"] == oc)["label"] == "my run"
    await api.put(f"/api/v1/monitor/session/opencode/{oc}/label", json={"label": ""})
    assert {i["id"]: i for i in (await api.get("/api/v1/monitor/status")).json()["items"]}[oc]["label"] is None
    # delete: each provider's own way; gone from the monitor
    r = (await api.post("/api/v1/monitor/delete", json={"sessions": [{"provider": "codex", "id": "cx-del"}, {"provider": "claude", "id": "cl-del"},
                                                                    {"provider": "opencode", "id": oc}, {"provider": "codex", "id": "nope-1"}]})).json()
    # a session that no longer exists on this machine (e.g. added by a wrong id) is simply no longer monitored
    assert set(r["deleted"]) == {"codex:cx-del", "claude:cl-del", f"opencode:{oc}", "codex:nope-1"} and r["failed"] == []
    assert not cl.exists() and not list(homes["codex"].rglob("*cx-del*"))
    assert oc not in (fake_env / "opencode_sessions.json").read_text()
    assert (await api.get("/api/v1/monitor/watched")).json()["sessions"] == []


async def test_opencode_listing_shares_runs_keeps_last_good_and_never_blocks_on_refresh(monkeypatch, tmp_path):
    """Slow `opencode session list` (many sessions, a busy host): concurrent callers share one run per folder, a
    failed run keeps the last good listing, and a stale listing is returned while it is refreshed."""
    import asyncio

    from backend.app import monitor

    calls: list[str] = []
    result: dict = {"value": {"ses_1": {"id": "ses_1"}}, "delay": 0.2}

    async def fake_list(exe, cwd):
        calls.append(str(cwd))
        await asyncio.sleep(result["delay"])
        return result["value"]

    monkeypatch.setattr(monitor, "_opencode_list_in", fake_list)
    monkeypatch.setattr(monitor, "_oc_dir", {})
    monkeypatch.setattr(monitor, "_oc_running", {})
    d = str(tmp_path)
    a, b = await asyncio.gather(monitor._oc_folder("oc", d), monitor._oc_folder("oc", d))
    assert a == b == {"ses_1": {"id": "ses_1"}} and calls == [d]  # one run for both
    # fresh: no new run
    assert await monitor._oc_folder("oc", d) == a and calls == [d]
    # stale: the old listing comes back at once, a refresh runs in the background
    monitor._oc_dir[d] = (monitor._oc_dir[d][0] - monitor.OC_FRESH - 1, monitor._oc_dir[d][1])
    result.update(value=None, delay=0.5)  # ... and it fails
    t0 = asyncio.get_running_loop().time()
    assert await monitor._oc_folder("oc", d) == a and asyncio.get_running_loop().time() - t0 < 0.3
    await asyncio.sleep(0.7)
    assert len(calls) == 2 and monitor._oc_dir[d][1] == a  # failed: the last good listing is kept
