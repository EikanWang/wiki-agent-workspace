import { useCallback, useEffect, useRef, useState } from "react";
import type { PDFDocumentProxy, PDFPageProxy } from "pdfjs-dist";
import { Thread } from "../api";
import { MinusIcon, PlusIcon } from "./Icons";

/** What the user points at in a laid-out document (sent with the question). */
export interface PageTarget {
  kind: "text" | "page" | "area" | "image";
  page: number | null;
  unit: "page" | "slide";
  rect?: [number, number, number, number] | null; // x, y, w, h as fractions of the page
  label?: string | null;
}
export interface PageAsk {
  target: PageTarget;
  quote: string;
  image: string | null; // data: URL (page render or area crop)
}

const MAX_IMAGE_SIDE = 1800; // px of images sent to the agent

let pdfjsPromise: Promise<typeof import("pdfjs-dist")> | null = null;
function loadPdfjs() {
  // loaded on first use only (≈1 MB): most documents are not PDFs
  pdfjsPromise ??= Promise.all([import("pdfjs-dist"), import("pdfjs-dist/build/pdf.worker.min.mjs?url")]).then(([lib, worker]) => {
    lib.GlobalWorkerOptions.workerSrc = worker.default;
    return lib;
  });
  return pdfjsPromise;
}

function scaledCopy(src: HTMLCanvasElement, sx: number, sy: number, sw: number, sh: number): string {
  const k = Math.min(1, MAX_IMAGE_SIDE / Math.max(sw, sh));
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(sw * k));
  c.height = Math.max(1, Math.round(sh * k));
  const g = c.getContext("2d")!;
  g.fillStyle = "#fff";
  g.fillRect(0, 0, c.width, c.height);
  g.drawImage(src, sx, sy, sw, sh, 0, 0, c.width, c.height);
  return c.toDataURL("image/png");
}

/**
 * In-page PDF viewer (PDF.js) for PDF files and the Layout view of Office documents. Unlike the browser's own
 * viewer, its text can be selected and asked about, and each page/slide (or an area of it) can be sent to the
 * agent as an image.
 */
