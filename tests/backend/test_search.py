"""Folder search: file names, and the text in files (Markdown/text as they are, Word/PowerPoint/Visio/PDF through
their extracted text)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from test_locks_formats import _minimal_docx
from test_office import make_pptx, make_vsdx


def minimal_pdf(pages: list[str]) -> bytes:
    """A PDF with one line of text per page (Helvetica)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", ""]
    kids = []
    for text in pages:
        stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objs) + 2} 0 R /Resources << /Font << /F1 {len(objs) + 3} 0 R >> >> >>")
        kids.append(len(objs))
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
        objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


async def _content(api, q: str, **params) -> list[dict]:
    async with api.c.stream("GET", "/api/v1/search/content", params={"q": q, **params},
                            headers={"x-workspace": api.ws} if api.ws else None) as r:
        assert r.status_code == 200, await r.aread()
        return [json.loads(line) async for line in r.aiter_lines() if line.strip()]


def _files(events: list[dict]) -> dict[str, dict]:
    return {e["path"]: e for e in events if e["type"] == "file"}


async def test_names_match_every_word_of_the_path_the_ones_named_like_it_first_and_globs(api, workspace: Path):
    (workspace / "specs" / "eu").mkdir(parents=True)
    (workspace / "specs" / "eu" / "XE3P_EUGA_aspec.docx").write_bytes(_minimal_docx(["EU GA"]))
    (workspace / "specs" / "eu" / "notes.md").write_text("# eu notes\n")
    (workspace / "specs" / "aspec-index.md").write_text("# index\n")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "aspec.md").write_text("skipped\n")
    r = await api.get("/api/v1/search/names", params={"q": "aspec"})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["path"] for i in items] == ["specs/aspec-index.md", "specs/eu/XE3P_EUGA_aspec.docx"]  # named so first, shorter first
    assert items[1] == {"path": "specs/eu/XE3P_EUGA_aspec.docx", "name": "XE3P_EUGA_aspec.docx", "type": "file", "kind": "word",
                             "format": "docx", "openable": True}
    # every word, anywhere in the path; folders too
    assert [i["path"] for i in (await api.get("/api/v1/search/names", params={"q": "eu notes"})).json()["items"]] == ["specs/eu/notes.md"]
    assert next((i["path"], i["type"]) for i in (await api.get("/api/v1/search/names", params={"q": "eu"})).json()["items"]) == ("specs/eu", "dir")
    # a glob matches the name; a folder narrows it
    r = await api.get("/api/v1/search/names", params={"q": "*.md", "path": "specs/eu"})
    assert [i["path"] for i in r.json()["items"]] == ["specs/eu/notes.md"]
    r = await api.get("/api/v1/search/names", params={"q": "x", "path": "../etc"})
    assert r.status_code == 400


async def test_content_finds_text_in_markdown_word_powerpoint_visio_and_pdf_with_lines_and_pages(api, workspace: Path):
    (workspace / "specs").mkdir()
    (workspace / "specs" / "ga.docx").write_bytes(_minimal_docx(["Overview", "The EUGA unit maps host addresses.", "Nothing here."]))
    make_pptx(workspace / "specs" / "deck.pptx")
    make_vsdx(workspace / "specs" / "flow.vsdx")
    (workspace / "specs" / "paper.pdf").write_bytes(minimal_pdf(["First page", "Host address mapping on page two"]))
    (workspace / "notes.md").write_text("# Notes\n\nline two\nthe host Address is mapped\nhost again, HOST\n")
    events = await _content(api, "host address")
    files = _files(events)
    assert set(files) == {"specs/ga.docx", "specs/paper.pdf", "notes.md"}
    md = files["notes.md"]
    assert md["count"] == 1 and md["hits"][0]["line"] == 4 and md["hits"][0]["text"] == "the host Address is mapped"
    h = md["hits"][0]
    assert h["text"][h["s"]:h["e"]] == "host Address" and h["n"] == 0 and h["ctx"].endswith("line two\nthe ")
    assert files["specs/paper.pdf"]["hits"][0]["page"] == 2 and files["specs/ga.docx"]["format"] == "docx"
    done = events[-1]
    assert done["type"] == "done" and done["files"] == 3 and done["pending"] == 0 and not done["truncated"]
    # case, whole words; slides and Visio pages
    assert set(_files(await _content(api, "HOST", case="true"))) == {"notes.md"}
    assert _files(await _content(api, "host", case="true"))["notes.md"]["count"] == 2
    assert set(_files(await _content(api, "hos", word="true"))) == set()
    deck = _files(await _content(api, "slide two"))["specs/deck.pptx"]
    assert deck["hits"][0]["page"] == 2
    assert set(_files(await _content(api, "Second page text"))) == {"specs/flow.vsdx"}
    # a folder narrows it
    assert set(_files(await _content(api, "host address", path="specs"))) == {"specs/ga.docx", "specs/paper.pdf"}


async def test_extracted_text_is_kept_until_the_file_changes_and_broken_files_are_skipped(running_app, workspace: Path):
    h = await running_app()
    api = h.api
    doc = workspace / "a.docx"
    doc.write_bytes(_minimal_docx(["alpha version"]))
    (workspace / "broken.docx").write_bytes(b"not a zip")
    assert set(_files(await _content(api, "alpha"))) == {"a.docx"}
    ix = h.ctx.extra["search_index"]
    ws = next(iter(h.ctx.workspaces.runtimes))
    rows = ix._load(ws)
    assert rows["broken.docx"][3] and not rows["a.docx"][3]  # the error is kept: not tried again until it changes
    first_key = rows["a.docx"][2]
    assert ix.text_path(ws, first_key).exists()
    doc.write_bytes(_minimal_docx(["beta version"]))
    os.utime(doc, ns=(1, 1))
    assert set(_files(await _content(api, "beta"))) == {"a.docx"}
    assert set(_files(await _content(api, "alpha"))) == set()
    assert not ix.text_path(ws, first_key).exists()  # the old text is gone


async def test_linked_folders_are_listed_by_name_but_not_searched_unless_searched_in(api, workspace: Path, tmp_path: Path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "deep.md").write_text("needle in the corpus\n")
    os.symlink(corpus, workspace / "linked")
    (workspace / "here.md").write_text("needle here\n")
    events = await _content(api, "needle")
    assert set(_files(events)) == {"here.md"} and events[-1]["links"] == 1
    assert set(_files(await _content(api, "needle", path="linked"))) == {"linked/deep.md"}
    names = (await api.get("/api/v1/search/names", params={"q": "linked"})).json()["items"]
    assert [(i["path"], i["type"]) for i in names] == [("linked", "dir")]
