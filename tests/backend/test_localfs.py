"""Local files: a file manager on server paths."""

from __future__ import annotations

from pathlib import Path


async def test_local_files_browse_edit(api, tmp_path: Path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "a.txt").write_text("hello\n")
    (d / "sub").mkdir()
    (d / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    r = await api.put("/api/v1/local/roots", json={"roots": [str(d), str(d)]})
    assert [x["path"] for x in r.json()["roots"]] == [str(d)]
    assert (await api.put("/api/v1/local/roots", json={"roots": ["rel/x"]})).status_code == 400
    items = (await api.get("/api/v1/local/list", params={"path": str(d)})).json()["items"]
    assert [i["name"] for i in items] == ["sub", "a.txt", "img.png"]
    f = (await api.get("/api/v1/local/file", params={"path": str(d / "a.txt")})).json()
    assert f["text"] == "hello\n" and not f["binary"]
    w = await api.put("/api/v1/local/file", json={"path": str(d / "a.txt"), "text": "bye\n", "base_sha256": f["sha256"]})
    assert w.status_code == 200 and (d / "a.txt").read_text() == "bye\n"
    stale = await api.put("/api/v1/local/file", json={"path": str(d / "a.txt"), "text": "x", "base_sha256": f["sha256"]})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "FILE_CHANGED"
    assert (await api.put("/api/v1/local/file", json={"path": str(d / "sub/n.py"), "text": "x=1\n", "create": True})).status_code == 200
    assert (await api.post("/api/v1/local/rename", json={"path": str(d / "sub/n.py"), "new_path": str(d / "m.py")})).status_code == 200
    assert (d / "m.py").read_text() == "x=1\n"
    assert (await api.post("/api/v1/local/mkdir", json={"path": str(d / "new")})).status_code == 200
    assert (await api.post("/api/v1/local/delete", json={"path": str(d / "new")})).status_code == 200 and not (d / "new").exists()
    st = (await api.post("/api/v1/local/stat", json={"paths": [str(d / "m.py"), str(d / "gone")]})).json()["sha256"]
    assert st[str(d / "gone")] is None and len(st[str(d / "m.py")]) == 64
    assert (await api.get("/api/v1/local/file", params={"path": str(d / "img.png")})).json()["binary"]
    raw = await api.get("/api/v1/local/raw", params={"path": str(d / "img.png")})
    assert raw.headers["content-type"] == "image/png"
    assert (await api.get("/api/v1/local/raw", params={"path": str(d / "m.py")})).headers["content-disposition"].startswith("attachment")
