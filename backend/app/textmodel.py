"""Server-side Markdown rendering and the deterministic rendered-text model (DESIGN §10).

The browser builds the same model from the sanitized DOM. Both sides follow identical
rules: concatenate text nodes in document order, insert one newline between adjacent
block elements, normalize CRLF/CR to LF. Offsets are UTF-16 code units.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

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
