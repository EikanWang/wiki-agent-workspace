import { useEffect, useState } from "react";
import { ApiError, post } from "../../api";
import MarkdownView from "../MarkdownView";

interface Step { from: string; to: string; path: string; line: number; to_path: string; to_line: number; depth: number }
interface Seq { title: string; mermaid: string; steps: Step[]; notes: string[]; hidden_tests: string[]; subject?: { path: string; line: number }; error?: string }

/** A sequence diagram of what a function does when it runs: its calls in order, followed `depth` levels down
 * (see backend graphs.py `sequence`). Drawn by Mermaid; the steps below open the line of each call. */
export default function SequenceView({ devId, name, path, onClose, onOpen }: {
  devId: string; name: string; path: string | null; onClose: () => void; onOpen: (path: string, line: number) => void;
}) {
  const [depth, setDepth] = useState(() => Number(localStorage.getItem("waw.seq.depth")) || 2);
  const [tests, setTests] = useState(() => localStorage.getItem("waw.graph.tests") === "1");
  const [d, setD] = useState<Seq | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let off = false;
    setBusy(true); setErr(null);
    post<Seq>(`/dev/workspaces/${devId}/graph`, { kind: "sequence", name, path, depth, tests })
      .then((r) => { if (off) return; if (r.error) { setErr(r.error); setD(null); } else setD(r); })
      .catch((e) => { if (!off) setErr(e instanceof ApiError ? e.message : String(e)); })
      .finally(() => { if (!off) setBusy(false); });
    return () => { off = true; };
  }, [devId, name, path, depth, tests]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !document.querySelector(".svg-lightbox")) { e.stopPropagation(); onClose(); } };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);
  const copy = async () => {
    if (!d?.mermaid) return;
    try { await navigator.clipboard.writeText("```mermaid\n" + d.mermaid + "\n```"); setCopied(true); window.setTimeout(() => setCopied(false), 1500); } catch { /* no clipboard */ }
  };
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal uml-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={`Sequence of ${name}`} data-testid="sequence-view">
        <div className="modal-head">
          <span><b>Sequence of <span className="mono">{name}</span></b></span>
          {d && <span className="muted small" data-testid="sequence-counts">{d.steps.length} calls</span>}
          {d && d.hidden_tests.length > 0 && !tests && <span className="muted small" title={d.hidden_tests.join(", ")}>{d.hidden_tests.length} test{d.hidden_tests.length === 1 ? "" : "s"} hidden</span>}
          {busy && <span className="muted small uml-loading" data-testid="sequence-loading">⏳ following the calls…</span>}
          <span className="spacer" />
          <label className="small" title="How many levels of calls to follow">Depth{" "}
            <select value={depth} onChange={(e) => { setDepth(Number(e.target.value)); localStorage.setItem("waw.seq.depth", e.target.value); }} data-testid="sequence-depth">
              {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <label className="small"><input type="checkbox" checked={tests} onChange={(e) => { setTests(e.target.checked); localStorage.setItem("waw.graph.tests", e.target.checked ? "1" : "0"); }} /> Tests</label>
          <button className="btn small" onClick={() => void copy()} disabled={!d?.mermaid} title="Copy as a ```mermaid block" data-testid="sequence-copy">{copied ? "Copied" : "Copy Mermaid"}</button>
          <button className="icon-btn" onClick={onClose} aria-label="Close" data-testid="sequence-close">×</button>
        </div>
        <div className="uml-legend muted small">The calls it makes, in the order of its code, followed {depth} level{depth === 1 ? "" : "s"} down. Found by searching the worktree (no compiler):
          calls through pointers, virtual methods and methods of other objects aren't followed. Click the diagram to enlarge it; click a step to open the line of the call.</div>
        <div className="uml-canvas sequence-canvas">
          {err && <div className="error small pad" data-testid="sequence-error">{err}</div>}
          {d?.mermaid && <MarkdownView text={"```mermaid\n" + d.mermaid + "\n```"} />}
          {d && d.steps.length > 0 && (
            <ol className="sequence-steps small" data-testid="sequence-steps">
              {d.steps.map((s, i) => (
                <li key={i} style={{ marginLeft: (s.depth - 1) * 18 }}>
                  <button className="link-btn" onClick={() => onOpen(s.path, s.line)} title={`${s.path}:${s.line}`} data-testid="sequence-step">
                    <span className="mono">{s.from}</span> → <span className="mono">{s.to}()</span></button>
                  {" "}<button className="link-btn muted" onClick={() => onOpen(s.to_path, s.to_line)} title="Open its definition">{s.to_path.split("/").pop()}:{s.to_line}</button>
                </li>
              ))}
            </ol>
          )}
          {d && d.notes.length > 0 && <ul className="graph-notes muted small">{d.notes.map((t, i) => <li key={i}>{t}</li>)}</ul>}
        </div>
      </div>
    </div>
  );
}
