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


async def test_local_chat_works_in_the_local_folder(running_app, tmp_path: Path):
    import json

    from backend.app.models import Job

    h = await running_app()
    api = h.api
    d = tmp_path / "proj"
    (d / "src").mkdir(parents=True)
    (d / "src" / "main.py").write_text("print('hi')\n")
    assert (await api.put("/api/v1/local/roots", json={"roots": [str(d)]})).status_code == 200
    assert (await api.post("/api/v1/local/chats", json={"path": str(d / "missing")})).status_code == 404
    assert (await api.post("/api/v1/local/chats", json={"path": "rel/x"})).status_code == 400
    c = await api.post("/api/v1/local/chats", json={"path": str(d / "src" / "main.py"), "title": "About main"})
    assert c.status_code == 201, c.text
    c = c.json()
    assert c["path"].startswith(".waw-chat/") and c["about"] == {"path": str(d / "src" / "main.py"), "kind": "file", "cwd": str(d)}
    doc = (await api.get(f"/api/v1/documents/open?path={c['path']}")).json()
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={"whole_document": True, "version_sha256": doc["sha256"],
                       "question": "What does it print?"})
    assert r.status_code == 201, r.text
    await api.wait_job(r.json()["job_id"])
    tid = r.json()["thread_id"]
    r2 = await api.post(f"/api/v1/threads/{tid}/messages", json={"text": "And why?"})
    await api.wait_job(r2.json()["job_id"])
    with h.ctx.db.read() as s:
        jobs = s.query(Job).filter(Job.id.in_([r.json()["job_id"], r2.json()["job_id"]])).order_by(Job.created_at).all()
        prompts, ctxs = [j.prompt for j in jobs], [json.loads(j.context_json) for j in jobs]
    assert all(x["_cwd"] == str(d) for x in ctxs)  # the agent works in the opened Local files folder
    assert "the file `src/main.py`" in prompts[0] and "about their wiki" not in prompts[0]
    assert prompts[1].startswith("Follow-up in this same discussion") and prompts[1].endswith("And why?")
    # a folder outside any opened one: the agent works in it
    other = tmp_path / "elsewhere"
    other.mkdir()
    c2 = (await api.post("/api/v1/local/chats", json={"path": str(other)})).json()
    assert c2["about"] == {"path": str(other), "kind": "folder", "cwd": str(other)}
    # listed per path in Local files, not in Chats; no distill / update into the wiki
    mine = (await api.get("/api/v1/local/chats", params={"path": str(d / "src" / "main.py")})).json()["chats"]
    assert [(x["title"], x["messages"]) for x in mine] == [("About main", 4)]
    assert len((await api.get("/api/v1/local/chats")).json()["chats"]) == 2
    assert c["path"] not in [x["path"] for x in (await api.get("/api/v1/chats")).json()["chats"]]
    assert (await api.post(f"/api/v1/threads/{tid}/messages", json={"text": "-", "chat_action": "update"})).status_code == 400
    assert (await api.delete(f"/api/v1/chats?path={c['path']}")).status_code == 200  # archived
    assert (await api.get("/api/v1/local/chats", params={"path": str(d / "src" / "main.py")})).json()["chats"] == []


def make_xlsx(path: Path) -> None:
    """A minimal two-sheet workbook: shared and inline strings, numbers, booleans, a gap column and a gap row."""
    import zipfile

    m = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {m} {r}><sheets><sheet name="Hash" sheetId="1" r:id="rId1"/>'
                   f'<sheet name="Empty" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst {m}><si><t>Bank</t></si><si><r><t>Sl</t></r><r><t>ice</t></r></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {m}><sheetData>'
                   '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row>'
                   '<row r="3"><c r="A3"><v>42</v></c><c r="B3" t="b"><v>1</v></c><c r="C3" t="inlineStr"><is><t>x</t></is></c></row>'
                   '</sheetData></worksheet>')
        z.writestr("xl/worksheets/sheet2.xml", f'<worksheet {m}><sheetData/></worksheet>')


