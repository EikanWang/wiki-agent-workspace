"""Local files: a file manager on server paths."""

from __future__ import annotations

from pathlib import Path


async def test_local_files_browse_edit(api, tmp_path: Path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "a.txt").write_text("hello\n")
    (d / "sub").mkdir()
    (d / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    r = await api.put("/api/v1/local/roots", json={"roots": [str(d), str(d)]})
    assert [x["path"] for x in r.json()["roots"]] == [str(d)]
    assert (await api.put("/api/v1/local/roots", json={"roots": ["rel/x"]})).status_code == 400
    items = (await api.get("/api/v1/local/list", params={"path": str(d)})).json()["items"]
    assert [i["name"] for i in items] == ["sub", "a.txt", "img.png"]
    f = (await api.get("/api/v1/local/file", params={"path": str(d / "a.txt")})).json()
    assert f["text"] == "hello\n" and not f["binary"]
    w = await api.put("/api/v1/local/file", json={"path": str(d / "a.txt"), "text": "bye\n", "base_sha256": f["sha256"]})
    assert w.status_code == 200 and (d / "a.txt").read_text() == "bye\n"
    stale = await api.put("/api/v1/local/file", json={"path": str(d / "a.txt"), "text": "x", "base_sha256": f["sha256"]})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "FILE_CHANGED"
    assert (await api.put("/api/v1/local/file", json={"path": str(d / "sub/n.py"), "text": "x=1\n", "create": True})).status_code == 200
    assert (await api.post("/api/v1/local/rename", json={"path": str(d / "sub/n.py"), "new_path": str(d / "m.py")})).status_code == 200
    assert (d / "m.py").read_text() == "x=1\n"
    assert (await api.post("/api/v1/local/mkdir", json={"path": str(d / "new")})).status_code == 200
    assert (await api.post("/api/v1/local/delete", json={"path": str(d / "new")})).status_code == 200 and not (d / "new").exists()
    st = (await api.post("/api/v1/local/stat", json={"paths": [str(d / "m.py"), str(d / "gone")]})).json()["sha256"]
    assert st[str(d / "gone")] is None and len(st[str(d / "m.py")]) == 64
    assert (await api.get("/api/v1/local/file", params={"path": str(d / "img.png")})).json()["binary"]
    raw = await api.get("/api/v1/local/raw", params={"path": str(d / "img.png")})
    assert raw.headers["content-type"] == "image/png"
    assert (await api.get("/api/v1/local/raw", params={"path": str(d / "m.py")})).headers["content-disposition"].startswith("attachment")


async def test_local_chat_works_in_the_local_folder(running_app, tmp_path: Path):
    import json

    from backend.app.models import Job

    h = await running_app()
    api = h.api
    d = tmp_path / "proj"
    (d / "src").mkdir(parents=True)
    (d / "src" / "main.py").write_text("print('hi')\n")
    assert (await api.put("/api/v1/local/roots", json={"roots": [str(d)]})).status_code == 200
    assert (await api.post("/api/v1/local/chats", json={"path": str(d / "missing")})).status_code == 404
    assert (await api.post("/api/v1/local/chats", json={"path": "rel/x"})).status_code == 400
    c = await api.post("/api/v1/local/chats", json={"path": str(d / "src" / "main.py"), "title": "About main"})
    assert c.status_code == 201, c.text
    c = c.json()
    assert c["path"].startswith(".waw-chat/") and c["about"] == {"path": str(d / "src" / "main.py"), "kind": "file", "cwd": str(d)}
    doc = (await api.get(f"/api/v1/documents/open?path={c['path']}")).json()
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={"whole_document": True, "version_sha256": doc["sha256"],
                       "question": "What does it print?"})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    tid = r.json()["thread_id"]
    r2 = await api.post(f"/api/v1/threads/{tid}/messages", json={"text": "And why?"})
    await api.wait_job(r2.json()["job_id"])
    with h.ctx.db.read() as s:
        jobs = s.query(Job).filter(Job.id.in_([r.json()["job_id"], r2.json()["job_id"]])).order_by(Job.created_at).all()
        prompts, ctxs = [j.prompt for j in jobs], [json.loads(j.context_json) for j in jobs]
    assert all(x["_cwd"] == str(d) for x in ctxs)  # the agent works in the opened Local files folder
    assert "the file `src/main.py`" in prompts[0] and "about their wiki" not in prompts[0]
    assert "[user] What does it print?" in prompts[1] and "And why?" in prompts[1]
    # a folder outside any opened one: the agent works in it
    other = tmp_path / "elsewhere"
    other.mkdir()
    c2 = (await api.post("/api/v1/local/chats", json={"path": str(other)})).json()
    assert c2["about"] == {"path": str(other), "kind": "folder", "cwd": str(other)}
    # listed per path in Local files, not in Chats; no distill / update into the wiki
    mine = (await api.get("/api/v1/local/chats", params={"path": str(d / "src" / "main.py")})).json()["chats"]
    assert [(x["title"], x["messages"]) for x in mine] == [("About main", 4)]
    assert len((await api.get("/api/v1/local/chats")).json()["chats"]) == 2
    assert c["path"] not in [x["path"] for x in (await api.get("/api/v1/chats")).json()["chats"]]
    assert (await api.post(f"/api/v1/threads/{tid}/messages", json={"text": "-", "chat_action": "update"})).status_code == 400
    assert (await api.delete(f"/api/v1/chats?path={c['path']}")).status_code == 200  # archived
    assert (await api.get("/api/v1/local/chats", params={"path": str(d / "src" / "main.py")})).json()["chats"] == []
