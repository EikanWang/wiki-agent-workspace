"""Documents, file tree, path safety, reading state, review marks, diff (A1, B8, D13)."""

from __future__ import annotations

import os
import asyncio
from pathlib import Path

import pytest

from conftest import Api, anchor_for


async def test_tree_lists_untracked_markdown_with_git_status(api: Api, workspace: Path):
    r = await api.get("/api/v1/files", params={"path": ""})
    assert r.status_code == 200
    body = r.json()
    names = {i["name"]: i for i in body["items"]}
    assert "docs" in names and names["docs"]["type"] == "dir"
    assert names["README.md"]["git_status"] == "clean"
    assert ".git" not in names
    assert body["git"]["is_repo"] is True and body["git"]["branch"]
    r = await api.get("/api/v1/files", params={"path": "docs"})
    items = {i["name"]: i for i in r.json()["items"]}
    assert items["guide.md"]["git_status"] == "untracked"
    # modified status
    (workspace / "README.md").write_text("# Repo\n\nchanged\n")
    r = await api.get("/api/v1/files", params={"path": ""})
    assert {i["name"]: i for i in r.json()["items"]}["README.md"]["git_status"] == "modified"


async def test_open_untracked_document_creates_record_and_version(api: Api):
    doc = await api.open_doc()
    assert doc["path"] == "docs/guide.md"
    assert len(doc["sha256"]) == 64
    assert doc["git"]["file_status"] == "untracked"
    assert doc["default_session_id"] is None
    again = await api.open_doc()
    assert again["document_id"] == doc["document_id"]
    r = await api.get(f"/api/v1/documents/{doc['document_id']}/versions")
    assert [v["sha256"] for v in r.json()["versions"]] == [doc["sha256"]]