export default function PdfView({ url, unit, threads, activeThreadId, onAsk, onOpenThread, testId = "pdfjs-view" }: {
  url: string;
  unit: "page" | "slide";
  threads: Thread[];
  activeThreadId: string | null;
  onAsk: (a: PageAsk) => void;
  onOpenThread: (id: string) => void;
  testId?: string;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [sizes, setSizes] = useState<{ w: number; h: number }[]>([]); // page sizes at scale 1 (CSS px)
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState<number | "fit">("fit");
  const [width, setWidth] = useState(800);
  const [height, setHeight] = useState(700);
  const [areaPage, setAreaPage] = useState<number | null>(null); // page in "select area" mode
  const [chip, setChip] = useState<{ x: number; y: number; quote: string; page: number } | null>(null);
  const U = unit === "slide" ? "Slide" : "Page";

  // load the document
  useEffect(() => {
    let alive = true;
    let doc: PDFDocumentProxy | null = null;
    setPdf(null);
    setSizes([]);
    setError(null);
    void loadPdfjs()
      .then((lib) => lib.getDocument({ url, cMapUrl: "/pdfjs/cmaps/", cMapPacked: true, standardFontDataUrl: "/pdfjs/standard_fonts/", wasmUrl: "/pdfjs/wasm/", isEvalSupported: false }).promise)
      .then(async (d) => {
        doc = d;
        const s: { w: number; h: number }[] = [];
        for (let i = 1; i <= d.numPages; i++) {
          const v = (await d.getPage(i)).getViewport({ scale: 1 });
          s.push({ w: v.width, h: v.height });
        }
        if (alive) {
          setPdf(d);
          setSizes(s);
        }
      })
      .catch((e) => alive && setError(String((e as Error).message || e)));
    return () => {
      alive = false;
      void doc?.destroy();
    };
  }, [url]);

  // available width (fit-to-width zoom)
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      setWidth(Math.max(200, el.clientWidth - 40));
      setHeight(Math.max(200, el.clientHeight - 60));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // "fit": pages fill the width; slides are fitted whole (width and height), like a presentation
  const scaleFor = (i: number) => {
    if (zoom !== "fit") return zoom;
    const s = sizes[i] ?? { w: 600, h: 800 };
    const byWidth = width / s.w;
    return unit === "slide" ? Math.min(byWidth, height / s.h) : byWidth;
  };

  // open a page/slide discussion: scroll to its page
  useEffect(() => {
    const t = threads.find((x) => x.id === activeThreadId);
    const p = t?.target?.page;
    if (!p || !box.current) return;
    const el = box.current.querySelector<HTMLElement>(`[data-page="${p}"]`);
    el?.scrollIntoView({ block: "start", behavior: "smooth" });
    el?.classList.add("flash");
    window.setTimeout(() => el?.classList.remove("flash"), 1200);
  }, [activeThreadId, threads, sizes.length]);

  // text selected in a page's text layer → "Ask" chip
  const onMouseUp = useCallback((e: React.MouseEvent) => {
    window.setTimeout(() => {
      const sel = window.getSelection();
      const text = sel?.toString().replace(/\s+/g, " ").trim() ?? "";
      const node = sel?.anchorNode ? (sel.anchorNode.nodeType === 1 ? (sel.anchorNode as Element) : sel.anchorNode.parentElement) : null;
      const pageEl = node?.closest<HTMLElement>("[data-page]");
      if (!text || !pageEl || !box.current?.contains(pageEl)) return setChip(null);
      const r = box.current.getBoundingClientRect();
      setChip({ x: e.clientX - r.left + box.current.scrollLeft, y: e.clientY - r.top + box.current.scrollTop + 12, quote: text, page: Number(pageEl.dataset.page) });
    }, 10);
  }, []);

  const pageCanvas = (n: number) => box.current?.querySelector<HTMLCanvasElement>(`[data-page="${n}"] canvas`) ?? null;

  const askPage = (n: number) => {
    const c = pageCanvas(n);
    if (!c) return;
    onAsk({ target: { kind: "page", page: n, unit }, quote: "", image: scaledCopy(c, 0, 0, c.width, c.height) });
  };

  const counts = new Map<number, Thread[]>();
  for (const t of threads) if (t.target?.page) counts.set(t.target.page, [...(counts.get(t.target.page) ?? []), t]);

  if (error) return <div className="banner warn" data-testid={`${testId}-error`}>Could not show this PDF: {error}</div>;
  return (
    <div className="pdfjs-view" data-testid={testId} data-pages={sizes.length}>
      <div className="pdfjs-toolbar">
        <span className="muted small">{sizes.length ? `${sizes.length} ${U.toLowerCase()}${sizes.length === 1 ? "" : "s"}` : "Loading…"} · select text to ask about it, or use a {U.toLowerCase()}'s buttons</span>
        <span className="spacer" />
        <button className="btn small" onClick={() => setZoom((z) => Math.max(0.3, (z === "fit" ? scaleFor(0) : z) / 1.2))} aria-label="Zoom out" title="Zoom out"><MinusIcon /></button>
        <button className={`btn small ${zoom === "fit" ? "toggle on" : ""}`} onClick={() => setZoom("fit")} title={unit === "slide" ? "Fit each slide to the window" : "Fit to width"}>Fit</button>
        <button className="btn small" onClick={() => setZoom((z) => Math.min(5, (z === "fit" ? scaleFor(0) : z) * 1.2))} aria-label="Zoom in" title="Zoom in"><PlusIcon /></button>
      </div>
      <div className="pdfjs-pages" ref={box} onMouseUp={onMouseUp} onScroll={() => chip && setChip(null)}>
        {pdf &&
          sizes.map((s, i) => (
            <PdfPage
              key={`${i}-${url}`}
              pdf={pdf}
              n={i + 1}
              size={s}
              scale={scaleFor(i)}
              unit={U}
              discussions={counts.get(i + 1) ?? []}
              onOpenThread={onOpenThread}
              onAskPage={() => askPage(i + 1)}
              areaMode={areaPage === i + 1}
              onAreaMode={(on) => setAreaPage(on ? i + 1 : null)}
              onArea={(rect) => {
                setAreaPage(null);
                const c = pageCanvas(i + 1);
                if (!c) return;
                const [x, y, w, h] = rect;
                onAsk({ target: { kind: "area", page: i + 1, unit, rect }, quote: "", image: scaledCopy(c, x * c.width, y * c.height, w * c.width, h * c.height) });
              }}
            />
          ))}
        {chip && (
          <button
            className="btn small primary pdfjs-chip"
            style={{ left: chip.x, top: chip.y }}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              onAsk({ target: { kind: "text", page: chip.page, unit }, quote: chip.quote, image: null });
              setChip(null);
              window.getSelection()?.removeAllRanges();
            }}
            data-testid="pdfjs-ask-selection"
          >
            Ask about selection
          </button>
        )}
      </div>
    </div>
  );
}

