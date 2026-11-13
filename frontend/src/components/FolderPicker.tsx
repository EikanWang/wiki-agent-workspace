import { useEffect, useRef, useState } from "react";
import { ApiError, WorkspaceInfo, get } from "../api";
import { FileIcon } from "./FileIcon";

export interface DirListing {
  path: string;
  parent: string | null;
  is_git: boolean;
  open_as: string | null;
  entries: { name: string; path: string; is_git: boolean }[];
}

/** Clickable path segments: /local_disk › eikan › rel */
export function Breadcrumbs({ path, onGo }: { path: string; onGo: (p: string) => void }) {
  const parts = path.split("/").filter(Boolean);
  return (
    <div className="breadcrumbs small" data-testid="folder-breadcrumbs">
      <button className="link-btn" onClick={() => onGo("/")} title="/">/</button>
      {parts.map((p, i) => {
        const to = "/" + parts.slice(0, i + 1).join("/");
        return (
          <span key={to}>
            {i > 0 && <span className="muted"> › </span>}
            <button className="link-btn" onClick={() => onGo(to)} title={to}>{p}</button>
          </span>
        );
      })}
    </div>
  );
}

/** Quick places: home, the file system root, and the folders open in this window (and where they live). */
export function Places({ onGo }: { onGo: (p: string) => void }) {
  const [roots, setRoots] = useState<{ name: string; root: string }[]>([]);
  useEffect(() => {
    get<{ workspaces: WorkspaceInfo[] }>("/workspaces").then((r) => setRoots(r.workspaces.filter((w) => w.available).map((w) => ({ name: w.name, root: w.root })))).catch(() => undefined);
  }, []);
  const parents = [...new Set(roots.map((r) => r.root.replace(/\/[^/]+\/?$/, "") || "/"))].filter((p) => !roots.some((r) => r.root === p));
  return (
    <div className="places small" data-testid="folder-places">
      <span className="muted">Go to:</span>
      <button className="chip-btn" onClick={() => onGo("~")} data-testid="place-home">~ home</button>
      <button className="chip-btn" onClick={() => onGo("/")}>/</button>
      {roots.map((r) => <button key={r.root} className="chip-btn" onClick={() => onGo(r.root)} title={r.root}>{r.name}</button>)}
      {parents.map((p) => <button key={p} className="chip-btn" onClick={() => onGo(p)} title={p}>{p.split("/").filter(Boolean).pop() ?? "/"}/</button>)}
    </div>
  );
}

/**
 * Browse the server's file system and choose a folder (used for reference folders, a repository's local checkout,
 * …). Click a folder to enter it; "Choose" takes the folder shown. `requireGit`: only Git checkouts can be chosen.
 */
export default function FolderPicker({ title, hint, start, requireGit = false, confirmLabel = "Choose this folder", onChoose, onClose }: {
  title: string;
  hint?: string;
  start?: string;
  requireGit?: boolean;
  confirmLabel?: string;
  onChoose: (path: string) => void;
  onClose: () => void;
}) {
  const [path, setPath] = useState(start || "~");
  const [typed, setTyped] = useState(start || "");
  const [listing, setListing] = useState<DirListing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hidden, setHidden] = useState(false);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const edited = useRef(false);
  const input = useRef<HTMLInputElement>(null);
  // the listing shown may update the path box when a folder was asked for (Go, a click); a listing that comes by
  // itself (the fallback to home) does not touch the box while you are in it (it would mix with what you type)
  const asked = useRef(true);
  const go = (p: string) => {
    edited.current = false;
    asked.current = true;
    if (p !== path) setLoading(true); // at once: "Choose" must not take the folder still shown while the new one loads
    setFilter("");
    setPath(p);
  };
  useEffect(() => {
    let alive = true;
    setLoading(true);
    get<DirListing>(`/fs/dirs?path=${encodeURIComponent(path)}&hidden=${hidden}`)
      .then((l) => {
        if (!alive) return;
        setListing(l);
        if (!edited.current && (asked.current || document.activeElement !== input.current)) setTyped(l.path);
        asked.current = false;
        setError(null);
      })
      .catch((e) => {
        if (!alive) return;
        setError((e as ApiError).message);
        // a start path that is gone: begin at home instead (unless another folder was asked for meanwhile)
        if (!listing && path !== "~") setPath((cur) => { if (cur === path) asked.current = false; return cur === path ? "~" : cur; });
      })
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, hidden]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const entries = (listing?.entries ?? []).filter((e) => !filter || e.name.toLowerCase().includes(filter.toLowerCase()));
  const ok = !!listing && !loading && (!requireGit || listing.is_git);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal folder-picker" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={title} data-testid="folder-picker">
        <div className="modal-head">
          <span>{title}</span>
          <button className="icon-btn" onClick={onClose} aria-label="Close">×</button>
        </div>
        {hint && <div className="muted small">{hint} You are browsing the server's file system.</div>}
        <Places onGo={go} />
        <div className="row">
          <input ref={input} className="path-input" value={typed} onChange={(e) => { edited.current = true; setTyped(e.target.value); }} onKeyDown={(e) => { if (e.key === "Enter") go(e.currentTarget.value.trim() || "~"); }}
            placeholder="/absolute/path (Enter to open)" data-testid="folder-picker-path" />
          <button className="btn small" onClick={() => go(typed.trim() || "~")}>Go</button>
          <label className="small"><input type="checkbox" checked={hidden} onChange={(e) => setHidden(e.target.checked)} /> hidden</label>
        </div>
        {listing && <Breadcrumbs path={listing.path} onGo={go} />}
        <input className="dir-filter" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter the folders shown…" data-testid="folder-picker-filter" />
        <div className="dir-list" data-testid="folder-picker-list">
          {listing?.parent && <button className="list-item" onClick={() => go(listing.parent!)} data-testid="folder-picker-up"><span>‹ ..</span></button>}
          {entries.map((e) => (
            <button key={e.path} className="list-item" onClick={() => go(e.path)} data-testid="folder-picker-entry" data-name={e.name}>
              <span className="dir-entry"><FileIcon item={{ type: "dir", kind: "dir" }} /> {e.name}</span>
              {e.is_git && <span className="git-badge clean">git</span>}
            </button>
          ))}
          {listing && entries.length === 0 && <div className="muted small pad">{filter ? "No folder matches." : "No subfolders."}</div>}
          {loading && !listing && <div className="muted small pad">Loading…</div>}
        </div>
        {listing && (
          <div className="small" data-testid="folder-picker-selected">
            Selected: <code>{listing.path}</code>{listing.is_git ? " · Git repository" : requireGit ? " · not a Git checkout — open the checkout's top folder" : ""}
          </div>
        )}
        {error && <div className="error-text small" role="alert">{error}</div>}
        <div className="row">
          <span className="spacer" />
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={!ok} onClick={() => listing && onChoose(listing.path)} data-testid="folder-picker-choose">{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}
