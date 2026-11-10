"""Multi-root windows: several wiki folders in one window, fully isolated from each other."""

from __future__ import annotations

from pathlib import Path

from conftest import Api, anchor_for, make_settings, make_workspace


async def _add(api: Api, root: Path, name: str | None = None) -> str:
    r = await api.post("/api/v1/workspaces", json={"root": str(root), "name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_add_folder_and_isolation_of_same_relative_paths(running_app, settings, workspace: Path, tmp_path: Path):
    h = await running_app()
    api = h.api
    other = tmp_path / "second-wiki"
    make_workspace(other)
    (other / "docs" / "guide.md").write_text("# Other guide\n\nCompletely different TorchDynamo text.\n")
    primary = (await api.get("/api/v1/workspaces")).json()
    assert len(primary["workspaces"]) == 1 and primary["workspaces"][0]["primary"] is True
    ws1 = primary["workspaces"][0]["id"]
    ws2 = await _add(api, other, "Second")
    lst = (await api.get("/api/v1/workspaces")).json()["workspaces"]
    assert [w["name"] for w in lst] == [workspace.name, "Second"] and lst[1]["git"]["is_repo"] is True
    # same relative path, different documents, contents, discussions
    api.ws = ws1
    d1 = await api.open_doc()
    api.ws = ws2
    d2 = await api.open_doc()
    assert d1["document_id"] != d2["document_id"] and d2["text"].startswith("# Other guide")
    a = anchor_for(d2["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{d2['document_id']}/threads", json={**a, "version_sha256": d2["sha256"], "question": "in second"})
    await api.wait_job(r.json()["job_id"])
    api.ws = ws1
    assert (await api.threads(d1["document_id"]))["threads"] == []
    # the object decides the root: a wrong header cannot read root 2's document through root 1
    assert len((await api.threads(d2["document_id"]))["threads"]) == 1
    # locks are per root
    await api.post("/api/v1/locks", json={"path": "docs/guide.md"})
    assert (await api.open_doc())["lock"] is not None
    api.ws = ws2
    assert (await api.open_doc())["lock"] is None
    # agent sessions are per root, and turns run in that root's folder
    sess2 = (await api.get("/api/v1/agent-sessions")).json()["sessions"]
    api.ws = ws1
    sess1 = (await api.get("/api/v1/agent-sessions")).json()["sessions"]
    assert sess2 and not sess1
    # git status / diff are per root
    (other / "only-in-second.md").write_text("# only here\n")
    api.ws = ws2
    files2 = {f["path"] for f in (await api.get("/api/v1/git/status")).json()["files"]}
    api.ws = ws1
    files1 = {f["path"] for f in (await api.get("/api/v1/git/status")).json()["files"]}
    assert "only-in-second.md" in files2 and "only-in-second.md" not in files1
    # Recent: per root by default, all roots with all=true (tagged)
    api.ws = None
    everywhere = (await api.get("/api/v1/recent", params={"all": "true"})).json()["items"]
    assert {i["workspace_id"] for i in everywhere} >= {ws2}
    assert any(i.get("workspace_name") == "Second" for i in everywhere)
    # raw files can be addressed with ?workspace=
    (other / "docs" / "pic.png").write_bytes(b"\x89PNG")
    assert (await api.get("/api/v1/raw", params={"path": "docs/pic.png", "workspace": ws2})).status_code == 200
    assert (await api.get("/api/v1/raw", params={"path": "docs/pic.png"})).status_code == 404  # primary root has none
    # guards
    assert (await api.post("/api/v1/workspaces", json={"root": str(other)})).json()["error"]["code"] == "ALREADY_OPEN"
    assert (await api.post("/api/v1/workspaces", json={"root": "relative/path"})).status_code == 400
    assert (await api.post("/api/v1/workspaces", json={"root": str(tmp_path / "nope")})).status_code == 404
    assert (await api.get("/api/v1/files", headers={"x-workspace": "00000000-0000-0000-0000-000000000000"})).status_code == 404
    assert (await api.delete(f"/api/v1/workspaces/{ws1}")).status_code == 200  # config folders can be closed too


async def test_added_folders_persist_and_removed_ones_keep_their_data(running_app, settings, tmp_path: Path):
    h = await running_app()
    api = h.api
    other = tmp_path / "w2"
    make_workspace(other)
    ws2 = await _add(api, other, "W2")
    api.ws = ws2
    d = await api.open_doc()
    await api.put(f"/api/v1/documents/{d['document_id']}/reading-state", json={"scroll_fraction": 0.4})
    await h.stop()
    # restart: the folder added from the UI is still open, with its data
    h2 = await running_app(settings)
    api2 = h2.api
    assert [w["id"] for w in (await api2.get("/api/v1/workspaces")).json()["workspaces"]][1] == ws2
    api2.ws = ws2
    assert (await api2.open_doc())["reading_state"]["scroll_fraction"] == 0.4
    # remove from the window: gone from the list, data kept, comes back when re-added
    api2.ws = None
    assert (await api2.delete(f"/api/v1/workspaces/{ws2}")).json()["data_kept"] is True
    assert len((await api2.get("/api/v1/workspaces")).json()["workspaces"]) == 1
    assert (await api2.get("/api/v1/files", headers={"x-workspace": ws2})).json()["error"]["code"] == "WORKSPACE_NOT_OPEN"
    again = await _add(api2, other)
    assert again == ws2
    api2.ws = ws2
    assert (await api2.open_doc())["reading_state"]["scroll_fraction"] == 0.4
    # a folder that disappeared from disk is listed as unavailable and can be removed
    api2.ws = None
    (other).rename(tmp_path / "w2-moved")
    await h2.stop()
    h3 = await running_app(settings)
    lst = (await h3.api.get("/api/v1/workspaces")).json()["workspaces"]
    gone = next(w for w in lst if w["id"] == ws2)
    assert gone["available"] is False
    assert (await h3.api.delete(f"/api/v1/workspaces/{ws2}")).json()["removed"] is True
    assert len((await h3.api.get("/api/v1/workspaces")).json()["workspaces"]) == 1


async def test_config_lists_several_roots_and_folder_picker(running_app, tmp_path: Path, workspace: Path):
    second = tmp_path / "cfg-second"
    make_workspace(second)
    s = make_settings(tmp_path / "multi", workspace)
    s.workspaces = [{"root": str(second), "name": "From config"}]
    h = await running_app(s)
    lst = (await h.api.get("/api/v1/workspaces")).json()["workspaces"]
    assert [w["name"] for w in lst] == [workspace.name, "From config"] and all(w["configured"] for w in lst)
    pick = (await h.api.get("/api/v1/fs/dirs", params={"path": str(tmp_path)})).json()
    names = {e["name"]: e for e in pick["entries"]}
    assert names["cfg-second"]["is_git"] is True and pick["parent"] == str(tmp_path.parent)
    assert (await h.api.get("/api/v1/fs/dirs", params={"path": str(second)})).json()["open_as"] == lst[1]["id"]
    assert (await h.api.get("/api/v1/fs/dirs", params={"path": "rel"})).status_code == 400


# ---------------------------------------------------------------------- named workspaces, creating things

async def test_named_workspaces_switch_folders_terminals_and_survive_restart(running_app, settings, workspace: Path, tmp_path: Path):
    from conftest import requires_tmux  # noqa: F401  (terminal part is skipped below without tmux)
    import shutil

    h = await running_app()
    api = h.api
    named = (await api.get("/api/v1/named-workspaces")).json()
    assert [n["name"] for n in named["workspaces"]] == ["Default"] and named["workspaces"][0]["current"]
    default_id = named["current"]
    second = tmp_path / "second-wiki"
    make_workspace(second)
    have_tmux = shutil.which("tmux") is not None
    if have_tmux:
        t_default = (await api.post("/api/v1/terminals", json={})).json()
    # create a workspace with only the second folder: the window switches to it
    r = await api.post("/api/v1/named-workspaces", json={"name": "Second only", "root": str(second)})
    assert r.status_code == 201, r.text
    nid = r.json()["current"]
    assert nid != default_id
    folders = (await api.get("/api/v1/workspaces")).json()["workspaces"]
    assert [f["root"] for f in folders] == [str(second.resolve())]
    assert folders[0]["configured"] is False  # removable here; config folders are only pinned in Default
    if have_tmux:
        assert (await api.get("/api/v1/terminals")).json()["terminals"] == []  # Default's terminal is not shown here
        t_second = (await api.post("/api/v1/terminals", json={})).json()
    # names are unique
    assert (await api.post("/api/v1/named-workspaces", json={"name": "Default", "root": str(second)})).status_code == 409
    # folders added here belong to this workspace only
    third = tmp_path / "third-wiki"
    make_workspace(third)
    await _add(api, third, "Third")
    # back to Default: config folder again, neither second nor third
    r = await api.post(f"/api/v1/named-workspaces/{default_id}/open")
    assert r.status_code == 200
    roots = [f["root"] for f in (await api.get("/api/v1/workspaces")).json()["workspaces"]]
    assert roots == [str(workspace.resolve())]
    if have_tmux:
        ids = [t["id"] for t in (await api.get("/api/v1/terminals")).json()["terminals"]]
        assert ids == [t_default["id"]]
    # can't delete the current or Default; deleting another moves its terminals to Default (shells keep running)
    assert (await api.delete(f"/api/v1/named-workspaces/{default_id}")).status_code == 409
    await api.post(f"/api/v1/named-workspaces/{nid}/open")
    assert (await api.delete(f"/api/v1/named-workspaces/{nid}")).status_code == 409
    # restart: the current workspace and its folders come back
    await h.stop()
    h2 = await running_app()
    roots = [f["root"] for f in (await h2.api.get("/api/v1/workspaces")).json()["workspaces"]]
    assert roots == [str(second.resolve()), str(third.resolve())]
    assert (await h2.api.get("/api/v1/named-workspaces")).json()["current"] == nid
    # rename, then delete from Default
    assert (await h2.api.patch(f"/api/v1/named-workspaces/{nid}", json={"name": "Renamed"})).json()["workspaces"][1]["name"] == "Renamed"
    await h2.api.post(f"/api/v1/named-workspaces/{default_id}/open")
    r = await h2.api.delete(f"/api/v1/named-workspaces/{nid}")
    assert r.status_code == 200 and [n["name"] for n in r.json()["workspaces"]] == ["Default"]
    if have_tmux:
        ids = {t["id"] for t in (await h2.api.get("/api/v1/terminals")).json()["terminals"]}
        assert ids == {t_default["id"], t_second["id"]}


async def test_create_file_and_folder_in_the_tree(api: Api, workspace: Path):
    r = await api.post("/api/v1/files", json={"path": "notes/today.md", "kind": "file"})
    assert r.status_code == 201, r.text
    assert r.json() == {"path": "notes/today.md", "kind": "file", "format": "markdown", "openable": True}
    assert (workspace / "notes" / "today.md").read_text() == "# today\n"  # missing parents created
    assert (await api.post("/api/v1/files", json={"path": "notes/empty.txt", "kind": "file"})).status_code == 201
    assert (workspace / "notes" / "empty.txt").read_text() == ""
    assert (await api.post("/api/v1/files", json={"path": "notes/sub", "kind": "dir"})).status_code == 201
    assert (workspace / "notes" / "sub").is_dir()
    # refused: existing, traversal, .git, app-internal, a file as a folder
    assert (await api.post("/api/v1/files", json={"path": "notes/today.md", "kind": "file"})).status_code == 409
    assert (await api.post("/api/v1/files", json={"path": "../outside.md", "kind": "file"})).status_code == 400
    assert (await api.post("/api/v1/files", json={"path": ".git/x", "kind": "file"})).status_code == 400
    assert (await api.post("/api/v1/files", json={"path": ".waw-tmp-x", "kind": "file"})).status_code == 400
    assert (await api.post("/api/v1/files", json={"path": "README.md/x.md", "kind": "file"})).status_code == 400
    # a symlink leaving the folder is not written through
    outside = workspace.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    (workspace / "link-out").symlink_to(outside)
    r = await api.post("/api/v1/files", json={"path": "link-out/new.md", "kind": "file"})
    assert r.status_code == 403 and not (outside / "new.md").exists()
    # locked folders refuse new entries
    assert (await api.post("/api/v1/locks", json={"path": "notes"})).status_code in (200, 201)
    r = await api.post("/api/v1/files", json={"path": "notes/locked.md", "kind": "file"})
    assert r.status_code == 423 and not (workspace / "notes" / "locked.md").exists()


async def test_folder_picker_can_create_a_folder(api: Api, tmp_path: Path):
    r = await api.post("/api/v1/fs/mkdir", json={"parent": str(tmp_path), "name": "new-wiki", "git": True})
    assert r.status_code == 201, r.text
    assert r.json()["is_git"] is True and (tmp_path / "new-wiki" / ".git").is_dir()
    assert (tmp_path / "new-wiki" / ".gitignore").read_text() == ".waw-tmp-*\n"
    assert (await api.post("/api/v1/fs/mkdir", json={"parent": str(tmp_path), "name": "new-wiki"})).status_code == 409
    assert (await api.post("/api/v1/fs/mkdir", json={"parent": str(tmp_path), "name": "a/b"})).status_code == 400
    assert (await api.post("/api/v1/fs/mkdir", json={"parent": "relative", "name": "x"})).status_code == 400
    r = await api.post("/api/v1/fs/mkdir", json={"parent": str(tmp_path), "name": "plain"})
    assert r.status_code == 201 and r.json()["is_git"] is False


async def test_close_a_config_folder_for_good_and_reopen_it(running_app, tmp_path: Path, workspace: Path):
    """Closing a folder (also one from config.toml, also the first one) keeps it closed across restarts."""
    second = tmp_path / "cfg-second"
    make_workspace(second)
    s = make_settings(tmp_path / "multi", workspace)
    s.workspaces = [{"root": str(second), "name": "From config"}]
    h = await running_app(s)
    lst = (await h.api.get("/api/v1/workspaces")).json()["workspaces"]
    assert (await h.api.delete(f"/api/v1/workspaces/{lst[0]['id']}")).status_code == 200  # the primary one
    lst2 = (await h.api.get("/api/v1/workspaces")).json()["workspaces"]
    assert [w["name"] for w in lst2] == ["From config"] and lst2[0]["primary"] is True
    assert h.ctx.workspace_root == second.resolve()  # defaults follow the new first folder
    assert (await h.api.delete(f"/api/v1/workspaces/{lst2[0]['id']}")).json()["error"]["code"] == "LAST_WORKSPACE"
    await h.stop()
    h2 = await running_app(s)
    assert [w["name"] for w in (await h2.api.get("/api/v1/workspaces")).json()["workspaces"]] == ["From config"]  # stays closed
    # adding it again reopens it for good
    assert (await h2.api.post("/api/v1/workspaces", json={"root": str(workspace)})).status_code == 201
    await h2.stop()
    h3 = await running_app(s)
    assert len((await h3.api.get("/api/v1/workspaces")).json()["workspaces"]) == 2


async def _wait_clone(api, cid):
    import asyncio

    for _ in range(200):
        j = (await api.get(f"/api/v1/fs/clone/{cid}")).json()
        if j["state"] != "running":
            return j
        await asyncio.sleep(0.1)
    raise AssertionError("clone did not finish")


async def test_add_a_project_by_cloning_a_url(api: Api, tmp_path: Path):
    import subprocess

    src = tmp_path / "origin-wiki"
    make_workspace(src)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    dest = tmp_path / "projects"
    dest.mkdir()
    r = await api.post("/api/v1/fs/clone", json={"url": f"file://{bare}", "parent": str(dest)})
    assert r.status_code == 202, r.text
    j = await _wait_clone(api, r.json()["id"])
    assert j["state"] == "done" and j["path"] == str((dest / "origin").resolve()) and (dest / "origin" / "README.md").is_file()
    assert (await api.post("/api/v1/workspaces", json={"root": j["path"]})).status_code == 201
    # refused: an existing folder, not a URL, option injection
    assert (await api.post("/api/v1/fs/clone", json={"url": f"file://{bare}", "parent": str(dest)})).status_code == 409
    assert (await api.post("/api/v1/fs/clone", json={"url": "not a url", "parent": str(dest)})).status_code == 400
    assert (await api.post("/api/v1/fs/clone", json={"url": "--upload-pack=touch /tmp/x", "parent": str(dest)})).status_code == 400
    # a failing clone reports git's message and leaves no folder behind
    r = await api.post("/api/v1/fs/clone", json={"url": f"file://{tmp_path}/nope.git", "parent": str(dest), "name": "broken"})
    j = await _wait_clone(api, r.json()["id"])
    assert j["state"] == "failed" and j["error"] and not (dest / "broken").exists()
