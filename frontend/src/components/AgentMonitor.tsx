import { Splitter, usePane } from "./Split";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, WorkspaceInfo, get, post, put } from "../api";
import { confirmAction } from "./Confirm";
import FolderPicker from "./FolderPicker";
import MarkdownView from "./MarkdownView";
import { writeClipboard } from "../clipboard";
import { COMPOSER_HINT, composerKeyDown } from "./keys";

export type Provider = "opencode" | "codex" | "claude";
interface Watch { provider: Provider; id: string; label?: string }
interface Found { provider: Provider; id: string; title: string | null; directory: string | null; folder: string; folder_name: string; updated_at: string | null; watched: boolean; auto: boolean; excluded: boolean; label?: string | null }
interface AutoCfg { folders: string[] | null; days: number; exclude: string[] }
interface Status { state: "working" | "needs_input" | "error" | "stalled" | "idle" | "missing"; level: "alarm" | "error" | "warning" | "busy" | "ok"; reason: string; last_text: string; updated_at: string | null; messages: number }
export interface MonItem {
  provider: Provider; id: string; label: string | null; title: string | null; directory: string | null; folder: string | null; folder_name: string | null;
  model: string | null; found: boolean; status: Status; app_job: { job_id: string; status: string } | null;
  send: { state: string; started_at: string; rc: number | null; busy_warning: boolean } | null;
}
interface Msg { role: string; text: string; tool?: string; status?: string; time: string | null }