function PdfPage({ pdf, n, size, scale, unit, discussions, onOpenThread, onAskPage, areaMode, onAreaMode, onArea }: {
  pdf: PDFDocumentProxy;
  n: number;
  size: { w: number; h: number };
  scale: number;
  unit: string;
  discussions: Thread[];
  onOpenThread: (id: string) => void;
  onAskPage: () => void;
  areaMode: boolean;
  onAreaMode: (on: boolean) => void;
  onArea: (rect: [number, number, number, number]) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const textDiv = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [rendered, setRendered] = useState(false);
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const w = Math.round(size.w * scale);
  const h = Math.round(size.h * scale);

  // render only pages near the viewport
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const io = new IntersectionObserver((es) => es.forEach((e) => e.isIntersecting && setVisible(true)), { rootMargin: "800px 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  useEffect(() => {
    if (!visible) return;
    let cancelled = false;
    let task: { cancel: () => void } | null = null;
    void (async () => {
      const lib = await loadPdfjs();
      const page: PDFPageProxy = await pdf.getPage(n);
      if (cancelled || !canvas.current || !textDiv.current) return;
      const vp = page.getViewport({ scale });
      const dpr = window.devicePixelRatio || 1;
      const c = canvas.current;
      c.width = Math.floor(vp.width * dpr);
      c.height = Math.floor(vp.height * dpr);
      const render = page.render({ canvas: c, canvasContext: c.getContext("2d")!, viewport: vp, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined });
      task = render;
      await render.promise;
      if (cancelled) return;
      const tl = textDiv.current;
      tl.replaceChildren();
      tl.style.setProperty("--total-scale-factor", String(scale));
      await new lib.TextLayer({ textContentSource: page.streamTextContent(), container: tl, viewport: vp }).render();
      if (!cancelled) setRendered(true);
    })().catch(() => undefined);
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [visible, pdf, n, scale]);

  const frac = (e: React.MouseEvent) => {
    const r = host.current!.querySelector(".pdfjs-page-body")!.getBoundingClientRect();
    return { x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) };
  };

  return (
    <div className="pdfjs-page" ref={host} data-page={n} data-rendered={rendered ? "true" : "false"} style={{ width: w }}>
      <div className="pdfjs-page-bar">
        <span className="pdfjs-page-no">{unit} {n}</span>
        {discussions.length > 0 && (
          <button className="link-btn small" onClick={() => onOpenThread(discussions[discussions.length - 1].id)} title={`${discussions.length} discussion(s) about this ${unit.toLowerCase()}`} data-testid="pdfjs-page-threads">
            {discussions.length} discussion{discussions.length === 1 ? "" : "s"}
          </button>
        )}
        <span className="spacer" />
        <button className="btn small" onClick={onAskPage} disabled={!rendered} title={`Ask the agent about this whole ${unit.toLowerCase()} (an image of it is attached)`} data-testid="pdfjs-ask-page">
          Ask about this {unit.toLowerCase()}
        </button>
        <button className={`btn small ${areaMode ? "toggle on" : ""}`} onClick={() => onAreaMode(!areaMode)} disabled={!rendered} title="Drag a rectangle around a figure, diagram or table to ask about just that" data-testid="pdfjs-select-area">
          {areaMode ? "Drag over the area…" : "Select area"}
        </button>
      </div>
      <div className="pdfjs-page-body" style={{ width: w, height: h }}>
        <canvas ref={canvas} style={{ width: w, height: h }} />
        <div className="textLayer" ref={textDiv} />
        {areaMode && (
          <div
            className="pdfjs-area"
            data-testid="pdfjs-area"
            onMouseDown={(e) => {
              e.preventDefault();
              const p = frac(e);
              setDrag({ x0: p.x, y0: p.y, x1: p.x, y1: p.y });
            }}
            onMouseMove={(e) => drag && setDrag({ ...drag, ...((p) => ({ x1: p.x, y1: p.y }))(frac(e)) })}
            onMouseUp={() => {
              if (!drag) return;
              const x = Math.min(drag.x0, drag.x1), y = Math.min(drag.y0, drag.y1);
              const rw = Math.abs(drag.x1 - drag.x0), rh = Math.abs(drag.y1 - drag.y0);
              setDrag(null);
              if (rw > 0.01 && rh > 0.01) onArea([x, y, rw, rh]);
            }}
            onKeyDown={(e) => e.key === "Escape" && onAreaMode(false)}
          >
            {drag && (
              <div className="pdfjs-area-rect" style={{ left: `${Math.min(drag.x0, drag.x1) * 100}%`, top: `${Math.min(drag.y0, drag.y1) * 100}%`, width: `${Math.abs(drag.x1 - drag.x0) * 100}%`, height: `${Math.abs(drag.y1 - drag.y0) * 100}%` }} />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
