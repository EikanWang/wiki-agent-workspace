"""Layout view: Office files rendered to PDF by LibreOffice (real soffice; skipped if it is not installed),
and the Visio text view."""

from __future__ import annotations

import asyncio
import io
import shutil
import time
import zipfile
from pathlib import Path

import pytest

from backend.app.office import OfficeRenderer
from backend.app.textmodel import build_text_model_from_html, document_format, render_visio

requires_soffice = pytest.mark.skipif(not (shutil.which("soffice") or shutil.which("libreoffice")), reason="LibreOffice not installed")


def make_docx(path: Path, title: str = "Layout title") -> None:
    from test_locks_formats import _minimal_docx

    path.write_bytes(_minimal_docx([title, "Body text of the word document."]))


def make_pptx(path: Path) -> None:
    from pptx import Presentation

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Slide one"
    s.placeholders[1].text_frame.text = "A bullet"
    prs.slides.add_slide(prs.slide_layouts[5]).shapes.title.text = "Slide two"
    prs.save(path)


def make_vsdx(path: Path) -> None:
    """Minimal Visio package: two foreground pages and one background page, shapes with text."""
    ns = "http://schemas.microsoft.com/office/visio/2012/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

    def shape(i, text, x, y, inner=""):
        return (f"<Shape ID='{i}' Type='Shape'><Cell N='PinX' V='{x}'/><Cell N='PinY' V='{y}'/>"
                f"{inner}<Text><cp IX='0'/>{text}</Text></Shape>")

    page1 = (f"<PageContents xmlns='{ns}'><Shapes>"
             + shape(1, "Bottom left", 1, 1) + shape(2, "Top right", 5, 9) + shape(3, "Top left", 1, 9)
             + f"<Shape ID='4' Type='Group'><Cell N='PinX' V='3'/><Cell N='PinY' V='5'/><Shapes>{shape(5, 'Grouped A', 0, 0)}{shape(6, 'Grouped B', 0, 0)}</Shapes></Shape>"
             + "</Shapes></PageContents>")
    page2 = f"<PageContents xmlns='{ns}'><Shapes>{shape(1, 'Second page text', 1, 1)}</Shapes></PageContents>"
    bg = f"<PageContents xmlns='{ns}'><Shapes>{shape(1, 'BACKGROUND', 1, 1)}</Shapes></PageContents>"
    pages = (f"<Pages xmlns='{ns}' xmlns:r='{r}'>"
             f"<Page ID='0' Name='Overview'><Rel r:id='rId1'/></Page>"
             f"<Page ID='1' Name='Bg' Background='1'><Rel r:id='rId3'/></Page>"
             f"<Page ID='2' NameU='Details'><Rel r:id='rId2'/></Page></Pages>")
    rels = ("<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
            "<Relationship Id='rId1' Type='page' Target='page1.xml'/><Relationship Id='rId2' Type='page' Target='page2.xml'/>"
            "<Relationship Id='rId3' Type='page' Target='page3.xml'/></Relationships>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("visio/pages/pages.xml", pages)
        z.writestr("visio/pages/_rels/pages.xml.rels", rels)
        z.writestr("visio/pages/page1.xml", page1)
        z.writestr("visio/pages/page2.xml", page2)
        z.writestr("visio/pages/page3.xml", bg)


def test_formats():
    assert [document_format(n) for n in ["a.docx", "a.pptx", "a.vsdx", "a.vsd", "a.doc", "a.ppt", "a.odg", "a.pdf"]] == [
        "docx", "pptx", "visio", "office", "office", "office", "office", "pdf"]


def test_visio_text_view(tmp_path: Path):
    f = tmp_path / "d.vsdx"
    make_vsdx(f)
    model = build_text_model_from_html(render_visio(f.read_bytes()))
    assert [h.text for h in model.headings] == ["Page 1: Overview", "Page 2: Details"]  # background page skipped
    t = model.text
    # reading order: top row left to right, then lower shapes; group members after their group
    assert t.index("Top left") < t.index("Top right") < t.index("Grouped A") < t.index("Grouped B") < t.index("Bottom left")
    assert "BACKGROUND" not in t and "Second page text" in t


def test_visio_rejects_dtd(tmp_path: Path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("visio/pages/pages.xml", "<!DOCTYPE x [<!ENTITY a 'b'>]><Pages/>")
    with pytest.raises(ValueError):
        render_visio(buf.getvalue())


def test_unavailable_without_libreoffice(settings, tmp_path: Path):
    r = OfficeRenderer(settings)
    r.exe = None
    f = tmp_path / "a.docx"
    make_docx(f)
    st = r.status(f)
    assert st["state"] == "unavailable" and "LibreOffice" in st["message"]


async def _poll(api, path: str, timeout: float = 120, **params) -> dict:
    end = time.time() + timeout
    q = "&".join(f"{k}={v}" for k, v in params.items())
    while time.time() < end:
        r = await api.get(f"/api/v1/office/render?path={path}" + (f"&{q}" if q else ""))
        assert r.status_code == 200, r.text
        st = r.json()
        params = {}  # retry only once
        q = ""
        if st["state"] != "pending":
            return st
        await asyncio.sleep(0.5)
    raise AssertionError("render did not finish")


@requires_soffice
async def test_layout_render_docx_pptx_cached_and_wiki_untouched(api, workspace: Path, settings):
    make_docx(workspace / "docs" / "report.docx")
    make_pptx(workspace / "docs" / "deck.pptx")
    before = sorted(p.name for p in (workspace / "docs").iterdir())
    for name in ("report.docx", "deck.pptx"):
        st = await _poll(api, f"docs/{name}")
        assert st["state"] == "ready", st
        r = await api.get(f"/api/v1/office/pdf?path=docs/{name}")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        assert r.headers["content-type"] == "application/pdf"
        # cached: the next status is immediately ready
        r2 = await api.get(f"/api/v1/office/render?path=docs/{name}")
        assert r2.json()["state"] == "ready"
    # LibreOffice never worked inside the wiki (no lock files, nothing written)
    assert sorted(p.name for p in (workspace / "docs").iterdir()) == before
    assert len(list((settings.state_dir / "renders").glob("*.pdf"))) == 2
    # a changed file is rendered again (content-addressed cache)
    make_docx(workspace / "docs" / "report.docx", title="Changed title")
    r = await api.get("/api/v1/office/render?path=docs/report.docx")
    assert r.json()["state"] in ("pending", "ready")
    assert (await _poll(api, "docs/report.docx"))["state"] == "ready"
    assert len(list((settings.state_dir / "renders").glob("*.pdf"))) == 3


async def test_layout_render_failure_is_remembered_and_retryable(running_app, workspace: Path, tmp_path: Path):
    """A conversion that fails is reported, not re-run on every poll, and runs again only on retry."""
    h = await running_app()
    fake = tmp_path / "soffice"
    runs = tmp_path / "runs"
    fake.write_text(f"#!/bin/sh\necho run >> {runs}\necho 'Error: source file could not be loaded' >&2\nexit 1\n")
    fake.chmod(0o755)
    h.ctx.office.exe = str(fake)
    make_docx(workspace / "docs" / "broken.docx")
    st = await _poll(h.api, "docs/broken.docx")
    assert st["state"] == "failed" and st["code"] == "RENDER_FAILED" and "could not be loaded" in st["message"], st
    r = await h.api.get("/api/v1/office/render?path=docs/broken.docx")
    assert r.json()["state"] == "failed"
    assert runs.read_text().count("run") == 1  # remembered: LibreOffice is not run again
    r = await h.api.get("/api/v1/office/render?path=docs/broken.docx&retry=true")
    assert r.json()["state"] == "pending"
    assert (await _poll(h.api, "docs/broken.docx"))["state"] == "failed"
    assert runs.read_text().count("run") == 2
    assert (await h.api.get("/api/v1/office/pdf?path=docs/broken.docx")).status_code == 409


async def test_layout_render_timeout_kills_libreoffice(running_app, workspace: Path, tmp_path: Path):
    h = await running_app()
    fake = tmp_path / "soffice"
    fake.write_text("#!/bin/sh\nsleep 600\n")
    fake.chmod(0o755)
    h.ctx.office.exe = str(fake)
    h.ctx.office.timeout = 1
    make_docx(workspace / "docs" / "slow.docx")
    t0 = time.time()
    st = await _poll(h.api, "docs/slow.docx", timeout=20)
    assert st["state"] == "failed" and "did not finish" in st["message"] and time.time() - t0 < 15


async def test_layout_endpoints_validate(api, workspace: Path):
    assert (await api.get("/api/v1/office/render?path=docs/guide.md")).status_code == 400
    assert (await api.get("/api/v1/office/render?path=docs/none.docx")).status_code == 404
    assert (await api.get("/api/v1/office/render?path=../etc/x.docx")).status_code == 400


async def test_open_visio_and_layout_only_formats(api, workspace: Path):
    make_vsdx(workspace / "docs" / "diagram.vsdx")
    d = (await api.get("/api/v1/documents/open?path=docs/diagram.vsdx")).json()
    assert d["format"] == "visio" and "Page 1: Overview" in d["html"]
    (workspace / "docs" / "old.vsd").write_bytes(b"\xd0\xcf\x11\xe0binary")
    d = (await api.get("/api/v1/documents/open?path=docs/old.vsd")).json()
    assert d["format"] == "office" and d["html"] is None
    items = (await api.get("/api/v1/files?path=docs")).json()["items"]
    kinds = {i["name"]: (i["kind"], i["openable"]) for i in items}
    assert kinds["diagram.vsdx"] == ("diagram", True) and kinds["old.vsd"] == ("diagram", True)
