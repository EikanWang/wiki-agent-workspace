"""Direct editing of text documents in the browser."""

from __future__ import annotations

import os
from pathlib import Path

from conftest import Api, anchor_for


async def test_edit_save_conflict_lock_and_versions(api: Api, workspace: Path):
    doc = await api.open_doc()
    did = doc["document_id"]
    assert doc["editable"] is True
    new_text = doc["text"].replace("# Guide", "# Guide (edited in the browser)")
    r = await api.put(f"/api/v1/documents/{did}/content", json={"text": new_text, "base_sha256": doc["sha256"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["changed"] is True and out["previous_sha256"] == doc["sha256"]
    assert (workspace / "docs" / "guide.md").read_text() == new_text
    versions = (await api.get(f"/api/v1/documents/{did}/versions")).json()["versions"]
    assert versions[0]["sha256"] == out["sha256"] and versions[0]["source"] == "user_edit"
    diff = (await api.get(f"/api/v1/documents/{did}/diff", params={"from": doc["sha256"], "to": "current"})).json()
    assert "+# Guide (edited in the browser)" in diff["unified"]
    # saving the same content is a no-op
    r = await api.put(f"/api/v1/documents/{did}/content", json={"text": new_text, "base_sha256": out["sha256"]})
    assert r.json()["changed"] is False
    # stale base (someone changed the file meanwhile) -> 409 with the current hash; the other change is kept
    (workspace / "docs" / "guide.md").write_text(new_text + "\nterminal edit\n")
    r = await api.put(f"/api/v1/documents/{did}/content", json={"text": "mine", "base_sha256": out["sha256"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOCUMENT_VERSION_CONFLICT"
    cur = r.json()["error"]["details"]["current_sha256"]
    assert (workspace / "docs" / "guide.md").read_text().endswith("terminal edit\n")
    # explicit overwrite = save again against the current hash
    r = await api.put(f"/api/v1/documents/{did}/content", json={"text": new_text, "base_sha256": cur})
    assert r.status_code == 200 and (workspace / "docs" / "guide.md").read_text() == new_text
    # locks forbid editing
    await api.post("/api/v1/locks", json={"path": "docs"})
    d2 = await api.open_doc()
    r = await api.put(f"/api/v1/documents/{did}/content", json={"text": "x", "base_sha256": d2["sha256"]})
    assert r.status_code == 423 and r.json()["error"]["code"] == "PATH_LOCKED"
    await api.delete("/api/v1/locks", params={"path": "docs"})


async def test_edit_keeps_crlf_reanchors_and_handles_formats(api: Api, workspace: Path):
    (workspace / "docs" / "crlf.md").write_bytes(b"# CRLF\r\n\r\nline one\r\nline two\r\n")
    doc = await api.open_doc("docs/crlf.md")
    edited = doc["text"].replace("\r\n", "\n").replace("line two", "line 2")  # what a browser textarea sends
    r = await api.put(f"/api/v1/documents/{doc['document_id']}/content", json={"text": edited, "base_sha256": doc["sha256"]})
    assert r.status_code == 200
    assert (workspace / "docs" / "crlf.md").read_bytes() == b"# CRLF\r\n\r\nline one\r\nline 2\r\n"
    # discussions follow the edited text
    g = await api.open_doc()
    a = anchor_for(g["text"], "TorchInductor")
    t = await api.post(f"/api/v1/documents/{g['document_id']}/threads", json={**a, "version_sha256": g["sha256"], "question": "q"})
    await api.wait_job(t.json()["job_id"])
    r = await api.put(f"/api/v1/documents/{g['document_id']}/content", json={"text": "Intro line.\n\n" + g["text"], "base_sha256": g["sha256"]})
    assert r.status_code == 200
    th = (await api.threads(g["document_id"]))["threads"][0]
    assert th["anchor_state"] == "moved" and th["current_start"] > a["start"]
    # source files are editable, binary formats are not
    (workspace / "docs" / "tool.py").write_text("print(1)\n")
    py = await api.open_doc("docs/tool.py")
    assert py["editable"] is True
    r = await api.put(f"/api/v1/documents/{py['document_id']}/content", json={"text": "print(2)\n", "base_sha256": py["sha256"]})
    assert r.status_code == 200 and (workspace / "docs" / "tool.py").read_text() == "print(2)\n"
    (workspace / "docs" / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    pdf = await api.open_doc("docs/paper.pdf")
    assert pdf["editable"] is False
    r = await api.put(f"/api/v1/documents/{pdf['document_id']}/content", json={"text": "x", "base_sha256": pdf["sha256"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "NOT_EDITABLE"


async def test_edit_through_internal_symlink_writes_the_target_and_refuses_escapes(api: Api, workspace: Path, tmp_path: Path):
    os.symlink("guide.md", workspace / "docs" / "alias.md")
    al = await api.open_doc("docs/alias.md")
    r = await api.put(f"/api/v1/documents/{al['document_id']}/content", json={"text": "# via alias\n", "base_sha256": al["sha256"]})
    assert r.status_code == 200
    assert (workspace / "docs" / "alias.md").is_symlink() and (workspace / "docs" / "guide.md").read_text() == "# via alias\n"
    outside = tmp_path / "outside.md"
    outside.write_text("# outside\n")
    os.symlink(outside, workspace / "docs" / "escape.md")
    es = await api.open_doc("docs/escape.md")
    r = await api.put(f"/api/v1/documents/{es['document_id']}/content", json={"text": "hacked", "base_sha256": es["sha256"]})
    assert r.status_code == 403 and r.json()["error"]["code"] == "PATH_ESCAPES_WORKSPACE"
    assert outside.read_text() == "# outside\n"
