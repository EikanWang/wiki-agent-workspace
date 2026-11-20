"""Text of Word / PowerPoint / Visio / PDF files for the folder search, extracted in worker processes.

Run as `python -m backend.app.search_extract`: one job per line on stdin (`{"src", "fmt", "out"}`), one answer per
line on stdout (`{"ok": true, "chars": n}` or `{"ok": false, "error": "..."}`). The text is written to `out`; pages
and slides are separated by form feeds (\\f) so a match can tell its page. A separate process keeps a slow or broken
file (and the GIL) away from the server, and it can be stopped when one takes too long.
"""

from __future__ import annotations

import json
import os
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
MAX_XML_BYTES = 512 * 1024 * 1024  # uncompressed XML read from one file (zip-bomb guard)


def _xml_parts(z: zipfile.ZipFile, names: list[str]) -> list[str]:
    total = 0
    out = []
    for n in names:
        total += z.getinfo(n).file_size
        if total > MAX_XML_BYTES:
            break
        out.append(n)
    return out


def _paragraphs(z: zipfile.ZipFile, name: str, ns: str, t: str, para: str, breaks: tuple[str, ...], tab: str | None) -> list[str]:
    lines: list[str] = []
    buf: list[str] = []
    with z.open(name) as f:
        for _, el in ET.iterparse(f, events=("end",)):
            tag = el.tag
            if tag == ns + t:
                buf.append(el.text or "")
            elif tab and tag == ns + tab:
                buf.append("\t")
            elif tag in breaks:
                buf.append("\n")
            elif tag == ns + para:
                lines.append("".join(buf))
                buf = []
                el.clear()
    if buf:
        lines.append("".join(buf))
    return lines


def docx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        parts = [n for n in ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml") if n in names]
        lines: list[str] = []
        for n in _xml_parts(z, parts):
            lines += _paragraphs(z, n, W, "t", "p", (W + "br", W + "cr"), "tab")
    return "\n".join(lines)


def pptx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[-1]))
        return "\f".join("\n".join(_paragraphs(z, n, A, "t", "p", (A + "br",), None)) for n in _xml_parts(z, slides))


def visio_text(path: str) -> str:
    from .textmodel import build_text_model_from_html, render_visio

    return build_text_model_from_html(render_visio(Path(path).read_bytes())).text


def pdf_text(path: str) -> str:
    from pypdf import PdfReader

    pages = []
    for page in PdfReader(path).pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # one bad page does not lose the others
            pages.append("")
    return "\f".join(pages)


EXTRACTORS = {"docx": docx_text, "pptx": pptx_text, "visio": visio_text, "pdf": pdf_text}


def extract(src: str, fmt: str) -> str:
    return EXTRACTORS[fmt](src).replace("\0", "")


def main() -> None:
    try:
        os.nice(10)  # indexing never competes with the server or the user's work
    except OSError:
        pass
    for line in sys.stdin:
        try:
            job = json.loads(line)
            text = extract(job["src"], job["fmt"])
            out = Path(job["out"])
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp")
            tmp.write_text(text, "utf-8")
            os.replace(tmp, out)
            answer = {"ok": True, "chars": len(text)}
        except Exception as exc:
            answer = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        sys.stdout.write(json.dumps(answer) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
