"""Folder chats: an open conversation about a wiki folder, distilled into a page or folded back into its sources."""

from __future__ import annotations

from backend.app.models import Job, Message


async def start_chat(api, title="Capture questions"):
    r = await api.post("/api/v1/chats", json={"title": title})
    assert r.status_code == 201, r.text
    c = r.json()
    d = (await api.get(f"/api/v1/documents/open?path={c['path']}")).json()
    return c, d


async def test_chat_scopes_are_explicit_and_wiki_by_default(running_app):
    h = await running_app()
    wiki, _ = await start_chat(h.api)
    assert wiki["scope"] == "wiki"
    response = await h.api.post("/api/v1/chats", json={"title": "Control Center", "scope": "control_center"})
    assert response.status_code == 201
    control = response.json()
    assert control["scope"] == "control_center"
    chats = (await h.api.get("/api/v1/chats")).json()["chats"]
    assert {chat["path"]: chat["scope"] for chat in chats} == {wiki["path"]: "wiki", control["path"]: "control_center"}
    assert (await h.api.post("/api/v1/chats", json={"scope": "all"})).status_code == 400
    assert (await h.api.post("/api/v1/chats", json={"scope": "control_center", "about": "docs"})).status_code == 400
    next_wiki, _ = await start_chat(h.api, "Another wiki chat")
    assert next_wiki["scope"] == "wiki"


async def test_control_context_is_opt_in_read_only_and_refreshed_per_turn(running_app, monkeypatch):
    from backend.app import control_center

    observations = []

    def observe(ctx):
        observations.append(ctx.identity)
        return {"status": "observed", "marker": f"APP_ONLY_{len(observations)}"}

    monkeypatch.setattr(control_center, "agents", observe)
    h = await running_app()
    created = (await h.api.post("/api/v1/chats", json={"title": "Control", "scope": "control_center", "sources": ["agents"]})).json()
    doc = (await h.api.get("/api/v1/documents/open", params={"path": created["path"]})).json()
    first = await h.api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={"whole_document": True,
                            "version_sha256": doc["sha256"], "question": "What needs attention?"})
    assert first.status_code == 201, first.text
    await h.api.wait_job(first.json()["job_id"])
    thread = first.json()["thread_id"]
    reply = await h.api.post(f"/api/v1/threads/{thread}/messages", json={"text": "What next?"})
    await h.api.wait_job(reply.json()["job_id"])
    with h.ctx.db.read() as session:
        prompts = [job.prompt for job in session.query(Job).order_by(Job.created_at).all()]
    assert "APP_ONLY_1" in prompts[0] and "APP_ONLY_2" in prompts[1]
    assert all("opt-in Control Center" in prompt and "Do not use tools" in prompt for prompt in prompts)
    assert len(observations) == 2
    rejected = await h.api.post(f"/api/v1/threads/{thread}/messages", json={"text": "-", "chat_action": "update"})
    assert rejected.status_code == 400 and rejected.json()["error"]["code"] == "INVALID_ACTION"
    wiki, wiki_doc = await start_chat(h.api, "Wiki unchanged")
    response = await h.api.post(f"/api/v1/documents/{wiki_doc['document_id']}/threads", json={"whole_document": True,
                               "version_sha256": wiki_doc["sha256"], "question": "Only wiki"})
    await h.api.wait_job(response.json()["job_id"])
    with h.ctx.db.read() as session:
        prompt = session.get(Job, response.json()["job_id"]).prompt
    assert "APP_ONLY_" not in prompt and "APPLICATION_EVIDENCE" not in prompt
    assert len(observations) == 2
    mixed = await h.api.post(f"/api/v1/documents/{wiki_doc['document_id']}/threads", json={"whole_document": True,
                            "version_sha256": wiki_doc["sha256"], "question": "Do not reuse Control history",
                            "session_id": first.json()["session_id"]})
    assert mixed.status_code == 400 and mixed.json()["error"]["code"] == "INVALID_CHAT_SESSION"
    mixed = await h.api.post(f"/api/v1/threads/{thread}/messages", json={"text": "Do not reuse Wiki history",
                            "session_id": response.json()["session_id"]})
    assert mixed.status_code == 400 and mixed.json()["error"]["code"] == "INVALID_CHAT_SESSION"
    assert (await h.api.get("/api/v1/chats/context", params={"path": wiki["path"]})).status_code == 400
    assert (await h.api.post("/api/v1/chats", json={"sources": ["agents"]})).status_code == 400
    side = await h.api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={"version_sha256": doc["sha256"],
                           "question": "Explain that status", "quote": "attention", "side_of": thread})
    assert side.status_code == 201, side.text
    await h.api.wait_job(side.json()["job_id"])
    with h.ctx.db.read() as session:
        prompt = session.get(Job, side.json()["job_id"]).prompt
    assert "APP_ONLY_3" in prompt and "opt-in Control Center" in prompt and "read-only" in prompt


