"""Read-only locks and non-Markdown document formats."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path

from conftest import Api, anchor_for


def _minimal_docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + body + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


async def test_lock_file_and_folder_blocks_app_writes_but_not_reading(api: Api, workspace: Path):
    doc = await api.open_doc()
    did = doc["document_id"]
    assert doc["lock"] is None
    r = await api.post("/api/v1/locks", json={"path": "docs/guide.md", "reason": "frozen for review"})
    assert r.status_code == 201 and r.json()["is_dir"] is False
    doc = await api.open_doc()
    assert doc["lock"]["path"] == "docs/guide.md" and doc["lock"]["reason"] == "frozen for review"
    tree = {i["name"]: i for i in (await api.get("/api/v1/files", params={"path": "docs"})).json()["items"]}
    assert tree["guide.md"]["locked"] is True and tree["guide.md"]["lock_inherited"] is False
    # discussion (read) still works; the agent is told the file is locked and the prompt says so
    a = anchor_for(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{did}/threads", json={**a, "version_sha256": doc["sha256"], "question": "please change this [[edit:docs/guide.md:::Guide:::HACKED]]"})
    ev = await api.wait_job(r.json()["job_id"])
    assert any(e["type"] == "notice" and "locked" in e["payload"]["message"] for e in ev)
    # the fake agent ignored the instruction and edited anyway -> loud violation, never silently reverted
    assert any(e["type"] == "lock_violation" for e in ev)
    assert "HACKED" in (workspace / "docs" / "guide.md").read_text()
    job = (await api.get(f"/api/v1/jobs/{r.json()['job_id']}")).json()
    assert job["status"] == "completed"
    # app writes are refused with 423
    doc = await api.open_doc()
    pv = (await api.post(f"/api/v1/documents/{did}/previews", json={"instruction": "[[edit:docs/guide.md:::HACKED:::Guide]]", "base_sha256": doc["sha256"]})).json()
    await api.wait_job(pv["job_id"])
    for _ in range(50):
        p = (await api.get(f"/api/v1/previews/{pv['preview_id']}")).json()
        if p["status"] != "pending":
            break
        await asyncio.sleep(0.1)
    r = await api.post(f"/api/v1/previews/{pv['preview_id']}/apply", json={"base_sha256": doc["sha256"]})
    assert r.status_code == 423 and r.json()["error"]["code"] == "PATH_LOCKED"
    assert (await api.delete(f"/api/v1/documents/{did}/history")).status_code == 423
    assert (await api.post(f"/api/v1/documents/{did}/relink", json={"path": "README.md"})).status_code == 423
    # folder lock covers children (inherited), the whole workspace lock covers everything
    await api.delete("/api/v1/locks", params={"path": "docs/guide.md"})
    assert (await api.open_doc())["lock"] is None
    r = await api.post("/api/v1/locks", json={"path": "docs"})
    assert r.json()["is_dir"] is True
    tree = {i["name"]: i for i in (await api.get("/api/v1/files", params={"path": "docs"})).json()["items"]}
    assert tree["guide.md"]["locked"] is True and tree["guide.md"]["lock_inherited"] is True
    assert (await api.open_doc())["lock"]["path"] == "docs"
    assert (await api.get("/api/v1/locks/check", params={"path": "docs/sub/x.md"})).json()["lock"]["path"] == "docs"
    assert (await api.get("/api/v1/locks/check", params={"path": "README.md"})).json()["lock"] is None
    await api.delete("/api/v1/locks", params={"path": "docs"})
    await api.post("/api/v1/locks", json={"path": ""})
    assert (await api.get("/api/v1/locks/check", params={"path": "README.md"})).json()["lock"]["path"] == ""
    assert len((await api.get("/api/v1/locks")).json()["locks"]) == 1
    assert (await api.delete("/api/v1/locks", params={"path": "nope"})).status_code == 404


async def test_codex_turn_on_locked_document_runs_read_only(api: Api, fake_env: Path):
    r = await api.post("/api/v1/agent-sessions", json={"provider": "codex", "display_name": "cx"})
    cx = r.json()["id"]
    doc = await api.open_doc()
    await api.post("/api/v1/locks", json={"path": "docs/guide.md"})
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "edit [[edit:docs/guide.md:::Guide:::X]]", "session_id": cx})
    ev = await api.wait_job(r.json()["job_id"])
    assert (fake_env / "codex_last_sandbox").read_text() == "read-only"
    assert ev[-1]["type"] == "failed" and "read-only" in ev[-1]["payload"]["message"]  # the fake honours the sandbox
    assert not any(e["type"] == "lock_violation" for e in ev)


async def test_antigravity_turn_on_locked_document_runs_in_plan_mode(api: Api, fake_env: Path, workspace: Path):
    import json

    r = await api.post("/api/v1/agent-sessions", json={"provider": "antigravity", "display_name": "ag"})
    ag = r.json()["id"]
    await api.post("/api/v1/locks", json={"path": "docs/guide.md"})
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "edit [[edit:docs/guide.md:::Guide:::X]]", "session_id": ag})
    ev = await api.wait_job(r.json()["job_id"])
    call = json.loads((fake_env / "agy_calls.log").read_text().splitlines()[-1])
    assert call[call.index("--mode") + 1] == "plan" and "--dangerously-skip-permissions" not in call
    assert ev[-1]["type"] == "completed" and "Guide" in (workspace / "docs" / "guide.md").read_text()
    assert any(e["type"] == "notice" and "Antigravity plan mode" in e["payload"].get("message", "") for e in ev)


async def test_open_text_source_docx_and_pdf(api: Api, workspace: Path):
    (workspace / "src").mkdir()
    (workspace / "src" / "kernel.cpp").write_text("#include <cstdio>\nint main() {\n  printf(\"hi\");\n}\n")
    (workspace / "src" / "config.yaml").write_text("name: demo\nvalue: 3\n")
    (workspace / "src" / "report.docx").write_bytes(_minimal_docx(["Executive summary", "The kernel is memory bound."]))
    (workspace / "src" / "paper.pdf").write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    cpp = (await api.get("/api/v1/documents/open", params={"path": "src/kernel.cpp"})).json()
    assert cpp["format"] == "text" and 'language-cpp' in cpp["html"] and cpp["text"].startswith("#include")
    # discussions anchor to source lines like any rendered text
    threads = await api.threads(cpp["document_id"])
    assert threads["threads"] == []
    from backend.app.textmodel import build_text_model_from_html

    model = build_text_model_from_html(cpp["html"]).text
    q = 'printf("hi");'
    i = model.index(q)
    r = await api.post(f"/api/v1/documents/{cpp['document_id']}/threads", json={"version_sha256": cpp["sha256"], "quote": q, "prefix": model[max(0, i - 40):i], "suffix": model[i + len(q):i + len(q) + 40], "start": i, "end": i + len(q), "question": "why printf?"})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    assert (await api.threads(cpp["document_id"]))["threads"][0]["anchor_state"] == "attached"
    yaml_doc = (await api.get("/api/v1/documents/open", params={"path": "src/config.yaml"})).json()
    assert yaml_doc["format"] == "text" and "language-yaml" in yaml_doc["html"]
    docx = (await api.get("/api/v1/documents/open", params={"path": "src/report.docx"})).json()
    assert docx["format"] == "docx" and "<p>Executive summary</p>" in docx["html"] and docx["text"] == ""
    model = build_text_model_from_html(docx["html"]).text
    assert "memory bound" in model
    q = "memory bound"
    i = model.index(q)
    r = await api.post(f"/api/v1/documents/{docx['document_id']}/threads", json={"version_sha256": docx["sha256"], "quote": q, "prefix": model[max(0, i - 40):i], "suffix": model[i + len(q):i + len(q) + 40], "start": i, "end": i + len(q), "question": "evidence?"})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    pdf = (await api.get("/api/v1/documents/open", params={"path": "src/paper.pdf"})).json()
    assert pdf["format"] == "pdf" and pdf["html"] is None
    raw = await api.get("/api/v1/raw", params={"path": "src/paper.pdf"})
    assert raw.status_code == 200 and raw.headers["content-type"].startswith("application/pdf") and raw.headers["x-frame-options"] == "SAMEORIGIN"
    # SVG is served as an image (agent answers and pages show it), sandboxed when opened directly
    (workspace / "src" / "chart.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    svg = await api.get("/api/v1/raw", params={"path": "src/chart.svg"})
    assert svg.headers["content-type"].startswith("image/svg+xml") and "sandbox" in svg.headers["content-security-policy"]
    # tree flags
    items = {i["name"]: i for i in (await api.get("/api/v1/files", params={"path": "src"})).json()["items"]}
    assert all(items[n]["openable"] for n in ("kernel.cpp", "config.yaml", "report.docx", "paper.pdf"))
    assert items["report.docx"]["format"] == "docx"
    # non-UTF-8 "text" file is reported, not crashed on
    (workspace / "src" / "bad.txt").write_bytes(b"\xff\xfe\x00bad")
    assert (await api.get("/api/v1/documents/open", params={"path": "src/bad.txt"})).json()["error"]["code"] == "INVALID_UTF8"
    assert json.dumps(items["kernel.cpp"]["kind"]) == '"code"'


def test_a_large_document_is_rendered_once_per_content(monkeypatch):
    """Opening a big Word file, asking about it and listing its discussions all need its HTML: converted once."""
    from backend.app import documents

    calls: list[bytes] = []
    monkeypatch.setattr(documents, "render_document", lambda fmt, data, name: calls.append(data) or f"<p>{len(calls)}</p>")
    big = b"x" * (100 * 1024)
    assert documents.DocumentService.render("a.docx", big) == ("docx", "<p>1</p>")
    assert documents.DocumentService.render("b.docx", big) == ("docx", "<p>1</p>")  # same content: cached
    assert documents.DocumentService.render("a.docx", big + b"y")[1] == "<p>2</p>"  # changed: rendered again
    documents.DocumentService.render("s.docx", b"small")
    documents.DocumentService.render("s.docx", b"small")
    assert len(calls) == 4  # small files are not kept
