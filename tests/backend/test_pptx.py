"""PowerPoint (.pptx) documents: rendered to readable HTML, discussable, read-only."""

from __future__ import annotations

import io
from pathlib import Path

from conftest import Api


def _make_pptx(path: Path) -> None:
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[1])  # title + content
    s1.shapes.title.text = "Kernel performance"
    body = s1.placeholders[1].text_frame
    body.text = "Memory bound on BMG"
    p = body.add_paragraph()
    p.text = "L3 hit rate 42%"
    p.level = 1
    p2 = body.add_paragraph()
    p2.text = "Next steps"
    s1.notes_slide.notes_text_frame.text = "Mention the prefetch experiment."
    s2 = prs.slides.add_slide(prs.slide_layouts[5])  # title only
    s2.shapes.title.text = "Results"
    rows = s2.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    rows.cell(0, 0).text, rows.cell(0, 1).text = "Kernel", "GB/s"
    rows.cell(1, 0).text, rows.cell(1, 1).text = "softmax", "812"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, format="PNG")
    buf.seek(0)
    s2.shapes.add_picture(buf, Inches(5), Inches(2), Inches(1), Inches(1))
    prs.save(str(path))


async def test_pptx_renders_slides_and_supports_discussions(api: Api, workspace: Path):
    deck = workspace / "docs" / "deck.pptx"
    _make_pptx(deck)
    items = {i["name"]: i for i in (await api.get("/api/v1/files", params={"path": "docs"})).json()["items"]}
    assert items["deck.pptx"]["kind"] == "slides" and items["deck.pptx"]["format"] == "pptx" and items["deck.pptx"]["openable"] is True
    doc = await api.open_doc("docs/deck.pptx")
    assert doc["format"] == "pptx" and doc["editable"] is False and doc["text"] == ""
    h = doc["html"]
    assert h.count('<section class="slide"') == 2
    assert "<h2>Slide 1: Kernel performance</h2>" in h and "<h2>Slide 2: Results</h2>" in h
    # bullet levels become valid nested lists (sub-list inside its parent item)
    assert "<ul><li>Memory bound on BMG<ul><li>L3 hit rate 42%</li></ul></li><li>Next steps</li></ul>" in h
    assert "<th>Kernel</th><th>GB/s</th>" in h and "<td>softmax</td><td>812</td>" in h
    assert 'src="data:image/png;base64,' in h
    assert "Mention the prefetch experiment." in h and 'class="slide-notes"' in h
    # text model / anchors work like any other format
    from backend.app.textmodel import build_text_model_from_html

    model = build_text_model_from_html(h).text
    assert "Slide 1: Kernel performance" in model and "L3 hit rate 42%" in model and "812" in model
    q = "L3 hit rate 42%"
    i = model.index(q)
    r = await api.post(f"/api/v1/documents/{doc['document_id']}/threads", json={
        "version_sha256": doc["sha256"], "quote": q, "prefix": model[max(0, i - 40):i], "suffix": model[i + len(q):i + len(q) + 40],
        "start": i, "end": i + len(q), "question": "Is 42% low?"})
    assert r.status_code == 201, r.text
    ev = await api.wait_job(r.json()["job_id"])
    assert ev[-1]["type"] == "completed"
    t = (await api.threads(doc["document_id"]))["threads"][0]
    assert t["anchor_state"] == "attached" and t["anchor"]["heading"] == "Slide 1: Kernel performance"
    # read-only
    r = await api.put(f"/api/v1/documents/{doc['document_id']}/content", json={"text": "x", "base_sha256": doc["sha256"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "NOT_EDITABLE"
    # a corrupt file is reported, not crashed on
    (workspace / "docs" / "broken.pptx").write_bytes(b"not a zip")
    r = await api.get("/api/v1/documents/open", params={"path": "docs/broken.pptx"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "RENDER_FAILED"


async def test_pptx_diff_compares_slide_text(api: Api, workspace: Path):
    import subprocess

    from pptx import Presentation

    deck = workspace / "docs" / "deck.pptx"
    _make_pptx(deck)
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "deck"], cwd=workspace, check=True)
    prs = Presentation(str(deck))
    prs.slides[0].shapes.title.text = "Kernel performance (revised)"
    prs.save(str(deck))
    d = (await api.get("/api/v1/diff", params={"path": "docs/deck.pptx"})).json()
    f = d["files"][0]
    assert f["commentable"] is True
    assert "-Slide 1: Kernel performance\n" in f["unified"] and "+Slide 1: Kernel performance (revised)\n" in f["unified"]
    assert "PK\x03\x04" not in f["unified"]  # never raw zip bytes
