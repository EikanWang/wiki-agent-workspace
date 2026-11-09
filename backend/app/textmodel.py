"""Server-side Markdown rendering and the deterministic rendered-text model (DESIGN §10).

The browser builds the same model from the sanitized DOM. Both sides follow identical
rules: concatenate text nodes in document order, insert one newline between adjacent
block elements, normalize CRLF/CR to LF. Offsets are UTF-16 code units.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from pathlib import Path

from markdown_it import MarkdownIt

BLOCK_TAGS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "pre", "blockquote",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "hr", "section", "article", "dl", "dt", "dd",
    "figure", "figcaption", "details", "summary",
}
VOID_TAGS = {"br", "hr", "img", "input", "wbr", "source", "col", "area", "base", "embed", "link", "meta", "param", "track"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# Whitespace-only text nodes directly inside these containers (or at the root) are layout
# artefacts of the HTML serialization, not document text, and are ignored on both sides.
CONTAINER_TAGS = {"div", "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "blockquote", "dl", "dd",
                  "section", "article", "figure", "details"}

_md: MarkdownIt | None = None


def _slugify(text: str) -> str:
    """GitHub-style heading slug (github-slugger): lowercase, drop punctuation, each space -> '-'."""
    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = s.replace(" ", "-")
    return s or "section"


def markdown_renderer() -> MarkdownIt:
    global _md
    if _md is None:
        md = MarkdownIt("gfm-like", {"html": False, "linkify": False, "typographer": False})
        md.enable("table")
        md.enable("strikethrough")

        def render_heading_open(self, tokens, idx, options, env):
            tok = tokens[idx]
            inline = tokens[idx + 1]
            text = "".join(t.content for t in (inline.children or []) if t.type in ("text", "code_inline"))
            slug = _slugify(text)
            counts = env.setdefault("slugs", {})
            n = counts.get(slug, 0)
            counts[slug] = n + 1
            if n:
                slug = f"{slug}-{n}"
            tok.attrSet("id", slug)
            return self.renderToken(tokens, idx, options, env)

        md.add_render_rule("heading_open", render_heading_open)
        _md = md
    return _md


def render_markdown(text: str) -> str:
    return markdown_renderer().render(text, {})


# ---------------------------------------------------------------------- other formats

TEXT_SUFFIXES = {
    ".txt", ".rst", ".adoc", ".org", ".log", ".tex", ".bib", ".csv", ".tsv",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm", ".css", ".svg",
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".cu", ".cuh",
    ".rs", ".go", ".java", ".kt", ".kts", ".swift", ".rb", ".php", ".cs", ".scala", ".lua", ".pl", ".pm", ".r", ".m", ".mm", ".cl",
    ".glsl", ".hlsl", ".asm", ".s", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd", ".sql", ".proto", ".cmake", ".mk",
    ".gradle", ".groovy", ".dart", ".zig", ".nim", ".ex", ".exs", ".erl", ".hs", ".ml", ".mli", ".clj", ".lisp", ".el", ".vim",
    ".dockerfile", ".env", ".gitignore", ".gitattributes", ".editorconfig", ".ipynb", ".diff", ".patch",
}
TEXT_NAMES = {"makefile", "dockerfile", "cmakelists.txt", "license", "readme", "changelog", "authors", "copying", "notice"}
LANG_BY_SUFFIX = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx",
    ".jsx": "jsx", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp", ".cu": "cuda",
    ".cuh": "cuda", ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin", ".swift": "swift", ".rb": "ruby", ".php": "php", ".cs": "csharp",
    ".scala": "scala", ".lua": "lua", ".pl": "perl", ".r": "r", ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".ps1": "powershell",
    ".sql": "sql", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".xml": "xml", ".html": "html", ".css": "css",
    ".ini": "ini", ".cfg": "ini", ".conf": "ini", ".cmake": "cmake", ".proto": "protobuf", ".diff": "diff", ".patch": "diff", ".tex": "latex",
}


# Shown in their original layout only (rendered to PDF by LibreOffice): no text model, so discussions attach to the
# whole document, as for PDFs. .docx / .pptx / .vsdx have a text view as well (formats "docx" / "pptx" / "visio").
LAYOUT_ONLY_SUFFIXES = {".doc", ".rtf", ".odt", ".ppt", ".pps", ".ppsx", ".odp", ".vsd", ".vsdm", ".odg"}
# formats rendered with LibreOffice for the "Layout" view
OFFICE_FORMATS = {"docx", "pptx", "visio", "office"}
# raster images: shown as pictures; discussions are about the whole image, which is attached to the agent's turn
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}
# formats without a text model (no anchored passages; discussions are about the whole document)
NO_TEXT_FORMATS = {"pdf", "office", "image"}


def document_format(name: str) -> str:
    """markdown | text | docx | pptx | visio | office | pdf | image | unsupported"""
    lower = name.lower()
    suffix = Path(lower).suffix
    if suffix in (".md", ".markdown"):
        return "markdown"
    if suffix == ".docx":
        return "docx"
    if suffix == ".pptx":
        return "pptx"
    if suffix == ".vsdx":
        return "visio"
    if suffix in LAYOUT_ONLY_SUFFIXES:
        return "office"
    if suffix == ".pdf":
        return "pdf"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in TEXT_SUFFIXES or lower in TEXT_SUFFIXES or lower in TEXT_NAMES or (not suffix and lower.split(".")[0] in TEXT_NAMES):
        return "text"
    return "unsupported"


def render_text_as_code(text: str, name: str) -> str:
    """Plain/source text is shown as one fenced code block (line numbers are added by the browser)."""
    lang = LANG_BY_SUFFIX.get(Path(name.lower()).suffix, "")
    fence = "`" * 3
    while fence in text:
        fence += "`"
    return markdown_renderer().render(f"{fence}{lang}\n{text}\n{fence}\n", {})


def render_docx(data: bytes) -> str:
    import io

    import mammoth

    result = mammoth.convert_to_html(io.BytesIO(data))
    return result.value  # sanitized by DOMPurify in the browser; server model is built from the same HTML


MAX_INLINE_IMAGE = 4 * 1024 * 1024  # larger slide images are replaced by a placeholder


def render_pptx(data: bytes) -> str:
    """PowerPoint (.pptx) → HTML: one <section> per slide with its title as a heading, text frames (paragraphs and
    bullet levels), tables, pictures (inline data URIs) and speaker notes. Shapes are read top-to-bottom,
    left-to-right, which is the usual reading order. Sanitized by DOMPurify in the browser."""
    import base64
    import html
    import io

    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from pptx.util import Emu

    prs = Presentation(io.BytesIO(data))
    esc = html.escape

    def para_html(paragraphs) -> str:
        out: list[str] = []
        items: list[tuple[int, str]] = []

        def flush() -> None:
            """Emit collected (level, html) items as properly nested lists (sub-lists live inside their <li>)."""
            if not items:
                return

            def build(start: int, level: int) -> tuple[str, int]:
                buf = ["<ul>"]
                i = start
                while i < len(items):
                    lv, txt = items[i]
                    if lv < level:
                        break
                    if lv > level:  # deeper item without a parent at this level: nest under an empty item
                        sub, i = build(i, level + 1)
                        buf.append(f"<li>{sub}</li>")
                        continue
                    i += 1
                    sub = ""
                    if i < len(items) and items[i][0] > level:
                        sub, i = build(i, level + 1)
                    buf.append(f"<li>{txt}{sub}</li>")
                buf.append("</ul>")
                return "".join(buf), i

            base = min(lv for lv, _ in items)
            html_list, _ = build(0, base)
            out.append(html_list)
            items.clear()

        for p in paragraphs:
            txt = "".join(
                (f"<strong>{esc(r.text)}</strong>" if r.font.bold else f"<em>{esc(r.text)}</em>" if r.font.italic else esc(r.text))
                for r in p.runs
            ) or esc(p.text)
            if not txt.strip():
                flush()
                continue
            if p.level > 0 or len(paragraphs) > 1:
                items.append((min(p.level, 8), txt))
            else:
                flush()
                out.append(f"<p>{txt}</p>")
        flush()
        return "".join(out)

    def shape_html(shape, title_shape) -> str:
        if shape == title_shape:
            return ""
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            return "".join(shape_html(s, title_shape) for s in sorted(shape.shapes, key=_pos))
        if getattr(shape, "has_table", False) and shape.has_table:
            rows = []
            for r_i, row in enumerate(shape.table.rows):
                tag = "th" if r_i == 0 else "td"
                rows.append("<tr>" + "".join(f"<{tag}>{esc(c.text)}</{tag}>" for c in row.cells) + "</tr>")
            return "<table>" + "".join(rows) + "</table>"
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            try:
                blob = shape.image.blob
                ctype = shape.image.content_type or "image/png"
            except Exception:
                return ""
            alt = esc(getattr(shape, "name", "") or "picture")
            if len(blob) > MAX_INLINE_IMAGE or not ctype.startswith("image/"):
                return f"<p><em>[image: {alt}]</em></p>"
            return f'<p><img alt="{alt}" src="data:{ctype};base64,{base64.b64encode(blob).decode()}"></p>'
        if getattr(shape, "has_text_frame", False) and shape.has_text_frame and shape.text_frame.text.strip():
            return para_html(shape.text_frame.paragraphs)
        return ""

    def _pos(sh) -> tuple[int, int]:
        return (int(sh.top or Emu(0)), int(sh.left or Emu(0)))

    parts: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        title_shape = slide.shapes.title if slide.shapes.title is not None and slide.shapes.title.has_text_frame else None
        title = title_shape.text_frame.text.strip() if title_shape is not None else ""
        heading = f"Slide {i}" + (f": {title}" if title else "")
        body = "".join(shape_html(sh, title_shape) for sh in sorted(slide.shapes, key=_pos))
        notes = ""
        if slide.has_notes_slide:
            nt = slide.notes_slide.notes_text_frame.text.strip() if slide.notes_slide.notes_text_frame is not None else ""
            if nt:
                notes = '<div class="slide-notes"><p><strong>Notes</strong></p>' + "".join(f"<p>{esc(ln)}</p>" for ln in nt.splitlines() if ln.strip()) + "</div>"
        parts.append(f'<section class="slide" data-slide="{i}"><h2>{esc(heading)}</h2>{body or "<p><em>(no text)</em></p>"}{notes}</section>')
    if not parts:
        return "<p><em>(empty presentation)</em></p>"
    return "".join(parts)


def render_document(fmt: str, data: bytes, name: str) -> str:
    """HTML for any supported document format; raises UnicodeDecodeError for non-UTF-8 text."""
    if fmt == "markdown":
        return render_markdown(data.decode("utf-8"))
    if fmt == "text":
        return render_text_as_code(data.decode("utf-8"), name)
    if fmt == "docx":
        return render_docx(data)
    if fmt == "pptx":
        return render_pptx(data)
    if fmt == "visio":
        return render_visio(data)
    raise ValueError(f"unsupported document format: {fmt}")


VISIO_MAX_XML_BYTES = 64 * 1024 * 1024  # total uncompressed page XML read from one .vsdx (zip-bomb guard)


def render_visio(data: bytes) -> str:
    """Visio (.vsdx) → HTML for the text view: one <section> per foreground page ("Page N: name") listing the text
    of its shapes, top-to-bottom then left-to-right (grouped shapes keep their order). The faithful drawing is the
    Layout view (LibreOffice). Sanitized by DOMPurify in the browser."""
    import html
    import io
    import posixpath
    import zipfile
    import xml.etree.ElementTree as ET

    ns = {"v": "http://schemas.microsoft.com/office/visio/2012/main"}
    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    z = zipfile.ZipFile(io.BytesIO(data))
    budget = [VISIO_MAX_XML_BYTES]

    def read_xml(name: str) -> ET.Element:
        info = z.getinfo(name)
        budget[0] -= info.file_size
        if budget[0] < 0:
            raise ValueError("diagram is too large to extract text from")
        raw = z.read(name)
        if b"<!DOCTYPE" in raw[:2048] or b"<!ENTITY" in raw:
            raise ValueError("unexpected DTD in diagram XML")
        return ET.fromstring(raw)

    def cell(shape: ET.Element, name: str) -> float:
        c = shape.find(f"v:Cell[@N='{name}']", ns)
        try:
            return float(c.get("V", "0")) if c is not None else 0.0
        except ValueError:
            return 0.0

    def shape_text(shape: ET.Element) -> str:
        t = shape.find("v:Text", ns)
        if t is None:
            return ""
        return re.sub(r"[ \t]+", " ", "".join(t.itertext()).replace("\r\n", "\n").replace("\r", "\n")).strip()

    def walk(shapes: ET.Element | None, out: list[str], top: bool) -> None:
        if shapes is None:
            return
        items = shapes.findall("v:Shape", ns)
        if top:  # reading order for the page's own shapes; a group's members keep their stacking order
            items.sort(key=lambda sh: (-round(cell(sh, "PinY"), 1), cell(sh, "PinX")))
        for sh in items:
            txt = shape_text(sh)
            if txt and (not out or out[-1] != txt):
                out.append(txt)
            walk(sh.find("v:Shapes", ns), out, False)

    pages_xml = read_xml("visio/pages/pages.xml")
    rels: dict[str, str] = {}
    try:
        rel_root = read_xml("visio/pages/_rels/pages.xml.rels")
        for r in rel_root:
            rels[r.get("Id", "")] = posixpath.normpath(posixpath.join("visio/pages", r.get("Target", "")))
    except KeyError:
        pass
    parts: list[str] = []
    n = 0
    for page in pages_xml.findall("v:Page", ns):
        if page.get("Background") == "1":
            continue  # background pages are drawn behind others, not read on their own
        rel = page.find("v:Rel", ns)
        target = rels.get(rel.get(rel_ns, "") if rel is not None else "")
        if not target or target not in z.namelist():
            continue
        n += 1
        name = page.get("Name") or page.get("NameU") or ""
        texts: list[str] = []
        walk(read_xml(target).find("v:Shapes", ns), texts, True)
        title = f"Page {n}" + (f": {name}" if name else "")
        body = "".join(f"<li>{html.escape(t).replace(chr(10), '<br>')}</li>" for t in texts) if texts else ""
        parts.append(f'<section class="slide diagram-page"><h2>{html.escape(title)}</h2>'
                     + (f"<ul>{body}</ul>" if body else '<p class="muted">(no text on this page)</p>') + "</section>")
    return "".join(parts) or '<p class="muted">(no pages)</p>'



@dataclass
class Heading:
    level: int
    text: str
    start: int  # model offset of heading text start


@dataclass
class TextModel:
    text: str
    headings: list[Heading] = field(default_factory=list)

    def heading_for(self, offset: int) -> str | None:
        best: Heading | None = None
        for h in self.headings:
            if h.start <= offset:
                best = h
            else:
                break
        return best.text if best else None


class _ModelBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.length = 0  # UTF-16 length
        self.headings: list[Heading] = []
        self._heading_stack: list[tuple[int, int]] = []  # (level, start)
        self._heading_text: list[list[str]] = []
        self._stack: list[str] = []
        self._pending_br = False

    def _append(self, s: str) -> None:
        if not s:
            return
        self.parts.append(s)
        self.length += utf16_len(s)
        for buf in self._heading_text:
            buf.append(s)

    def _ends_with_newline(self) -> bool:
        return not self.parts or self.parts[-1].endswith("\n")

    def _block_boundary(self) -> None:
        if self.parts and not self._ends_with_newline():
            self.parts.append("\n")
            self.length += 1

    def _flush_br(self) -> None:
        if self._pending_br:
            self._pending_br = False
            self.parts.append("\n")
            self.length += 1

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            # <br> becomes one newline; a serialized newline right after it is not doubled.
            self._flush_br()
            self._pending_br = True
            return
        self._flush_br()
        if tag not in VOID_TAGS:
            self._stack.append(tag)
        if tag in BLOCK_TAGS:
            self._block_boundary()
            if tag in HEADING_TAGS:
                self._heading_stack.append((int(tag[1]), self.length))
                self._heading_text.append([])
        if tag in VOID_TAGS and tag in BLOCK_TAGS:
            # hr: block boundary only
            pass

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self._flush_br()
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()
        elif tag in self._stack:
            while self._stack and self._stack[-1] != tag:
                self._stack.pop()
            if self._stack:
                self._stack.pop()
        if tag in BLOCK_TAGS:
            if tag in HEADING_TAGS and self._heading_stack:
                level, start = self._heading_stack.pop()
                buf = self._heading_text.pop()
                self.headings.append(Heading(level=level, text="".join(buf).strip(), start=start))
            self._block_boundary()

    def handle_data(self, data):
        data = data.replace("\r\n", "\n").replace("\r", "\n")
        parent = self._stack[-1] if self._stack else None
        if data.strip() == "" and "\n" in data and (parent is None or parent in CONTAINER_TAGS):
            return
        if self._pending_br:
            self._pending_br = False
            if not data.startswith("\n"):
                data = "\n" + data
        self._append(data)


def build_text_model_from_html(html: str) -> TextModel:
    b = _ModelBuilder()
    b.feed(html)
    b.close()
    return TextModel(text="".join(b.parts), headings=b.headings)


def build_text_model(markdown: str) -> TextModel:
    return build_text_model_from_html(render_markdown(markdown))


# ---------------------------------------------------------------------- UTF-16 helpers

def utf16_len(s: str) -> int:
    n = 0
    for ch in s:
        n += 2 if ord(ch) > 0xFFFF else 1
    return n


def utf16_index_map(s: str) -> list[int]:
    """Returns list mapping Python index -> UTF-16 offset, with a trailing total."""
    out = [0]
    acc = 0
    for ch in s:
        acc += 2 if ord(ch) > 0xFFFF else 1
        out.append(acc)
    return out


class Utf16Text:
    """Helper to slice a Python str by UTF-16 code-unit offsets."""

    def __init__(self, text: str):
        self.text = text
        self.map = utf16_index_map(text)
        self.length = self.map[-1]
        self._rev: dict[int, int] | None = None

    def _reverse(self) -> dict[int, int]:
        if self._rev is None:
            self._rev = {u: i for i, u in enumerate(self.map)}
        return self._rev

    def py_index(self, u16: int) -> int | None:
        """Python index for a UTF-16 offset; None if it splits a surrogate pair or is out of range."""
        if u16 < 0 or u16 > self.length:
            return None
        return self._reverse().get(u16)

    def slice(self, start: int, end: int) -> str | None:
        a = self.py_index(start)
        b = self.py_index(end)
        if a is None or b is None or a > b:
            return None
        return self.text[a:b]

    def u16_offset(self, py_index: int) -> int:
        return self.map[py_index]
