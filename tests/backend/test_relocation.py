"""The state directory is self-contained: copy it anywhere, point the config at it, and everything is back."""

from __future__ import annotations

import shutil
from pathlib import Path

from conftest import Api, anchor_for, make_settings


async def test_copy_state_dir_and_move_wiki_restores_everything(running_app, settings, workspace: Path, tmp_path: Path):
    h = await running_app()
    api: Api = h.api
    doc = await api.open_doc()
    a = anchor_for(doc["text"], "TorchDynamo")
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={**a, "version_sha256": doc["sha256"], "question": "remember me"})
    await api.wait_job(r.json()["job_id"])
    await api.put(f"/api/v1/documents/{doc['document_id']}/reading-state", json={"version_sha256": doc["sha256"], "heading": "Capture", "offset": 5, "scroll_fraction": 0.5, "active_thread_id": r.json()["thread_id"]})
    socket_before = settings.tmux_socket_name
    t = (await api.post("/api/v1/terminals", json={})).json() if h.ctx.terminals.tmux.available else None

    # stop the server cleanly (WAL folded into the main db file), copy the folder, move the wiki
    await h.stop()
    assert not (settings.state_dir / "workspace.sqlite3-wal").exists() or (settings.state_dir / "workspace.sqlite3-wal").stat().st_size == 0
    new_state = tmp_path / "moved-state"
    shutil.copytree(settings.state_dir, new_state)
    assert (new_state / "workspace.sqlite3").exists() and (new_state / "snapshots").is_dir() and (new_state / "tmux-socket").exists()
    new_wiki = tmp_path / "wiki-moved"
    shutil.move(str(workspace), str(new_wiki))

    # "change the configuration file": new state_dir and new workspace root
    new_settings = make_settings(tmp_path / "cfg2", new_wiki)
    new_settings.state_dir = new_state
    assert new_settings.tmux_socket_name == socket_before  # terminals on the same host keep working
    h2 = await running_app(new_settings)
    api2 = h2.api
    recent = (await api2.get("/api/v1/recent")).json()["items"]
    assert [i["path"] for i in recent] == ["docs/guide.md"]
    assert recent[0]["thread_count"] == 1 and recent[0]["reading_state"]["heading"] == "Capture"
    doc2 = await api2.open_doc()
    assert doc2["document_id"] == doc["document_id"]  # same identity, relocated workspace root
    assert doc2["reading_state"]["scroll_fraction"] == 0.5 and doc2["reading_state"]["active_thread_id"] == r.json()["thread_id"]
    threads = (await api2.threads(doc["document_id"]))["threads"]
    assert threads[0]["anchor_state"] == "attached"
    assert [m["content"] for m in threads[0]["messages"]] == ["remember me", "echo: remember me"]
    versions = (await api2.get(f"/api/v1/documents/{doc['document_id']}/versions")).json()["versions"]
    assert versions and (await api2.get(f"/api/v1/documents/{doc['document_id']}/versions/{versions[0]['sha256']}")).status_code == 200
    if t is not None:
        listing = (await api2.get("/api/v1/terminals")).json()["terminals"]
        mine = [x for x in listing if x["id"] == t["id"]]
        assert mine and mine[0]["state"] == "live"
        await api2.delete(f"/api/v1/terminals/{t['id']}")


async def test_pointing_state_dir_at_a_different_existing_wiki_keeps_old_data_separate(running_app, settings, workspace: Path, tmp_path: Path):
    h = await running_app()
    doc = await h.api.open_doc()
    await h.api.put(f"/api/v1/documents/{doc['document_id']}/reading-state", json={"scroll_fraction": 0.2})
    await h.stop()
    other = tmp_path / "other-wiki"
    from conftest import make_workspace

    make_workspace(other)
    s2 = make_settings(tmp_path / "cfg2", other)
    s2.state_dir = settings.state_dir  # same state dir, different (still existing) wiki
    h2 = await running_app(s2)
    assert (await h2.api.get("/api/v1/recent")).json()["items"] == []  # old wiki's data is not mislabeled
    d2 = await h2.api.open_doc()
    assert d2["document_id"] != doc["document_id"]
    await h2.stop()
    # switching back to the original root brings the original data back
    h3 = await running_app(settings)
    assert [i["path"] for i in (await h3.api.get("/api/v1/recent")).json()["items"]] == ["docs/guide.md"]
    assert (await h3.api.open_doc())["document_id"] == doc["document_id"]
