import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, post } from "../../api";

interface Member { text: string; method: boolean; visibility: string }
interface UClass { name: string; path: string | null; line: number | null; kind: string; bases: string[]; members?: Member[]; others?: number }
interface UParent extends UClass { depth: number; child: string }
interface UUser { name: string; path: string; line: number; count: number; file: boolean }
interface Part { name: string; class: UClass | null; parents?: UParent[]; children?: UClass[]; users?: UUser[]; more_users?: number; error?: string;
  hidden_tests?: string[] }
type PartName = "class" | "parents" | "children" | "users";

/** A class (or a file using one) in the growing diagram. */
interface Node {
  id: string; name: string; path: string | null; line: number | null; kind: string; members: Member[] | null;
  file: boolean; count?: number; found: boolean;
  /** what has been looked up around it: loading / done per part (missing: not asked) */
  parts: Partial<Record<PartName, "loading" | "done" | "error">>;
  moreUsers?: number;
}
interface Edge { from: string; to: string; kind: "inherit" | "use"; count?: number }
interface Box { node: Node; x: number; y: number; w: number; h: number; sub: string; members: Member[] }

const CH = 7.2; // px per character, 12px monospace
const LINE = 16;
const HEAD = 26;
const GAP_X = 28;
const GAP_Y = 70;
const MAX_ROW = 1500;
const MAX_MEMBERS = 14;
const fit = (s: string, n: number) => (s.length > n ? s.slice(0, Math.max(1, n - 1)) + "…" : s);
const file = (p: string | null) => (p ? p.split("/").pop()! : "");
const ekey = (e: Edge) => `${e.kind}:${e.from}>${e.to}`;

function sizeOf(n: Node, members: Member[], sub: string) {
  const texts = [n.name + "  ⊕", sub, ...members.map((m) => `${m.visibility} ${m.text}`)];
  const w = Math.min(420, Math.max(130, Math.max(...texts.map((t) => Math.min(t.length, 56))) * CH + 28));
  return { w, h: HEAD + 14 + (members.length ? members.length * LINE + 10 : 0) };
}

/** Layered layout: parents above their children (inheritance ranks), users to the right of the row of the class
 * they use; each row ordered by its neighbours' positions in the row above and wrapped when too wide. */
function layout(nodes: Map<string, Node>, edges: Edge[], root: string): { boxes: Box[]; w: number; h: number } {
  const rank = new Map<string, number>([[root, 0]]);
  const inh = edges.filter((e) => e.kind === "inherit");
  const inherits = new Set(inh.flatMap((e) => [e.from, e.to]));
  for (let pass = 0; pass < nodes.size + 2; pass++) {
    let changed = false;
    for (const e of inh) {  // child → parent: the parent is one row above
      const c = rank.get(e.from), p = rank.get(e.to);
      if (c !== undefined && p === undefined) { rank.set(e.to, c - 1); changed = true; }
      else if (p !== undefined && c === undefined) { rank.set(e.from, p + 1); changed = true; }
      else if (c !== undefined && p !== undefined && p >= c && e.from !== root) { rank.set(e.from, p + 1); changed = true; }
    }
    if (!changed) break;
  }
  // users: on the row of the (first) class they use, in a group on its right
  const useOf = new Map<string, string>();
  for (const e of edges) if (e.kind === "use" && !rank.has(e.from) && !useOf.has(e.from)) useOf.set(e.from, e.to);
  for (const id of nodes.keys()) if (!rank.has(id) && !useOf.has(id)) rank.set(id, 0);
  const rowOf = (id: string) => rank.get(id) ?? rank.get(useOf.get(id) ?? root) ?? 0;
  const ranks = [...new Set([...nodes.keys()].map(rowOf))].sort((a, b) => a - b);
  const xs = new Map<string, number>();
  const boxes: Box[] = [];
  let y = 20;
  for (const r of ranks) {
    const ids = [...nodes.keys()].filter((id) => rowOf(id) === r);
    const neigh = (id: string) => {
      const ps = edges.filter((e) => e.from === id || e.to === id).map((e) => xs.get(e.from === id ? e.to : e.from)).filter((x): x is number => x !== undefined);
      return ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : Number.MAX_SAFE_INTEGER;
    };
    const order = (list: string[]) => list.map((id, i) => ({ id, k: neigh(id), i })).sort((a, b) => a.k - b.k || a.i - b.i).map((x) => x.id);
    const main = order(ids.filter((id) => rank.has(id)));
    const side = order(ids.filter((id) => !rank.has(id)));
    const items = [...main, ...side].map((id) => {
      const n = nodes.get(id)!;
      const members = (n.members ?? []).slice(0, MAX_MEMBERS);
      const sub = n.file ? `${n.count ?? 0} use${n.count === 1 ? "" : "s"} · file` : !n.found ? "not found in this worktree"
        : n.count && !inherits.has(id) ? `${n.count} use${n.count === 1 ? "" : "s"} · ${file(n.path)}` : `${n.kind} · ${file(n.path)}`;
      return { node: n, members, sub, side: !rank.has(id), ...sizeOf(n, members, sub) };
    });
    // wrap into lines of at most MAX_ROW, a gap before the users
    let line: typeof items = [];
    const flush = () => {
      if (!line.length) return;
      const total = line.reduce((s, b, i) => s + b.w + (i ? GAP_X + (b.side && !line[i - 1].side ? 60 : 0) : 0), 0);
      let x = Math.max(20, 700 - total / 2);
      const h = Math.max(...line.map((b) => b.h));
      line.forEach((b, i) => {
        if (i && b.side && !line[i - 1].side) x += 60;
        boxes.push({ node: b.node, x, y, w: b.w, h: b.h, sub: b.sub, members: b.members });
        xs.set(b.node.id, x + b.w / 2);
        x += b.w + GAP_X;
      });
      y += h + 26;
      line = [];
    };
    let width = 0;
    for (const it of items) {
      if (width + it.w > MAX_ROW && line.length) { flush(); width = 0; }
      line.push(it);
      width += it.w + GAP_X;
    }
    flush();
    y += GAP_Y - 26;
  }
  const w = Math.max(800, ...boxes.map((b) => b.x + b.w)) + 40;
  return { boxes, w, h: y };
}

