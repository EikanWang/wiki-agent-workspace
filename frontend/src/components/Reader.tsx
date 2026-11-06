import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { COMPOSER_HINT, composerKeyDown } from "./keys";
import { AgentSession, ApiError, OpenDocument, ReviewMark, Thread, getWorkspace, put } from "../api";
import { renderMarkdown, sanitizeHtml, slugify } from "../markdown";
import { SelectionAnchor, TextModel, anchorFromSelection, buildTextModel, makeAnchor, rangeFromOffsets, resolveAnchor } from "../textmodel";

export interface ReaderProps {
  doc: OpenDocument;
  threads: Thread[];
  marks: ReviewMark[];
  activeThreadId: string | null;
  changedNotice: { sha256?: string; missing?: boolean } | null;
  onRefresh: () => void;
  onRelink: () => void;
  onOpenThread: (id: string) => void;
  /** Create a discussion; resolves when the thread is created (throws ApiError on failure). */
  onSend: (anchor: SelectionAnchor, question: string, sessionChoice: string | null) => Promise<void>;
  onMarkReviewed: (anchor: SelectionAnchor) => void;
  sessions: AgentSession[];
  defaultSessionId: string | null;
  defaultProvider: string | null;
  onReanchor: ((anchor: SelectionAnchor) => void) | null;
  onOpenInternalLink: (path: string, fragment: string | null) => void;
  /** Heading fragment to scroll to once the document is rendered (from a cross-document link). */
  initialFragment?: string | null;
  onFragmentConsumed?: () => void;
  tocOpen?: boolean;
  onToggleToc?: () => void;
  readerMode?: "read" | "discuss";
  onReaderModeChange?: (m: "read" | "discuss") => void;
  pendingSelectionRef: React.MutableRefObject<SelectionAnchor | null>;
}

const SELECTION_DELAY_MS = 350;

