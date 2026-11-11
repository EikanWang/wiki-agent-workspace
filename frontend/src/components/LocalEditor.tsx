import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import monaco from "./webide/monaco";
import type * as M from "monaco-editor";
import { ApiError, get, post, put } from "../api";
import { langFor, outline, registerLanguages } from "./webide/languages";
import { renderMarkdown } from "../markdown";

/**
 * The editor of Local files: tabs over Monaco (VS Code's editor) for any file on the server — syntax highlighting,
 * find/replace, outline (Ctrl+Shift+O), conditional saves (Ctrl+S; a file changed on disk is never overwritten
 * silently), clean tabs follow changes on disk; Markdown with a preview; images shown; other binaries downloadable.
 */

interface Tab { path: string; dirty: boolean; kind: "text" | "image" | "binary"; stale: "changed" | "deleted" | null; readonly: boolean; note?: string }

const IMAGE = /\.(png|jpe?g|gif|webp|bmp|ico|svg)$/i;
const isDark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches;
const raw = (p: string) => `/api/v1/local/raw?path=${encodeURIComponent(p)}`;

export default function LocalEditor({ openRequest, renamed, onActive }: {
  /** a file to open (from the panel) */
  openRequest: { path: string; at: number } | null;
  /** a file / folder was renamed (new path) or deleted (null) */
  renamed: { from: string; to: string | null; at: number } | null;
  onActive: (path: string | null) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const ed = useRef<M.editor.IStandaloneCodeEditor | null>(null);
  const [tabs, setTabs] = useState<Tab[]>([]);
  const tabsRef = useRef<Tab[]>([]);
  tabsRef.current = tabs;
  const [active, setActive] = useState<string | null>(null);
  const activeRef = useRef<string | null>(null);
  activeRef.current = active;
  const base = useRef(new Map<string, { sha: string; version: number }>());
  const views = useRef(new Map<string, M.editor.ICodeEditorViewState | null>());
  const [err, setErr] = useState<string | null>(null);
  const [cursor, setCursor] = useState<{ line: number; col: number } | null>(null);
  const [mdPreview, setMdPreview] = useState<Record<string, boolean>>({});
  const [mdText, setMdText] = useState("");
  const uri = (p: string) => monaco.Uri.file(p);
  const model = (p: string) => monaco.editor.getModel(uri(p));

  useEffect(() => onActive(active), [active, onActive]);

  const show = useCallback((p: string) => {
    const e = ed.current;
    const cur = e?.getModel();
    if (e && cur && cur.uri.path !== p) views.current.set(cur.uri.path, e.saveViewState());
    const m = model(p);
    if (e && m && cur !== m) {
      e.setModel(m);
      const v = views.current.get(p);
      if (v) e.restoreViewState(v);
      e.updateOptions({ readOnly: !!tabsRef.current.find((t) => t.path === p)?.readonly });
    }
    setActive(p);
    if (m) setMdText(m.getValue());
    setTimeout(() => e?.focus(), 0);
  }, []);

  const openFile = useCallback(async (p: string) => {
    setErr(null);
    if (!tabsRef.current.some((t) => t.path === p)) {
      let tab: Tab;
      if (IMAGE.test(p) && !/\.svg$/i.test(p)) tab = { path: p, dirty: false, kind: "image", stale: null, readonly: true };
      else {
        let f: { text: string | null; sha256: string | null; binary: boolean; too_large: boolean; writable: boolean; size: number };
        try {
          f = await get(`/local/file?path=${encodeURIComponent(p)}`);
        } catch (e) {
          setErr((e as ApiError).message);
          return;
        }
        if (f.binary || f.text === null) {
          tab = { path: p, dirty: false, kind: IMAGE.test(p) ? "image" : "binary", stale: null, readonly: true,
            note: f.too_large ? `${f.size.toLocaleString()} bytes — too large to edit here` : `${f.size.toLocaleString()} bytes — binary or not UTF-8 text` };
        } else {
          registerLanguages();
          const m = model(p) ?? monaco.editor.createModel(f.text, langFor(p), uri(p));
          base.current.set(p, { sha: f.sha256!, version: m.getAlternativeVersionId() });
          m.onDidChangeContent(() => {
            const b = base.current.get(p);
            const dirty = !!b && m.getAlternativeVersionId() !== b.version;
            setTabs((ts) => ts.map((t) => (t.path === p && t.dirty !== dirty ? { ...t, dirty } : t)));
            if (activeRef.current === p) setMdText(m.getValue());
          });
          tab = { path: p, dirty: false, kind: "text", stale: null, readonly: !f.writable };
        }
      }
      tabsRef.current = [...tabsRef.current, tab];
      setTabs(tabsRef.current);
    }
    show(p);
  }, [show]);

  const save = useCallback(async (p: string, overwrite = false): Promise<void> => {
    const t = tabsRef.current.find((x) => x.path === p);
    const m = model(p);
    const b = base.current.get(p);
    if (!t || !m || !b || t.kind !== "text") return;
    const text = m.getValue();
    const version = m.getAlternativeVersionId();
    try {
      const r = await put<{ sha256: string }>("/local/file", { path: p, text, base_sha256: b.sha });
      base.current.set(p, { sha: r.sha256, version });
      setTabs((ts) => ts.map((x) => (x.path === p ? { ...x, dirty: m.getAlternativeVersionId() !== version, stale: null } : x)));
    } catch (e) {
      const ae = e as ApiError;
      if (ae.code === "FILE_CHANGED") {
        const cur = (ae.details?.current_sha256 as string | null) ?? null;
        if (overwrite || window.confirm(`${p} changed on disk since you opened it.\n\nOK: overwrite it with your version.\nCancel: keep your edits unsaved.`)) {
          if (cur === null) {
            const r = await put<{ sha256: string }>("/local/file", { path: p, text, create: true });
            base.current.set(p, { sha: r.sha256, version });
            setTabs((ts) => ts.map((x) => (x.path === p ? { ...x, dirty: false, stale: null } : x)));
            return;
          }
          base.current.set(p, { sha: cur, version: b.version });
          return save(p, true);
        }
        setTabs((ts) => ts.map((x) => (x.path === p ? { ...x, stale: cur === null ? "deleted" : "changed" } : x)));
      } else setErr(ae.message);
    }
  }, []);
  const saveRef = useRef(save);
  saveRef.current = save;

  const reload = useCallback(async (p: string) => {
    const m = model(p);
    if (!m) return;
    try {
      const f = await get<{ text: string | null; sha256: string }>(`/local/file?path=${encodeURIComponent(p)}`);
      if (f.text === null) return;
      if (f.text !== m.getValue()) {
        m.pushEditOperations([], [{ range: m.getFullModelRange(), text: f.text }], () => null);
        m.pushStackElement();
      }
      base.current.set(p, { sha: f.sha256, version: m.getAlternativeVersionId() });
      setTabs((ts) => ts.map((x) => (x.path === p ? { ...x, dirty: false, stale: null } : x)));
    } catch (e) {
      if ((e as ApiError).code === "FILE_NOT_FOUND") setTabs((ts) => ts.map((x) => (x.path === p ? { ...x, stale: "deleted" } : x)));
    }
  }, []);

  const close = (p: string) => {
    const t = tabsRef.current.find((x) => x.path === p);
    if (t?.dirty && !window.confirm(`${p} has unsaved changes. Close it anyway?`)) return;
    const rest = tabsRef.current.filter((x) => x.path !== p);
    tabsRef.current = rest;
    setTabs(rest);
    model(p)?.dispose();
    base.current.delete(p);
    views.current.delete(p);
    if (activeRef.current === p) {
      if (rest.length) show(rest[rest.length - 1].path);
      else { ed.current?.setModel(null); setActive(null); }
    }
  };

  // editor
  useEffect(() => {
    if (!host.current) return;
    const e = monaco.editor.create(host.current, {
      model: null, automaticLayout: true, theme: isDark() ? "vs-dark" : "vs", fontSize: 13, minimap: { enabled: true },
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace", renderWhitespace: "selection",
      stickyScroll: { enabled: true }, bracketPairColorization: { enabled: true }, wordBasedSuggestions: "allDocuments", fixedOverflowWidgets: true,
    });
    ed.current = e;
    e.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => { if (activeRef.current) void saveRef.current(activeRef.current); });
    const subs: M.IDisposable[] = [
      e.onDidChangeCursorPosition((ev) => setCursor({ line: ev.position.lineNumber, col: ev.position.column })),
      monaco.languages.registerDocumentSymbolProvider("*", {
        displayName: "local outline",
        provideDocumentSymbols: (m) => (m.uri.scheme === "file" && tabsRef.current.some((t) => t.path === m.uri.path) ? outline(m.getLanguageId(), m) : undefined),
      }),
    ];
    const before = (ev: BeforeUnloadEvent) => { if (tabsRef.current.some((t) => t.dirty)) { ev.preventDefault(); ev.returnValue = ""; } };
    window.addEventListener("beforeunload", before);
    // reopen the tabs of the last visit
    const saved = JSON.parse(localStorage.getItem("waw.local.tabs") || "null") as { tabs: string[]; active: string | null } | null;
    void (async () => {
      for (const p of saved?.tabs ?? []) await openFile(p);
      if (saved?.active && tabsRef.current.some((t) => t.path === saved.active)) show(saved.active);
    })();
    return () => {
      window.removeEventListener("beforeunload", before);
      subs.forEach((s) => s.dispose());
      e.dispose();
      for (const t of tabsRef.current) model(t.path)?.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { localStorage.setItem("waw.local.tabs", JSON.stringify({ tabs: tabs.map((t) => t.path), active })); }, [tabs, active]);
  useEffect(() => { if (openRequest) void openFile(openRequest.path); }, [openRequest, openFile]);
  useEffect(() => {
    if (!renamed) return;
    for (const t of tabsRef.current.filter((x) => x.path === renamed.from || x.path.startsWith(renamed.from + "/"))) {
      if (renamed.to && !t.dirty) { const np = renamed.to + t.path.slice(renamed.from.length); close(t.path); void openFile(np); }
      else setTabs((ts) => ts.map((x) => (x.path === t.path ? { ...x, stale: "deleted" } : x)));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [renamed]);
  // follow the disk (other editors, the agent, git, the terminal)
  useEffect(() => {
    const tick = async () => {
      const list = tabsRef.current.filter((t) => t.kind === "text");
      if (!list.length || document.visibilityState !== "visible") return;
      try {
        const r = await post<{ sha256: Record<string, string | null> }>("/local/stat", { paths: list.map((t) => t.path) });
        for (const t of list) {
          const disk = r.sha256[t.path];
          const b = base.current.get(t.path);
          if (!b || disk === b.sha) continue;
          if (disk === null) setTabs((ts) => ts.map((x) => (x.path === t.path ? { ...x, stale: "deleted" } : x)));
          else if (!t.dirty) await reload(t.path);
          else setTabs((ts) => ts.map((x) => (x.path === t.path ? { ...x, stale: "changed" } : x)));
        }
      } catch {
        /* offline */
      }
    };
    const iv = setInterval(() => void tick(), 4000);
    return () => clearInterval(iv);
  }, [reload]);

  const tab = tabs.find((t) => t.path === active) ?? null;
  const isMd = !!tab && tab.kind === "text" && ["markdown", "mdx"].includes(langFor(tab.path));
  const preview = isMd && mdPreview[tab!.path] !== false; // Markdown opens in preview
  const html = useMemo(() => {
    if (!preview || !tab) return "";
    return renderMarkdown(mdText, tab.path, { imageUrl: (p) => raw("/" + p) });
  }, [preview, mdText, tab]);
  const showEditor = !!tab && tab.kind === "text" && !preview;
  return (
    <div className="local-editor webide" data-testid="local-editor"
      onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (active) void save(active); } }}>
      <div className="webide-tabs" role="tablist">
        {tabs.map((t) => (
          <div key={t.path} role="tab" aria-selected={t.path === active} className={`webide-tab ${t.path === active ? "active" : ""} ${t.stale ? "stale" : ""}`}
            onClick={() => show(t.path)} onAuxClick={(e) => { if (e.button === 1) close(t.path); }} title={t.path} data-testid="local-tab" data-path={t.path}>
            <span className="webide-tab-name">{t.path.split("/").pop()}</span>
            <button className="webide-tab-close" onClick={(e) => { e.stopPropagation(); close(t.path); }} aria-label={`Close ${t.path}`} data-dirty={t.dirty ? "1" : "0"}>{t.dirty ? "●" : "×"}</button>
          </div>
        ))}
      </div>
      {tab && (
        <div className="webide-crumbs muted small">
          <span className="webide-crumbs-path mono" title={tab.path}>{tab.path}</span>
          <span className="spacer" />
          {tab.readonly && tab.kind === "text" && <span className="chip">read-only</span>}
          {isMd && (
            <span className="mode-switch" role="tablist">
              <button role="tab" className={preview ? "active" : ""} onClick={() => setMdPreview((x) => ({ ...x, [tab.path]: true }))} data-testid="local-md-preview">Preview</button>
              <button role="tab" className={!preview ? "active" : ""} onClick={() => setMdPreview((x) => ({ ...x, [tab.path]: false }))} data-testid="local-md-edit">Edit</button>
            </span>
          )}
          {tab.kind === "text" && <button className="btn small" disabled={!tab.dirty} onClick={() => void save(tab.path)} data-testid="local-save">Save</button>}
        </div>
      )}
      {tab?.stale && (
        <div className="banner warn small" data-testid="local-stale">
          {tab.stale === "deleted" ? "This file was deleted or moved on disk." : "This file changed on disk while you have unsaved edits."}
          {tab.stale === "changed" && <button className="btn small" onClick={() => void reload(tab.path)}>Reload from disk (lose my edits)</button>}
          <button className="btn small" onClick={() => void save(tab.path, true)}>{tab.stale === "deleted" ? "Save again" : "Overwrite with mine"}</button>
        </div>
      )}
      {err && <div className="banner warn small" role="alert">{err} <button className="icon-btn small" onClick={() => setErr(null)}>×</button></div>}
      <div className="webide-editor" ref={host} style={{ display: showEditor ? undefined : "none" }} data-testid="local-monaco" />
      {preview && (
        <div className="webide-md-scroll local-md" onClick={(e) => {
          const a = (e.target as HTMLElement).closest("a") as HTMLAnchorElement | null;
          if (a?.dataset.internal !== undefined) { e.preventDefault(); void openFile("/" + a.dataset.internal); }
        }}>
          <article className="markdown-body webide-md-body" dangerouslySetInnerHTML={{ __html: html }} data-testid="local-md-body" />
        </div>
      )}
      {tab?.kind === "image" && (
        <div className="webide-viewer"><div className="image-view"><img src={raw(tab.path)} alt={tab.path} data-testid="local-image" /></div></div>
      )}
      {tab?.kind === "binary" && (
        <div className="webide-empty" data-testid="local-binary">
          <div className="webide-empty-title">{tab.path.split("/").pop()}</div>
          <div className="muted small">{tab.note}</div>
          <a className="btn small" href={raw(tab.path)} download>Download</a>
        </div>
      )}
      {!tab && (
        <div className="webide-empty">
          <div className="webide-empty-title">Local files</div>
          <div className="muted small">Open a folder or a file on the left. <kbd>Ctrl+S</kbd> saves · <kbd>Ctrl+F</kbd> find · <kbd>Ctrl+Shift+O</kbd> outline · <kbd>F1</kbd> commands</div>
        </div>
      )}
      <div className="webide-status">
        <span>{tab ? tab.path : "Local files"}</span>
        <span className="spacer" />
        {cursor && showEditor && <span>Ln {cursor.line}, Col {cursor.col}</span>}
        {tab?.kind === "text" && <span>{langFor(tab.path)}</span>}
      </div>
    </div>
  );
}
