import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, post } from "../../api";

/** What the diagrams besides the UML one show (see backend development/graphs.py). */
export type GraphKind = "calls" | "deps" | "op" | "impact";
export interface GraphReq { kind: GraphKind; name?: string; path?: string | null }
interface GNode { id: string; label: string; sub?: string; path?: string | null; line?: number | null; col: number; kind: string; test?: boolean;
  fn?: string | null; more?: number; detail?: string; count?: number }
interface GEdge { from: string; to: string; label?: string }
interface Graph { kind: GraphKind; title: string; nodes: GNode[]; edges: GEdge[]; notes: string[]; hidden_tests: string[]; center: string | null; error?: string }

const CH = 7.2;
const BOX_H = 42;
const GAP_X = 90;
const GAP_Y = 14;
const fit = (s: string, n: number) => (s.length > n ? s.slice(0, Math.max(1, n - 1)) + "…" : s);

const LEGEND: Record<GraphKind, string> = {
  calls: "← callers · what it calls → · ⊕ on a box adds its own callers (left) or what it calls (right)",
  deps: "← files that include / import it · what it includes / imports →",
  op: "← autograd formula, decompositions and Inductor lowerings · native_functions.yaml entries · kernels per dispatch key →",
  impact: "changed files → the functions changed in them ← what calls those functions ← tests that call them",
};

