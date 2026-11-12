"""PR Development: worktrees (new branch / existing PR), editor lifecycle + proxy, agent turns with the wiki, tasks,
git/PR lifecycle, the integration extension's API, SSH hosts (through a fake ssh that runs commands locally)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import FAKES, ORIGIN, Api, make_settings

from backend.app.config import DevCompletion, DevHost, DevSettings


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_source(tmp: Path) -> tuple[Path, Path]:
    """A source checkout (C++ + Python) with a bare `origin`."""
    bare = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    src = tmp / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    git(src, "config", "user.email", "dev@example.com")
    git(src, "config", "user.name", "Dev")
    (src / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.16)\nproject(p CXX)\n")
    (src / "pyproject.toml").write_text("[project]\nname='p'\n")
    (src / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "init")
    git(src, "remote", "add", "origin", str(bare))
    git(src, "push", "-q", "origin", "main")
    return src, bare


@pytest.fixture
def dev_settings(tmp_path, workspace):
    s = make_settings(tmp_path, workspace)
    s.development = DevSettings(code_server=str(FAKES / "fake-code-server"), ssh=str(FAKES / "fake-ssh"),
                                worktrees_dir=tmp_path / "worktrees",
                                hosts=[DevHost(name="box", ssh="me@box", work_dir=str(tmp_path / "remote"),
                                               code_server=str(FAKES / "fake-code-server"), opencode=str(FAKES / "fake-opencode"),
                                               codex=str(FAKES / "fake-codex")),
                                       DevHost(name="down", ssh="unreachable.example", work_dir="/tmp/x")])
    return s


async def wait_ready(api: Api, dev_id: str) -> dict:
    for _ in range(200):
        d = (await api.get(f"/api/v1/dev/workspaces/{dev_id}")).json()
        if d["status"] != "creating":
            return d
        await asyncio.sleep(0.05)
    raise AssertionError("still creating")


async def wait_editor(api: Api, dev_id: str, want: str = "running") -> dict:
    for _ in range(300):
        d = (await api.get(f"/api/v1/dev/workspaces/{dev_id}")).json()["editor"]
        if d["state"] in (want, "failed"):
            return d
        await asyncio.sleep(0.05)
    raise AssertionError("editor did not start")


async def new_branch_ws(api: Api, src: Path, wiki_id: str | None, host: str = "local", **kw) -> dict:
    r = await api.post("/api/v1/dev/workspaces", json={"host": host, "source_repo": str(src), "mode": "branch", "branch": "feature/x",
                                                       "wiki_workspace_id": wiki_id, **kw})
    assert r.status_code == 201, r.text
    d = await wait_ready(api, r.json()["id"])
    assert d["status"] == "ready", d
    return d


async def test_new_branch_workspace_editor_proxy_and_agent(running_app, dev_settings, tmp_path, fake_env):
    h = await running_app(dev_settings)
    api = h.api
    src, bare = make_source(tmp_path)
    info = (await api.get("/api/v1/dev/info")).json()
    assert info["editor"]["available"] and [x["name"] for x in info["hosts"]] == ["local", "box", "down"]
    assert info["completion"]["enabled"] is False
    wiki_id = h.ctx.workspace_id
    d = await new_branch_ws(api, src, wiki_id)
    wt = Path(d["worktree_path"])
    assert wt.is_dir() and git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "feature/x"
    assert git(src, "rev-parse", "--abbrev-ref", "HEAD") == "main"  # the user's checkout is untouched
    assert d["base_ref"] == "main" and d["push_remote"] == "origin" and d["push_branch"] == "feature/x"
    s = d["settings"]
    assert set(s["languages"]) == {"cpp", "python"} and "cmake --build build" in s["build_command"] and "pytest" in s["test_command"]
    assert s["compile_commands_dir"] == "build"
    assert any(w["id"] == d["id"] for w in (await api.get("/api/v1/dev/workspaces")).json()["workspaces"])

    # --- editor: starts on a private socket with the integration extension installed and a generated workspace file
    r = await api.post(f"/api/v1/dev/workspaces/{d['id']}/editor/start")
    assert r.status_code == 200, r.text
    ed = await wait_editor(api, d["id"])
    assert ed["state"] == "running", ed
    env = json.loads((fake_env / "code_server_env.json").read_text())
    assert env["WAW_DEV_ID"] == d["id"] and env["WAW_DEV_URL"].startswith("http://127.0.0.1:") and len(env["WAW_DEV_TOKEN"]) > 20
    wsf = json.loads(Path(env["workspace"]).read_text())
    assert wsf["folders"][0]["path"] == str(wt) and wsf["folders"][1]["path"] == str(h.ctx.documents.root)
    assert wsf["settings"]["files.readonlyInclude"] == {f"{h.ctx.documents.root}/**": True}
    assert f"--compile-commands-dir={wt}/build" in wsf["settings"]["clangd.arguments"]
    assert [t["label"] for t in wsf["tasks"]["tasks"]] == ["waw: build", "waw: test"]
    assert any(c["type"] == "debugpy" for c in wsf["launch"]["configurations"])
    calls = [json.loads(x) for x in (fake_env / "code_server_calls.log").read_text().splitlines()]
    assert any("--install-extension" in c and c[c.index("--install-extension") + 1].endswith(".vsix") for c in calls)
    serve = next(c for c in calls if "--socket" in c)
    assert serve[serve.index("--auth") + 1] == "none" and serve[serve.index("--socket-mode") + 1] == "600"
    assert (Path(dev_settings.state_dir) / "dev" / "code-server" / "extensions" / "wiki-agent-workspace.waw-dev-0.1.0" / "extension.js").is_file()
    assert ed["url"].startswith(f"/dev-editor/{d['id']}/?workspace=")

    # --- proxy: needs the login; strips the app cookie; frames only by this app; relative/absolute redirects stay inside
    page = await api.get(f"/dev-editor/{d['id']}/some/page?x=1")
    assert page.status_code == 200 and "fake-code-server /some/page?x=1" in page.text
    assert page.headers["x-frame-options"] == "SAMEORIGIN" and "content-security-policy" not in page.headers
    assert page.headers.get_list("set-cookie") == ["a=1; Path=/", "b=2; Path=/"]
    red = await api.get(f"/dev-editor/{d['id']}/redirect")
    assert red.status_code == 302 and red.headers["location"] == f"/dev-editor/{d['id']}/login"
    sess = api.c.cookies.get("waw_session")
    echo = (await api.c.post(f"/dev-editor/{d['id']}/echo", content=b"hi", headers={"origin": ORIGIN, "cookie": f"x=1; waw_session={sess}"})).json()
    assert echo["body"] == "hi" and echo["cookie"] == "x=1" and echo["origin"] == "http://localhost" and echo["host"] == "localhost"
    assert (await api.c.post(f"/dev-editor/{d['id']}/echo", content=b"hi", headers={"origin": "https://evil.example"})).status_code == 403
    import httpx

    anon = httpx.AsyncClient(transport=api.c._transport, base_url=ORIGIN)
    assert (await anon.get(f"/dev-editor/{d['id']}/")).status_code == 401
    await anon.aclose()

    # --- extension API: token only (cookies are not enough)
    tok = {"authorization": f"Bearer {env['WAW_DEV_TOKEN']}"}
    assert (await api.get(f"/api/v1/dev/ext/{d['id']}/info")).status_code == 401
    ei = (await api.get(f"/api/v1/dev/ext/{d['id']}/info", headers=tok)).json()
    assert ei["branch"] == "feature/x" and "fix" in ei["kinds"]
    assert (await api.c.post(f"/api/v1/dev/ext/{d['id']}/complete", json={"prefix": "x"}, headers=tok)).json()["disabled"] is True

    # --- agent turn from the editor: runs in the worktree, wiki as reference, editor context in the prompt
    r = await api.c.post(f"/api/v1/dev/ext/{d['id']}/ask", headers=tok, json={
        "text": "Why does add fail? [[write:new_test.py:::def test_add(): pass]]", "kind": "fix",
        "context": {"file": "lib.py", "language": "python", "selection": {"start_line": 1, "end_line": 2, "text": "def add(a, b):"},
                    "diagnostics": [{"file": "lib.py", "line": 2, "severity": "error", "message": "bad operand", "source": "basedpyright"}]}})
    assert r.status_code == 201, r.text
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "completed", ev[-1]
    assert (wt / "new_test.py").read_text() == "def test_add(): pass"  # the agent wrote in the worktree
    job = (await api.get(f"/api/v1/dev/ext/{d['id']}/jobs/{r.json()['job_id']}", headers=tok)).json()
    assert job["status"] == "completed" and job["text"].startswith("wrote new_test.py")
    with h.ctx.db.read() as s:
        from backend.app.models import Job

        prompt = s.get(Job, r.json()["job_id"]).prompt
    assert f"Git worktree at {wt}" in prompt and f"REFERENCE WIKI: the whole folder {h.ctx.documents.root}" in prompt
    assert "Wiki references" in prompt and "bad operand [basedpyright]" in prompt and "`lib.py` lines 1–2 (python)" in prompt
    assert "Do not commit, push" in prompt and "Instructions: Fix the problem" in prompt
    # continue the same conversation from the app
    conv = (await api.get(f"/api/v1/dev/workspaces/{d['id']}/conversation")).json()
    tid = conv["threads"][0]["id"]
    r2 = await api.post(f"/api/v1/dev/workspaces/{d['id']}/ask", json={"text": "and now explain", "kind": "explain", "thread_id": tid})
    assert r2.status_code == 201, r2.text
    await api.wait_job(r2.json()["job_id"])
    conv = (await api.get(f"/api/v1/dev/workspaces/{d['id']}/conversation")).json()
    assert len(conv["threads"]) == 1 and [m["role"] for m in conv["threads"][0]["messages"]] == ["user", "agent", "user", "agent"]

    # --- git: status / diff (untracked included) / commit / push
    st = (await api.get(f"/api/v1/dev/workspaces/{d['id']}/status")).json()
    assert st["files"] == [{"path": "new_test.py", "status": "untracked"}] and st["commits_on_branch"] == 0
    assert "+def test_add(): pass" in (await api.get(f"/api/v1/dev/workspaces/{d['id']}/diff")).json()["diff"]
    assert (await api.post(f"/api/v1/dev/workspaces/{d['id']}/commit", json={"message": "x", "paths": ["nope"]})).status_code == 400
    c = await api.post(f"/api/v1/dev/workspaces/{d['id']}/commit", json={"message": "Add a test"})
    assert c.status_code == 200, c.text
    st = (await api.get(f"/api/v1/dev/workspaces/{d['id']}/status")).json()
    assert st["files"] == [] and st["commits_on_branch"] == 1 and st["unpushed"] == 1 and st["subject"] == "Add a test"
    p = await api.post(f"/api/v1/dev/workspaces/{d['id']}/push")
    assert p.status_code == 200, p.text
    assert git(bare, "log", "-1", "--format=%s", "feature/x") == "Add a test"
    assert (await api.get(f"/api/v1/dev/workspaces/{d['id']}/status")).json()["unpushed"] == 0

    # --- settings update rewrites the workspace file of the running editor
    r = await api.patch(f"/api/v1/dev/workspaces/{d['id']}", json={"settings": {"python": "/opt/venv/bin/python", "debug_program": "build/app"}})
    assert r.status_code == 200 and r.json()["settings"]["python"] == "/opt/venv/bin/python"
    wsf = json.loads(Path(env["workspace"]).read_text())
    assert wsf["settings"]["python.defaultInterpreterPath"] == "/opt/venv/bin/python"
    assert any(c["type"] == "lldb" and c["program"] == f"{wt}/build/app" for c in wsf["launch"]["configurations"])

    # --- stop; archive refuses to drop a worktree with work in it, unless forced
    await api.post(f"/api/v1/dev/workspaces/{d['id']}/editor/stop")
    assert (await api.get(f"/dev-editor/{d['id']}/")).status_code == 409
    (wt / "dirty.txt").write_text("x")
    r = await api.delete(f"/api/v1/dev/workspaces/{d['id']}", params={"remove_worktree": "true"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "WORKTREE_DIRTY"
    r = await api.delete(f"/api/v1/dev/workspaces/{d['id']}", params={"remove_worktree": "true", "force": "true"})
    assert r.status_code == 200 and r.json()["worktree_removed"] and not wt.exists()
    assert all(w["id"] != d["id"] for w in (await api.get("/api/v1/dev/workspaces")).json()["workspaces"])


async def test_editor_websocket_relay(running_app, dev_settings, tmp_path, fake_env):
    """A real uvicorn server: the editor's WebSocket goes through the app with the cookie and a same-origin Origin."""
    import socket

    import uvicorn
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    h = await running_app(dev_settings)
    src, _ = make_source(tmp_path)
    d = await new_branch_ws(h.api, src, None)
    await h.api.post(f"/api/v1/dev/workspaces/{d['id']}/editor/start")
    assert (await wait_editor(h.api, d["id"]))["state"] == "running"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(h.app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        cookie = "waw_session=" + h.api.c.cookies.get("waw_session")
        origin = f"http://127.0.0.1:{port}"
        url = f"ws://127.0.0.1:{port}/dev-editor/{d['id']}/ws?reconnectionToken=1"
        async with connect(url, additional_headers={"cookie": cookie, "origin": origin}, subprotocols=["vscode"]) as ws:
            assert ws.subprotocol == "vscode"
            await ws.send("hello")
            assert await ws.recv() == "HELLO"
            await ws.send(b"\x01\x02\x03")
            assert await ws.recv() == b"\x03\x02\x01"
        hdrs = json.loads((fake_env / "code_server_ws_headers.json").read_text())
        assert hdrs["origin"] == "http://localhost" and "waw_session" not in hdrs.get("cookie", "")
        with pytest.raises((InvalidStatus, ConnectionClosed)):  # no cookie: refused
            async with connect(url, additional_headers={"origin": origin}) as ws:
                await ws.recv()
        with pytest.raises((InvalidStatus, ConnectionClosed)):  # another site's page: refused
            async with connect(url, additional_headers={"cookie": cookie, "origin": "https://evil.example"}) as ws:
                await ws.recv()
    finally:
        server.should_exit = True
        await task


async def test_tasks_run_log_cancel_and_fix(running_app, dev_settings, tmp_path, fake_env):
    h = await running_app(dev_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    d = await new_branch_ws(api, src, h.ctx.workspace_id, settings={"test_command": "echo running tests; echo boom >&2; exit 3"})
    assert d["settings"]["test_command"].startswith("echo running tests")  # explicit settings beat detected defaults
    r = await api.post(f"/api/v1/dev/workspaces/{d['id']}/tasks", json={"kind": "test"})
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    for _ in range(100):
        log = (await api.get(f"/api/v1/dev/tasks/{run_id}/log")).json()
        if log["run"]["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert log["run"]["status"] == "failed" and log["run"]["exit_code"] == 3 and "running tests" in log["text"] and "boom" in log["text"]
    more = (await api.get(f"/api/v1/dev/tasks/{run_id}/log", params={"offset": log["offset"]})).json()
    assert more["text"] == ""
    # the agent gets the failed output
    fx = await api.post(f"/api/v1/dev/tasks/{run_id}/fix")
    assert fx.status_code == 201, fx.text
    await api.wait_job(fx.json()["job_id"])
    with h.ctx.db.read() as s:
        from backend.app.models import Job

        prompt = s.get(Job, fx.json()["job_id"]).prompt
    assert "Output of the failed command `echo running tests" in prompt and "boom" in prompt
    # custom long command, cancelled
    r = await api.post(f"/api/v1/dev/workspaces/{d['id']}/tasks", json={"kind": "custom", "command": "sleep 30"})
    run2 = r.json()["id"]
    c = await api.post(f"/api/v1/dev/tasks/{run2}/cancel")
    assert c.json()["status"] in ("cancelled", "running")
    for _ in range(100):
        if (await api.get(f"/api/v1/dev/tasks/{run2}/log")).json()["run"]["status"] == "cancelled":
            break
        await asyncio.sleep(0.05)
    assert (await api.get(f"/api/v1/dev/tasks/{run2}/log")).json()["run"]["status"] == "cancelled"
    runs = (await api.get(f"/api/v1/dev/workspaces/{d['id']}/tasks")).json()["runs"]
    assert [x["kind"] for x in runs] == ["custom", "test"]
    assert (await api.post(f"/api/v1/dev/workspaces/{d['id']}/tasks", json={"kind": "build", "command": ""})).status_code in (201, 400)


async def test_existing_pr_workspace(running_app, dev_settings, tmp_path, fake_env, monkeypatch):
    h = await running_app(dev_settings)
    api = h.api
    src, bare = make_source(tmp_path)
    # the PR's head lives in the "GitHub" repository as refs/pull/7/head on a branch of a fork
    other = tmp_path / "contrib"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    git(other, "config", "user.email", "c@example.com")
    git(other, "config", "user.name", "C")
    git(other, "checkout", "-q", "-b", "fix-thing")
    (other / "lib.py").write_text("def add(a, b):\n    return a + b  # fixed\n")
    git(other, "commit", "-q", "-am", "PR change")
    git(other, "push", "-q", str(bare), "HEAD:refs/pull/7/head")
    git(other, "push", "-q", str(bare), "HEAD:refs/heads/fix-thing")
    git(src, "config", f"url.{bare}.insteadOf", "https://github.com/acme/proj.git")

    from backend.app.development import service

    async def meta(repo, number):
        assert (repo, number) == ("acme/proj", 7)
        return {"headRefName": "fix-thing", "head_repo": "acme/proj", "baseRefName": "main", "isCrossRepository": False,
                "maintainerCanModify": True, "title": "Fix the thing"}
    monkeypatch.setattr(service, "fetch_pr_meta", meta)
    assert (await api.post("/api/v1/dev/workspaces", json={"source_repo": str(src), "mode": "pr", "github_repo": "acme/proj"})).status_code == 400
    r = await api.post("/api/v1/dev/workspaces", json={"source_repo": str(src), "mode": "pr", "github_repo": "acme/proj", "pr_number": 7,
                                                       "wiki_workspace_id": h.ctx.workspace_id})
    d = await wait_ready(api, r.json()["id"])
    assert d["status"] == "ready", d
    wt = Path(d["worktree_path"])
    assert d["name"] == "#7 Fix the thing" and d["branch"] == "waw/pr-7" and d["base_ref"] == "refs/waw/base/main"
    assert d["push_branch"] == "fix-thing" and d["push_remote"] == "https://github.com/acme/proj.git"
    assert "# fixed" in (wt / "lib.py").read_text()
    st = (await api.get(f"/api/v1/dev/workspaces/{d['id']}/status")).json()
    assert st["commits_on_branch"] == 1 and st["unpushed"] == 0  # the PR's commit is already on GitHub
    # PR-specific agent instructions
    r = await api.post(f"/api/v1/dev/workspaces/{d['id']}/ask", json={"text": "go", "kind": "review"})
    await api.wait_job(r.json()["job_id"])
    with h.ctx.db.read() as s:
        from backend.app.models import Job

        prompt = s.get(Job, r.json()["job_id"]).prompt
    assert "pull request acme/proj#7" in prompt and "pushes go to branch `fix-thing`" in prompt
    assert "gh api repos/acme/proj/pulls/7/comments" in prompt
    # pushing updates the PR branch (no force)
    (wt / "lib.py").write_text("def add(a, b):\n    return b + a\n")
    assert (await api.post(f"/api/v1/dev/workspaces/{d['id']}/commit", json={"message": "Address review"})).status_code == 200
    assert (await api.post(f"/api/v1/dev/workspaces/{d['id']}/push")).status_code == 200
    assert git(bare, "log", "-1", "--format=%s", "fix-thing") == "Address review"
    assert (await api.post(f"/api/v1/dev/workspaces/{d['id']}/pull-request", json={"title": "x"})).json()["error"]["code"] == "PR_EXISTS"


async def test_validation_and_failures(running_app, dev_settings, tmp_path):
    h = await running_app(dev_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    bad = [{"source_repo": str(src), "mode": "branch", "branch": "-x"},
           {"source_repo": str(src), "mode": "branch", "branch": "a..b"},
           {"source_repo": str(src), "mode": "nope", "branch": "a"},
           {"source_repo": str(src), "mode": "branch", "branch": "a", "host": "mystery"},
           {"source_repo": str(src), "mode": "branch", "branch": "a", "base": "--upload-pack=x"},
           {"source_repo": "-x", "mode": "branch", "branch": "a"}]
    for b in bad:
        assert (await api.post("/api/v1/dev/workspaces", json=b)).status_code == 400, b
    r = await api.post("/api/v1/dev/workspaces", json={"source_repo": str(tmp_path), "mode": "branch", "branch": "a"})
    d = await wait_ready(api, r.json()["id"])
    assert d["status"] == "failed" and "not a Git checkout" in d["error"]
    assert (await api.get(f"/api/v1/dev/workspaces/{d['id']}/status")).status_code == 409
    r = await api.post("/api/v1/dev/workspaces", json={"source_repo": "/x", "mode": "branch", "branch": "a", "host": "down"})
    d = await wait_ready(api, r.json()["id"])
    assert d["status"] == "failed" and "ssh to unreachable.example failed" in d["error"]
    # no editor executable
    h.ctx.settings.development.code_server = str(tmp_path / "nope")
    ok = await new_branch_ws(api, src, None, branch="b2")
    r = await api.post(f"/api/v1/dev/workspaces/{ok['id']}/editor/start")
    assert r.status_code == 409 and r.json()["error"]["code"] == "EDITOR_UNAVAILABLE"
    assert (await api.get("/api/v1/dev/info")).json()["editor"]["available"] is False


async def test_ssh_host_workspace(running_app, dev_settings, tmp_path, fake_env):
    """An SSH host (fake ssh runs the commands on this machine): worktree, wiki snapshot, editor through -L, agent via ssh."""
    h = await running_app(dev_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    (h.ctx.documents.root / "docs" / "design.md").write_text("# Design\n\nadd must be commutative\n")
    d = await new_branch_ws(api, src, h.ctx.workspace_id, host="box")
    remote = tmp_path / "remote"
    wt = Path(d["worktree_path"])
    assert str(wt).startswith(str(remote / "worktrees")) and wt.is_dir()
    snap = remote / "wiki" / h.ctx.workspace_id
    assert not d["error"], d["error"]
    assert (snap / "docs" / "design.md").read_text().endswith("commutative\n") and not (snap / ".git").exists()
    assert not os.access(snap / "docs" / "design.md", os.W_OK)  # read-only snapshot
    calls = [json.loads(x) for x in (fake_env / "ssh_calls.log").read_text().splitlines()]
    assert all(c[:4] == ["-o", "BatchMode=yes", "-o", "ServerAliveInterval=30"] and "me@box" in c for c in calls)
    # editor: runs "remotely", reached through the -L socket forward; -R gives the extension a way back
    await api.post(f"/api/v1/dev/workspaces/{d['id']}/editor/start")
    ed = await wait_editor(api, d["id"])
    assert ed["state"] == "running", ed
    env = json.loads((fake_env / "code_server_env.json").read_text())
    assert env["WAW_DEV_SOCKET"].startswith(str(remote / "run")) and "WAW_DEV_URL" not in env and env["cwd"] == str(wt)
    serve_call = [json.loads(x) for x in (fake_env / "ssh_calls.log").read_text().splitlines()][-1]
    assert "-L" in serve_call and "-R" in serve_call and "-tt" in serve_call
    wsf = json.loads(Path(env["workspace"]).read_text())
    assert wsf["folders"][1]["path"] == str(snap)
    page = await api.get(f"/dev-editor/{d['id']}/")
    assert page.status_code == 200 and "fake-code-server" in page.text
    # agent turn: the provider CLI runs through ssh in the remote worktree, with the snapshot as wiki
    r = await api.post(f"/api/v1/dev/workspaces/{d['id']}/ask", json={"text": "[[write:remote.txt:::made remotely]]", "kind": "implement"})
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "completed", ev[-1]
    assert (wt / "remote.txt").read_text() == "made remotely"
    with h.ctx.db.read() as s:
        from backend.app.models import Job

        prompt = s.get(Job, r.json()["job_id"]).prompt
    assert f"REFERENCE WIKI: the whole folder {snap}" in prompt and "on box" in prompt
    last_ssh = [json.loads(x) for x in (fake_env / "ssh_calls.log").read_text().splitlines()]
    assert any(str(FAKES / "fake-opencode") in " ".join(c) and f"cd {wt}" in " ".join(c) for c in last_ssh)
    # a task runs there too
    r = await api.post(f"/api/v1/dev/workspaces/{d['id']}/tasks", json={"kind": "custom", "command": "pwd"})
    run_id = r.json()["id"]
    for _ in range(100):
        log = (await api.get(f"/api/v1/dev/tasks/{run_id}/log")).json()
        if log["run"]["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert log["run"]["status"] == "passed" and str(wt) in log["text"]
    await api.post(f"/api/v1/dev/workspaces/{d['id']}/editor/stop")


async def test_language_extensions_install_and_completion(running_app, dev_settings, tmp_path, fake_env, monkeypatch):
    h = await running_app(dev_settings)
    api = h.api
    r = await api.post("/api/v1/dev/extensions", json={"host": "local", "extensions": ["llvm-vs-code-extensions.vscode-clangd", "x.missing"]})
    assert r.status_code == 202
    for _ in range(100):
        st = (await api.get("/api/v1/dev/extensions", params={"host": "local"})).json()
        if st["state"] != "running":
            break
        await asyncio.sleep(0.05)
    assert st["state"] == "failed" and st["failed"] == ["x.missing"] and "successfully installed" in st["log"]
    # AI inline completion against an OpenAI-compatible endpoint
    from backend.app.development import completion

    seen = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            import httpx

            seen.update(url=url, body=json, headers=headers)
            return httpx.Response(200, json={"choices": [{"message": {"content": "```python\n    return a + b\n```"}}]})

    monkeypatch.setattr(completion.httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("TEST_COMPLETION_KEY", "sekrit")
    cfg = DevCompletion(base_url="https://llm.example/v1", model="m1", api_key_env="TEST_COMPLETION_KEY")
    out = await completion.complete(cfg, language="python", path="lib.py", prefix="def add(a, b):\n    ", suffix="\n")
    assert out["text"] == "return a + b"  # fences and the repeated line start removed
    assert seen["url"] == "https://llm.example/v1/chat/completions" and seen["headers"]["Authorization"] == "Bearer sekrit"
    assert seen["body"]["model"] == "m1" and "<CURSOR>" in seen["body"]["messages"][1]["content"]
    assert (await completion.complete(cfg, language="python", path="lib.py", prefix="def add(a, b):\n    ", suffix="\n"))["cached"]
    assert completion.info(cfg)["enabled"] and not completion.info(DevCompletion())["enabled"]


async def test_web_editor_files(running_app, dev_settings, tmp_path, fake_env):
    h = await running_app(dev_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    (src / "pkg").mkdir()
    (src / "pkg" / "util.py").write_text("X = 1\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "pkg")
    d = await new_branch_ws(api, src, h.ctx.workspace_id)
    assert d["settings"]["editor"] == "web"  # the web editor is the default
    base = f"/api/v1/dev/workspaces/{d['id']}"
    wt = Path(d["worktree_path"])
    roots = (await api.get(f"{base}/roots")).json()["roots"]
    assert roots["wt"] == {"path": str(wt), "name": "feature/x", "readonly": False} and roots["wiki"]["readonly"] is True
    items = (await api.get(f"{base}/fs/list", params={"root": "wt", "path": ""})).json()["items"]
    assert [i["name"] for i in items][:1] == ["pkg"] and ".git" not in [i["name"] for i in items]  # folders first, no .git
    assert "pkg/util.py" in (await api.get(f"{base}/fs/all")).json()["files"]
    f = (await api.get(f"{base}/fs/file", params={"root": "wt", "path": "lib.py"})).json()
    assert f["text"].startswith("def add") and not f["readonly"] and len(f["sha256"]) == 64
    # conditional save
    r = await api.put(f"{base}/fs/file", json={"path": "lib.py", "text": "def add(a, b):\n    return b + a\n", "base_sha256": f["sha256"]})
    assert r.status_code == 200, r.text
    r2 = await api.put(f"{base}/fs/file", json={"path": "lib.py", "text": "stale", "base_sha256": f["sha256"]})
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "FILE_CHANGED" and r2.json()["error"]["details"]["current_sha256"] == r.json()["sha256"]
    st = (await api.post(f"{base}/fs/stat", json={"root": "wt", "paths": ["lib.py", "gone.py"]})).json()["sha256"]
    assert st == {"lib.py": r.json()["sha256"], "gone.py": None}
    # create / mkdir / rename / delete; the wiki is read-only; no escaping
    assert (await api.put(f"{base}/fs/file", json={"path": "new/a.cpp", "text": "int x;\n", "create": True})).status_code == 200
    assert (await api.put(f"{base}/fs/file", json={"path": "new/a.cpp", "text": "", "create": True})).json()["error"]["code"] == "ALREADY_EXISTS"
    assert (await api.post(f"{base}/fs/rename", json={"path": "new/a.cpp", "new_path": "src/a.cpp"})).status_code == 200
    assert (wt / "src" / "a.cpp").read_text() == "int x;\n"
    assert (await api.post(f"{base}/fs/delete", json={"path": "src"})).status_code == 200 and not (wt / "src").exists()
    for bad in ("../x", ".git/config", "/etc/passwd"):
        assert (await api.put(f"{base}/fs/file", json={"path": bad, "text": "x", "create": True})).status_code == 400, bad
    (wt / "out").symlink_to(tmp_path)
    assert (await api.put(f"{base}/fs/file", json={"path": "out/evil", "text": "x", "create": True})).json()["error"]["code"] == "PATH_ESCAPES"
    wiki_doc = (await api.get(f"{base}/fs/file", params={"root": "wiki", "path": "docs/guide.md"})).json()
    assert wiki_doc["readonly"] and "TorchDynamo" in wiki_doc["text"]
    # search in the worktree and the wiki
    hits = (await api.get(f"{base}/fs/search", params={"q": "return b", "root": "wt"})).json()["hits"]
    assert hits == [{"path": "lib.py", "line": 2, "text": "    return b + a"}]
    hits = (await api.get(f"{base}/fs/search", params={"q": "torchdynamo", "root": "wiki"})).json()["hits"]
    assert hits and hits[0]["path"] == "docs/guide.md"
    assert (await api.get(f"{base}/fs/search", params={"q": "X =", "glob": "*.py"})).json()["hits"][0]["path"] == "pkg/util.py"
    # editor switch
    r = await api.patch(base, json={"settings": {"editor": "vscode"}})
    assert r.json()["settings"]["editor"] == "vscode"
    assert (await api.patch(base, json={"settings": {"editor": "emacs"}})).status_code == 400


async def test_web_editor_files_on_ssh_host(running_app, dev_settings, tmp_path, fake_env):
    h = await running_app(dev_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    d = await new_branch_ws(api, src, h.ctx.workspace_id, host="box")
    base = f"/api/v1/dev/workspaces/{d['id']}"
    names = [i["name"] for i in (await api.get(f"{base}/fs/list", params={"root": "wt"})).json()["items"]]
    assert "lib.py" in names and ".git" not in names
    f = (await api.get(f"{base}/fs/file", params={"path": "lib.py"})).json()
    r = await api.put(f"{base}/fs/file", json={"path": "lib.py", "text": "y = 'ü'\n", "base_sha256": f["sha256"]})
    assert r.status_code == 200, r.text
    assert (Path(d["worktree_path"]) / "lib.py").read_text() == "y = 'ü'\n"
    assert (await api.put(f"{base}/fs/file", json={"path": "lib.py", "text": "z", "base_sha256": f["sha256"]})).status_code == 409
    st = (await api.post(f"{base}/fs/stat", json={"paths": ["lib.py", "nope"]})).json()["sha256"]
    assert st["lib.py"] == r.json()["sha256"] and st["nope"] is None
    assert (await api.get(f"{base}/fs/file", params={"root": "wiki", "path": "docs/guide.md"})).json()["readonly"]
    assert (await api.get(f"{base}/fs/search", params={"q": "y ="})).json()["hits"][0]["path"] == "lib.py"


async def test_language_server_bridge(running_app, dev_settings, tmp_path, fake_env):
    import socket

    import uvicorn
    from websockets.asyncio.client import connect

    h = await running_app(dev_settings)
    src, _ = make_source(tmp_path)
    d = await new_branch_ws(h.api, src, None, settings={"python_lsp": str(FAKES / "fake-lsp"), "clangd": str(tmp_path / "no-clangd")})
    info = (await h.api.get(f"/api/v1/dev/workspaces/{d['id']}/lsp-info")).json()
    assert info["python"]["available"] and info["python"]["command"].endswith("fake-lsp")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(h.app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        cookie = "waw_session=" + h.api.c.cookies.get("waw_session")
        csrf = ["waw-csrf." + h.api.csrf]
        origin = f"http://127.0.0.1:{port}"
        url = f"ws://127.0.0.1:{port}/api/v1/dev/workspaces/{d['id']}/lsp/python"
        async with connect(url, additional_headers={"cookie": cookie, "origin": origin}, subprotocols=csrf) as ws:
            assert json.loads(await ws.recv())["waw"] == "started"
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"rootUri": "file://" + d["worktree_path"]}}))
            assert json.loads(await ws.recv())["result"]["capabilities"]["hoverProvider"] is True
            uri = "file://" + d["worktree_path"] + "/lib.py"
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": "x"}}}))
            diag = json.loads(await ws.recv())
            assert diag["method"] == "textDocument/publishDiagnostics" and diag["params"]["diagnostics"][0]["message"] == "fake error"
            big = "x" * 200_000  # large messages pass whole
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "textDocument/hover", "params": {"pad": big}}))
            assert json.loads(await ws.recv())["result"]["contents"]["value"] == "**fake hover**"
        log = (fake_env / "lsp_methods.log").read_text()
        assert f"initialize {d['worktree_path']}" in log  # runs in the worktree
        # no clangd: the client is told why
        async with connect(url.replace("/python", "/cpp"), additional_headers={"cookie": cookie, "origin": origin}, subprotocols=csrf) as ws:
            m = json.loads(await ws.recv())
            assert m["waw"] == "unavailable"
        from websockets.exceptions import ConnectionClosed, InvalidStatus

        with pytest.raises((InvalidStatus, ConnectionClosed)):  # the CSRF subprotocol is required
            async with connect(url, additional_headers={"cookie": cookie, "origin": origin}) as ws:
                await ws.recv()
    finally:
        server.should_exit = True
        await task


def _skill(folder: Path, sub: str, name: str, desc: str) -> None:
    d = folder / sub / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n")


async def test_skills_of_connected_folders_and_projects(running_app, dev_settings, tmp_path, fake_env, workspace):
    src, _ = make_source(tmp_path)
    _skill(workspace, ".opencode/skills", "wiki-style", "How pages are written.")
    _skill(src, ".claude/skills", "repo-build", "How to build the repo.")
    other = tmp_path / "other-wiki"
    other.mkdir()
    _skill(other, ".agents/skills", "other-folder-skill", "From another open folder.")
    dev_settings.workspaces = [{"root": str(other), "name": "other"}]
    h = await running_app(dev_settings)
    api = h.api
    d = await new_branch_ws(api, src, h.ctx.workspace_id)
    sk = (await api.get(f"/api/v1/dev/workspaces/{d['id']}/skills")).json()
    names = {s["name"] for s in sk["skills"]}
    assert {"wiki-style", "repo-build", "other-folder-skill"} <= names  # worktree (from the checkout), wiki, other folder
    r = await api.post(f"/api/v1/dev/workspaces/{d['id']}/ask", json={"text": "go", "kind": "ask"})
    await api.wait_job(r.json()["job_id"])
    prompt = (fake_env / "opencode_last_prompt").read_text()  # what the agent received
    assert "SKILLS of the current folder and of every folder and project" in prompt and "repo-build: How to build the repo." in prompt
    assert "other-folder-skill" in prompt and "wiki-style" in prompt
    cfg = json.loads((fake_env / "opencode_last_config_content").read_text())
    paths = cfg["skills"]["paths"]
    assert str(workspace / ".opencode/skills") in paths and str(other / ".agents/skills") in paths
    assert str(src / ".claude/skills") in paths  # the source checkout (the skill is not committed, so not in the worktree)
    # wiki discussions get them too (all open folders)
    doc = await api.open_doc()
    from conftest import anchor_for

    a = anchor_for(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "q"})
    await api.wait_job(r.json()["job_id"])
    prompt = (fake_env / "opencode_last_prompt").read_text()
    assert "other-folder-skill" in prompt and "wiki-style" in prompt
    # an existing OPENCODE_CONFIG_CONTENT is merged, not replaced
    from backend.app.skills import opencode_env

    env = opencode_env([Path("/x/skills")], {"OPENCODE_CONFIG_CONTENT": json.dumps({"model": "a/b", "skills": {"paths": ["/y"]}})})
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == {"model": "a/b", "skills": {"paths": ["/y", "/x/skills"]}}


async def test_discussions_on_code_and_files_in_context(running_app, dev_settings, tmp_path, fake_env):
    h = await running_app(dev_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    d = await new_branch_ws(api, src, h.ctx.workspace_id)
    base = f"/api/v1/dev/workspaces/{d['id']}"
    anchor = {"root": "wt", "file": "lib.py", "start_line": 1, "end_line": 2, "quote": "def add(a, b):\n    return a + b", "language": "python"}
    r = await api.post(f"{base}/ask", json={"text": "Is this commutative?", "kind": "ask", "anchor": anchor,
                                            "context": {"files": [{"root": "wt", "path": "pyproject.toml"}, {"root": "wiki", "path": "docs/guide.md"},
                                                                  {"root": "wt", "path": "missing.py"}]}})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    tid = r.json()["thread_id"]
    conv = (await api.get(f"{base}/conversation")).json()["threads"]
    assert conv[0]["target"]["kind"] == "code" and conv[0]["target"]["file"] == "lib.py" and conv[0]["target"]["start_line"] == 1
    from backend.app.models import Job

    with h.ctx.db.read() as s:
        p1 = s.get(Job, r.json()["job_id"]).prompt
    assert "`lib.py` lines 1–2 (python)" in p1 and "return a + b" in p1
    assert "`pyproject.toml`:\n```\n[project]" in p1 and "TorchDynamo" in p1  # file contents, wiki by absolute path
    assert f"`{h.ctx.documents.root}/docs/guide.md`" in p1 and "`missing.py` (missing.py not found)" in p1
    # a follow-up in that discussion is still about those lines
    r2 = await api.post(f"{base}/ask", json={"text": "and with floats?", "kind": "ask", "thread_id": tid})
    await api.wait_job(r2.json()["job_id"])
    with h.ctx.db.read() as s:
        p2 = s.get(Job, r2.json()["job_id"]).prompt
    assert "`lib.py` lines 1–2" in p2 and "and with floats?" in p2


@pytest.mark.skipif(__import__("shutil").which("tmux") is None, reason="tmux not installed")
async def test_dev_terminals(running_app, dev_settings, tmp_path, fake_env):
    h = await running_app(dev_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    d = await new_branch_ws(api, src, None)
    base = f"/api/v1/dev/workspaces/{d['id']}"
    assert (await api.get(f"{base}/terminals")).json()["terminals"] == []
    t = (await api.post(f"{base}/terminals")).json()
    assert t["cwd"] == d["worktree_path"] and t["state"] == "live"
    assert [x["id"] for x in (await api.get(f"{base}/terminals")).json()["terminals"]] == [t["id"]]
    # SSH host: the tmux session runs an ssh login that starts in the remote worktree
    r = await new_branch_ws(api, src, None, host="box", branch="b-remote")
    tr = (await api.post(f"/api/v1/dev/workspaces/{r['id']}/terminals")).json()
    assert tr["label"].endswith("(box)")
    for _ in range(50):
        calls = (fake_env / "ssh_calls.log").read_text()
        if f"cd {r['worktree_path']}" in calls and "-tt" in calls.splitlines()[-1]:
            break
        await asyncio.sleep(0.1)
    assert f"cd {r['worktree_path']}" in (fake_env / "ssh_calls.log").read_text()
    for x in (t, tr):
        await api.delete(f"/api/v1/terminals/{x['id']}")


async def test_skills_for_every_chat_and_discussion(running_app, dev_settings, tmp_path, fake_env, workspace):
    """Wiki discussions, folder chats, PR development (local, SSH, both providers) get the skills of the current
    folder and every connected folder — also those at a connected folder's project root."""
    _skill(workspace, ".opencode/skills", "wiki-style", "How pages are written.")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _skill(repo, ".claude/skills", "repo-root-skill", "At the repository root.")
    sub_wiki = repo / "docs-wiki"
    sub_wiki.mkdir()
    (sub_wiki / "x.md").write_text("# X\n")
    dev_settings.workspaces = [{"root": str(sub_wiki), "name": "sub"}]
    h = await running_app(dev_settings)
    api = h.api
    sk = (await api.get("/api/v1/agent-skills")).json()
    assert {"wiki-style", "repo-root-skill"} <= {s["name"] for s in sk["skills"]}
    # a folder chat of the other folder: its project root's skill and the current folder's
    sub_id = next(w["id"] for w in h.ctx.workspaces.list() if w["root"] == str(sub_wiki))
    chat = (await api.post("/api/v1/chats", json={"title": "c"}, headers={"x-workspace": sub_id})).json()
    api.ws = sub_id
    r = await api.post(f"/api/v1/documents/{chat['document_id']}/threads", json={"version_sha256": "x", "question": "q", "whole_document": True})
    if r.status_code == 409:  # needs the current hash
        sha = r.json()["error"]["details"]["current_sha256"]
        r = await api.post(f"/api/v1/documents/{chat['document_id']}/threads", json={"version_sha256": sha, "question": "q", "whole_document": True})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    api.ws = None
    p = (fake_env / "opencode_last_prompt").read_text()
    assert "repo-root-skill" in p and "wiki-style" in p
    paths = json.loads((fake_env / "opencode_last_config_content").read_text())["skills"]["paths"]
    assert str(repo / ".claude/skills") in paths and str(workspace / ".opencode/skills") in paths
    # Codex: no skills config — the same list in its prompt (the way Codex lists skills itself)
    src, _ = make_source(tmp_path)
    d = await new_branch_ws(api, src, h.ctx.workspace_id)
    s = (await api.post("/api/v1/agent-sessions", json={"provider": "codex"})).json()
    r = await api.post(f"/api/v1/dev/workspaces/{d['id']}/ask", json={"text": "go", "kind": "ask", "session_id": s["id"]})
    await api.wait_job(r.json()["job_id"])
    assert "repo-root-skill" in (fake_env / "codex_last_prompt").read_text()
    # SSH host: every connected folder's skills are copied there read-only and loaded from the copies
    rd = await new_branch_ws(api, src, h.ctx.workspace_id, host="box", branch="remote-sk")
    r = await api.post(f"/api/v1/dev/workspaces/{rd['id']}/ask", json={"text": "go", "kind": "ask"})
    await api.wait_job(r.json()["job_id"])
    copies = tmp_path / "remote" / "skills"
    names = {p.parent.name for p in copies.rglob("SKILL.md")}
    assert {"wiki-style", "repo-root-skill"} <= names
    p = (fake_env / "opencode_last_prompt").read_text()
    assert str(copies) in p and "repo-root-skill" in p
    paths = json.loads((fake_env / "opencode_last_config_content").read_text())["skills"]["paths"]
    assert paths and all(x.startswith(str(copies)) for x in paths)
    sk = (await api.get(f"/api/v1/dev/workspaces/{rd['id']}/skills")).json()
    assert "repo-root-skill" in {x["name"] for x in sk["skills"]} and "box" in sk["note"]


async def test_discussion_about_a_whole_file(running_app, dev_settings, tmp_path, fake_env):
    h = await running_app(dev_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    d = await new_branch_ws(api, src, None)
    base = f"/api/v1/dev/workspaces/{d['id']}"
    r = await api.post(f"{base}/ask", json={"text": "Review this file", "kind": "ask", "anchor": {"root": "wt", "file": "lib.py"}})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    t = (await api.get(f"{base}/conversation")).json()["threads"][0]
    assert t["target"]["kind"] == "code" and t["target"]["file"] == "lib.py" and t["target"]["start_line"] is None
    p1 = (fake_env / "opencode_last_prompt").read_text()
    assert "about the whole file `lib.py`" in p1 and "`lib.py`:\n```\ndef add" in p1  # the content goes with it
    r2 = await api.post(f"{base}/ask", json={"text": "and now?", "kind": "ask", "thread_id": t["id"]})
    await api.wait_job(r2.json()["job_id"])
    p2 = (fake_env / "opencode_last_prompt").read_text()
    assert "about the whole file `lib.py`" in p2 and "and now?" in p2


def _bazel_files(src: Path) -> None:
    (src / "tools").mkdir(exist_ok=True)
    (src / "tools" / "defs.bzl").write_text('def my_macro(name, srcs = []):\n    native.filegroup(name = name, srcs = srcs)\n\nMY_RULE = rule(implementation = _impl)\n')
    (src / "pkg").mkdir(exist_ok=True)
    (src / "pkg" / "BUILD.bazel").write_text('load("//tools:defs.bzl", "my_macro")\n\nmy_macro(\n    name = "lib",\n    srcs = ["lib.py"],\n)\n\ncc_library(\n    name = "core",\n    deps = [":lib", "//tools:defs.bzl"],\n)\n')
    (src / "pkg" / "lib.py").write_text("x = 1\n")
    (src / "ci.yaml").write_text("jobs:\n  build:\n    runs-on: ubuntu\n  test:\n    needs: build\n")
    (src / "cmake").mkdir(exist_ok=True)
    (src / "cmake" / "util.cmake").write_text("function(add_my_target name)\nendfunction()\n")
    (src / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.16)\nproject(p CXX)\ninclude(cmake/util.cmake)\nadd_my_target(foo)\n")
    (src / "doc.md").write_text("# Title\n\n![logo](img/logo.png)\n")
    (src / "img").mkdir(exist_ok=True)
    (src / "img" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "bazel")


async def test_navigation_without_language_server(running_app, dev_settings, tmp_path, fake_env):
    h = await running_app(dev_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    _bazel_files(src)
    d = await new_branch_ws(api, src, None)
    base = f"/api/v1/dev/workspaces/{d['id']}/nav"

    async def defn(path, word, line_text, col, language):
        r = await api.post(f"{base}/definition", json={"path": path, "word": word, "line_text": line_text, "col": col, "language": language})
        assert r.status_code == 200, r.text
        return [(x["path"], x["line"]) for x in r.json()["locations"]]

    # Bazel labels in strings: //pkg:file.bzl, :target, //pkg:target
    assert await defn("pkg/BUILD.bazel", "tools", 'load("//tools:defs.bzl", "my_macro")', 9, "starlark") == [("tools/defs.bzl", 1)]
    assert await defn("pkg/BUILD.bazel", "lib", '    deps = [":lib", "//tools:defs.bzl"],', 15, "starlark") == [("pkg/BUILD.bazel", 4)]
    assert await defn("BUILD", "core", '"//pkg:core"', 8, "starlark") == [("pkg/BUILD.bazel", 9)]
    # a relative path in a string (from the file's folder)
    assert await defn("pkg/BUILD.bazel", "lib", '    srcs = ["lib.py"],', 15, "starlark") == [("pkg/lib.py", 1)]
    assert await defn("CMakeLists.txt", "cmake", "include(cmake/util.cmake)", 12, "cmake") == []  # not quoted: by symbol below
    # symbols: a Starlark macro used in BUILD, a CMake function, a YAML key
    assert ("tools/defs.bzl", 1) in await defn("pkg/BUILD.bazel", "my_macro", "my_macro(", 3, "starlark")
    assert ("tools/defs.bzl", 4) in await defn("x.bzl", "MY_RULE", "MY_RULE", 2, "starlark")
    assert await defn("CMakeLists.txt", "add_my_target", "add_my_target(foo)", 3, "cmake") == [("cmake/util.cmake", 1)]
    assert ("ci.yaml", 2) in await defn("ci.yaml", "build", "    needs: build", 13, "yaml")
    refs = (await api.post(f"{base}/references", json={"path": "ci.yaml", "word": "build", "language": "yaml"})).json()["locations"]
    assert {(x["path"], x["line"]) for x in refs} >= {("ci.yaml", 2), ("ci.yaml", 5)}
    assert (await api.post(f"{base}/definition", json={"path": "a", "word": "--evil", "language": "x"})).json()["locations"] == []
    # images of rendered Markdown
    raw = await api.get(f"/api/v1/dev/workspaces/{d['id']}/fs/raw", params={"path": "img/logo.png"})
    assert raw.status_code == 200 and raw.headers["content-type"] == "image/png" and raw.content.startswith(b"\x89PNG")
    other = await api.get(f"/api/v1/dev/workspaces/{d['id']}/fs/raw", params={"path": "lib.py"})
    assert other.headers["content-disposition"] == "attachment"
    # language servers: known ones are reported with a hint when missing
    info = (await api.get(f"/api/v1/dev/workspaces/{d['id']}/lsp-info")).json()
    assert info["yaml"]["available"] is False and "basic navigation" in info["yaml"]["hint"] and "starlark" in info


async def test_each_file_has_its_own_agent_session(running_app, dev_settings, tmp_path, fake_env):
    h = await running_app(dev_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    d = await new_branch_ws(api, src, None)
    base = f"/api/v1/dev/workspaces/{d['id']}"

    async def ask(file, text, thread=None):
        body = {"text": text, "kind": "ask"} | ({"thread_id": thread} if thread else {"anchor": {"root": "wt", "file": file}})
        r = await api.post(f"{base}/ask", json=body)
        assert r.status_code == 201, r.text
        await api.wait_job(r.json()["job_id"])
        return r.json()

    a1 = await ask("lib.py", "one")
    a2 = await ask("lib.py", "two")  # a second discussion of the same file: the same session
    b1 = await ask("main.cpp", "three")  # another file: its own session
    assert a1["session_id"] == a2["session_id"] != b1["session_id"]
    conv = (await api.get(f"{base}/conversation")).json()
    assert conv["file_sessions"] == {"wt:lib.py": a1["session_id"], "wt:main.cpp": b1["session_id"]}
    # choose another session for a file, or a new one
    r = await api.put(f"{base}/file-session", json={"root": "wt", "file": "lib.py", "session_id": b1["session_id"]})
    assert r.json()["session"]["id"] == b1["session_id"]
    assert (await ask("lib.py", "four"))["session_id"] == b1["session_id"]
    r = await api.put(f"{base}/file-session", json={"root": "wt", "file": "lib.py", "session_id": "new"})
    new_sid = r.json()["session"]["id"]
    assert new_sid not in (a1["session_id"], b1["session_id"])
    assert (await ask("lib.py", "five"))["session_id"] == new_sid
    # continuing an older discussion keeps that discussion's session
    assert (await ask("lib.py", "six", thread=a1["thread_id"]))["session_id"] == a1["session_id"]
    assert (await api.get(f"{base}/conversation")).json()["file_sessions"]["wt:lib.py"] == new_sid  # the chosen one stays


async def test_chat_about_a_folder_or_file_gets_its_project_and_connected_skills(running_app, dev_settings, tmp_path, fake_env, workspace):
    # wiki: a chat about a sub-folder that has its own skills; a reference folder (connected) with skills
    sub = workspace / "docs"
    _skill(sub, ".claude/skills", "docs-folder-skill", "Only in the docs folder.")
    ref = tmp_path / "ref"
    ref.mkdir()
    _skill(ref, ".agents/skills", "reference-skill", "From a connected reference folder.")
    h = await running_app(dev_settings)
    api = h.api
    assert (await api.put(f"/api/v1/agent-context?scope=ws:{h.ctx.workspace_id}", json={"dirs": [str(ref)]})).status_code == 200
    c = (await api.post("/api/v1/chats", json={"title": "docs", "about": "docs"})).json()
    assert c["about"] == {"path": "docs", "kind": "folder"}
    assert (await api.post("/api/v1/chats", json={"title": "x", "about": "nope"})).status_code == 404
    d = await api.open_doc(c["path"])
    r = await api.post(f"/api/v1/documents/{d['document_id']}/threads", json={"version_sha256": d["sha256"], "question": "q", "whole_document": True})
    await api.wait_job(r.json()["job_id"])
    p = (fake_env / "opencode_last_prompt").read_text()
    assert "This conversation is about the folder `docs`" in p
    assert "docs-folder-skill" in p and "reference-skill" in p
    paths = json.loads((fake_env / "opencode_last_config_content").read_text())["skills"]["paths"]
    assert paths[0] == str(sub / ".claude/skills") and str(ref / ".agents/skills") in paths  # the folder's own first
    assert [x["about"] for x in (await api.get("/api/v1/chats")).json()["chats"] if x["path"] == c["path"]] == [{"path": "docs", "kind": "folder"}]
    # a discussion of a document gets its folder's skills
    doc = await api.open_doc("docs/guide.md")
    from conftest import anchor_for

    a = anchor_for(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "q"})
    await api.wait_job(r.json()["job_id"])
    assert "docs-folder-skill" in (fake_env / "opencode_last_prompt").read_text()
    # PR Development: a discussion about a folder of the worktree
    src, _ = make_source(tmp_path)
    (src / "pkg").mkdir()
    _skill(src / "pkg", ".claude/skills", "pkg-skill", "In the pkg folder.")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "pkg skill")
    dv = await new_branch_ws(api, src, None)
    r = await api.post(f"/api/v1/dev/workspaces/{dv['id']}/ask", json={"text": "what is here?", "kind": "explain",
                                                                         "anchor": {"root": "wt", "file": "pkg", "folder": True}})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    p = (fake_env / "opencode_last_prompt").read_text()
    assert "about the folder `pkg` of the worktree" in p and "pkg-skill" in p and "reference-skill" in p
    t = (await api.get(f"/api/v1/dev/workspaces/{dv['id']}/conversation")).json()["threads"][-1]
    assert t["target"]["folder"] is True


async def test_chat_about_any_file_type(running_app, dev_settings, tmp_path, fake_env):
    import base64

    h = await running_app(dev_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    (src / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0fake")
    (src / "spec.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (src / "model.onnx").write_bytes(b"\0\1\2")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "files")
    d = await new_branch_ws(api, src, None)
    base = f"/api/v1/dev/workspaces/{d['id']}"
    wt = Path(d["worktree_path"])
    # an image: the agent gets the picture itself
    r = await api.post(f"{base}/ask", json={"text": "What is this?", "kind": "ask", "anchor": {"root": "wt", "file": "logo.png", "quote": ""}})
    await api.wait_job(r.json()["job_id"])
    assert str(wt / "logo.png") in (fake_env / "opencode_last_files").read_text()
    assert "The file `logo.png` is an image. The picture is attached" in (fake_env / "opencode_last_prompt").read_text()
    # a PDF page: its picture and text go with the question
    png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\npage").decode()
    r = await api.post(f"{base}/ask", json={"text": "Explain this page", "kind": "explain", "image": png,
                                            "anchor": {"root": "wt", "file": "spec.pdf", "quote": "Section 3 timing"},
                                            "context": {"page": "page 3", "page_text": "Section 3 timing"}})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    p = (fake_env / "opencode_last_prompt").read_text()
    assert "is a PDF document" in p and "A picture of page 3 is attached" in p and "Section 3 timing" in p
    assert "/attachments/dev-" in (fake_env / "opencode_last_files").read_text()
    raw = await api.get(f"{base}/fs/raw", params={"path": "spec.pdf"})
    assert raw.headers["content-type"] == "application/pdf" and "content-disposition" not in raw.headers
    # any other file
    r = await api.post(f"{base}/ask", json={"text": "What is in it?", "kind": "ask", "anchor": {"root": "wt", "file": "model.onnx", "quote": ""}})
    await api.wait_job(r.json()["job_id"])
    assert "The file `model.onnx` is an ONNX model" in (fake_env / "opencode_last_prompt").read_text()
    assert (await api.post(f"{base}/ask", json={"text": "x", "image": "data:text/html,evil"})).status_code == 400


async def test_model_choice_per_discussion(running_app, dev_settings, tmp_path, fake_env):
    h = await running_app(dev_settings)
    api = h.api
    # wiki: the default session; then another model for a new discussion (a new session with it), default untouched
    doc = await api.open_doc()
    from conftest import anchor_for

    a = anchor_for(doc["text"], "TorchDynamo")
    url = f"/api/v1/documents/{doc['document_id']}/threads"
    r1 = await api.post(url, json={**a, "version_sha256": doc["sha256"], "question": "q1"})
    await api.wait_job(r1.json()["job_id"])
    assert not (fake_env / "opencode_last_model").exists()
    r2 = await api.post(url, json={**a, "version_sha256": doc["sha256"], "question": "q2", "model_choice": {"model": "fake/beta", "variant": "high"}})
    assert r2.status_code == 201, r2.text
    await api.wait_job(r2.json()["job_id"])
    assert (fake_env / "opencode_last_model").read_text() == "fake/beta" and (fake_env / "opencode_last_variant").read_text() == "high"
    assert r2.json()["session_id"] != r1.json()["session_id"]
    assert (await api.get(f"/api/v1/agent-sessions/{r1.json()['session_id']}")).json()["model"] is None
    # the same model again reuses that session; follow-ups keep it
    r3 = await api.post(url, json={**a, "version_sha256": doc["sha256"], "question": "q3", "session_id": r2.json()["session_id"],
                                   "model_choice": {"model": "fake/beta", "variant": "high"}})
    assert r3.json()["session_id"] == r2.json()["session_id"]
    await api.wait_job(r3.json()["job_id"])
    # a reply with another model switches that discussion to a session with it
    r4 = await api.post(f"/api/v1/threads/{r1.json()['thread_id']}/messages", json={"text": "again", "model_choice": {"model": "other/gamma"}})
    await api.wait_job(r4.json()["job_id"])
    assert (fake_env / "opencode_last_model").read_text() == "other/gamma"
    assert (await api.post(url, json={**a, "version_sha256": doc["sha256"], "question": "x", "model_choice": {"model": "--bad"}})).status_code == 400
    # a folder chat with Codex and an effort
    c = (await api.post("/api/v1/chats", json={"title": "c", "about": "docs"})).json()
    d = await api.open_doc(c["path"])
    r = await api.post(f"/api/v1/documents/{d['document_id']}/threads", json={"version_sha256": d["sha256"], "question": "q", "whole_document": True,
                                                                               "model_choice": {"provider": "codex", "model": "gpt-5", "variant": "low"}})
    await api.wait_job(r.json()["job_id"])
    assert (fake_env / "codex_last_model").read_text() == "gpt-5" and (fake_env / "codex_last_effort").read_text() == "low"
    # PR Development: a file's discussion with another model becomes the file's session
    src, _ = make_source(tmp_path)
    dv = await new_branch_ws(api, src, None)
    r = await api.post(f"/api/v1/dev/workspaces/{dv['id']}/ask", json={"text": "hi", "kind": "ask", "anchor": {"root": "wt", "file": "lib.py"},
                                                                         "model_choice": {"model": "fake/beta"}})
    await api.wait_job(r.json()["job_id"])
    assert (fake_env / "opencode_last_model").read_text() == "fake/beta"
    fs = (await api.get(f"/api/v1/dev/workspaces/{dv['id']}/conversation")).json()["file_sessions"]
    assert fs["wt:lib.py"] == r.json()["session_id"]


async def test_changes_view_compare_refs_file_and_commit(running_app, dev_settings, tmp_path, fake_env):
    """The Changes view: per-file diffs of uncommitted work, of the whole branch and of one commit; refs; file lines
    for more context; drafting a message for some files; committing a message that starts with '-'."""
    h = await running_app(dev_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    d = await new_branch_ws(api, src, None)
    base = f"/api/v1/dev/workspaces/{d['id']}"
    wt = Path(d["worktree_path"])
    (wt / "lib.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    (wt / "notes.txt").write_text("hello\n")
    (wt / "pyproject.toml").unlink()
    c = (await api.get(f"{base}/compare")).json()
    by = {f["path"]: f for f in c["files"]}
    assert set(by) == {"lib.py", "notes.txt", "pyproject.toml"} and c["worktree"] is True
    assert by["lib.py"]["status"] == "modified" and by["lib.py"]["additions"] == 4 and by["lib.py"]["deletions"] == 0
    assert by["lib.py"]["unified"].startswith("@@ ") and "+def sub(a, b):" in by["lib.py"]["unified"]
    assert by["notes.txt"]["status"] == "untracked" and by["pyproject.toml"]["status"] == "deleted"
    assert c["summary"] == {"files": 3, "additions": 5, "deletions": 2}
    # one file only; lines of the worktree file for more context
    assert [f["path"] for f in (await api.get(f"{base}/compare", params={"path": "lib.py"})).json()["files"]] == ["lib.py"]
    assert (await api.get(f"{base}/compare/file", params={"path": "lib.py"})).json()["lines"][4] == "def sub(a, b):"
    assert (await api.get(f"{base}/compare/file", params={"path": "../x"})).status_code == 400
    # a commit message drafted for the selected files only
    r = await api.post(f"{base}/draft?what=commit", json={"paths": ["notes.txt"]})
    assert r.status_code == 202, r.text
    await api.wait_job(r.json()["job_id"])
    prompt = (fake_env / "opencode_last_prompt").read_text()
    assert "+hello" in prompt and "def sub" not in prompt
    # commit lib.py with a message starting with "-" (not taken as an option)
    r = await api.post(f"{base}/commit", json={"message": "-Add sub", "paths": ["lib.py"]})
    assert r.status_code == 200, r.text
    assert git(wt, "log", "-1", "--format=%s") == "-Add sub"
    refs = (await api.get(f"{base}/refs")).json()
    assert [x["subject"] for x in refs["branch_commits"]] == ["-Add sub"] and refs["commits"][1]["subject"] == "init"
    assert "main" in refs["branches"] or "origin/main" in refs["branches"]
    sha = refs["branch_commits"][0]["sha"]
    # just that commit; the whole branch (committed + uncommitted); committed only (HEAD as the target)
    one = (await api.get(f"{base}/compare", params={"commit": sha})).json()
    assert [f["path"] for f in one["files"]] == ["lib.py"] and one["worktree"] is False
    assert (await api.get(f"{base}/compare/file", params={"path": "lib.py", "ref": sha})).json()["lines"][-1] == "    return a - b"
    br = (await api.get(f"{base}/compare", params={"base": "branch"})).json()
    assert {f["path"] for f in br["files"]} == {"lib.py", "notes.txt", "pyproject.toml"}
    committed = (await api.get(f"{base}/compare", params={"base": "branch", "target": "HEAD"})).json()
    assert [f["path"] for f in committed["files"]] == ["lib.py"]
    # the root commit (no parent): everything added
    root = refs["commits"][-1]["sha"]
    assert {f["status"] for f in (await api.get(f"{base}/compare", params={"commit": root})).json()["files"]} == {"added"}
    for bad in ({"base": "--output=/tmp/x"}, {"target": "nope-ref"}, {"commit": "-p"}, {"base": "a b"}):
        assert (await api.get(f"{base}/compare", params=bad)).status_code == 400, bad
    # asking about lines of a change: the agent gets the diff and the discussion sits on those lines
    r = await api.post(f"{base}/ask", json={"text": "Why sub?", "kind": "ask",
                                            "anchor": {"root": "wt", "file": "lib.py", "start_line": 5, "end_line": 6, "quote": "def sub(a, b):\n    return a - b"},
                                            "context": {"diff": "+def sub(a, b):\n+    return a - b", "diff_label": "lines of lib.py in the uncommitted changes"}})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    prompt = (fake_env / "opencode_last_prompt").read_text()
    assert "reviewing lines of lib.py in the uncommitted changes in the Changes view" in prompt and "```diff\n+def sub(a, b):" in prompt
    t = (await api.get(f"{base}/conversation")).json()["threads"][-1]
    assert t["target"]["file"] == "lib.py" and t["target"]["start_line"] == 5


def test_split_diff_renames_binary_and_quoted_paths(tmp_path):
    from backend.app.development.service import split_diff

    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "d@e.com")
    git(r, "config", "user.name", "D")
    (r / "old name.txt").write_text("".join(f"line {i}\n" for i in range(20)))
    (r / "pic.bin").write_bytes(b"\x00\x01\x02")
    (r / "é.txt").write_text("a\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "a")
    git(r, "mv", "old name.txt", "new name.txt")
    (r / "new name.txt").write_text("".join(f"line {i}\n" for i in range(19)) + "changed\n")
    (r / "pic.bin").write_bytes(b"\x00\x09")
    (r / "é.txt").write_text("b\n")
    git(r, "add", "-A")
    out = subprocess.run(["git", "diff", "--no-color", "-M", "--cached"], cwd=r, capture_output=True, text=True).stdout
    by = {f["path"]: f for f in split_diff(out)}
    assert set(by) == {"new name.txt", "pic.bin", "é.txt"}
    assert by["new name.txt"]["status"] == "renamed" and by["new name.txt"]["previous_path"] == "old name.txt"
    assert by["new name.txt"]["additions"] == 1 and by["new name.txt"]["deletions"] == 1
    assert by["pic.bin"]["binary"] is True and by["pic.bin"]["unified"] == ""
    assert by["é.txt"]["unified"] == "@@ -1 +1 @@\n-a\n+b\n"


async def test_open_a_folder_directly_branches_and_diffs_per_folder(running_app, dev_settings, tmp_path, fake_env):
    """A checkout opened directly (no worktree) and a plain folder made a repository; create and switch branches
    there; the Changes view for one folder or file; archiving never removes the folder."""
    h = await running_app(dev_settings)
    api = h.api
    src, _ = make_source(tmp_path)
    r = await api.post("/api/v1/dev/workspaces", json={"source_repo": str(src / "."), "mode": "folder"})
    assert r.status_code == 201, r.text
    d = await wait_ready(api, r.json()["id"])
    assert d["status"] == "ready", d
    assert d["worktree_path"] == str(src) and d["branch"] == "main" and d["mode"] == "folder" and d["name"] == "src"
    base = f"/api/v1/dev/workspaces/{d['id']}"
    # a new branch, in the checkout itself; it is compared against where it started
    r = await api.post(f"{base}/branch", json={"name": "feature/direct"})
    assert r.status_code == 200, r.text
    assert git(src, "symbolic-ref", "--short", "HEAD") == "feature/direct"
    br = (await api.get(f"{base}/branches")).json()
    assert br["current"] == "feature/direct" and set(br["branches"]) == {"main", "feature/direct"} and br["base_ref"] == "main"
    assert (await api.post(f"{base}/branch", json={"name": "main"})).status_code == 409  # exists: switch instead
    assert (await api.post(f"{base}/branch", json={"name": "-x"})).status_code == 400
    # changes in a folder and elsewhere: the diff of the folder, of one file, of everything
    (src / "pkg").mkdir()
    (src / "pkg" / "a.py").write_text("A = 1\n")
    (src / "pkg" / "b.py").write_text("B = 1\n")
    (src / "lib.py").write_text("def add(a, b):\n    return b + a\n")
    every = (await api.get(f"{base}/compare")).json()
    assert {f["path"] for f in every["files"]} == {"lib.py", "pkg/a.py", "pkg/b.py"}
    folder = (await api.get(f"{base}/compare", params={"path": "pkg"})).json()
    assert {f["path"] for f in folder["files"]} == {"pkg/a.py", "pkg/b.py"} and folder["path"] == "pkg"
    assert [f["path"] for f in (await api.get(f"{base}/compare", params={"path": "pkg/b.py"})).json()["files"]] == ["pkg/b.py"]
    assert (await api.get(f"{base}/compare", params={"path": "../x"})).status_code == 400
    c = await api.post(f"{base}/commit", json={"message": "pkg", "paths": ["pkg/a.py", "pkg/b.py"]})
    assert c.status_code == 200, c.text
    assert git(src, "log", "-1", "--format=%s", "feature/direct") == "pkg"
    branch_diff = (await api.get(f"{base}/compare", params={"base": "branch", "target": "HEAD"})).json()
    assert {f["path"] for f in branch_diff["files"]} == {"pkg/a.py", "pkg/b.py"}
    # switching outside the app is followed
    git(src, "stash", "-q")
    git(src, "switch", "-q", "main")
    assert (await api.get(f"{base}/status")).json()["branch"] == "main"
    r = await api.post(f"{base}/branch", json={"name": "feature/direct", "create": False})
    assert r.status_code == 200 and git(src, "symbolic-ref", "--short", "HEAD") == "feature/direct"
    # archiving with "remove the worktree" leaves the folder alone
    r = await api.delete(base, params={"remove_worktree": "true", "force": "true"})
    assert r.status_code == 200 and r.json()["worktree_removed"] is False and (src / "lib.py").exists()

    # a plain folder: made a Git repository; with no commit yet, everything shows as new
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "main.py").write_text("print('hi')\n")
    r = await api.post("/api/v1/dev/workspaces", json={"source_repo": str(plain), "mode": "folder", "branch": "work"})
    d2 = await wait_ready(api, r.json()["id"])
    assert d2["status"] == "ready", d2
    assert (plain / ".git").is_dir() and d2["branch"] == "work"
    files = (await api.get(f"/api/v1/dev/workspaces/{d2['id']}/compare")).json()["files"]
    assert [(f["path"], f["status"]) for f in files] == [("main.py", "untracked")]
    # without git init: refused
    other = tmp_path / "other"
    other.mkdir()
    r = await api.post("/api/v1/dev/workspaces", json={"source_repo": str(other), "mode": "folder", "init_git": False})
    d3 = await wait_ready(api, r.json()["id"])
    assert d3["status"] == "failed" and "not in a Git repository" in d3["error"] and not (other / ".git").exists()


async def test_your_pull_requests_of_a_repo_and_checking_one_out_in_a_folder(running_app, dev_settings, tmp_path, fake_env, monkeypatch):
    """List your PRs of a repository (or of a checkout's repository), with the workspaces that have them; check one
    out in a folder workspace, update it, push to its branch; switching away leaves the PR."""
    h = await running_app(dev_settings)
    api = h.api
    src, bare = make_source(tmp_path)
    git(src, "remote", "set-url", "origin", "https://github.com/acme/proj.git")
    git(src, "config", f"url.{bare}.insteadOf", "https://github.com/acme/proj.git")
    other = tmp_path / "contrib"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    git(other, "config", "user.email", "c@example.com")
    git(other, "config", "user.name", "C")
    git(other, "checkout", "-q", "-b", "my-fix")
    (other / "lib.py").write_text("def add(a, b):\n    return a + b  # my fix\n")
    git(other, "commit", "-q", "-am", "My fix")
    git(other, "push", "-q", str(bare), "HEAD:refs/pull/7/head", "HEAD:refs/heads/my-fix")

    from backend.app.development import service

    seen: list = []

    async def my_prs(repo, state="open"):
        seen.append((repo, state))
        return [{"number": 7, "title": "My fix", "url": "u7", "isDraft": False, "state": "OPEN", "headRefName": "my-fix", "baseRefName": "main",
                 "updatedAt": "2026-11-10T00:00:00Z", "reviewDecision": None, "ci": "SUCCESS"},
                {"number": 9, "title": "Another", "url": "u9", "isDraft": True, "state": "OPEN", "headRefName": "x", "baseRefName": "main",
                 "updatedAt": "2026-11-09T00:00:00Z", "reviewDecision": None, "ci": None}]

    async def meta(repo, number):
        return {"headRefName": "my-fix", "head_repo": "acme/proj", "baseRefName": "main", "isCrossRepository": False,
                "maintainerCanModify": True, "title": "My fix"}
    monkeypatch.setattr(service, "list_my_prs", my_prs)
    monkeypatch.setattr(service, "fetch_pr_meta", meta)

    r = await api.post("/api/v1/dev/workspaces", json={"source_repo": str(src), "mode": "folder"})
    d = await wait_ready(api, r.json()["id"])
    assert d["github_repo"] == "acme/proj"
    base = f"/api/v1/dev/workspaces/{d['id']}"
    # the known repositories, and your PRs of one — by name or by a checkout of it
    assert (await api.get("/api/v1/dev/repos")).json()["repos"][0] == {"repo": "acme/proj", "source": str(src), "workspaces": 1}
    got = (await api.get("/api/v1/dev/my-prs", params={"source": str(src)})).json()
    assert got["repo"] == "acme/proj" and got["source"] == str(src) and [p["number"] for p in got["prs"]] == [7, 9]
    assert got["prs"][0]["workspaces"] == [] and seen[-1] == ("acme/proj", "open")
    assert (await api.get("/api/v1/dev/my-prs", params={"repo": "acme/proj", "state": "all"})).json()["state"] == "all"
    assert (await api.get("/api/v1/dev/my-prs", params={"repo": "not a repo"})).status_code == 400
    # check #7 out in the folder: its branch, compared with its base; pushes go to the PR's branch
    r = await api.post(f"{base}/checkout-pr", json={"number": 7})
    assert r.status_code == 200, r.text
    assert r.json()["branch"] == "my-fix" and git(src, "symbolic-ref", "--short", "HEAD") == "my-fix"
    assert "# my fix" in (src / "lib.py").read_text()
    d = (await api.get(base)).json()
    assert d["pr_number"] == 7 and d["push_branch"] == "my-fix" and d["base_ref"] == "refs/waw/base/main"
    st = (await api.get(f"{base}/status")).json()
    assert st["commits_on_branch"] == 1 and st["unpushed"] == 0
    assert (await api.get("/api/v1/dev/my-prs", params={"repo": "acme/proj"})).json()["prs"][0]["workspaces"][0]["id"] == d["id"]
    (src / "lib.py").write_text("def add(a, b):\n    return b + a  # updated\n")
    assert (await api.post(f"{base}/commit", json={"message": "Update the PR"})).status_code == 200
    assert (await api.post(f"{base}/push")).status_code == 200
    assert git(bare, "log", "-1", "--format=%s", "my-fix") == "Update the PR"
    # back to main: the workspace no longer belongs to the PR
    assert (await api.post(f"{base}/branch", json={"name": "main", "create": False})).status_code == 200
    d = (await api.get(base)).json()
    assert d["pr_number"] is None and d["branch"] == "main" and d["push_remote"] == "origin"
    # a PR workspace cannot switch to another PR
    r = await api.post("/api/v1/dev/workspaces", json={"source_repo": str(src), "mode": "pr", "github_repo": "acme/proj", "pr_number": 7})
    p = await wait_ready(api, r.json()["id"])
    assert (await api.post(f"/api/v1/dev/workspaces/{p['id']}/checkout-pr", json={"number": 9})).status_code == 409
