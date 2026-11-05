import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AgentSession,
  ApiError,
  Bootstrap,
  OpenDocument,
  ReviewMark,
  SessionList,
  TerminalInfo,
  Thread,
  get,
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
import { SelectionAnchor } from "./textmodel";
import { FONT_STACKS, FontSettings, Prefs, Resizer, loadPrefs, savePrefs } from "./components/Layout";

type View = "document" | "terminal";

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
  const pendingSelection = useRef<SelectionAnchor | null>(null);

  const notify = useCallback((m: string) => {
    setToast(m);
    window.setTimeout(() => setToast(null), 5000);
  }, []);

  // ---------------------------------------------------------------- bootstrap
  const loadBootstrap = useCallback(async () => {
    try {
      const b = await get<Bootstrap>("/bootstrap");
      if (b.csrf_token) setCsrf(b.csrf_token);
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
      setDocError(null);
      try {
        const d = await get<OpenDocument>(`/documents/open?path=${encodeURIComponent(path)}`);
        setDoc(d);
        setChanged(null);
        if (!keepThread) setActiveThread(null);
        localStorage.setItem("waw.lastPath", path);
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
    if (authed && !doc) {
      const last = localStorage.getItem("waw.lastPath");
      if (last) void openDocument(last);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authed]);

  // ---------------------------------------------------------------- global events
  useEffect(() => {
    if (!authed) return;
    return subscribeGlobal((ev) => {
      if (ev.type === "tree_changed") setTreeKey((k) => k + 1);
      if (ev.type === "watcher_ready") setWatcherReady(true);
      if (ev.type === "file_changed" && doc && (ev.document_id === doc.document_id || ev.path === doc.path)) {
        if (ev.missing) setChanged({ missing: true });
        else if (ev.sha256 && ev.sha256 !== doc.sha256) setChanged({ sha256: ev.sha256 });
        else if (!ev.sha256) setChanged({});
        void refreshThreads(doc.document_id);
      }
      if (ev.type === "terminal_state") void refreshTerminals();
      if (ev.type === "job_event" && ev.status === "completed" && doc) {
        setTreeKey((k) => k + 1);
      }
    });
  }, [authed, doc, refreshThreads, refreshTerminals]);

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
        ["--reader-width" as string]: `${prefs.readerWidth}px`,
        ["--sidebar-width" as string]: sidebarOpen ? `${prefs.sidebarWidth}px` : "0px",
        ["--panel-width" as string]: panelOpen ? `${prefs.panelWidth}px` : "36px",
      }}
    >
      <header className="topbar">
        <button className="icon-btn" onClick={() => setSidebarOpen((s) => !s)} title={sidebarOpen ? "Hide file tree" : "Show file tree"} data-testid="toggle-sidebar">☰</button>
        <span className="brand-mark">W</span>
        <span className="brand-title">Wiki Reader</span>
        <span className="muted small hide-mobile" title={boot.workspace.root}>{boot.workspace.name}{boot.workspace.git.branch ? ` · ${boot.workspace.git.branch}` : ""}</span>
        <span className="spacer" />
        {doc && (
          <div className="session-area" data-testid="session-area">
            <span className="muted small hide-mobile">Document session</span>
            <button className="chip-btn" onClick={() => setShowSessions(true)} data-testid="session-button">
              <SessionChip session={defaultSession} />
            </button>
            <button className="btn small" onClick={() => setShowSessions(true)} data-testid="new-session-button">+ New session</button>
            <button className="btn small" onClick={() => setShowPreview(true)} title="Ask the agent for a change in an isolated copy and review the diff before applying">Preview change</button>
          </div>
        )}
        <nav className="view-tabs">
          <button className={view === "document" ? "active" : ""} onClick={() => setView("document")} data-testid="view-document">Document</button>
          <button className={view === "terminal" ? "active" : ""} onClick={() => setView("terminal")} data-testid="view-terminal">Terminal</button>
        </nav>
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

      <main className={`layout view-${view}`}>
        <aside className="sidebar">
          <FileTree workspaceName={boot.workspace.name} selectedPath={doc?.path ?? null} onOpen={(p) => void openDocument(p)} refreshKey={treeKey} />
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
              sessions={sessions}
              defaultSessionId={doc.default_session_id}
              defaultProvider={boot.default_provider}
            />
          ) : (
            <div className="empty">
              <h2>Open a Markdown file</h2>
              <p className="muted">Pick a document from the file tree. Files appear as soon as they exist in the working tree — no commit or push required.</p>
            </div>
          )}
        </section>

        <section className="center terminal-center" style={{ display: view === "terminal" ? undefined : "none" }}>
          <TerminalPanel terminals={terminals} info={boot.terminal_info} activeId={activeTerminal} onActive={setActiveTerminal} onChanged={refreshTerminals} visible={view === "terminal"} workspaceRoot={boot.workspace.root} fontSize={prefs.terminalSize} />
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
            docSha={doc.sha256}
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
