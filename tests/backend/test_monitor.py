"""Agent monitor: OpenCode / Codex / Claude Code sessions found in folders or given by id, their status (needs you,
working, error, idle), the conversation, continuing it, opening it in a terminal."""

from __future__ import annotations

import asyncio
import json
import os
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

    subprocess.run([str(FAKES / "fake-opencode"), "run", "--", "hello"], cwd=workspace, env={**os.environ, "PWD": str(workspace)}, check=True,
                   capture_output=True)  # v2 runs in $PWD (as a shell sets it)
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
    # control commands (/model, /effort, /agent): for that message
    for f in ("opencode_last_model", "opencode_last_variant", "opencode_last_agent"):
        (fake_env / f).unlink(missing_ok=True)
    r = await api.post(f"/api/v1/monitor/session/opencode/{oc_id}/send", json={"text": "with controls", "model": "acme/big", "variant": "high", "agent": "plan"})
    assert r.status_code == 202, r.text
    for _ in range(100):
        d = (await api.get(f"/api/v1/monitor/session/opencode/{oc_id}")).json()
        if d["send"] and d["send"]["state"] != "running":
            break
        await asyncio.sleep(0.05)
    assert d["send"]["state"] == "done", d
    assert (fake_env / "opencode_last_model").read_text() == "acme/big" and (fake_env / "opencode_last_variant").read_text() == "high"
    assert (fake_env / "opencode_last_agent").read_text() == "plan"
    for bad in ({"model": "--upload=x"}, {"agent": "a b"}, {"variant": "-x"}):
        assert (await api.post(f"/api/v1/monitor/session/opencode/{oc_id}/send", json={"text": "x", **bad})).status_code == 400, bad
    r = await api.post("/api/v1/monitor/session/claude/cl-q/send", json={"text": "x", "agent": "plan"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "UNSUPPORTED_CONTROL"
    # pictures pasted into the composer go with the message: OpenCode --file, Claude Code by their path
    import base64

    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    (fake_env / "opencode_last_files").unlink(missing_ok=True)

    async def sent(provider, sid, body):
        r = await api.post(f"/api/v1/monitor/session/{provider}/{sid}/send", json=body)
        assert r.status_code == 202, r.text
        for _ in range(100):
            d = (await api.get(f"/api/v1/monitor/session/{provider}/{sid}")).json()
            if d["send"] and d["send"]["state"] != "running":
                break
            await asyncio.sleep(0.05)
        assert d["send"]["state"] == "done", d
        return d
    await sent("opencode", oc_id, {"text": "what is in this picture?", "images": [url, url]})
    files = (fake_env / "opencode_last_files").read_text().split()
    assert len(files) == 2 and all(Path(f).read_bytes() == png for f in files)
    d = await sent("claude", "cl-q", {"text": "and this one?", "images": [url]})
    assert "[Attached image: " in d["messages"][-2]["text"]
    for bad in (["data:image/png;base64,AAAA"], ["https://example.com/x.png"], [url] * 7):
        r = await api.post(f"/api/v1/monitor/session/opencode/{oc_id}/send", json={"text": "x", "images": bad})
        assert r.status_code == 400, r.text
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
    subprocess.run([str(FAKES / "fake-opencode"), "run", "--", "hello"], cwd=proj, env={**os.environ, "PWD": str(proj)}, check=True,
                   capture_output=True)  # v2 runs in $PWD (as a shell sets it)
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


async def test_sessions_of_a_folder_on_a_machine(running_app, settings, homes, tmp_path, fake_env, workspace, monkeypatch):
    """A folder on a machine (`box:/path`): its sessions are listed and read there over ssh (the fake ssh runs the
    commands here, so the machine's transcripts are this test's), keyed with the machine everywhere."""
    from backend.app import monitor_remote
    from backend.app.config import DevHost, DevSettings
    from backend.app.development import targets

    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    targets._HOMES.clear()
    monitor_remote.forget()
    settings.development = DevSettings(ssh=str(FAKES / "fake-ssh"), worktrees_dir=tmp_path / "worktrees", hosts=[
        DevHost(name="box", ssh="me@box", work_dir=str(tmp_path / "remote"), opencode=str(FAKES / "fake-opencode"), codex=str(FAKES / "fake-codex")),
        DevHost(name="down", ssh="unreachable.example", work_dir="/tmp/x")])
    h = await running_app(settings)
    api = h.api
    proj = tmp_path / "rproj"
    proj.mkdir()
    write_codex(homes["codex"], "cx-r", str(proj), [ev("task_started"), ev("exec_approval_request", call_id="c1")])
    write_claude(homes["claude"], "cl-r", str(proj), [{"type": "user", "message": {"role": "user", "content": "plan it"}},
                                                    {"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "Done."}]}}])
    import subprocess

    subprocess.run([str(FAKES / "fake-opencode"), "run", "--", "hello"], cwd=proj, env={**os.environ, "PWD": str(proj)}, check=True, capture_output=True)
    oc_id = json.loads((fake_env / "opencode_sessions.json").read_text())[-1]["id"]

    # added: checked on the machine; a missing folder there is refused
    r = await api.put("/api/v1/monitor/folders", json={"folders": [f"box:{proj}"]})
    assert r.status_code == 200, r.text
    f = r.json()["folders"][0]
    assert f["path"] == f"box:{proj}" and f["machine"] == "box" and f["name"] == "rproj @box"
    assert (await api.put("/api/v1/monitor/folders", json={"folders": [f"box:{proj}/nope"]})).status_code == 404
    assert (await api.put("/api/v1/monitor/folders", json={"folders": [f"nobox:{proj}"]})).status_code == 400

    found = (await api.get("/api/v1/monitor/discover", params={"folders": f"box:{proj}\ndown:/srv"})).json()
    keys = {(x["provider"], x["id"], x["machine"]) for x in found["sessions"]}
    assert {("codex", "cx-r", "box"), ("claude", "cl-r", "box"), ("opencode", oc_id, "box")} <= keys
    assert all(x["folder"] == f"box:{proj}" for x in found["sessions"])
    assert [e["machine"] for e in found["remote_errors"]] == ["down"]

    sessions = [{"provider": p, "id": i, "machine": "box"} for p, i in (("codex", "cx-r"), ("claude", "cl-r"), ("opencode", oc_id))]
    await api.put("/api/v1/monitor/watched", json={"sessions": [*sessions, {"provider": "claude", "id": "cl-x", "machine": "down"}]})
    st = (await api.get("/api/v1/monitor/status")).json()
    by = {(i["provider"], i["id"], i["machine"]): i for i in st["items"]}
    assert by[("codex", "cx-r", "box")]["status"]["state"] == "needs_input"
    assert by[("claude", "cl-r", "box")]["status"]["state"] == "idle"
    assert by[("opencode", oc_id, "box")]["status"]["state"] == "idle" and by[("opencode", oc_id, "box")]["folder"] == f"box:{proj}"
    down = by[("claude", "cl-x", "down")]["status"]
    assert down["state"] == "missing" and "ssh" in down["reason"]

    # the conversation and continuing it, there
    d = (await api.get(f"/api/v1/monitor/session/opencode/{oc_id}", params={"machine": "box"})).json()
    assert d["machine"] == "box" and d["messages"][-1]["text"] == "echo: hello"
    r = await api.post(f"/api/v1/monitor/session/opencode/{oc_id}/send", params={"machine": "box"}, json={"text": "more please"})
    assert r.status_code == 202, r.text
    for _ in range(200):
        d = (await api.get(f"/api/v1/monitor/session/opencode/{oc_id}", params={"machine": "box"})).json()
        if d["send"] and d["send"]["state"] != "running":
            break
        await asyncio.sleep(0.05)
    assert d["send"]["state"] == "done", d
    assert d["messages"][-1]["text"] == "echo: more please"
    calls = (fake_env / "ssh_calls.log").read_text()
    assert "me@box" in calls

    # its own UI in a terminal on the machine, in the session's folder
    if __import__("shutil").which("tmux"):
        r = await api.post("/api/v1/monitor/session/claude/cl-r/terminal", params={"machine": "box"})
        assert r.status_code == 201, r.text
        t = r.json()
        assert t["machine"] == "box" and t["cwd"] == str(proj) and t["label"].endswith("@box")
        await api.delete(f"/api/v1/terminals/{t['id']}")

    # renamed and deleted with the machine in the key
    assert (await api.put("/api/v1/monitor/session/claude/cl-r/label", params={"machine": "box"}, json={"label": "remote one"})).status_code == 200
    st = (await api.get("/api/v1/monitor/status")).json()
    assert next(i for i in st["items"] if i["id"] == "cl-r")["label"] == "remote one"
    r = (await api.post("/api/v1/monitor/delete", json={"sessions": [{"provider": "claude", "id": "cl-r", "machine": "box"}]})).json()
    assert r["deleted"] == ["claude:cl-r@box"], r
    assert not list(homes["claude"].glob("projects/*/cl-r.jsonl"))


async def test_pasted_pictures_are_copied_to_the_machine(monkeypatch, tmp_path):
    from backend.app import monitor

    calls = []

    class T:
        async def expand(self, p):
            assert p.startswith("~/")
            return str(tmp_path) + p[1:]

        async def run(self, args, timeout=60, stdin=None, **_):
            calls.append(args)
            import subprocess

            r = subprocess.run(args, input=stdin, capture_output=True)
            return r.returncode, r.stdout.decode(), r.stderr.decode()

    monkeypatch.setattr(monitor.remote, "target", lambda ctx, machine: T())
    out = await monitor._remote_images(None, "box", [(b"\x89PNG one", "png"), (b"\xff\xd8 two", "jpg")])
    assert [Path(p).name for p in out] == ["image-1.png", "image-2.jpg"]
    assert Path(out[0]).read_bytes() == b"\x89PNG one" and Path(out[1]).read_bytes() == b"\xff\xd8 two"
    assert all(str(tmp_path / ".cache/wiki-agent-workspace/attachments") in p for p in out)
    assert await monitor._remote_images(None, "box", []) == [] and len(calls) == 2
