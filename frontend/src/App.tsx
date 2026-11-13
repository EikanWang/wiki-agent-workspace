import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import FileTree, { notifyTreeChanged } from "./components/FileTree";
import DocTabs, { DocTab, PR_TABS, loadTabs, sameTab, tabsKey, withTab } from "./components/DocTabs";
import PrView from "./components/PrView";
import { isChatPath } from "./components/ChatView";
import ChatsPage, { ChatTarget } from "./components/ChatsPage";
import { ConfirmHost, confirmAction } from "./components/Confirm";
import WorkspaceMenu from "./components/WorkspaceMenu";
import AccountMenu from "./components/AccountMenu";
import ReviewsPanel, { loadReviews, reviewBadge, useReviews } from "./components/ReviewsPanel";
import { CodeIcon, DashboardIcon, FilesIcon, InsightsIcon, PullRequestIcon, TerminalIcon, WikiIcon } from "./components/Icons";
import InsightsView, { InsightsPanel } from "./components/Insights";
import TerminalsPanel from "./components/TerminalsPanel";
import { useGhAutoRefresh } from "./ghRefresh";
import AgentMonitor, { AgentsPanel, Provider } from "./components/AgentMonitor";
import LocalPanel from "./components/LocalPanel";
const LocalEditor = lazy(() => import("./components/LocalEditor"));
import DevPanel from "./components/DevPanel";
import { ModelChoiceValue } from "./components/ModelChoice";
import DevView from "./components/DevView";
import { DevSetupPreset } from "./components/DevSetupDialog";
import Reader from "./components/Reader";
import DiscussionPanel, { LiveJob, SessionChip, useJobStream } from "./components/DiscussionPanel";
import SessionPicker from "./components/SessionPicker";
import TerminalPanel from "./components/TerminalPanel";
import PreviewDialog from "./components/PreviewDialog";
import RecentList from "./components/RecentList";
import DiffReview from "./components/DiffReview";
import AddFolderDialog from "./components/AddFolderDialog";
import { SelectionAnchor, wholeDocument } from "./textmodel";
import { FONT_STACKS, FontSettings, Prefs, Resizer, loadPrefs, savePrefs } from "./components/Layout";

const ACTIVITY_BAR_PX = 45;

type View = "document" | "terminal" | "diff" | "chats" | "development" | "local" | "agents" | "insights";

