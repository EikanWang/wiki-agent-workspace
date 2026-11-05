"""Edits, diff, preview-before-apply, codex sandbox (D12, §8, §14)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from conftest import Api, anchor_for


async def test_user_authorized_edit_shows_diff_without_extra_prompt(api: Api, workspace: Path, fake_env: Path):
    doc = await api.open_doc()
    did = doc["document_id"]
    a = anchor_for(doc["text"], "TorchInductor converts the graph into device code.")
    r = await api.post(f"/api/v1/documents/{did}/threads", json={
        **a, "version_sha256": doc["sha256"],
        "question": "Please change this sentence. [[edit:docs/guide.md:::device code.:::target-device code.]]"})
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "completed"
    assert any(e["type"] == "tool_activity" for e in ev)
    # exactly one agent turn ran; no app approval round-trip
    calls = [json.loads(l) for l in (fake_env / "opencode_calls.log").read_text().splitlines() if '"run"' in l]
    assert len(calls) == 1
    # the file really changed; the watcher (or next open) records the new version
    new_text = (workspace / "docs" / "guide.md").read_text()
    assert "target-device code." in new_text
    for _ in range(40):
        versions = (await api.get(f"/api/v1/documents/{did}/versions")).json()["versions"]
        if len(versions) >= 2:
            break
        await asyncio.sleep(0.1)
    doc2 = await api.open_doc()
    assert doc2["sha256"] != doc["sha256"]
    r = await api.get(f"/api/v1/documents/{did}/diff", params={"from": doc["sha256"], "to": "current"})
    u = r.json()["unified"]
    assert "-TorchInductor converts the graph into device code." in u
    assert "+TorchInductor converts the graph into target-device code." in u
    # anchor re-resolves: the old quote is orphaned (text changed), old quote preserved
    t = (await api.threads(did))["threads"][0]
    assert t["anchor_state"] == "orphaned" and t["anchor"]["quote"] == a["quote"]


async def test_preview_does_not_touch_worktree_until_apply(api: Api, workspace: Path):
    doc = await api.open_doc()
    did = doc["document_id"]
    before = (workspace / "docs" / "guide.md").read_bytes()
    r = await api.post(f"/api/v1/documents/{did}/previews", json={
        "instruction": "Change it. [[edit:docs/guide.md:::device code.:::PREVIEWED code.]]", "base_sha256": doc["sha256"]})
    assert r.status_code == 201, r.text
    pv = r.json()
    ev = await api.wait_job(pv["job_id"])
    assert ev[-1]["type"] == "completed"
    for _ in range(50):
        p = (await api.get(f"/api/v1/previews/{pv['preview_id']}")).json()
        if p["status"] != "pending":
            break
        await asyncio.sleep(0.1)
    assert p["status"] == "ready", p
    assert "+TorchInductor converts the graph into PREVIEWED code." in p["unified"]
    assert p["identical"] is False and p["stale"] is False
    # worktree untouched before Apply
    assert (workspace / "docs" / "guide.md").read_bytes() == before
    assert not list((Path(api.c._transport.app.state.ctx.settings.state_dir) / "previews").glob("preview-*"))  # temp copy removed
    r = await api.post(f"/api/v1/previews/{pv['preview_id']}/apply", json={"base_sha256": doc["sha256"]})
    assert r.status_code == 200, r.text
    assert "PREVIEWED code." in (workspace / "docs" / "guide.md").read_text()
    assert r.json()["sha256"] == p["proposed_sha256"]
    versions = (await api.get(f"/api/v1/documents/{did}/versions")).json()["versions"]
    assert versions[0]["source"] == "preview_apply"
    # applying twice is refused
    r = await api.post(f"/api/v1/previews/{pv['preview_id']}/apply", json={"base_sha256": doc["sha256"]})
    assert r.status_code == 409


async def test_preview_apply_rejects_external_change(api: Api, workspace: Path):
    doc = await api.open_doc()
    did = doc["document_id"]
    r = await api.post(f"/api/v1/documents/{did}/previews", json={
        "instruction": "[[edit:docs/guide.md:::device code.:::P code.]]", "base_sha256": doc["sha256"]})
    pv = r.json()
    await api.wait_job(pv["job_id"])
    for _ in range(50):
        p = (await api.get(f"/api/v1/previews/{pv['preview_id']}")).json()
        if p["status"] != "pending":
            break
        await asyncio.sleep(0.1)
    assert p["status"] == "ready"
    # external edit in the terminal between preview and apply
    f = workspace / "docs" / "guide.md"
    external = f.read_text().replace("# Guide", "# Guide (edited externally)")
    f.write_text(external)
    r = await api.post(f"/api/v1/previews/{pv['preview_id']}/apply", json={"base_sha256": doc["sha256"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOCUMENT_VERSION_CONFLICT"
    assert f.read_text() == external  # the newer change is preserved
    p = (await api.get(f"/api/v1/previews/{pv['preview_id']}")).json()
    assert p["status"] == "stale"
    # wrong base is also refused
    r = await api.post(f"/api/v1/previews/{pv['preview_id']}/apply", json={"base_sha256": "0" * 64})
    assert r.status_code == 409
    # stale base on preview creation
    r = await api.post(f"/api/v1/documents/{did}/previews", json={"instruction": "x", "base_sha256": doc["sha256"]})
    assert r.status_code == 409


async def test_codex_turns_use_configured_sandbox_and_resume(api: Api, fake_env: Path, workspace: Path):
    r = await api.post("/api/v1/agent-sessions", json={"provider": "codex", "display_name": "cx"})
    cx = r.json()["id"]
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "hi", "session_id": cx})
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "completed" and ev[-1]["payload"]["text"] == "echo: hi"
    assert (fake_env / "codex_last_sandbox").read_text() == "workspace-write"
    calls = [json.loads(l) for l in (fake_env / "codex_calls.log").read_text().splitlines() if l.startswith('["exec"')]
    assert calls[-1][:2] == ["exec", "--json"] and "-s" in calls[-1] and "-C" in calls[-1]
    # resume passes the native id and the sandbox as a config override
    tid = (await api.threads(doc["document_id"]))["threads"][0]["id"]
    r = await api.post(f"/api/v1/threads/{tid}/messages", json={"text": "again [[edit:docs/guide.md:::Guide:::Guide2]]"})
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "completed"
    assert "Guide2" in (workspace / "docs" / "guide.md").read_text()
    calls = [json.loads(l) for l in (fake_env / "codex_calls.log").read_text().splitlines() if l.startswith('["exec"')]
    assert calls[-1][:2] == ["exec", "resume"] and 'sandbox_mode="workspace-write"' in calls[-1]
    native = (await api.get(f"/api/v1/agent-sessions/{cx}")).json()["native_id"]
    assert native in calls[-1]
    assert (fake_env / "codex_last_sandbox").read_text() == "workspace-write"


async def test_codex_read_only_denial_is_shown_not_claimed_as_edit(running_app, settings, workspace: Path):
    settings.providers["codex"].sandbox = "read-only"
    settings.default_provider = "codex"
    h = await running_app(settings)
    api = h.api
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "edit [[edit:docs/guide.md:::Guide:::X]]"})
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "failed" and "read-only" in ev[-1]["payload"]["message"]
    assert "# Guide" in (workspace / "docs" / "guide.md").read_text()
    t = (await api.threads(doc["document_id"]))["threads"][0]
    assert t["messages"][1]["status"] == "failed"