async def test_path_traversal_and_symlink_escape_rejected(api: Api, workspace: Path, tmp_path: Path):
    r = await api.get("/api/v1/documents/open", params={"path": "../outside.md"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_PATH"
    r = await api.get("/api/v1/files", params={"path": "/etc"})
    assert r.status_code == 400
    outside = tmp_path / "secret.md"
    outside.write_text("# secret\n")
    os.symlink(outside, workspace / "docs" / "link.md")
    r = await api.get("/api/v1/documents/open", params={"path": "docs/link.md"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "PATH_ESCAPES_WORKSPACE"
    os.symlink(tmp_path, workspace / "escape")
    r = await api.get("/api/v1/files", params={"path": "escape"})
    assert r.status_code == 403
    r = await api.get("/api/v1/documents/open", params={"path": "docs/guide.md\0x"})
    assert r.status_code in (400, 404)


async def test_invalid_utf8_and_oversize_documents(api: Api, workspace: Path, running_app, settings):
    (workspace / "docs" / "bad.md").write_bytes(b"# bad\n\xff\xfe\n")
    r = await api.get("/api/v1/documents/open", params={"path": "docs/bad.md"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_UTF8"
    (workspace / "docs" / "big.md").write_text("x" * 5000)
    settings.max_document_bytes = 1000
    h = await running_app(settings)
    r = await h.api.get("/api/v1/documents/open", params={"path": "docs/big.md"})
    assert r.status_code == 413
    r = await api.get("/api/v1/documents/open", params={"path": "README.txt"})
    assert r.status_code == 404


async def test_reading_state_persists_across_restart(running_app, settings):
    h = await running_app()
    doc = await h.api.open_doc()
    r = await h.api.put(f"/api/v1/documents/{doc['document_id']}/reading-state",
                        json={"version_sha256": doc["sha256"], "heading": "Capture", "offset": 120, "scroll_fraction": 0.42})
    assert r.status_code == 200 and r.json()["offset"] == 120
    h2 = await running_app(settings)  # web server restart with the same state dir
    doc2 = await h2.api.open_doc()
    assert doc2["document_id"] == doc["document_id"]
    assert doc2["reading_state"]["scroll_fraction"] == pytest.approx(0.42)
    assert doc2["reading_state"]["heading"] == "Capture"


async def test_review_marks_need_review_after_change_but_not_on_scroll(api: Api, workspace: Path):
    doc = await api.open_doc()
    anchor = anchor_for(doc["text"], "AOTAutograd handles forward and backward graphs.")
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/review-marks", json={"version_sha256": doc["sha256"], "anchor": anchor})
    assert r.status_code == 201 and r.json()["state"] == "reviewed"
    # scrolling only saves reading state; it never changes review status
    await api.put(f"/api/v1/documents/{doc['document_id']}/reading-state", json={"version_sha256": doc["sha256"], "offset": 500, "scroll_fraction": 0.9})
    r = await api.get(f"/api/v1/documents/{doc['document_id']}/review-marks")
    assert r.json()["marks"][0]["state"] == "reviewed"
    # unrelated change elsewhere keeps the mark reviewed (exact text + context intact)
    p = workspace / "docs" / "guide.md"
    p.write_text(p.read_text().replace("device code.", "device code!"))
    r = await api.get(f"/api/v1/documents/{doc['document_id']}/review-marks")
    assert r.json()["marks"][0]["state"] == "reviewed"
    # change inside the reviewed sentence -> needs review
    p.write_text(p.read_text().replace("forward and backward graphs", "forward, backward and joint graphs"))
    r = await api.get(f"/api/v1/documents/{doc['document_id']}/review-marks")
    assert r.json()["marks"][0]["state"] == "needs_review"
    assert r.json()["marks"][0]["anchor_state"] == "orphaned"


async def test_diff_between_versions_and_git_ref(api: Api, workspace: Path):
    doc = await api.open_doc("README.md")
    p = workspace / "README.md"
    p.write_text("# Repo\n\ncommitted file\nnew line\n")
    doc2 = await api.open_doc("README.md")
    assert doc2["sha256"] != doc["sha256"]
    r = await api.get(f"/api/v1/documents/{doc['document_id']}/diff", params={"from": doc["sha256"], "to": doc2["sha256"]})
    assert r.status_code == 200 and "+new line" in r.json()["unified"]
    r = await api.get(f"/api/v1/documents/{doc['document_id']}/diff", params={"from": "HEAD", "to": "current"})
    assert r.status_code == 200 and "+new line" in r.json()["unified"]
    r = await api.get(f"/api/v1/documents/{doc['document_id']}/diff", params={"from": "nonexistent-ref", "to": "current"})
    assert r.status_code == 404


async def test_missing_document_and_relink(api: Api, workspace: Path):
    doc = await api.open_doc()
    (workspace / "docs" / "guide.md").rename(workspace / "docs" / "renamed.md")
    r = await api.get(f"/api/v1/documents/{doc['document_id']}/threads")
    assert r.status_code == 200 and r.json()["missing"] is True
    r = await api.get("/api/v1/documents")
    assert next(d for d in r.json()["documents"] if d["id"] == doc["document_id"])["missing"] is True
    # same filename elsewhere is NOT auto-linked
    (workspace / "other").mkdir()
    (workspace / "other" / "guide.md").write_text("# different\n")
    r = await api.get("/api/v1/documents/open", params={"path": "other/guide.md"})
    assert r.json()["document_id"] != doc["document_id"]
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/relink", json={"path": "docs/renamed.md"})
    assert r.status_code == 200 and r.json()["path"] == "docs/renamed.md"
    r = await api.get(f"/api/v1/documents/{doc['document_id']}/threads")
    assert r.json()["missing"] is False


async def test_recent_work_sorted_by_activity_and_restores_thread(api: Api, workspace: Path):
    from conftest import anchor_for as _anchor

    (workspace / "docs" / "second.md").write_text("# Second\n\nSome text here.\n")
    assert (await api.get("/api/v1/recent")).json()["items"] == []
    a = await api.open_doc("docs/guide.md")
    b = await api.open_doc("docs/second.md")
    # merely opening is not "work"; saving a reading position is
    assert (await api.get("/api/v1/recent")).json()["items"] == []
    await api.put(f"/api/v1/documents/{a['document_id']}/reading-state", json={"version_sha256": a["sha256"], "heading": "Capture", "offset": 10, "scroll_fraction": 0.3})
    import asyncio as _a

    await _a.sleep(0.05)
    # a discussion on the second document makes it the most recent
    anc = _anchor(b["text"], "Some text")
    r = await api.post(f"/api/v1/documents/{b['document_id']}/threads", json={**anc, "version_sha256": b["sha256"], "question": "what?"})
    await api.wait_job(r.json()["job_id"])
    items = (await api.get("/api/v1/recent")).json()["items"]
    assert [i["path"] for i in items] == ["docs/second.md", "docs/guide.md"]
    assert items[0]["thread_count"] == 1 and items[0]["message_count"] == 2
    assert items[0]["last_thread_id"] == r.json()["thread_id"] and items[0]["last_thread_quote"] == "Some text"
    assert items[0]["last_message"]["role"] == "agent" and items[0]["last_message"]["content"].startswith("echo:")
    assert items[0]["session"]["provider"] == "opencode"
    assert items[1]["reading_state"]["heading"] == "Capture" and items[1]["reading_state"]["scroll_fraction"] == pytest.approx(0.3)
    # partial reading-state update: remembering the open discussion does not clobber the position
    await _a.sleep(0.05)
    r2 = await api.put(f"/api/v1/documents/{a['document_id']}/reading-state", json={"active_thread_id": "thread-x"})
    assert r2.status_code == 200 and r2.json()["active_thread_id"] == "thread-x" and r2.json()["scroll_fraction"] == pytest.approx(0.3)
    items = (await api.get("/api/v1/recent")).json()["items"]
    assert items[0]["path"] == "docs/guide.md"  # touching the reading state is activity


async def test_file_tree_lists_all_kinds_with_icons_metadata(api: Api, workspace: Path, tmp_path: Path):
    import os

    (workspace / "docs" / "pic.png").write_bytes(b"\x89PNG\r\n")
    (workspace / "docs" / "script.py").write_text("print(1)\n")
    (workspace / "docs" / "data.json").write_text("{}\n")
    (workspace / "docs" / "notes.txt").write_text("x\n")
    (workspace / "docs" / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    os.symlink("guide.md", workspace / "docs" / "alias.md")          # link inside the workspace
    os.symlink("does-not-exist.md", workspace / "docs" / "dangling.md")
    (tmp_path / "outside.md").write_text("# out\n")
    os.symlink(tmp_path / "outside.md", workspace / "docs" / "escape.md")
    items = {i["name"]: i for i in (await api.get("/api/v1/files", params={"path": "docs"})).json()["items"]}
    assert items["guide.md"]["kind"] == "markdown" and items["guide.md"]["openable"] is True
    assert items["pic.png"]["kind"] == "image" and items["pic.png"]["openable"] is False and items["pic.png"]["size"] == 6
    assert items["script.py"]["kind"] == "code" and items["script.py"]["openable"] is True and items["script.py"]["format"] == "text"
    assert items["data.json"]["kind"] == "data" and items["notes.txt"]["kind"] == "text" and items["paper.pdf"]["kind"] == "pdf" and items["paper.pdf"]["format"] == "pdf"
    assert items["alias.md"]["symlink"] is True and items["alias.md"]["link_target"] == "guide.md" and items["alias.md"]["openable"] is True
    assert items["dangling.md"]["kind"] == "broken_link" and items["dangling.md"]["openable"] is False
    assert items["escape.md"]["escapes"] is True and items["escape.md"]["openable"] is False
    # unsupported or escaping entries are refused by the API; text/source files open as code
    assert (await api.get("/api/v1/documents/open", params={"path": "docs/pic.png"})).json()["error"]["code"] == "UNSUPPORTED_FORMAT"
    assert (await api.get("/api/v1/documents/open", params={"path": "docs/escape.md"})).status_code == 403
    py = (await api.get("/api/v1/documents/open", params={"path": "docs/script.py"})).json()
    assert py["format"] == "text" and py["text"] == "print(1)\n" and 'class="language-python"' in py["html"]


async def test_recent_hide_unhide_and_delete_history(api: Api, workspace: Path):
    from conftest import anchor_for as _anchor

    doc = await api.open_doc()
    did = doc["document_id"]
    anc = _anchor(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{did}/threads", json={**anc, "version_sha256": doc["sha256"], "question": "q"})
    await api.wait_job(r.json()["job_id"])
    assert [i["path"] for i in (await api.get("/api/v1/recent")).json()["items"]] == ["docs/guide.md"]
    # hide: gone from the default list, present with include_hidden, history intact
    assert (await api.put(f"/api/v1/documents/{did}/hidden", json={"hidden": True})).status_code == 200
    assert (await api.get("/api/v1/recent")).json()["items"] == []
    hidden = (await api.get("/api/v1/recent", params={"include_hidden": "true"})).json()["items"]
    assert hidden[0]["hidden"] is True and hidden[0]["thread_count"] == 1
    assert len((await api.threads(did))["threads"]) == 1
    await api.put(f"/api/v1/documents/{did}/hidden", json={"hidden": False})
    assert (await api.get("/api/v1/recent")).json()["items"][0]["hidden"] is False
    # delete history: everything app-side goes, the file stays, the document can be used again
    before = (workspace / "docs" / "guide.md").read_bytes()
    snaps = list((api.c._transport.app.state.ctx.settings.snapshots_dir / did).glob("*.md"))
    assert snaps
    r = await api.delete(f"/api/v1/documents/{did}/history")
    assert r.status_code == 200 and r.json()["deleted"]["threads"] == 1 and r.json()["deleted"]["messages"] == 2
    assert (workspace / "docs" / "guide.md").read_bytes() == before
    assert (await api.get("/api/v1/recent", params={"include_hidden": "true"})).json()["items"] == []
    assert (await api.threads(did))["threads"] == []
    assert not list((api.c._transport.app.state.ctx.settings.snapshots_dir / did).glob("*.md"))
    doc2 = await api.open_doc()
    assert doc2["document_id"] == did and doc2["default_session_id"] is None
    # a running job blocks deletion
    r = await api.post(f"/api/v1/documents/{did}/threads", json={**anc, "version_sha256": doc2["sha256"], "question": "[[sleep:5]] slow"})
    await asyncio.sleep(0.3)
    assert (await api.delete(f"/api/v1/documents/{did}/history")).json()["error"]["code"] == "DOCUMENT_BUSY"
    await api.post(f"/api/v1/jobs/{r.json()['job_id']}/cancel")
    await api.wait_job(r.json()["job_id"])