async def test_local_xlsx_as_tables(api, tmp_path: Path):
    make_xlsx(tmp_path / "book.xlsx")
    r = (await api.get("/api/v1/local/sheet", params={"path": str(tmp_path / "book.xlsx")})).json()
    assert [s["name"] for s in r["sheets"]] == ["Hash", "Empty"]
    assert r["sheets"][0]["rows"] == [["Bank", "", "Slice"], [], ["42", "TRUE", "x"]]
    assert r["sheets"][1]["rows"] == []
    (tmp_path / "a.txt").write_text("x")
    assert (await api.get("/api/v1/local/sheet", params={"path": str(tmp_path / "a.txt")})).status_code == 400
    (tmp_path / "bad.xlsx").write_text("not a zip")
    assert (await api.get("/api/v1/local/sheet", params={"path": str(tmp_path / "bad.xlsx")})).json()["error"]["code"] == "BAD_WORKBOOK"


async def test_local_files_on_a_machine(running_app, tmp_path: Path, workspace, fake_env):
    """`<machine>:/path` is a path on a machine of the Machines view, reached over ssh (the fake ssh runs it here)."""
    from conftest import FAKES, make_settings

    from backend.app.config import DevSettings
    from backend.app.development import targets

    targets._HOMES.clear()
    s = make_settings(tmp_path, workspace)
    s.development = DevSettings(ssh=str(FAKES / "fake-ssh"))
    h = await running_app(s)
    api = h.api
    for m in ({"name": "box", "ssh": "box.internal"}, {"name": "down", "ssh": "unreachable.internal"}, {"name": "b", "kind": "bastion", "ssh": "b.example"}):
        assert (await api.post("/api/v1/machines", json=m)).status_code == 201
    d = tmp_path / "remote proj"
    d.mkdir()
    (d / "a.txt").write_text("hello\n")
    (d / ".hidden").write_text("h")
    (d / "sub").mkdir()
    (d / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "link").symlink_to(d / "sub")
    R = f"box:{d}"
    # open it (normalized), next to a folder of the server; a missing folder, an unknown machine and a bastion are refused
    r = await api.put("/api/v1/local/roots", json={"roots": [str(tmp_path), f"box:{d}/sub/.."]})
    assert r.status_code == 200, r.text
    roots = r.json()["roots"]
    assert [(x["path"], x["machine"], x["name"], x["exists"]) for x in roots] == [(str(tmp_path), None, tmp_path.name, True), (R, "box", "remote proj", True)]
    assert (await api.put("/api/v1/local/roots", json={"roots": [f"box:{d}/nope"]})).json()["error"]["code"] == "FOLDER_NOT_FOUND"
    assert (await api.put("/api/v1/local/roots", json={"roots": [f"ghost:{d}"]})).json()["error"]["code"] == "MACHINE_NOT_FOUND"
    assert (await api.put("/api/v1/local/roots", json={"roots": [f"b:{d}"]})).json()["error"]["code"] == "MACHINE_IS_BASTION"
    # browse
    lst = (await api.get("/api/v1/local/list", params={"path": R, "hidden": "false"})).json()
    assert [(i["name"], i["dir"], i["link"]) for i in lst["items"]] == [("link", True, True), ("sub", True, False), ("a.txt", False, False), ("img.png", False, False)]
    assert lst["path"] == R and lst["parent"] == f"box:{tmp_path}" and lst["items"][2]["path"] == f"{R}/a.txt" and lst["items"][2]["size"] == 6
    assert ".hidden" in [i["name"] for i in (await api.get("/api/v1/local/list", params={"path": R})).json()["items"]]
    assert (await api.get("/api/v1/local/list", params={"path": f"{R}/a.txt"})).json()["error"]["code"] == "FOLDER_NOT_FOUND"
    # read, conditional write (keeping the mode), create, conflicts
    (d / "a.txt").chmod(0o640)
    f = (await api.get("/api/v1/local/file", params={"path": f"{R}/a.txt"})).json()
    assert f["text"] == "hello\n" and f["writable"] and f["path"] == f"{R}/a.txt"
    w = await api.put("/api/v1/local/file", json={"path": f"{R}/a.txt", "text": "bye ü\n", "base_sha256": f["sha256"]})
    assert w.status_code == 200, w.text
    assert (d / "a.txt").read_text() == "bye ü\n" and (d / "a.txt").stat().st_mode & 0o777 == 0o640
    stale = await api.put("/api/v1/local/file", json={"path": f"{R}/a.txt", "text": "x", "base_sha256": f["sha256"]})
    assert stale.status_code == 409 and stale.json()["error"]["details"]["current_sha256"] == w.json()["sha256"]
    assert (await api.put("/api/v1/local/file", json={"path": f"{R}/a.txt", "text": "x", "create": True})).json()["error"]["code"] == "ALREADY_EXISTS"
    assert (await api.put("/api/v1/local/file", json={"path": f"{R}/sub/n.py", "text": "x=1\n", "create": True})).status_code == 200
    assert (await api.get("/api/v1/local/file", params={"path": f"{R}/img.png"})).json()["binary"]
    assert (await api.get("/api/v1/local/file", params={"path": f"{R}/gone"})).json()["error"]["code"] == "FILE_NOT_FOUND"
    # move, new folder, delete — moving between machines is refused
    assert (await api.post("/api/v1/local/rename", json={"path": f"{R}/sub/n.py", "new_path": f"{R}/m.py"})).json() == {"path": f"{R}/m.py"}
    assert (d / "m.py").read_text() == "x=1\n"
    assert (await api.post("/api/v1/local/rename", json={"path": f"{R}/m.py", "new_path": str(d / "x.py")})).status_code == 400
    assert (await api.post("/api/v1/local/mkdir", json={"path": f"{R}/new"})).status_code == 200 and (d / "new").is_dir()
    assert (await api.post("/api/v1/local/mkdir", json={"path": f"{R}/new"})).status_code == 409
    assert (await api.post("/api/v1/local/delete", json={"path": f"{R}/new"})).status_code == 200 and not (d / "new").exists()
    assert (await api.post("/api/v1/local/delete", json={"path": "box:/"})).status_code == 400
    # following the disk: hashes of open files on the server and on machines; an unreachable machine's are left out
    st = (await api.post("/api/v1/local/stat", json={"paths": [f"{R}/m.py", f"{R}/gone", str(d / "a.txt"), f"down:{d}/a.txt"]})).json()["sha256"]
    assert st[f"{R}/gone"] is None and len(st[f"{R}/m.py"]) == 64 and len(st[str(d / "a.txt")]) == 64
    assert f"down:{d}/a.txt" not in st
    raw = await api.get("/api/v1/local/raw", params={"path": f"{R}/img.png"})
    assert raw.headers["content-type"] == "image/png" and raw.content == b"\x89PNG\r\n\x1a\n"
    make_xlsx(d / "t.xlsx")
    sheets = (await api.get("/api/v1/local/sheet", params={"path": f"{R}/t.xlsx"})).json()["sheets"]
    assert sheets and sheets[0]["rows"]
    # an unreachable machine: its folder cannot be opened; one opened before shows why, and closing it still works
    assert (await api.put("/api/v1/local/roots", json={"roots": [f"down:{d}"]})).json()["error"]["code"] == "MACHINE_UNREACHABLE"
    from backend.app.kv import kv_put

    kv_put(h.ctx, "local_roots", [R, f"down:{d}"])
    roots = (await api.get("/api/v1/local/roots")).json()["roots"]
    assert roots[0]["exists"] and not roots[1]["exists"] and "Connection refused" in roots[1]["error"]
    assert [x["path"] for x in (await api.put("/api/v1/local/roots", json={"roots": [R]})).json()["roots"]] == [R]
    # no chats about remote folders (yet)
    assert (await api.post("/api/v1/local/chats", json={"path": R})).json()["error"]["code"] == "REMOTE_CHAT_UNSUPPORTED"