const PROV_LABEL: Record<Provider, string> = { opencode: "OpenCode", codex: "Codex", claude: "Claude Code" };
const keyOf = (x: { provider: string; id: string }) => `${x.provider}:${x.id}`;
const ago = (iso: string | null) => {
  if (!iso) return "";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  return s < 60 ? "just now" : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago` : `${Math.floor(s / 86400)} d ago`;
};
const LEVEL_ORDER = { alarm: 0, error: 1, warning: 2, busy: 3, ok: 4 };
const STATE_LABEL: Record<string, string> = { working: "working", needs_input: "needs you", error: "error", stalled: "stalled", idle: "idle", missing: "not found" };

// ---------------------------------------------------------------- sidebar

/** Sidebar of Agents: folders (the wiki folders and any local folders you add), sessions found in them, and
 * sessions added by id. Checked sessions are monitored on the main page. */
export function AgentsPanel({ workspaces, watched, onWatched, onAutoChanged }: { workspaces: WorkspaceInfo[]; watched: Watch[]; onWatched: (w: Watch[]) => void; onAutoChanged: () => void }) {
  const [extra, setExtra] = useState<{ path: string; name: string; exists: boolean }[]>([]);
  // checked folders: their recent sessions are monitored automatically (server-side, so every browser agrees)
  const [auto, setAuto] = useState<AutoCfg | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [showList, setShowList] = useState(() => localStorage.getItem("waw.mon.list") === "1");
  useEffect(() => { localStorage.setItem("waw.mon.list", showList ? "1" : "0"); }, [showList]);
  useEffect(() => {
    void get<AutoCfg>("/monitor/auto").then(setAuto).catch((e: ApiError) =>
      setErr(`Folder selection is unavailable (${e.message}). The server may be older than this page: restart it.`));
  }, []);
  const saveAuto = async (next: AutoCfg) => {
    setAuto(next);
    try {
      setAuto(await put<AutoCfg>("/monitor/auto", next));
      onAutoChanged();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const sel = auto?.folders ?? null;
  const setSel = (folders: string[]) => {
    if (!auto) return setErr("Folder selection is not loaded yet (or the server does not support it: restart the server).");
    void saveAuto({ ...auto, folders });
  };
  const [found, setFound] = useState<Found[] | null>(null);
  const [providers, setProviders] = useState<Record<string, boolean>>({});
  const [pick, setPick] = useState(false);
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const [manual, setManual] = useState<{ provider: Provider; id: string; label: string }>({ provider: "opencode", id: "", label: "" });
  const [more, setMore] = useState<Record<string, boolean>>({});
  const folders = useMemo(() => [
    ...workspaces.filter((w) => w.available).map((w) => ({ path: w.root, name: w.name, wiki: true })),
    ...extra.filter((f) => !workspaces.some((w) => w.root === f.path)).map((f) => ({ path: f.path, name: f.name, wiki: false })),
  ], [workspaces, extra]);
  const chosen = sel ?? folders.map((f) => f.path);
  useEffect(() => { void get<{ folders: typeof extra }>("/monitor/folders").then((r) => setExtra(r.folders)).catch(() => undefined); }, []);
  const discover = useCallback(async () => {
    if (!chosen.length) { setFound([]); return; }
    setBusy(true);
    try {
      const r = await get<{ sessions: Found[]; providers: Record<string, boolean> }>(`/monitor/discover?folders=${encodeURIComponent(chosen.join("\n"))}`);
      setFound(r.sessions);
      setProviders(r.providers);
      setErr(null);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  }, [chosen.join("\n")]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (auto) void discover(); }, [discover, auto, showList]);
  useEffect(() => {
    const t = setInterval(() => { if (document.visibilityState === "visible") void discover(); }, 30000);
    return () => clearInterval(t);
  }, [discover]);
  const isWatched = (x: { provider: string; id: string }) => watched.some((w) => keyOf(w) === keyOf(x));
  const monitored = (x: Found) => isWatched(x) || x.auto;
  const toggle = (x: Found) => {
    if (isWatched(x)) return onWatched(watched.filter((w) => keyOf(w) !== keyOf(x)));
    if (x.auto && auto) { setFound((f) => f && f.map((y) => (keyOf(y) === keyOf(x) ? { ...y, auto: false, excluded: true } : y))); return void saveAuto({ ...auto, exclude: [...auto.exclude, keyOf(x)] }); }
    if (x.excluded && auto) setFound((f) => f && f.map((y) => (keyOf(y) === keyOf(x) ? { ...y, excluded: false } : y)));
    if (x.excluded && auto) void saveAuto({ ...auto, exclude: auto.exclude.filter((k) => k !== keyOf(x)) });
    onWatched([...watched, { provider: x.provider, id: x.id }]);
  };
  /** Monitor (or stop monitoring) every session of a folder at once. */
  const setAllOf = (list: Found[], on: boolean) => {
    const keys = new Set(list.map(keyOf));
    if (on) {
      const add = list.filter((x) => !monitored(x)).map((x) => ({ provider: x.provider, id: x.id }));
      if (auto) void saveAuto({ ...auto, exclude: auto.exclude.filter((k) => !keys.has(k)) });
      setFound((f) => f && f.map((y) => (keys.has(keyOf(y)) ? { ...y, excluded: false } : y)));
      onWatched([...watched, ...add.filter((a) => !list.find((x) => keyOf(x) === keyOf(a))?.excluded)]);
    } else {
      const autoKeys = list.filter((x) => x.auto).map(keyOf);
      if (auto && autoKeys.length) void saveAuto({ ...auto, exclude: [...new Set([...auto.exclude, ...autoKeys])] });
      setFound((f) => f && f.map((y) => (keys.has(keyOf(y)) && y.auto ? { ...y, auto: false, excluded: true } : y)));
      onWatched(watched.filter((w) => !keys.has(keyOf(w))));
    }
  };
  const addFolder = async (p: string) => {
    try {
      const r = await put<{ folders: typeof extra }>("/monitor/folders", { folders: [...extra.map((f) => f.path), p] });
      setExtra(r.folders);
      setSel([...chosen, p]);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const removeFolder = async (p: string) => {
    const r = await put<{ folders: typeof extra }>("/monitor/folders", { folders: extra.map((f) => f.path).filter((x) => x !== p) });
    setExtra(r.folders);
    setSel(chosen.filter((x) => x !== p));
  };
  const addManual = async () => {
    const id = manual.id.trim();
    if (!id) return;
    setErr(null);
    try {
      const r = await post<{ found: boolean; title: string | null }>("/monitor/watched", { provider: manual.provider, id, label: manual.label.trim() || undefined });
      onWatched([{ provider: manual.provider, id, ...(manual.label.trim() ? { label: manual.label.trim() } : {}) }, ...watched.filter((w) => keyOf(w) !== `${manual.provider}:${id}`)]);
      if (!r.found) setErr(`Added, but no ${PROV_LABEL[manual.provider]} session ${id} was found on this machine (yet).`);
      setManual({ ...manual, id: "", label: "" });
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const q = filter.trim().toLowerCase();
  const byFolder = useMemo(() => {
    const m = new Map<string, Found[]>();
    for (const s of found ?? []) {
      if (q && !`${s.title ?? ""} ${s.id} ${s.provider} ${s.directory ?? ""}`.toLowerCase().includes(q)) continue;
      m.set(s.folder, [...(m.get(s.folder) ?? []), s]);
    }
    return m;
  }, [found, q]);
  return (
    <div className="agents-panel" data-testid="agents-panel">
      <div className="local-head"><b>Agents</b><span className="spacer" />
        <button className="icon-btn small" title="Find sessions again" onClick={() => void discover()} disabled={busy}>↻</button></div>
      <div className="agents-section">
        <div className="label">Folders <span className="spacer" /><button className="link-btn small" onClick={() => setPick(true)} data-testid="mon-add-folder">+ add folder</button></div>
        <div className="muted small">Sessions of the checked folders are monitored when active in the last{" "}
          <select value={auto?.days ?? 7} onChange={(e) => auto && void saveAuto({ ...auto, days: Number(e.target.value) })} data-testid="mon-days">
            {[1, 3, 7, 14, 30, 90].map((d) => <option key={d} value={d}>{d} day{d > 1 ? "s" : ""}</option>)}
            <option value={0}>— (none: pick them below)</option>
          </select>
        </div>
        {err && !showList && <div className="error-text small" data-testid="mon-err">{err}</div>}
        {folders.map((f) => (
          <label key={f.path} className="agents-check small" title={f.path}>
            <input type="checkbox" checked={chosen.includes(f.path)} onChange={(e) => setSel(e.target.checked ? [...chosen, f.path] : chosen.filter((x) => x !== f.path))} data-testid="mon-folder" data-path={f.path} />
            <span className="agents-session-name">{f.name}</span>
            <span className="muted">{f.wiki ? "wiki" : "local"}</span>
            {!f.wiki && <button className="icon-btn small" title="Remove this folder from the list" onClick={(e) => { e.preventDefault(); void removeFolder(f.path); }}>✕</button>}
          </label>
        ))}
      </div>
      <div className="agents-section">
        <div className="label">Add a session by its id</div>
        <div className="row nowrap mon-manual">
          <select value={manual.provider} onChange={(e) => setManual({ ...manual, provider: e.target.value as Provider })} data-testid="mon-manual-provider">
            {(["opencode", "codex", "claude"] as Provider[]).map((p) => <option key={p} value={p}>{PROV_LABEL[p]}{providers[p] === false ? " (not installed)" : ""}</option>)}
          </select>
          <input value={manual.id} onChange={(e) => setManual({ ...manual, id: e.target.value })} placeholder={manual.provider === "opencode" ? "ses_…" : "session UUID"} className="mono"
            onKeyDown={(e) => { if (e.key === "Enter") void addManual(); }} data-testid="mon-manual-id" />
        </div>
        <div className="row nowrap mon-manual">
          <input value={manual.label} onChange={(e) => setManual({ ...manual, label: e.target.value })} placeholder="name to show (optional)" data-testid="mon-manual-label"
            onKeyDown={(e) => { if (e.key === "Enter") void addManual(); }} />
          <button className="btn small" disabled={!manual.id.trim()} onClick={() => void addManual()} data-testid="mon-manual-add">Add</button>
        </div>
      </div>
      <div className={`agents-section agents-sessions ${showList ? "" : "folded"}`}>
        <button className="link-btn small agents-list-toggle" onClick={() => setShowList(!showList)} data-testid="mon-list-toggle" aria-expanded={showList}>
          {showList ? "▾" : "▸"} All sessions in these folders{found ? ` (${found.length})` : ""} — add older ones, remove some
        </button>
        {showList && <>
        <input className="mon-filter" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="filter by title, id, folder" />
        {err && <div className="error-text small">{err}</div>}
        {busy && !found && <div className="muted small">Looking for sessions…</div>}
        {found && byFolder.size === 0 && <div className="muted small">No OpenCode, Codex or Claude Code sessions in these folders.</div>}
        {[...byFolder.entries()].map(([folder, list]) => (
          <div key={folder} className="agents-group">
            <label className="agents-check small agents-group-head" title={`${folder}\nSelect all: monitor every session of this folder`}>
              <input type="checkbox" checked={list.every(monitored)} ref={(el) => { if (el) el.indeterminate = !list.every(monitored) && list.some(monitored); }}
                onChange={(e) => setAllOf(list, e.target.checked)} data-testid="mon-found-folder" />
              <b>{list[0].folder_name}</b> <span className="muted">({list.length})</span>
            </label>
            {(more[folder] ? list : list.slice(0, 30)).map((s) => (
              <label key={keyOf(s)} className="agents-check small agents-session" title={`${s.directory ?? ""}\n${s.id}`}>
                <input type="checkbox" checked={monitored(s)} onChange={() => toggle(s)} data-testid="mon-found" data-key={keyOf(s)} data-auto={s.auto ? "1" : "0"} />
                <span className={`mon-prov ${s.provider}`}>{s.provider === "opencode" ? "OC" : s.provider === "codex" ? "CX" : "CL"}</span>
                <span className="agents-session-name">{s.label || s.title || s.id}</span>
                <span className="muted">{ago(s.updated_at)}</span>
              </label>
            ))}
            {list.length > 30 && !more[folder] && <button className="link-btn small" onClick={() => setMore({ ...more, [folder]: true })}>{list.length - 30} more…</button>}
          </div>
        ))}
        </>}
      </div>
      {pick && <FolderPicker title="Add a folder" hint="Its OpenCode, Codex and Claude Code sessions can be monitored." confirmLabel="Add this folder"
        onClose={() => setPick(false)} onChoose={(p) => { setPick(false); void addFolder(p); }} />}
    </div>
  );
}

// ---------------------------------------------------------------- main page

/**
 * The monitored sessions at a glance — those that need you first (they are flagged and counted in the tab title and
 * on the Agents icon). Click one to put it on stage: 80 % of the page for its conversation, where you can continue
 * it; the other sessions stay in a narrow column.
 */
export default function AgentMonitor({ watched, onWatched, visible, onAlarms, autoVersion = 0 }: { watched: Watch[]; onWatched: (w: Watch[]) => void; visible: boolean; onAlarms: (n: number) => void; autoVersion?: number }) {
  const [items, setItems] = useState<MonItem[] | null>(null);
  const [checked, setChecked] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [stage, setStage] = useState<string | null>(() => localStorage.getItem("waw.mon.stage"));
  const listPane = usePane("mon.list", 220, 140, 900);
  const [notify, setNotify] = useState(() => localStorage.getItem("waw.mon.notify") === "1");
  const alarmed = useRef<Set<string>>(new Set());
  useEffect(() => { if (stage) localStorage.setItem("waw.mon.stage", stage); else localStorage.removeItem("waw.mon.stage"); }, [stage]);
  useEffect(() => { localStorage.setItem("waw.mon.notify", notify ? "1" : "0"); }, [notify]);
  const wkey = watched.map(keyOf).join(",");
  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      const r = await get<{ items: MonItem[]; alarms: number; checked_at: string }>("/monitor/status");
      setItems(r.items);
      setChecked(r.checked_at);
      onAlarms(r.alarms);
      setErr(null);
      // a notification when a session starts needing you
      const now = new Set(r.items.filter((i) => i.status.level === "alarm").map(keyOf));
      if (notify && "Notification" in window && Notification.permission === "granted") {
        for (const i of r.items) if (now.has(keyOf(i)) && !alarmed.current.has(keyOf(i))) new Notification(`${i.label ?? i.title ?? i.id} needs you`, { body: i.status.reason });
      }
      alarmed.current = now;
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  }, [wkey, onAlarms, notify, autoVersion]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    const t = setInterval(() => { if (document.visibilityState === "visible") void refresh(); }, visible ? 8000 : 30000);
    return () => clearInterval(t);
  }, [visible, refresh]);
  // selection (for Delete selected), grouped by folder
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const pick = (list: MonItem[], on: boolean) => setPicked((p) => { const n = new Set(p); for (const i of list) on ? n.add(keyOf(i)) : n.delete(keyOf(i)); return n; });
  const renameItem = async (i: MonItem) => {
    const name = window.prompt(`Name to show for this ${PROV_LABEL[i.provider]} session (empty: its own title):`, i.label ?? i.title ?? "");
    if (name === null) return;
    try {
      await put(`/monitor/session/${i.provider}/${encodeURIComponent(i.id)}/label`, { label: name });
      await refresh();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const removeItems = async (list: MonItem[]) => {
    if (!list.length) return;
    const names = list.slice(0, 8).map((i) => `• ${i.label ?? i.title ?? i.id} (${PROV_LABEL[i.provider]})`).join("\n");
    const ok = await confirmAction({
      title: list.length === 1 ? "Delete this session?" : `Delete ${list.length} sessions?`,
      message: `${names}${list.length > 8 ? `\n… and ${list.length - 8} more` : ""}\n\nThey are deleted permanently from OpenCode / Codex / Claude Code (their conversation history is gone) — not only from this monitor.`,
      confirmLabel: list.length === 1 ? "Delete session" : `Delete ${list.length} sessions`,
    });
    if (!ok) return;
    try {
      const r = await post<{ deleted: string[]; failed: { id: string; error: string }[] }>("/monitor/delete", { sessions: list.map((i) => ({ provider: i.provider, id: i.id })) });
      const gone = new Set(r.deleted);
      onWatched(watched.filter((w) => !gone.has(keyOf(w))));
      setPicked((p) => new Set([...p].filter((k) => !gone.has(k))));
      if (stage && gone.has(stage)) setStage(null);
      if (r.failed.length) setErr(`Not deleted: ${r.failed.map((f) => `${f.id} (${f.error})`).join("; ")}`);
      await refresh();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const sorted = (items ?? []).slice().sort((a, b) => LEVEL_ORDER[a.status.level] - LEVEL_ORDER[b.status.level] || (b.status.updated_at ?? "").localeCompare(a.status.updated_at ?? ""));
  const groups = useMemo(() => {
    const m = new Map<string, MonItem[]>();
    for (const i of sorted) { const f = i.folder_name ?? i.directory ?? "other"; m.set(f, [...(m.get(f) ?? []), i]); }
    return [...m.entries()];
  }, [sorted]);
  const pickedItems = sorted.filter((i) => picked.has(keyOf(i)));
  const staged = sorted.find((i) => keyOf(i) === stage) ?? null;
  const count = (l: string) => (items ?? []).filter((i) => i.status.level === l).length;
  const unwatch = async (i: MonItem) => {
    if ((i as MonItem & { auto?: boolean }).auto) {
      const cfg = await get<AutoCfg>("/monitor/auto");
      await put("/monitor/auto", { ...cfg, exclude: [...cfg.exclude, keyOf(i)] });
    } else onWatched(watched.filter((w) => keyOf(w) !== keyOf(i)));
    if (stage === keyOf(i)) setStage(null);
    void refresh();
  };
  return (
    <div className={`agent-monitor ${staged ? "staged" : ""}`} data-testid="agent-monitor">
      <div className="dash-bar">
        <h2>Agents</h2>
        {count("alarm") > 0 && <span className="chip dash-alarm" data-testid="mon-alarms">{count("alarm")} need you</span>}
        <span className="chip dash-error">{count("error")} errors</span>
        <span className="chip">{count("busy")} working</span>
        <span className="chip dash-ok">{count("ok")} idle</span>
        <span className="spacer" />
        {checked && <span className="muted small">checked {ago(checked)}</span>}
        <label className="small" title="A browser notification when a session starts needing you">
          <input type="checkbox" checked={notify} onChange={(e) => { setNotify(e.target.checked); if (e.target.checked && "Notification" in window && Notification.permission === "default") void Notification.requestPermission(); }} /> notify me
        </label>
        {pickedItems.length > 0 && <>
          <span className="small">{pickedItems.length} selected</span>
          <button className="btn small danger" onClick={() => void removeItems(pickedItems)} data-testid="mon-delete-selected">Delete selected</button>
          <button className="link-btn small" onClick={() => setPicked(new Set())}>clear</button>
        </>}
        <button className="btn small primary" disabled={busy} onClick={() => void refresh()} data-testid="mon-refresh">{busy ? "Checking…" : "Refresh all"}</button>
      </div>
      {err && <div className="banner warn small" role="alert">{err}</div>}
      {items && items.length === 0 && <div className="muted dev-hint" data-testid="mon-empty">No session to monitor: check folders in the sidebar (their recent sessions are monitored), widen the time window, or add a session by its id.</div>}
      <div className="mon-body">
        {(!staged || !listPane.folded) && <div className={staged ? "mon-list" : "mon-groups"} style={staged ? { width: listPane.size } : undefined}>
          {groups.map(([folder, list]) => {
            const all = list.every((i) => picked.has(keyOf(i)));
            return (
              <div key={folder} className="mon-group" data-testid="mon-group">
                <label className="mon-group-head small">
                  <input type="checkbox" checked={all} ref={(el) => { if (el) el.indeterminate = !all && list.some((i) => picked.has(keyOf(i))); }}
                    onChange={() => pick(list, !all)} title="Select all sessions of this folder" data-testid="mon-select-folder" />
                  <b>{folder}</b> <span className="muted">({list.length})</span>
                </label>
                <div className={staged ? "mon-group-list" : "dash-grid"}>
                  {list.map((i) => (
                    <div key={keyOf(i)} className="mon-card-wrap">
                      <input type="checkbox" className="mon-pick" checked={picked.has(keyOf(i))} onChange={() => pick([i], !picked.has(keyOf(i)))} aria-label="Select" data-testid="mon-pick" />
                      <MonCard item={i} compact={!!staged} active={keyOf(i) === stage} onOpen={() => setStage(keyOf(i) === stage ? null : keyOf(i))} />
                      <span className="mon-card-actions">
                        <button className="icon-btn small" title="Rename (the name shown here)" onClick={() => void renameItem(i)} data-testid="mon-rename">✎</button>
                        <button className="icon-btn small" title="Delete this session" onClick={() => void removeItems([i])} data-testid="mon-delete">🗑</button>
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>}
        {staged && <Splitter dir="x" side="before" pane={listPane} label="session list" testId="mon-list-splitter" />}
        {staged && <SessionStage key={keyOf(staged)} item={staged} onClose={() => setStage(null)} onUnwatch={() => void unwatch(staged)} onChanged={() => void refresh()}
          onRename={() => void renameItem(staged)} onDelete={() => void removeItems([staged])} />}
      </div>
    </div>
  );
}

function MonCard({ item: i, compact, active, onOpen }: { item: MonItem; compact: boolean; active: boolean; onOpen: () => void }) {
  const name = i.label ?? i.title ?? i.id;
  return (
    <button className={`dash-card mon-card ${i.status.level} ${compact ? "compact" : ""} ${active ? "active" : ""}`} onClick={onOpen}
      data-testid="mon-card" data-key={keyOf(i)} data-state={i.status.state} data-level={i.status.level} title={`${PROV_LABEL[i.provider]} · ${i.id}\n${i.directory ?? ""}`}>
      <div className="dash-card-head">
        <span className={`dash-dot ${i.status.level}`} />
        <b className="dash-name">{name}</b>
        <span className={`chip mon-state ${i.status.state}`} data-testid="mon-state">{STATE_LABEL[i.status.state] ?? i.status.state}</span>
      </div>
      <div className="muted small mon-meta"><span className={`mon-prov ${i.provider}`}>{PROV_LABEL[i.provider]}</span> {i.folder_name ?? i.directory ?? ""} · {ago(i.status.updated_at)}</div>
      {!compact && <div className={`small mon-reason ${i.status.level}`}>{i.status.reason}</div>}
      {!compact && i.status.last_text && <div className="muted small mon-last">{i.status.last_text.slice(-240)}</div>}
    </button>
  );
}

/** One session on stage: its conversation, live while it runs, and a composer to continue it. */
function SessionStage({ item, onClose, onUnwatch, onChanged, onRename, onDelete }: { item: MonItem; onClose: () => void; onUnwatch: () => void; onChanged: () => void; onRename: () => void; onDelete: () => void }) {
  const [detail, setDetail] = useState<{ messages: Msg[]; total_messages: number; send_output: string | null } & MonItem | null>(null);
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [tools, setTools] = useState(() => localStorage.getItem("waw.mon.tools") !== "0");
  const [limit, setLimit] = useState(200);
  const bottom = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const base = `/monitor/session/${item.provider}/${encodeURIComponent(item.id)}`;
  const load = useCallback(async () => {
    try {
      setDetail(await get(`${base}?limit=${limit}`));
      setErr(null);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [base, limit]);
  useEffect(() => { void load(); }, [load]);
  const live = detail?.send?.state === "running" || detail?.status.state === "working";
  useEffect(() => {
    const t = setInterval(() => { if (document.visibilityState === "visible") void load(); }, live ? 2500 : 6000);
    return () => clearInterval(t);
  }, [load, live]);
  useEffect(() => { localStorage.setItem("waw.mon.tools", tools ? "1" : "0"); }, [tools]);
  useEffect(() => { if (stick.current) bottom.current?.scrollIntoView({ block: "end" }); }, [detail?.total_messages, detail?.send_output?.length]);
  const send = async () => {
    const t = text.trim();
    if (!t) return;
    setErr(null);
    setNote(null);
    try {
      const r = await post<{ busy_warning: boolean }>(`${base}/send`, { text: t });
      setText("");
      if (r.busy_warning) setNote("The session looked busy (maybe open in a terminal): your message was sent anyway.");
      stick.current = true;
      await load();
      onChanged();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const terminal = async () => {
    try {
      await post(`${base}/terminal`);
      setNote("A terminal tab with this session is open in the Terminal view.");
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const d = detail ?? { ...item, messages: [], total_messages: 0, send_output: null };
  const msgs = tools ? d.messages : d.messages.filter((m) => m.role !== "tool");
  return (
    <section className="mon-stage" data-testid="mon-stage">
      <header className="mon-stage-head">
        <span className={`dash-dot ${d.status.level}`} />
        <b className="dash-name">{d.label ?? d.title ?? d.id}</b>
        <span className={`chip mon-state ${d.status.state}`}>{STATE_LABEL[d.status.state] ?? d.status.state}</span>
        <span className="spacer" />
        <label className="small"><input type="checkbox" checked={tools} onChange={(e) => setTools(e.target.checked)} /> tool calls</label>
        <button className="btn small" onClick={() => void terminal()} title="Open the provider's own interactive UI on this session in a terminal tab (answer permission prompts there)" data-testid="mon-terminal">Open in terminal</button>
        <button className="btn small" onClick={() => void load()}>↻</button>
        <button className="btn small" onClick={onRename} data-testid="mon-stage-rename">Rename</button>
        <button className="btn small" onClick={onUnwatch} title="Stop monitoring this session (nothing is deleted)">Unwatch</button>
        <button className="btn small danger" onClick={onDelete} title="Delete the session from its provider" data-testid="mon-stage-delete">Delete</button>
        <button className="icon-btn" onClick={onClose} aria-label="Close" data-testid="mon-stage-close">×</button>
      </header>
      <div className="muted small mon-stage-meta">
        <span className={`mon-prov ${d.provider}`}>{PROV_LABEL[d.provider]}</span>{" "}
        <button className="link-btn small mono" onClick={() => void writeClipboard(d.id)} title="Copy the session id">{d.id}</button>
        {d.directory && <> · <span className="mono">{d.directory}</span></>}{d.model && <> · {d.model}</>} · {d.total_messages} messages
      </div>
      <div className={`banner small mon-reason-banner ${d.status.level}`} data-testid="mon-stage-reason">{d.status.reason}</div>
      {err && <div className="banner warn small" role="alert">{err}</div>}
      <div className="mon-transcript" data-testid="mon-transcript" onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; }}>
        {d.total_messages > d.messages.length && <button className="link-btn small" onClick={() => setLimit(400)}>show earlier messages</button>}
        {msgs.map((m, idx) => (
          m.role === "tool" ? (
            <div key={idx} className="mon-tool mono small"><span className={`chip status ${m.status === "error" ? "failed" : ""}`}>{m.tool}</span> {m.text}{m.status && m.status !== "completed" && m.status !== "called" ? ` · ${m.status}` : ""}</div>
          ) : (
            <div key={idx} className={`msg ${m.role === "user" ? "user" : "agent"}`} data-testid="mon-msg" data-role={m.role}>
              <div className="msg-head"><span className="who">{m.role === "user" ? "You" : "Agent"}</span><span className="muted small">{m.time ? new Date(m.time).toLocaleString() : ""}</span></div>
              {m.role === "user" ? <div className="msg-body">{m.text}</div> : <MarkdownView text={m.text} className="msg-body" />}
            </div>
          )
        ))}
        {d.send?.state === "running" && (
          <div className="msg agent running"><div className="msg-head"><span className="who">Agent</span><span className="chip status running">answering…</span></div>
            {d.send_output && <pre className="mon-send-out">{d.send_output.slice(-3000)}</pre>}</div>
        )}
        {d.send?.state === "failed" && <div className="banner warn small">Your last message failed (exit {d.send.rc}). {d.send_output?.slice(-500)}</div>}
        <div ref={bottom} />
      </div>
      <div className="mon-composer">
        {note && <div className="muted small">{note}</div>}
        <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder={`Message ${PROV_LABEL[d.provider]} in this session (${COMPOSER_HINT})`}
          onKeyDown={(e) => composerKeyDown(e, () => void send(), setText)} disabled={!d.found} data-testid="mon-input" />
        <div className="row">
          <span className="muted small">Runs in {d.directory ?? "~"} with the provider's permission rules; for interactive prompts use <b>Open in terminal</b>.</span>
          <span className="spacer" />
          <button className="btn small primary" disabled={!text.trim() || d.send?.state === "running" || !d.found} onClick={() => void send()} data-testid="mon-send">Send</button>
        </div>
      </div>
    </section>
  );
}
