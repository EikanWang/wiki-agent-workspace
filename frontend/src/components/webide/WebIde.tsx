import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import monaco from "./monaco";
import type * as M from "monaco-editor";
import { AgentSession, ApiError, DevWorkspace, Thread, get, post, put, subscribeGlobal } from "../../api";
import ModelChoice, { ModelChoiceValue } from "../ModelChoice";
import { CodeAnchor, EditorFiles, FileRef, FileScope, anchorOf, scopeFiles } from "../DevAgentPanel";
import { CORE_LSP, LSP_LANGS, LspClient, LspStatus } from "./lsp";
import { langFor, outline, registerLanguages } from "./languages";
import MarkdownPreview from "./MarkdownPreview";
import FileViewer, { viewerKind } from "./FileViewer";
import { Splitter, usePane } from "../Split";
import { FilesIcon, ProblemsIcon, SearchIcon } from "../Icons";

/**
 * The web editor of a development workspace — a VS Code-like workbench in the page: activity bar (Explorer,
 * Search, Problems), editor tabs over Monaco (VS Code's editor), quick open (Ctrl+P), a status bar, and language
 * intelligence from real language servers on the workspace's host (clangd for C/C++, basedpyright / pyright /
 * pylsp for Python). The agent is one right-click away; AI inline completion shows as ghost text.
 */

interface Root { path: string; name: string; readonly: boolean }
interface Item { name: string; dir: boolean; size: number; link: boolean }
interface Tab {
  uri: string;
  root: string;
  rel: string;
  readonly: boolean;
  dirty: boolean;
  /** the file on disk changed (agent, terminal, git) while this tab had unsaved edits; or it was deleted */
  stale: "changed" | "deleted" | null;
  binary?: boolean;
}
interface Hit { path: string; line: number; text: string }
type Side = "explorer" | "search" | "problems";

const isDark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches;

export interface AgentRequest { kind: string; text: string; context: Record<string, unknown>; anchor?: CodeAnchor; threadId?: string; image?: string; modelChoice?: ModelChoiceValue | null }

/** image: a picture that goes with the question (a PDF page / area); label: what it shows */
interface DiscussBox { anchor: CodeAnchor | null; top: number; left: number; image?: string | null; label?: string }

const DISCUSS_KINDS = [["ask", "Ask"], ["explain", "Explain"], ["fix", "Fix"], ["tests", "Write tests"], ["implement", "Implement"]] as const;