async def test_control_center_history_and_jobs_are_private_to_the_creating_login(running_app):
    from backend.app.models import LoginSession

    h = await running_app()
    created = (await h.api.post("/api/v1/chats", json={"scope": "control_center", "sources": []})).json()
    doc = (await h.api.get("/api/v1/documents/open", params={"path": created["path"]})).json()
    response = await h.api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={"whole_document": True,
                               "version_sha256": doc["sha256"], "question": "Private context"})
    assert response.status_code == 201, response.text
    await h.api.wait_job(response.json()["job_id"])
    with h.ctx.db.write() as session:
        for login in session.query(LoginSession).all():
            login.identity = "github:another-user"
    assert (await h.api.get("/api/v1/chats")).json()["chats"] == []
    for endpoint in (f"/api/v1/documents/{doc['document_id']}/threads", f"/api/v1/jobs/{response.json()['job_id']}",
                     f"/api/v1/jobs/{response.json()['job_id']}/events"):
        assert (await h.api.get(endpoint)).status_code == 403
    assert (await h.api.get("/api/v1/documents/open", params={"path": created["path"]})).status_code == 403
    assert (await h.api.get("/api/v1/documents/open", params={"path": "./" + created["path"]})).status_code == 403
    for query in ("", "?all=true"):
        recent = (await h.api.get("/api/v1/recent" + query)).json()["items"]
        assert all(item["path"] != created["path"] for item in recent)