function toMermaid(g: Graph): string {
  const id = new Map(g.nodes.map((n, i) => [n.id, `n${i}`]));
  const esc = (s: string) => s.replace(/["\n]/g, " ");
  return ["flowchart LR", ...g.nodes.map((n) => `  ${id.get(n.id)}["${esc(n.label)}${n.sub ? `<br/><small>${esc(n.sub)}</small>` : ""}"]`),
    ...g.edges.filter((e) => id.has(e.from) && id.has(e.to)).map((e) => `  ${id.get(e.from)} -->${e.label ? `|${esc(e.label)}|` : ""} ${id.get(e.to)}`)].join("\n");
}

/** A diagram laid out in columns (callers | function | callees, importers | file | imports …): drag boxes, click to open
 * the source, ⊕ to grow a call graph, Copy Mermaid to paste it into a document or an agent question. */
export default function GraphView({ devId, req, onClose, onOpen }: {
  devId: string; req: GraphReq; onClose: () => void; onOpen: (path: string, line: number) => void;
}) {
  const [g, setG] = useState<Graph | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>("loading");
  const [tests, setTests] = useState(() => localStorage.getItem("waw.graph.tests") === "1");
  const [zoom, setZoom] = useState(1);
  const [moved, setMoved] = useState<Map<string, { x: number; y: number }>>(new Map());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [copied, setCopied] = useState(false);
  const drag = useRef<{ id: string; cx: number; cy: number; x: number; y: number; moved: boolean } | null>(null);
  const dragged = useRef(false);

  const load = useCallback(async () => {
    setBusy("loading"); setErr(null); setMoved(new Map()); setExpanded(new Set());
    try {
      const r = await post<Graph>(`/dev/workspaces/${devId}/graph`, { kind: req.kind, name: req.name ?? null, path: req.path ?? null, tests });
      if (r.error) setErr(r.error);
      setG(r.error ? null : r);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [devId, req.kind, req.name, req.path, tests]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  const canExpand = (n: GNode) => req.kind === "calls" && n.col !== 0 && !!n.fn && !!n.path && !expanded.has(n.id);
  const expand = async (n: GNode) => {
    if (!g || !canExpand(n)) return;
    const side = n.col < 0 ? "callers" : "callees";
    setBusy(`${side} of ${n.label}`);
    setExpanded((s) => new Set(s).add(n.id));
    try {
      const r = await post<Graph>(`/dev/workspaces/${devId}/graph`, { kind: "calls", name: n.fn, path: n.path, at_path: n.path, at_line: n.line, parts: [side], tests });
      setG((cur) => {
        if (!cur || r.error) return cur;
        const have = new Map(cur.nodes.map((x) => [x.id, x]));
        const nodes = [...cur.nodes];
        const edges = [...cur.edges];
        const seenE = new Set(edges.map((e) => `${e.from}>${e.to}`));
        for (const x of r.nodes) {
          if (x.id === r.center) continue;
          if (!have.has(x.id)) { const nx = { ...x, col: n.col + (n.col < 0 ? -1 : 1) }; nodes.push(nx); have.set(x.id, nx); }
        }
        for (const e of r.edges) {
          const from = e.from === r.center ? n.id : e.from, to = e.to === r.center ? n.id : e.to;
          if (from !== to && !seenE.has(`${from}>${to}`)) { seenE.add(`${from}>${to}`); edges.push({ ...e, from, to }); }
        }
        return { ...cur, nodes, edges, notes: [...cur.notes, ...r.notes.map((t) => `${n.label}: ${t}`)] };
      });
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const lay = useMemo(() => {
    if (!g) return null;
    const cols = [...new Set(g.nodes.map((n) => n.col))].sort((a, b) => a - b);
    const width = (n: GNode) => Math.min(340, Math.max(120, Math.max(n.label.length + 3, Math.min(44, (n.sub ?? "").length) * 0.85) * CH + 24));
    const colW = new Map(cols.map((c) => [c, Math.max(...g.nodes.filter((n) => n.col === c).map(width))]));
    const heights = cols.map((c) => g.nodes.filter((n) => n.col === c).length * (BOX_H + GAP_Y));
    const H = Math.max(...heights, BOX_H) + 20;
    const boxes = new Map<string, { n: GNode; x: number; y: number; w: number }>();
    let x = 10;
    for (const c of cols) {
      const list = g.nodes.filter((n) => n.col === c);
      let y = (H - list.length * (BOX_H + GAP_Y)) / 2 + GAP_Y / 2;
      for (const n of list) {
        const m = moved.get(n.id);
        boxes.set(n.id, { n, x: m?.x ?? x, y: m?.y ?? y, w: width(n) });
        y += BOX_H + GAP_Y;
      }
      x += colW.get(c)! + GAP_X;
    }
    let w = x - GAP_X + 10, h = H;
    for (const b of boxes.values()) { w = Math.max(w, b.x + b.w + 10); h = Math.max(h, b.y + BOX_H + 10); }
    return { boxes, w, h };
  }, [g, moved]);

  useEffect(() => {
    const move = (e: PointerEvent) => {
      const d = drag.current;
      if (!d) return;
      const dx = (e.clientX - d.cx) / zoom, dy = (e.clientY - d.cy) / zoom;
      if (!d.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
      d.moved = true;
      setMoved((m) => new Map(m).set(d.id, { x: Math.max(0, d.x + dx), y: Math.max(0, d.y + dy) }));
    };
    const up = () => {
      if (drag.current?.moved) { dragged.current = true; window.setTimeout(() => { dragged.current = false; }, 0); }
      drag.current = null;
      document.body.classList.remove("uml-dragging");
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
  }, [zoom]);

  const edgePath = (e: GEdge) => {
    const a = lay?.boxes.get(e.from), b = lay?.boxes.get(e.to);
    if (!a || !b) return null;
    const right = a.x + a.w <= b.x, left = b.x + b.w <= a.x;
    const x1 = right ? a.x + a.w : left ? a.x : a.x + a.w, y1 = a.y + BOX_H / 2;
    const x2 = right ? b.x : left ? b.x + b.w : b.x + b.w, y2 = b.y + BOX_H / 2;
    const dx = right || left ? Math.max(30, Math.abs(x2 - x1) / 2.5) * (left ? -1 : 1) : 50;
    return { d: `M${x1},${y1} C${x1 + dx},${y1} ${x2 - (right || left ? dx : -dx)},${y2} ${x2},${y2}`, mx: (x1 + x2) / 2, my: (y1 + y2) / 2 };
  };
  const copy = async () => {
    if (!g) return;
    try { await navigator.clipboard.writeText("```mermaid\n" + toMermaid(g) + "\n```"); setCopied(true); window.setTimeout(() => setCopied(false), 1500); } catch { /* no clipboard */ }
  };
  const hidden = g?.hidden_tests ?? [];
  const title = g?.title ?? (req.kind === "calls" ? `Calls of ${req.name}` : req.kind === "deps" ? `Dependencies of ${req.path?.split("/").pop()}` :
    req.kind === "op" ? `Operator ${req.name}` : "Impact of the changes");

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal uml-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={title} data-testid="graph-view" data-kind={req.kind}>
        <div className="modal-head">
          <span><b>{title}</b></span>
          {g && <span className="muted small" data-testid="graph-counts">{g.nodes.length} boxes · {g.edges.length} arrows</span>}
          {hidden.length > 0 && !tests && <span className="muted small" title={`Left out: ${hidden.slice(0, 20).join(", ")}`} data-testid="graph-hidden-tests">{hidden.length} test{hidden.length === 1 ? "" : "s"} hidden</span>}
          {busy && <span className="muted small uml-loading" data-testid="graph-loading">⏳ {busy === "loading" ? "searching the worktree…" : busy}</span>}
          <span className="spacer" />
          {req.kind !== "op" && req.kind !== "impact" && (
            <label className="small" title="Show tests too (in tests/ folders, test_*.py, *_test.cc …)">
              <input type="checkbox" checked={tests} onChange={(e) => { setTests(e.target.checked); localStorage.setItem("waw.graph.tests", e.target.checked ? "1" : "0"); }} data-testid="graph-tests" /> Tests
            </label>
          )}
          {moved.size > 0 && <button className="btn small" onClick={() => setMoved(new Map())} data-testid="graph-relayout">Tidy up</button>}
          <button className="btn small" onClick={() => void copy()} disabled={!g} title="Copy as a ```mermaid block, to paste into a document or an agent question" data-testid="graph-copy">{copied ? "Copied" : "Copy Mermaid"}</button>
          <button className="icon-btn small" onClick={() => setZoom((z) => Math.max(0.3, z / 1.2))} title="Zoom out">−</button>
          <span className="muted small">{Math.round(zoom * 100)}%</span>
          <button className="icon-btn small" onClick={() => setZoom((z) => Math.min(2.5, z * 1.2))} title="Zoom in">+</button>
          <button className="icon-btn" onClick={onClose} aria-label="Close" data-testid="graph-close">×</button>
        </div>
        <div className="uml-legend muted small">{LEGEND[req.kind]} · drag a box to move it · click a box to open its source. Found by searching the worktree (no compiler), so macros, overloads and calls through pointers can be missed.</div>
        <div className="uml-canvas">
          {err && <div className="error small pad" data-testid="graph-error">{err}</div>}
          {g && g.nodes.length === 0 && <div className="muted small pad" data-testid="graph-empty">Nothing to show.</div>}
          {lay && g && g.nodes.length > 0 && (
            <svg width={lay.w * zoom} height={lay.h * zoom} viewBox={`0 0 ${lay.w} ${lay.h}`} className="uml-svg">
              <defs>
                <marker id="graph-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
                  <path d="M0,0 L10,5 L0,10" className="uml-open" />
                </marker>
              </defs>
              {g.edges.map((e, i) => {
                const p = edgePath(e);
                return p ? (
                  <g key={i}>
                    <path d={p.d} className="uml-edge graph-edge" markerEnd="url(#graph-arrow)" />
                    {e.label && <text x={p.mx} y={p.my - 3} textAnchor="middle" className="graph-edge-label">{fit(e.label, 22)}</text>}
                  </g>
                ) : null;
              })}
              {[...lay.boxes.values()].map(({ n, x, y, w }) => {
                const role = n.kind === "subject" ? "self" : n.kind === "missing" || n.kind === "deleted" ? "missing" : n.test || n.kind === "test" ? "user" :
                  n.col < 0 ? "parent" : n.col > 0 ? "child" : "other";
                return (
                  <g key={n.id} className={`uml-box ${role}`} transform={`translate(${x},${y})`} data-testid={`graph-node-${role}`} data-name={n.label} data-col={n.col}
                    onPointerDown={(e) => {
                      if (e.button !== 0 || (e.target as Element).closest(".uml-expand")) return;
                      drag.current = { id: n.id, cx: e.clientX, cy: e.clientY, x, y, moved: false };
                      document.body.classList.add("uml-dragging");
                    }}
                    onClick={() => { if (!dragged.current && n.path && n.line) onOpen(n.path, n.line); }}
                    onDoubleClick={() => void expand(n)}>
                    <title>{[n.detail, n.path ? `${n.path}:${n.line}` : "not found in this worktree", n.count && n.count > 1 ? `${n.count} calls` : "",
                      n.more ? `+${n.more} more callers` : ""].filter(Boolean).join("\n")}</title>
                    <rect width={w} height={BOX_H} rx={4} />
                    <text x={w / 2} y={17} textAnchor="middle" className="uml-title">{fit(n.label + (n.more ? ` (+${n.more})` : ""), Math.floor((w - 36) / CH))}</text>
                    <text x={w / 2} y={33} textAnchor="middle" className="uml-sub">{fit(n.sub ?? "", Math.floor((w - 12) / 6.2))}</text>
                    {canExpand(n) && (
                      <g className="uml-expand" transform={`translate(${n.col < 0 ? 4 : w - 20},4)`} onClick={(e) => { e.stopPropagation(); void expand(n); }} data-testid="graph-expand">
                        <title>{n.col < 0 ? `Add the callers of ${n.label}` : `Add what ${n.label} calls`}</title>
                        <rect width={16} height={16} rx={3} /><text x={8} y={12.5} textAnchor="middle">+</text>
                      </g>
                    )}
                  </g>
                );
              })}
            </svg>
          )}
          {g && g.notes.length > 0 && <ul className="graph-notes muted small" data-testid="graph-notes">{g.notes.map((t, i) => <li key={i}>{t}</li>)}</ul>}
        </div>
      </div>
    </div>
  );
}
