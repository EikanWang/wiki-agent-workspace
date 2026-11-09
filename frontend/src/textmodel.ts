// Deterministic rendered-text model (DESIGN §10), identical to backend/app/textmodel.py:
// concatenate text nodes in document order, one newline between adjacent block elements,
// <br> = one newline (not doubled with a following serialized newline), CRLF/CR -> LF,
// whitespace-only text nodes directly inside block containers are ignored.
// Offsets are UTF-16 code units (JavaScript string indices).

const BLOCK_TAGS = new Set([
  "P", "DIV", "H1", "H2", "H3", "H4", "H5", "H6", "LI", "UL", "OL", "PRE", "BLOCKQUOTE", "TABLE", "THEAD", "TBODY", "TFOOT",
  "TR", "TD", "TH", "HR", "SECTION", "ARTICLE", "DL", "DT", "DD", "FIGURE", "FIGCAPTION", "DETAILS", "SUMMARY",
]);
const CONTAINER_TAGS = new Set(["DIV", "UL", "OL", "LI", "TABLE", "THEAD", "TBODY", "TFOOT", "TR", "BLOCKQUOTE", "DL", "DD", "SECTION", "ARTICLE", "FIGURE", "DETAILS"]);
const HEADING_TAGS = new Set(["H1", "H2", "H3", "H4", "H5", "H6"]);

export interface NodeSpan {
  node: Text;
  start: number; // model offset of node text start
  end: number;
}

export interface HeadingInfo {
  text: string;
  start: number;
  level: number;
  element: HTMLElement;
}

export interface TextModel {
  text: string;
  spans: NodeSpan[];
  headings: HeadingInfo[];
}

