"""GitHub-style reviews: stash comments, submit once, agent triages (address / reply / skip)."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from conftest import Api


def _commit_all(ws: Path, msg: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ws, check=True)


async def test_stash_edit_delete_and_submit_review(api: Api, workspace: Path):
    _commit_all(workspace, "base")
    guide = workspace / "docs" / "guide.md"
    guide.write_text(guide.read_text().replace("- item one\n- item two\n", "- item one\n- item twoo\n- item three\n"))
    (workspace / "docs" / "notes.md").write_text("# Notes\n\nTemporary draft.\n")
    cur = (await api.get("/api/v1/reviews/current")).json()
    assert cur["pending"]["id"] is None and cur["pending"]["comments"] == [] and cur["last"] is None
    # stash three comments; nothing runs
    c1 = (await api.post("/api/v1/reviews/comments", json={"path": "docs/guide.md", "side": "added", "line_no": 15, "line_text": "- item twoo", "body": "Typo: please change twoo to two."})).json()
    c2 = (await api.post("/api/v1/reviews/comments", json={"path": "docs/guide.md", "side": "added", "line_no": 16, "line_text": "- item three", "body": "Why a third item?"})).json()
    c3 = (await api.post("/api/v1/reviews/comments", json={"path": "docs/notes.md", "side": "added", "line_no": 3, "line_text": "Temporary draft.", "body": "[[skip]] ignore this one"})).json()
    assert c1["review_id"] == c2["review_id"] == c3["review_id"]
    rid = c1["review_id"]
    cur = (await api.get("/api/v1/reviews/current")).json()
    assert cur["pending"]["id"] == rid and len(cur["pending"]["comments"]) == 3 and cur["pending"]["status"] == "pending"
    assert (await api.get("/api/v1/documents")).json()  # no threads yet for guide
    threads_before = (await api.threads((await api.open_doc("docs/guide.md"))["document_id"]))["threads"]
    assert threads_before == []
    # edit one, delete one, add it back
    r = await api.put(f"/api/v1/reviews/comments/{c2['id']}", json={"body": "Why a third item? Is it needed?"})
    assert r.json()["body"].endswith("needed?")
    assert (await api.delete(f"/api/v1/reviews/comments/{c3['id']}")).json()["deleted"] is True
    c3 = (await api.post("/api/v1/reviews/comments", json={"path": "docs/notes.md", "side": "added", "line_no": 3, "line_text": "Temporary draft.", "body": "[[skip]] ignore this one"})).json()
    # submit: one agent job for all comments
    r = await api.post(f"/api/v1/reviews/{rid}/submit", json={"summary": "Overall looks fine, small fixes."})
    assert r.status_code == 202, r.text
    assert r.json()["comments"] == 3
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "completed"
    for _ in range(50):
        rv = (await api.get(f"/api/v1/reviews/{rid}")).json()
        if rv["status"] != "submitted":
            break
        await asyncio.sleep(0.1)
    assert rv["status"] == "completed" and rv["summary"].startswith("Overall")
    by = {c["path"] + ":" + str(c["line_no"]): c for c in rv["comments"]}
    assert by["docs/guide.md:15"]["decision"] == "address" and "twoo" in by["docs/guide.md:15"]["response"]
    assert by["docs/guide.md:16"]["decision"] == "reply" and by["docs/guide.md:16"]["response"].startswith("Answer to:")
    assert by["docs/notes.md:3"]["decision"] == "skip"
    assert rv["result"]["summary"] == "Handled 3 comments."
    # the agent actually addressed the typo, and only that
    assert "- item two\n- item three\n" in guide.read_text() and "twoo" not in guide.read_text()
    assert (workspace / "docs" / "notes.md").read_text() == "# Notes\n\nTemporary draft.\n"
    # each comment became a discussion thread with the verdict as the agent's answer
    doc = await api.open_doc("docs/guide.md")
    threads = (await api.threads(doc["document_id"]))["threads"]
    assert len(threads) == 2
    verdicts = {t["messages"][0]["content"]: t["messages"][1]["content"] for t in threads}
    assert verdicts["Typo: please change twoo to two."].startswith("**Addressed.**")
    assert verdicts["Why a third item? Is it needed?"].startswith("**Reply.**")
    assert all(t["messages"][1]["status"] == "completed" and t["diff"]["line_no"] in (15, 16) for t in threads)
    # a new pending review starts empty; the finished one is reported as `last`
    cur = (await api.get("/api/v1/reviews/current")).json()
    assert cur["pending"]["id"] is None and cur["last"]["id"] == rid
    # submitted reviews are immutable
    assert (await api.put(f"/api/v1/reviews/comments/{c1['id']}", json={"body": "x"})).json()["error"]["code"] == "REVIEW_NOT_PENDING"
    assert (await api.post(f"/api/v1/reviews/{rid}/submit", json={})).json()["error"]["code"] == "REVIEW_NOT_PENDING"


async def test_review_respects_locks_and_empty_submit(api: Api, workspace: Path):
    _commit_all(workspace, "base")
    guide = workspace / "docs" / "guide.md"
    guide.write_text(guide.read_text().replace("item one", "item onee"))
    await api.post("/api/v1/locks", json={"path": "docs/guide.md", "reason": "frozen"})
    c = (await api.post("/api/v1/reviews/comments", json={"path": "docs/guide.md", "side": "added", "line_no": 14, "line_text": "- item onee", "body": "please change onee to one."})).json()
    r = await api.post(f"/api/v1/reviews/{c['review_id']}/submit", json={})
    assert r.status_code == 202
    await api.wait_job(r.json()["job_id"])
    for _ in range(50):
        rv = (await api.get(f"/api/v1/reviews/{c['review_id']}")).json()
        if rv["status"] != "submitted":
            break
        await asyncio.sleep(0.1)
    assert rv["comments"][0]["decision"] == "reply" and "locked" in rv["comments"][0]["response"].lower()
    assert "onee" in guide.read_text()  # untouched
    # empty pending review cannot be submitted; discard works
    c2 = (await api.post("/api/v1/reviews/comments", json={"path": "docs/guide.md", "side": "added", "line_no": 14, "line_text": "- item onee", "body": "tmp"})).json()
    await api.delete(f"/api/v1/reviews/comments/{c2['id']}")
    assert (await api.post(f"/api/v1/reviews/{c2['review_id']}/submit", json={})).json()["error"]["code"] == "EMPTY_REVIEW"
    assert (await api.delete(f"/api/v1/reviews/{c2['review_id']}")).json()["id"] == c2["review_id"]
    assert (await api.get("/api/v1/reviews/current")).json()["pending"]["id"] is None
