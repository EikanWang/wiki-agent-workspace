"""Diff review: file and folder diffs against Git refs, untracked files, line comments as discussions."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import Api


def _commit_all(ws: Path, msg: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ws, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True, check=True).stdout.strip()


async def test_folder_diff_vs_head_includes_modified_and_untracked_markdown(api: Api, workspace: Path):
    first = _commit_all(workspace, "guide committed")
    guide = workspace / "docs" / "guide.md"
    guide.write_text(guide.read_text().replace("device code.", "device code!!"))
    (workspace / "docs" / "new.md").write_text("# New page\n\nfresh\n")
    (workspace / "docs" / "notes.txt").write_text("not markdown\n")
    (workspace / "docs" / "sub").mkdir()
    (workspace / "docs" / "sub" / "deep.md").write_text("# Deep\n")
    r = await api.get("/api/v1/diff", params={"path": "docs"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["is_dir"] and d["base"] == "HEAD" and d["target"] == "worktree"
    by = {f["path"]: f for f in d["files"]}
    assert set(by) == {"docs/guide.md", "docs/new.md", "docs/sub/deep.md"}  # markdown_only drops notes.txt
    assert by["docs/guide.md"]["status"] == "modified" and "+TorchInductor converts the graph into device code!!" in by["docs/guide.md"]["unified"]
    assert by["docs/new.md"]["status"] == "untracked" and by["docs/new.md"]["additions"] == 3 and by["docs/new.md"]["unified"].startswith("--- /dev/null")
    assert d["summary"]["files"] == 3
    # include every file type
    all_files = {f["path"] for f in (await api.get("/api/v1/diff", params={"path": "docs", "markdown_only": "false"})).json()["files"]}
    assert "docs/notes.txt" in all_files
    # whole workspace, single file, and an explicit base/target pair
    assert len((await api.get("/api/v1/diff", params={"path": ""})).json()["files"]) >= 3
    single = (await api.get("/api/v1/diff", params={"path": "docs/guide.md"})).json()
    assert not single["is_dir"] and single["files"][0]["status"] == "modified" and single["files"][0]["target_sha256"]
    second = _commit_all(workspace, "edits")
    between = (await api.get("/api/v1/diff", params={"path": "docs", "base": first, "target": second})).json()
    assert {f["path"] for f in between["files"]} == {"docs/guide.md", "docs/new.md", "docs/sub/deep.md"}
    assert (await api.get("/api/v1/diff", params={"path": "docs"})).json()["files"] == []  # clean after commit
    # bad refs and traversal are rejected
    assert (await api.get("/api/v1/diff", params={"path": "docs", "base": "--output=/tmp/x"})).status_code == 400
    assert (await api.get("/api/v1/diff", params={"path": "docs", "base": "nope"})).json()["error"]["code"] == "REF_NOT_FOUND"
    assert (await api.get("/api/v1/diff", params={"path": "../etc"})).status_code == 400
    refs = (await api.get("/api/v1/refs")).json()
    assert [c["sha"] for c in refs["commits"]][:2] == [second, first] and "master" in refs["branches"] or "main" in refs["branches"]


async def test_diff_comment_becomes_anchored_discussion(api: Api, workspace: Path):
    _commit_all(workspace, "base")
    guide = workspace / "docs" / "guide.md"
    guide.write_text(guide.read_text().replace("TorchInductor converts the graph into device code.", "TorchInductor lowers the graph into device code."))
    d = (await api.get("/api/v1/diff", params={"path": "docs/guide.md"})).json()
    unified = d["files"][0]["unified"]
    added = next(ln[1:] for ln in unified.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    r = await api.post("/api/v1/diff/comments", json={
        "path": "docs/guide.md", "base": "HEAD", "target": "worktree", "side": "added", "line_no": 9,
        "line_text": added, "hunk": unified, "question": "Why 'lowers' instead of 'converts'?"})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["anchored"] is True
    ev = await api.wait_job(out["job_id"])
    assert ev[-1]["type"] == "completed"
    threads = (await api.threads(out["document_id"]))["threads"]
    t = threads[0]
    assert t["anchor"]["quote"] == "TorchInductor lowers the graph into device code." and t["anchor_state"] == "attached"
    assert t["diff"]["side"] == "added" and t["diff"]["base"] == "HEAD" and t["diff"]["anchored"] is True
    assert t["messages"][0]["content"] == "Why 'lowers' instead of 'converts'?"
    # the agent received the diff context in its prompt
    job = (await api.get(f"/api/v1/jobs/{out['job_id']}")).json()
    assert job["status"] == "completed"
    assert t["messages"][1]["content"].startswith("echo:") and "reviewing a diff" in t["messages"][1]["content"]
    # the diff view lists the comment on that file
    d2 = (await api.get("/api/v1/diff", params={"path": "docs"})).json()
    assert d2["comments"]["docs/guide.md"][0]["thread_id"] == out["thread_id"]
    # a comment on a removed line still creates a discussion, flagged as not anchored to current text
    removed = next(ln[1:] for ln in unified.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    r2 = await api.post("/api/v1/diff/comments", json={"path": "docs/guide.md", "side": "deleted", "line_text": removed, "question": "why removed?"})
    assert r2.status_code == 201 and r2.json()["anchored"] is False
    await api.wait_job(r2.json()["job_id"])
    t2 = [x for x in (await api.threads(out["document_id"]))["threads"] if x["id"] == r2.json()["thread_id"]][0]
    assert t2["diff"]["side"] == "deleted" and t2["anchor_state"] in ("orphaned", "attached")
    # non-markdown files cannot take comments
    assert (await api.post("/api/v1/diff/comments", json={"path": "README.txt", "line_text": "x", "question": "q"})).status_code == 400


async def test_diff_comment_on_markdown_source_line_anchors_to_rendered_text(api: Api, workspace: Path):
    _commit_all(workspace, "base")
    guide = workspace / "docs" / "guide.md"
    guide.write_text(guide.read_text().replace("- item one", "- item **one** (changed)").replace("## Lowering", "## Lowering stage"))
    r = await api.post("/api/v1/diff/comments", json={"path": "docs/guide.md", "side": "added", "line_text": "- item **one** (changed)", "question": "bullet?"})
    assert r.json()["anchored"] is True
    r2 = await api.post("/api/v1/diff/comments", json={"path": "docs/guide.md", "side": "added", "line_text": "## Lowering stage", "question": "heading?"})
    assert r2.json()["anchored"] is True
    threads = {t["messages"][0]["content"]: t for t in (await api.threads(r.json()["document_id"]))["threads"]}
    assert threads["bullet?"]["anchor"]["quote"] == "item one (changed)"
    assert threads["heading?"]["anchor"]["quote"] == "Lowering stage" and threads["heading?"]["anchor"]["heading"] == "Lowering stage"
    for t in threads.values():
        await api.wait_job(t["messages"][1]["job_id"])
