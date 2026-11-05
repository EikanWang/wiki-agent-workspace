"""Reader discussions, sessions, jobs (B4, B5, B6, A1, A2, D13 idempotency)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from conftest import Api, anchor_for


def _types(events):
    return [e["type"] for e in events]


async def test_create_thread_streams_answer_and_persists(api: Api):
    doc = await api.open_doc()
    anchor = anchor_for(doc["text"], "captures **PyTorch** operations".replace("**", ""))
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads",
                       json={**anchor, "version_sha256": doc["sha256"], "question": "Why capture?"})
    assert r.status_code == 201, r.text
    out = r.json()
    events = await api.wait_job(out["job_id"])
    assert _types(events)[-1] == "completed"
    assert "started" in _types(events)
    t = (await api.threads(doc["document_id"]))["threads"][0]
    assert t["anchor_state"] == "attached"
    assert t["anchor"]["quote"] == "captures PyTorch operations"
    roles = [(m["role"], m["status"]) for m in t["messages"]]
    assert roles == [("user", "completed"), ("agent", "completed")]
    assert t["messages"][1]["content"] == "echo: Why capture?"
    # the document default session was created lazily and bound to the provider-native id
    doc2 = await api.open_doc()
    assert doc2["default_session_id"] == out["session_id"]
    r = await api.get(f"/api/v1/agent-sessions/{out['session_id']}")
    assert r.json()["native_id"].startswith("ses_fake") and r.json()["status"] == "ready"
    # SSE replay from a sequence number does not duplicate
    replay = await api.wait_job(out["job_id"])
    assert replay[0]["seq"] == 1 and replay[-1]["type"] == "completed"
    async with api.c.stream("GET", f"/api/v1/jobs/{out['job_id']}/events", params={"after": replay[-1]["seq"] - 1}) as resp:
        tail = [json.loads(l[5:]) async for l in resp.aiter_lines() if l.startswith("data:")]
    assert [e["seq"] for e in tail] == [replay[-1]["seq"]]


async def test_cross_line_selection_anchor(api: Api):
    doc = await api.open_doc()
    quote = "forms a graph.\nAOTAutograd handles"
    anchor = anchor_for(doc["text"], quote)
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**anchor, "version_sha256": doc["sha256"], "question": "q"})
    assert r.status_code == 201, r.text
    t = (await api.threads(doc["document_id"]))["threads"][0]
    assert t["anchor"]["quote"] == quote and t["anchor_state"] == "attached"


async def test_two_threads_share_default_session_and_explicit_new_session(api: Api, fake_env: Path):
    doc = await api.open_doc()
    did = doc["document_id"]
    a1 = anchor_for(doc["text"], "TorchDynamo")
    a2 = anchor_for(doc["text"], "TorchInductor")
    r1 = await api.post(f"/api/v1/documents/{did}/threads", json={**a1, "version_sha256": doc["sha256"], "question": "one"})
    r2 = await api.post(f"/api/v1/documents/{did}/threads", json={**a2, "version_sha256": doc["sha256"], "question": "two"})
    assert r1.json()["session_id"] == r2.json()["session_id"]
    await api.wait_job(r1.json()["job_id"])
    await api.wait_job(r2.json()["job_id"])
    sessions = json.loads((fake_env / "opencode_sessions.json").read_text())
    assert len(sessions) == 1  # both turns resumed the same native session
    calls = [json.loads(l) for l in (fake_env / "opencode_calls.log").read_text().splitlines()]
    resumed = [c for c in calls if "--session" in c]
    assert len(resumed) == 1 and resumed[0][resumed[0].index("--session") + 1] == sessions[0]["id"]

    # header "new session": becomes the default for future threads only
    r = await api.post("/api/v1/agent-sessions", json={"provider": "opencode", "display_name": "second"})
    assert r.status_code == 201
    new_sid = r.json()["id"]
    assert r.json()["status"] == "uninitialized" and r.json()["native_id"] is None
    r = await api.put(f"/api/v1/documents/{did}/default-session", json={"session_id": new_sid})
    assert r.status_code == 200
    a3 = anchor_for(doc["text"], "AOTAutograd")
    r3 = await api.post(f"/api/v1/documents/{did}/threads", json={**a3, "version_sha256": doc["sha256"], "question": "three"})
    assert r3.json()["session_id"] == new_sid
    await api.wait_job(r3.json()["job_id"])
    # the old threads keep their historical session; replies default to the thread's last session
    tid1 = r1.json()["thread_id"]
    r = await api.post(f"/api/v1/threads/{tid1}/messages", json={"text": "follow-up"})
    assert r.json()["session_id"] == r1.json()["session_id"]
    await api.wait_job(r.json()["job_id"])
    # explicit switch for one reply, then later replies default to that one
    r = await api.post(f"/api/v1/threads/{tid1}/messages", json={"text": "switch", "session_id": new_sid})
    assert r.json()["session_id"] == new_sid
    await api.wait_job(r.json()["job_id"])
    r = await api.post(f"/api/v1/threads/{tid1}/messages", json={"text": "after switch"})
    assert r.json()["session_id"] == new_sid
    await api.wait_job(r.json()["job_id"])
    t1 = [t for t in (await api.threads(did))["threads"] if t["id"] == tid1][0]
    sess_per_msg = [m["session_id"] for m in t1["messages"]]
    assert sess_per_msg[:4] == [r1.json()["session_id"]] * 4
    assert sess_per_msg[4:] == [new_sid] * 4
    assert t1["last_session_id"] == new_sid


async def test_session_ids_never_cross_providers(api: Api, fake_env: Path):
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "x"})
    await api.wait_job(r.json()["job_id"])
    oc_native = json.loads((fake_env / "opencode_sessions.json").read_text())[0]["id"]
    # linking an opencode id as a codex session is refused because codex does not know it
    r = await api.post("/api/v1/agent-sessions", json={"provider": "codex", "native_id": oc_native})
    assert r.status_code == 404 and r.json()["error"]["code"] == "SESSION_NOT_FOUND"
    # a codex session used for a reply calls the codex binary, never opencode with that id
    r = await api.post("/api/v1/agent-sessions", json={"provider": "codex", "display_name": "cx"})
    cx = r.json()["id"]
    tid = (await api.threads(doc["document_id"]))["threads"][0]["id"]
    r = await api.post(f"/api/v1/threads/{tid}/messages", json={"text": "hello codex", "session_id": cx})
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "completed"
    r = await api.get(f"/api/v1/agent-sessions/{cx}")
    cx_native = r.json()["native_id"]
    assert cx_native and not cx_native.startswith("ses_")
    oc_calls = [json.loads(l) for l in (fake_env / "opencode_calls.log").read_text().splitlines()]
    assert not any(cx_native in c for c in oc_calls)
    cx_calls = [json.loads(l) for l in (fake_env / "codex_calls.log").read_text().splitlines()]
    assert not any(oc_native in c for c in cx_calls)


async def test_version_conflict_and_invalid_anchor(api: Api, workspace: Path):
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    (workspace / "docs" / "guide.md").write_text(doc["text"] + "\nappended\n")
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "q"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOCUMENT_VERSION_CONFLICT"
    assert r.json()["error"]["details"]["current_sha256"] != doc["sha256"]
    doc = await api.open_doc()
    bad = {**a, "quote": "not in document at all"}
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**bad, "version_sha256": doc["sha256"], "question": "q"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_ANCHOR"
    assert (await api.threads(doc["document_id"]))["threads"] == []


async def test_failure_keeps_question_and_explicit_retry(api: Api):
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "[[fail:model exploded]] why"})
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "failed" and ev[-1]["payload"]["message"] == "model exploded"
    t = (await api.threads(doc["document_id"]))["threads"][0]
    assert t["messages"][0]["content"].endswith("why")  # question retained
    assert t["messages"][1]["status"] == "failed" and t["messages"][1]["error_message"] == "model exploded"
    # explicit retry creates a new job on the same thread
    r = await api.post(f"/api/v1/threads/{t['id']}/retry", json={"message_id": t["messages"][1]["id"]})
    assert r.status_code == 201
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "failed"  # the directive still fails: deterministic fake
    t = (await api.threads(doc["document_id"]))["threads"][0]
    assert len(t["messages"]) == 4


async def test_auth_busy_missing_session_errors_are_named(api: Api, fake_env: Path):
    doc = await api.open_doc()
    did = doc["document_id"]
    a = anchor_for(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{did}/threads", json={**a, "version_sha256": doc["sha256"], "question": "[[auth]] q"})
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["payload"]["code"] == "PROVIDER_AUTH_REQUIRED" and "terminal" in ev[-1]["payload"]["hint"]
    sid = r.json()["session_id"]
    r = await api.get(f"/api/v1/agent-sessions/{sid}")
    assert r.json()["native_id"] is None  # no native id was manufactured
    tid = (await api.threads(did))["threads"][0]["id"]
    # a real turn binds the id, then a busy provider is reported, not silently retried on a new session
    r = await api.post(f"/api/v1/threads/{tid}/messages", json={"text": "ok"})
    await api.wait_job(r.json()["job_id"])
    r = await api.post(f"/api/v1/threads/{tid}/messages", json={"text": "[[busy]] q"})
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["payload"]["code"] == "SESSION_BUSY"
    sessions = json.loads((fake_env / "opencode_sessions.json").read_text())
    assert len(sessions) == 1
    # provider loses the session -> SESSION_NOT_FOUND, session marked missing, no new session claimed
    (fake_env / "opencode_sessions.json").write_text("[]")
    r = await api.post(f"/api/v1/threads/{tid}/messages", json={"text": "still there?"})
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["payload"]["code"] == "SESSION_NOT_FOUND"
    r = await api.get(f"/api/v1/agent-sessions/{sid}")
    assert r.json()["status"] == "missing"
    assert json.loads((fake_env / "opencode_sessions.json").read_text()) == []


async def test_link_existing_provider_session(api: Api, fake_env: Path):
    (fake_env / "opencode_sessions.json").write_text(json.dumps([{"id": "ses_external1", "title": "from terminal", "updated": 1, "directory": "/x"}]))
    r = await api.get("/api/v1/agent-sessions", params={"provider": "opencode"})
    assert r.status_code == 200
    ps = r.json()["provider_sessions"]
    assert ps and ps[0]["native_id"] == "ses_external1" and ps[0]["linked"] is False
    r = await api.post("/api/v1/agent-sessions", json={"provider": "opencode", "native_id": "ses_external1"})
    assert r.status_code == 201 and r.json()["status"] == "availability_unknown" and r.json()["origin"] == "linked"
    r = await api.post("/api/v1/agent-sessions", json={"provider": "opencode", "native_id": "ses_nope"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "SESSION_NOT_FOUND"
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    linked = (await api.get("/api/v1/agent-sessions")).json()["sessions"][0]["id"]
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "hi", "session_id": linked})
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "completed"
    assert (await api.get(f"/api/v1/agent-sessions/{linked}")).json()["status"] == "ready"


async def test_cancel_running_job(api: Api):
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "[[sleep:20]] slow"})
    job = r.json()["job_id"]
    for _ in range(50):
        await asyncio.sleep(0.1)
        if (await api.get(f"/api/v1/jobs/{job}")).json()["status"] == "running":
            break
    r = await api.post(f"/api/v1/jobs/{job}/cancel")
    assert r.status_code == 200
    ev = await api.wait_job(job)
    assert ev[-1]["type"] == "cancelled"
    t = (await api.threads(doc["document_id"]))["threads"][0]
    assert t["messages"][1]["status"] == "cancelled"
    assert t["messages"][0]["content"].endswith("slow")


async def test_jobs_serialize_per_session_and_run_concurrently_across_sessions(api: Api):
    doc = await api.open_doc()
    did = doc["document_id"]
    a = anchor_for(doc["text"], "TorchDynamo")
    r1 = await api.post(f"/api/v1/documents/{did}/threads", json={**a, "version_sha256": doc["sha256"], "question": "[[sleep:1.5]] a"})
    t1 = r1.json()["thread_id"]
    r2 = await api.post(f"/api/v1/threads/{t1}/messages", json={"text": "b"})
    # second job on the same session must wait
    await asyncio.sleep(0.5)
    j2 = (await api.get(f"/api/v1/jobs/{r2.json()['job_id']}")).json()
    assert j2["status"] == "queued"
    ev2 = await api.wait_job(r2.json()["job_id"])
    assert "waiting" in _types(ev2) and ev2[-1]["type"] == "completed"


async def test_restart_marks_in_flight_jobs_interrupted(running_app, settings):
    h = await running_app()
    doc = await h.api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    r = await h.api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "[[sleep:30]] x"})
    await asyncio.sleep(0.5)
    # terminate the child so the "restart" does not leave the fake running
    await h.ctx.jobs.cancel(r.json()["job_id"])
    for _ in range(100):
        if (await h.api.get(f"/api/v1/jobs/{r.json()['job_id']}")).json()["status"] in ("cancelled", "failed", "completed"):
            break
        await asyncio.sleep(0.1)
    # simulate crash: mark job as running again and start a new server instance on the same DB
    from backend.app.models import Job

    with h.ctx.db.write() as s:
        j = s.get(Job, r.json()["job_id"])
        j.status = "running"
    h2 = await running_app(settings)
    j = (await h2.api.get(f"/api/v1/jobs/{r.json()['job_id']}")).json()
    assert j["status"] == "interrupted"
    t = (await h2.api.threads(doc["document_id"]))["threads"][0]
    assert t["messages"][0]["content"].endswith("x")
    assert t["messages"][1]["status"] in ("interrupted", "cancelled")


async def test_idempotency_key(api: Api):
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    body = {**a, "version_sha256": doc["sha256"], "question": "idem"}
    url = f"/api/v1/documents/{doc['document_id']}/threads"
    r1 = await api.post(url, json=body, headers={"Idempotency-Key": "k1"})
    r2 = await api.post(url, json=body, headers={"Idempotency-Key": "k1"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json() == r2.json()
    assert r2.headers.get("idempotent-replayed") == "true"
    assert len((await api.threads(doc["document_id"]))["threads"]) == 1
    r3 = await api.post(url, json={**body, "question": "different"}, headers={"Idempotency-Key": "k1"})
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    await api.wait_job(r1.json()["job_id"])


async def test_missing_provider_does_not_block_reader_or_terminal(running_app, settings, tmp_path):
    settings.providers["opencode"].bin = str(tmp_path / "does-not-exist")
    settings.providers["codex"].bin = str(tmp_path / "does-not-exist-either")
    settings.default_provider = "opencode"
    h = await running_app(settings)
    b = (await h.api.get("/api/v1/bootstrap")).json()
    assert b["providers"]["opencode"]["available"] is False and "not found" in b["providers"]["opencode"]["error"]
    doc = await h.api.open_doc()
    assert doc["text"]
    r = await h.api.get("/api/v1/terminals")
    assert r.status_code == 200
    a = anchor_for(doc["text"], "TorchDynamo")
    r = await h.api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "q"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"


async def test_anchor_resolution_against_document_changes_and_manual_reanchor(api: Api, workspace: Path):
    doc = await api.open_doc()
    did = doc["document_id"]
    a = anchor_for(doc["text"], "AOTAutograd handles forward and backward graphs.")
    r = await api.post(f"/api/v1/documents/{did}/threads", json={**a, "version_sha256": doc["sha256"], "question": "q"})
    tid = r.json()["thread_id"]
    await api.wait_job(r.json()["job_id"])
    p = workspace / "docs" / "guide.md"
    original = p.read_text()
    # insert content before -> moved
    p.write_text(original.replace("## Capture", "New paragraph inserted here.\n\n## Capture"))
    t = (await api.threads(did))["threads"][0]
    assert t["anchor_state"] == "moved" and t["current_start"] > a["start"]
    # duplicate the quote with a different context -> still unique by context
    p.write_text(original + "\nAOTAutograd handles forward and backward graphs.\n")
    t = (await api.threads(did))["threads"][0]
    assert t["anchor_state"] == "moved" and t["current_start"] == a["start"]
    # modify the quote -> orphaned
    p.write_text(original.replace("backward graphs", "reverse graphs"))
    t = (await api.threads(did))["threads"][0]
    assert t["anchor_state"] == "orphaned" and t["current_start"] is None
    assert t["anchor"]["quote"] == a["quote"]  # old quote still shown
    # manual reanchor to the new sentence
    doc2 = await api.open_doc()
    na = anchor_for(doc2["text"], "AOTAutograd handles forward and reverse graphs.")
    r = await api.put(f"/api/v1/threads/{tid}/anchor", json={**na, "version_sha256": doc2["sha256"]})
    assert r.status_code == 200
    t = (await api.threads(did))["threads"][0]
    assert t["anchor_state"] == "attached" and t["anchor"]["quote"].endswith("reverse graphs.")
    assert t["original_anchor"]["quote"] == a["quote"] and t["original_anchor"]["version_sha256"] == doc["sha256"]
    hist = (await api.get(f"/api/v1/threads/{tid}/anchor-history")).json()
    assert hist["history"][0]["kind"] == "manual"
    # stale version on reanchor -> 409
    r = await api.put(f"/api/v1/threads/{tid}/anchor", json={**na, "version_sha256": doc["sha256"]})
    assert r.status_code == 409


async def test_cancel_queued_job_takes_effect_immediately(api: Api):
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    r1 = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "[[sleep:3]] first"})
    tid = r1.json()["thread_id"]
    r2 = await api.post(f"/api/v1/threads/{tid}/messages", json={"text": "second (queued behind the first)"})
    await asyncio.sleep(0.3)
    assert (await api.get(f"/api/v1/jobs/{r2.json()['job_id']}")).json()["status"] == "queued"
    r = await api.post(f"/api/v1/jobs/{r2.json()['job_id']}/cancel")
    assert r.json()["status"] == "cancelled"
    assert (await api.get(f"/api/v1/jobs/{r2.json()['job_id']}")).json()["status"] == "cancelled"
    ev = await api.wait_job(r2.json()["job_id"])
    assert ev[-1]["type"] == "cancelled"
    ev1 = await api.wait_job(r1.json()["job_id"])
    assert ev1[-1]["type"] == "completed"  # the running job is unaffected
    t = (await api.threads(did := doc["document_id"]))["threads"][0]
    assert [m["status"] for m in t["messages"] if m["role"] == "agent"] == ["completed", "cancelled"]
