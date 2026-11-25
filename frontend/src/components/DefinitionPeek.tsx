import { useEffect, useRef, useState } from "react";
import { ApiError, get } from "../api";

export interface DefHit { path: string; line: number; text: string; kind: string; rev: "head" | "base" }
interface DefResult { symbol: string; scope: "repository" | "pr-files"; note: string | null; results: DefHit[] }
export interface DefRequest { symbol: string; path: string | null; at: number }

/** The identifier under the pointer of a Ctrl/⌘-click (null when the click is not on a name). */
export function symbolAt(e: { clientX: number; clientY: number }): string | null {
  const d = document as Document & { caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null };
  let node: Node | null = null;
  let off = 0;
  if (d.caretPositionFromPoint) {
    const p = d.caretPositionFromPoint(e.clientX, e.clientY);
    if (p) { node = p.offsetNode; off = p.offset; }
  } else if (document.caretRangeFromPoint) {
    const r = document.caretRangeFromPoint(e.clientX, e.clientY);
    if (r) { node = r.startContainer; off = r.startOffset; }
  }
  if (!node || node.nodeType !== Node.TEXT_NODE) return null;
  const t = node.textContent ?? "";
  const isId = (c: string) => /[A-Za-z0-9_]/.test(c);
  let a = off, b = off;
  while (a > 0 && isId(t[a - 1])) a--;
  while (b < t.length && isId(t[b])) b++;
  const w = t.slice(a, b);
  return /^[A-Za-z_]\w*$/.test(w) ? w : null;
}

/** Handlers for code: Ctrl/⌘-click a name to look up its definition (instead of starting a line selection). */
export function defClick(onDefinition: ((symbol: string) => void) | undefined) {
  return {
    onPointerDownCapture: (e: React.PointerEvent) => {
      if (!onDefinition || e.button !== 0 || !(e.ctrlKey || e.metaKey)) return;
      const s = symbolAt(e);
      if (!s) return;
      e.preventDefault();
      e.stopPropagation();
      onDefinition(s);
    },
  };
}

interface View { req: { symbol: string; path: string | null }; result: DefResult | null; error: string | null; open: DefHit | null }

/** Go to definition, as a peek panel over the PR's diff: the places a name is defined, and the file at the chosen one
 * (where Ctrl/⌘-click goes further; ← goes back). */
export default function DefinitionPeek({ apiBase, request, onClose }: { apiBase: string; request: DefRequest | null; onClose: () => void }) {
  const [stack, setStack] = useState<View[]>([]);
  const [lines, setLines] = useState<string[] | null>(null);
  const [fileErr, setFileErr] = useState<string | null>(null);
  const target = useRef<HTMLTableRowElement | null>(null);
  const view = stack[stack.length - 1] ?? null;

  const search = async (symbol: string, path: string | null, fresh: boolean) => {
    const v: View = { req: { symbol, path }, result: null, error: null, open: null };
    setStack((s) => (fresh ? [v] : [...s, v]));
    try {
      const q = new URLSearchParams({ symbol, ...(path ? { path } : {}) });
      const r = await get<DefResult>(`${apiBase}/definition?${q}`);
      setStack((s) => s.map((x) => (x === v ? { ...x, result: r, open: r.results.length === 1 ? r.results[0] : null } : x)));
    } catch (e) {
      setStack((s) => s.map((x) => (x === v ? { ...x, error: (e as ApiError).message } : x)));
    }
  };
  useEffect(() => { if (request) void search(request.symbol, request.path, true); else setStack([]); }, [request]); // eslint-disable-line react-hooks/exhaustive-deps

  const open = view?.open ?? null;
  useEffect(() => {
    setLines(null);
    setFileErr(null);
    if (!open) return;
    let live = true;
    get<{ lines: string[] }>(`${apiBase}/source?${new URLSearchParams({ path: open.path, rev: open.rev })}`)
      .then((r) => { if (live) setLines(r.lines); })
      .catch((e) => { if (live) setFileErr((e as ApiError).message); });
    return () => { live = false; };
  }, [apiBase, open]);
  useEffect(() => { if (lines) target.current?.scrollIntoView({ block: "center" }); }, [lines]);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape" && view) onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [view, onClose]);

  if (!view) return null;
  const choose = (h: DefHit | null) => setStack((s) => s.map((x, i) => (i === s.length - 1 ? { ...x, open: h } : x)));
  const back = () => {
    if (view.open && (view.result?.results.length ?? 0) > 1) return choose(null);
    setStack((s) => s.slice(0, -1));
  };
  const canBack = stack.length > 1 || (!!view.open && (view.result?.results.length ?? 0) > 1);
  const r = view.result;
  return (
    <aside className="def-peek" data-testid="def-peek" aria-label={`Definition of ${view.req.symbol}`}>
      <header className="def-peek-head">
        <button className="icon-btn small" disabled={!canBack} onClick={back} title="Back" data-testid="def-back">←</button>
        <b className="def-sym">{view.req.symbol}</b>
        {open && <span className="muted small def-where" title={open.path}>{open.path}:{open.line}{open.rev === "base" ? " (base)" : ""}</span>}
        <span className="spacer" />
        <button className="icon-btn" onClick={onClose} aria-label="Close" data-testid="def-close">×</button>
      </header>
      {!r && !view.error && <div className="muted small pad">Searching for the definition…</div>}
      {view.error && <div className="error small pad">{view.error}</div>}
      {r && !open && (
        <div className="def-list">
          {r.note && <div className="muted small pad" data-testid="def-note">{r.note}</div>}
          {r.results.length === 0 && <div className="muted small pad" data-testid="def-none">No definition of <code>{r.symbol}</code> found{r.scope === "pr-files" ? " in the PR's files" : ""}.</div>}
          {r.results.map((h) => (
            <button key={`${h.rev}:${h.path}:${h.line}`} className="def-hit" onClick={() => choose(h)} data-testid="def-hit">
              <span className="small"><b>{h.path}</b>:{h.line} <span className="muted">· {h.kind}{h.rev === "base" ? " · base (removed by the PR)" : ""}</span></span>
              <code className="def-text">{h.text}</code>
            </button>
          ))}
        </div>
      )}
      {open && (
        <div className="def-file" {...defClick((s) => void search(s, open.path, false))}>
          {fileErr && <div className="error small pad">{fileErr}</div>}
          {!lines && !fileErr && <div className="muted small pad">Loading {open.path}…</div>}
          {lines && (
            <table className="diff-table def-src" data-testid="def-source">
              <tbody>
                {lines.map((t, i) => (
                  <tr key={i} ref={i + 1 === open.line ? target : undefined} className={`diff-line ctx ${i + 1 === open.line ? "def-target" : ""}`}>
                    <td className="no">{i + 1}</td>
                    <td className="code">{t || " "}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
      <footer className="muted small def-foot">Ctrl/⌘-click a name here to go further · Esc closes</footer>
    </aside>
  );
}
