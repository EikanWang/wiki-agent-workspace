"""Reviews ▸ settings ▸ Skills: the skills an agent PR review gets, edited in place; the next review reads the edit."""

from __future__ import annotations

import subprocess
from pathlib import Path

from test_batch_review import _drain, _setup
from test_github_hub import as_github

SKILL = "---\nname: {name}\ndescription: {desc}\n---\n\n# body\n"


def _skill(root: Path, rel: str, name: str, desc: str) -> Path:
    p = root / ".claude/skills" / rel / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SKILL.format(name=name, desc=desc))
    return p


async def test_list_edit_and_the_next_review_uses_it(running_app, workspace, fake_env, tmp_path):
    rev = _skill(workspace, "pr/gpu-review", "gpu-review", "Review PRs for Intel GPU naming")
    _skill(workspace, "pptx", "pptx-to-markdown", "Convert slides")
    checkout = tmp_path / "pytorch"
    up = _skill(checkout, "pr-review", "pr-review", "Review PyTorch pull requests")
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    h, ws = await _setup(running_app)
    api = h.api
    from backend.app.kv import kv_put
    from backend.app.models import GithubRepoWiki

    kv_put(h.ctx, "local_repo:pytorch/pytorch", {"path": str(checkout), "update": True, "remote": "origin"})
    with h.ctx.db.write() as s:
        s.add(GithubRepoWiki(repo="pytorch/pytorch", workspace_id=ws))

    r = (await api.get("/api/v1/github/review-skills?repo=pytorch/pytorch")).json()
    assert r["repos"] == ["pytorch/pytorch"] and r["checkout"] == str(checkout) and r["wiki"] == ws
    by = {s["name"]: s for s in r["skills"]}
    assert by["gpu-review"]["review"] and by["pr-review"]["review"] and not by["pptx-to-markdown"]["review"]
    # the checkout's own skill is tracked by its git: editing it makes the checkout dirty
    assert by["pr-review"]["checkout_of"] == "pytorch/pytorch" and by["gpu-review"]["checkout_of"] is None
    # read, then save over that version; the list answered is the one the next review sees
    f = (await api.get("/api/v1/github/review-skills/file", params={"path": str(rev)})).json()
    new = f["text"].replace("Review PRs for Intel GPU naming", "Review every PR for XeCore naming")
    w = await api.put("/api/v1/github/review-skills/file", json={"path": str(rev), "text": new, "base_sha256": f["sha256"], "repo": "pytorch/pytorch"})
    assert w.status_code == 200, w.text
    assert {s["name"]: s for s in w.json()["skills"]}["gpu-review"]["description"] == "Review every PR for XeCore naming"
    # stale version, a lost header, a file that is no skill of a review: refused
    stale = await api.put("/api/v1/github/review-skills/file", json={"path": str(rev), "text": new, "base_sha256": f["sha256"]})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "FILE_CHANGED"
    nohead = await api.put("/api/v1/github/review-skills/file", json={"path": str(rev), "text": "# no header", "base_sha256": w.json()["sha256"]})
    assert nohead.status_code == 400 and nohead.json()["error"]["code"] == "NO_FRONTMATTER"
    other = tmp_path / "elsewhere" / "SKILL.md"
    other.parent.mkdir()
    other.write_text(SKILL.format(name="x", desc="y"))
    assert (await api.get("/api/v1/github/review-skills/file", params={"path": str(other)})).status_code == 404
    assert (await api.get("/api/v1/github/review-skills/file", params={"path": str(workspace / "README.md")})).status_code == 400
    assert rev.read_text() == new and "Review PyTorch" in up.read_text()
    # the next agent review's prompt lists the edited skill
    r = await api.post("/api/v1/github/batch-review", json={"prs": [{"repo": "pytorch/pytorch", "number": 7}], "workspace_id": ws})
    assert r.status_code == 202, r.text
    await _drain()
    assert "gpu-review: Review every PR for XeCore naming" in (fake_env / "opencode_last_prompt").read_text()
    # only the owner signed in with GitHub
    as_github(h, "someone-else")
    assert (await api.get("/api/v1/github/review-skills")).status_code == 403
