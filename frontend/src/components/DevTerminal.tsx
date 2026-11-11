import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, DevWorkspace, TerminalInfo, del, get, post } from "../api";
import { writeClipboard } from "../clipboard";
import { ConnState, StickyMods, TerminalView } from "./TerminalPanel";

/**
 * Terminals of a development workspace, below the editor: a shell in the worktree (an ssh login on the workspace's
 * host that starts there), for builds and anything else. They are tmux sessions like the Terminal view's tabs — they
 * keep running when the page reloads or the browser closes, and are listed in the Terminal view too.
 */
export default function DevTerminal({ dev, visible }: { dev: DevWorkspace; visible: boolean }) {
  const [terms, setTerms] = useState<TerminalInfo[] | null>(null);
  const [active, setActive] = useState<string | null>(null);
  const [states, setStates] = useState<Record<string, ConnState>>({});
  const [err, setErr] = useState<string | null>(null);
  const modsRef = useRef<StickyMods>({ ctrl: false, alt: false });
  const started = useRef(false);
  const load = useCallback(async () => {
    try {
      const r = await get<{ terminals: TerminalInfo[] }>(`/dev/workspaces/${dev.id}/terminals`);
      setTerms(r.terminals);
      setActive((a) => (a && r.terminals.some((t) => t.id === a) ? a : r.terminals[0]?.id ?? null));
      return r.terminals;
    } catch (e) {
      setErr((e as ApiError).message);
      return [];
    }
  }, [dev.id]);
  const create = useCallback(async () => {
    setErr(null);
    try {
      const t = await post<TerminalInfo>(`/dev/workspaces/${dev.id}/terminals`);
      setTerms((ts) => [...(ts ?? []), t]);
      setActive(t.id);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [dev.id]);
  // the first time the tab is shown: open the workspace's terminal (create one if there is none)
  useEffect(() => {
    if (!visible || started.current) return;
    started.current = true;
    void load().then((ts) => { if (ts.length === 0) void create(); });
  }, [visible, load, create]);
  const close = async (t: TerminalInfo) => {
    if (t.state === "live" && !window.confirm(`Close "${t.label}"? Programs running in it are stopped.`)) return;
    await del(`/terminals/${t.id}`).catch((e) => setErr((e as ApiError).message));
    void load();
  };
  return (
    <div className="dev-terminal" data-testid="dev-terminal">
      <div className="dev-terminal-tabs">
        {(terms ?? []).map((t) => (
          <span key={t.id} className={`dev-terminal-tab ${t.id === active ? "active" : ""} ${t.state}`} onClick={() => setActive(t.id)} title={`${t.cwd} · tmux ${t.tmux_session}`} data-testid="dev-terminal-tab">
            {t.label} <span className="muted small">{states[t.id] && states[t.id] !== "connected" ? states[t.id] : ""}</span>
            <button className="icon-btn small" onClick={(e) => { e.stopPropagation(); void close(t); }} aria-label={`Close ${t.label}`}>×</button>
          </span>
        ))}
        <button className="icon-btn small" onClick={() => void create()} title={`New terminal in the worktree${dev.host !== "local" ? ` on ${dev.host}` : ""}`} data-testid="dev-terminal-new">+</button>
        <span className="spacer" />
        <span className="muted small mono" title="The shell starts in the worktree">{dev.host !== "local" ? `${dev.host}:` : ""}{dev.worktree_path}</span>
      </div>
      {err && <div className="error-text small">{err}</div>}
      <div className="dev-terminal-body">
        {(terms ?? []).map((t) => (
          <div key={t.id} className="dev-terminal-view" style={{ display: t.id === active ? undefined : "none" }}>
            <TerminalView terminal={t} active={visible && t.id === active} fontSize={13} modsRef={modsRef}
              onState={(s) => setStates((m) => ({ ...m, [t.id]: s }))} onExited={() => void load()} onDismiss={() => void close(t)}
              onModsUsed={() => { modsRef.current = { ctrl: false, alt: false }; }} onCopy={(text) => void writeClipboard(text)} />
          </div>
        ))}
        {terms?.length === 0 && <div className="muted small dev-hint">No terminal. <button className="btn small" onClick={() => void create()}>Open one in the worktree</button></div>}
      </div>
    </div>
  );
}
