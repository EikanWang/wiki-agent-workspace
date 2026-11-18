"""References in agent answers: the prompt asks for links to the places, POST /refs/resolve finds where they are."""

from __future__ import annotations

from pathlib import Path

from conftest import anchor_for, make_workspace

from backend.app.models import Job


async def _resolve(api, target: str, from_path: str | None = None) -> list[dict]:
    r = await api.post("/api/v1/refs/resolve", json={"targets": [target], "from_path": from_path})
    assert r.status_code == 200, r.text
    return r.json()["results"][0]["candidates"]


async def test_links_resolve_in_any_open_folder_and_a_name_several_files_have_lists_them(running_app, workspace: Path, tmp_path: Path):
    h = await running_app()
    api = h.api
    other = tmp_path / "papers"
    make_workspace(other)
    (other / "a").mkdir()
    (other / "a" / "notes.md").write_text("# A\n")
    (other / "b").mkdir()
    (other / "b" / "notes.md").write_text("# B\n")
    (other / "kernels.pdf").write_bytes(b"%PDF-1.4\n")
    r = await api.post("/api/v1/workspaces", json={"root": str(other)})
    assert r.status_code == 201, r.text
    papers = r.json()["id"]
    # an absolute path in an open folder: that folder's document, the fragment kept as given
    c = await _resolve(api, f"{other}/kernels.pdf#page=7")
    assert c == [{"kind": "doc", "workspace_id": papers, "workspace_name": "papers", "path": "kernels.pdf",
                  "abs": str(other / "kernels.pdf"), "fragment": "page=7"}]
    c = await _resolve(api, f"{workspace}/docs/guide.md#Capture%20details")
    assert [(x["path"], x["fragment"]) for x in c] == [("docs/guide.md", "Capture details")] and c[0]["workspace_id"] != papers
    # the wrong folder (the agent's guess): found by its name; relative to the document discussed
    c = await _resolve(api, "/somewhere/else/kernels.pdf#page=2")
    assert [(x["workspace_id"], x["path"], x["fragment"]) for x in c] == [(papers, "kernels.pdf", "page=2")]
    assert [x["path"] for x in await _resolve(api, "guide.md", from_path="docs/README.md")] == ["docs/guide.md"]
    # one name, several files: all of them, the closest path first
    c = await _resolve(api, "/x/b/notes.md#B")
    assert [x["path"] for x in c] == ["b/notes.md", "a/notes.md"]
    # elsewhere on the server: Local files; nowhere: nothing
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n")
    assert await _resolve(api, f"{outside}#L3") == [{"kind": "local", "abs": str(outside), "fragment": "L3"}]
    assert await _resolve(api, "/no/such/file-anywhere.md") == []


async def test_document_prompts_give_the_absolute_path_and_ask_for_links(running_app, workspace: Path):
    h = await running_app()
    api = h.api
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "q"})
    await api.wait_job(r.json()["job_id"])
    with h.ctx.db.read() as s:
        prompt = s.query(Job).order_by(Job.created_at.desc()).first().prompt
    assert f"Document path: {workspace / 'docs/guide.md'} (in the folder {workspace})" in prompt
    assert "LINKS TO WHAT YOU REFER TO" in prompt and "#page=N" in prompt and str(workspace) in prompt