async def test_control_reviews_refresh_full_queue_and_respect_owner_and_hidden_repositories(running_app, monkeypatch):
    import time

    from backend.app import github_hub
    from backend.app.kv import kv_put
    from backend.app.models import LoginSession

    h = await running_app()
    h.ctx.extra["gh_owner"] = lambda: "control-owner"
    row = github_hub.normalize({"number": 17, "title": "PRIVATE_REVIEW_EVIDENCE", "state": "OPEN",
                               "reviewDecision": "APPROVED", "author": {"login": "alice"},
                               "url": "https://github.com/private/repo/pull/17",
                               "repository": {"nameWithOwner": "private/repo", "isPrivate": True}}, "control-owner")
    hidden = {**row, "repo": "hidden/repo", "title": "HIDDEN_REVIEW_EVIDENCE"}
    monkeypatch.setitem(github_hub._cache, "control-owner", (time.time() - 900, {"items": [row, hidden], "truncated": False}))
    refreshed = []
    truncated = False
    kernel_repo = "example-org/kernel-xpu"

    def fetch_queue(graphql, owner):
        refreshed.append(owner)
        return {"items": [{**row, "number": number, "url": f"https://github.com/private/repo/pull/{number}",
                   "title": "CURRENT_REVIEW_EVIDENCE"} for number in range(1, 153)] +
                 [{**row, "repo": kernel_repo, "number": 358, "url": f"https://github.com/{kernel_repo}/pull/358"}, hidden],
            "total": 154, "truncated": truncated}

    watched = [{**row, "repo": "example-org/inference", "number": number,
                "url": f"https://github.com/example-org/inference/pull/{number}",
                "author": "bob", "title": f"CURRENT_INFERENCE_{number}"} for number in (324, 323, 314)]
    monkeypatch.setattr(github_hub, "fetch_queue", fetch_queue)
    monkeypatch.setattr(github_hub, "_owned_found", lambda *args: {})
    monkeypatch.setattr(github_hub, "fetch_watched", lambda *args: (watched, []))
    kv_put(h.ctx, "github_hub_prefs", {"hidden_repos": ["hidden/repo"], "watched_repos": [watched[0]["repo"]]})
    token_chat = (await h.api.post("/api/v1/chats", json={"scope": "control_center", "sources": ["reviews"]})).json()
    token_context = (await h.api.get("/api/v1/chats/context", params={"path": token_chat["path"]})).json()
    assert token_context["sources"][0]["status"] == "forbidden"
    assert "PRIVATE_REVIEW_EVIDENCE" not in str(token_context)
    assert refreshed == []
    with h.ctx.db.write() as session:
        for login in session.query(LoginSession).all():
            login.identity = "github:control-owner"
    owner_chat = (await h.api.post("/api/v1/chats", json={"scope": "control_center", "sources": ["reviews"]})).json()
    owner_context = (await h.api.get("/api/v1/chats/context", params={"path": owner_chat["path"]})).json()
    evidence = owner_context["sources"][0]
    assert refreshed == ["control-owner"]
    assert evidence["status"] == "live" and evidence["age_seconds"] < 10
    assert evidence["total"] == len(evidence["items"]) == 156 and not evidence["truncated"]
    assert evidence["items"][0]["author"] == "alice"
    assert evidence["items"][0]["review_decision"] == "APPROVED"
    assert evidence["items"][-4]["repo"] == kernel_repo and evidence["items"][-4]["number"] == 358
    assert {(item["number"], item["author"], item["queue_group"]) for item in evidence["items"][-3:]} == {
        (number, "bob", "watched") for number in (324, 323, 314)}
    assert evidence["source_url"] == "/api/v1/github/reviews"
    assert evidence["filters"]["hidden_repos"] == ["hidden/repo"]
    assert "HIDDEN_REVIEW_EVIDENCE" not in str(owner_context)

    github_hub._cache.pop("control-owner")
    truncated = True
    context = (await h.api.get("/api/v1/chats/context", params={"path": owner_chat["path"]})).json()
    assert refreshed == ["control-owner", "control-owner"]
    assert context["sources"][0]["status"] == "live" and context["sources"][0]["truncated"]
    assert len(context["sources"][0]["items"]) == 156

    def fail_refresh(graphql, owner):
        raise RuntimeError("PRIVATE_REFRESH_FAILURE")

    monkeypatch.setattr(github_hub, "fetch_queue", fail_refresh)
    context = (await h.api.get("/api/v1/chats/context", params={"path": owner_chat["path"]})).json()
    assert context["sources"][0]["status"] == "unavailable"
    assert "CURRENT_REVIEW_EVIDENCE" not in str(context) and "PRIVATE_REFRESH_FAILURE" not in str(context)


async def test_control_insights_are_bounded_and_source_failures_are_isolated(running_app, monkeypatch, tmp_path):
    from backend.app import control_center
    from backend.app.insights import pipeline, settings, store

    h = await running_app()
    settings.save(h.ctx, {"data_dir": str(tmp_path / "insights"), "repos": [{"repo": "example/project"}]})
    store.write_summary(h.ctx, "example/project", "2026-11-18", "2026-11-18", "SUMMARY_EVIDENCE " + "x" * 4000)
    monkeypatch.setattr(pipeline, "reports", lambda ctx: [{"id": "report-one", "repo": "example/project", "status": "done",
                                                         "api_key": "SHOULD_NOT_APPEAR", "text": "z" * 10000}])
    created = (await h.api.post("/api/v1/chats", json={"scope": "control_center", "sources": ["insights", "agents"]})).json()
    context = (await h.api.get("/api/v1/chats/context", params={"path": created["path"]})).json()
    summaries = context["sources"][0]["summaries"]
    assert len(summaries) == 1 and "SUMMARY_EVIDENCE" in summaries[0]["excerpt"]
    assert len(summaries[0]["excerpt"]) == 1800 and summaries[0]["excerpt_truncated"]
    assert "SHOULD_NOT_APPEAR" not in str(context) and "api_key" not in str(context)
    assert context["sources"][1]["status"] == "observed"

    def broken(ctx):
        raise RuntimeError("PRIVATE_EXCEPTION_DETAIL")

    monkeypatch.setattr(control_center, "insights", broken)
    context = (await h.api.get("/api/v1/chats/context", params={"path": created["path"]})).json()
    assert [source["status"] for source in context["sources"]] == ["unavailable", "observed"]
    assert "PRIVATE_EXCEPTION_DETAIL" not in str(context)


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