export function buildTextModel(root: HTMLElement): TextModel {
  const parts: string[] = [];
  let length = 0;
  const spans: NodeSpan[] = [];
  const headings: HeadingInfo[] = [];
  let pendingBr = false;

  const endsWithNewline = () => parts.length === 0 || parts[parts.length - 1].endsWith("\n");
  const append = (s: string) => {
    if (!s) return;
    parts.push(s);
    length += s.length;
  };
  const blockBoundary = () => {
    if (parts.length && !endsWithNewline()) append("\n");
  };
  const flushBr = () => {
    if (pendingBr) {
      pendingBr = false;
      append("\n");
    }
  };

  const walk = (node: Node, parentTag: string | null) => {
    if (node.nodeType === Node.TEXT_NODE) {
      const t = node as Text;
      const data = t.data.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      if (data.trim() === "" && data.includes("\n") && (parentTag === null || CONTAINER_TAGS.has(parentTag))) {
        return;
      }
      if (pendingBr) {
        pendingBr = false;
        // a serialized "\n" right after <br> *is* the <br>'s newline; otherwise emit one
        if (!data.startsWith("\n")) append("\n");
      }
      const start = length;
      append(data);
      spans.push({ node: t, start, end: length });
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node as HTMLElement;
    const tag = el.tagName;
    if (tag === "BR") {
      flushBr();
      pendingBr = true;
      return;
    }
    flushBr();
    let headingStart = -1;
    if (BLOCK_TAGS.has(tag)) {
      blockBoundary();
      if (HEADING_TAGS.has(tag)) headingStart = length;
    }
    for (const child of Array.from(el.childNodes)) walk(child, tag);
    flushBr();
    if (BLOCK_TAGS.has(tag)) {
      if (HEADING_TAGS.has(tag)) {
        headings.push({ text: (el.textContent || "").trim(), start: headingStart, level: Number(tag[1]), element: el });
      }
      blockBoundary();
    }
  };
  for (const child of Array.from(root.childNodes)) walk(child, null);
  flushBr();
  return { text: parts.join(""), spans, headings };
}

export function headingFor(model: TextModel, offset: number): HeadingInfo | null {
  let best: HeadingInfo | null = null;
  for (const h of model.headings) {
    if (h.start <= offset) best = h;
    else break;
  }
  return best;
}

/** Model offset for a DOM position (node, offset). Returns null when outside the model. */
export function domToOffset(model: TextModel, node: Node, offset: number): number | null {
  if (node.nodeType === Node.TEXT_NODE) {
    const span = model.spans.find((s) => s.node === node);
    if (!span) {
      // text node not represented (whitespace-only between blocks): map to the nearest following span
      return nearestOffsetAfter(model, node);
    }
    const t = node as Text;
    // normalize CRLF: compute offset in normalized data
    const before = t.data.slice(0, offset).replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    return Math.min(span.start + before.length, span.end);
  }
  if (node.nodeType === Node.ELEMENT_NODE) {
    const el = node as Element;
    const children = el.childNodes;
    if (offset < children.length) {
      return firstOffsetWithin(model, children[offset]) ?? nearestOffsetAfter(model, children[offset]);
    }
    // position after the last child: end of the last span inside the element
    let last: NodeSpan | null = null;
    for (const s of model.spans) if (el.contains(s.node)) last = s;
    if (last) return last.end;
    return nearestOffsetAfter(model, el);
  }
  return null;
}

function firstOffsetWithin(model: TextModel, node: Node): number | null {
  for (const s of model.spans) if (node === s.node || node.contains(s.node)) return s.start;
  return null;
}

function nearestOffsetAfter(model: TextModel, node: Node): number | null {
  // walk following nodes in document order until a represented text node is found
  for (const s of model.spans) {
    const cmp = node.compareDocumentPosition(s.node);
    if (cmp & Node.DOCUMENT_POSITION_FOLLOWING) return s.start;
  }
  return model.text.length;
}

/** DOM position for a model offset. */
export function offsetToDom(model: TextModel, offset: number, preferEnd = false): { node: Text; offset: number } | null {
  for (let i = 0; i < model.spans.length; i++) {
    const s = model.spans[i];
    const inside = preferEnd ? offset > s.start && offset <= s.end : offset >= s.start && offset < s.end;
    if (inside || (offset === s.end && i === model.spans.length - 1)) {
      const rel = offset - s.start;
      // map normalized index back to raw data index (CR handling)
      return { node: s.node, offset: rawIndex(s.node.data, rel) };
    }
  }
  if (model.spans.length && offset === 0) return { node: model.spans[0].node, offset: 0 };
  return null;
}

function rawIndex(data: string, normalizedIndex: number): number {
  if (!data.includes("\r")) return normalizedIndex;
  let n = 0;
  for (let i = 0; i < data.length; i++) {
    if (n === normalizedIndex) return i;
    if (data[i] === "\r" && data[i + 1] === "\n") continue;
    n++;
  }
  return data.length;
}

export function rangeFromOffsets(model: TextModel, start: number, end: number): Range | null {
  const a = offsetToDom(model, start, false);
  const b = offsetToDom(model, end, true);
  if (!a || !b) return null;
  const r = document.createRange();
  try {
    r.setStart(a.node, a.offset);
    r.setEnd(b.node, b.offset);
  } catch {
    return null;
  }
  return r;
}

export interface SelectionAnchor {
  quote: string;
  prefix: string;
  suffix: string;
  start: number;
  end: number;
  heading: string | null;
  /** a question about the document as a whole: no passage (quote empty) */
  whole_document?: boolean;
}

/** The "anchor" of a question about the whole document. */
export const wholeDocument = (): SelectionAnchor => ({ quote: "", prefix: "", suffix: "", start: 0, end: 0, heading: null, whole_document: true });

export function anchorFromSelection(model: TextModel, root: HTMLElement, sel: Selection): SelectionAnchor | null {
  if (sel.rangeCount === 0 || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null;
  const s = domToOffset(model, range.startContainer, range.startOffset);
  const e = domToOffset(model, range.endContainer, range.endOffset);
  if (s === null || e === null) return null;
  let start = Math.min(s, e);
  let end = Math.max(s, e);
  // trim surrounding whitespace/newlines from the selection
  while (start < end && /\s/.test(model.text[start])) start++;
  while (end > start && /\s/.test(model.text[end - 1])) end--;
  if (end <= start) return null;
  // never split surrogate pairs
  if (isLowSurrogate(model.text.charCodeAt(start))) start--;
  if (isHighSurrogate(model.text.charCodeAt(end - 1))) end++;
  return makeAnchor(model, start, end);
}

export function makeAnchor(model: TextModel, start: number, end: number): SelectionAnchor {
  const quote = model.text.slice(start, end);
  return {
    quote,
    prefix: model.text.slice(Math.max(0, start - 40), start),
    suffix: model.text.slice(end, end + 40),
    start,
    end,
    heading: headingFor(model, start)?.text ?? null,
  };
}

function isHighSurrogate(c: number) {
  return c >= 0xd800 && c <= 0xdbff;
}
function isLowSurrogate(c: number) {
  return c >= 0xdc00 && c <= 0xdfff;
}

// ---------------------------------------------------------------- client-side resolution (same algorithm as server)

export type AnchorState = "attached" | "moved" | "ambiguous" | "orphaned";

export function resolveAnchor(model: TextModel, anchor: { quote: string; prefix: string; suffix: string; start: number; end: number; version_sha256: string }, currentSha: string): { state: AnchorState; start: number | null; end: number | null } {
  const text = model.text;
  if (anchor.version_sha256 === currentSha && text.slice(anchor.start, anchor.end) === anchor.quote) {
    return { state: "attached", start: anchor.start, end: anchor.end };
  }
  const occ: number[] = [];
  let i = text.indexOf(anchor.quote);
  while (i !== -1 && anchor.quote) {
    occ.push(i);
    i = text.indexOf(anchor.quote, i + 1);
  }
  if (occ.length === 0) return { state: "orphaned", start: null, end: null };
  if (occ.length === 1) return { state: "moved", start: occ[0], end: occ[0] + anchor.quote.length };
  const keep = occ.filter((o) => {
    if (anchor.prefix && text.slice(Math.max(0, o - anchor.prefix.length), o) !== anchor.prefix) return false;
    if (anchor.suffix && text.slice(o + anchor.quote.length, o + anchor.quote.length + anchor.suffix.length) !== anchor.suffix) return false;
    return true;
  });
  if (keep.length === 1) return { state: "moved", start: keep[0], end: keep[0] + anchor.quote.length };
  return { state: "ambiguous", start: null, end: null };
}
