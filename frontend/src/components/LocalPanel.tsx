import { useCallback, useEffect, useState } from "react";
import { ApiError, get, post, put } from "../api";
import FolderPicker from "./FolderPicker";

interface Root { path: string; name: string; exists: boolean }
interface Item { name: string; path: string; dir: boolean; size: number; link: boolean }

/**
 * Local files: any folder or file on the server, browsed and edited like a file manager (no wiki features).
 * Open folders stay in the list (server-side); a file can be opened by its path.
 */
export default function LocalPanel({ activePath, onOpen, onClosedFile }: {
  activePath: string | null;
  onOpen: (path: string) => void;
  /** a file / folder was deleted or renamed: tabs under it react */
  onClosedFile: (oldPath: string, newPath: string | null) => void;
}) {
  const [roots, setRoots] = useState<Root[] | null>(null);
  const [dirs, setDirs] = useState<Record<string, Item[]>>({});
  const [open, setOpen] = useState<Set<string>>(() => new Set(JSON.parse(localStorage.getItem("waw.local.open") || "[]")));
  const [hidden, setHidden] = useState(() => localStorage.getItem("waw.local.hidden") === "1");
  const [pick, setPick] = useState(false);
  const [filePath, setFilePath] = useState("");
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { localStorage.setItem("waw.local.open", JSON.stringify([...open])); }, [open]);
  useEffect(() => { localStorage.setItem("waw.local.hidden", hidden ? "1" : "0"); }, [hidden]);

  const load = useCallback(async (path: string) => {
    try {
      const r = await get<{ items: Item[] }>(`/local/list?path=${encodeURIComponent(path)}&hidden=${hidden}`);
      setDirs((d) => ({ ...d, [path]: r.items }));
    } catch (e) {
      if ((e as ApiError).code === "FOLDER_NOT_FOUND") setOpen((o) => { const n = new Set(o); n.delete(path); return n; });
      else setErr((e as ApiError).message);
    }
  }, [hidden]);
  useEffect(() => {
    void get<{ roots: Root[] }>("/local/roots").then((r) => setRoots(r.roots)).catch((e) => setErr((e as ApiError).message));
  }, []);
  const refreshAll = useCallback(() => {
    for (const r of roots ?? []) if (open.has(r.path)) void load(r.path);
    for (const p of open) void load(p);
  }, [roots, open, load]);
  useEffect(() => { refreshAll(); }, [roots, hidden]); // eslint-disable-line react-hooks/exhaustive-deps

  const saveRoots = async (list: string[]) => {
    try {
      const r = await put<{ roots: Root[] }>("/local/roots", { roots: list });
      setRoots(r.roots);
      setErr(null);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const addRoot = async (p: string) => {
    await saveRoots([...(roots ?? []).map((r) => r.path), p]);
    setOpen((o) => new Set(o).add(p));
    void load(p);
  };
  const toggle = (p: string) => setOpen((o) => {
    const n = new Set(o);
    if (n.has(p)) n.delete(p);
    else { n.add(p); void load(p); }
    return n;
  });
  const parentOf = (p: string) => p.slice(0, p.lastIndexOf("/")) || "/";
  const create = async (dir: string, kind: "file" | "dir") => {
    const name = window.prompt(kind === "file" ? `New file in ${dir}:` : `New folder in ${dir}:`);
    if (!name?.trim()) return;
    const p = `${dir.replace(/\/$/, "")}/${name.trim()}`;
    try {
      if (kind === "file") await put("/local/file", { path: p, text: "", create: true });
      else await post("/local/mkdir", { path: p });
      setOpen((o) => new Set(o).add(dir));
      await load(dir);
      if (kind === "file") onOpen(p);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const rename = async (it: Item) => {
    const to = window.prompt("Rename / move to (absolute path):", it.path);
    if (!to?.trim() || to.trim() === it.path) return;
    try {
      await post("/local/rename", { path: it.path, new_path: to.trim() });
      onClosedFile(it.path, to.trim());
      await load(parentOf(it.path));
      if (parentOf(to.trim()) !== parentOf(it.path)) void load(parentOf(to.trim()));
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const remove = async (it: Item) => {
    if (!window.confirm(`Delete ${it.path}${it.dir ? " and everything in it" : ""}? This cannot be undone.`)) return;
    try {
      await post("/local/delete", { path: it.path });
      onClosedFile(it.path, null);
      await load(parentOf(it.path));
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const render = (dir: string, depth: number): React.ReactNode => {
    const items = dirs[dir];
    if (!items) return <div className="muted small local-row" style={{ paddingLeft: depth * 12 + 20 }}>Loading…</div>;
    if (!items.length) return <div className="muted small local-row" style={{ paddingLeft: depth * 12 + 20 }}>(empty)</div>;
    return items.map((it) => (
      <div key={it.path}>
        <div className={`local-row ${it.path === activePath ? "selected" : ""}`} style={{ paddingLeft: depth * 12 + 6 }} title={it.path}
          onClick={() => (it.dir ? toggle(it.path) : onOpen(it.path))} data-testid="local-item" data-path={it.path}>
          <span className="local-twisty">{it.dir ? (open.has(it.path) ? "▾" : "▸") : ""}</span>
          <span className="local-icon">{it.dir ? "📁" : "📄"}</span>
          <span className="local-name">{it.name}{it.link ? " ↪" : ""}</span>
          <span className="local-actions">
            {it.dir && <button className="icon-btn small" title="New file" onClick={(e) => { e.stopPropagation(); void create(it.path, "file"); }}>+</button>}
            <button className="icon-btn small" title="Rename / move" onClick={(e) => { e.stopPropagation(); void rename(it); }}>✎</button>
            <button className="icon-btn small" title="Delete" onClick={(e) => { e.stopPropagation(); void remove(it); }} data-testid="local-delete">🗑</button>
          </span>
        </div>
        {it.dir && open.has(it.path) && render(it.path, depth + 1)}
      </div>
    ));
  };
  return (
    <div className="local-panel" data-testid="local-panel">
      <div className="local-head">
        <b>Local files</b>
        <span className="spacer" />
        <button className="icon-btn small" title="Open a folder" onClick={() => setPick(true)} data-testid="local-open-folder">📂</button>
        <button className={`icon-btn small ${hidden ? "on" : ""}`} title={hidden ? "Hide hidden files" : "Show hidden files"} onClick={() => setHidden(!hidden)}>.*</button>
        <button className="icon-btn small" title="Refresh" onClick={refreshAll}>↻</button>
      </div>
      <form className="local-file-open" onSubmit={(e) => { e.preventDefault(); if (filePath.trim()) { onOpen(filePath.trim()); setFilePath(""); } }}>
        <input value={filePath} onChange={(e) => setFilePath(e.target.value)} placeholder="Open a file: /absolute/path" className="mono" data-testid="local-open-file" />
      </form>
      {err && <div className="error-text small local-pad">{err} <button className="icon-btn small" onClick={() => setErr(null)}>×</button></div>}
      <div className="local-tree">
        {roots?.length === 0 && (
          <div className="muted small local-pad">
            Browse and edit any folder or file on the server — like a file manager, without the wiki's discussions.
            <div className="row"><button className="btn small primary" onClick={() => setPick(true)} data-testid="local-open-folder-empty">Open a folder…</button></div>
          </div>
        )}
        {roots?.map((r) => (
          <div key={r.path}>
            <div className="local-row root" onClick={() => toggle(r.path)} title={r.path} data-testid="local-root" data-path={r.path}>
              <span className="local-twisty">{open.has(r.path) ? "▾" : "▸"}</span>
              <b className="local-name">{r.name}</b>
              {!r.exists && <span className="warn small"> missing</span>}
              <span className="local-actions">
                <button className="icon-btn small" title="New file" onClick={(e) => { e.stopPropagation(); void create(r.path, "file"); }} data-testid="local-new-file">+</button>
                <button className="icon-btn small" title="New folder" onClick={(e) => { e.stopPropagation(); void create(r.path, "dir"); }}>⊞</button>
                <button className="icon-btn small" title="Close this folder (nothing is deleted)" onClick={(e) => { e.stopPropagation(); void saveRoots((roots ?? []).filter((x) => x.path !== r.path).map((x) => x.path)); }} data-testid="local-close-root">✕</button>
              </span>
            </div>
            {open.has(r.path) && r.exists && render(r.path, 1)}
          </div>
        ))}
      </div>
      {pick && (
        <FolderPicker title="Open a folder" hint="Browse and edit its files (no wiki features)." confirmLabel="Open this folder"
          onClose={() => setPick(false)} onChoose={(p) => { setPick(false); void addRoot(p); }} />
      )}
    </div>
  );
}
