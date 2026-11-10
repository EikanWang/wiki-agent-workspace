import { useEffect, useState } from "react";
import { ApiError, get, put } from "../api";

/**
 * Extra reference folders for the agent (server paths): named in its prompt so it reads them for background.
 * scope: `repo:<owner>/<name>` (that repository's PR tabs) or `ws:<folder id>` (documents of that wiki folder).
 */
export default function ContextFolders({ scope, title, onClose }: { scope: string; title: string; onClose: () => void }) {
  const [dirs, setDirs] = useState<string[] | null>(null);
  const [path, setPath] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    get<{ dirs: string[] }>(`/agent-context?scope=${encodeURIComponent(scope)}`).then((r) => setDirs(r.dirs)).catch((e) => setErr((e as ApiError).message));
  }, [scope]);
  const save = async (next: string[]) => {
    setBusy(true);
    try {
      const r = await put<{ dirs: string[] }>(`/agent-context?scope=${encodeURIComponent(scope)}`, { dirs: next });
      setDirs(r.dirs);
      setErr(null);
      return true;
    } catch (e) {
      setErr((e as ApiError).message);
      return false;
    } finally {
      setBusy(false);
    }
  };
  const add = async () => {
    if (!path.trim() || !dirs) return;
    if (await save([...dirs, path.trim()])) setPath("");
  };
  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet context-folders" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Reference folders" data-testid="context-folders">
        <div className="row"><b>Reference folders</b><span className="spacer" /><button className="icon-btn" onClick={onClose} aria-label="Close">×</button></div>
        <div className="muted small">{title} The agent is told to read these folders on the server for background (it does not change them).</div>
        {dirs === null && !err && <div className="muted small">Loading…</div>}
        {dirs?.length === 0 && <div className="muted small">None yet.</div>}
        {dirs?.map((d) => (
          <div key={d} className="reviews-settings-item" data-testid="context-folder">
            <span className="mono small">{d}</span>
            <button className="icon-btn small" disabled={busy} onClick={() => void save(dirs.filter((x) => x !== d))} aria-label={`Remove ${d}`} data-testid="context-folder-remove">×</button>
          </div>
        ))}
        <div className="reviews-settings-row">
          <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="/absolute/path/on/the/server" onKeyDown={(e) => { if (e.key === "Enter") void add(); }} data-testid="context-folder-input" />
          <button className="btn small primary" disabled={busy || !path.trim() || dirs === null} onClick={() => void add()} data-testid="context-folder-add">Add</button>
        </div>
        {err && <div className="tree-error small" role="alert" data-testid="context-folder-error">{err}</div>}
      </div>
    </div>
  );
}
