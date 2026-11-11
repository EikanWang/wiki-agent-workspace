"""Agent dashboard: status and health of chosen sessions."""

from __future__ import annotations

from conftest import anchor_for


async def test_agent_dashboard(running_app, fake_env):
    h = await running_app()
    api = h.api
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    url = f"/api/v1/documents/{doc['document_id']}/threads"
    r1 = await api.post(url, json={**a, "version_sha256": doc["sha256"], "question": "ok"})
    await api.wait_job(r1.json()["job_id"])
    r2 = await api.post(url, json={**a, "version_sha256": doc["sha256"], "question": "[[fail:boom]]", "session_id": "new"})
    await api.wait_job(r2.json()["job_id"])
    sessions = (await api.get("/api/v1/agent-dashboard/sessions")).json()["sessions"]
    ids = {s["id"] for s in sessions}
    assert {r1.json()["session_id"], r2.json()["session_id"]} <= ids
    d = (await api.get("/api/v1/agent-dashboard", params={"ids": ",".join([r1.json()["session_id"], r2.json()["session_id"], "nope"]), "deep": "true"})).json()
    by = {it["session"]["id"]: it for it in d["items"]}
    assert len(by) == 2 and d["deep"] is True
    ok, failed = by[r1.json()["session_id"]], by[r2.json()["session_id"]]
    assert ok["health"]["level"] == "ok" and ok["health"]["state"] == "idle" and ok["health"]["confirmed"] is True
    assert ok["last"]["status"] == "completed" and ok["about"]["kind"] == "document" and ok["about"]["path"] == "docs/guide.md"
    assert failed["health"]["level"] == "warning" and "failed" in failed["health"]["problems"][0]
    # a job marked running that nothing runs (e.g. after a crash): an error
    from backend.app.models import Job

    with h.ctx.db.write() as s:
        s.get(Job, r1.json()["job_id"]).status = "running"
    d = (await api.get("/api/v1/agent-dashboard", params={"ids": r1.json()["session_id"]})).json()
    it = d["items"][0]
    assert it["active"]["alive"] is False and it["health"]["level"] == "error" and it["health"]["state"] == "running"
    # a session the provider no longer knows
    from backend.app.models import AgentSession

    with h.ctx.db.write() as s:
        s.get(Job, r1.json()["job_id"]).status = "completed"
        s.get(AgentSession, r1.json()["session_id"]).native_id = "ses_gone"
    d = (await api.get("/api/v1/agent-dashboard", params={"ids": r1.json()["session_id"], "deep": "true"})).json()
    assert d["items"][0]["health"]["confirmed"] is False and d["items"][0]["health"]["level"] == "error"
