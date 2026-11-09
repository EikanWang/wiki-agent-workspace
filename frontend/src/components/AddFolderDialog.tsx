import { useEffect, useRef, useState } from "react";
import { ApiError, WorkspaceInfo, get, post } from "../api";
import { FileIcon } from "./FileIcon";

interface DirListing {
  path: string;
  parent: string | null;
  is_git: boolean;
  open_as: string | null;
  entries: { name: string; path: string; is_git: boolean }[];
}

/**
 * Browse directories on the server (where the wikis live). Mode "add": open one as a new root of this window.
 * Mode "new-workspace": create a named workspace whose first folder is the chosen one (VS Code "New Window").
 * "New folder" creates a directory (optionally a Git repository) in the folder shown.
 */
export default function AddFolderDialog({ start, onAdded, onClose, mode = "add", onWorkspaceCreated }: { start: string; onAdded: (w: WorkspaceInfo) => void; onClose: () => void; mode?: "add" | "new-workspace"; onWorkspaceCreated?: () => void }) {
  const [wsName, setWsName] = useState("");
  const [copyCurrent, setCopyCurrent] = useState(false);
  const [newDir, setNewDir] = useState<string | null>(null); // name being typed for "New folder"
  const [newDirGit, setNewDirGit] = useState(true);
  const [path, setPath] = useState(start);
  const [typed, setTyped] = useState(start);
  const [listing, setListing] = useState<DirListing | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showHidden, setShowHidden] = useState(false);
  const [loading, setLoading] = useState(false);
  const edited = useRef(false); // the user typed in the path box since the last navigation
  const go = (p: string) => {
    edited.current = false;
    setPath(p);
  };

  useEffect(() => {
    let alive = true;
    setLoading(true);
    get<DirListing>(`/fs/dirs?path=${encodeURIComponent(path)}&hidden=${showHidden}`)
      .then((l) => {
        if (!alive) return;
        setListing(l);
        if (!edited.current) setTyped(l.path); // never overwrite what the user is typing
        setError(null);
      })
      .catch((e) => alive && setError((e as ApiError).message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [path, showHidden]);
  // never add a folder other than the one shown: the path box must match the listed folder, and nothing may be loading
  const stale = loading || !listing || typed.replace(/\/+$/, "") !== listing.path.replace(/\/+$/, "");

  const createDir = async () => {
    if (!listing || !newDir?.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const r = await post<{ path: string; is_git: boolean; git_error: string | null }>("/fs/mkdir", { parent: listing.path, name: newDir.trim(), git: newDirGit });
      setNewDir(null);
      go(r.path); // show (and select) the new folder
      if (r.git_error) setError(`Folder created, but git init failed: ${r.git_error}`);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const add = async () => {
    if (!listing) return;
    setBusy(true);
    setError(null);
    try {
      if (mode === "new-workspace") {
        await post("/named-workspaces", { name: wsName.trim(), root: listing.path, copy_current: copyCurrent });
        onWorkspaceCreated?.();
        return;
      }
      const w = await post<WorkspaceInfo>("/workspaces", { root: listing.path, name: name.trim() || null });
      onAdded(w);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const folderName = listing ? listing.path.split("/").filter(Boolean).pop() ?? listing.path : "";
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} data-testid="add-folder-dialog">
        <div className="modal-head">
          <span>{mode === "new-workspace" ? "New workspace" : "Add a folder to this window"}</span>
          <button className="icon-btn" onClick={onClose}>×</button>
        </div>
        {mode === "new-workspace" ? (
          <>
            <div className="muted small">
              A workspace is a named set of folders with its own document tabs and terminals, like a VS Code window. Choose its first folder
              (add more later with ＋ in the file tree). Discussions and history stay with each folder, whichever workspace shows it.
            </div>
            <div className="row">
              <input value={wsName} onChange={(e) => setWsName(e.target.value)} placeholder="Workspace name, e.g. Xe3 display" autoFocus data-testid="new-workspace-name" />
              <label className="small" title="Start with the folders this window shows now, plus the one chosen below">
                <input type="checkbox" checked={copyCurrent} onChange={(e) => setCopyCurrent(e.target.checked)} data-testid="new-workspace-copy" /> include this window's folders
              </label>
            </div>
          </>
        ) : (
          <div className="muted small">
            Each folder is an independent wiki root with its own documents, discussions, agent sessions, locks, reviews and Git state. You are
            browsing the server's file system (where your wikis and agents live).
          </div>
        )}
        <div className="row">
          <input className="path-input" value={typed} onChange={(e) => { edited.current = true; setTyped(e.target.value); }} onKeyDown={(e) => { if (e.key === "Enter") go(typed); }} data-testid="add-folder-path" />
          <button className="btn small" onClick={() => go(typed)}>Go</button>
          <button className="btn small" onClick={() => setNewDir((v) => (v === null ? "" : null))} disabled={!listing} title={listing ? `Create a folder in ${listing.path}` : undefined} data-testid="add-folder-newdir">＋ New folder</button>
          <label className="small"><input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} /> hidden</label>
        </div>
        {newDir !== null && listing && (
          <div className="row new-dir-row">
            <input value={newDir} onChange={(e) => setNewDir(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void createDir(); if (e.key === "Escape") setNewDir(null); }} placeholder={`New folder in ${listing.path}`} autoFocus data-testid="add-folder-newdir-name" />
            <label className="small"><input type="checkbox" checked={newDirGit} onChange={(e) => setNewDirGit(e.target.checked)} data-testid="add-folder-newdir-git" /> git init</label>
            <button className="btn small primary" disabled={busy || !newDir.trim()} onClick={() => void createDir()} data-testid="add-folder-newdir-create">Create</button>
          </div>
        )}
        <div className="dir-list" data-testid="add-folder-list">
          {listing?.parent && (
            <button className="list-item" onClick={() => go(listing.parent!)}><span>‹ ..</span></button>
          )}
          {listing?.entries.map((e) => (
            <button key={e.path} className="list-item" onClick={() => go(e.path)} onDoubleClick={() => go(e.path)} data-testid="add-folder-entry">
              <span className="dir-entry"><FileIcon item={{ type: "dir", kind: "dir" }} /> {e.name}</span>
              {e.is_git && <span className="git-badge clean">git</span>}
            </button>
          ))}
          {listing && listing.entries.length === 0 && <div className="muted small pad">No subfolders.</div>}
        </div>
        {listing && (
          <div className="row">
            <span className="small" data-testid="add-folder-selected">Selected: <code>{listing.path}</code>{listing.is_git ? " · Git repository" : " · not a Git repository (diff/commit unavailable)"}</span>
          </div>
        )}
        <div className="row">
          {mode === "add" && <input value={name} onChange={(e) => setName(e.target.value)} placeholder={`Display name (default: ${folderName})`} data-testid="add-folder-name" />}
          {mode === "add" ? (
            <button className="btn primary" disabled={busy || stale || !!listing?.open_as} onClick={() => void add()} data-testid="add-folder-submit" title={stale ? "Press Enter / Go to open the typed path first" : undefined}>
              {listing?.open_as ? "Already open" : busy ? "Adding…" : loading ? "Loading…" : "Add folder"}
            </button>
          ) : (
            <button className="btn primary" disabled={busy || stale || !wsName.trim()} onClick={() => void add()} data-testid="new-workspace-submit" title={!wsName.trim() ? "Give the workspace a name" : stale ? "Press Enter / Go to open the typed path first" : undefined}>
              {busy ? "Creating…" : `Create workspace with ${folderName || "this folder"}`}
            </button>
          )}
        </div>
        {error && <div className="error-text" role="alert">{error}</div>}
      </div>
    </div>
  );
}
