import RefreshButton from "./RefreshButton";
import { useCallback, useEffect, useState } from "react";
import { FileItem, GitSummary, WorkspaceInfo, getIn } from "../api";
import { FileIcon, KIND_LABEL, formatSize } from "./FileIcon";

interface DirState {
  items: FileItem[];
  loading: boolean;
  error: string | null;
}

const key = (ws: string, path: string) => `${ws}\u0000${path}`;

/**
 * Multi-root file tree (VS Code-style "folders in a workspace"). Each root folder is a top-level node with its own
 * Git badge and actions; entries below it are loaded with that root's X-Workspace scope, so roots never mix.
 */
export default function FileTree({
  workspaces,
  activeWorkspace,
  selectedPath,
  onOpen,
  onOpenDiff,
  onToggleLock,
  onAddFolder,
  onRemoveFolder,
  refreshKey,
}: {
  workspaces: WorkspaceInfo[];
  activeWorkspace: string | null;
  selectedPath: string | null;
  onOpen: (workspace: string, path: string) => void;
  /** Open the diff review for a file or folder of a root (undefined disables the action). */
  onOpenDiff?: (workspace: string, path: string, isDir: boolean) => void;
  /** Toggle a read-only lock on a file or folder of a root. */
  onToggleLock?: (workspace: string, path: string, locked: boolean) => void;
  onAddFolder?: () => void;
  onRemoveFolder?: (workspace: WorkspaceInfo) => void;
  refreshKey: number;
}) {
  const [dirs, setDirs] = useState<Record<string, DirState>>({});
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("waw.treeExpanded") || "null");
      if (Array.isArray(saved)) return new Set(saved as string[]);
    } catch {
      /* ignore */
    }
    return new Set<string>();
  });
  const multi = workspaces.length > 1;

  // every available root is expanded by default the first time it is seen
  useEffect(() => {
    setExpanded((s) => {
      const n = new Set(s);
      let changed = false;
      for (const w of workspaces) {
        if (!w.available) continue;
        const k = key(w.id, "");
        const seen = `${k}\u0001seen`;
        if (!n.has(seen)) {
          n.add(seen);
          n.add(k);
          changed = true;
        }
      }
      return changed ? n : s;
    });
  }, [workspaces]);

  useEffect(() => {
    localStorage.setItem("waw.treeExpanded", JSON.stringify([...expanded]));
  }, [expanded]);

  const load = useCallback(async (ws: string, path: string) => {
    const k = key(ws, path);
    setDirs((d) => ({ ...d, [k]: { items: d[k]?.items ?? [], loading: true, error: null } }));
    try {
      const r = await getIn<{ items: FileItem[]; git: GitSummary }>(ws, `/files?path=${encodeURIComponent(path)}`);
      setDirs((d) => ({ ...d, [k]: { items: r.items, loading: false, error: null } }));
    } catch (e) {
      setDirs((d) => ({ ...d, [k]: { items: [], loading: false, error: (e as Error).message } }));
    }
  }, []);

  const expandedEntries = () =>
    [...expanded].filter((k) => !k.endsWith("\u0001seen")).map((k) => {
      const [ws, path] = k.split("\u0000");
      return { ws, path };
    }).filter(({ ws }) => workspaces.some((w) => w.id === ws && w.available));

  useEffect(() => {
    for (const { ws, path } of expandedEntries()) void load(ws, path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey, workspaces]);

  useEffect(() => {
    for (const { ws, path } of expandedEntries()) if (!dirs[key(ws, path)]) void load(ws, path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded]);

  const toggle = (ws: string, path: string) => {
    setExpanded((s) => {
      const n = new Set(s);
      const k = key(ws, path);
      if (n.has(k)) n.delete(k);
      else n.add(k);
      return n;
    });
  };

  const renderDir = (ws: WorkspaceInfo, path: string, depth: number, first: boolean): React.ReactNode => {
    const st = dirs[key(ws.id, path)];
    if (!st) return <div className="tree-loading" style={{ paddingLeft: depth * 14 + 8 }}>Loading…</div>;
    if (st.error) return <div className="tree-error" style={{ paddingLeft: depth * 14 + 8 }}>{st.error}</div>;
    if (st.items.length === 0 && !st.loading) return <div className="tree-empty" style={{ paddingLeft: depth * 14 + 8 }}>(empty)</div>;
    // test ids stay path-based for the first root; other roots are distinguished by data-ws
    const tid = (p: string) => (first ? `tree-file-${p}` : `tree-file-${ws.name}:${p}`);
    return st.items.map((it) => {
      const tip = [
        it.path,
        KIND_LABEL[it.type === "dir" ? "dir" : it.kind] ?? it.kind,
        it.symlink ? `symlink → ${it.link_target ?? "?"}${it.escapes ? " (outside the folder)" : ""}` : "",
        it.type === "file" ? formatSize(it.size) : "",
        it.git_status ? `git: ${it.git_status}` : "",
      ]
        .filter(Boolean)
        .join("\n");
      if (it.type === "dir") {
        const isOpen = expanded.has(key(ws.id, it.path));
        return (
          <div key={it.path}>
            <div className={`tree-row dir ${it.escapes ? "escapes" : ""}`} style={{ paddingLeft: depth * 14 + 8 }} title={tip} data-ws={ws.id}>
              <button className="tree-main" onClick={() => toggle(ws.id, it.path)}>
                <span className="chev">{isOpen ? "⌄" : "›"}</span>
                <FileIcon item={it} open={isOpen} />
                <span className="name">{it.name}</span>
              </button>
              {onOpenDiff && it.git_status && it.git_status !== "clean" && (
                <button className="tree-action" onClick={() => onOpenDiff(ws.id, it.path, true)} title="Review all changes in this folder" data-testid={first ? `tree-diff-${it.path}` : undefined}>±</button>
              )}
              {onToggleLock && !it.lock_inherited && (
                <button className="tree-action" onClick={() => onToggleLock(ws.id, it.path, !it.locked)} title={it.locked ? "Unlock this folder" : "Lock this folder (read-only for the app and app-driven agents)"} data-testid={first ? `tree-lock-${it.path}` : undefined}>{it.locked ? "🔓" : "🔒"}</button>
              )}
              {it.locked && <span className={`lock-badge ${it.lock_inherited ? "inherited" : ""}`} title={it.lock_inherited ? "Locked by a parent folder" : "Locked"}>🔒</span>}
              {it.git_status && it.git_status !== "clean" && <span className={`git-badge ${it.git_status}`}>{gitLabel(it.git_status)}</span>}
            </div>
            {isOpen && renderDir(ws, it.path, depth + 1, first)}
          </div>
        );
      }
      const openable = it.openable === true;
      const selected = activeWorkspace === ws.id && selectedPath === it.path;
      return (
        <div key={it.path} className={`tree-row file ${selected ? "selected" : ""} ${openable ? "" : "inert"} ${it.kind}`} style={{ paddingLeft: depth * 14 + 22 }} title={tip} data-testid={tid(it.path)} data-kind={it.kind} data-ws={ws.id}>
          <button className="tree-main" onClick={() => (openable ? onOpen(ws.id, it.path) : undefined)} disabled={!openable} aria-disabled={!openable}>
            <FileIcon item={it} />
            <span className="name">{it.name}</span>
          </button>
          {onOpenDiff && it.git_status && it.git_status !== "clean" && it.git_status !== "untracked" && (
            <button className="tree-action" onClick={() => onOpenDiff(ws.id, it.path, false)} title="Review changes in this file" data-testid={first ? `tree-diff-${it.path}` : undefined}>±</button>
          )}
          {onToggleLock && !it.lock_inherited && (
            <button className="tree-action" onClick={() => onToggleLock(ws.id, it.path, !it.locked)} title={it.locked ? "Unlock this file" : "Lock this file (read-only for the app and app-driven agents)"} data-testid={first ? `tree-lock-${it.path}` : undefined}>{it.locked ? "🔓" : "🔒"}</button>
          )}
          {it.locked && <span className={`lock-badge ${it.lock_inherited ? "inherited" : ""}`} title={it.lock_inherited ? "Locked by a parent folder" : "Locked"}>🔒</span>}
          {it.git_status && <span className={`git-badge ${it.git_status}`}>{gitLabel(it.git_status)}</span>}
        </div>
      );
    });
  };

  return (
    <div className="file-tree">
      <div className="tree-header">
        <span className="ws-name">{multi ? `${workspaces.filter((w) => w.available).length} folders` : workspaces[0]?.name ?? ""}</span>
        <span className="spacer" />
        {onAddFolder && <button className="icon-btn" onClick={onAddFolder} title="Add a folder (another wiki) to this window" data-testid="tree-add-folder">＋</button>}
        <RefreshButton onRefresh={() => Promise.all(expandedEntries().map(({ ws, path }) => load(ws, path)))} title="Refresh file tree" testId="tree-refresh" />
      </div>
      <div className="tree-body">
        {workspaces.map((ws, i) => {
          const open = expanded.has(key(ws.id, ""));
          const active = ws.id === activeWorkspace;
          return (
            <div key={ws.id} className={`tree-root-group ${active ? "active" : ""}`} data-testid="tree-root" data-ws={ws.id}>
              <div className={`tree-root ${ws.available ? "" : "unavailable"}`} title={`${ws.root}${ws.git?.branch ? `\nbranch: ${ws.git.branch}` : ""}${ws.configured ? "\n(defined in config.toml)" : ""}`}>
                <button className="tree-main" onClick={() => ws.available && toggle(ws.id, "")} disabled={!ws.available}>
                  <span className="chev">{open ? "⌄" : "›"}</span>
                  <FileIcon item={{ type: "dir", kind: "dir" }} open={open} />
                  <span className="name root-name">{ws.name}</span>
                  {ws.git?.branch && <span className="git-badge clean" title="current branch">{ws.git.branch}</span>}
                  {!ws.available && <span className="git-badge deleted">missing</span>}
                </button>
                {ws.available && onOpenDiff && (
                  <button className="tree-action" onClick={() => onOpenDiff(ws.id, "", true)} title="Review all uncommitted changes in this folder" data-testid={i === 0 ? "tree-diff-root" : `tree-diff-root-${ws.name}`}>±</button>
                )}
                {onRemoveFolder && !ws.configured && (
                  <button className="tree-action remove" onClick={() => onRemoveFolder(ws)} title="Remove this folder from the window (its discussions and history are kept)" data-testid={`tree-remove-${ws.name}`}>✕</button>
                )}
              </div>
              {ws.available && open && renderDir(ws, "", multi ? 1 : 0, i === 0)}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function gitLabel(s: string): string {
  return s;
}
