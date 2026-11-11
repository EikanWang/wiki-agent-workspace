import { Splitter, usePane } from "../Split";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type * as M from "monaco-editor";
import { Thread } from "../../api";
import { renderMarkdown } from "../../markdown";
import { buildTextModel, rangeFromOffsets } from "../../textmodel";
import { CodeAnchor, anchorOf } from "../DevAgentPanel";

/**
 * A Markdown file of a development workspace shown like a wiki page: rendered (same renderer as the reader),
 * outline, links between files (open in the editor, `#section` scrolls), images from the worktree, the file's
 * discussions highlighted (click: open it), and select text → Discuss (anchored to the source lines). Follows the
 * editor's text live (Split view).
 */

const norm = (s: string) => s.replace(/[*_`#>~[\]()|!-]/g, " ").replace(/\s+/g, " ").trim().toLowerCase();

/** Source lines (1-based) of rendered text selected in the preview. */
export function linesOfQuote(source: string, quote: string): { start: number; end: number } {
  const lines = source.split("\n");
  const nl = lines.map(norm);
  const parts = quote.split("\n").map(norm).filter((x) => x.length > 0);
  if (!parts.length) return { start: 1, end: 1 };
  const find = (needle: string, from: number) => {
    const n = needle.slice(0, 60);
    for (let i = from; i < nl.length; i++) if (nl[i] && (nl[i].includes(n) || (n.length > 12 && n.includes(nl[i]) && nl[i].length > 8))) return i;
    return -1;
  };
  let s = find(parts[0], 0);
  if (s < 0) s = 0;
  let e = parts.length > 1 ? find(parts[parts.length - 1].slice(-60), s) : s;
  if (e < s) e = s;
  return { start: s + 1, end: e + 1 };
}

function wrap(range: Range, cls: string, threadId: string, title: string) {
  const nodes: Text[] = [];
  const common = range.commonAncestorContainer;
  if (common.nodeType === Node.TEXT_NODE) nodes.push(common as Text);
  else {
    const w = document.createTreeWalker(common, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) if (range.intersectsNode(n) && (n as Text).data.trim()) nodes.push(n as Text);
  }
  for (const tn of nodes) {
    let node = tn;
    if (node === range.startContainer && range.startOffset > 0) node = node.splitText(range.startOffset);
    if (tn === range.endContainer) {
      const endOff = range.endOffset - (tn === range.startContainer ? range.startOffset : 0);
      if (endOff < node.data.length) node.splitText(endOff);
    }
    if (!node.data) continue;
    const mark = document.createElement("mark");
    mark.className = `waw-hl ${cls}`;
    mark.title = title;
    mark.dataset.threadId = threadId;
    node.parentNode!.insertBefore(mark, node);
    mark.appendChild(node);
  }
}

export default function MarkdownPreview({ model, root, rel, devId, threads, fragment, onFragmentDone, onOpen, onDiscuss, onOpenThread }: {
  model: M.editor.ITextModel;
  root: string;
  rel: string;
  devId: string;
  threads: Thread[];
  /** scroll to this heading id once rendered */
  fragment: string | null;
  onFragmentDone: () => void;
  onOpen: (root: string, path: string, fragment: string | null) => void;
  /** discuss a selection (rect: where it is, relative to the viewport) or the whole page (anchor without lines) */
  onDiscuss: (anchor: CodeAnchor, rect: DOMRect | null) => void;
  onOpenThread: (threadId: string) => void;
}) {
  const [text, setText] = useState(model.getValue());
  const body = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const [heads, setHeads] = useState<{ level: number; text: string; el: HTMLElement }[]>([]);
  const tocPane = usePane("webide.mdtoc", 190, 100, 500);
  const tocOpen = !tocPane.folded;
  const [sel, setSel] = useState<{ anchor: CodeAnchor; top: number; left: number; rect: DOMRect } | null>(null);
  useEffect(() => {
    setText(model.getValue());
    let t: ReturnType<typeof setTimeout> | undefined;
    const d = model.onDidChangeContent(() => { clearTimeout(t); t = setTimeout(() => setText(model.getValue()), 200); });
    return () => { clearTimeout(t); d.dispose(); };
  }, [model]);
  const html = useMemo(() => renderMarkdown(text, rel, {
    imageUrl: (p) => `/api/v1/dev/workspaces/${devId}/fs/raw?root=${root}&path=${encodeURIComponent(p)}`,
  }), [text, rel, devId, root]);
  const mine = useMemo(() => threads.map((t, i) => ({ t, i, a: anchorOf(t) })).filter((x) => x.a && x.a.root === root && x.a.file === rel && x.a.quote), [threads, root, rel]);

  useLayoutEffect(() => {
    const el = body.current;
    if (!el) return;
    el.innerHTML = html;
    // highlight the passages discussed (selected here, or lines selected in the editor)
    for (const { t, i, a } of mine) {
      const tm = buildTextModel(el);
      let start = tm.text.indexOf(a!.quote);
      let len = a!.quote.length;
      if (start < 0) {
        const first = a!.quote.split("\n").map((x) => x.replace(/^[#>*\-\d.\s]+/, "").replace(/[*_`]/g, "").trim()).find((x) => x.length >= 6);
        if (first) { start = tm.text.indexOf(first); len = first.length; }
      }
      if (start < 0) continue;
      const range = rangeFromOffsets(tm, start, start + len);
      if (range) wrap(range, "thread", t.id, `Discussion ${i + 1} · ${(t.messages[0]?.content ?? "").slice(0, 80)}`);
    }
    setHeads([...el.querySelectorAll<HTMLElement>("h1, h2, h3, h4, h5, h6")].map((h) => ({ level: Number(h.tagName[1]), text: h.textContent ?? "", el: h })));
  }, [html, mine]);

  useEffect(() => {
    if (!fragment || !body.current) return;
    const target = body.current.querySelector<HTMLElement>(`[id="${CSS.escape(fragment)}"]`);
    if (target) target.scrollIntoView({ block: "start" });
    onFragmentDone();
  }, [fragment, html, onFragmentDone]);

  const onClick = (e: React.MouseEvent) => {
    const t = e.target as HTMLElement;
    const mark = t.closest("mark[data-thread-id]") as HTMLElement | null;
    if (mark && window.getSelection()?.isCollapsed) return onOpenThread(mark.dataset.threadId!);
    const a = t.closest("a") as HTMLAnchorElement | null;
    if (!a) return;
    if (a.dataset.internal !== undefined) {
      e.preventDefault();
      onOpen(root, a.dataset.internal || rel, a.dataset.anchor ?? null);
    } else if (a.dataset.anchor !== undefined) {
      e.preventDefault();
      body.current?.querySelector<HTMLElement>(`[id="${CSS.escape(a.dataset.anchor)}"]`)?.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  };
  const onMouseUp = () => {
    setTimeout(() => {
      const s = window.getSelection();
      const el = body.current;
      if (!s || s.isCollapsed || !el || !s.rangeCount || !el.contains(s.getRangeAt(0).commonAncestorContainer)) return setSel(null);
      const quote = s.toString().trim();
      if (!quote) return setSel(null);
      const rect = s.getRangeAt(0).getBoundingClientRect();
      const box = scroller.current!.getBoundingClientRect();
      const { start, end } = linesOfQuote(model.getValue(), quote);
      setSel({ anchor: { root, file: rel, start_line: start, end_line: end, quote: quote.slice(0, 20000), language: "markdown" },
        top: rect.bottom - box.top + scroller.current!.scrollTop + 4, left: Math.min(rect.right - box.left, box.width - 120), rect });
    }, 10);
  };
  return (
    <div className="webide-md" data-testid="webide-md">
      {heads.length > 1 && tocOpen && (
        <nav className="webide-md-toc" style={{ width: tocPane.size }} data-testid="webide-md-toc">
          {heads.map((h, i) => (
            <button key={i} className={`toc-item l${h.level}`} onClick={() => h.el.scrollIntoView({ block: "start", behavior: "smooth" })}>{h.text}</button>
          ))}
        </nav>
      )}
      {heads.length > 1 && <Splitter dir="x" side="before" pane={tocPane} label="outline" testId="webide-md-toc-splitter" />}
      <div className="webide-md-scroll" ref={scroller} onScroll={() => setSel(null)}>
        <div className="webide-md-bar">
          <span className="muted small">{mine.length ? `${mine.length} discussion${mine.length === 1 ? "" : "s"} highlighted · ` : ""}select text to discuss it</span>
          <span className="spacer" />
          <button className="btn small" onClick={() => onDiscuss({ root, file: rel, quote: "", language: "markdown" }, null)} data-testid="webide-md-discuss-page">Discuss this page</button>
        </div>
        <article className="markdown-body webide-md-body" ref={body} onClick={onClick} onMouseUp={onMouseUp} data-testid="webide-md-body" />
        {sel && (
          <button className="webide-discuss-btn" style={{ position: "absolute", top: sel.top, left: sel.left }} onMouseDown={(e) => e.preventDefault()}
            onClick={() => { onDiscuss(sel.anchor, sel.rect); setSel(null); }} data-testid="webide-md-discuss-btn">💬 Discuss</button>
        )}
      </div>
    </div>
  );
}