export default function Reader(props: ReaderProps) {
  const { doc, threads, marks, activeThreadId, changedNotice, onRefresh, onRelink, onOpenThread, initialFragment, onFragmentConsumed, tocOpen = true, onToggleToc, readerMode = "discuss", onReaderModeChange, onSend, onMarkReviewed, onReanchor, onOpenInternalLink, sessions, defaultSessionId, defaultProvider } = props;
  const [sheet, setSheet] = useState<SelectionAnchor | null>(null); // mobile / header-button composer
  const [copyChip, setCopyChip] = useState<{ text: string; x: number; y: number; done: boolean } | null>(null); // read mode
  const containerRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const modelRef = useRef<TextModel | null>(null);
  const [popover, setPopover] = useState<{ anchor: SelectionAnchor; x: number; y: number; above: boolean } | null>(null);
  const [mobileSel, setMobileSel] = useState<SelectionAnchor | null>(null);
  const selectionTimer = useRef<number | null>(null);
  const pointerDown = useRef(false);
  const restoredFor = useRef<string | null>(null);
  const isTouch = useMemo(() => typeof window !== "undefined" && window.matchMedia("(pointer: coarse)").matches, []);

  const html = useMemo(() => {
    if (doc.format === "markdown") return renderMarkdown(doc.text, doc.path);
    if (doc.format === "pdf") return "";
    return sanitizeHtml(doc.html ?? "", doc.path);
  }, [doc.text, doc.html, doc.format, doc.path]);

  // Render + build model + apply highlights synchronously after DOM update.
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    el.innerHTML = html;
    if (doc.format === "text") {
      // line numbers as CSS counters: wrap each source line so numbers never enter the text model/selection
      const code = el.querySelector("pre > code");
      if (code && !code.querySelector(".ln")) {
        const raw = code.textContent ?? "";
        const lines = raw.endsWith("\n") ? raw.slice(0, -1).split("\n") : raw.split("\n");
        code.textContent = "";
        lines.forEach((line, i) => {
          const span = document.createElement("span");
          span.className = "ln";
          span.textContent = line + (i < lines.length - 1 ? "\n" : "");
          code.appendChild(span);
        });
        el.classList.add("source-view");
      }
    } else {
      el.classList.remove("source-view");
    }
    modelRef.current = buildTextModel(el);
    applyHighlights();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [html, doc.format]);

  useLayoutEffect(() => {
    applyHighlights();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threads, marks, activeThreadId]);

  function applyHighlights() {
    const el = containerRef.current;
    if (!el) return;
    // remove previous marks
    el.querySelectorAll("mark.waw-hl:not(.waw-pending)").forEach((m) => {
      const parent = m.parentNode!;
      while (m.firstChild) parent.insertBefore(m.firstChild, m);
      parent.removeChild(m);
      parent.normalize();
    });
    el.querySelectorAll(".waw-thread-pin").forEach((p) => p.remove());
    let model = buildTextModel(el);
    const items: { start: number; end: number; cls: string; id?: string; title: string }[] = [];
    for (const t of threads) {
      const res = resolveLocal(model, t, doc.sha256);
      if (res.start !== null && res.end !== null) {
        items.push({ start: res.start, end: res.end, cls: `thread ${t.id === activeThreadId ? "active" : ""} ${res.state}`, id: t.id, title: `Discussion · ${t.messages[0]?.content.slice(0, 60) ?? ""}` });
      }
    }
    for (const m of marks) {
      const res = resolveAnchor(model, { quote: m.quote, prefix: "", suffix: "", start: m.start, end: m.end, version_sha256: m.reviewed_version_sha256 }, doc.sha256);
      if (res.start !== null && res.end !== null) items.push({ start: res.start, end: res.end, cls: `review ${m.state}`, title: m.state === "reviewed" ? "Marked as reviewed" : "Reviewed passage changed: needs review" });
    }
    // apply from the end so earlier offsets stay valid; rebuild model between wraps for safety
    items.sort((a, b) => b.start - a.start);
    for (const it of items) {
      model = buildTextModel(el);
      const range = rangeFromOffsets(model, it.start, it.end);
      if (!range) continue;
      wrapRange(range, it.cls, it.id, it.title);
    }
    modelRef.current = buildTextModel(el);
  }

  function wrapRange(range: Range, cls: string, threadId: string | undefined, title: string) {
    // wrap every text node portion inside the range with <mark>
    const nodes: Text[] = [];
    const common = range.commonAncestorContainer;
    if (common.nodeType === Node.TEXT_NODE) {
      nodes.push(common as Text);
    } else {
      const walker = document.createTreeWalker(common, NodeFilter.SHOW_TEXT);
      let n = walker.nextNode();
      while (n) {
        if (range.intersectsNode(n) && (n as Text).data.length && !(/^\s*$/.test((n as Text).data) && (n as Text).data.includes("\n"))) nodes.push(n as Text);
        n = walker.nextNode();
      }
    }
    let first = true;
    for (const tn of nodes) {
      let node = tn;
      if (node === range.startContainer && range.startOffset > 0) node = node.splitText(range.startOffset);
      if (node === range.endContainer || tn === range.endContainer) {
        const endOff = tn === range.endContainer ? range.endOffset - (tn === range.startContainer ? range.startOffset : 0) : null;
        if (endOff !== null && endOff < node.data.length) node.splitText(endOff);
      }
      if (!node.data) continue;
      const mark = document.createElement("mark");
      mark.className = `waw-hl ${cls}`;
      mark.title = title;
      if (threadId) {
        mark.dataset.threadId = threadId;
        if (first) {
          const pin = document.createElement("span");
          pin.className = "waw-thread-pin";
          pin.textContent = "▤";
          pin.dataset.threadId = threadId;
          node.parentNode!.insertBefore(pin, node);
        }
      }
      node.parentNode!.insertBefore(mark, node);
      mark.appendChild(node);
      first = false;
    }
  }

  // Keep the quoted passage visibly highlighted while the question box is open, even after the
  // DOM selection collapses because focus moved into the input.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    el.querySelectorAll("mark.waw-pending").forEach((m) => {
      const parent = m.parentNode!;
      while (m.firstChild) parent.insertBefore(m.firstChild, m);
      parent.removeChild(m);
      parent.normalize();
    });
    const a = popover?.anchor ?? sheet;
    if (a) {
      const model = buildTextModel(el);
      const range = rangeFromOffsets(model, a.start, a.end);
      if (range) wrapRange(range, "pending waw-pending", undefined, "");
    }
    modelRef.current = buildTextModel(el);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [popover?.anchor.start, popover?.anchor.end, sheet?.start, sheet?.end, html]);

  // Before sending, re-locate the quoted passage in the *current* model: highlights may have re-wrapped the DOM
  // and the document may have been refreshed while the question box was open. Keeps the offsets the server sees in
  // sync with the quote; the server additionally relocates by context as a last resort.
  const freshAnchor = useCallback((a: SelectionAnchor): SelectionAnchor => {
    const model = modelRef.current;
    if (!model || a.end <= a.start) return a;
    if (model.text.slice(a.start, a.end) === a.quote) return a;
    const res = resolveAnchor(model, { quote: a.quote, prefix: a.prefix, suffix: a.suffix, start: a.start, end: a.end, version_sha256: "" }, "x");
    if (res.start !== null && res.end !== null) return makeAnchor(model, res.start, res.end);
    return a;
  }, []);

  // In-document fragment navigation: exact id, then case-insensitive id, then heading-text slug.
  const [fragmentMissing, setFragmentMissing] = useState<string | null>(null);
  const scrollToFragment = useCallback((fragment: string): boolean => {
    const el = containerRef.current;
    if (!el) return false;
    const want = fragment.replace(/^#/, "");
    const wantLower = want.toLowerCase();
    const wantSlug = slugify(want.replace(/-/g, " "));
    let target: HTMLElement | null = null;
    try {
      target = el.querySelector<HTMLElement>(`#${CSS.escape(want)}`);
    } catch {
      target = null;
    }
    if (!target) {
      const withIds = Array.from(el.querySelectorAll<HTMLElement>("[id]"));
      target = withIds.find((h) => h.id.toLowerCase() === wantLower) ?? null;
      if (!target) target = withIds.find((h) => slugify(h.textContent || "") === wantLower || slugify(h.textContent || "") === wantSlug) ?? null;
    }
    if (!target) return false;
    const scroller = scrollRef.current;
    if (scroller) {
      const top = target.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - 12;
      scroller.scrollTo({ top, behavior: "smooth" });
    } else {
      target.scrollIntoView({ block: "start", behavior: "smooth" });
    }
    target.classList.add("waw-target");
    window.setTimeout(() => target?.classList.remove("waw-target"), 1600);
    setFragmentMissing(null);
    return true;
  }, []);

  useEffect(() => {
    if (!initialFragment) return;
    const ok = scrollToFragment(initialFragment);
    if (!ok) setFragmentMissing(initialFragment);
    onFragmentConsumed?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialFragment, html]);

  // Clicks: thread marks open the discussion; internal links open documents.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      const pin = target.closest<HTMLElement>("[data-thread-id]");
      if (pin && (target.tagName === "MARK" || target.classList.contains("waw-thread-pin"))) {
        const sel = window.getSelection();
        if (sel && !sel.isCollapsed) return; // user is selecting text
        onOpenThread(pin.dataset.threadId!);
        return;
      }
      const a = target.closest("a");
      if (!a) return;
      if (a.dataset.internal) {
        e.preventDefault();
        onOpenInternalLink(a.dataset.internal, a.dataset.anchor ?? null);
      } else if (a.dataset.anchor !== undefined) {
        e.preventDefault();
        if (!scrollToFragment(a.dataset.anchor)) setFragmentMissing(a.dataset.anchor);
      }
    };
    el.addEventListener("click", onClick);
    return () => el.removeEventListener("click", onClick);
  }, [onOpenThread, onOpenInternalLink, scrollToFragment]);

  // Selection handling (desktop: 350 ms after pointer release; mobile: visible Ask action)
  const computeSelection = useCallback((): SelectionAnchor | null => {
    const el = containerRef.current;
    const model = modelRef.current;
    const sel = window.getSelection();
    if (!el || !model || !sel) return null;
    return anchorFromSelection(model, el, sel);
  }, []);

  const showPopoverForSelection = useCallback(() => {
    const anchor = computeSelection();
    const sel = window.getSelection();
    if (!anchor || !sel || sel.rangeCount === 0) {
      setPopover(null);
      setMobileSel(null);
      setCopyChip(null);
      return;
    }
    props.pendingSelectionRef.current = anchor;
    if (readerMode === "read") {
      // plain viewer: offer only a small copy chip at the end of the selection
      const rect = sel.getRangeAt(0).getBoundingClientRect();
      const scroller = scrollRef.current!;
      const sRect = scroller.getBoundingClientRect();
      setCopyChip({ text: anchor.quote, x: Math.min(rect.right - sRect.left + scroller.scrollLeft + 6, sRect.width - 40), y: rect.top - sRect.top + scroller.scrollTop - 30, done: false });
      return;
    }
    if (isTouch) {
      setMobileSel(anchor);
      return;
    }
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    const scroller = scrollRef.current!;
    const sRect = scroller.getBoundingClientRect();
    const popH = 190;
    const spaceBelow = sRect.bottom - rect.bottom;
    const above = spaceBelow < popH + 12 && rect.top - sRect.top > popH;
    const x = Math.min(Math.max(rect.left - sRect.left + scroller.scrollLeft, 8), Math.max(8, sRect.width - 360));
    const y = above ? rect.top - sRect.top + scroller.scrollTop - 8 : rect.bottom - sRect.top + scroller.scrollTop + 8;
    setPopover({ anchor, x, y, above });
  }, [computeSelection, isTouch, props.pendingSelectionRef, readerMode]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const down = () => {
      pointerDown.current = true;
    };
    const up = (e: PointerEvent) => {
      pointerDown.current = false;
      const target = e.target instanceof Element ? e.target : null;
      // interacting with the popover/sheet must not re-evaluate the document selection
      if (target && (target.closest(".sel-popover") || target.closest(".sheet-backdrop") || target.closest(".mobile-ask-bar"))) return;
      if (selectionTimer.current) window.clearTimeout(selectionTimer.current);
      selectionTimer.current = window.setTimeout(showPopoverForSelection, SELECTION_DELAY_MS);
    };
    const change = () => {
      // The DOM selection collapses when focus moves into the question input; the popover keeps
      // its own copy of the anchor, so a collapsed selection never closes it here.
      const s0 = window.getSelection();
      if (s0 && s0.isCollapsed) setCopyChip((c) => (c && !c.done ? null : c));
      if (!isTouch) return;
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) {
        if (!pointerDown.current && !(document.activeElement && document.activeElement.closest(".sel-popover"))) setMobileSel(null);
        return;
      }
      if (selectionTimer.current) window.clearTimeout(selectionTimer.current);
      selectionTimer.current = window.setTimeout(showPopoverForSelection, SELECTION_DELAY_MS);
    };
    el.addEventListener("pointerdown", down);
    document.addEventListener("pointerup", up);
    document.addEventListener("selectionchange", change);
    return () => {
      el.removeEventListener("pointerdown", down);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("selectionchange", change);
    };
  }, [showPopoverForSelection, isTouch]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setPopover(null);
        setMobileSel(null);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // Reading position: save after 1 s idle and on page hide; restore once per document version.
  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    let timer: number | null = null;
    const save = () => {
      const model = modelRef.current;
      const el = containerRef.current;
      if (!model || !el) return;
      const frac = scroller.scrollHeight > scroller.clientHeight ? scroller.scrollTop / (scroller.scrollHeight - scroller.clientHeight) : 0;
      const top = scroller.getBoundingClientRect().top;
      let heading: string | null = null;
      for (const h of model.headings) {
        if (h.element.getBoundingClientRect().top - top <= 8) heading = h.text;
        else break;
      }
      let offset = 0;
      for (const s of model.spans) {
        const r = document.createRange();
        r.selectNodeContents(s.node);
        const rect = r.getBoundingClientRect();
        if (rect.bottom - top >= 0) {
          offset = s.start;
          break;
        }
      }
      void put(`/documents/${doc.document_id}/reading-state`, { version_sha256: doc.sha256, heading, offset, scroll_fraction: Math.min(1, Math.max(0, frac)) }).catch(() => undefined);
    };
    const onScroll = () => {
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(save, 1000);
    };
    const onHide = () => save();
    scroller.addEventListener("scroll", onScroll, { passive: true });
    document.addEventListener("visibilitychange", onHide);
    window.addEventListener("pagehide", onHide);
    return () => {
      scroller.removeEventListener("scroll", onScroll);
      document.removeEventListener("visibilitychange", onHide);
      window.removeEventListener("pagehide", onHide);
      if (timer) window.clearTimeout(timer);
    };
  }, [doc.document_id, doc.sha256]);

  useLayoutEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    if (restoredFor.current === doc.document_id) return; // keep position across refreshes of the same document
    restoredFor.current = doc.document_id;
    if (initialFragment) return; // a link target takes precedence over the saved reading position
    const rs = doc.reading_state;
    if (rs && rs.scroll_fraction > 0) {
      requestAnimationFrame(() => {
        scroller.scrollTop = rs.scroll_fraction * (scroller.scrollHeight - scroller.clientHeight);
      });
    } else {
      scroller.scrollTop = 0;
    }
  }, [doc.document_id, doc.reading_state, html]);

  const scrollToThread = useCallback((id: string) => {
    const el = containerRef.current?.querySelector<HTMLElement>(`mark[data-thread-id="${id}"]`);
    el?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, []);
  useEffect(() => {
    if (activeThreadId) scrollToThread(activeThreadId);
  }, [activeThreadId, scrollToThread]);

  const headings = modelRef.current?.headings ?? [];

  return (
    <div className="reader">
      <div className="doc-header">
        <div className="crumbs" title={doc.path}>
          {doc.path.split("/").map((p, i, arr) => (
            <span key={i}>
              {p}
              {i < arr.length - 1 && <span className="sep"> / </span>}
            </span>
          ))}
        </div>
        <div className="doc-meta">
          <span className="chip">{doc.format.toUpperCase()}</span>
          {doc.lock && (
            <span className="chip locked" title={`Locked${doc.lock.is_dir && doc.lock.path !== doc.path ? ` by folder ${doc.lock.path || "(workspace)"}` : ""}${doc.lock.reason ? ` — ${doc.lock.reason}` : ""}. The app and app-driven agents will not modify this file; your terminal is unrestricted.`} data-testid="doc-lock-badge">
              🔒 locked{doc.lock.is_dir && doc.lock.path !== doc.path ? " (folder)" : ""}
            </span>
          )}
          {doc.git.branch && <span className="chip" title={doc.git.commit ?? ""}>{doc.git.branch}</span>}
          {doc.git.file_status && <span className={`chip git ${doc.git.file_status}`}>{doc.git.file_status}</span>}
          {doc.git.remote_state && <span className="chip" title="Relation to the remote">{doc.git.remote_state}</span>}
          <span className="chip mono" title={doc.sha256}>{doc.sha256.slice(0, 8)}</span>
          <span className="chip">{threads.length} discussions</span>
          {onReaderModeChange && (
            <span className="mode-switch" role="tablist" aria-label="Reader mode" title="Read: selecting text only copies. Discuss: selecting text opens the question box.">
              <button role="tab" aria-selected={readerMode === "read"} className={readerMode === "read" ? "active" : ""} onClick={() => onReaderModeChange("read")} data-testid="reader-mode-read">Read</button>
              <button role="tab" aria-selected={readerMode === "discuss"} className={readerMode === "discuss" ? "active" : ""} onClick={() => onReaderModeChange("discuss")} data-testid="reader-mode-discuss">Discuss</button>
            </span>
          )}
          <button
            className="btn small"
            data-testid="ask-current-selection"
            onClick={() => {
              if (doc.format === "pdf") {
                // no text anchors for PDFs: start a discussion about the document as a whole
                setSheet({ quote: doc.path, prefix: "", suffix: "", start: 0, end: 0, heading: null });
                return;
              }
              const a = computeSelection() ?? props.pendingSelectionRef.current;
              if (a) setSheet(a);
              else alert("Select some text in the document first.");
            }}
            title={doc.format === "pdf" ? "Ask the agent about this PDF" : "On touch devices: select text first, then tap here to ask"}
          >
            {doc.format === "pdf" ? "Ask about this PDF" : "Ask about selection"}
          </button>
        </div>
      </div>
      {changedNotice && (
        <div className="banner" role="status" data-testid="doc-changed-banner">
          {changedNotice.missing ? "This file was deleted or renamed. Discussions are kept; you can relink the document to another file." : "The document changed on disk. Your reading position is kept where possible."}
          {!changedNotice.missing ? (
            <button className="btn small primary" onClick={onRefresh} data-testid="doc-refresh">
              Refresh
            </button>
          ) : (
            <button className="btn small primary" onClick={onRelink} data-testid="doc-relink">
              Relink to another file…
            </button>
          )}
        </div>
      )}
      {fragmentMissing && (
        <div className="banner" role="status">
          No heading matches the link target <code>#{fragmentMissing}</code> in this document.
          <button className="icon-btn" onClick={() => setFragmentMissing(null)} aria-label="Dismiss">×</button>
        </div>
      )}
      <div className="reader-body" ref={scrollRef}>
        {headings.length > 1 && (
          <nav className={`toc ${tocOpen ? "" : "collapsed"}`} data-testid="toc">
            <button className="toc-toggle" onClick={onToggleToc} title={tocOpen ? "Fold outline" : "Unfold outline"} data-testid="toc-toggle">
              {tocOpen ? "▾ Outline" : "▸"}
            </button>
            {tocOpen &&
              headings.map((h, i) => (
                <button key={i} className={`toc-item l${h.level}`} onClick={() => h.element.scrollIntoView({ block: "start", behavior: "smooth" })}>
                  {h.text}
                </button>
              ))}
          </nav>
        )}
        {doc.format === "pdf" ? (
          <div className="pdf-view" data-testid="pdf-view">
            <div className="muted small">PDF shown with the browser's viewer. Discussions on PDFs attach to the document (no text anchors); use the discussion panel to ask about it.</div>
            <iframe title={doc.path} src={`/api/v1/raw?path=${encodeURIComponent(doc.path)}${getWorkspace() ? `&workspace=${encodeURIComponent(getWorkspace()!)}` : ""}`} />
          </div>
        ) : (
          <article className="markdown-body" ref={containerRef} data-testid="markdown-body" />
        )}
        {copyChip && (
          <button
            className={`copy-chip ${copyChip.done ? "done" : ""}`}
            style={{ left: copyChip.x, top: copyChip.y }}
            onMouseDown={(e) => e.preventDefault()}
            onClick={async () => {
              await copyText(copyChip.text);
              setCopyChip({ ...copyChip, done: true });
              window.setTimeout(() => setCopyChip(null), 1200);
            }}
            title="Copy selection"
            aria-label="Copy selection"
            data-testid="copy-chip"
          >
            {copyChip.done ? <CheckIcon /> : <CopyIcon />}
          </button>
        )}
        {popover && !isTouch && (
          <SelectionPopover
            anchor={popover.anchor}
            style={{ left: popover.x, top: popover.y, transform: popover.above ? "translateY(-100%)" : undefined }}
            sessions={sessions}
            defaultSessionId={defaultSessionId}
            defaultProvider={defaultProvider}
            onSend={async (q, sid) => {
              await onSend(freshAnchor(popover.anchor), q, sid);
              setPopover(null);
            }}
            onMark={() => {
              onMarkReviewed(popover.anchor);
              setPopover(null);
            }}
            onReanchor={onReanchor ? () => { onReanchor(popover.anchor); setPopover(null); } : null}
            onClose={() => setPopover(null)}
          />
        )}
      </div>
      {isTouch && mobileSel && !sheet && (
        <div className="mobile-ask-bar" data-testid="mobile-ask-bar">
          <span className="quote">"{mobileSel.quote.slice(0, 60)}{mobileSel.quote.length > 60 ? "…" : ""}"</span>
          <button className="btn primary small" onClick={() => { setSheet(mobileSel); setMobileSel(null); }}>Ask</button>
          <button className="btn small" onClick={() => { void copyText(mobileSel.quote); setMobileSel(null); }} aria-label="Copy selection"><CopyIcon /></button>
          <button className="btn small" onClick={() => { onMarkReviewed(mobileSel); setMobileSel(null); }}>Mark reviewed</button>
        </div>
      )}
      {sheet && (
        <div className="sheet-backdrop" onClick={() => setSheet(null)}>
          <div onClick={(e) => e.stopPropagation()}>
            <SelectionPopover
              anchor={sheet}
              style={{}}
              sessions={sessions}
              defaultSessionId={defaultSessionId}
              defaultProvider={defaultProvider}
              className="sheet"
              onSend={async (q, sid) => {
                await onSend(freshAnchor(sheet), q, sid);
                setSheet(null);
              }}
              onMark={() => {
                onMarkReviewed(sheet);
                setSheet(null);
              }}
              onReanchor={onReanchor ? () => { onReanchor(sheet); setSheet(null); } : null}
              onClose={() => setSheet(null)}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function resolveLocal(model: TextModel, t: Thread, sha: string) {
  // prefer the server's resolution when it agrees with our model, else resolve locally
  if (t.current_start !== null && t.current_end !== null && model.text.slice(t.current_start, t.current_end) === t.anchor.quote) {
    return { state: t.anchor_state, start: t.current_start, end: t.current_end };
  }
  return resolveAnchor(model, t.anchor, sha);
}

function SelectionPopover({ anchor, style, sessions, defaultSessionId, defaultProvider, className, onSend, onMark, onReanchor, onClose }: {
  anchor: SelectionAnchor;
  style: React.CSSProperties;
  sessions: AgentSession[];
  defaultSessionId: string | null;
  defaultProvider: string | null;
  className?: string;
  onSend: (question: string, sessionChoice: string | null) => Promise<void>;
  onMark: () => void;
  onReanchor: (() => void) | null;
  onClose: () => void;
}) {
  const [question, setQuestion] = useState("");
  const [session, setSession] = useState<string>(defaultSessionId ?? "__default__");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    // focusing the input must not clear the document selection; we only move focus after a tick
    const t = window.setTimeout(() => taRef.current?.focus({ preventScroll: true }), 50);
    return () => window.clearTimeout(t);
  }, []);
  const send = async () => {
    if (!question.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await onSend(question.trim(), session === "__default__" ? null : session);
    } catch (e) {
      const ae = e as ApiError;
      if (ae.code === "DOCUMENT_VERSION_CONFLICT") setError("The document changed after you selected the text. Your question is kept: refresh the document, reselect the passage and send again.");
      else if (ae.code === "INVALID_ANCHOR") setError("The selected text could not be located in the current document; please reselect.");
      else if (ae.code === "PROVIDER_UNAVAILABLE") setError(`Agent unavailable: ${ae.message}`);
      else setError(ae.message || "Send failed");
    } finally {
      setBusy(false);
    }
  };
  const defaultSess = sessions.find((s) => s.id === defaultSessionId);
  return (
    <div className={`sel-popover ${className ?? ""}`} style={style} data-testid="selection-popover" onMouseDown={(e) => e.stopPropagation()}>
      <div className="sel-head">
        <span>Ask about the selected text</span>
        <button className="icon-btn" onClick={onClose} aria-label="Close">×</button>
      </div>
      <div className="sel-quote-wrap">
        <blockquote className="sel-quote" data-testid="selection-quote">{anchor.quote}</blockquote>
        <button
          className={`icon-copy ${copied ? "done" : ""}`}
          onClick={async () => {
            await copyText(anchor.quote);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          }}
          title={copied ? "Copied" : "Copy selection"}
          aria-label="Copy selection"
          data-testid="selection-copy"
        >
          {copied ? <CheckIcon /> : <CopyIcon />}
        </button>
      </div>
      {anchor.heading && <div className="sel-heading">Section: {anchor.heading}</div>}
      <textarea
        ref={taRef}
        className="sel-input"
        placeholder={`Your question about the selected text (${COMPOSER_HINT})`}
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        onKeyDown={(e) => composerKeyDown(e, () => void send(), setQuestion, onClose)}
        data-testid="selection-question"
        rows={3}
      />
      <div className="sel-row">
        <select value={session} onChange={(e) => setSession(e.target.value)} data-testid="selection-session" title="Session used for this question">
          <option value="__default__">
            {defaultSess ? `Document session · ${defaultSess.provider} ${defaultSess.short_id ?? defaultSess.status}` : defaultProvider ? `Document session (created in ${defaultProvider} on first question)` : "No agent provider configured"}
          </option>
          <option value="new">New session</option>
          {sessions.filter((s) => s.id !== defaultSessionId && s.origin !== "preview").map((s) => (
            <option key={s.id} value={s.id}>
              {s.provider} · {s.display_name ?? s.short_id ?? s.id.slice(0, 8)} {s.status !== "ready" ? `(${s.status})` : ""}
            </option>
          ))}
        </select>
      </div>
      {error && <div className="error-text" role="alert" data-testid="selection-error">{error}</div>}
      <div className="sel-actions">
        <button className="btn primary" onClick={() => void send()} disabled={busy || !question.trim()} data-testid="selection-send">{busy ? "Sending…" : "Send"}</button>
        <button className="btn" onClick={onMark} title="Explicitly mark as reviewed; scrolling never marks automatically">Mark reviewed</button>
        {onReanchor && <button className="btn" onClick={onReanchor}>Reanchor current discussion here</button>}
      </div>
    </div>
  );
}


async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // clipboard API unavailable (insecure context): copy the live selection via execCommand
    const sel = window.getSelection();
    const had = sel && !sel.isCollapsed;
    if (!had) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    } else {
      document.execCommand("copy");
    }
  }
}

/** GitHub-style copy / check icons. */
function CopyIcon() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <path fill="currentColor" d="M0 6.75C0 5.784.784 5 1.75 5h1.5a.75.75 0 0 1 0 1.5h-1.5a.25.25 0 0 0-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 0 0 .25-.25v-1.5a.75.75 0 0 1 1.5 0v1.5A1.75 1.75 0 0 1 9.25 16h-7.5A1.75 1.75 0 0 1 0 14.25Z" />
      <path fill="currentColor" d="M5 1.75C5 .784 5.784 0 6.75 0h7.5C15.216 0 16 .784 16 1.75v7.5A1.75 1.75 0 0 1 14.25 11h-7.5A1.75 1.75 0 0 1 5 9.25Zm1.75-.25a.25.25 0 0 0-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 0 0 .25-.25v-7.5a.25.25 0 0 0-.25-.25Z" />
    </svg>
  );
}
function CheckIcon() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <path fill="currentColor" d="M13.78 4.22a.75.75 0 0 1 0 1.06l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 9.28a.751.751 0 0 1 .018-1.042.751.751 0 0 1 1.042-.018L6 10.94l6.72-6.72a.75.75 0 0 1 1.06 0Z" />
    </svg>
  );
}