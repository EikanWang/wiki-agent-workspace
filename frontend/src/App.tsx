import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AgentSession,
  ApiError,
  Bootstrap,
  del,
  OpenDocument,
  RecentItem,
  ReviewMark,
  SessionList,
  TerminalInfo,
  Thread,
  WorkspaceInfo,
  get,
  getWorkspace,
  setWorkspace,
  newIdempotencyKey,
  post,
  put,
  setCsrf,
  subscribeGlobal,
} from "./api";
import Login from "./components/Login";
import FileTree from "./components/FileTree";
import Reader from "./components/Reader";
import DiscussionPanel, { LiveJob, SessionChip, useJobStream } from "./components/DiscussionPanel";
import SessionPicker from "./components/SessionPicker";
import TerminalPanel from "./components/TerminalPanel";
import PreviewDialog from "./components/PreviewDialog";
import RecentList from "./components/RecentList";
import DiffReview from "./components/DiffReview";
import AddFolderDialog from "./components/AddFolderDialog";
import { SelectionAnchor } from "./textmodel";
import { FONT_STACKS, FontSettings, Prefs, Resizer, loadPrefs, savePrefs } from "./components/Layout";

type View = "document" | "terminal" | "diff";

export default function App() {
  const [boot, setBoot] = useState<Bootstrap | null>(null);
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [view, setView] = useState<View>("document");
  const [doc, setDoc] = useState<OpenDocument | null>(null);
  const [docError, setDocError] = useState<string | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [marks, setMarks] = useState<ReviewMark[]>([]);
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [activeThread, setActiveThread] = useState<string | null>(null);
  const [terminals, setTerminals] = useState<TerminalInfo[]>([]);
  const [activeTerminal, setActiveTerminal] = useState<string | null>(() => localStorage.getItem("waw.activeTerminal"));
  const [treeKey, setTreeKey] = useState(0);
  const [changed, setChanged] = useState<{ sha256?: string; missing?: boolean } | null>(null);
  const [liveJobId, setLiveJobId] = useState<string | null>(null);
  const [showSessions, setShowSessions] = useState(false);
  const [showPreview, setShowPreview] = useState(false);
  const [reanchorArmed, setReanchorArmed] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [mobilePane, setMobilePane] = useState<"tree" | "doc" | "chat">("doc");
  const [prefs, setPrefs] = useState<Prefs>(() => loadPrefs());
  const [showFonts, setShowFonts] = useState(false);
  const updatePrefs = useCallback((patch: Partial<Prefs>) => {
    setPrefs((p) => {
      const next = { ...p, ...patch };
      savePrefs(next);
      return next;
    });
  }, []);
  const isNarrow = useMemo(() => typeof window !== "undefined" && window.matchMedia("(max-width: 900px)").matches, []);
  const panelOpen = prefs.panelOpen || isNarrow; // phones always use the full panel via the bottom navigation
  const sidebarOpen = prefs.sidebarOpen;
  const setSidebarOpen = (fn: (v: boolean) => boolean) => updatePrefs({ sidebarOpen: fn(prefs.sidebarOpen) });
  const resizeSidebar = useCallback((dx: number) => setPrefs((p) => ({ ...p, sidebarWidth: Math.min(600, Math.max(160, p.sidebarWidth + dx)) })), []);
  const resizePanel = useCallback((dx: number) => setPrefs((p) => ({ ...p, panelWidth: Math.min(900, Math.max(260, p.panelWidth - dx)) })), []);
  const persistPrefs = useCallback(() => setPrefs((p) => { savePrefs(p); return p; }), []);
  const [watcherReady, setWatcherReady] = useState(true);
  const [pendingFragment, setPendingFragment] = useState<string | null>(null);
  const [showRecent, setShowRecent] = useState(false);
  const [recentKey, setRecentKey] = useState(0);
  const restoreThreadRef = useRef<string | null>(null);
  const [diffTarget, setDiffTarget] = useState<{ path: string; isDir: boolean } | null>(null);
  // multi-root window: the folder (workspace) everything is currently scoped to
  const [workspaces, setWorkspaces] = useState<WorkspaceInfo[]>([]);
  const [activeWs, setActiveWs] = useState<string | null>(() => {
    const id = localStorage.getItem("waw.activeWorkspace");
    setWorkspace(id);
    return id;
  });
  const [showAddFolder, setShowAddFolder] = useState(false);
  // unsaved edits guard + suppress the "changed on disk" banner for our own saves
  const editorDirty = useRef(false);
  const ownSaves = useRef<Set<string>>(new Set());
  const confirmLeaveEditor = () => !editorDirty.current || window.confirm("You have unsaved changes in the editor. Discard them?");
  const openDiff = useCallback((path: string, isDir: boolean) => {
    setDiffTarget({ path, isDir });
    setView("diff");
  }, []);
  useEffect(() => {
    // the Diff tab follows the document being read; an explicitly chosen folder is kept
    if (doc && diffTarget && !diffTarget.isDir && diffTarget.path !== doc.path) setDiffTarget({ path: doc.path, isDir: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc?.path]);
  const pendingSelection = useRef<SelectionAnchor | null>(null);

  const notify = useCallback((m: string) => {
    setToast(m);
    window.setTimeout(() => setToast(null), 5000);
  }, []);

  // ---------------------------------------------------------------- bootstrap
  const loadBootstrap = useCallback(async () => {
    try {
      let b: Bootstrap;
      try {
        b = await get<Bootstrap>("/bootstrap");
      } catch (e) {
        const code = (e as ApiError).code;
        if (code !== "WORKSPACE_NOT_FOUND" && code !== "WORKSPACE_NOT_OPEN") throw e;
        // the remembered folder was removed from the window: fall back to the primary folder
        localStorage.removeItem("waw.activeWorkspace");
        setWorkspace(null);
        b = await get<Bootstrap>("/bootstrap");
      }
      if (b.csrf_token) setCsrf(b.csrf_token);
      setWorkspaces(b.workspaces ?? []);
      setWorkspace(b.workspace.id);
      setActiveWs(b.workspace.id);
      setBoot(b);
      setTerminals(b.terminals);
      setWatcherReady(!b.watcher || b.watcher.ready || !!b.watcher.error);
      setAuthed(true);
    } catch (e) {
      if ((e as ApiError).status === 401) setAuthed(false);
      else notify((e as ApiError).message);
    }
  }, [notify]);

  useEffect(() => {
    void loadBootstrap();
  }, [loadBootstrap]);

  const refreshSessions = useCallback(async () => {
    try {
      const r = await get<SessionList>("/agent-sessions");
      setSessions(r.sessions);
    } catch {
      /* non-fatal */
    }
  }, []);

  const refreshWorkspaces = useCallback(async () => {
    try {
      const r = await get<{ workspaces: WorkspaceInfo[] }>("/workspaces");
      setWorkspaces(r.workspaces);
    } catch {
      /* non-fatal */
    }
  }, []);

  const refreshTerminals = useCallback(async () => {
    const r = await get<{ terminals: TerminalInfo[] }>("/terminals");
    setTerminals(r.terminals);
  }, []);

  useEffect(() => {
    if (authed) void refreshSessions();
  }, [authed, refreshSessions]);

  useEffect(() => {
    if (activeTerminal) localStorage.setItem("waw.activeTerminal", activeTerminal);
  }, [activeTerminal]);
  useEffect(() => {
    if (terminals.length && (!activeTerminal || !terminals.some((t) => t.id === activeTerminal))) setActiveTerminal(terminals[0].id);
  }, [terminals, activeTerminal]);

  // ---------------------------------------------------------------- documents
  const refreshThreads = useCallback(
    async (docId: string) => {
      try {
        const [t, m] = await Promise.all([
          get<{ threads: Thread[] }>(`/documents/${docId}/threads`),
          get<{ marks: ReviewMark[] }>(`/documents/${docId}/review-marks`),
        ]);
        setThreads(t.threads);
        setMarks(m.marks);
      } catch (e) {
        if ((e as ApiError).code === "DOCUMENT_MISSING") setChanged({ missing: true });
      }
    },
    [],
  );

  const openDocument = useCallback(
    async (path: string, keepThread = false) => {
      if (!keepThread && !confirmLeaveEditor()) return;
      if (!keepThread) editorDirty.current = false;
      setDocError(null);
      try {
        const d = await get<OpenDocument>(`/documents/open?path=${encodeURIComponent(path)}`);
        setDoc(d);
        setChanged(null);
        if (!keepThread) {
          // restore the discussion that was open the last time this document was read
          const wanted = restoreThreadRef.current ?? d.reading_state?.active_thread_id ?? null;
          restoreThreadRef.current = null;
          setActiveThread(wanted);
        }
        localStorage.setItem(`waw.lastPath.${getWorkspace() ?? ""}`, path);
        await refreshThreads(d.document_id);
        setView("document");
        setMobilePane("doc");
      } catch (e) {
        const ae = e as ApiError;
        setDocError(`${ae.code}: ${ae.message}`);
      }
    },
    [refreshThreads],
  );

  useEffect(() => {
    if (authed && !doc && activeWs) {
      const last = localStorage.getItem(`waw.lastPath.${activeWs}`) ?? localStorage.getItem("waw.lastPath");
      if (last) void openDocument(last);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authed, activeWs && boot ? "ready" : "wait"]);

  /** Scope the whole window to another root folder (file tree selection, Recent, diff, locks…). */
  const switchWorkspace = useCallback(
    (id: string, openLast = true) => {
      if (id === getWorkspace()) return;
      if (!confirmLeaveEditor()) return;
      editorDirty.current = false;
      setWorkspace(id);
      setActiveWs(id);
      localStorage.setItem("waw.activeWorkspace", id);
      setDoc(null);
      setThreads([]);
      setMarks([]);
      setActiveThread(null);
      setDiffTarget(null);
      setView((v) => (v === "terminal" ? v : "document"));
      void refreshSessions();
      if (openLast) {
        const last = localStorage.getItem(`waw.lastPath.${id}`);
        if (last) void openDocument(last);
      }
    },
    [openDocument, refreshSessions],
  );

  // ---------------------------------------------------------------- global events
  useEffect(() => {
    if (!authed) return;
    return subscribeGlobal((ev) => {
      if (ev.type === "tree_changed") setTreeKey((k) => k + 1);
      if (ev.type === "watcher_ready") setWatcherReady(true);
      if (ev.type === "workspaces_changed") {
        void refreshWorkspaces();
        setTreeKey((k) => k + 1);
      }
      const sameRoot = !ev.workspace_id || ev.workspace_id === getWorkspace();
      if (ev.type === "file_changed" && sameRoot && doc && (ev.document_id === doc.document_id || ev.path === doc.path)) {
        if (ev.missing) setChanged({ missing: true });
        else if (ev.sha256 && ownSaves.current.has(ev.sha256)) {
          /* our own save from the editor: not an external change */
        } else if (ev.sha256 && ev.sha256 !== doc.sha256) setChanged({ sha256: ev.sha256 });
        else if (!ev.sha256) setChanged({});
        void refreshThreads(doc.document_id);
      }
      if (ev.type === "terminal_state") void refreshTerminals();
      if (ev.type === "job_event" && ev.status === "completed" && doc) {
        setTreeKey((k) => k + 1);
      }
    });
  }, [authed, doc, refreshThreads, refreshTerminals, refreshWorkspaces]);

  // ---------------------------------------------------------------- jobs
  const onJobFinished = useCallback(
    (job: LiveJob) => {
      if (doc) void refreshThreads(doc.document_id);
      void refreshSessions();
      if (job.status === "failed" && job.error) notify(`Agent failed: ${job.error.code}`);
    },
    [doc, refreshThreads, refreshSessions, notify],
  );
  const liveJob = useJobStream(liveJobId, onJobFinished);

  // resume streaming for a running message when a thread is opened (e.g. after reload)
  useEffect(() => {
    const t = threads.find((x) => x.id === activeThread);
    const running = t?.messages.find((m) => m.role === "agent" && ["queued", "running", "waiting_provider_permission"].includes(m.status) && m.job_id);
    if (running && running.job_id && running.job_id !== liveJobId) setLiveJobId(running.job_id);
  }, [threads, activeThread, liveJobId]);

  const createThread = useCallback(
    async (anchor: SelectionAnchor, question: string, sessionChoice: string | null) => {
      if (!doc) return;
      const r = await post<{ thread_id: string; job_id: string }>(
        `/documents/${doc.document_id}/threads`,
        { ...anchor, version_sha256: doc.sha256, question, session_id: sessionChoice ?? undefined },
        { idempotencyKey: newIdempotencyKey() },
      );
      pendingSelection.current = null;
      await refreshThreads(doc.document_id);
      const d = await get<OpenDocument>(`/documents/open?path=${encodeURIComponent(doc.path)}`);
      setDoc((cur) => (cur ? { ...cur, default_session_id: d.default_session_id } : cur));
      void refreshSessions();
      setActiveThread(r.thread_id);
      setLiveJobId(r.job_id);
      setMobilePane("chat");
    },
    [doc, refreshThreads, refreshSessions],
  );

  const reply = useCallback(
    async (threadId: string, text: string, sessionChoice: string | null) => {
      const r = await post<{ job_id: string }>(`/threads/${threadId}/messages`, { text, session_id: sessionChoice ?? undefined }, { idempotencyKey: newIdempotencyKey() });
      if (doc) await refreshThreads(doc.document_id);
      void refreshSessions();
      setLiveJobId(r.job_id);
    },
    [doc, refreshThreads, refreshSessions],
  );

  const retry = useCallback(
    async (threadId: string, messageId: string, sessionChoice: string | null) => {
      try {
        const r = await post<{ job_id: string }>(`/threads/${threadId}/retry`, { message_id: messageId, session_id: sessionChoice ?? undefined });
        if (doc) await refreshThreads(doc.document_id);
        setLiveJobId(r.job_id);
      } catch (e) {
        notify((e as ApiError).message);
      }
    },
    [doc, refreshThreads, notify],
  );

  const cancel = useCallback(
    async (jobId: string) => {
      try {
        await post(`/jobs/${jobId}/cancel`);
      } catch (e) {
        notify((e as ApiError).message);
      }
    },
    [notify],
  );

  const permission = useCallback(
    async (jobId: string, requestId: string, decision: string) => {
      try {
        await post(`/jobs/${jobId}/permission-decisions`, { request_id: requestId, decision });
      } catch (e) {
        notify((e as ApiError).message);
      }
    },
    [notify],
  );

  const markReviewed = useCallback(
    async (anchor: SelectionAnchor) => {
      if (!doc) return;
      try {
        await post(`/documents/${doc.document_id}/review-marks`, { version_sha256: doc.sha256, anchor });
        await refreshThreads(doc.document_id);
        notify("Marked as reviewed.");
      } catch (e) {
        const ae = e as ApiError;
        notify(ae.code === "DOCUMENT_VERSION_CONFLICT" ? "The document changed; refresh before marking." : ae.message);
      }
    },
    [doc, refreshThreads, notify],
  );

  const reanchor = useCallback(
    async (anchor: SelectionAnchor) => {
      if (!doc || !activeThread) return;
      try {
        await put(`/threads/${activeThread}/anchor`, { ...anchor, version_sha256: doc.sha256 });
        setReanchorArmed(false);
        await refreshThreads(doc.document_id);
        notify("Discussion reanchored.");
      } catch (e) {
        notify((e as ApiError).message);
      }
    },
    [doc, activeThread, refreshThreads, notify],
  );

  const refreshDoc = useCallback(() => {
    if (doc) void openDocument(doc.path, true);
  }, [doc, openDocument]);

  const toggleLock = useCallback(
    async (path: string, locked: boolean, ws: string | null = getWorkspace()) => {
      try {
        if (locked) {
          const reason = window.prompt(`Lock ${path || "the whole folder"}? The app and app-driven agent turns will not modify it (your terminal is unrestricted). Optional reason:`, "");
          if (reason === null) return;
          await post("/locks", { path, reason: reason || null }, { workspace: ws });
          notify(`Locked ${path || "folder"}.`);
        } else {
          await del(`/locks?path=${encodeURIComponent(path)}`, { workspace: ws });
          notify(`Unlocked ${path || "folder"}.`);
        }
        setTreeKey((k) => k + 1);
        if (ws === getWorkspace() && doc && (doc.path === path || (path === "" || doc.path.startsWith(path + "/")))) void openDocument(doc.path, true);
      } catch (e) {
        notify((e as ApiError).message);
      }
    },
    [doc, notify, openDocument],
  );

  const logout = async () => {
    try {
      await post("/auth/logout");
    } finally {
      setCsrf(null);
      setAuthed(false);
      setBoot(null);
      setDoc(null);
    }
  };

  const activeThreadObj = useMemo(() => threads.find((t) => t.id === activeThread) ?? null, [threads, activeThread]);

  // Remember which discussion is open so "Recent" can restore it later (server-side, per document).
  const lastSavedThread = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (!doc) return;
    if (lastSavedThread.current === activeThread) return;
    lastSavedThread.current = activeThread;
    void put(`/documents/${doc.document_id}/reading-state`, { active_thread_id: activeThread }).catch(() => undefined);
  }, [doc, activeThread]);
  useEffect(() => {
    lastSavedThread.current = undefined;
  }, [doc?.document_id]);

  const openRecent = useCallback(
    (item: RecentItem) => {
      setShowRecent(false);
      if (item.workspace_id && item.workspace_id !== getWorkspace()) switchWorkspace(item.workspace_id, false);
      restoreThreadRef.current = item.last_thread_id;
      void openDocument(item.path);
      if (item.last_thread_id) setMobilePane("chat");
    },
    [openDocument, switchWorkspace],
  );
  const activeWsInfo = useMemo(() => workspaces.find((w) => w.id === activeWs) ?? null, [workspaces, activeWs]);
  const removeFolder = useCallback(
    async (w: WorkspaceInfo) => {
      if (!window.confirm(`Remove "${w.name}" (${w.root}) from this window? Its discussions, sessions and history are kept and come back if you add the folder again.`)) return;
      try {
        await del(`/workspaces/${w.id}`);
        if (w.id === getWorkspace()) {
          const primary = workspaces.find((x) => x.primary) ?? workspaces.find((x) => x.id !== w.id);
          if (primary) switchWorkspace(primary.id);
        }
        await refreshWorkspaces();
        notify(`Removed ${w.name} from the window.`);
      } catch (e) {
        notify((e as ApiError).message);
      }
    },
    [workspaces, switchWorkspace, refreshWorkspaces, notify],
  );
  const defaultSession = useMemo(() => sessions.find((s) => s.id === doc?.default_session_id), [sessions, doc?.default_session_id]);

  if (authed === null) return <div className="loading-screen">Loading…</div>;
  if (!authed) return <Login onDone={() => void loadBootstrap()} />;
  if (!boot) return <div className="loading-screen">Loading…</div>;

  const providerWarn = boot.default_provider_warning;

  return (
    <div
      className={`app ${sidebarOpen ? "" : "sidebar-collapsed"} ${panelOpen ? "" : "panel-collapsed"} pane-${mobilePane}`}
      style={{
        ["--reader-font" as string]: FONT_STACKS[prefs.readerFont],
        ["--reader-size" as string]: `${prefs.readerSize}px`,
        ["--reader-width" as string]: prefs.readerWidth ? `${prefs.readerWidth}px` : "none",
        ["--sidebar-width" as string]: sidebarOpen ? `${prefs.sidebarWidth}px` : "0px",
        ["--panel-width" as string]: panelOpen ? `${prefs.panelWidth}px` : "36px",
      }}
    >
      <header className="topbar">
        <button
          className={`btn small toggle ${sidebarOpen ? "on" : ""}`}
          onClick={() => setSidebarOpen((s) => !s)}
          title={sidebarOpen ? "Hide the file tree" : "Show the file tree"}
          aria-pressed={sidebarOpen}
          data-testid="toggle-sidebar"
          data-state={sidebarOpen ? "open" : "closed"}
        >
          <span className="toggle-glyph" aria-hidden="true">{sidebarOpen ? "◧" : "◨"}</span> Files
        </button>
        <span className="brand-mark">W</span>
        <span className="brand-title">Wiki Reader</span>
        {workspaces.filter((w) => w.available).length > 1 ? (
          <select className="ws-switch hide-mobile" value={activeWs ?? ""} onChange={(e) => switchWorkspace(e.target.value)} title="Folder (wiki) the window is working in" data-testid="workspace-switch">
            {workspaces.filter((w) => w.available).map((w) => (
              <option key={w.id} value={w.id}>{w.name}{w.git?.branch ? ` · ${w.git.branch}` : ""}</option>
            ))}
          </select>
        ) : (
          <span className="muted small hide-mobile" title={activeWsInfo?.root ?? boot.workspace.root}>{activeWsInfo?.name ?? boot.workspace.name}{(activeWsInfo?.git ?? boot.workspace.git)?.branch ? ` · ${(activeWsInfo?.git ?? boot.workspace.git)!.branch}` : ""}</span>
        )}
        <span className="spacer" />
        {doc && (
          <div className="session-area" data-testid="session-area">
            <span className="muted small hide-mobile">Document session</span>
            <button className="btn small session-btn" onClick={() => setShowSessions(true)} data-testid="session-button" title="Choose or link the session new discussions use">
              {defaultSession ? <SessionChip session={defaultSession} /> : <>No session bound · choose…</>}
            </button>
            <button className="btn small" onClick={() => setShowSessions(true)} data-testid="new-session-button">+ New session</button>
            <button className="btn small" onClick={() => setShowPreview(true)} title="Ask the agent for a change in an isolated copy and review the diff before applying" disabled={!!doc.lock}>Preview change</button>
            <button className={`btn small toggle ${doc.lock ? "on" : ""}`} onClick={() => void toggleLock(doc.lock && doc.lock.path !== doc.path ? doc.lock.path : doc.path, !doc.lock)} title={doc.lock ? `Locked${doc.lock.path !== doc.path ? ` by folder ${doc.lock.path || "(workspace)"}` : ""} — click to unlock` : "Lock this document (read-only for the app and app-driven agents)"} data-testid="doc-lock-toggle" aria-pressed={!!doc.lock}>
              {doc.lock ? "🔒 Locked" : "🔓 Lock"}
            </button>
          </div>
        )}
        <nav className="view-tabs">
          <button className={view === "document" ? "active" : ""} onClick={() => setView("document")} data-testid="view-document">Document</button>
          <button className={view === "terminal" ? "active" : ""} onClick={() => setView("terminal")} data-testid="view-terminal">Terminal</button>
          <button
            className={view === "diff" ? "active" : ""}
            onClick={() => (doc ? openDiff(doc.path, false) : openDiff("", true))}
            data-testid="view-diff"
            title={doc ? `Review uncommitted changes of ${doc.path} (use ± in the file tree for a folder)` : "Review uncommitted changes of the workspace"}
          >
            Diff
          </button>
        </nav>
        <button className="btn small" onClick={() => { setRecentKey((k) => k + 1); setShowRecent(true); }} title="Documents and discussions you worked on, newest first" data-testid="recent-button">Recent</button>
        <span className="font-settings-anchor">
          <button className="btn small" onClick={() => setShowFonts((v) => !v)} title="Font style and size" data-testid="font-button">Aa</button>
          {showFonts && <FontSettings prefs={prefs} onChange={updatePrefs} onClose={() => setShowFonts(false)} />}
        </span>
        <a className="btn small hide-mobile" href="/api/v1/export" download title="Download a backup of the SQLite database and document snapshots">Backup</a>
        <button className="btn small" onClick={() => void logout()} title="Sign out">Sign out</button>
      </header>

      {providerWarn && <div className="banner warn">{providerWarn}</div>}
      {boot.settings.watcher_error && <div className="banner warn">{boot.settings.watcher_error}</div>}
      {!watcherReady && <div className="banner">The file watcher is still indexing this workspace; until it finishes, use the ↻ button in the file tree to see new files and changes.</div>}

      <main className={`layout view-${view === "diff" ? "terminal" : view} ${view === "diff" ? "view-diff" : ""}`}>
        <aside className="sidebar">
          <FileTree
            workspaces={workspaces.length ? workspaces : [{ id: boot.workspace.id, name: boot.workspace.name, root: boot.workspace.root, primary: true, configured: true, available: true, git: boot.workspace.git, watcher: null }]}
            activeWorkspace={activeWs}
            selectedPath={doc?.path ?? null}
            onOpen={(ws, p) => {
              if (ws !== getWorkspace()) switchWorkspace(ws, false);
              void openDocument(p);
            }}
            onOpenDiff={(ws, p, isDir) => {
              if (ws !== getWorkspace()) switchWorkspace(ws, false);
              openDiff(p, isDir);
            }}
            onToggleLock={(ws, p, l) => void toggleLock(p, l, ws)}
            onAddFolder={() => setShowAddFolder(true)}
            onRemoveFolder={(w) => void removeFolder(w)}
            refreshKey={treeKey}
          />
          {sidebarOpen && <Resizer side="right" onResize={resizeSidebar} onEnd={persistPrefs} />}
        </aside>

        <section className="center" style={{ display: view === "document" ? undefined : "none" }}>
          {docError && <div className="banner warn">{docError}</div>}
          {doc ? (
            <Reader
              doc={doc}
              threads={threads}
              marks={marks}
              activeThreadId={activeThread}
              changedNotice={changed}
              onRefresh={refreshDoc}
              onRelink={async () => {
                const p = window.prompt("Relink this document (and its discussions) to another file in the workspace. Relative path:", doc.path);
                if (!p) return;
                try {
                  const r = await post<{ path: string }>(`/documents/${doc.document_id}/relink`, { path: p });
                  await openDocument(r.path, true);
                  notify(`Relinked to ${r.path}.`);
                } catch (e) {
                  notify((e as ApiError).message);
                }
              }}
              onOpenThread={(id) => {
                setActiveThread(id);
                setMobilePane("chat");
              }}
              onSend={createThread}
              onMarkReviewed={(a) => void markReviewed(a)}
              onReanchor={reanchorArmed ? (a) => void reanchor(a) : null}
              onOpenInternalLink={(p, frag) => {
                setPendingFragment(frag);
                if (p === doc.path) return; // same document: Reader scrolls via initialFragment
                void openDocument(p).catch(() => setPendingFragment(null));
              }}
              initialFragment={pendingFragment}
              onFragmentConsumed={() => setPendingFragment(null)}
              pendingSelectionRef={pendingSelection}
              tocOpen={prefs.tocOpen}
              onToggleToc={() => updatePrefs({ tocOpen: !prefs.tocOpen })}
              readerMode={prefs.readerMode}
              onReaderModeChange={(m) => updatePrefs({ readerMode: m })}
              onEdited={(sha) => {
                ownSaves.current.add(sha);
                notify("Saved.");
                void openDocument(doc.path, true);
              }}
              onDirtyChange={(d) => {
                editorDirty.current = d;
              }}
              sessions={sessions}
              defaultSessionId={doc.default_session_id}
              defaultProvider={boot.default_provider}
            />
          ) : (
            <div className="empty">
              <h2>Open a Markdown file</h2>
              <p className="muted">Pick a document from the file tree. Files appear as soon as they exist in the working tree — no commit or push required.</p>
              <h3>Recent work</h3>
              <RecentList onOpen={openRecent} refreshKey={recentKey} onNotify={notify} />
            </div>
          )}
        </section>

        <section className="center" style={{ display: view === "diff" ? undefined : "none" }}>
          {view === "diff" && diffTarget && (
            <DiffReview
              path={diffTarget.path}
              isDir={diffTarget.isDir}
              sessions={sessions}
              defaultProvider={boot.default_provider}
              refreshKey={treeKey}
              onOpenDocument={(p) => void openDocument(p)}
              onOpenThread={(p, tid) => {
                restoreThreadRef.current = tid;
                void openDocument(p);
              }}
              onClose={() => setView("document")}
              onNotify={notify}
            />
          )}
        </section>

        <section className="center terminal-center" style={{ display: view === "terminal" ? undefined : "none" }}>
          <TerminalPanel terminals={terminals} info={boot.terminal_info} activeId={activeTerminal} onActive={setActiveTerminal} onChanged={refreshTerminals} visible={view === "terminal"} workspaceRoot={activeWsInfo?.root ?? boot.workspace.root} fontSize={prefs.terminalSize} />
        </section>

        {view === "document" && doc && !panelOpen && (
          <aside className="discussion collapsed">
            <button className="collapsed-tab" onClick={() => updatePrefs({ panelOpen: true })} title="Show discussions" data-testid="panel-expand">
              ‹ Discussions{threads.length ? ` (${threads.length})` : ""}
            </button>
          </aside>
        )}
        {view === "document" && doc && panelOpen && (
          <DiscussionPanel
            onCollapse={() => updatePrefs({ panelOpen: false })}
            resizer={<Resizer side="left" onResize={resizePanel} onEnd={persistPrefs} />}
            mode={prefs.panelMode}
            onModeChange={(m) => updatePrefs({ panelMode: m })}
            onSessionChanged={() => void refreshSessions()}
            thread={activeThreadObj}
            threads={threads}
            sessions={sessions}
            defaultSessionId={doc.default_session_id}
            liveJob={liveJob}
            onReply={reply}
            onRetry={retry}
            onCancel={cancel}
            onPermission={permission}
            onSelectThread={(id) => {
              setActiveThread(id);
              setReanchorArmed(false);
              if (id === null) setMobilePane("doc");
            }}
            docId={doc.document_id}
            onRequestReanchor={() => {
              setReanchorArmed((a) => !a);
              setMobilePane("doc");
            }}
            reanchorArmed={reanchorArmed}
          />
        )}
      </main>

      <nav className="mobile-nav">
        <button className={mobilePane === "tree" ? "active" : ""} onClick={() => { setView("document"); setMobilePane("tree"); }}>Files</button>
        <button className={mobilePane === "doc" && view === "document" ? "active" : ""} onClick={() => { setView("document"); setMobilePane("doc"); }}>Document</button>
        <button className={mobilePane === "chat" ? "active" : ""} onClick={() => { setView("document"); setMobilePane("chat"); }}>Discussion</button>
        <button className={view === "terminal" ? "active" : ""} onClick={() => setView("terminal")}>Terminal</button>
      </nav>

      {showAddFolder && (
        <AddFolderDialog
          start={(activeWsInfo?.root ?? boot.workspace.root).split("/").slice(0, -1).join("/") || "/"}
          onClose={() => setShowAddFolder(false)}
          onAdded={async (w) => {
            setShowAddFolder(false);
            await refreshWorkspaces();
            switchWorkspace(w.id);
            notify(`Added ${w.name} (${w.root}).`);
          }}
        />
      )}
      {showRecent && (
        <div className="modal-backdrop" onClick={() => setShowRecent(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} data-testid="recent-dialog">
            <div className="modal-head">
              <span>Recent work</span>
              <button className="icon-btn" onClick={() => setShowRecent(false)}>×</button>
            </div>
            <div className="muted small">Newest activity first. Opening an entry restores the document, your reading position and the discussion you had open.</div>
            <RecentList onOpen={openRecent} refreshKey={recentKey} onNotify={notify} />
          </div>
        </div>
      )}
      {showSessions && doc && (
        <SessionPicker
          docId={doc.document_id}
          defaultSessionId={doc.default_session_id}
          sessions={sessions}
          defaultProvider={boot.default_provider}
          onChanged={async () => {
            await refreshSessions();
            const d = await get<OpenDocument>(`/documents/open?path=${encodeURIComponent(doc.path)}`);
            setDoc((cur) => (cur ? { ...cur, default_session_id: d.default_session_id } : cur));
          }}
          onClose={() => setShowSessions(false)}
        />
      )}
      {showPreview && doc && (
        <PreviewDialog
          docId={doc.document_id}
          docPath={doc.path}
          baseSha={doc.sha256}
          sessions={sessions}
          defaultSessionId={doc.default_session_id}
          providers={boot.providers}
          onApplied={() => {
            notify("Preview applied. Reloading the document.");
            refreshDoc();
          }}
          onClose={() => setShowPreview(false)}
        />
      )}
      {toast && <div className="toast" role="status" data-testid="toast">{toast}</div>}
    </div>
  );
}
