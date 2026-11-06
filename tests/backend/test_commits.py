"""Commit workflow: status, agent-drafted message, selective commit, push/PR guards."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import Api


def _git(ws: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True, check=False).stdout.strip()


async def test_status_draft_and_selective_commit(api: Api, workspace: Path):
    (workspace / "docs" / "guide.md").write_text((workspace / "docs" / "guide.md").read_text() + "\nAppended line.\n")
    (workspace / "docs" / "new.md").write_text("# New\n")
    (workspace / "README.md").write_text("# Repo\n\nchanged readme\n")
    st = (await api.get("/api/v1/git/status")).json()
    assert st["branch"] in ("master", "main") and st["identity"]["configured"] is True and st["upstream"] is None
    by = {f["path"]: f["status"] for f in st["files"]}
    assert by == {"README.md": "modified", "docs/guide.md": "untracked", "docs/new.md": "untracked"}
    # draft a message for two of the three files
    r = await api.post("/api/v1/git/draft-message", json={"paths": ["docs/guide.md", "docs/new.md"]})
    assert r.status_code == 202, r.text
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "completed"
    msg = ev[-1]["payload"]["text"]
    assert msg.startswith("Update docs/guide.md") and "summarise the diff" in msg
    # commit only those two files; README stays modified
    r = await api.post("/api/v1/git/commit", json={"paths": ["docs/guide.md", "docs/new.md"], "message": msg})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["subject"] == "Update docs/guide.md" and set(out["files"]) == {"docs/guide.md", "docs/new.md"}
    assert _git(workspace, "log", "-1", "--format=%s") == "Update docs/guide.md"
    assert set(_git(workspace, "show", "--name-only", "--format=", "HEAD").split()) == {"docs/guide.md", "docs/new.md"}
    assert _git(workspace, "log", "-1", "--format=%an <%ae>") == "T <t@example.com>"  # the user's identity, not the app's
    st = (await api.get("/api/v1/git/status")).json()
    assert [f["path"] for f in st["files"]] == ["README.md"]
    assert st["recent_commits"][0]["subject"] == "Update docs/guide.md"
    # guards
    assert (await api.post("/api/v1/git/commit", json={"paths": ["docs/guide.md"], "message": "x"})).json()["error"]["code"] == "NOT_CHANGED"
    assert (await api.post("/api/v1/git/commit", json={"paths": ["README.md"], "message": "-rf"})).status_code == 400
    assert (await api.post("/api/v1/git/draft-message", json={"paths": ["docs/guide.md"]})).json()["error"]["code"] == "NOTHING_TO_COMMIT"
    assert (await api.post("/api/v1/git/commit", json={"paths": ["../x"], "message": "m"})).status_code == 400
    # a locked file cannot be committed through the app
    await api.post("/api/v1/locks", json={"path": "README.md"})
    assert (await api.post("/api/v1/git/commit", json={"paths": ["README.md"], "message": "m"})).status_code == 423
    await api.delete("/api/v1/locks", params={"path": "README.md"})
    # multi-line message is preserved verbatim
    r = await api.post("/api/v1/git/commit", json={"paths": ["README.md"], "message": "Tweak readme\n\n- line one\n- line two\n"})
    assert r.status_code == 201
    assert _git(workspace, "log", "-1", "--format=%B").strip() == "Tweak readme\n\n- line one\n- line two"


async def test_push_to_a_local_bare_remote_and_pr_guards(api: Api, workspace: Path, tmp_path: Path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=workspace, check=True)
    (workspace / "README.md").write_text("# Repo\n\npush me\n")
    r = await api.post("/api/v1/git/commit", json={"paths": ["README.md"], "message": "Push me"})
    assert r.status_code == 201
    st = (await api.get("/api/v1/git/status")).json()
    assert st["origin"] == str(bare) and st["origin_is_github"] is False and st["upstream"] is None
    r = await api.post("/api/v1/git/push", json={})
    assert r.status_code == 200, r.text
    assert r.json()["upstream"] in ("origin/master", "origin/main") and r.json()["ahead"] == 0
    assert _git(bare, "log", "-1", "--format=%s") == "Push me"
    # a diverging remote is never force-pushed over: the app reports PUSH_REJECTED
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    subprocess.run(["git", "-c", "user.name=O", "-c", "user.email=o@x", "commit", "-q", "--allow-empty", "-m", "remote work"], cwd=other, check=True)
    subprocess.run(["git", "push", "-q"], cwd=other, check=True)
    (workspace / "README.md").write_text("# Repo\n\npush me again\n")
    await api.post("/api/v1/git/commit", json={"paths": ["README.md"], "message": "Local work"})
    r = await api.post("/api/v1/git/push", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "PUSH_REJECTED"
    assert _git(bare, "log", "-1", "--format=%s") == "remote work"
    # PR creation needs GitHub + gh; here it is refused with an actionable code (no network involved)
    r = await api.post("/api/v1/git/pull-request", json={"title": "My change"})
    assert r.status_code == 409 and r.json()["error"]["code"] in ("GH_UNAVAILABLE", "NOT_GITHUB")
    assert (await api.post("/api/v1/git/push", json={"remote": "--upstream"})).status_code == 400
