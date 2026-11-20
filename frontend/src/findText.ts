/**
 * Find in a document: matches of a text in what is shown, highlighted with the CSS Custom Highlight API (no change to
 * the document's DOM, so discussion marks and the text model stay as they are).
 */
import { squash } from "./viewSync";

export interface FindOpts {
  case: boolean;
  word: boolean;
}

export interface Match {
  s: number;
  e: number;
}

/** What a find bar drives: the text view (the DOM) or a PDF / layout view. */
export interface FindEngine {
  /** look the text up; how many matches there are */
  search(q: string, opts: FindOpts): Promise<number>;
  /** highlight the matches, `i` as the current one (scrolled to when `scroll`) */
  show(i: number, scroll: boolean): void;
  /** the match a link from the folder search means: by the text before it, else the n-th */
  pick(req: { n: number; ctx: string; page?: number | null }): number;
  clear(): void;
}

const MAX_MATCHES = 5000;

/** Spaces in the text match any run of spaces / line breaks (or none: words split over lines and PDF text runs). */
export function findRegex(q: string, opts: FindOpts): RegExp | null {
  const t = q.trim();
  if (!t) return null;
  let src = t.split(/\s+/).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s*");
  if (opts.word) src = `(?<![\\p{L}\\p{N}_])${src}(?![\\p{L}\\p{N}_])`;
  return new RegExp(src, `gu${opts.case ? "" : "i"}`);
}

export function matchesIn(text: string, re: RegExp | null): Match[] {
  const out: Match[] = [];
  if (!re) return out;
  re.lastIndex = 0;
  for (let m = re.exec(text); m && out.length < MAX_MATCHES; m = re.exec(text)) {
    if (m[0].length === 0) {
      re.lastIndex++;
      continue;
    }
    out.push({ s: m.index, e: m.index + m[0].length });
  }
  return out;
}

/** Which match the folder search's hit is: the ones preceded by its text (letters and digits), the nearest the n-th. */
export function pickMatch(text: string, matches: Match[], req: { n: number; ctx: string }): number {
  if (!matches.length) return -1;
  const want = squash(req.ctx);
  for (const len of [24, 12, 6]) {
    if (want.length < Math.min(len, 6)) break;
    const tail = want.slice(-len);
    const cands = matches.map((m, i) => ({ m, i })).filter(({ m }) => squash(text.slice(Math.max(0, m.s - 4 * len - 40), m.s)).endsWith(tail));
    if (cands.length) return cands.reduce((a, b) => (Math.abs(b.i - req.n) < Math.abs(a.i - req.n) ? b : a)).i;
  }
  return Math.min(Math.max(0, req.n), matches.length - 1);
}

export interface TextIndex {
  text: string;
  nodes: Text[];
  starts: number[];
}

/** The text under `root` (its text nodes in order), skipping what is not document text. */
export function indexText(root: Node, skip = ".waw-thread-pin, script, style, .toc"): TextIndex {
  const nodes: Text[] = [];
  const starts: number[] = [];
  let text = "";
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement?.closest(skip) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  for (let n = walker.nextNode() as Text | null; n; n = walker.nextNode() as Text | null) {
    nodes.push(n);
    starts.push(text.length);
    text += n.data;
  }
  return { text, nodes, starts };
}

function locate(ix: TextIndex, at: number, end: boolean): [Text, number] | null {
  let lo = 0;
  let hi = ix.nodes.length - 1;
  if (hi < 0) return null;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ix.starts[mid] < at || (!end && ix.starts[mid] === at)) lo = mid;
    else hi = mid - 1;
  }
  const n = ix.nodes[lo];
  return [n, Math.min(n.data.length, at - ix.starts[lo])];
}

export function rangeOf(ix: TextIndex, m: Match): Range | null {
  const a = locate(ix, m.s, false);
  const b = locate(ix, m.e, true);
  if (!a || !b) return null;
  const r = document.createRange();
  try {
    r.setStart(a[0], a[1]);
    r.setEnd(b[0], b[1]);
  } catch {
    return null;
  }
  return r;
}

type HighlightCtor = new (...ranges: Range[]) => unknown;
const registry = (): Map<string, unknown> | null => {
  const css = (globalThis as unknown as { CSS?: { highlights?: Map<string, unknown> } }).CSS;
  return css?.highlights && typeof (globalThis as unknown as { Highlight?: unknown }).Highlight === "function" ? css.highlights : null;
};

export function setFindHighlights(all: Range[], current: Range | null): void {
  const reg = registry();
  if (!reg) return;
  const H = (globalThis as unknown as { Highlight: HighlightCtor }).Highlight;
  reg.set("waw-find", new H(...all.filter((r) => r !== current)));
  if (current) reg.set("waw-find-current", new H(current));
  else reg.delete("waw-find-current");
}

export function clearFindHighlights(): void {
  const reg = registry();
  reg?.delete("waw-find");
  reg?.delete("waw-find-current");
}

/** Scroll `scroller` so the range is in view (a third down), unless it already is. */
export function revealRange(scroller: HTMLElement, r: Range): void {
  const rect = r.getBoundingClientRect();
  const box = scroller.getBoundingClientRect();
  if (rect.top >= box.top + 8 && rect.bottom <= box.bottom - 8 && rect.left >= box.left && rect.right <= box.right) return;
  scroller.scrollTop += rect.top - box.top - scroller.clientHeight / 3;
  if (rect.left < box.left || rect.right > box.right) scroller.scrollLeft += rect.left - box.left - 40;
}

/** The find engine of a text view: the document's text in `root`, scrolled in `scroller`. */
export function domFindEngine(root: () => HTMLElement | null, scroller: () => HTMLElement | null): FindEngine {
  let ix: TextIndex | null = null;
  let matches: Match[] = [];
  let ranges: (Range | null)[] = [];
  let re: RegExp | null = null;
  return {
    async search(q, opts) {
      const el = root();
      re = findRegex(q, opts);
      ix = el ? indexText(el) : null;
      matches = ix ? matchesIn(ix.text, re) : [];
      ranges = matches.map((m) => (ix ? rangeOf(ix, m) : null));
      return matches.length;
    },
    show(i, scroll) {
      const cur = ranges[i] ?? null;
      setFindHighlights(ranges.filter((r): r is Range => !!r), cur);
      const sc = scroller();
      if (scroll && cur && sc) revealRange(sc, cur);
    },
    pick(req) {
      return ix ? pickMatch(ix.text, matches, req) : -1;
    },
    clear() {
      clearFindHighlights();
      ix = null;
      matches = [];
      ranges = [];
    },
  };
}