export default function WebIde({ dev, completionEnabled, refreshKey, onAsk, threads, reveal, onFiles, onOpenThread, discussSession, onDiff }: {
  /** show the changes (diff) of this folder / file of the worktree ("" = all of it) */
  onDiff?: (path: string | null) => void;
  /** the session a new discussion about this file / folder would use (for the model shown as default) */
  discussSession?: (a: CodeAnchor | null) => AgentSession | null;
  dev: DevWorkspace;
  completionEnabled: boolean;
  /** bumped when the worktree may have changed (an agent turn or a command finished) */
  refreshKey: number;
  onAsk: (req: AgentRequest) => Promise<void>;
  /** the workspace's discussions: those about lines of a file are marked in the editor's margin */
  threads: Thread[];
  /** show these lines (a discussion's passage, clicked in the agent panel) */
  reveal: { anchor: CodeAnchor; at: number } | null;
  onFiles: (f: EditorFiles) => void;
  onOpenThread: (threadId: string) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const editorRef = useRef<M.editor.IStandaloneCodeEditor | null>(null);
  const [roots, setRoots] = useState<Record<string, Root> | null>(null);
  // the worktree is named after its branch: follow a branch switch (the roots are read once)
  const shownRoots = useMemo(() => (roots?.wt ? { ...roots, wt: { ...roots.wt, name: dev.branch } } : roots), [roots, dev.branch]);
  const rootsRef = useRef<Record<string, Root> | null>(null);
  const [tabs, setTabs] = useState<Tab[]>([]);
  const tabsRef = useRef<Tab[]>([]);
  tabsRef.current = tabs;
  const [active, setActive] = useState<string | null>(null);
  const activeRef = useRef<string | null>(null);
  activeRef.current = active;
  const base = useRef(new Map<string, { sha: string; version: number }>()); // uri -> disk hash + saved version
  const views = useRef(new Map<string, M.editor.ICodeEditorViewState | null>());
  const lsps = useRef(new Map<string, LspClient>());
  const [lspStatus, setLspStatus] = useState<Record<string, LspStatus>>({});
  const [side, setSide] = useState<Side>("explorer");
  const sidePane = usePane("webide.side", 260, 160, 800);
  const sideOpen = !sidePane.folded;
  const sideOpenRef = useRef(sideOpen);
  sideOpenRef.current = sideOpen;
  const setSideOpen = (v: boolean | ((o: boolean) => boolean)) => sidePane.setFolded(!(typeof v === "function" ? v(sideOpenRef.current) : v));
  const mdPane = usePane("webide.mdsplit", 520, 200, 2000);
  const [cursor, setCursor] = useState<{ line: number; col: number; sel: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [quick, setQuick] = useState(false);
  const [markersTick, setMarkersTick] = useState(0);
  const [inline, setInline] = useState(() => localStorage.getItem("waw.webide.inline") !== "0");
  const inlineRef = useRef(inline);
  inlineRef.current = inline;
  const [gitStatus, setGitStatus] = useState<Record<string, string>>({});
  // files selected in the explorer (Ctrl/⌘-click) for "discuss with the selected files"
  const [picked, setPicked] = useState<FileRef[]>([]);
  const [discuss, setDiscuss] = useState<DiscussBox | null>(null);
  // Markdown files are shown like a wiki page (Preview), next to the source (Split) or as source (Edit)
  const [mdMode, setMdMode] = useState<Record<string, "preview" | "split" | "edit">>(() => JSON.parse(localStorage.getItem(`waw.webide.md.${dev.id}`) || "{}"));
  const [fragments, setFragments] = useState<Record<string, string | null>>({});
  useEffect(() => { localStorage.setItem(`waw.webide.md.${dev.id}`, JSON.stringify(mdMode)); }, [mdMode, dev.id]);
  const [selBtn, setSelBtn] = useState<{ top: number; left: number } | null>(null);
  const threadsRef = useRef(threads);
  threadsRef.current = threads;
  const onOpenThreadRef = useRef(onOpenThread);
  onOpenThreadRef.current = onOpenThread;
  const devRef = useRef(dev);
  devRef.current = dev;
  const askRef = useRef(onAsk);
  askRef.current = onAsk;

  const fileApi = `/dev/workspaces/${dev.id}/fs`;
  const uriOf = (root: string, rel: string) => monaco.Uri.file(`${rootsRef.current![root].path.replace(/\/$/, "")}/${rel}`).toString();
  const locate = useCallback((uri: M.Uri): { root: string; rel: string } | null => {
    const rs = rootsRef.current;
    if (!rs) return null;
    for (const [name, r] of Object.entries(rs)) {
      const p = r.path.replace(/\/$/, "");
      if (uri.path.startsWith(p + "/")) return { root: name, rel: uri.path.slice(p.length + 1) };
    }
    return null;
  }, []);

  // ---------------------------------------------------------------- language servers
  const lspFor = useCallback((model: M.editor.ITextModel): LspClient | null => {
    const lang = Object.keys(LSP_LANGS).find((l) => LSP_LANGS[l].includes(model.getLanguageId()));
    const wt = rootsRef.current?.wt;
    if (!lang || !wt || !model.uri.path.startsWith(wt.path.replace(/\/$/, "") + "/")) return null;
    let c = lsps.current.get(lang);
    if (!c) {
      c = new LspClient(monaco, devRef.current.id, lang, wt.path, { python: devRef.current.settings.python },
        (s) => setLspStatus((x) => ({ ...x, [lang]: { ...s } })), () => setMarkersTick((n) => n + 1), (u) => ensureModelRef.current(u));
      lsps.current.set(lang, c);
      setLspStatus((x) => ({ ...x, [lang]: { state: "connecting" } }));
      c.connect();
    }
    return c;
  }, []);
  const restartLsp = (lang: string) => {
    lsps.current.get(lang)?.dispose();
    lsps.current.delete(lang);
    for (const m of monaco.editor.getModels()) {
      const c = lspFor(m);
      if (c) void c.whenReady().then(() => c.open(m));
    }
  };

  // ---------------------------------------------------------------- models
  const loadModel = useCallback(async (root: string, rel: string): Promise<{ model: M.editor.ITextModel | null; binary: boolean }> => {
    const uri = monaco.Uri.parse(uriOf(root, rel));
    const existing = monaco.editor.getModel(uri);
    if (existing) return { model: existing, binary: false };
    // images and PDFs open in a viewer (any size); files too large for the editor are shown like binary files
    if (viewerKind(rel) !== "binary" && !/\.svg$/i.test(rel)) return { model: null, binary: true };
    let f: { text: string | null; sha256: string; binary: boolean };
    try {
      f = await get<{ text: string | null; sha256: string; binary: boolean }>(`${fileApi}/file?root=${root}&path=${encodeURIComponent(rel)}`);
    } catch (e) {
      if ((e as ApiError).code === "FILE_TOO_LARGE") return { model: null, binary: true };
      throw e;
    }
    if (f.binary || f.text === null) return { model: null, binary: true };
    registerLanguages();
    const lang = langFor(rel);
    const again = monaco.editor.getModel(uri); // created meanwhile
    if (again) return { model: again, binary: false };
    const model = monaco.editor.createModel(f.text, lang, uri);
    base.current.set(uri.toString(), { sha: f.sha256, version: model.getAlternativeVersionId() });
    model.onDidChangeContent(() => {
      const b = base.current.get(model.uri.toString());
      const dirty = !!b && model.getAlternativeVersionId() !== b.version;
      setTabs((ts) => ts.map((t) => (t.uri === model.uri.toString() && t.dirty !== dirty ? { ...t, dirty } : t)));
    });
    const c = lspFor(model);
    if (c) void c.whenReady().then(() => c.open(model));
    return { model, binary: false };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileApi, lspFor]);
  const ensureModelRef = useRef<(u: M.Uri) => Promise<M.editor.ITextModel | null>>(async () => null);
  ensureModelRef.current = async (u: M.Uri) => {
    const existing = monaco.editor.getModel(u);
    if (existing) return existing;
    const loc = locate(u);
    if (!loc) return null;
    try {
      return (await loadModel(loc.root, loc.rel)).model;
    } catch {
      return null;
    }
  };

  const show = useCallback((uri: string, at?: { line: number; col?: number; endLine?: number; endCol?: number }) => {
    const ed = editorRef.current;
    const model = monaco.editor.getModel(monaco.Uri.parse(uri));
    if (!ed) return;
    const cur = ed.getModel();
    if (cur && cur.uri.toString() !== uri) views.current.set(cur.uri.toString(), ed.saveViewState());
    if (model && cur !== model) {
      ed.setModel(model);
      const v = views.current.get(uri);
      if (v) ed.restoreViewState(v);
      ed.updateOptions({ readOnly: !!tabsRef.current.find((t) => t.uri === uri)?.readonly });
    }
    if (!model) ed.setModel(null);
    setActive(uri);
    if (at && model) {
      const range = new monaco.Range(at.line, at.col ?? 1, at.endLine ?? at.line, at.endCol ?? at.col ?? 1);
      ed.setSelection(range);
      ed.revealRangeInCenterIfOutsideViewport(range);
    }
    ed.focus();
  }, []);

  const openFile = useCallback(async (root: string, rel: string, at?: { line: number; col?: number; endLine?: number; endCol?: number }) => {
    setErr(null);
    const uri = uriOf(root, rel);
    if (!tabsRef.current.some((t) => t.uri === uri)) {
      try {
        const { binary } = await loadModel(root, rel);
        const readonly = !!rootsRef.current?.[root]?.readonly;
        setTabs((ts) => (ts.some((t) => t.uri === uri) ? ts : [...ts, { uri, root, rel, readonly, dirty: false, stale: null, binary }]));
        tabsRef.current = [...tabsRef.current, { uri, root, rel, readonly, dirty: false, stale: null, binary }];
      } catch (e) {
        setErr((e as ApiError).message);
        return;
      }
    }
    show(uri, at);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadModel, show]);
  const openRef = useRef(openFile);
  openRef.current = openFile;

  const closeTab = (uri: string) => {
    const t = tabsRef.current.find((x) => x.uri === uri);
    if (t?.dirty && !window.confirm(`${t.rel} has unsaved changes. Close it anyway?`)) return;
    const rest = tabsRef.current.filter((x) => x.uri !== uri);
    setTabs(rest);
    views.current.delete(uri);
    const model = monaco.editor.getModel(monaco.Uri.parse(uri));
    if (model) {
      for (const c of lsps.current.values()) c.close(model);
      model.dispose();
    }
    base.current.delete(uri);
    if (activeRef.current === uri) {
      const next = rest[rest.length - 1];
      if (next) show(next.uri);
      else {
        editorRef.current?.setModel(null);
        setActive(null);
      }
    }
  };

  const save = useCallback(async (uri: string, overwrite = false): Promise<boolean> => {
    const t = tabsRef.current.find((x) => x.uri === uri);
    const model = monaco.editor.getModel(monaco.Uri.parse(uri));
    const b = base.current.get(uri);
    if (!t || !model || !b || t.readonly) return false;
    const text = model.getValue();
    const version = model.getAlternativeVersionId();
    try {
      const r = await put<{ sha256: string }>(`${fileApi}/file`, { path: t.rel, text, base_sha256: b.sha, create: false });
      base.current.set(uri, { sha: r.sha256, version });
      setTabs((ts) => ts.map((x) => (x.uri === uri ? { ...x, dirty: model.getAlternativeVersionId() !== version, stale: null } : x)));
      for (const c of lsps.current.values()) c.saved(model);
      void loadGit();
      return true;
    } catch (e) {
      const ae = e as ApiError;
      if (ae.code === "FILE_CHANGED") {
        const cur = (ae.details?.current_sha256 as string | null) ?? null;
        if (overwrite || window.confirm(`${t.rel} was changed on disk (by the agent, a command or git) since you opened it.\n\nOK: overwrite it with your version.\nCancel: keep your edits unsaved (use "Reload from disk" to take theirs).`)) {
          if (cur === null) {
            try {
              const r = await put<{ sha256: string }>(`${fileApi}/file`, { path: t.rel, text, create: true });
              base.current.set(uri, { sha: r.sha256, version });
              setTabs((ts) => ts.map((x) => (x.uri === uri ? { ...x, dirty: false, stale: null } : x)));
              return true;
            } catch (e2) {
              setErr((e2 as ApiError).message);
              return false;
            }
          }
          base.current.set(uri, { sha: cur, version: b.version });
          return save(uri, true);
        }
        setTabs((ts) => ts.map((x) => (x.uri === uri ? { ...x, stale: cur === null ? "deleted" : "changed" } : x)));
        return false;
      }
      setErr(ae.message);
      return false;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileApi]);
  const saveRef = useRef(save);
  saveRef.current = save;
  const saveAll = async () => {
    for (const t of tabsRef.current.filter((x) => x.dirty)) await saveRef.current(t.uri);
  };

  /** Take the file on disk (keeps undo history). */
  const reloadFromDisk = useCallback(async (uri: string) => {
    const t = tabsRef.current.find((x) => x.uri === uri);
    const model = monaco.editor.getModel(monaco.Uri.parse(uri));
    if (!t || !model) return;
    try {
      const f = await get<{ text: string | null; sha256: string }>(`${fileApi}/file?root=${t.root}&path=${encodeURIComponent(t.rel)}`);
      if (f.text === null) return;
      if (f.text !== model.getValue()) {
        model.pushEditOperations([], [{ range: model.getFullModelRange(), text: f.text }], () => null);
        model.pushStackElement();
      }
      base.current.set(uri, { sha: f.sha256, version: model.getAlternativeVersionId() });
      setTabs((ts) => ts.map((x) => (x.uri === uri ? { ...x, dirty: false, stale: null } : x)));
    } catch (e) {
      if ((e as ApiError).code === "FILE_NOT_FOUND") setTabs((ts) => ts.map((x) => (x.uri === uri ? { ...x, stale: "deleted" } : x)));
    }
  }, [fileApi]);

  /** Files changed by the agent / a command / git: clean tabs follow the disk, dirty ones are flagged. */
  const syncTabs = useCallback(async () => {
    for (const root of ["wt", "wiki"]) {
      const list = tabsRef.current.filter((t) => t.root === root && !t.binary);
      if (!list.length) continue;
      let r: { sha256: Record<string, string | null> };
      try {
        r = await post(`${fileApi}/stat`, { root, paths: list.map((t) => t.rel) });
      } catch {
        continue;
      }
      for (const t of list) {
        const disk = r.sha256[t.rel];
        const b = base.current.get(t.uri);
        if (!b || disk === b.sha) continue;
        if (disk === null) setTabs((ts) => ts.map((x) => (x.uri === t.uri ? { ...x, stale: "deleted" } : x)));
        else if (!t.dirty) await reloadFromDisk(t.uri);
        else setTabs((ts) => ts.map((x) => (x.uri === t.uri ? { ...x, stale: "changed" } : x)));
      }
    }
  }, [fileApi, reloadFromDisk]);

  const loadGit = useCallback(async () => {
    try {
      const s = await get<{ files: { path: string; status: string }[] }>(`/dev/workspaces/${dev.id}/status`);
      setGitStatus(Object.fromEntries(s.files.map((f) => [f.path, f.status])));
    } catch {
      /* not ready */
    }
  }, [dev.id]);

  // ---------------------------------------------------------------- agent
  const agentContext = useCallback((kind: string) => {
    const ed = editorRef.current;
    const model = ed?.getModel();
    const c: Record<string, unknown> = {};
    if (ed && model) {
      const loc = locate(model.uri);
      const sel = ed.getSelection();
      c.file = loc ? (loc.root === "wt" ? loc.rel : `${rootsRef.current?.[loc.root]?.path}/${loc.rel}`) : model.uri.path;
      c.language = model.getLanguageId();
      if (sel && !sel.isEmpty()) c.selection = { start_line: sel.startLineNumber, end_line: sel.endLineNumber, text: model.getValueInRange(sel).slice(0, 20000) };
      else if (sel) c.selection = { start_line: sel.positionLineNumber, end_line: sel.positionLineNumber };
      const markers = monaco.editor.getModelMarkers({ resource: model.uri })
        .filter((m) => m.severity >= monaco.MarkerSeverity.Warning)
        .filter((m) => kind === "fix" && (!sel || sel.isEmpty()) ? true : !sel || (m.endLineNumber >= sel.startLineNumber && m.startLineNumber <= sel.endLineNumber));
      c.diagnostics = markers.slice(0, 60).map((m) => ({ file: c.file, line: m.startLineNumber, severity: m.severity === monaco.MarkerSeverity.Error ? "error" : "warning", message: m.message, source: m.source }));
    }
    return c;
  }, [locate]);
  const ask = useCallback(async (kind: string, preset?: string, extra?: Record<string, unknown>) => {
    let text = preset;
    if (!text) {
      text = window.prompt(kind === "implement" ? "What should the agent implement? (it reads the reference wiki first)" : "Ask the agent (the current file, selection and problems go with it):") ?? "";
      if (!text.trim()) return;
    }
    await saveAll(); // the agent works on the files on disk
    try {
      await askRef.current({ kind, text, context: { ...agentContext(kind), ...(extra ?? {}) } });
    } catch (e) {
      setErr((e as ApiError).message);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentContext]);
  const askFnRef = useRef(ask);
  askFnRef.current = ask;

  /** The editor's selection as a discussion anchor (null when nothing is selected). */
  const currentAnchor = useCallback((): CodeAnchor | null => {
    const ed = editorRef.current;
    const model = ed?.getModel();
    const sel = ed?.getSelection();
    if (!ed || !model || !sel || sel.isEmpty()) return null;
    const loc = locate(model.uri);
    if (!loc) return null;
    return { root: loc.root, file: loc.rel, start_line: sel.startLineNumber, end_line: sel.endLineNumber, start_col: sel.startColumn,
      end_col: sel.endColumn, quote: model.getValueInRange(sel).slice(0, 20000), language: model.getLanguageId() };
  }, [locate]);
  const openDiscuss = useCallback((withSelection: boolean) => {
    const ed = editorRef.current;
    const box = host.current?.getBoundingClientRect();
    const root = host.current?.closest(".webide")?.getBoundingClientRect();
    const anchor = withSelection ? currentAnchor() : null;
    let top = 60, left = 80;
    if (ed && anchor && box && root) {
      const p = ed.getScrolledVisiblePosition({ lineNumber: anchor.end_line ?? 1, column: anchor.end_col ?? 1 });
      if (p) {
        top = Math.min(box.top - root.top + p.top + p.height + 4, root.height - 260);
        left = Math.min(Math.max(8, box.left - root.left + 40), root.width - 440);
      }
    }
    setSelBtn(null);
    setDiscuss({ anchor, top, left });
  }, [currentAnchor]);
  const openDiscussRef = useRef(openDiscuss);
  openDiscussRef.current = openDiscuss;
  const sendDiscuss = async (text: string, kind: string, scope: FileScope, modelChoice: ModelChoiceValue | null) => {
    const anchor = discuss?.anchor ?? undefined;
    await saveAll();
    const filesOf = scopeFiles(scope, filesNow());
    await askRef.current({ kind, text, anchor, image: discuss?.image ?? undefined, modelChoice,
      context: { ...(anchor ? {} : agentContext(kind)), ...(discuss?.label ? { page: discuss.label } : {}),
        ...(anchor && !anchor.start_line && anchor.quote ? { page_text: anchor.quote } : {}), ...(filesOf.length ? { files: filesOf } : {}) } });
    setDiscuss(null);
  };

  // ---------------------------------------------------------------- mount: roots, editor, global hooks
  useEffect(() => {
    let alive = true;
    void get<{ roots: Record<string, Root> }>(`/dev/workspaces/${dev.id}/roots`).then((r) => {
      if (!alive) return;
      rootsRef.current = r.roots;
      setRoots(r.roots);
      const saved = JSON.parse(localStorage.getItem(`waw.webide.tabs.${dev.id}`) || "null") as { tabs: { root: string; rel: string }[]; active: string | null } | null;
      void (async () => {
        for (const t of saved?.tabs ?? []) if (r.roots[t.root]) await openRef.current(t.root, t.rel).catch(() => undefined);
        if (saved?.active && tabsRef.current.some((t) => t.uri === saved.active)) show(saved.active);
      })();
    }).catch((e) => setErr((e as ApiError).message));
    void loadGit();
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dev.id]);

  useEffect(() => {
    if (!host.current) return;
    const ed = monaco.editor.create(host.current, {
      model: null, automaticLayout: true, theme: isDark() ? "vs-dark" : "vs", fontSize: 13, minimap: { enabled: true },
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace",
      inlineSuggest: { enabled: true }, renderWhitespace: "selection", smoothScrolling: true, stickyScroll: { enabled: true },
      bracketPairColorization: { enabled: true }, guides: { bracketPairs: true }, "semanticHighlighting.enabled": true,
      fixedOverflowWidgets: true, glyphMargin: true, wordBasedSuggestions: "allDocuments", occurrencesHighlight: "singleFile",
      links: true, colorDecorators: true,
    });
    editorRef.current = ed;
    const subs: M.IDisposable[] = [];
    let selTimer: ReturnType<typeof setTimeout> | undefined;
    subs.push(ed.onDidChangeCursorSelection((e) => {
      const s = e.selection;
      const model = ed.getModel();
      setCursor({ line: s.positionLineNumber, col: s.positionColumn, sel: model && !s.isEmpty() ? model.getValueInRange(s).length : 0 });
      // a "Discuss" button next to a selection, like selecting a passage in the wiki reader
      clearTimeout(selTimer);
      setSelBtn(null);
      if (s.isEmpty() || !model) return;
      selTimer = setTimeout(() => {
        const p = ed.getScrolledVisiblePosition({ lineNumber: s.endLineNumber, column: s.endColumn });
        const box = host.current?.getBoundingClientRect();
        const root = host.current?.closest(".webide")?.getBoundingClientRect();
        if (!p || !box || !root) return;
        setSelBtn({ top: box.top - root.top + p.top + p.height + 2, left: Math.min(box.left - root.left + p.left, root.width - 140) });
      }, 250);
    }));
    subs.push(ed.onDidScrollChange(() => setSelBtn(null)));
    subs.push(ed.onMouseDown((e) => {
      // the margin marker of a discussion opens it in the agent panel
      if (e.target.type !== monaco.editor.MouseTargetType.GUTTER_GLYPH_MARGIN || !e.target.position) return;
      const model = ed.getModel();
      const loc = model ? locate(model.uri) : null;
      const line = e.target.position.lineNumber;
      const t = threadsRef.current.find((x) => { const a = anchorOf(x); return a && loc && a.root === loc.root && a.file === loc.rel && a.start_line === line; });
      if (t) onOpenThreadRef.current(t.id);
    }));
    subs.push(ed.addAction({ id: "waw.discuss", label: "Agent: Discuss the selection…", contextMenuGroupId: "0_agent", contextMenuOrder: 0,
      keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyMod.Alt | monaco.KeyCode.KeyD], run: () => openDiscussRef.current(true) }));
    ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => { const u = activeRef.current; if (u) void saveRef.current(u); });
    ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyP, () => setQuick(true));
    ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyMod.Shift | monaco.KeyCode.KeyF, () => { setSide("search"); setSideOpen(true); });
    ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyB, () => setSideOpen((o) => !o));
    const action = (id: string, label: string, kind: string, preset: string | undefined, order: number, keys?: number[]) =>
      subs.push(ed.addAction({ id, label, contextMenuGroupId: "0_agent", contextMenuOrder: order, keybindings: keys, run: () => void askFnRef.current(kind, preset) }));
    action("waw.ask", "Agent: Ask…", "ask", undefined, 1, [monaco.KeyMod.CtrlCmd | monaco.KeyMod.Alt | monaco.KeyCode.KeyA]);
    action("waw.explain", "Agent: Explain this code", "explain", "Explain this code.", 2);
    action("waw.fix", "Agent: Fix problems here", "fix", "Fix the problems here.", 3, [monaco.KeyMod.CtrlCmd | monaco.KeyMod.Alt | monaco.KeyCode.KeyF]);
    action("waw.tests", "Agent: Write tests for this code", "tests", "Write tests for this code.", 4);
    action("waw.implement", "Agent: Implement a change…", "implement", undefined, 5);
    subs.push(ed.addAction({ id: "waw.quickOpen", label: "Go to File…", run: () => setQuick(true) }));
    subs.push(ed.addAction({ id: "waw.saveAll", label: "File: Save All", run: () => void saveAll() }));
    // other files (go to definition, peek, references) open as tabs
    subs.push(monaco.editor.registerEditorOpener({
      openCodeEditor: (_src, resource, sel) => {
        const loc = locate(resource);
        if (!loc) return false;
        const at = sel ? ("startLineNumber" in sel ? { line: sel.startLineNumber, col: sel.startColumn, endLine: sel.endLineNumber, endCol: sel.endColumn } : { line: sel.lineNumber, col: sel.column }) : undefined;
        void openRef.current(loc.root, loc.rel, at);
        return true;
      },
    }));
    // "Fix with the agent" on any error / warning
    const cmd = `waw.fixWithAgent.${Math.random().toString(36).slice(2, 8)}`;
    subs.push(monaco.editor.registerCommand(cmd, (_a, diags: unknown) => void askFnRef.current("fix", "Fix these problems.", { diagnostics: diags })));
    subs.push(monaco.languages.registerCodeActionProvider("*", {
      provideCodeActions: (model, _range, ctx) => {
        const ms = ctx.markers.filter((m) => m.severity >= monaco.MarkerSeverity.Warning);
        if (!ms.length) return { actions: [], dispose: () => undefined };
        const loc = locate(model.uri);
        const diags = ms.map((m) => ({ file: loc?.rel ?? model.uri.path, line: m.startLineNumber, severity: m.severity === monaco.MarkerSeverity.Error ? "error" : "warning", message: m.message, source: m.source }));
        return { actions: [{ title: "Fix with the agent", kind: "quickfix", diagnostics: ms, command: { id: cmd, title: "Fix with the agent", arguments: [diags] } }], dispose: () => undefined };
      },
    }));
    subs.push(monaco.editor.onDidChangeMarkers(() => setMarkersTick((n) => n + 1)));
    // ---- navigation for every language: a ready language server answers; otherwise search-based (backend) and
    // an outline from the text — go to definition (F12 / Ctrl+click, also Bazel labels and paths in strings),
    // references (Shift+F12), outline / breadcrumbs / Ctrl+Shift+O
    const lspReady = (model: M.editor.ITextModel) => {
      const key = Object.keys(LSP_LANGS).find((l) => LSP_LANGS[l].includes(model.getLanguageId()));
      return !!key && !!lsps.current.get(key)?.ready_;
    };
    const navCache = new Map<string, { at: number; p: Promise<M.languages.Location[]> }>();
    const nav = (kind: "definition" | "references", model: M.editor.ITextModel, position: M.Position): Promise<M.languages.Location[]> => {
      const loc = locate(model.uri);
      if (!loc) return Promise.resolve([]);
      const word = model.getWordAtPosition(position)?.word ?? "";
      const lineText = model.getLineContent(position.lineNumber);
      const key = `${kind}|${loc.root}|${loc.rel}|${position.lineNumber}|${word}|${kind === "definition" ? position.column : ""}`;
      const hit = navCache.get(key);
      if (hit && Date.now() - hit.at < 15000) return hit.p;
      const p = post<{ locations: { path: string; line: number; col?: number }[] }>(`/dev/workspaces/${devRef.current.id}/nav/${kind}`, {
        root: loc.root, path: loc.rel, word, language: model.getLanguageId(), line_text: lineText.slice(0, 20000), col: position.column,
      }).then(async (r) => {
        const out: M.languages.Location[] = [];
        for (const l of r.locations) {
          const uri = monaco.Uri.parse(uriOf(loc.root, l.path));
          await ensureModelRef.current(uri); // the peek view needs the target's text
          const col = l.col ?? 1;
          out.push({ uri, range: new monaco.Range(l.line, col, l.line, col + (word && kind !== "definition" ? word.length : word.length || 1)) });
        }
        return out;
      }).catch(() => [] as M.languages.Location[]);
      navCache.set(key, { at: Date.now(), p });
      if (navCache.size > 200) navCache.delete(navCache.keys().next().value!);
      return p;
    };
    subs.push(monaco.languages.registerDefinitionProvider("*", {
      provideDefinition: (model, position) => (lspReady(model) ? undefined : nav("definition", model, position)),
    }));
    subs.push(monaco.languages.registerReferenceProvider("*", {
      provideReferences: (model, position) => (lspReady(model) ? undefined : nav("references", model, position)),
    }));
    subs.push(monaco.languages.registerDocumentSymbolProvider("*", {
      displayName: "outline",
      provideDocumentSymbols: (model) => (lspReady(model) || !locate(model.uri) ? undefined : outline(model.getLanguageId(), model)),
    }));
    // JSON: report syntax errors (comments allowed in .jsonc / .code-workspace)
    const checkJson = (model: M.editor.ITextModel) => {
      if (model.getLanguageId() !== "json" || lspReady(model)) return;
      const text = model.getValue();
      const lenient = /\.(jsonc|json5|code-workspace)$/.test(model.uri.path) || model.uri.path.endsWith("/.clangd");
      const src = lenient ? text.replace(/\/\*[\s\S]*?\*\/|(^|[^:"])\/\/.*$/gm, (m, pre) => (pre ?? "") + m.slice((pre ?? "").length).replace(/[^\n]/g, " ")).replace(/,(\s*[}\]])/g, " $1") : text;
      const markers: M.editor.IMarkerData[] = [];
      if (src.trim()) {
        try {
          JSON.parse(src);
        } catch (e) {
          const msg = String((e as Error).message);
          const m = msg.match(/position (\d+)/);
          const pos = model.getPositionAt(m ? Number(m[1]) : src.length);
          markers.push({ severity: monaco.MarkerSeverity.Error, message: msg.replace(/^JSON\.parse: /, ""), source: "json", startLineNumber: pos.lineNumber, startColumn: pos.column, endLineNumber: pos.lineNumber, endColumn: pos.column + 1 });
        }
      }
      monaco.editor.setModelMarkers(model, "waw-json", markers);
    };
    const jsonTimers = new Map<string, ReturnType<typeof setTimeout>>();
    const watchJson = (model: M.editor.ITextModel) => {
      if (model.getLanguageId() !== "json") return;
      checkJson(model);
      subs.push(model.onDidChangeContent(() => {
        clearTimeout(jsonTimers.get(model.uri.toString()));
        jsonTimers.set(model.uri.toString(), setTimeout(() => checkJson(model), 400));
      }));
    };
    monaco.editor.getModels().forEach(watchJson);
    subs.push(monaco.editor.onDidCreateModel(watchJson));
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    const onScheme = () => monaco.editor.setTheme(isDark() ? "vs-dark" : "vs");
    mq?.addEventListener?.("change", onScheme);
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (tabsRef.current.some((t) => t.dirty)) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => {
      clearTimeout(selTimer);
      subs.forEach((s) => s.dispose());
      mq?.removeEventListener?.("change", onScheme);
      window.removeEventListener("beforeunload", onBeforeUnload);
      for (const c of lsps.current.values()) c.dispose();
      lsps.current.clear();
      ed.dispose();
      for (const t of tabsRef.current) monaco.editor.getModel(monaco.Uri.parse(t.uri))?.dispose();
      for (const m of monaco.editor.getModels()) if (locate(m.uri)) m.dispose();
      editorRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dev.id]);

  // AI inline completion (ghost text)
  useEffect(() => {
    if (!completionEnabled) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const d = monaco.languages.registerInlineCompletionsProvider(["c", "cpp", "python", "cmake", "shell"], {
      provideInlineCompletions: (model, position, _ctx, token) => new Promise((resolve) => {
        if (!inlineRef.current || !locate(model.uri)) return resolve({ items: [] });
        clearTimeout(timer);
        timer = setTimeout(async () => {
          if (token.isCancellationRequested) return resolve({ items: [] });
          const start = Math.max(1, position.lineNumber - 150);
          const end = Math.min(model.getLineCount(), position.lineNumber + 50);
          const prefix = model.getValueInRange(new monaco.Range(start, 1, position.lineNumber, position.column));
          const suffix = model.getValueInRange(new monaco.Range(position.lineNumber, position.column, end, model.getLineMaxColumn(end)));
          try {
            const r = await post<{ text: string }>(`/dev/workspaces/${devRef.current.id}/complete`, { language: model.getLanguageId(), path: locate(model.uri)?.rel ?? "", prefix, suffix });
            if (token.isCancellationRequested || !r.text) return resolve({ items: [] });
            resolve({ items: [{ insertText: r.text, range: new monaco.Range(position.lineNumber, position.column, position.lineNumber, position.column) }] });
          } catch {
            resolve({ items: [] });
          }
        }, 350);
      }),
      disposeInlineCompletions: () => undefined,
    });
    return () => { clearTimeout(timer); d.dispose(); };
  }, [completionEnabled, locate]);

  // follow the disk: on agent turns / commands / saves elsewhere, and every few seconds
  useEffect(() => { void syncTabs(); void loadGit(); }, [refreshKey, syncTabs, loadGit]);
  useEffect(() => {
    const t = setInterval(() => { if (document.visibilityState === "visible") void syncTabs(); }, 4000);
    const off = subscribeGlobal((ev) => { if ((ev.type === "dev_changed" || ev.type === "dev_task") && ev.dev_id === dev.id) { void syncTabs(); void loadGit(); } });
    return () => { clearInterval(t); off(); };
  }, [dev.id, syncTabs, loadGit]);
  // remember the open tabs
  useEffect(() => {
    if (!roots) return;
    localStorage.setItem(`waw.webide.tabs.${dev.id}`, JSON.stringify({ tabs: tabs.map((t) => ({ root: t.root, rel: t.rel })), active }));
  }, [tabs, active, roots, dev.id]);
  // the workspace's Python interpreter changed: the language server needs a restart to pick it up
  const pyRef = useRef(dev.settings.python);
  useEffect(() => {
    if (pyRef.current !== dev.settings.python && lsps.current.has("python")) restartLsp("python");
    pyRef.current = dev.settings.python;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dev.settings.python]);

  // what is open / selected, for "discuss with … files"
  const filesNow = useCallback((): EditorFiles => {
    const at = tabsRef.current.find((t) => t.uri === activeRef.current);
    return { active: at ? { root: at.root, path: at.rel } : null, open: tabsRef.current.filter((t) => !t.binary).map((t) => ({ root: t.root, path: t.rel })), selected: picked };
  }, [picked]);
  useEffect(() => { onFiles(filesNow()); }, [tabs, active, picked, filesNow, onFiles]);

  // discussions about lines of the shown file: a marker in the margin (click: open it), the lines tinted
  const decos = useRef<M.editor.IEditorDecorationsCollection | null>(null);
  useEffect(() => {
    const ed = editorRef.current;
    const model = ed?.getModel();
    if (!ed) return;
    if (!decos.current) decos.current = ed.createDecorationsCollection();
    const loc = model ? locate(model.uri) : null;
    const list = loc ? threads.map((t, i) => ({ t, i, a: anchorOf(t) })).filter((x) => x.a && x.a.start_line && x.a.root === loc.root && x.a.file === loc.rel) : [];
    decos.current.set(list.flatMap(({ t, i, a }) => [
      { range: new monaco.Range(a!.start_line!, 1, Math.min(a!.end_line ?? a!.start_line!, model!.getLineCount()), 1),
        options: { isWholeLine: true, className: "webide-thread-lines", overviewRuler: { color: "#4f8cf7", position: monaco.editor.OverviewRulerLane.Left } } },
      { range: new monaco.Range(a!.start_line!, 1, a!.start_line!, 1),
        options: { glyphMarginClassName: "webide-thread-glyph",
          glyphMarginHoverMessage: { value: `**Task ${i + 1}** — ${(t.messages[0]?.content ?? "").slice(0, 200)}\n\n*Click to open the discussion*` } } },
    ]));
  }, [threads, active, locate]);
  useEffect(() => {
    if (!reveal) return;
    const a = reveal.anchor;
    void openFile(a.root, a.file, a.start_line ? { line: a.start_line, col: a.start_col ?? 1, endLine: a.end_line ?? a.start_line, endCol: a.end_col ?? 1 } : undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "p" && !e.shiftKey) { e.preventDefault(); setQuick(true); }
    else if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); if (e.shiftKey || e.altKey) void saveAll(); else if (activeRef.current) void save(activeRef.current); }
    else if (mod && e.shiftKey && e.key.toLowerCase() === "f") { e.preventDefault(); setSide("search"); setSideOpen(true); }
    else if (mod && e.shiftKey && e.key.toLowerCase() === "e") { e.preventDefault(); setSide("explorer"); setSideOpen(true); }
    else if (mod && e.shiftKey && e.key.toLowerCase() === "m") { e.preventDefault(); setSide("problems"); setSideOpen(true); }
  };

  const activeTab = tabs.find((t) => t.uri === active) ?? null;
  /** A new discussion about the whole file (any type), optionally with a picture (PDF page / area, image). */
  const chatAbout = (t: Tab, image?: string | null, label?: string, quote?: string) => {
    const box = host.current?.closest(".webide")?.getBoundingClientRect();
    setSelBtn(null);
    setDiscuss({ anchor: { root: t.root, file: t.rel, quote: quote ?? "", language: t.binary ? viewerKind(t.rel) : langFor(t.rel) },
      top: 70, left: Math.max(8, (box?.width ?? 600) / 2 - 215), image: image ?? null, label });
  };
  const isMd = (t: Tab) => !t.binary && ["markdown", "mdx"].includes(langFor(t.rel));
  const mdOf = (t: Tab) => mdMode[t.uri] ?? "preview";
  const discussionsOf = useMemo(() => {
    const m = new Map<string, number>();
    for (const t of threads) { const a = anchorOf(t); if (a) m.set(`${a.root}:${a.file}`, (m.get(`${a.root}:${a.file}`) ?? 0) + 1); }
    return m;
  }, [threads]);
  const problems = useMemo(() => {
    void markersTick;
    const all = monaco.editor.getModelMarkers({});
    const seen = new Set(all.map((m) => m.resource.toString()));
    const out: { uri: string; line: number; col: number; severity: number; message: string; source?: string }[] =
      all.filter((m) => m.severity >= monaco.MarkerSeverity.Warning || m.owner.startsWith("lsp")).map((m) => ({ uri: m.resource.toString(), line: m.startLineNumber, col: m.startColumn, severity: m.severity, message: m.message, source: m.source }));
    for (const c of lsps.current.values()) {
      for (const { uri, diags } of c.allDiagnostics()) {
        if (seen.has(uri)) continue;
        for (const d of diags) out.push({ uri, line: d.range.start.line + 1, col: d.range.start.character + 1, severity: d.severity === 1 ? 8 : d.severity === 2 ? 4 : 2, message: d.message, source: d.source });
      }
    }
    return out.sort((a, b) => b.severity - a.severity || a.uri.localeCompare(b.uri) || a.line - b.line);
  }, [markersTick]);
  const errors = problems.filter((p) => p.severity === monaco.MarkerSeverity.Error).length;
  const warnings = problems.filter((p) => p.severity === monaco.MarkerSeverity.Warning).length;
  const langOfActive = activeTab ? langFor(activeTab.rel) : null;
  const lspLang = langOfActive ? Object.keys(LSP_LANGS).find((l) => LSP_LANGS[l].includes(langOfActive)) : undefined;
  const ls = lspLang ? lspStatus[lspLang] : undefined;

  return (
    <div className="webide" onKeyDown={onKeyDown} data-testid="webide">
      <div className="webide-body">
        <nav className="webide-activity" aria-label="Editor views">
          {(["explorer", "search", "problems"] as Side[]).map((s) => (
            <button key={s} className={side === s && sideOpen ? "active" : ""} title={{ explorer: "Explorer (Ctrl+Shift+E)", search: "Search (Ctrl+Shift+F)", problems: "Problems (Ctrl+Shift+M)" }[s]}
              onClick={() => { if (side === s) setSideOpen(!sideOpen); else { setSide(s); setSideOpen(true); } }} data-testid={`webide-view-${s}`}>
              {s === "explorer" ? <FilesIcon size={20} /> : s === "search" ? <SearchIcon size={20} /> : <ProblemsIcon size={20} />}
              {s === "problems" && errors + warnings > 0 && <span className="webide-badge">{errors + warnings}</span>}
            </button>
          ))}
        </nav>
        {sideOpen && (
          <aside className="webide-side" style={{ width: sidePane.size }}>
            {side === "explorer" && shownRoots && <Explorer devId={dev.id} roots={shownRoots} git={gitStatus} activeUri={active} uriOf={uriOf} refreshKey={refreshKey} onDiff={onDiff}
              picked={picked} onPick={setPicked} onDiscussPicked={() => openDiscuss(false)}
              onDiscussFolder={(r, p) => { setSelBtn(null); setDiscuss({ anchor: { root: r, file: p, quote: "", folder: true }, top: 70, left: 60 }); }}
              onOpen={(r, p) => void openFile(r, p)} onError={setErr}
              onRenamed={(r, from, to) => { const t = tabsRef.current.find((x) => x.root === r && x.rel === from); if (t) { closeTab(t.uri); void openFile(r, to); } }}
              onDeleted={(r, p) => { for (const t of tabsRef.current.filter((x) => x.root === r && (x.rel === p || x.rel.startsWith(p + "/")))) setTabs((ts) => ts.map((x) => (x.uri === t.uri ? { ...x, stale: "deleted" } : x))); }} />}
            {side === "search" && roots && <SearchView devId={dev.id} roots={roots} onOpen={(r, p, line) => void openFile(r, p, { line })} />}
            {side === "problems" && (
              <div className="webide-problems" data-testid="webide-problems">
                <div className="webide-side-head">PROBLEMS</div>
                {problems.length === 0 && <div className="muted small webide-pad">No problems have been detected in the open files.</div>}
                {problems.map((p, i) => {
                  const loc = locate(monaco.Uri.parse(p.uri));
                  return (
                    <button key={i} className="webide-row" onClick={() => loc && void openFile(loc.root, loc.rel, { line: p.line, col: p.col })} title={p.message}>
                      <span className={`webide-sev s${p.severity}`}>{p.severity === 8 ? "✖" : p.severity === 4 ? "▲" : "ℹ"}</span>
                      <span className="webide-msg">{p.message}</span>
                      <span className="muted small">{loc?.rel ?? p.uri}:{p.line}{p.source ? ` · ${p.source}` : ""}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </aside>
        )}
        <Splitter dir="x" side="before" pane={sidePane} label="side bar" testId="webide-side-splitter" />
        <div className="webide-main">
          <div className="webide-tabs" role="tablist">
            {tabs.map((t) => (
              <div key={t.uri} role="tab" aria-selected={t.uri === active} className={`webide-tab ${t.uri === active ? "active" : ""} ${t.stale ? "stale" : ""}`}
                onClick={() => show(t.uri)} onAuxClick={(e) => { if (e.button === 1) closeTab(t.uri); }} title={`${t.root === "wt" ? "" : `${roots?.[t.root]?.name}: `}${t.rel}${t.readonly ? " (read-only)" : ""}`} data-testid="webide-tab" data-path={t.rel}>
                <span className="webide-tab-name">{t.rel.split("/").pop()}</span>
                {t.root !== "wt" && <span className="muted small">wiki</span>}
                {(discussionsOf.get(`${t.root}:${t.rel}`) ?? 0) > 0 && (
                  <span className="webide-tab-disc" title="Discussions about this file (shown in the agent panel)" data-testid="webide-tab-discussions">💬{discussionsOf.get(`${t.root}:${t.rel}`)}</span>
                )}
                <button className="webide-tab-close" onClick={(e) => { e.stopPropagation(); closeTab(t.uri); }} aria-label={`Close ${t.rel}`} data-dirty={t.dirty ? "1" : "0"}>{t.dirty ? "●" : "×"}</button>
              </div>
            ))}
          </div>
          {activeTab && (
            <div className="webide-crumbs muted small">
              <span className="webide-crumbs-path" title={activeTab.rel}>
                <span>{activeTab.root === "wt" ? dev.branch : roots?.[activeTab.root]?.name}</span>
                {activeTab.rel.split("/").map((p, i) => <span key={i}> › {p}</span>)}
              </span>
              <span className="spacer" />
              {activeTab.readonly && <span className="chip">read-only</span>}
              {isMd(activeTab) && (
                <span className="mode-switch webide-md-mode" role="tablist" aria-label="Markdown view">
                  {(["preview", "split", "edit"] as const).map((m) => (
                    <button key={m} role="tab" aria-selected={mdOf(activeTab) === m} className={mdOf(activeTab) === m ? "active" : ""}
                      onClick={() => { setMdMode((x) => ({ ...x, [activeTab.uri]: m })); if (m !== "preview") setTimeout(() => editorRef.current?.layout(), 0); }}
                      data-testid={`webide-md-${m}`}>{m === "preview" ? "Preview" : m === "split" ? "Split" : "Edit"}</button>
                  ))}
                </span>
              )}
              <button className="btn small webide-chat-file" onClick={() => chatAbout(activeTab)} title="Chat with the agent about this file (its discussions are in the agent panel's This file view)" data-testid="webide-chat-file">💬 Chat about this file</button>
            </div>
          )}
          {activeTab?.stale && (
            <div className="banner warn small" data-testid="webide-stale">
              {activeTab.stale === "deleted" ? "This file was deleted on disk." : "This file was changed on disk (agent, command or git) while you have unsaved edits."}
              {activeTab.stale === "changed" && <button className="btn small" onClick={() => void reloadFromDisk(activeTab.uri)}>Reload from disk (lose my edits)</button>}
              <button className="btn small" onClick={() => void save(activeTab.uri, true)}>{activeTab.stale === "deleted" ? "Save again" : "Overwrite with mine"}</button>
            </div>
          )}
          {err && <div className="banner warn small" role="alert">{err} <button className="icon-btn small" onClick={() => setErr(null)}>×</button></div>}
          <div className="webide-editor-area" style={{ display: activeTab?.binary ? "none" : undefined }}>
            <div className="webide-editor" ref={host} data-testid="webide-editor"
              style={{ display: activeTab && !activeTab.binary && !(isMd(activeTab) && mdOf(activeTab) === "preview") ? undefined : "none" }} />
            {activeTab && isMd(activeTab) && mdOf(activeTab) === "split" && <Splitter dir="x" side="after" pane={{ ...mdPane, folded: false, setFolded: (f) => { if (f) setMdMode((x) => ({ ...x, [activeTab.uri]: "edit" })); } }} label="preview" testId="webide-md-splitter" />}
            {activeTab && isMd(activeTab) && mdOf(activeTab) !== "edit" && monaco.editor.getModel(monaco.Uri.parse(activeTab.uri)) && (
              <div className="webide-md-wrap" style={mdOf(activeTab) === "split" ? { width: mdPane.size, flex: "none", maxWidth: "70%" } : undefined}>
              <MarkdownPreview key={activeTab.uri} model={monaco.editor.getModel(monaco.Uri.parse(activeTab.uri))!} root={activeTab.root} rel={activeTab.rel}
                devId={dev.id} threads={threads} fragment={fragments[activeTab.uri] ?? null}
                onFragmentDone={() => setFragments((f) => (f[activeTab.uri] ? { ...f, [activeTab.uri]: null } : f))}
                onOpen={(r, p, frag) => { if (frag) setFragments((f) => ({ ...f, [uriOf(r, p)]: frag })); void openFile(r, p); }}
                onOpenThread={(id) => onOpenThreadRef.current(id)}
                onDiscuss={(anchor, rect) => {
                  const root = host.current?.closest(".webide")?.getBoundingClientRect();
                  const top = rect && root ? Math.min(rect.bottom - root.top + 6, root.height - 280) : 70;
                  const left = rect && root ? Math.min(Math.max(8, rect.left - root.left), root.width - 440) : Math.max(8, (root?.width ?? 600) / 2 - 215);
                  setSelBtn(null);
                  setDiscuss({ anchor, top, left });
                }} />
              </div>
            )}
          </div>
          {activeTab?.binary && (
            <FileViewer devId={dev.id} root={activeTab.root} rel={activeTab.rel} threads={threads}
              onOpenThread={(id) => onOpenThreadRef.current(id)}
              onDiscuss={(image, label, quote) => chatAbout(activeTab, image, label, quote)} />
          )}
          {!activeTab && (
            <div className="webide-empty">
              <div className="webide-empty-title">{dev.name}</div>
              <div className="muted small">
                <div><kbd>Ctrl+P</kbd> Go to file · <kbd>Ctrl+Shift+F</kbd> Search · <kbd>Ctrl+S</kbd> Save · <kbd>F12</kbd> Go to definition · <kbd>Shift+F12</kbd> References · <kbd>F2</kbd> Rename · <kbd>Ctrl+.</kbd> Quick fix · <kbd>F1</kbd> Commands</div>
                <div>Right-click in a file → <b>Agent: Ask / Explain / Fix / Write tests</b> (<kbd>Ctrl+Alt+A</kbd>)</div>
              </div>
            </div>
          )}
        </div>
      </div>
      <div className="webide-status" data-testid="webide-status">
        <span title={dev.worktree_path}>⎇ {dev.branch}{Object.keys(gitStatus).length ? ` · ${Object.keys(gitStatus).length} changed` : ""}</span>
        <button className="webide-status-btn" onClick={() => { setSide("problems"); setSideOpen(true); }} title="Problems">✖ {errors} ▲ {warnings}</button>
        <span className="spacer" />
        {cursor && activeTab && <span>Ln {cursor.line}, Col {cursor.col}{cursor.sel ? ` (${cursor.sel} selected)` : ""}</span>}
        {langOfActive && <span>{langOfActive}</span>}
        {lspLang && (
          <button className={`webide-status-btn lsp-${CORE_LSP.has(lspLang) ? ls?.state ?? "none" : ls?.state === "ready" ? "ready" : "basic"}`} data-testid="webide-lsp" onClick={() => restartLsp(lspLang)}
            title={`${ls?.command ?? "language server"}: ${ls?.state ?? "not started"}${ls?.message ? ` — ${ls.message}` : ""}\nClick to restart`}>
            {ls?.state === "ready" ? "✓" : ls?.state === "connecting" ? "…" : CORE_LSP.has(lspLang) ? "⚠" : "◌"}{" "}
            {ls?.state === "ready" ? ls.command?.split("/").pop() : !CORE_LSP.has(lspLang) && ls?.state !== "connecting" ? "basic navigation" : ls?.command?.split("/").pop() ?? (lspLang === "cpp" ? "clangd" : lspLang === "python" ? "Python LS" : lspLang)}
            {ls?.progress ? ` · ${ls.progress}` : ""}
          </button>
        )}
        {completionEnabled && (
          <button className="webide-status-btn" onClick={() => { setInline(!inline); localStorage.setItem("waw.webide.inline", inline ? "0" : "1"); }} title="AI inline completion (ghost text; Tab accepts)" data-testid="webide-inline">
            AI {inline ? "on" : "off"}
          </button>
        )}
      </div>
      {ls && lspLang && CORE_LSP.has(lspLang) && (ls.state === "unavailable" || ls.state === "exited" || ls.state === "error") && activeTab && (
        <div className="webide-lsp-note small" data-testid="webide-lsp-note">{ls.message}</div>
      )}
      {selBtn && !discuss && (
        <button className="webide-discuss-btn" style={{ top: selBtn.top, left: selBtn.left }} onMouseDown={(e) => e.preventDefault()} onClick={() => openDiscuss(true)}
          title="Discuss the selected code with the agent (Ctrl+Alt+D)" data-testid="webide-discuss-btn">💬 Discuss</button>
      )}
      {discuss && <DiscussComposer box={discuss} files={filesNow()} session={discussSession ? discussSession(discuss.anchor) : null} onCancel={() => { setDiscuss(null); editorRef.current?.focus(); }} onSend={sendDiscuss} />}
      {quick && <QuickOpen devId={dev.id} roots={shownRoots} onClose={() => { setQuick(false); editorRef.current?.focus(); }} onOpen={(r, p) => { setQuick(false); void openFile(r, p); }} />}
    </div>
  );
}

// ---------------------------------------------------------------- explorer

function Explorer({ devId, roots, git, activeUri, uriOf, refreshKey, picked, onPick, onDiscussPicked, onDiscussFolder, onOpen, onError, onRenamed, onDeleted, onDiff }: {
  onDiff?: (path: string | null) => void;
  devId: string; roots: Record<string, Root>; git: Record<string, string>; activeUri: string | null; uriOf: (r: string, p: string) => string; refreshKey: number;
  picked: FileRef[]; onPick: (f: FileRef[]) => void; onDiscussPicked: () => void; onDiscussFolder: (root: string, path: string) => void;
  onOpen: (root: string, path: string) => void; onError: (m: string) => void;
  onRenamed: (root: string, from: string, to: string) => void; onDeleted: (root: string, path: string) => void;
}) {
  const [dirs, setDirs] = useState<Record<string, Item[]>>({});
  const [open, setOpen] = useState<Set<string>>(() => new Set(JSON.parse(localStorage.getItem(`waw.webide.open.${devId}`) || '["wt:"]')));
  const key = (root: string, path: string) => `${root}:${path}`;
  const load = useCallback(async (root: string, path: string) => {
    try {
      const r = await get<{ items: Item[] }>(`/dev/workspaces/${devId}/fs/list?root=${root}&path=${encodeURIComponent(path)}`);
      setDirs((d) => ({ ...d, [key(root, path)]: r.items }));
    } catch (e) {
      if ((e as ApiError).code === "FOLDER_NOT_FOUND") setOpen((o) => { const n = new Set(o); n.delete(key(root, path)); return n; });
      else onError((e as ApiError).message);
    }
  }, [devId, onError]);
  useEffect(() => {
    for (const k of open) {
      const [root, ...rest] = k.split(":");
      if (roots[root]) void load(root, rest.join(":"));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey, load]);
  useEffect(() => { localStorage.setItem(`waw.webide.open.${devId}`, JSON.stringify([...open])); }, [open, devId]);
  const toggle = (root: string, path: string) => {
    const k = key(root, path);
    setOpen((o) => {
      const n = new Set(o);
      if (n.has(k)) n.delete(k);
      else {
        n.add(k);
        void load(root, path);
      }
      return n;
    });
  };
  const refreshAll = () => { for (const k of open) { const [r, ...p] = k.split(":"); void load(r, p.join(":")); } };
  const create = async (dir: string, kind: "file" | "dir") => {
    const name = window.prompt(kind === "file" ? `New file in ${dir || "the worktree"}:` : `New folder in ${dir || "the worktree"}:`);
    if (!name?.trim()) return;
    const rel = (dir ? `${dir}/` : "") + name.trim();
    try {
      if (kind === "file") await put(`/dev/workspaces/${devId}/fs/file`, { path: rel, text: "", create: true });
      else await post(`/dev/workspaces/${devId}/fs/mkdir`, { path: rel });
      setOpen((o) => new Set(o).add(key("wt", dir)));
      await load("wt", dir);
      if (kind === "file") onOpen("wt", rel);
    } catch (e) {
      onError((e as ApiError).message);
    }
  };
  const rename = async (path: string) => {
    const to = window.prompt("Rename / move to (path in the worktree):", path);
    if (!to?.trim() || to.trim() === path) return;
    try {
      await post(`/dev/workspaces/${devId}/fs/rename`, { path, new_path: to.trim() });
      onRenamed("wt", path, to.trim());
      refreshAll();
    } catch (e) {
      onError((e as ApiError).message);
    }
  };
  const remove = async (path: string, dir: boolean) => {
    if (!window.confirm(`Delete ${path}${dir ? " and everything in it" : ""}? (Git can bring back committed files.)`)) return;
    try {
      await post(`/dev/workspaces/${devId}/fs/delete`, { path });
      onDeleted("wt", path);
      refreshAll();
    } catch (e) {
      onError((e as ApiError).message);
    }
  };
  const isPicked = (root: string, rel: string) => picked.some((f) => f.root === root && f.path === rel);
  const pick = (root: string, rel: string) =>
    onPick(isPicked(root, rel) ? picked.filter((f) => !(f.root === root && f.path === rel)) : [...picked, { root, path: rel }]);
  const gitOf = (rel: string, dir: boolean): string | undefined => {
    if (!dir) return git[rel];
    const pre = rel + "/";
    return Object.keys(git).some((p) => p.startsWith(pre)) ? "modified" : undefined;
  };
  const render = (root: string, path: string, depth: number): React.ReactNode => {
    const items = dirs[key(root, path)];
    if (!items) return <div className="muted small webide-row" style={{ paddingLeft: depth * 12 + 20 }}>Loading…</div>;
    return items.map((it) => {
      const rel = path ? `${path}/${it.name}` : it.name;
      const g = root === "wt" ? gitOf(rel, it.dir) : undefined;
      const isOpen = open.has(key(root, rel));
      return (
        <div key={rel}>
          <div className={`webide-row tree ${!it.dir && uriOf(root, rel) === activeUri ? "selected" : ""} ${isPicked(root, rel) ? "picked" : ""} ${g ? `git-${g}` : ""}`} style={{ paddingLeft: depth * 12 + 6 }}
            onClick={(e) => {
              if (!it.dir && (e.ctrlKey || e.metaKey)) return pick(root, rel);
              if (it.dir) toggle(root, rel);
              else onOpen(root, rel);
            }} title={`${rel}${it.dir ? "" : "\nCtrl/⌘-click: select for the agent"}`} data-testid="webide-tree-item" data-path={rel} data-root={root} data-picked={isPicked(root, rel) ? "1" : "0"}>
            <span className="webide-twisty">{it.dir ? (isOpen ? "▾" : "▸") : ""}</span>
            <span className="webide-name">{it.name}</span>
            {g && <span className="webide-git">{g[0].toUpperCase()}</span>}
            {it.dir && root !== "wt" && (
              <span className="webide-row-actions">
                <button className="icon-btn small" title="Discuss this folder with the agent" onClick={(e) => { e.stopPropagation(); onDiscussFolder(root, rel); }}>💬</button>
              </span>
            )}
            {root === "wt" && (
              <span className="webide-row-actions">
                {it.dir && <button className="icon-btn small" title="Discuss this folder with the agent" onClick={(e) => { e.stopPropagation(); onDiscussFolder(root, rel); }} data-testid="webide-discuss-folder">💬</button>}
                {it.dir && <button className="icon-btn small" title="New file" onClick={(e) => { e.stopPropagation(); void create(rel, "file"); }}>+</button>}
                {onDiff && <button className="icon-btn small" title={`View the changes (diff) of this ${it.dir ? "folder" : "file"}`} onClick={(e) => { e.stopPropagation(); onDiff(rel); }} data-testid="webide-diff">±</button>}
                <button className="icon-btn small" title="Rename / move" onClick={(e) => { e.stopPropagation(); void rename(rel); }}>✎</button>
                <button className="icon-btn small" title="Delete" onClick={(e) => { e.stopPropagation(); void remove(rel, it.dir); }}>🗑</button>
              </span>
            )}
          </div>
          {it.dir && isOpen && render(root, rel, depth + 1)}
        </div>
      );
    });
  };
  return (
    <div className="webide-explorer" data-testid="webide-explorer">
      <div className="webide-side-head">
        EXPLORER
        <span className="spacer" />
        <button className="icon-btn small" title="New file" onClick={() => void create("", "file")} data-testid="webide-new-file">+</button>
        <button className="icon-btn small" title="New folder" onClick={() => void create("", "dir")}>⊞</button>
        <button className="icon-btn small" title="Refresh" onClick={refreshAll}>↻</button>
        {onDiff && <button className="icon-btn small" title="View all changes (diff)" onClick={() => onDiff(null)} data-testid="webide-diff-all">±</button>}
      </div>
      {picked.length > 0 && (
        <div className="webide-picked" data-testid="webide-picked">
          <span>{picked.length} selected</span>
          <button className="btn small primary" onClick={onDiscussPicked} data-testid="webide-discuss-picked">Discuss</button>
          <button className="icon-btn small" onClick={() => onPick([])} title="Clear the selection">×</button>
        </div>
      )}
      {Object.entries(roots).map(([name, r]) => (
        <div key={name}>
          <div className="webide-row root tree" onClick={() => toggle(name, "")} title={r.path}>
            <span className="webide-twisty">{open.has(key(name, "")) ? "▾" : "▸"}</span>
            <b>{name === "wt" ? r.name.toUpperCase() : r.name.toUpperCase()}</b>
            {r.readonly && <span className="muted small"> read-only</span>}
            {name !== "wt" && <span className="webide-row-actions"><button className="icon-btn small" title="Discuss the reference wiki with the agent" onClick={(e) => { e.stopPropagation(); onDiscussFolder(name, "."); }}>💬</button></span>}
          </div>
          {open.has(key(name, "")) && render(name, "", 1)}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- search

function SearchView({ devId, roots, onOpen }: { devId: string; roots: Record<string, Root>; onOpen: (root: string, path: string, line: number) => void }) {
  const [q, setQ] = useState("");
  const [root, setRoot] = useState("wt");
  const [regex, setRegex] = useState(false);
  const [cs, setCs] = useState(false);
  const [glob, setGlob] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => input.current?.focus(), []);
  const run = async () => {
    if (!q.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await get<{ hits: Hit[]; truncated: boolean }>(`/dev/workspaces/${devId}/fs/search?root=${root}&q=${encodeURIComponent(q)}&regex=${regex}&case=${cs}${glob.trim() ? `&glob=${encodeURIComponent(glob.trim())}` : ""}`);
      setHits(r.hits);
      setTruncated(r.truncated);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const byFile = useMemo(() => {
    const m = new Map<string, Hit[]>();
    for (const h of hits ?? []) m.set(h.path, [...(m.get(h.path) ?? []), h]);
    return [...m.entries()];
  }, [hits]);
  return (
    <div className="webide-search" data-testid="webide-search">
      <div className="webide-side-head">SEARCH</div>
      <form className="webide-pad webide-search-form" onSubmit={(e) => { e.preventDefault(); void run(); }}>
        <div className="row nowrap">
          <input ref={input} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search (Enter)" data-testid="webide-search-input"
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void run(); } }} />
          <button type="button" className={`icon-btn small ${cs ? "on" : ""}`} title="Match case" onClick={() => setCs(!cs)}>Aa</button>
          <button type="button" className={`icon-btn small ${regex ? "on" : ""}`} title="Regular expression" onClick={() => setRegex(!regex)}>.*</button>
        </div>
        <div className="row nowrap">
          <input value={glob} onChange={(e) => setGlob(e.target.value)} placeholder="files to include, e.g. *.cpp"
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void run(); } }} />
          <select value={root} onChange={(e) => setRoot(e.target.value)}>
            {Object.entries(roots).map(([n, r]) => <option key={n} value={n}>{n === "wt" ? "worktree" : r.name}</option>)}
          </select>
        </div>
      </form>
      {busy && <div className="muted small webide-pad">Searching…</div>}
      {err && <div className="error-text small webide-pad">{err}</div>}
      {hits && !busy && <div className="muted small webide-pad">{hits.length}{truncated ? "+" : ""} results in {byFile.length} files</div>}
      <div className="webide-results">
        {byFile.map(([path, hs]) => (
          <div key={path}>
            <div className="webide-row file-head" title={path}><b>{path.split("/").pop()}</b> <span className="muted small">{path}</span> <span className="chip">{hs.length}</span></div>
            {hs.map((h, i) => (
              <button key={i} className="webide-row hit" onClick={() => onOpen(root, h.path, h.line)} data-testid="webide-search-hit">
                <span className="muted small">{h.line}</span> <span className="webide-hit-text">{h.text.trim()}</span>
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- quick open

function QuickOpen({ devId, roots, onClose, onOpen }: { devId: string; roots: Record<string, Root> | null; onClose: () => void; onOpen: (root: string, path: string) => void }) {
  const [files, setFiles] = useState<string[] | null>(null);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  useEffect(() => {
    void get<{ files: string[] }>(`/dev/workspaces/${devId}/fs/all?root=wt`).then((r) => setFiles(r.files)).catch(() => setFiles([]));
  }, [devId]);
  const matches = useMemo(() => {
    if (!files) return [];
    const needle = q.toLowerCase().replace(/\s+/g, "");
    if (!needle) return files.slice(0, 50);
    const scored: [number, string][] = [];
    for (const f of files) {
      const l = f.toLowerCase();
      const name = l.split("/").pop()!;
      let i = 0, score = 0, last = -1;
      for (const ch of needle) {
        const j = l.indexOf(ch, i);
        if (j < 0) { score = -1; break; }
        score += j === last + 1 ? 2 : 0;
        last = j;
        i = j + 1;
      }
      if (score < 0) continue;
      if (name.includes(needle)) score += 20;
      if (name.startsWith(needle)) score += 10;
      scored.push([score - f.length / 100, f]);
    }
    return scored.sort((a, b) => b[0] - a[0]).slice(0, 50).map((x) => x[1]);
  }, [files, q]);
  useEffect(() => setSel(0), [q]);
  return (
    <div className="webide-quick-backdrop" onClick={onClose}>
      <div className="webide-quick" onClick={(e) => e.stopPropagation()} data-testid="webide-quick">
        <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder={roots?.wt ? `Go to a file in ${roots.wt.name}…` : "Go to file…"} data-testid="webide-quick-input"
          onKeyDown={(e) => {
            if (e.key === "Escape") onClose();
            else if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(matches.length - 1, s + 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(0, s - 1)); }
            else if (e.key === "Enter" && matches[sel]) onOpen("wt", matches[sel]);
          }} />
        <div className="webide-quick-list">
          {files === null && <div className="muted small webide-pad">Listing files…</div>}
          {matches.map((f, i) => (
            <button key={f} className={`webide-row ${i === sel ? "selected" : ""}`} onMouseEnter={() => setSel(i)} onClick={() => onOpen("wt", f)} data-testid="webide-quick-item">
              <span>{f.split("/").pop()}</span> <span className="muted small">{f}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------- discuss a selection / files

function DiscussComposer({ box, files, onCancel, onSend, session }: {
  box: DiscussBox;
  /** the session the discussion would use by default (the file's) */
  session?: AgentSession | null;
  files: EditorFiles;
  onCancel: () => void;
  onSend: (text: string, kind: string, scope: FileScope, modelChoice: ModelChoiceValue | null) => Promise<void>;
}) {
  const [text, setText] = useState("");
  const [kind, setKind] = useState("ask");
  const [modelChoice, setModelChoice] = useState<ModelChoiceValue | null>(null);
  const [scope, setScope] = useState<FileScope>(box.anchor ? "none" : files.selected.length ? "selected" : "current");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const send = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await onSend(text.trim(), kind, scope, modelChoice);
    } catch (e) {
      setErr((e as ApiError).message);
      setBusy(false);
    }
  };
  const a = box.anchor;
  return (
    <div className="webide-discuss" style={{ top: box.top, left: box.left }} data-testid="webide-discuss" onKeyDown={(e) => { if (e.key === "Escape") onCancel(); e.stopPropagation(); }}>
      <div className="row nowrap webide-discuss-head">
        <b>{box.label ? `Discuss ${box.label} of ${a?.file.split("/").pop()}` : a?.folder ? `Discuss the folder ${a.file === "." ? "(the whole wiki)" : a.file}` : a ? (a.start_line ? `Discuss ${a.file.split("/").pop()}:${a.start_line}${a.end_line && a.end_line !== a.start_line ? `–${a.end_line}` : ""}` : `Discuss ${a.file.split("/").pop()} (the whole file)`) : "Discuss with the agent"}</b>
        <span className="spacer" />
        <button className="icon-btn small" onClick={onCancel} aria-label="Cancel">×</button>
      </div>
      {box.image && <img className="webide-discuss-image" src={box.image} alt="" data-testid="webide-discuss-image" />}
      {a && a.quote && <pre className="webide-discuss-quote">{a.quote.split("\n").slice(0, 4).join("\n")}{a.quote.split("\n").length > 4 ? "\n…" : ""}</pre>}
      <textarea autoFocus rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder={a ? "Ask about these lines, or what to change…" : "Ask about these files, or what to change…"}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }} data-testid="webide-discuss-input" />
      <div className="row nowrap">
        <select value={kind} onChange={(e) => setKind(e.target.value)} data-testid="webide-discuss-kind">
          {DISCUSS_KINDS.map(([id, l]) => <option key={id} value={id}>{l}</option>)}
        </select>
        <select value={scope} onChange={(e) => setScope(e.target.value as FileScope)} title="Files sent with the request" data-testid="webide-discuss-scope">
          <option value="none">{a ? "Only the selection" : "No files"}</option>
          <option value="current">{a ? "+ " : ""}Current file</option>
          <option value="open">{a ? "+ " : ""}Open files ({files.open.length})</option>
          <option value="selected">{a ? "+ " : ""}Selected files ({files.selected.length})</option>
        </select>
        <span className="spacer" />
        <button className="btn small primary" disabled={!text.trim() || busy} onClick={() => void send()} data-testid="webide-discuss-send">{busy ? "Sending…" : "Send"}</button>
      </div>
      <div className="row nowrap"><ModelChoice value={modelChoice} onChange={setModelChoice} session={session ?? null} /></div>
      {err && <div className="error-text small">{err}</div>}
      <div className="muted small">Enter sends · Shift+Enter new line · the answer appears in the agent panel</div>
    </div>
  );
}
