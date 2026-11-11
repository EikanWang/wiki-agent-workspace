import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, WorkspaceInfo, get, post } from "../api";

export interface DashSession { id: string; provider: string; display_name: string | null; short_id: string | null; workspace_id: string; workspace_name: string; origin: string; last_used: string | null }
interface Item {
  session: { id: string; provider: string; display_name: string | null; short_id: string | null; model: string | null; variant: string | null; status: string; origin: string; workspace_id: string; workspace_name: string };
  active: { job_id: string; kind: string; status: string; seconds: number; idle_seconds: number | null; alive: boolean; waiting: number } | null;
  last: { status: string; finished_at: string | null; error_code: string | null; error_message: string | null; seconds: number | null } | null;
  recent: { total: number; failed: number; history: string[] };
  about: { kind: string; path: string; document_id: string; workspace_id: string } | null;
  last_activity: string | null;
  health: { level: "ok" | "warning" | "error" | "unknown"; state: string; problems: string[]; confirmed: boolean | null };
}
export interface DashOpen { kind: string; path: string; workspace_id: string }

const dur = (s: number | null | undefined) => (s == null ? "" : s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`);
const ago = (iso: string | null) => {
  if (!iso) return "";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  return s < 60 ? "just now" : `${dur(s)} ago`;
};

/** Sidebar of the Agents dashboard: pick the folders, then the sessions to monitor. */
export function AgentsPanel({ workspaces, selected, onSelected }: { workspaces: WorkspaceInfo[]; selected: string[]; onSelected: (ids: string[]) => void }) {
  const [sessions, setSessions] = useState<DashSession[] | null>(null);
  const [folders, setFolders] = useState<string[]>(() => JSON.parse(localStorage.getItem("waw.dash.folders") || "null") ?? []);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => {
    get<{ sessions: DashSession[] }>("/agent-dashboard/sessions").then((r) => setSessions(r.sessions)).catch((e) => setErr((e as ApiError).message));
  }, []);
  useEffect(load, [load]);
  const open = workspaces.filter((w) => w.available);
  const chosenFolders = folders.length ? folders : open.map((w) => w.id); // none chosen yet: all open folders
  useEffect(() => { localStorage.setItem("waw.dash.folders", JSON.stringify(folders)); }, [folders]);
  const sel = new Set(selected);
  const byFolder = useMemo(() => {
    const m = new Map<string, DashSession[]>();
    for (const s of sessions ?? []) if (chosenFolders.includes(s.workspace_id)) m.set(s.workspace_id, [...(m.get(s.workspace_id) ?? []), s]);
    return m;
  }, [sessions, chosenFolders]);
  const toggle = (id: string) => onSelected(sel.has(id) ? selected.filter((x) => x !== id) : [...selected, id]);
  const setMany = (ids: string[], on: boolean) => onSelected(on ? [...new Set([...selected, ...ids])] : selected.filter((x) => !ids.includes(x)));
  return (
    <div className="agents-panel" data-testid="agents-panel">
      <div className="local-head"><b>Agents</b><span className="spacer" /><button className="icon-btn small" title="Reload the session list" onClick={load}>↻</button></div>
      <div className="agents-section">
        <div className="label">Folders</div>
        {open.map((w) => (
          <label key={w.id} className="agents-check small">
            <input type="checkbox" checked={chosenFolders.includes(w.id)} data-testid="agents-folder"
              onChange={(e) => setFolders((f) => { const base = f.length ? f : open.map((x) => x.id); return e.target.checked ? [...new Set([...base, w.id])] : base.filter((x) => x !== w.id); })} />
            {w.name}
          </label>
        ))}
      </div>
      <div className="agents-section agents-sessions">
        <div className="label">Sessions to monitor <span className="spacer" />
          <button className="link-btn small" onClick={() => setMany([...byFolder.values()].flat().map((s) => s.id), true)} data-testid="agents-select-all">all</button>
          <button className="link-btn small" onClick={() => onSelected([])}>none</button>
        </div>
        {err && <div className="error-text small">{err}</div>}
        {sessions && byFolder.size === 0 && <div className="muted small">No agent sessions in these folders yet.</div>}
        {[...byFolder.entries()].map(([ws, list]) => (
          <div key={ws} className="agents-group">
            <label className="agents-check small agents-group-head">
              <input type="checkbox" checked={list.every((s) => sel.has(s.id))} onChange={(e) => setMany(list.map((s) => s.id), e.target.checked)} />
              <b>{list[0].workspace_name}</b> <span className="muted">({list.length})</span>
            </label>
            {list.map((s) => (
              <label key={s.id} className="agents-check small agents-session" title={`${s.provider} · ${s.short_id ?? "no native id yet"} · ${s.origin}`}>
                <input type="checkbox" checked={sel.has(s.id)} onChange={() => toggle(s.id)} data-testid="agents-session" data-id={s.id} />
                <span className="agents-session-name">{s.display_name ?? s.short_id ?? s.id.slice(0, 8)}</span>
                <span className="muted">{s.provider}{s.last_used ? ` · ${ago(s.last_used)}` : ""}</span>
              </label>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

/** The Agents dashboard: current status and health of the monitored sessions; refresh on demand or periodically. */
export default function AgentDashboard({ selected, visible, onOpen }: { selected: string[]; visible: boolean; onOpen: (o: DashOpen) => void }) {
  const [items, setItems] = useState<Item[] | null>(null);
  const [providers, setProviders] = useState<Record<string, { available: boolean; version: string | null; error: string | null }>>({});
  const [checked, setChecked] = useState<{ at: string; deep: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [auto, setAuto] = useState(() => localStorage.getItem("waw.dash.auto") !== "0");
  const [filter, setFilter] = useState<"all" | "problems" | "active">("all");
  useEffect(() => { localStorage.setItem("waw.dash.auto", auto ? "1" : "0"); }, [auto]);
  const key = selected.join(",");
  const refresh = useCallback(async (deep: boolean) => {
    if (!key) { setItems([]); return; }
    setBusy(true);
    try {
      const r = await get<{ items: Item[]; checked_at: string; deep: boolean; providers: typeof providers }>(`/agent-dashboard?ids=${encodeURIComponent(key)}&deep=${deep}`);
      setItems(r.items);
      setProviders(r.providers);
      setChecked({ at: r.checked_at, deep: r.deep });
      setErr(null);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  }, [key]);
  useEffect(() => { if (visible) void refresh(true); }, [visible, refresh]);
  useEffect(() => {
    if (!visible || !auto) return;
    const t = setInterval(() => { if (document.visibilityState === "visible") void refresh(false); }, 10000);
    return () => clearInterval(t);
  }, [visible, auto, refresh]);
  const order = { error: 0, warning: 1, unknown: 2, ok: 3 };
  const shown = (items ?? []).filter((it) => filter === "all" || (filter === "problems" ? it.health.level !== "ok" : !!it.active))
    .sort((a, b) => order[a.health.level] - order[b.health.level] || (b.last_activity ?? "").localeCompare(a.last_activity ?? ""));
  const count = (l: string) => (items ?? []).filter((it) => it.health.level === l).length;
  const running = (items ?? []).filter((it) => it.active).length;
  return (
    <div className="agent-dashboard" data-testid="agent-dashboard">
      <div className="dash-bar">
        <h2>Agent dashboard</h2>
        <span className="chip dash-ok" data-testid="dash-count-ok">{count("ok")} healthy</span>
        <span className="chip dash-warning" data-testid="dash-count-warning">{count("warning")} warnings</span>
        <span className="chip dash-error" data-testid="dash-count-error">{count("error")} errors</span>
        <span className="chip">{running} active</span>
        <span className="spacer" />
        <span className="mode-switch" role="tablist">
          {(["all", "problems", "active"] as const).map((f) => <button key={f} role="tab" className={filter === f ? "active" : ""} onClick={() => setFilter(f)}>{f === "all" ? "All" : f === "problems" ? "Problems" : "Active"}</button>)}
        </span>
        <label className="small"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto-refresh (10 s)</label>
        <button className="btn small primary" disabled={busy} onClick={() => void refresh(true)} title="Refresh every session's status and check with the providers that the sessions still exist" data-testid="dash-refresh">{busy ? "Checking…" : "Refresh all"}</button>
      </div>
      <div className="dash-providers small">
        {Object.entries(providers).map(([n, p]) => (
          <span key={n} className={`chip ${p.available ? "dash-ok" : "dash-error"}`} title={p.error ?? p.version ?? ""}>{n} {p.available ? `✓ ${p.version ?? ""}` : "unavailable"}</span>
        ))}
        {checked && <span className="muted">checked {ago(checked.at)}{checked.deep ? " (with the providers)" : ""}</span>}
      </div>
      {err && <div className="banner warn small" role="alert">{err}</div>}
      {selected.length === 0 && <div className="muted dev-hint" data-testid="dash-empty">Choose the folders and the sessions to monitor in the sidebar.</div>}
      <div className="dash-grid">
        {shown.map((it) => {
          const s = it.session;
          return (
            <div key={s.id} className={`dash-card ${it.health.level}`} data-testid="dash-card" data-level={it.health.level} data-state={it.health.state} data-id={s.id}>
              <div className="dash-card-head">
                <span className={`dash-dot ${it.health.level}`} title={it.health.level} />
                <b className="dash-name" title={s.id}>{s.display_name ?? s.short_id ?? s.id.slice(0, 8)}</b>
                <span className={`chip dash-state ${it.health.state}`} data-testid="dash-state">{it.health.state}</span>
              </div>
              <div className="muted small">{s.workspace_name} · {s.provider}{s.short_id ? ` #${s.short_id}` : ""}{s.model ? ` · ${s.model.split("/").pop()}` : ""}{s.variant ? ` (${s.variant})` : ""}</div>
              {it.active && (
                <div className="small dash-active">
                  {it.active.status === "running" ? "Running" : it.active.status === "queued" ? "Queued" : "Waiting for permission"} {dur(it.active.seconds)}
                  {it.active.idle_seconds != null && it.active.status === "running" ? ` · last output ${dur(it.active.idle_seconds)} ago` : ""}
                  {it.active.waiting > 0 ? ` · ${it.active.waiting} more queued` : ""}
                  <button className="link-btn small" onClick={() => void post(`/jobs/${it.active!.job_id}/cancel`).then(() => refresh(false)).catch((e) => setErr((e as ApiError).message))} data-testid="dash-cancel"> cancel</button>
                </div>
              )}
              {it.last && (
                <div className="small">Last turn: <span className={`chip status ${it.last.status === "completed" ? "" : it.last.status}`}>{it.last.status}</span> {ago(it.last.finished_at)}{it.last.seconds != null ? ` · took ${dur(it.last.seconds)}` : ""}</div>
              )}
              {!it.last && !it.active && <div className="small muted">No turns yet.</div>}
              {it.recent.history.length > 0 && (
                <div className="dash-history" title="Last turns, newest first">
                  {it.recent.history.map((h, i) => <span key={i} className={`dash-h ${h}`} title={h} />)}
                </div>
              )}
              {it.health.problems.length > 0 && <ul className="dash-problems small" data-testid="dash-problems">{it.health.problems.map((p, i) => <li key={i}>{p}</li>)}</ul>}
              {it.last?.error_message && it.health.level !== "ok" && <div className="muted small dash-error-msg" title={it.last.error_message}>{it.last.error_message}</div>}
              <div className="row dash-actions">
                {it.about && <button className="btn small" onClick={() => onOpen(it.about!)} data-testid="dash-open">Open {it.about.kind === "document" ? it.about.path.split("/").pop() : it.about.kind === "pr" ? "PR" : it.about.kind === "dev" ? "workspace" : "chat"}</button>}
                <span className="muted small">{it.last_activity ? `active ${ago(it.last_activity)}` : ""}</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
