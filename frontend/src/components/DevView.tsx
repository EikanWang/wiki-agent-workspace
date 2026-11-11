import { Splitter, usePane } from "./Split";
import { Suspense, lazy, useCallback, useEffect, useState } from "react";
import { AgentSession, ApiError, DevInfo, DevSettings, DevWorkspace, Thread, WorkspaceInfo, del, get, patch, post, subscribeGlobal } from "../api";
import DevAgentPanel, { CodeAnchor, EditorFiles } from "./DevAgentPanel";
import DevTerminal from "./DevTerminal";
import { PrTab, TasksTab } from "./DevTabs";
import DevChanges, { ChangesAsk } from "./DevChanges";
import { confirmAction } from "./Confirm";
import { usePersistentState } from "../persist";

type Bottom = "terminal" | "tasks" | "changes" | "pr";

// the web editor (Monaco + language servers) is loaded only when a workspace shows it
const WebIde = lazy(() => import("./webide/WebIde"));

/**
 * A development workspace: the VS Code editor (code-server through the app) on the left, the agent on the right,
 * and below the editor: builds/tests, the worktree's changes (commit, push) and the pull request.
 */
export default function DevView({ devId, workspaces, sessions, onSessionsChanged, onArchived, onOpenPrReview }: {
  devId: string;
  workspaces: WorkspaceInfo[];
  sessions: AgentSession[];
  onSessionsChanged: () => void;
  onArchived: () => void;
  onOpenPrReview?: (repo: string, number: number) => void;
}) {
  const [dev, setDev] = useState<DevWorkspace | null>(null);
  const [info, setInfo] = useState<DevInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [bottom, setBottom] = usePersistentState<Bottom>(`dev.bottom.${devId}`, "tasks");
  const [bottomOpen, setBottomOpen] = usePersistentState(`dev.bottomOpen`, true);
  // height of the bottom panel (Terminal, Build & test, Changes, Pull request) in px; null = the default (34%)
  const [bottomHeight, setBottomHeight] = usePersistentState<number | null>(`dev.bottomHeight`, null);
  const agentPane = usePane("dev.agent", 380, 240, 1400);
  const agentOpen = !agentPane.folded;
  const setAgentOpen = (open: boolean) => agentPane.setFolded(!open);
  // the Changes view in the editor area (true) instead of the bottom panel
  const [changesWide, setChangesWide] = usePersistentState(`dev.changesWide.${devId}`, false);
  // the Changes view shows only this folder / file (the ± of a row in the explorer); null: everything
  const [changesScope, setChangesScope] = usePersistentState<string | null>(`dev.changesScope.${devId}`, null);
  const [branchMenu, setBranchMenu] = useState(false);
  const [settings, setSettings] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [ext, setExt] = useState<{ state: string; log: string; failed?: string[] } | null>(null);
  // the workspace's conversation (tasks), shared by the agent panel and the editor's margin markers
  const [threads, setThreads] = useState<Thread[]>([]);
  const [fileSessions, setFileSessions] = useState<Record<string, string>>({});
  const [files, setFiles] = useState<EditorFiles | null>(null);
  const [focusThread, setFocusThread] = useState<{ id: string; at: number } | null>(null);
  const [reveal, setReveal] = useState<{ anchor: CodeAnchor; at: number } | null>(null);
  const loadThreads = useCallback(async () => {
    try {
      const r = await get<{ threads: Thread[]; file_sessions?: Record<string, string> }>(`/dev/workspaces/${devId}/conversation`);
      setThreads(r.threads);
      setFileSessions(r.file_sessions ?? {});
    } catch {
      /* not ready yet */
    }
  }, [devId]);
  useEffect(() => {
    void loadThreads();
    const t = setInterval(() => { if (document.visibilityState === "visible") void loadThreads(); }, 4000); // turns started elsewhere
    return () => clearInterval(t);
  }, [loadThreads]);
  const load = useCallback(async () => {
    try {
      setDev(await get<DevWorkspace>(`/dev/workspaces/${devId}`));
      setErr(null);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [devId]);
  useEffect(() => {
    setDev(null);
    void load();
    void get<DevInfo>("/dev/info").then(setInfo).catch(() => undefined);
    return subscribeGlobal((ev) => {
      if ((ev.type === "dev_changed" || ev.type === "dev_task") && ev.dev_id === devId) {
        void load();
        if (ev.type === "dev_task") setRefresh((n) => n + 1);
      }
    });
  }, [devId, load]);
  // auto-start the editor when the workspace is ready (it is cheap to keep running; Stop frees it)
  useEffect(() => {
    if (dev?.status === "ready" && dev.settings.editor === "vscode" && dev.editor.state === "stopped" && info?.editor.available !== false && dev.host === "local" && !sessionStorage.getItem(`dev.stopped.${dev.id}`)) {
      void post(`/dev/workspaces/${dev.id}/editor/start`).then(load).catch((e) => setErr((e as ApiError).message));
    }
  }, [dev?.status, dev?.editor.state, dev?.id, dev?.host, dev?.settings.editor, info, load]);
  useEffect(() => {
    if (!ext || ext.state !== "running") return;
    const t = setInterval(() => void get<typeof ext>(`/dev/extensions?host=${encodeURIComponent(dev?.host ?? "local")}`).then(setExt), 1500);
    return () => clearInterval(t);
  }, [ext, dev?.host]);

  if (err && !dev) return <div className="banner warn">{err}</div>;
  if (!dev) return <div className="muted dev-hint">Loading…</div>;
  const editor = dev.editor;
  const web = dev.settings.editor !== "vscode";
  const switchEditor = async (to: "web" | "vscode") => {
    try {
      if (to === "web" && editor.state !== "stopped") await post(`/dev/workspaces/${dev.id}/editor/stop`);
      setDev(await patch<DevWorkspace>(`/dev/workspaces/${dev.id}`, { settings: { editor: to } }));
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const editorAction = async (what: "start" | "stop" | "restart") => {
    setErr(null);
    if (what === "stop") sessionStorage.setItem(`dev.stopped.${dev.id}`, "1");
    else sessionStorage.removeItem(`dev.stopped.${dev.id}`);
    try {
      await post(`/dev/workspaces/${dev.id}/editor/${what}`);
      await load();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const ask = async (kind: string, text: string) => {
    try {
      await post(`/dev/workspaces/${dev.id}/ask`, { text, kind });
      setAgentOpen(true);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const askAbout = async ({ text, anchor, context }: ChangesAsk) => {
    const r = await post<{ thread_id: string }>(`/dev/workspaces/${dev.id}/ask`, { text, kind: "ask", context, anchor });
    setAgentOpen(true);
    onSessionsChanged();
    await loadThreads();
    setFocusThread({ id: r.thread_id, at: Date.now() });
  };
  const openFile = web ? (path: string, line?: number) => {
    setChangesWide(false);
    setReveal({ anchor: { root: "wt", file: path, start_line: line ?? null, end_line: line ?? null, quote: "" }, at: Date.now() });
  } : undefined;
  const showDiff = (path: string | null) => { setChangesScope(path); setChangesWide(true); };
  const changes = (wide: boolean) => (
    <DevChanges key={dev.id} dev={dev} refreshKey={refresh} onChanged={() => void load()} onAsk={askAbout} onOpenFile={openFile}
      scope={changesScope} onScope={setChangesScope}
      wide={wide} onToggleWide={() => {
        setChangesWide(!wide);
        if (wide) { setBottom("changes"); setBottomOpen(true); } // back to the editor: the changes stay at hand below it
      }} />
  );
  const archive = async () => {
    if (!(await confirmAction({ title: "Archive this development workspace?", message: dev.mode === "folder" ? `The editor stops and the workspace leaves the list; the folder ${dev.worktree_path} is not touched.` : "The editor stops and the workspace leaves the list; its branch and conversation are kept.", confirmLabel: "Archive" }))) return;
    const remove = dev.mode !== "folder" && await confirmAction({ title: "Remove its Git worktree too?", message: `Deletes the folder ${dev.worktree_path} (refused when it has uncommitted or unpushed work). Choose Cancel to keep it.`, confirmLabel: "Remove worktree" });
    try {
      await del(`/dev/workspaces/${dev.id}?remove_worktree=${remove ? "true" : "false"}`);
      onArchived();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const wiki = workspaces.find((w) => w.id === dev.wiki_workspace_id);
  return (
    <div className={`dev-view ${agentOpen ? "" : "agent-closed"}`} data-testid="dev-view">
      <div className="dev-toolbar">
        <b className="dev-title" title={dev.worktree_path}>{dev.name}</b>
        <span className="branch-anchor">
          {dev.mode === "pr" ? <span className="chip mono" title={`worktree ${dev.worktree_path}`}>{dev.branch}</span> : (
            <button className="chip chip-btn mono" onClick={() => setBranchMenu((v) => !v)} disabled={dev.status !== "ready"}
              title={`${dev.mode === "folder" ? "folder" : "worktree"} ${dev.worktree_path}\nCreate a branch or switch to another`} data-testid="dev-branch-button">⎇ {dev.branch} ▾</button>
          )}
          {branchMenu && <BranchMenu dev={dev} onClose={() => setBranchMenu(false)} onDone={(m) => { setBranchMenu(false); void load(); setRefresh((n) => n + 1); setErr(null); if (m) setNote(m); }} />}
        </span>
        {dev.mode === "folder" && <span className="chip" title={`Works directly in ${dev.worktree_path}`}>folder</span>}
        {dev.host !== "local" && <span className="chip" title="Runs on this SSH host">{dev.host}</span>}
        {dev.pr_number && dev.github_repo && (
          <button className="chip chip-btn" onClick={() => onOpenPrReview?.(dev.github_repo!, dev.pr_number!)} title="Open it in PR Review">#{dev.pr_number}</button>
        )}
        {wiki && <span className="muted small" title={wiki.root}>wiki: {wiki.name}</span>}
        <span className="spacer" />
        {dev.status === "ready" && (
          <select value={web ? "web" : "vscode"} onChange={(e) => void switchEditor(e.target.value as "web" | "vscode")} title="Editor: the app's web editor, or VS Code (code-server)" data-testid="dev-editor-kind">
            <option value="web">Web editor</option>
            <option value="vscode">VS Code (code-server)</option>
          </select>
        )}
        {dev.status === "ready" && !web && (editor.state === "running" ? (
          <>
            <a className="btn small" href={editor.url ?? "#"} target="_blank" rel="noreferrer" title="Open the editor in its own browser tab (more keyboard shortcuts work there)">Open in tab</a>
            <button className="btn small" onClick={() => void editorAction("restart")}>Restart editor</button>
            <button className="btn small" onClick={() => void editorAction("stop")} data-testid="dev-editor-stop">Stop editor</button>
          </>
        ) : (
          <button className="btn small primary" disabled={editor.state === "starting"} onClick={() => void editorAction("start")} data-testid="dev-editor-start">{editor.state === "starting" ? "Starting editor…" : "Start editor"}</button>
        ))}
        {dev.status === "ready" && (
          <button className={`btn small toggle ${changesWide ? "on" : ""}`} onClick={() => (changesWide ? setChangesWide(false) : showDiff(null))} aria-pressed={changesWide}
            title="View all changes (diff) and commit them, in the editor area; ± on a file or folder of the explorer shows only its changes" data-testid="dev-changes-toggle">Diff</button>
        )}
        <button className="btn small" onClick={() => setSettings(true)} data-testid="dev-settings">Settings</button>
        <button className={`btn small toggle ${agentOpen ? "on" : ""}`} onClick={() => setAgentOpen(!agentOpen)} aria-pressed={agentOpen}>Agent</button>
        <button className="icon-btn" onClick={() => void archive()} title="Archive">⋯</button>
      </div>
      {dev.status === "creating" && <div className="banner">Preparing the worktree (fetching, checking out)…</div>}
      {dev.status === "failed" && <div className="banner warn" role="alert">{dev.error}</div>}
      {dev.status === "ready" && dev.error && <div className="banner warn small">{dev.error}</div>}
      {err && <div className="banner warn small" role="alert">{err}</div>}
      {note && <div className="banner small" data-testid="dev-banner-note">{note} <button className="link-btn" onClick={() => setNote(null)}>ok</button></div>}
      <div className="dev-body">
        <div className="dev-main">
          {changesWide && dev.status === "ready" && <div className="dev-editor dev-changes-main">{changes(true)}</div>}
          <div className="dev-editor" style={changesWide && dev.status === "ready" ? { display: "none" } : undefined}>
            {web && dev.status === "ready" ? (
              <Suspense fallback={<div className="muted dev-hint">Loading the editor…</div>}>
                <WebIde key={dev.id} dev={dev} completionEnabled={!!info?.completion.enabled} refreshKey={refresh} threads={threads} reveal={reveal} onDiff={showDiff}
                  onFiles={setFiles} onOpenThread={(id) => { setAgentOpen(true); setFocusThread({ id, at: Date.now() }); }}
                  discussSession={(a) => { const sid = a ? fileSessions[`${a.root}:${a.file}`] : undefined; return sid ? sessions.find((x) => x.id === sid) ?? null : null; }}
                  onAsk={async ({ kind, text, context, anchor, threadId, image, modelChoice }) => {
                    const r = await post<{ thread_id: string }>(`/dev/workspaces/${dev.id}/ask`, { text, kind, context, anchor, thread_id: threadId, image, model_choice: modelChoice ?? undefined });
                    setAgentOpen(true);
                    onSessionsChanged();
                    await loadThreads();
                    setFocusThread({ id: r.thread_id, at: Date.now() });
                  }} />
              </Suspense>
            ) : editor.state === "running" && editor.url ? (
              <iframe key={editor.started_at} src={editor.url} title="Editor" className="dev-iframe" data-testid="dev-iframe" allow="clipboard-read; clipboard-write" />
            ) : (
              <div className="dev-editor-placeholder">
                {dev.status !== "ready" ? null : editor.state === "starting" ? <div className="muted">Starting the editor…</div> : (
                  <>
                    {editor.state === "failed" && <div className="error-text" data-testid="dev-editor-error">{editor.error}</div>}
                    {info && !info.editor.available && dev.host === "local" && <div className="warn">{info.editor.install_hint}</div>}
                    <div className="row"><button className="btn primary" onClick={() => void editorAction("start")}>Start editor</button></div>
                  </>
                )}
                {dev.status === "ready" && (
                  <div className="muted small dev-hint">
                    C/C++ and Python language support (completion, go to definition, diagnostics, debugging) comes from editor extensions:
                    clangd, CodeLLDB, Python, debugpy, basedpyright, Ruff.{" "}
                    <button className="btn small" disabled={ext?.state === "running"} onClick={() => void post<typeof ext>("/dev/extensions", { host: dev.host }).then(setExt).catch((e) => setErr((e as ApiError).message))} data-testid="dev-install-ext">
                      {ext?.state === "running" ? "Installing…" : "Install language support"}
                    </button>
                    {ext && ext.state !== "running" && <span className={ext.state === "failed" ? "warn" : ""}> {ext.state === "done" ? "Installed — restart the editor." : `Failed: ${(ext.failed ?? []).join(", ")}`}</span>}
                  </div>
                )}
              </div>
            )}
          </div>
          {dev.status === "ready" && (
            <div className={`dev-bottom ${bottomOpen ? "" : "closed"}`} style={bottomOpen && bottomHeight ? { height: bottomHeight } : undefined}>
              {bottomOpen && <BottomSplitter onHeight={setBottomHeight} />}
              <div className="dev-bottom-tabs">
                {(["terminal", "tasks", "changes", "pr"] as Bottom[]).map((b) => (
                  <button key={b} className={bottom === b && bottomOpen ? "active" : ""} onClick={() => { setBottom(b); setBottomOpen(true); }} data-testid={`dev-tab-${b}`}>
                    {b === "terminal" ? "Terminal" : b === "tasks" ? `Build & test${dev.running_tasks ? ` (${dev.running_tasks})` : ""}` : b === "changes" ? "Changes" : dev.pr_number ? `Pull request #${dev.pr_number}` : "Pull request"}
                  </button>
                ))}
                <span className="spacer" />
                <button className="icon-btn small" onClick={() => setBottomOpen(!bottomOpen)} title={bottomOpen ? "Hide" : "Show"}>{bottomOpen ? "▾" : "▴"}</button>
              </div>
              {bottomOpen && (
                <div className="dev-bottom-body">
                  <div className="dev-terminal-wrap" style={{ display: bottom === "terminal" ? undefined : "none" }}>
                    <DevTerminal key={dev.id} dev={dev} visible={bottom === "terminal" && bottomOpen} />
                  </div>
                  {bottom === "tasks" && <TasksTab key={dev.id} dev={dev} onAsked={() => setAgentOpen(true)} />}
                  {bottom === "changes" && (changesWide
                    ? <div className="muted small dev-hint">The changes are shown in the editor area. <button className="link-btn" onClick={() => setChangesWide(false)}>Show them here</button></div>
                    : changes(false))}
                  {bottom === "pr" && <PrTab key={dev.id} dev={dev} onAsk={ask} onChanged={() => void load()} />}
                </div>
              )}
            </div>
          )}
        </div>
        {dev.status === "ready" && <Splitter dir="x" side="after" pane={agentPane} label="agent panel" testId="dev-agent-splitter" />}
        {agentOpen && dev.status === "ready" && (
          <aside className="dev-side" style={{ width: agentPane.size }}>
            <DevAgentPanel dev={dev} sessions={sessions} threads={threads} fileSessions={fileSessions} onReload={loadThreads} onSessionsChanged={onSessionsChanged}
              onTurnFinished={() => setRefresh((n) => n + 1)} files={web ? files : null} focusThread={focusThread}
              onRevealAnchor={web ? (a) => setReveal({ anchor: a, at: Date.now() }) : undefined} />
          </aside>
        )}
      </div>
      {settings && <DevSettingsDialog dev={dev} workspaces={workspaces} onClose={() => setSettings(false)} onSaved={(d) => { setDev(d); setSettings(false); }} />}
    </div>
  );
}

function DevSettingsDialog({ dev, workspaces, onClose, onSaved }: { dev: DevWorkspace; workspaces: WorkspaceInfo[]; onClose: () => void; onSaved: (d: DevWorkspace) => void }) {
  const [name, setName] = useState(dev.name);
  const [wiki, setWiki] = useState(dev.wiki_workspace_id ?? "");
  const [s, setS] = useState<Record<string, string>>(() => ({
    python: dev.settings.python ?? "", python_lsp: dev.settings.python_lsp ?? "", compile_commands_dir: dev.settings.compile_commands_dir ?? "", clangd: dev.settings.clangd ?? "",
    build_command: dev.settings.build_command ?? "", test_command: dev.settings.test_command ?? "", debug_program: dev.settings.debug_program ?? "",
  }));
  const [args, setArgs] = useState((dev.settings.debug_args ?? []).join(" "));
  const [err, setErr] = useState<string | null>(null);
  const [snap, setSnap] = useState<string | null>(null);
  const field = (k: keyof DevSettings, label: string, ph: string) => (
    <label className="dev-field"><span className="label">{label}</span>
      <input className="mono" value={s[k as string] ?? ""} placeholder={ph} onChange={(e) => setS({ ...s, [k]: e.target.value })} data-testid={`dev-set-${k}`} /></label>
  );
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Development workspace settings" data-testid="dev-settings-dialog">
        <div className="modal-head"><span>Workspace settings</span><button className="icon-btn" onClick={onClose}>×</button></div>
        <div className="muted small mono">{dev.worktree_path}{dev.host !== "local" ? ` on ${dev.host}` : ""}</div>
        <label className="dev-field"><span className="label">Name</span><input value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="dev-field"><span className="label">Reference wiki</span>
          <select value={wiki} onChange={(e) => setWiki(e.target.value)}>
            <option value="">None</option>
            {workspaces.filter((w) => w.available).map((w) => <option key={w.id} value={w.id}>{w.name} — {w.root}</option>)}
          </select>
        </label>
        {dev.host !== "local" && dev.wiki_workspace_id && (
          <div className="row"><button className="btn small" onClick={() => void post<{ synced: boolean }>(`/dev/workspaces/${dev.id}/wiki-snapshot`).then(() => setSnap("Wiki snapshot refreshed.")).catch((e) => setErr((e as ApiError).message))}>Refresh the wiki snapshot on {dev.host}</button>{snap && <span className="small">{snap}</span>}</div>
        )}
        {field("python", "Python interpreter", "python3 or /path/to/venv/bin/python")}
        {field("python_lsp", "Python language server (web editor)", "default: basedpyright-langserver / pyright-langserver / pylsp")}
        {field("compile_commands_dir", "compile_commands.json folder (relative to the worktree)", "build")}
        {field("clangd", "clangd executable", "default: clangd on the host's PATH")}
        {field("build_command", "Build command", "cmake --build build")}
        {field("test_command", "Test command", "ctest --test-dir build && pytest -q")}
        {field("debug_program", "C++ program to debug (relative to the worktree)", "build/app")}
        <label className="dev-field"><span className="label">Program arguments</span><input className="mono" value={args} onChange={(e) => setArgs(e.target.value)} /></label>
        <div className="muted small">The editor's tasks (waw: build / waw: test) and debug configurations follow these settings.</div>
        {err && <div className="error-text">{err}</div>}
        <div className="row"><span className="spacer" /><button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" data-testid="dev-settings-save" onClick={() => void patch<DevWorkspace>(`/dev/workspaces/${dev.id}`, {
            name, wiki_workspace_id: wiki, settings: { ...s, debug_args: args.trim() ? args.trim().split(/\s+/) : undefined },
          }).then(onSaved).catch((e) => setErr((e as ApiError).message))}>Save</button>
        </div>
      </div>
    </div>
  );
}


/**
 * The drag handle above the bottom panel: drag to change its height (kept per browser), double-click for the
 * default. Pointer capture keeps the drag going over the editor (also the VS Code iframe).
 */
function BottomSplitter({ onHeight }: { onHeight: (h: number | null) => void }) {
  return (
    <div className="dev-bottom-splitter" role="separator" aria-orientation="horizontal" title="Drag to resize · double-click: default height"
      data-testid="dev-bottom-splitter"
      onDoubleClick={() => onHeight(null)}
      onPointerDown={(e) => {
        const panel = (e.currentTarget.parentElement as HTMLElement);
        const main = panel.parentElement as HTMLElement;
        const startY = e.clientY;
        const startH = panel.getBoundingClientRect().height;
        const max = main.getBoundingClientRect().height - 80; // keep some editor visible
        const el = e.currentTarget;
        el.setPointerCapture(e.pointerId);
        document.body.classList.add("resizing-v");
        const move = (ev: PointerEvent) => onHeight(Math.round(Math.max(120, Math.min(max, startH + (startY - ev.clientY)))));
        const up = (ev: PointerEvent) => {
          el.releasePointerCapture(ev.pointerId);
          el.removeEventListener("pointermove", move);
          el.removeEventListener("pointerup", up);
          el.removeEventListener("pointercancel", up);
          document.body.classList.remove("resizing-v");
        };
        el.addEventListener("pointermove", move);
        el.addEventListener("pointerup", up);
        el.addEventListener("pointercancel", up);
        e.preventDefault();
      }} />
  );
}


/** Create a branch from the current one (the uncommitted changes go along), or switch to another branch. */
function BranchMenu({ dev, onClose, onDone }: { dev: DevWorkspace; onClose: () => void; onDone: (note: string | null) => void }) {
  const [data, setData] = useState<{ current: string; branches: string[]; base_ref: string | null } | null>(null);
  const [name, setName] = useState("");
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { void get<NonNullable<typeof data>>(`/dev/workspaces/${dev.id}/branches`).then(setData).catch((e) => setErr((e as ApiError).message)); }, [dev.id]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const go = async (branch: string, create: boolean) => {
    setBusy(true);
    setErr(null);
    try {
      await post(`/dev/workspaces/${dev.id}/branch`, { name: branch, create });
      onDone(create ? `Created branch ${branch} from ${data?.current ?? "the current branch"} and switched to it.` : `Switched to ${branch}.`);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const shown = (data?.branches ?? []).filter((b) => b !== data?.current && (!filter || b.toLowerCase().includes(filter.toLowerCase())));
  return (
    <div className="branch-menu" data-testid="dev-branch-menu" onClick={(e) => e.stopPropagation()}>
      <div className="muted small">On <b className="mono">{data?.current ?? dev.branch}</b>{data?.base_ref ? <> · compared with <span className="mono">{data.base_ref}</span></> : null}</div>
      <form className="row nowrap" onSubmit={(e) => { e.preventDefault(); if (name.trim()) void go(name.trim(), true); }}>
        <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="new branch, e.g. feature/x" className="mono" data-testid="dev-branch-new" />
        <button className="btn small primary" disabled={!name.trim() || busy} data-testid="dev-branch-create">Create</button>
      </form>
      <div className="muted small">Created from the current branch; uncommitted changes go along.</div>
      {(data?.branches.length ?? 0) > 1 && <>
        <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="switch to…" className="mono" />
        <div className="branch-list">
          {shown.slice(0, 200).map((b) => (
            <button key={b} className="list-item small mono" disabled={busy} onClick={() => void go(b, false)} data-testid="dev-branch-switch">{b}</button>
          ))}
        </div>
      </>}
      {err && <div className="error-text small" role="alert" data-testid="dev-branch-error">{err}</div>}
      <div className="row"><span className="spacer" /><button className="btn small" onClick={onClose}>Close</button></div>
    </div>
  );
}
