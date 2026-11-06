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

/** "Add folder to window": browse directories on the server (where the wikis live) and open one as a new root. */
export default function AddFolderDialog({ start, onAdded, onClose }: { start: string; onAdded: (w: WorkspaceInfo) => void; onClose: () => void }) {
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

  const add = async () => {
    if (!listing) return;
    setBusy(true);
    setError(null);
    try {
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
          <span>Add a folder to this window</span>
          <button className="icon-btn" onClick={onClose}>×</button>
        </div>
        <div className="muted small">
          Each folder is an independent wiki root with its own documents, discussions, agent sessions, locks, reviews and Git state. You are
          browsing the server's file system (where your wikis and agents live).
        </div>
        <div className="row">
          <input className="path-input" value={typed} onChange={(e) => { edited.current = true; setTyped(e.target.value); }} onKeyDown={(e) => { if (e.key === "Enter") go(typed); }} data-testid="add-folder-path" />
          <button className="btn small" onClick={() => go(typed)}>Go</button>
          <label className="small"><input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} /> hidden</label>
        </div>
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
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={`Display name (default: ${folderName})`} data-testid="add-folder-name" />
          <button className="btn primary" disabled={busy || stale || !!listing?.open_as} onClick={() => void add()} data-testid="add-folder-submit" title={stale ? "Press Enter / Go to open the typed path first" : undefined}>
            {listing?.open_as ? "Already open" : busy ? "Adding…" : loading ? "Loading…" : "Add folder"}
          </button>
        </div>
        {error && <div className="error-text" role="alert">{error}</div>}
      </div>
    </div>
  );
}