async def test_side_questions_about_a_part_of_an_answer_have_their_own_session_and_go_into_a_distill(running_app, workspace):
    h = await running_app()
    api = h.api
    _, d = await start_chat(api)
    r = await api.post(f"/api/v1/documents/{d['document_id']}/threads", json={"whole_document": True, "version_sha256": d["sha256"],
                       "question": "What does AOTAutograd do?"})
    await api.wait_job(r.json()["job_id"])
    main, main_session = r.json()["thread_id"], r.json()["session_id"]
    side_body = {"version_sha256": d["sha256"], "question": "What is a joint graph?", "quote": "traces forward and backward", "side_of": main}
    # a part of an answer is needed, and the thread must be this chat's
    assert (await api.post(f"/api/v1/documents/{d['document_id']}/threads", json={**side_body, "quote": " "})).json()["error"]["code"] == "QUOTE_REQUIRED"
    assert (await api.post(f"/api/v1/documents/{d['document_id']}/threads", json={**side_body, "side_of": "nope"})).status_code == 404
    r = await api.post(f"/api/v1/documents/{d['document_id']}/threads", json=side_body)
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    side = r.json()["thread_id"]
    assert r.json()["session_id"] != main_session  # the chat's session stays free and its context clean
    r = await api.post(f"/api/v1/threads/{side}/messages", json={"text": "And why both at once?"})
    await api.wait_job(r.json()["job_id"])
    with h.ctx.db.read() as s:
        prompts = [j.prompt for j in s.query(Job).order_by(Job.created_at).all()]
    assert "side question" in prompts[1] and "[user] What does AOTAutograd do?" in prompts[1] and "> traces forward and backward" in prompts[1]
    assert "User's side question:\n\nWhat is a joint graph?" in prompts[1]
    assert "> traces forward and backward" in prompts[2] and "[user] What is a joint graph?" in prompts[2]
    threads = (await api.get(f"/api/v1/documents/{d['document_id']}/threads")).json()["threads"]
    assert [t["id"] for t in threads] == [main, side]
    assert threads[1]["target"]["kind"] == "side" and threads[1]["target"]["of"] == main and threads[1]["anchor"]["quote"] == "traces forward and backward"
    assert threads[1]["anchor_state"] == "document"
    # no actions on a side question; a distill of the chat includes them
    assert (await api.post(f"/api/v1/threads/{side}/messages", json={"text": "-", "chat_action": "update"})).json()["error"]["code"] == "INVALID_ACTION"
    r = await api.post(f"/api/v1/threads/{main}/messages", json={"text": "-", "chat_action": "distill", "target_path": "notes/aot.md"})
    await api.wait_job(r.json()["job_id"])
    with h.ctx.db.read() as s:
        prompt = s.query(Job).order_by(Job.created_at.desc()).first().prompt
    assert "Side questions the user asked" in prompt and "> traces forward and backward" in prompt
    assert "[user] What is a joint graph?" in prompt and "[user] And why both at once?" in prompt
    # side questions are only for chats
    doc = (await api.get("/api/v1/documents/open?path=docs/guide.md")).json()
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**side_body, "version_sha256": doc["sha256"]})
    assert r.json()["error"]["code"] == "INVALID_SIDE_QUESTION"