/** A UML class diagram that grows in place: the class shows as soon as it is found; its parents, derived classes and
 * users are added as each search finishes, and any class in it can be expanded the same way (⊕ / double-click). */
export default function UmlDiagram({ devId, name, path, onOpen, onClose }: {
  devId: string; name: string; path: string | null; onOpen: (path: string, line: number) => void; onClose: () => void;
}) {
  const [nodes, setNodes] = useState<Map<string, Node>>(new Map());
  const [edges, setEdges] = useState<Map<string, Edge>>(new Map());
  const [root, setRoot] = useState<string>(name);
  const [err, setErr] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  // test classes / files are left out unless asked for (remembered per browser)
  const [tests, setTests] = useState(() => localStorage.getItem("waw.uml.tests") === "1");
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const hiddenTests = hidden.size;
  const gen = useRef(0); // a new start drops the answers of the previous one
  const clickTimer = useRef<number | null>(null);
  // boxes dragged to another place (diagram coordinates); their arrows follow
  const [moved, setMoved] = useState<Map<string, { x: number; y: number }>>(new Map());
  const drag = useRef<{ id: string; cx: number; cy: number; x: number; y: number; moved: boolean } | null>(null);
  const dragged = useRef(false);

  const upsert = useCallback((id: string, patch: Partial<Node> & { name: string }) => {
    setNodes((m) => {
      const cur = m.get(id);
      const n: Node = cur
        ? { ...cur, ...Object.fromEntries(Object.entries(patch).filter(([, v]) => v !== undefined)), parts: { ...cur.parts, ...(patch.parts ?? {}) } }
        : { id, path: null, line: null, kind: "class", members: null, file: false, found: true, parts: {}, ...patch };
      return new Map(m).set(id, n);
    });
  }, []);
  const link = useCallback((e: Edge) => setEdges((m) => (m.has(ekey(e)) ? m : new Map(m).set(ekey(e), e))), []);
  const setPart = useCallback((id: string, part: PartName, st: "loading" | "done" | "error") =>
    setNodes((m) => { const n = m.get(id); return n ? new Map(m).set(id, { ...n, parts: { ...n.parts, [part]: st } }) : m; }), []);

  const fetchParts = useCallback(async (g: number, cls: { name: string; path: string | null; line: number | null }, parts: PartName[]) => {
    const id = cls.name;
    for (const p of parts) setPart(id, p, "loading");
    await Promise.all(parts.map(async (part) => {
      try {
        const r = await post<Part>(`/dev/workspaces/${devId}/uml`, {
          name: cls.name, parts: [part], tests, ...(cls.path && cls.line ? { at_path: cls.path, at_line: cls.line } : { path: path ?? undefined }),
        });
        if (g !== gen.current) return;
        if (part === "class" && r.class) upsert(id, { name: id, path: r.class.path, line: r.class.line, kind: r.class.kind, members: r.class.members ?? [] });
        for (const p of r.parents ?? []) {
          upsert(p.name, { name: p.name, path: p.path, line: p.line, kind: p.kind, found: !!p.path, ...(p.members ? { members: p.members } : {}) });
          link({ from: p.child, to: p.name, kind: "inherit" });
        }
        for (const k of r.children ?? []) {
          upsert(k.name, { name: k.name, path: k.path, line: k.line, kind: k.kind });
          link({ from: k.name, to: id, kind: "inherit" });
        }
        for (const u of r.users ?? []) {
          const uid = u.file ? `file:${u.path}` : u.name;
          upsert(uid, { name: u.name, path: u.path, line: u.line, file: u.file, count: u.count, kind: "class" });
          if (uid !== id) link({ from: uid, to: id, kind: "use", count: u.count });
        }
        if (part === "users" && r.more_users) upsert(id, { name: id, moreUsers: r.more_users });
        if (r.hidden_tests?.length) setHidden((h) => new Set([...h, ...r.hidden_tests!]));
        setPart(id, part, "done");
      } catch (e) {
        if (g !== gen.current) return;
        setPart(id, part, "error");
        setErr((e as ApiError).message);
      }
    }));
  }, [devId, path, tests, upsert, link, setPart]);

  // start: find the class quickly, then its surroundings in parallel
  const start = useCallback(async (nm: string) => {
    const g = ++gen.current;
    setNodes(new Map());
    setEdges(new Map());
    setMoved(new Map());
    setRoot(nm);
    setErr(null);
    setHidden(new Set());
    upsert(nm, { name: nm, parts: { class: "loading" } });
    try {
      const r = await post<Part>(`/dev/workspaces/${devId}/uml`, { name: nm, path: path ?? undefined, parts: ["class"], tests });
      if (g !== gen.current) return;
      if (r.error) return setErr(r.error);
      if (!r.class) return upsert(nm, { name: nm, found: false, parts: { class: "done" } });
      const c = r.class;
      upsert(nm, { name: nm, path: c.path, line: c.line, kind: c.kind, members: c.members ?? [], parts: { class: "done" } });
      void fetchParts(g, c, ["parents", "children", "users"]);
    } catch (e) {
      if (g === gen.current) setErr((e as ApiError).message);
    }
  }, [devId, path, tests, upsert, fetchParts]);
  useEffect(() => { void start(name); }, [name, start]); // also again when test classes are switched on / off

  const expand = useCallback((n: Node) => {
    if (n.file || !n.found) return;
    const want = (["class", "parents", "children", "users"] as PartName[]).filter((p) => !n.parts[p] && !(p === "class" && n.members));
    if (want.length) void fetchParts(gen.current, n, want);
  }, [fetchParts]);
  const expandable = (n: Node) => !n.file && n.found && (["parents", "children", "users"] as PartName[]).some((p) => !n.parts[p]);
  const expandAll = () => { for (const n of nodes.values()) if (expandable(n)) expand(n); };

  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);

  // a use between classes already linked by inheritance (a derived class names its parent) is not drawn again
  const edgeList = useMemo(() => {
    const all = [...edges.values()].filter((e) => nodes.has(e.from) && nodes.has(e.to) && e.from !== e.to);
    const inh = new Set(all.filter((e) => e.kind === "inherit").flatMap((e) => [`${e.from}>${e.to}`, `${e.to}>${e.from}`]));
    return all.filter((e) => e.kind === "inherit" || !inh.has(`${e.from}>${e.to}`));
  }, [edges, nodes]);
  const lay = useMemo(() => (nodes.size ? layout(nodes, edgeList, root) : null), [nodes, edgeList, root]);
  const boxes = useMemo(() => (lay?.boxes ?? []).map((b) => { const m = moved.get(b.node.id); return m ? { ...b, x: m.x, y: m.y } : b; }), [lay, moved]);
  const byId = useMemo(() => new Map(boxes.map((b) => [b.node.id, b])), [boxes]);
  const size = useMemo(() => ({
    w: Math.max(lay?.w ?? 0, ...boxes.map((b) => b.x + b.w + 20)),
    h: Math.max(lay?.h ?? 0, ...boxes.map((b) => b.y + b.h + 20)),
  }), [lay, boxes]);
  // dragging a box: it follows the pointer (at the current zoom); a drag is not a click
  useEffect(() => {
    const move = (e: PointerEvent) => {
      const d = drag.current;
      if (!d) return;
      const dx = (e.clientX - d.cx) / zoom, dy = (e.clientY - d.cy) / zoom;
      if (!d.moved && Math.hypot(dx, dy) < 4) return;
      d.moved = true;
      setMoved((m) => new Map(m).set(d.id, { x: Math.max(0, Math.round(d.x + dx)), y: Math.max(0, Math.round(d.y + dy)) }));
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
  const rootNode = nodes.get(root);
  // ancestors / descendants of the class the diagram started from (colours)
  const [ups, downs] = useMemo(() => {
    const walk = (from: string, dir: "up" | "down") => {
      const seen = new Set<string>();
      const todo = [from];
      while (todo.length) {
        const x = todo.pop()!;
        for (const e of edgeList) {
          if (e.kind !== "inherit") continue;
          const nxt = dir === "up" ? (e.from === x ? e.to : null) : (e.to === x ? e.from : null);
          if (nxt && !seen.has(nxt) && nxt !== from) { seen.add(nxt); todo.push(nxt); }
        }
      }
      return seen;
    };
    return [walk(root, "up"), walk(root, "down")];
  }, [edgeList, root]);
  const loading = [...nodes.values()].flatMap((n) => Object.entries(n.parts).filter(([, s]) => s === "loading").map(([p]) => `${p} of ${n.name}`));
  const counts = useMemo(() => ({
    classes: [...nodes.values()].filter((n) => !n.file).length,
    inherit: edgeList.filter((e) => e.kind === "inherit").length,
    use: edgeList.filter((e) => e.kind === "use").length,
  }), [nodes, edgeList]);

  const edgePath = (e: Edge) => {
    const a = byId.get(e.from), b = byId.get(e.to);
    if (!a || !b) return null;
    if (e.kind === "use") {
      const left = a.x > b.x + b.w;
      const x1 = left ? a.x : a.x + a.w, y1 = a.y + Math.min(a.h / 2, 30);
      const x2 = left ? b.x + b.w : b.x, y2 = b.y + Math.min(b.h / 2, 30);
      const dx = Math.max(40, Math.abs(x1 - x2) / 3) * (left ? -1 : 1);
      return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
    }
    const up = a.y > b.y;
    const x1 = a.x + a.w / 2, y1 = up ? a.y : a.y + a.h, x2 = b.x + b.w / 2, y2 = up ? b.y + b.h : b.y;
    const my = (y1 + y2) / 2;
    return `M${x1},${y1} L${x1},${my} L${x2},${my} L${x2},${y2}`;
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal uml-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={`UML class diagram of ${root}`} data-testid="uml-diagram">
        <div className="modal-head">
          <span>UML class diagram · <b className="mono">{root}</b></span>
          <span className="muted small" data-testid="uml-counts">{counts.classes} classes · {counts.inherit} inheritance · {counts.use} uses
            {rootNode?.moreUsers ? ` (+${rootNode.moreUsers} more users)` : ""}</span>
          {hiddenTests > 0 && !tests && <span className="muted small" data-testid="uml-hidden-tests" title={`Left out: ${[...hidden].slice(0, 20).join(", ")}${hidden.size > 20 ? " …" : ""}. Tick “Test classes” to show them.`}>{hiddenTests} test class{hiddenTests === 1 ? "" : "es"} hidden</span>}
          {loading.length > 0 && <span className="muted small uml-loading" title={loading.join("\n")} data-testid="uml-loading">⏳ loading {loading[0]}{loading.length > 1 ? ` +${loading.length - 1}` : ""}</span>}
          <span className="spacer" />
          <label className="small" title="Show test classes and test files too (in tests/ folders, test_*.py, *_test.cc …, or named TestFoo / FooTest / MockFoo)">
            <input type="checkbox" checked={tests} onChange={(e) => { setTests(e.target.checked); localStorage.setItem("waw.uml.tests", e.target.checked ? "1" : "0"); }} data-testid="uml-tests" /> Test classes
          </label>
          <button className="btn small" onClick={expandAll} disabled={![...nodes.values()].some(expandable)}
            title="Expand every class shown: add its parents, derived classes and users" data-testid="uml-expand-all">⊕ Expand all</button>
          {moved.size > 0 && <button className="btn small" onClick={() => setMoved(new Map())} title="Put the boxes you dragged back where the diagram placed them" data-testid="uml-relayout">Tidy up</button>}
          <button className="btn small" onClick={() => void start(root)} disabled={nodes.size <= 1} title="Back to just this class and its surroundings" data-testid="uml-reset">Reset</button>
          <button className="icon-btn small" onClick={() => setZoom((z) => Math.max(0.3, z / 1.2))} title="Zoom out">−</button>
          <span className="muted small">{Math.round(zoom * 100)}%</span>
          <button className="icon-btn small" onClick={() => setZoom((z) => Math.min(2.5, z * 1.2))} title="Zoom in">+</button>
          <button className="icon-btn" onClick={onClose} aria-label="Close" data-testid="uml-close">×</button>
        </div>
        <div className="uml-legend muted small">
          ▲ inherits from · ⇠ uses · drag a box to move it (its arrows follow) · ⊕ (or double-click) expands a class here: its members, parents, derived classes and users
          are added to this diagram · click a box to open its source. Found by searching the worktree (no compiler): template /
          macro tricks and base lists spread over many lines can be missed.
        </div>
        <div className="uml-canvas">
          {err && <div className="error small pad">{err}</div>}
          {rootNode && !rootNode.found && <div className="muted small pad" data-testid="uml-none">No class or struct named <code>{root}</code> was found in this worktree.</div>}
          {lay && rootNode?.found !== false && (
            <svg width={size.w * zoom} height={size.h * zoom} viewBox={`0 0 ${size.w} ${size.h}`} className="uml-svg">
              <defs>
                <marker id="uml-tri" viewBox="0 0 12 12" refX="11" refY="6" markerWidth="12" markerHeight="12" orient="auto-start-reverse">
                  <path d="M0,0 L12,6 L0,12 z" className="uml-tri" />
                </marker>
                <marker id="uml-open" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse">
                  <path d="M0,0 L10,5 L0,10" className="uml-open" />
                </marker>
              </defs>
              {edgeList.map((e) => {
                const d = edgePath(e);
                return d ? <path key={ekey(e)} d={d} className={`uml-edge ${e.kind}`} markerEnd={e.kind === "inherit" ? "url(#uml-tri)" : "url(#uml-open)"} /> : null;
              })}
              {boxes.map((b) => {
                const n = b.node;
                const role = n.id === root ? "self" : ups.has(n.id) ? "parent" : downs.has(n.id) ? "child" : n.file || edgeList.some((e) => e.kind === "use" && e.from === n.id) ? "user" : "other";
                const busy = Object.values(n.parts).includes("loading");
                return (
                  <g key={n.id} className={`uml-box ${role}${n.found ? "" : " missing"}${n.file ? " file" : ""}${busy ? " busy" : ""}`} transform={`translate(${b.x},${b.y})`}
                    onPointerDown={(e) => {
                      if (e.button !== 0 || (e.target as Element).closest(".uml-expand")) return;
                      drag.current = { id: n.id, cx: e.clientX, cy: e.clientY, x: b.x, y: b.y, moved: false };
                      document.body.classList.add("uml-dragging");
                    }}
                    onClick={() => {
                      if (dragged.current) return;
                      if (clickTimer.current) window.clearTimeout(clickTimer.current);
                      clickTimer.current = window.setTimeout(() => { clickTimer.current = null; if (n.path && n.line) onOpen(n.path, n.line); }, 250);
                    }}
                    onDoubleClick={(e) => {
                      e.stopPropagation();
                      if (clickTimer.current) window.clearTimeout(clickTimer.current);
                      clickTimer.current = null;
                      expand(n);
                    }}
                    data-testid={`uml-${role}`} data-name={n.name} data-expanded={expandable(n) ? "0" : "1"}>
                    <title>{n.path ? `${n.path}:${n.line}` : "not found in this worktree"}</title>
                    <rect width={b.w} height={b.h} rx={4} />
                    <text x={b.w / 2} y={17} textAnchor="middle" className="uml-title">{fit(n.name, Math.floor((b.w - 40) / CH))}</text>
                    {expandable(n) && (
                      <g className="uml-expand" transform={`translate(${b.w - 20},4)`} onClick={(e) => { e.stopPropagation(); expand(n); }} data-testid="uml-expand">
                        <title>Expand here: members, parents, derived classes and users of {n.name}</title>
                        <rect width={16} height={16} rx={3} /><text x={8} y={12.5} textAnchor="middle">+</text>
                      </g>
                    )}
                    {busy && <text x={b.w - 12} y={17} textAnchor="middle" className="uml-spin">⏳</text>}
                    <text x={b.w / 2} y={HEAD + 6} textAnchor="middle" className="uml-sub">{fit(b.sub, Math.floor((b.w - 12) / 6.2))}</text>
                    {b.members.length > 0 && <line x1={0} x2={b.w} y1={HEAD + 14} y2={HEAD + 14} />}
                    {b.members.map((m, i) => (
                      <text key={i} x={8} y={HEAD + 14 + 16 + i * LINE} className={`uml-member ${m.method ? "method" : "field"}`}>
                        {fit(`${m.visibility} ${m.text}`, Math.floor((b.w - 14) / CH))}
                      </text>
                    ))}
                  </g>
                );
              })}
            </svg>
          )}
        </div>
      </div>
    </div>
  );
}
