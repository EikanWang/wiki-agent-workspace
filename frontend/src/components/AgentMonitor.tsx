import { FoldStrip, Splitter, usePane } from "./Split";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgentSession, ApiError, MachineInfo, WorkspaceInfo, get, post, put } from "../api";
import ModelChoice from "./ModelChoice";
import { PASTE_HINT, PictureStrip, picturesOnly, usePictures } from "./Pictures";
import { MAX_CUSTOM_BYTES, SOUNDS, customSound, customSoundName, playSound, saveCustomSound, saveSound, saveVolume, savedSound, savedVolume } from "../sounds";
import { confirmAction } from "./Confirm";
import FolderPicker from "./FolderPicker";
import MarkdownView from "./MarkdownView";
import { writeClipboard } from "../clipboard";
import { utc8 } from "../time";
import { COMPOSER_HINT, composerKeyDown } from "./keys";

export type Provider = "opencode" | "codex" | "claude";
export interface Watch { provider: Provider; id: string; label?: string; machine?: string }
interface Found { provider: Provider; id: string; machine?: string | null; title: string | null; directory: string | null; folder: string; folder_name: string; updated_at: string | null; watched: boolean; auto: boolean; excluded: boolean; label?: string | null }
interface AutoCfg { folders: string[] | null; days: number; exclude: string[] }
interface Status { state: "working" | "needs_input" | "error" | "stalled" | "idle" | "missing"; level: "alarm" | "error" | "warning" | "busy" | "ok"; reason: string; last_text: string; updated_at: string | null; messages: number }
export interface MonItem {
  provider: Provider; id: string; machine?: string | null; label: string | null; title: string | null; directory: string | null; folder: string | null; folder_name: string | null;
  model: string | null; found: boolean; status: Status; app_job: { job_id: string; status: string } | null;
  send: { state: string; started_at: string; rc: number | null; busy_warning: boolean } | null;
}
interface Msg { role: string; text: string; tool?: string; status?: string; time: string | null }

