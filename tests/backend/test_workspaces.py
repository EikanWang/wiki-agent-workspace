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
    assert (await api.delete(f"/api/v1/workspaces/{ws1}")).json()["error"]["code"] == "CONFIGURED_WORKSPACE"


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
    assert (await h.api.delete(f"/api/v1/workspaces/{lst[1]['id']}")).json()["error"]["code"] == "CONFIGURED_WORKSPACE"
    pick = (await h.api.get("/api/v1/fs/dirs", params={"path": str(tmp_path)})).json()
    names = {e["name"]: e for e in pick["entries"]}
    assert names["cfg-second"]["is_git"] is True and pick["parent"] == str(tmp_path.parent)
    assert (await h.api.get("/api/v1/fs/dirs", params={"path": str(second)})).json()["open_as"] == lst[1]["id"]
    assert (await h.api.get("/api/v1/fs/dirs", params={"path": "rel"})).status_code == 400
