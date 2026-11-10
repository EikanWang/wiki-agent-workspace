"""Folder chats: an open conversation about a wiki folder, distilled into a page or folded back into its sources."""

from __future__ import annotations

from backend.app.models import Job, Message


async def start_chat(api, title="Capture questions"):
    r = await api.post("/api/v1/chats", json={"title": title})
    assert r.status_code == 201, r.text
    c = r.json()
    d = (await api.get(f"/api/v1/documents/open?path={c['path']}")).json()
    return c, d


async def test_chat_is_a_hidden_read_only_document_with_its_own_prompt(running_app, workspace):
    h = await running_app()
    api = h.api
    c, d = await start_chat(api)
    assert c["path"].startswith(".waw-chat/capture-questions-") and c["path"].endswith(".md")
    assert d["editable"] is False and "# Capture questions" in d["text"] and "open conversation" in d["text"]
    assert not (workspace / ".waw-chat").exists()  # never a file in the wiki
    assert (await api.put(f"/api/v1/documents/{d['document_id']}/content", json={"text": "x", "base_sha256": d["sha256"]})).json()["error"]["code"] == "NOT_EDITABLE"
    r = await api.post(f"/api/v1/documents/{d['document_id']}/threads", json={"whole_document": True, "version_sha256": d["sha256"],
                       "question": "How does TorchDynamo capture graphs?"})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    thread = r.json()["thread_id"]
    r = await api.post(f"/api/v1/threads/{thread}/messages", json={"text": "And AOTAutograd?"})
    await api.wait_job(r.json()["job_id"])
    with h.ctx.db.read() as s:
        prompts = [j.prompt for j in s.query(Job).order_by(Job.created_at).all()]
    assert "chatting with the user about their wiki" in prompts[0] and str(workspace.resolve()) in prompts[0]
    assert "Document path" not in prompts[0] and "SELECTED_PASSAGE" not in prompts[0]
    assert "[user] How does TorchDynamo capture graphs?" in prompts[1] and "User message:\n\nAnd AOTAutograd?" in prompts[1]
    # listed with its title and message count, newest first; archived ones leave the list
    c2, _ = await start_chat(api, "Second")
    chats = (await api.get("/api/v1/chats")).json()["chats"]
    assert {x["title"] for x in chats} == {"Capture questions", "Second"}
    assert next(x for x in chats if x["path"] == c["path"])["messages"] == 4
    assert (await api.delete("/api/v1/chats", params={"path": c2["path"]})).status_code == 200
    assert [x["path"] for x in (await api.get("/api/v1/chats")).json()["chats"]] == [c["path"]]


async def test_chat_distill_into_a_page_and_update_source_pages(running_app, workspace):
    h = await running_app()
    api = h.api
    _, d = await start_chat(api)
    r = await api.post(f"/api/v1/documents/{d['document_id']}/threads", json={"whole_document": True, "version_sha256": d["sha256"],
                       "question": "What does AOTAutograd do?"})
    await api.wait_job(r.json()["job_id"])
    thread = r.json()["thread_id"]
    # distill: a new page in the folder (path checked; locks respected)
    assert (await api.post(f"/api/v1/threads/{thread}/messages", json={"text": "-", "chat_action": "distill", "target_path": "../x.md"})).status_code == 400
    assert (await api.post(f"/api/v1/threads/{thread}/messages", json={"text": "-", "chat_action": "distill", "target_path": "notes/a.txt"})).json()["error"]["code"] == "INVALID_PATH"
    assert (await api.post(f"/api/v1/threads/{thread}/messages", json={"text": "-", "chat_action": "nope"})).json()["error"]["code"] == "INVALID_ACTION"
    (workspace / "locked").mkdir()
    assert (await api.post("/api/v1/locks", json={"path": "locked"})).status_code == 201
    assert (await api.post(f"/api/v1/threads/{thread}/messages", json={"text": "-", "chat_action": "distill", "target_path": "locked/p.md"})).status_code == 423
    r = await api.post(f"/api/v1/threads/{thread}/messages", json={"text": "-", "chat_action": "distill", "target_path": "notes/aot.md"})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    assert (workspace / "notes" / "aot.md").read_text().startswith("# Distilled\n\n- What does AOTAutograd do?")
    with h.ctx.db.read() as s:
        job = s.query(Job).order_by(Job.created_at.desc()).first()
        shown = s.query(Message).filter_by(id=r.json()["message_id"]).one().content
    assert shown == "Distill this conversation into the wiki page `notes/aot.md`."
    assert "LOCKED by the user and must NOT be modified: locked" in job.prompt and "Do not modify any other file" in job.prompt
    # update the source pages (with a note for the agent)
    r = await api.post(f"/api/v1/threads/{thread}/messages", json={"text": "Keep it short. [[edit:docs/guide.md:::forms a graph:::forms an FX graph]]",
                       "chat_action": "update"})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    assert "forms an FX graph" in (workspace / "docs" / "guide.md").read_text()
    with h.ctx.db.read() as s:
        job = s.query(Job).order_by(Job.created_at.desc()).first()
    assert "Update those source pages in place" in job.prompt and "Keep it short." in job.prompt
    # actions are only for chats
    doc = (await api.get("/api/v1/documents/open?path=docs/guide.md")).json()
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={"whole_document": True, "version_sha256": doc["sha256"], "question": "q"})
    await api.wait_job(r.json()["job_id"])
    assert (await api.post(f"/api/v1/threads/{r.json()['thread_id']}/messages", json={"text": "-", "chat_action": "update"})).json()["error"]["code"] == "INVALID_ACTION"