const PROV_LABEL: Record<Provider, string> = { opencode: "OpenCode", codex: "Codex", claude: "Claude Code" };
/** A session's key: `provider:id`, `provider:id@machine` for one on a machine (as the server keys it). */
const keyOf = (x: { provider: string; id: string; machine?: string | null }) => `${x.provider}:${x.id}${x.machine ? `@${x.machine}` : ""}`;
const watchOf = (x: { provider: Provider; id: string; machine?: string | null }): Watch => ({ provider: x.provider, id: x.id, ...(x.machine ? { machine: x.machine } : {}) });
/** The `?machine=` of a session's routes. */
const onMachine = (x: { machine?: string | null }, sep = "?") => (x.machine ? `${sep}machine=${encodeURIComponent(x.machine)}` : "");
interface RemoteError { machine: string; error: string }
const ago = (iso: string | null) => {
  if (!iso) return "";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  return s < 60 ? "just now" : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago` : `${Math.floor(s / 86400)} d ago`;
};
const LEVEL_ORDER = { alarm: 0, error: 1, warning: 2, busy: 3, ok: 4 };
const STATE_LABEL: Record<string, string> = { working: "working", needs_input: "needs you", error: "error", stalled: "stalled", idle: "idle", missing: "not found" };

/** What the next messages to a session run with (control commands of its composer); unset: the session's own. */
interface Controls { model?: string; variant?: string; agent?: string }
const COMMANDS: { name: string; args: string; help: string; providers?: Provider[] }[] = [
  { name: "model", args: "[provider/model | default]", help: "the model the next messages use (no argument: choose one)" },
  { name: "effort", args: "[level | default]", help: "the reasoning level (variant) of the next messages", providers: ["opencode", "codex"] },
  { name: "agent", args: "[name | default]", help: "the OpenCode agent (build, plan, …) of the next messages", providers: ["opencode"] },
  { name: "reset", args: "", help: "back to the session's own model, effort and agent" },
  { name: "help", args: "", help: "list these commands" },
];
/** The sound played when a session starts needing you: choose one, try it, set its volume, or use a file of your own. */
function SoundChooser({ sound, onSound }: { sound: string; onSound: (id: string) => void }) {
  const [volume, setVolume] = useState(savedVolume);
  const [err, setErr] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const [customName, setCustomName] = useState(customSoundName);
  const pickFile = (f: File | undefined) => {
    if (!f) return;
    if (f.size > MAX_CUSTOM_BYTES) { setErr("The sound file must be at most 1 MiB."); return; }
    const r = new FileReader();
    r.onload = () => {
      try {
        saveCustomSound(String(r.result), f.name);
      } catch {
        setErr("The browser could not keep this file (too large).");
        return;
      }
      setErr(null);
      setCustomName(f.name);
      onSound("custom");
      playSound("custom", volume);
    };
    r.readAsDataURL(f);
  };
  return (
    <span className="mon-sound row nowrap small" title="A sound when a session starts needing you">
      <span aria-hidden>🔔</span>
      <select value={sound} aria-label="Sound when a session needs you" data-testid="mon-sound"
        onChange={(e) => {
          const id = e.target.value;
          if (id === "custom" && !customSound()) { file.current?.click(); return; }
          onSound(id);
          playSound(id, volume);
        }}>
        {SOUNDS.map((s) => <option key={s.id} value={s.id}>{s.id === "custom" && customName ? `My own: ${customName}` : s.label}</option>)}
      </select>
      {sound === "custom" && <button type="button" className="link-btn small" onClick={() => file.current?.click()} title="Choose another sound file" data-testid="mon-sound-file">change…</button>}
      <input ref={file} type="file" accept="audio/*" hidden data-testid="mon-sound-input" onChange={(e) => { pickFile(e.target.files?.[0]); e.target.value = ""; }} />
      {sound !== "off" && <>
        <input type="range" min={0} max={1} step={0.05} value={volume} aria-label="Volume" title={`Volume ${Math.round(volume * 100)}%`} data-testid="mon-sound-volume"
          onChange={(e) => { const v = Number(e.target.value); setVolume(v); saveVolume(v); }} onPointerUp={() => playSound(sound, volume)} />
        <button type="button" className="icon-btn small" onClick={() => playSound(sound, volume)} title="Play it" aria-label="Play the sound" data-testid="mon-sound-test">▶</button>
      </>}
      {err && <span className="warn small" role="alert">{err}</span>}
    </span>
  );
}

const commandsFor = (p: Provider) => COMMANDS.filter((c) => !c.providers || c.providers.includes(p));

// ---------------------------------------------------------------- sidebar

/** Sidebar of Agents: folders (the wiki folders, local folders you add, folders on a machine — `machine:/path`),
 * sessions found in them, and sessions added by id. Checked sessions are monitored on the main page. */
export function AgentsPanel({ workspaces, watched, onWatched, onAutoChanged }: { workspaces: WorkspaceInfo[]; watched: Watch[]; onWatched: (w: Watch[]) => void; onAutoChanged: () => void }) {
  const [extra, setExtra] = useState<{ path: string; name: string; exists: boolean; machine?: string | null }[]>([]);
  const [machines, setMachines] = useState<string[]>([]);
  const [remotePick, setRemotePick] = useState<string | null>(null); // the machine being browsed for a folder
  const [remoteErrs, setRemoteErrs] = useState<RemoteError[]>([]);
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
  const [manual, setManual] = useState<{ provider: Provider; id: string; label: string; machine: string }>({ provider: "opencode", id: "", label: "", machine: "" });
  const [more, setMore] = useState<Record<string, boolean>>({});
  const folders = useMemo(() => [
    ...workspaces.filter((w) => w.available).map((w) => ({ path: w.root, name: w.name, wiki: true, machine: null as string | null })),
    ...extra.filter((f) => !workspaces.some((w) => w.root === f.path)).map((f) => ({ path: f.path, name: f.name, wiki: false, machine: f.machine ?? null })),
  ], [workspaces, extra]);
  const chosen = sel ?? folders.map((f) => f.path);
  useEffect(() => { void get<{ folders: typeof extra }>("/monitor/folders").then((r) => setExtra(r.folders)).catch(() => undefined); }, []);
  useEffect(() => {
    void get<{ machines: MachineInfo[] }>("/machines").then((r) => setMachines(r.machines.filter((m) => m.kind === "machine" && m.name !== "local").map((m) => m.name))).catch(() => undefined);
  }, []);
  const discover = useCallback(async () => {
    if (!chosen.length) { setFound([]); return; }
    setBusy(true);
    try {
      const r = await get<{ sessions: Found[]; providers: Record<string, boolean>; remote_errors?: RemoteError[] }>(`/monitor/discover?folders=${encodeURIComponent(chosen.join("\n"))}`);
      setFound(r.sessions);
      setProviders(r.providers);
      setRemoteErrs(r.remote_errors ?? []);
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
  const isWatched = (x: { provider: string; id: string; machine?: string | null }) => watched.some((w) => keyOf(w) === keyOf(x));
  const monitored = (x: Found) => isWatched(x) || x.auto;
  const toggle = (x: Found) => {
    if (isWatched(x)) return onWatched(watched.filter((w) => keyOf(w) !== keyOf(x)));
    if (x.auto && auto) { setFound((f) => f && f.map((y) => (keyOf(y) === keyOf(x) ? { ...y, auto: false, excluded: true } : y))); return void saveAuto({ ...auto, exclude: [...auto.exclude, keyOf(x)] }); }
    if (x.excluded && auto) setFound((f) => f && f.map((y) => (keyOf(y) === keyOf(x) ? { ...y, excluded: false } : y)));
    if (x.excluded && auto) void saveAuto({ ...auto, exclude: auto.exclude.filter((k) => k !== keyOf(x)) });
    onWatched([...watched, watchOf(x)]);
  };
  /** Monitor (or stop monitoring) every session of a folder at once. */
  const setAllOf = (list: Found[], on: boolean) => {
    const keys = new Set(list.map(keyOf));
    if (on) {
      const add = list.filter((x) => !monitored(x)).map(watchOf);
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
      const w: Watch = { ...watchOf({ provider: manual.provider, id, machine: manual.machine || null }), ...(manual.label.trim() ? { label: manual.label.trim() } : {}) };
      const r = await post<{ found: boolean; title: string | null }>("/monitor/watched", w);
      onWatched([w, ...watched.filter((x) => keyOf(x) !== keyOf(w))]);
      if (!r.found) setErr(`Added, but no ${PROV_LABEL[manual.provider]} session ${id} was found on ${manual.machine || "this machine"} (yet).`);
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
        <div className="label">Folders <span className="spacer" /><button className="link-btn small" onClick={() => setPick(true)} data-testid="mon-add-folder">+ add folder</button>
          {machines.length > 0 && <select className="mon-add-remote small" value="" onChange={(e) => { if (e.target.value) setRemotePick(e.target.value); }}
            title="Add a folder on a machine (Machines view): its sessions are read there over SSH" data-testid="mon-add-remote">
            <option value="">+ on a machine…</option>
            {machines.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>}</div>
        <div className="muted small">Sessions of the checked folders are monitored when active in the last{" "}
          <select value={auto?.days ?? 7} onChange={(e) => auto && void saveAuto({ ...auto, days: Number(e.target.value) })} data-testid="mon-days">
            {[1, 3, 7, 14, 30, 90].map((d) => <option key={d} value={d}>{d} day{d > 1 ? "s" : ""}</option>)}
            <option value={0}>— (none: pick them below)</option>
          </select>
        </div>
        {err && !showList && <div className="error-text small" data-testid="mon-err">{err}</div>}
        {remoteErrs.map((e) => <div key={e.machine} className="error-text small" data-testid="mon-remote-err" title={e.error}>{e.machine}: {e.error}</div>)}
        {folders.map((f) => (
          <label key={f.path} className="agents-check small" title={f.path}>
            <input type="checkbox" checked={chosen.includes(f.path)} onChange={(e) => setSel(e.target.checked ? [...chosen, f.path] : chosen.filter((x) => x !== f.path))} data-testid="mon-folder" data-path={f.path} />
            <span className="agents-session-name">{f.name}</span>
            <span className={f.machine ? "chip mon-machine" : "muted"}>{f.wiki ? "wiki" : f.machine ? `@${f.machine}` : "local"}</span>
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
        {machines.length > 0 && <div className="row nowrap mon-manual">
          <select value={manual.machine} onChange={(e) => setManual({ ...manual, machine: e.target.value })} title="Where the session is" data-testid="mon-manual-machine">
            <option value="">on this server</option>
            {machines.map((m) => <option key={m} value={m}>on {m}</option>)}
          </select>
        </div>}
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
      {remotePick && <FolderPicker title={`Add a folder on ${remotePick}`} machine={remotePick} confirmLabel="Add this folder"
        hint="Its OpenCode, Codex and Claude Code sessions (made on that machine) are read there over SSH."
        onClose={() => setRemotePick(null)} onChoose={(p) => { const m = remotePick; setRemotePick(null); void addFolder(`${m}:${p}`); }} />}
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
  const [remoteErrs, setRemoteErrs] = useState<RemoteError[]>([]);
  const [stage, setStage] = useState<string | null>(() => localStorage.getItem("waw.mon.stage"));
  const listPane = usePane("mon.list", 220, 140, 900);
  const [stageFolded, setStageFolded] = useState(false); // the session view folded: the list takes the width
  const [notify, setNotify] = useState(() => localStorage.getItem("waw.mon.notify") === "1");
  const alarmed = useRef<Set<string> | null>(null); // null until the first check: what already needs you then makes no sound
  const [sound, setSound] = useState(savedSound);
  const soundRef = useRef(sound);
  soundRef.current = sound;
  useEffect(() => { if (stage) localStorage.setItem("waw.mon.stage", stage); else localStorage.removeItem("waw.mon.stage"); }, [stage]);
  useEffect(() => { localStorage.setItem("waw.mon.notify", notify ? "1" : "0"); }, [notify]);
  const wkey = watched.map(keyOf).join(",");
  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      const r = await get<{ items: MonItem[]; alarms: number; checked_at: string; remote_errors?: RemoteError[] }>("/monitor/status");
      setItems(r.items);
      setRemoteErrs(r.remote_errors ?? []);
      setChecked(r.checked_at);
      onAlarms(r.alarms);
      setErr(null);
      // a notification when a session starts needing you
      const now = new Set(r.items.filter((i) => i.status.level === "alarm").map(keyOf));
      const before = alarmed.current ?? new Set<string>();
      if (notify && "Notification" in window && Notification.permission === "granted") {
        for (const i of r.items) if (now.has(keyOf(i)) && !before.has(keyOf(i))) new Notification(`${i.label ?? i.title ?? i.id} needs you`, { body: i.status.reason });
      }
      // …and a sound (once per check, however many started needing you)
      if (alarmed.current && [...now].some((k) => !before.has(k))) playSound(soundRef.current);
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
    const own = i.provider === "opencode";
    const name = window.prompt(own ? "Rename this OpenCode session (its own title, shown in OpenCode too):"
      : `Name to show for this ${PROV_LABEL[i.provider]} session (empty: its own title):`, i.label ?? i.title ?? "");
    if (name === null) return;
    try {
      const r = await put<{ native: boolean }>(`/monitor/session/${i.provider}/${encodeURIComponent(i.id)}/label${onMachine(i)}`, { label: name });
      if (own && name.trim() && !r.native) setErr("OpenCode could not rename it (v1, or its server is not running): the new name is shown only here.");
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
      const r = await post<{ deleted: string[]; failed: { id: string; error: string }[] }>("/monitor/delete", { sessions: list.map(watchOf) });
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
        <SoundChooser sound={sound} onSound={(id) => { setSound(id); saveSound(id); }} />
        {pickedItems.length > 0 && <>
          <span className="small">{pickedItems.length} selected</span>
          <button className="btn small danger" onClick={() => void removeItems(pickedItems)} data-testid="mon-delete-selected">Delete selected</button>
          <button className="link-btn small" onClick={() => setPicked(new Set())}>clear</button>
        </>}
        <button className="btn small primary" disabled={busy} onClick={() => void refresh()} data-testid="mon-refresh">{busy ? "Checking…" : "Refresh all"}</button>
      </div>
      {err && <div className="banner warn small" role="alert">{err}</div>}
      {remoteErrs.map((e) => <div key={e.machine} className="banner warn small" data-testid="mon-remote-banner">Sessions on {e.machine} could not be read: {e.error}</div>)}
      {items && items.length === 0 && <div className="muted dev-hint" data-testid="mon-empty">No session to monitor: check folders in the sidebar (their recent sessions are monitored), widen the time window, or add a session by its id.</div>}
      <div className={`mon-body ${staged && stageFolded && !listPane.folded ? "list-only" : ""}`}>
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
                        <button className="icon-btn small" title="Rename (OpenCode: its own title; others: the name shown here)" onClick={() => void renameItem(i)} data-testid="mon-rename">✎</button>
                        <button className="icon-btn small" title="Delete this session" onClick={() => void removeItems([i])} data-testid="mon-delete">🗑</button>
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>}
        {staged && stageFolded && !listPane.folded && <FoldStrip label="Session" side="right" onShow={() => setStageFolded(false)} testId="mon-stage-unfold" />}
        {staged && !(stageFolded && !listPane.folded) && <Splitter dir="x" side="before" pane={listPane} label="session list" testId="mon-list-splitter"
          other={{ label: "session view", onFold: () => setStageFolded(true) }} />}
        {staged && !(stageFolded && !listPane.folded) && <SessionStage key={keyOf(staged)} item={staged} onClose={() => setStage(null)} onUnwatch={() => void unwatch(staged)} onChanged={() => void refresh()}
          onRename={() => void renameItem(staged)} onDelete={() => void removeItems([staged])} />}
      </div>
    </div>
  );
}

function MonCard({ item: i, compact, active, onOpen }: { item: MonItem; compact: boolean; active: boolean; onOpen: () => void }) {
  const name = i.label ?? i.title ?? i.id;
  return (
    <button className={`dash-card mon-card ${i.status.level} ${compact ? "compact" : ""} ${active ? "active" : ""}`} onClick={onOpen}
      data-testid="mon-card" data-key={keyOf(i)} data-state={i.status.state} data-level={i.status.level} title={`${PROV_LABEL[i.provider]} · ${i.id}${i.machine ? ` on ${i.machine}` : ""}\n${i.directory ?? ""}`}>
      <div className="dash-card-head">
        <span className={`dash-dot ${i.status.level}`} />
        <b className="dash-name">{name}</b>
        <span className={`chip mon-state ${i.status.state}`} data-testid="mon-state">{STATE_LABEL[i.status.state] ?? i.status.state}</span>
      </div>
      <div className="muted small mon-meta"><span className={`mon-prov ${i.provider}`}>{PROV_LABEL[i.provider]}</span>{i.machine && <span className="chip mon-machine" data-testid="mon-machine">@{i.machine}</span>} {i.folder_name ?? i.directory ?? ""} · {ago(i.status.updated_at)}</div>
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
  const mq = onMachine(item);
  const ctlKey = `waw.mon.ctl.${keyOf(item)}`;
  const [ctl, setCtlState] = useState<Controls>(() => { try { return JSON.parse(localStorage.getItem(ctlKey) ?? "{}"); } catch { return {}; } });
  const setCtl = (c: Controls) => {
    const clean = Object.fromEntries(Object.entries(c).filter(([, v]) => v)) as Controls;
    setCtlState(clean);
    if (Object.keys(clean).length) localStorage.setItem(ctlKey, JSON.stringify(clean));
    else localStorage.removeItem(ctlKey);
  };
  const [picker, setPicker] = useState<"model" | "agent" | null>(null);
  const pics = usePictures();
  const [agentText, setAgentText] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);
  const cmds = commandsFor(item.provider);
  /** a control command (`/model …`): true when `t` was one (it is not sent to the agent) */
  const command = (t: string): boolean => {
    const m = /^\/(\w+)(?:\s+([\s\S]*))?$/.exec(t.trim());
    const c = m && cmds.find((x) => x.name === m[1].toLowerCase() || (m[1].toLowerCase() === "variant" && x.name === "effort"));
    if (!m || !c) return false;
    const arg = (m[2] ?? "").trim();
    const reset = /^(default|reset|none)$/i.test(arg);
    if (arg && !reset && !/^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/.test(arg)) {
      setErr(`/${c.name}: "${arg.slice(0, 60)}" is not a valid name`);
      return true;
    }
    setErr(null);
    if (c.name === "help") setNote(cmds.map((x) => `/${x.name} ${x.args} — ${x.help}`).join("\n"));
    else if (c.name === "reset") { setCtl({}); setPicker(null); setNote("The next messages use the session's own model, effort and agent."); }
    else if (c.name === "model") {
      if (!arg) setPicker("model");
      else { setCtl({ ...ctl, model: reset ? undefined : arg, ...(reset ? { variant: undefined } : {}) }); setPicker(null); }
    } else if (c.name === "effort") {
      if (!arg) setPicker("model");
      else { setCtl({ ...ctl, variant: reset ? undefined : arg }); setPicker(null); }
    } else if (c.name === "agent") {
      if (!arg) { setAgentText(ctl.agent ?? ""); setPicker("agent"); }
      else { setCtl({ ...ctl, agent: reset ? undefined : arg }); setPicker(null); }
    }
    return true;
  };
  const suggest = /^\/\w*$/.test(text) ? cmds.filter((c) => c.name.startsWith(text.slice(1).toLowerCase())) : [];
  const load = useCallback(async () => {
    try {
      setDetail(await get(`${base}?limit=${limit}${onMachine(item, "&")}`));
      setErr(null);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [base, limit, item.machine]); // eslint-disable-line react-hooks/exhaustive-deps
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
    if (!t && !pics.pictures.length) return;
    setNote(null);
    if (t && command(t)) {
      setText("");
      return;
    }
    setErr(null);
    try {
      const r = await post<{ busy_warning: boolean }>(`${base}/send${mq}`, {
        text: t || picturesOnly(pics.pictures.length), ...ctl, images: pics.urls,
      });
      setText("");
      pics.clear();
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
      await post(`${base}/terminal${mq}`);
      setNote(`A terminal tab with this session${item.machine ? ` (on ${item.machine})` : ""} is open in the Terminal view.`);
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
        <span className={`mon-prov ${d.provider}`}>{PROV_LABEL[d.provider]}</span>{d.machine && <> <span className="chip mon-machine">on {d.machine}</span></>}{" "}
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
              <div className="msg-head"><span className="who">{m.role === "user" ? "You" : "Agent"}</span><span className="muted small msg-time" data-testid="msg-time">{m.time ? utc8(m.time) : ""}</span></div>
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
        {note && <div className="muted small mon-note" data-testid="mon-note">{note}</div>}
        {(Object.keys(ctl).length > 0 || picker) && (
          <div className="mon-controls small" data-testid="mon-controls">
            <span className="muted">Next messages:</span>
            {ctl.model && <span className="chip mono" data-testid="mon-ctl-model">model {ctl.model} <button className="icon-btn small" onClick={() => setCtl({ ...ctl, model: undefined })} aria-label="Back to the session's model">×</button></span>}
            {ctl.variant && <span className="chip" data-testid="mon-ctl-effort">effort {ctl.variant} <button className="icon-btn small" onClick={() => setCtl({ ...ctl, variant: undefined })} aria-label="Back to the session's effort">×</button></span>}
            {ctl.agent && <span className="chip" data-testid="mon-ctl-agent">agent {ctl.agent} <button className="icon-btn small" onClick={() => setCtl({ ...ctl, agent: undefined })} aria-label="Back to the session's agent">×</button></span>}
            {picker === "model" && (
              <span className="mon-picker" data-testid="mon-model-picker">
                <ModelChoice provider={d.provider} providers={[d.provider]} value={ctl.model || ctl.variant ? { provider: d.provider, model: ctl.model, variant: ctl.variant } : null}
                  session={{ provider: d.provider, model: d.model, variant: null } as unknown as AgentSession}
                  onChange={(v) => setCtl({ ...ctl, model: v?.model, variant: v?.variant })} />
                <button className="btn small" onClick={() => setPicker(null)} data-testid="mon-picker-done">Done</button>
              </span>
            )}
            {picker === "agent" && (
              <span className="mon-picker" data-testid="mon-agent-picker">
                <input value={agentText} onChange={(e) => setAgentText(e.target.value)} placeholder="agent name (build, plan, …)" className="mono" data-testid="mon-agent-input"
                  onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); command(`/agent ${agentText.trim() || "default"}`); } }} />
                <button className="btn small" onClick={() => command(`/agent ${agentText.trim() || "default"}`)} data-testid="mon-agent-set">Set</button>
              </span>
            )}
            {!picker && <button className="link-btn small" onClick={() => setCtl({})} data-testid="mon-ctl-reset">reset</button>}
          </div>
        )}
        {suggest.length > 0 && (
          <div className="mon-cmd-suggest" role="listbox" data-testid="mon-cmd-suggest">
            {suggest.map((c) => (
              <button key={c.name} role="option" className="mon-cmd" data-testid="mon-cmd"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => { setText(""); setNote(null); command(`/${c.name}`); input.current?.focus(); }}>
                <span className="mono">/{c.name}</span> <span className="muted">{c.args}</span> <span className="muted small">— {c.help}</span>
              </button>
            ))}
          </div>
        )}
        <PictureStrip pics={pics} />
        <textarea ref={input} rows={3} value={text} onChange={(e) => setText(e.target.value)}
          placeholder={`Message ${PROV_LABEL[d.provider]} in this session — ${PASTE_HINT}; / for commands such as /model (${COMPOSER_HINT})`}
          {...pics.handlers}
          onKeyDown={(e) => composerKeyDown(e, () => void send(), setText)} disabled={!d.found} data-testid="mon-input" />
        <div className="row">
          <span className="muted small">Runs in {d.directory ?? "~"} with the provider's permission rules; for interactive prompts use <b>Open in terminal</b>.</span>
          <span className="spacer" />
          <button className="btn small primary" disabled={(!text.trim() && !pics.pictures.length) || d.send?.state === "running" || !d.found} onClick={() => void send()} data-testid="mon-send">Send</button>
        </div>
      </div>
    </section>
  );
}
