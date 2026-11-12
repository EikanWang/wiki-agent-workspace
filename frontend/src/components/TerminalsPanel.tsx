import { useState } from "react";
import { ApiError, TerminalInfo, del, post } from "../api";
import FolderPicker from "./FolderPicker";
import { PlusIcon } from "./Icons";
import { confirmAction } from "./Confirm";

/**
 * Sidebar of the Terminal activity: every terminal of the server (the same tmux-backed terminals as the wiki's
 * Terminal view), a new one in the home folder or in a folder you pick, and closing them.
 */
export default function TerminalsPanel({ terminals, activeId, onActive, onChanged, available }: {
  terminals: TerminalInfo[];
  activeId: string | null;
  onActive: (id: string) => void;
  onChanged: () => Promise<void> | void;
  available: boolean;
}) {
  const [pick, setPick] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const create = async (cwd: string) => {
    setErr(null);
    try {
      const t = await post<TerminalInfo>("/terminals", { cwd });
      await onChanged();
      onActive(t.id);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const close = async (t: TerminalInfo) => {
    if (t.state === "live" && !(await confirmAction({ title: `Close ${t.label}?`, message: "Its shell and everything running in it stop.", confirmLabel: "Close terminal", danger: true }))) return;
    try {
      await del(`/terminals/${t.id}`);
      await onChanged();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const home = (p: string) => p.replace(/^\/home\/[^/]+|^\/root/, "~");
  return (
    <div className="terminals-panel" data-testid="terminals-panel">
      <div className="local-head">
        <b>Terminals</b>
        <span className="spacer" />
        <button className="icon-btn" disabled={!available} onClick={() => void create("~")} title="New terminal in the home folder" aria-label="New terminal" data-testid="terminals-new"><PlusIcon /></button>
      </div>
      <div className="row pad-x">
        <button className="link-btn small" disabled={!available} onClick={() => setPick(true)} data-testid="terminals-new-in">+ New terminal in a folder…</button>
      </div>
      {!available && <div className="warn small pad-x">tmux is not installed on the server; terminals are disabled.</div>}
      {err && <div className="error-text small pad-x" role="alert">{err}</div>}
      <div className="terminals-list">
        {terminals.map((t) => (
          <div key={t.id} className={`list-item terminals-item ${t.id === activeId ? "selected" : ""} ${t.state}`} onClick={() => onActive(t.id)} title={`${t.cwd} · tmux ${t.tmux_session}`}
            data-testid="terminals-item" data-state={t.state} role="button">
            <span className={`term-dot ${t.state}`} aria-hidden="true" />
            <span className="terminals-text">
              <span className="terminals-label">{t.label}{t.state !== "live" ? ` (${t.state})` : ""}</span>
              <span className="muted small mono terminals-cwd">{home(t.cwd)}</span>
            </span>
            <button className="icon-btn small" onClick={(e) => { e.stopPropagation(); void close(t); }} title={t.state === "live" ? "Close this terminal" : "Dismiss"} aria-label="Close" data-testid="terminals-close">×</button>
          </div>
        ))}
        {terminals.length === 0 && <div className="muted small pad-x">No terminals yet: + opens one in your home folder.</div>}
      </div>
      {pick && <FolderPicker title="New terminal in…" hint="The shell starts in this folder." confirmLabel="Open a terminal here"
        onClose={() => setPick(false)} onChoose={(p) => { setPick(false); void create(p); }} />}
    </div>
  );
}