export default function App() {
  const [boot, setBoot] = useState<Bootstrap | null>(null);
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [view, setView] = useState<View>("document");
  const [doc, setDoc] = useState<OpenDocument | null>(null);
  // open documents as tabs (folder + path), remembered in this browser; `lastTab` = the tab read most recently
  // the named workspace (VS Code window) this page shows; tabs are remembered per named workspace
  const namedRef = useRef<string>(localStorage.getItem("waw.namedWorkspace") ?? "");
  const [docTabs, setDocTabs] = useState<DocTab[]>(() => loadTabs(namedRef.current));
  const [showNewWorkspace, setShowNewWorkspace] = useState(false);
  const [editNext, setEditNext] = useState<string | null>(null);
  // GitHub review hub: only for the server owner's GitHub sign-in
  const [hub, setHub] = useState<{ available: boolean; owner: string | null } | null>(null);
  const reviews = useReviews();
  // the pull request shown in the center (a tab like a document; its discussions live in a hidden PR document)
  const [prTab, setPrTab] = useState<{ repo: string; number: number } | null>(null);
  const prTabRef = useRef(prTab);
  prTabRef.current = prTab; // path of a just-created file to open in the editor
  const lastTab = useRef<(DocTab & { document_id: string }) | null>(null);
  const [docError, setDocError] = useState<string | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [marks, setMarks] = useState<ReviewMark[]>([]);
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [activeThread, setActiveThread] = useState<string | null>(null);
  const [terminals, setTerminals] = useState<TerminalInfo[]>([]);
  const [activeTerminal, setActiveTerminal] = useState<string | null>(() => localStorage.getItem("waw.activeTerminal"));
  const [treeKey, setTreeKey] = useState(0);
  const [diffTick, setDiffTick] = useState(0);
  const diffThrottle = useRef<number | undefined>(undefined);
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
  const listOpen = prefs.listOpen !== false;
  const resizeSidebar = useCallback((dx: number) => setPrefs((p) => ({ ...p, sidebarWidth: Math.min(600, Math.max(160, p.sidebarWidth + dx)) })), []);
  const resizePanel = useCallback((dx: number) => setPrefs((p) => ({ ...p, panelWidth: Math.min(900, Math.max(260, p.panelWidth - dx)) })), []);
  const persistPrefs = useCallback(() => setPrefs((p) => { savePrefs(p); return p; }), []);
  const [watcherReady, setWatcherReady] = useState(true);
  const [pendingFragment, setPendingFragment] = useState<string | null>(null);
  const [showRecent, setShowRecent] = useState(false);
  const [recentKey, setRecentKey] = useState(0);
  const restoreThreadRef = useRef<string | null>(null);
  const [diffTarget, setDiffTarget] = useState<{ path: string; isDir: boolean } | null>(null);
  const [chatTarget, setChatTarget] = useState<ChatTarget | null>(null);
  // PR Development: the development workspace shown in the center (its editor stays loaded while you look elsewhere)
  const [devId, setDevId] = useState<string | null>(() => localStorage.getItem("waw.devActive"));
  const [devPreset, setDevPreset] = useState<DevSetupPreset | null>(null);
  // Local files: any folder / file of the server, browsed and edited like a file manager
  const [localOpen, setLocalOpen] = useState<{ path: string; at: number } | null>(null);
  const [localRenamed, setLocalRenamed] = useState<{ from: string; to: string | null; at: number } | null>(null);
  const [localActive, setLocalActive] = useState<string | null>(null);
  const [localUsed, setLocalUsed] = useState(false);
  // Agents dashboard: the monitored sessions
  const [watched, setWatchedState] = useState<{ provider: Provider; id: string; label?: string }[]>([]);
  const [alarms, setAlarms] = useState(0);
  const [autoVersion, setAutoVersion] = useState(0);
  useEffect(() => {
    if (!authed) return;
    void get<{ sessions: { provider: Provider; id: string; label?: string }[] }>("/monitor/watched").then((r) => setWatchedState(r.sessions)).catch(() => undefined);
  }, [authed]);
  const setWatched = useCallback((w: { provider: Provider; id: string; label?: string }[]) => {
    setWatchedState(w);
    void put("/monitor/watched", { sessions: w }).catch(() => undefined);
  }, []);
  useEffect(() => {
    const base = document.title.replace(/^\(\d+\) /, "");
    document.title = alarms > 0 ? `(${alarms}) ${base}` : base;
  }, [alarms]);
  const openDev = useCallback((id: string | null) => {
    setDevId(id);
    if (id) localStorage.setItem("waw.devActive", id);
    else localStorage.removeItem("waw.devActive");
    setView(id ? "development" : "document");
  }, []);
  /** the Chats page: talk with a project (folder) directly; a chat document opens there, never as a tab */
  const openChats = useCallback((t: ChatTarget) => {
    setChatTarget(t);
    setView("chats");
    setMobilePane("doc");
  }, []);
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
      const named = b.named_workspaces?.current ?? "";
      if (named && named !== namedRef.current) {
        // first start, or switched from another browser: show that workspace's tabs
        const isDefault = !!b.named_workspaces?.workspaces.find((w) => w.id === named)?.is_default;
        namedRef.current = named;
        localStorage.setItem("waw.namedWorkspace", named);
        setDocTabs(loadTabs(named, isDefault));
      }
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
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    const r = p.get("github_setup");
    if (!r) return;
    notify({
      ok: "Continue with GitHub is ready: the login page now offers it (only your GitHub account is allowed).",
      state: "GitHub setup expired or did not start here; open the account menu and try again.",
      failed: "GitHub did not hand over the new app's keys; try the setup again.",
      unreachable: "The server could not reach GitHub to finish the setup (network or proxy); try again.",
    }[r] ?? `GitHub setup: ${r}`);
    p.delete("github_setup");
    window.history.replaceState(null, "", window.location.pathname + (p.toString() ? `?${p}` : "") + window.location.hash);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
    async (path: string, keepThread = false, quiet = false): Promise<boolean> => {
      if (isChatPath(path)) {
        openChats({ ws: getWorkspace() ?? "", path });
        return true;
      }
      if (!keepThread && !confirmLeaveEditor()) return false;
      if (!keepThread) editorDirty.current = false;
      setDocError(null);
      try {
        const d = await get<OpenDocument>(`/documents/open?path=${encodeURIComponent(path)}`);
        const tab = { ws: getWorkspace() ?? "", path: d.path };
        const isPr = d.path.startsWith(".waw-github/");
        const prev = lastTab.current;
        // relinked (same document, new path): the tab follows; otherwise a new tab opens after the current one
        const renamed = prev && prev.document_id === d.document_id && !sameTab(prev, tab) ? prev : null;
        if (!isPr) {
          setDocTabs((tabs) => withTab(tabs, tab, prev, renamed));
          lastTab.current = { ...tab, document_id: d.document_id };
          setPrTab(null);
        }
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
        return true;
      } catch (e) {
        const ae = e as ApiError;
        if (!quiet) setDocError(`${ae.code}: ${ae.message}`);
        return false;
      }
    },
    [refreshThreads],
  );

  /**
   * Reopen what you were reading in folder `ws`: its last document, else one of its open tabs. Never a path
   * remembered without a folder (old builds stored one; opening it in another folder gave FILE_NOT_FOUND).
   * Remembered documents that no longer exist are dropped quietly instead of showing an error.
   */
  const restoreLast = useCallback(
    async (ws: string) => {
      localStorage.removeItem("waw.lastPath");
      const candidates = [localStorage.getItem(`waw.lastPath.${ws}`), ...loadTabs(namedRef.current).filter((t) => t.ws === ws).map((t) => t.path)];
      const tried = new Set<string>();
      for (const p of candidates) {
        if (!p || tried.has(p)) continue;
        tried.add(p);
        if (getWorkspace() !== ws) return; // the user switched folders meanwhile
        if (await openDocument(p, false, true)) return;
        // gone (deleted, renamed, or never in this folder): forget it
        if (localStorage.getItem(`waw.lastPath.${ws}`) === p) localStorage.removeItem(`waw.lastPath.${ws}`);
        setDocTabs((tabs) => tabs.filter((t) => !(t.ws === ws && t.path === p)));
      }
    },
    [openDocument],
  );

  // What the window showed (PR tab, Terminal / Diff view) survives a reload or closing the browser; documents,
  // their reading position and discussion are restored from the server, tabs from localStorage.
  const uiKey = () => `waw.ui.${namedRef.current}`;
  const savedUi = useRef<{ view?: View; diff?: { path: string; isDir: boolean; ws: string | null } | null; pr?: { repo: string; number: number } | null; chat?: ChatTarget | null } | null>(null);
  if (savedUi.current === null) {
    try {
      savedUi.current = JSON.parse(localStorage.getItem(uiKey()) || "{}");
    } catch {
      savedUi.current = {};
    }
  }
  const [uiRestored, setUiRestored] = useState(false);
  useEffect(() => {
    if (!uiRestored) return; // do not overwrite the saved state before it was applied
    localStorage.setItem(uiKey(), JSON.stringify({ view, diff: diffTarget ? { ...diffTarget, ws: getWorkspace() } : null, pr: prTab, chat: chatTarget }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, diffTarget, prTab, chatTarget, uiRestored]);

  useEffect(() => {
    if (!(authed && activeWs && boot)) return;
    void (async () => {
      if (!doc) await restoreLast(activeWs);
      const ui = savedUi.current ?? {};
      if (prefs.sidebarView === "agents") setView("agents"); // the Agents activity always shows the agents' status
      else if (prefs.sidebarView === "insights") setView("insights");
      else if (prefs.sidebarView === "terminal") setView("terminal"); // the Terminal activity: the terminals
      else if (ui.view === "terminal") setView("terminal");
      else if (ui.view === "chats") openChats(ui.chat ?? { ws: getWorkspace() ?? "", path: null });
      else if (ui.view === "development" && localStorage.getItem("waw.devActive")) setView("development");
      else if (ui.view === "local") { setLocalUsed(true); setView("local"); }
      else if (ui.view === "agents") setView("agents");
      else if (ui.view === "diff" && ui.diff && (!ui.diff.ws || ui.diff.ws === getWorkspace())) openDiff(ui.diff.path, ui.diff.isDir);
      setUiRestored(true);
    })();
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
      if (openLast) void restoreLast(id);
    },
    [restoreLast, refreshSessions],
  );

  useEffect(() => {
    localStorage.setItem(tabsKey(namedRef.current), JSON.stringify(docTabs));
  }, [docTabs]);
  // tabs of folders that are no longer in the window go away (their data is kept server-side)
  useEffect(() => {
    if (!workspaces.length) return;
    setDocTabs((tabs) => {
      const keep = tabs.filter((t) => t.ws === PR_TABS || workspaces.some((w) => w.id === t.ws));
      return keep.length === tabs.length ? tabs : keep;
    });
  }, [workspaces]);

  const openPr = useCallback((repo: string, number: number, title?: string) => {
    if (!confirmLeaveEditor()) return;
    const tab = { ws: PR_TABS, path: `${repo}#${number}`, title };
    setDocTabs((tabs) => {
      const i = tabs.findIndex((t) => sameTab(t, tab));
      if (i >= 0) return title && tabs[i].title !== title ? tabs.map((t, j) => (j === i ? { ...t, title } : t)) : tabs;
      const after = lastTab.current ? tabs.findIndex((t) => sameTab(t, lastTab.current)) : -1;
      const out = [...tabs];
      out.splice(after >= 0 ? after + 1 : out.length, 0, tab);
      return out;
    });
    lastTab.current = { ...tab, document_id: "" };
    setPrTab({ repo, number });
    setView("document");
    setMobilePane("doc");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const activateDocTab = useCallback(
    (t: DocTab) => {
      if (t.ws === PR_TABS) {
        const m = t.path.match(/^(.+)#(\d+)$/);
        if (m) openPr(m[1], Number(m[2]), t.title);
        return;
      }
      if (t.ws !== getWorkspace()) switchWorkspace(t.ws, false);
      void openDocument(t.path);
    },
    [switchWorkspace, openDocument, openPr],
  );

  const closeDocTab = useCallback(
    (t: DocTab) => {
      const activeTab = prTab ? { ws: PR_TABS, path: `${prTab.repo}#${prTab.number}` } : doc ? { ws: activeWs ?? "", path: doc.path } : null;
      const isActive = sameTab(t, activeTab);
      if (isActive && !confirmLeaveEditor()) return;
      const idx = docTabs.findIndex((x) => sameTab(x, t));
      const rest = docTabs.filter((x) => !sameTab(x, t));
      setDocTabs(rest);
      // don't reopen a closed document at the next start
      if (localStorage.getItem(`waw.lastPath.${t.ws}`) === t.path) {
        const other = rest.find((x) => x.ws === t.ws);
        if (other) localStorage.setItem(`waw.lastPath.${t.ws}`, other.path);
        else localStorage.removeItem(`waw.lastPath.${t.ws}`);
      }
      if (!isActive) return;
      editorDirty.current = false;
      lastTab.current = null;
      setPrTab(null);
      const next = rest[idx] ?? rest[idx - 1];
      if (next) activateDocTab(next);
      else {
        setDoc(null);
        setThreads([]);
        setMarks([]);
        setActiveThread(null);
        setChanged(null);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [doc, activeWs, docTabs, activateDocTab, prTab],
  );

  useEffect(() => {
    const pr = savedUi.current?.pr;
    if (!uiRestored || !pr || !hub?.available) return;
    savedUi.current = { ...savedUi.current, pr: null };
    if (docTabs.some((t) => t.ws === PR_TABS && t.path === `${pr.repo}#${pr.number}`)) {
      const view0 = savedUi.current?.view;
      openPr(pr.repo, pr.number);
      if (view0 === "terminal") setView("terminal");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uiRestored, hub?.available]);
  useEffect(() => {
    if (!authed || !boot) return;
    void get<{ available: boolean; owner: string | null }>("/github/hub")
      .then((h) => {
        setHub(h);
        if (!h.available) return;
        void loadReviews(); // then at the interval chosen in Reviews (useGhAutoRefresh below)
      })
      .catch(() => setHub(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authed, !!boot]);
  const refreshReviews = useCallback(() => void loadReviews(true), []);
  useGhAutoRefresh("reviews", refreshReviews, !!hub?.available); // the Reviews list and its badge, at the chosen interval
  const sidebarView = (prefs.sidebarView === "reviews" && !hub?.available) ? "files" : prefs.sidebarView ?? "files";
  // the Terminal activity: its terminals page, without the wiki's Document / Terminal / Chats / Diff
  const terminalMode = sidebarView === "terminal" && view === "terminal";
  const [insightsRepo, setInsightsRepo] = useState<string | null>(() => localStorage.getItem("waw.insights.repo") || "pytorch/pytorch");
  useEffect(() => { if (insightsRepo) localStorage.setItem("waw.insights.repo", insightsRepo); }, [insightsRepo]);
  // Document / Terminal / Chats / Diff belong to the wiki: opening one (Recent, a link) while in Agents goes back to it
  useEffect(() => {
    if (uiRestored && (sidebarView === "agents" || sidebarView === "insights") && (view === "document" || view === "terminal" || view === "chats" || view === "diff")) updatePrefs({ sidebarView: "files" });
    if (uiRestored && sidebarView === "terminal" && (view === "document" || view === "chats" || view === "diff")) updatePrefs({ sidebarView: "files" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view]);

  // ---------------------------------------------------------------- global events
  useEffect(() => {
    if (!authed) return;
    return subscribeGlobal((ev) => {
      if (ev.type === "tree_changed") {
        // the tree reloads only the folders that changed; the diff view (whole-folder diff) at most every 2 s
        notifyTreeChanged({ ws: ev.workspace_id ?? null, dirs: Array.isArray(ev.dirs) ? ev.dirs : null });
        if (diffThrottle.current === undefined) {
          diffThrottle.current = window.setTimeout(() => {
            diffThrottle.current = undefined;
            setDiffTick((t) => t + 1);
          }, 2000);
        }
      }
      if (ev.type === "watcher_ready") setWatcherReady(true);
      if (ev.type === "named_workspace_changed" && ev.id && ev.id !== namedRef.current) {
        // another browser switched the server's window to another workspace: follow it
        notify("Another browser switched to a different workspace; reloading…");
        localStorage.setItem("waw.namedWorkspace", ev.id);
        window.setTimeout(() => window.location.reload(), 1200);
      }
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
    async (anchor: SelectionAnchor, question: string, sessionChoice: string | null, modelChoice?: ModelChoiceValue | null) => {
      if (!doc) return;
      const r = await post<{ thread_id: string; job_id: string }>(
        `/documents/${doc.document_id}/threads`,
        { ...anchor, version_sha256: doc.sha256, question, session_id: sessionChoice ?? undefined, model_choice: modelChoice ?? undefined },
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
      const pr = item.path.match(/^\.waw-github\/([^/]+)\/([^/]+)\/(\d+)\.md$/);
      if (pr) {
        restoreThreadRef.current = item.last_thread_id;
        openPr(`${pr[1]}/${pr[2]}`, Number(pr[3]));
        return;
      }
      if (item.workspace_id && item.workspace_id !== getWorkspace()) switchWorkspace(item.workspace_id, false);
      restoreThreadRef.current = item.last_thread_id;
      void openDocument(item.path);
      if (item.last_thread_id) setMobilePane("chat");
    },
    [openDocument, switchWorkspace, openPr],
  );
  const activeWsInfo = useMemo(() => workspaces.find((w) => w.id === activeWs) ?? null, [workspaces, activeWs]);
  const removeFolder = useCallback(
    async (w: WorkspaceInfo) => {
      const ok = await confirmAction({
        title: `Close the folder "${w.name}"?`,
        message: `${w.root}\n\nIt leaves this window and will not show again (also after a restart). Nothing is deleted on disk; its discussions, sessions and history are kept and come back if you add the folder again with +.`,
        confirmLabel: "Close folder",
      });
      if (!ok) return;
      try {
        await del(`/workspaces/${w.id}`);
        if (w.id === getWorkspace()) {
          const primary = workspaces.find((x) => x.primary) ?? workspaces.find((x) => x.id !== w.id);
          if (primary) switchWorkspace(primary.id);
        }
        await refreshWorkspaces();
        notify(`Closed ${w.name}.`);
      } catch (e) {
        notify((e as ApiError).message);
      }
    },
    [workspaces, switchWorkspace, refreshWorkspaces, notify],
  );
  // with a PR tab in front, the panel and the session bar belong to the PR's discussion document (once it is open)
  const isPrDoc = !!doc && doc.path.startsWith(".waw-github/");
  const panelDoc = prTab ? (isPrDoc && doc!.path.endsWith(`/${prTab.number}.md`) ? doc : null) : doc;
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
        // the activity bar (Files · PR Review · PR Development) comes on top of the tree's own width
        ["--sidebar-width" as string]: sidebarOpen ? `${(listOpen ? prefs.sidebarWidth : 0) + ACTIVITY_BAR_PX}px` : "0px",
        ["--panel-width" as string]: panelOpen ? `${prefs.panelWidth}px` : "36px",
      }}
    >
      <header className="topbar">
        <button
          className={`btn small toggle files-toggle ${sidebarOpen ? "on" : ""}`}
          onClick={() => setSidebarOpen((s) => !s)}
          title={sidebarOpen ? "Hide the sidebar (Wiki, Local files, Reviews, PR Development)" : "Show the sidebar (Wiki, Local files, Reviews, PR Development)"}
          aria-pressed={sidebarOpen}
          data-testid="toggle-sidebar"
          data-state={sidebarOpen ? "open" : "closed"}
        >
          <span className="toggle-glyph" aria-hidden="true">{sidebarOpen ? "◧" : "◨"}</span> Sidebar
        </button>
        <span className="brand-mark">W</span>
        <span className="brand-title">Wiki Reader</span>
        <WorkspaceMenu initial={boot.named_workspaces} onNew={() => setShowNewWorkspace(true)} beforeSwitch={confirmLeaveEditor} onNotify={notify} />
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
        {panelDoc && view !== "agents" && view !== "insights" && !terminalMode && (
          <div className="session-area" data-testid="session-area">
            <span className="muted small hide-mobile">Document session</span>
            <button className="btn small session-btn" onClick={() => setShowSessions(true)} data-testid="session-button" title="Choose or link the session new discussions use">
              {defaultSession ? <SessionChip session={defaultSession} /> : <>No session bound · choose…</>}
            </button>
            <button className="btn small" onClick={() => setShowSessions(true)} data-testid="new-session-button">+ New session</button>
            {!isPrDoc && <button className="btn small" onClick={() => setShowPreview(true)} title="Ask the agent for a change in an isolated copy and review the diff before applying" disabled={!!panelDoc.lock}>Preview change</button>}
            {!isPrDoc && <button className={`btn small toggle ${panelDoc.lock ? "on" : ""}`} onClick={() => void toggleLock(panelDoc.lock && panelDoc.lock.path !== panelDoc.path ? panelDoc.lock.path : panelDoc.path, !panelDoc.lock)} title={panelDoc.lock ? `Locked${panelDoc.lock.path !== panelDoc.path ? ` by folder ${panelDoc.lock.path || "(workspace)"}` : ""} — click to unlock` : "Lock this document (read-only for the app and app-driven agents)"} data-testid="doc-lock-toggle" aria-pressed={!!panelDoc.lock}>
              {panelDoc.lock ? "🔒 Locked" : "🔓 Lock"}
            </button>}
          </div>
        )}
        {view !== "agents" && view !== "insights" && !terminalMode && <nav className="view-tabs">
          <button className={view === "document" ? "active" : ""} onClick={() => setView("document")} data-testid="view-document">Document</button>
          <button className={view === "terminal" ? "active" : ""} onClick={() => setView("terminal")} data-testid="view-terminal">Terminal</button>
          <button className={view === "chats" ? "active" : ""} onClick={() => openChats(chatTarget ?? { ws: getWorkspace() ?? "", path: null })} title="Chat with a project (a folder of this window) directly" data-testid="view-chats">Chats</button>
          <button
            className={view === "diff" ? "active" : ""}
            onClick={() => (doc ? openDiff(doc.path, false) : openDiff("", true))}
            data-testid="view-diff"
            title={doc ? `Review uncommitted changes of ${doc.path} (use ± in the file tree for a folder)` : "Review uncommitted changes of the workspace"}
          >
            Diff
          </button>
        </nav>}
        <button className="btn small" onClick={() => { setRecentKey((k) => k + 1); setShowRecent(true); }} title="Documents and discussions you worked on, newest first" data-testid="recent-button">Recent</button>
        <span className="font-settings-anchor">
          <button className="btn small" onClick={() => setShowFonts((v) => !v)} title="Font style and size" data-testid="font-button">Aa</button>
          {showFonts && <FontSettings prefs={prefs} onChange={updatePrefs} onClose={() => setShowFonts(false)} />}
        </span>
        <AccountMenu identity={boot.identity} onSignOut={() => void logout()} onNotify={notify} />
      </header>

      {providerWarn && <div className="banner warn">{providerWarn}</div>}
      {boot.settings.watcher_error && <div className="banner warn">{boot.settings.watcher_error}</div>}
      {!watcherReady && <div className="banner">The file watcher is still indexing this workspace; until it finishes, use the ↻ button in the file tree to see new files and changes.</div>}

      <main className={`layout view-${view === "diff" || view === "chats" || view === "development" || view === "local" || view === "agents" || view === "insights" ? "terminal" : view} ${view === "diff" ? "view-diff" : ""} ${view === "chats" ? "view-chats" : ""}`}>
        <aside className={`sidebar with-activity ${listOpen ? "" : "list-folded"}`} data-list={listOpen ? "open" : "folded"}>
          <nav className="activity-bar" aria-label="Sidebar views">
            <button className={sidebarView === "files" ? "active" : ""} onClick={() => { if (sidebarView === "files") { updatePrefs({ listOpen: !listOpen }); if (listOpen) return; } updatePrefs({ sidebarView: "files", listOpen: true }); if (view === "development" || view === "local" || view === "agents" || view === "insights" || sidebarView === "terminal") setView("document"); }} title="Wiki: read, discuss and edit the wiki folders with the agent" aria-label="Wiki" aria-pressed={sidebarView === "files"} data-testid="activity-files">
              <WikiIcon size={20} />
            </button>
            <button className={sidebarView === "local" ? "active" : ""} onClick={() => { if (sidebarView === "local") { updatePrefs({ listOpen: !listOpen }); if (listOpen) return; } updatePrefs({ sidebarView: "local", listOpen: true }); setLocalUsed(true); setView("local"); }} title="Local files: browse and edit any folder or file on the server, like a file manager" aria-label="Local files" aria-pressed={sidebarView === "local"} data-testid="activity-local">
              <FilesIcon size={20} />
            </button>
            {hub?.available && (
              <button className={sidebarView === "reviews" ? "active" : ""} onClick={() => { if (sidebarView === "reviews") { updatePrefs({ listOpen: !listOpen }); if (listOpen) return; } updatePrefs({ sidebarView: "reviews", listOpen: true }); if (view === "development" || view === "local" || view === "agents" || view === "insights" || sidebarView === "terminal") setView("document"); }} title="Pull requests waiting for your review" aria-label="Reviews" aria-pressed={sidebarView === "reviews"} data-testid="activity-reviews">
                <PullRequestIcon size={20} />
                {reviewBadge(reviews.queue) > 0 && <span className="activity-badge" data-testid="activity-reviews-badge">{reviewBadge(reviews.queue)}</span>}
              </button>
            )}
            <button className={sidebarView === "development" ? "active" : ""} onClick={() => { if (sidebarView === "development") { updatePrefs({ listOpen: !listOpen }); if (listOpen) return; } updatePrefs({ sidebarView: "development", listOpen: true }); if (devId) setView("development"); }} title="PR Development: develop new or existing pull requests with an editor and the agent" aria-label="PR Development" aria-pressed={sidebarView === "development"} data-testid="activity-development">
              <CodeIcon size={20} />
            </button>
            <button className={sidebarView === "agents" ? "active" : ""} onClick={() => { if (sidebarView === "agents") { updatePrefs({ listOpen: !listOpen }); if (listOpen) return; } updatePrefs({ sidebarView: "agents", listOpen: true }); setView("agents"); }} title="Agents: monitor the status and health of agent sessions" aria-label="Agents" aria-pressed={sidebarView === "agents"} data-testid="activity-agents">
              <DashboardIcon size={20} />
              {alarms > 0 && <span className="activity-badge alarm" data-testid="activity-agents-badge" title={`${alarms} session(s) need you`}>{alarms}</span>}
            </button>
            <button className={sidebarView === "terminal" ? "active" : ""} onClick={() => { if (sidebarView === "terminal") { updatePrefs({ listOpen: !listOpen }); if (listOpen) return; } updatePrefs({ sidebarView: "terminal", listOpen: true }); setView("terminal"); }}
              title="Terminal: shells on the server (tmux; they keep running when the page closes)" aria-label="Terminal" aria-pressed={sidebarView === "terminal"} data-testid="activity-terminal">
              <TerminalIcon size={20} />
              {terminals.some((t) => t.state === "live") && <span className="activity-badge quiet" title={`${terminals.filter((t) => t.state === "live").length} live terminal(s)`}>{terminals.filter((t) => t.state === "live").length}</span>}
            </button>
            <button className={sidebarView === "insights" ? "active" : ""} onClick={() => { if (sidebarView === "insights") { updatePrefs({ listOpen: !listOpen }); if (listOpen) return; } updatePrefs({ sidebarView: "insights", listOpen: true }); setView("insights"); }}
              title="Repo Insights: summaries of what happens in repositories — issues, PRs, commits, RFCs, XPU highlights — over any period" aria-label="Repo Insights" aria-pressed={sidebarView === "insights"} data-testid="activity-insights">
              <InsightsIcon size={20} />
            </button>
            <span className="activity-spacer" />
            <button onClick={() => updatePrefs({ listOpen: !listOpen })} title={listOpen ? "Fold the list (folders, projects, pull requests…) — click the current icon to unfold it again" : "Show the list"}
              aria-label={listOpen ? "Fold the list" : "Show the list"} aria-pressed={!listOpen} data-testid="sidebar-list-fold">{listOpen ? "«" : "»"}</button>
          </nav>
          <div className="sidebar-content" style={{ display: sidebarView === "files" ? undefined : "none" }}>
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
              onOpenChats={(ws, about) => openChats({ ws, path: null, about: about ?? null })}
              onCreated={(ws, p, kind, openable) => {
                if (kind !== "file") return notify(`Created folder ${p}.`);
                if (!openable) return notify(`Created ${p}.`);
                if (ws !== getWorkspace()) switchWorkspace(ws, false);
                setEditNext(p);
                void openDocument(p);
              }}
              refreshKey={treeKey}
            />
          </div>
          {sidebarView === "reviews" && (
            <div className="sidebar-content">
              <ReviewsPanel onOpen={(pr) => openPr(pr.repo, pr.number, `#${pr.number} ${pr.title}`)} />
            </div>
          )}
          {sidebarView === "local" && (
            <div className="sidebar-content">
              <LocalPanel activePath={view === "local" ? localActive : null} onOpen={(p) => { setLocalUsed(true); setView("local"); setLocalOpen({ path: p, at: Date.now() }); }}
                onClosedFile={(from, to) => setLocalRenamed({ from, to, at: Date.now() })} />
            </div>
          )}
          {sidebarView === "agents" && (
            <div className="sidebar-content">
              <AgentsPanel workspaces={workspaces} watched={watched} onWatched={setWatched} onAutoChanged={() => setAutoVersion((n) => n + 1)} />
            </div>
          )}
          {sidebarView === "insights" && (
            <div className="sidebar-content">
              <InsightsPanel active={insightsRepo} onOpen={(r) => { setInsightsRepo(r); setView("insights"); }} />
            </div>
          )}
          {sidebarView === "terminal" && (
            <div className="sidebar-content">
              <TerminalsPanel terminals={terminals} activeId={activeTerminal} onActive={(id) => { setActiveTerminal(id); setView("terminal"); }} onChanged={refreshTerminals}
                available={!!boot.terminal_info?.tmux_available} />
            </div>
          )}
          {sidebarView === "development" && (
            <div className="sidebar-content">
              <DevPanel activeId={view === "development" ? devId : null} workspaces={workspaces} onOpen={(id) => openDev(id || null)}
                preset={devPreset} onPresetUsed={() => setDevPreset(null)} onFold={() => updatePrefs({ listOpen: false })} />
            </div>
          )}
          {sidebarOpen && listOpen && <Resizer side="right" onResize={resizeSidebar} onEnd={persistPrefs} />}
        </aside>

        <section className="center" style={{ display: view === "document" ? undefined : "none" }}>
          <DocTabs
            tabs={(workspaces.length ? docTabs.filter((t) => !isChatPath(t.path)).filter((t) => t.ws === PR_TABS || workspaces.some((w) => w.id === t.ws && w.available)) : docTabs).filter((t) => t.ws !== PR_TABS || !!hub?.available)}
            active={prTab ? { ws: PR_TABS, path: `${prTab.repo}#${prTab.number}` } : doc ? { ws: activeWs ?? "", path: doc.path } : null}
            workspaces={workspaces}
            onActivate={activateDocTab}
            onClose={closeDocTab}
            onReorder={(moved, target) =>
              setDocTabs((tabs) => {
                const from = tabs.findIndex((x) => sameTab(x, moved));
                const to = tabs.findIndex((x) => sameTab(x, target));
                if (from < 0 || to < 0) return tabs;
                const out = [...tabs];
                out.splice(from, 1);
                out.splice(to, 0, moved);
                return out;
              })
            }
          />
          {docError && !prTab && <div className="banner warn">{docError}</div>}
          {prTab && hub?.available ? (
            <PrView
              key={`${prTab.repo}#${prTab.number}`}
              repo={prTab.repo}
              number={prTab.number}
              workspaces={workspaces}
              docReady={!!doc && doc.path.startsWith(".waw-github/") && doc.path.endsWith(`/${prTab.number}.md`)}
              onReady={(wsId, docPath, title) => {
                setDocTabs((tabs) => tabs.map((t) => (t.ws === PR_TABS && t.path === `${prTab.repo}#${prTab.number}` ? { ...t, title } : t)));
                if (wsId !== getWorkspace()) switchWorkspace(wsId, false);
                void openDocument(docPath, false, true); // the PR's discussions (panel); quiet: errors stay in the PR view
              }}
              onSend={createThread}
              sessions={sessions}
              defaultSessionId={doc?.default_session_id ?? null}
              defaultProvider={boot.default_provider}
              githubLogin={hub?.owner ?? null}
              onOpenPr={(r, n) => openPr(r, n)}
              onDiscussionsChanged={() => { if (doc) void refreshThreads(doc.document_id); }}
              onOpenChat={(ws, p) => openChats({ ws, path: p })}
              onDevelop={(r, n, wiki) => { updatePrefs({ sidebarView: "development" }); setDevPreset({ mode: "pr", github_repo: r, pr_number: n, wiki_workspace_id: wiki }); }}
            />
          ) : doc ? (
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
              startEditing={editNext !== null && editNext === doc.path}
              onEditingStarted={() => setEditNext(null)}
              officeView={prefs.officeView ?? "layout"}
              onOfficeViewChange={(v) => updatePrefs({ officeView: v })}
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

        <section className="center chats-center" style={{ display: view === "chats" ? undefined : "none" }}>
          {view === "chats" && (
            <ChatsPage
              workspaces={workspaces}
              target={chatTarget}
              onTarget={(t) => setChatTarget(t)}
              sessions={sessions}
              onNotify={notify}
              onReviewChanges={(ws) => {
                if (ws !== getWorkspace()) switchWorkspace(ws, false);
                openDiff("", true);
              }}
            />
          )}
        </section>
        <section className="center" style={{ display: view === "diff" ? undefined : "none" }}>
          {view === "diff" && diffTarget && (
            <DiffReview
              path={diffTarget.path}
              isDir={diffTarget.isDir}
              sessions={sessions}
              defaultProvider={boot.default_provider}
              refreshKey={treeKey + diffTick}
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

        {view === "insights" && authed && (
          <section className="center insights-center">
            <InsightsView repo={insightsRepo} onOpenChat={(ws, p) => openChats({ ws, path: p })} />
          </section>
        )}
        <section className="center agents-center" style={{ display: view === "agents" ? undefined : "none" }}>
          {authed && <AgentMonitor watched={watched} onWatched={setWatched} visible={view === "agents"} onAlarms={setAlarms} autoVersion={autoVersion} />}
        </section>

        <section className="center local-center" style={{ display: view === "local" ? undefined : "none" }}>
          {localUsed && (
            <Suspense fallback={<div className="muted dev-hint">Loading the editor…</div>}>
              <LocalEditor openRequest={localOpen} renamed={localRenamed} onActive={setLocalActive} />
            </Suspense>
          )}
        </section>

        <section className="center dev-center" style={{ display: view === "development" ? undefined : "none" }}>
          {devId && (
            <DevView key={devId} devId={devId} workspaces={workspaces} sessions={sessions} onSessionsChanged={() => void refreshSessions()}
              projectsFolded={!sidebarOpen || !listOpen} onShowProjects={() => { updatePrefs({ sidebarOpen: true, listOpen: true, sidebarView: "development" }); }}
              onArchived={() => openDev(null)}
              onOpenPrReview={hub?.available ? (r, n) => { updatePrefs({ sidebarView: "reviews" }); openPr(r, n); } : undefined} />
          )}
          {!devId && view === "development" && <div className="empty"><h2>PR Development</h2><p className="muted">Create or pick a development workspace in the sidebar.</p></div>}
        </section>

        <section className="center terminal-center" style={{ display: view === "terminal" ? undefined : "none" }}>
          <TerminalPanel terminals={terminals} info={boot.terminal_info} activeId={activeTerminal} onActive={setActiveTerminal} onChanged={refreshTerminals} visible={view === "terminal"} workspaceRoot={terminalMode ? "~" : activeWsInfo?.root ?? boot.workspace.root} fontSize={prefs.terminalSize} />
        </section>

        {view === "document" && panelDoc && !panelOpen && (
          <aside className="discussion collapsed">
            <button className="collapsed-tab" onClick={() => updatePrefs({ panelOpen: true })} title="Show discussions" data-testid="panel-expand">
              ‹ Discussions{threads.length ? ` (${threads.length})` : ""}
            </button>
          </aside>
        )}
        {view === "document" && panelDoc && panelOpen && (
          <DiscussionPanel
            onCollapse={() => updatePrefs({ panelOpen: false })}
            resizer={<Resizer side="left" onResize={resizePanel} onEnd={persistPrefs} />}
            mode={prefs.panelMode}
            onModeChange={(m) => updatePrefs({ panelMode: m })}
            onSessionChanged={() => void refreshSessions()}
            onAskDocument={(q) => createThread(wholeDocument(), q, null)}
            docFormat={panelDoc.format}
            thread={activeThreadObj}
            threads={threads}
            sessions={sessions}
            defaultSessionId={panelDoc.default_session_id}
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
            docId={panelDoc.document_id}
            onRequestReanchor={() => {
              setReanchorArmed((a) => !a);
              setMobilePane("doc");
            }}
            reanchorArmed={reanchorArmed}
          />
        )}
      </main>

      <nav className="mobile-nav">
        <button className={mobilePane === "tree" ? "active" : ""} onClick={() => { setView("document"); setMobilePane("tree"); }}>Wiki</button>
        <button className={mobilePane === "doc" && view === "document" ? "active" : ""} onClick={() => { setView("document"); setMobilePane("doc"); }}>Document</button>
        <button className={mobilePane === "chat" ? "active" : ""} onClick={() => { setView("document"); setMobilePane("chat"); }}>Discussion</button>
        <button className={view === "terminal" ? "active" : ""} onClick={() => setView("terminal")}>Terminal</button>
        <button className={view === "chats" ? "active" : ""} onClick={() => openChats(chatTarget ?? { ws: getWorkspace() ?? "", path: null })}>Chats</button>
      </nav>

      <ConfirmHost />
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
      {showNewWorkspace && (
        <AddFolderDialog
          mode="new-workspace"
          start={(activeWsInfo?.root ?? boot.workspace.root).split("/").slice(0, -1).join("/") || "/"}
          onClose={() => setShowNewWorkspace(false)}
          onAdded={() => undefined}
          onWorkspaceCreated={() => window.location.reload()}
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
