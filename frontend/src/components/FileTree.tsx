import RefreshButton from "./RefreshButton";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, FileItem, GitSummary, WorkspaceInfo, getIn, post } from "../api";
import { FileIcon, KIND_LABEL, formatSize } from "./FileIcon";
import { NewFileIcon, NewFolderIcon, PlusIcon } from "./Icons";

interface DirState {
  items: FileItem[];
  loading: boolean;
  /** last refresh failed; `loaded` tells whether `items` still hold an earlier good listing */
  error: string | null;
  loaded: boolean;
}

const key = (ws: string, path: string) => `${ws}\u0000${path}`;
const parentOf = (path: string) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");
const isUnder = (path: string, dir: string) => dir === "" || path === dir || path.startsWith(dir + "/");

const LOAD_TIMEOUT_MS = 20_000;
const MAX_PARALLEL = 4; // the browser allows ~6 connections per host and the event stream holds one
const EVENT_THROTTLE_MS = 1_000; // file changes arriving in bursts are folded into one refresh

/** The watcher's `tree_changed` events, handed from App's event stream to the tree (no second EventSource). */
type TreeChange = { ws: string | null; dirs: string[] | null };
const treeListeners = new Set<(c: TreeChange) => void>();
export function notifyTreeChanged(c: TreeChange): void {
  for (const l of treeListeners) l(c);
}

async function inParallel<T>(items: T[], limit: number, fn: (t: T) => Promise<unknown>): Promise<void> {
  let i = 0;
  const worker = async () => {
    while (i < items.length) await fn(items[i++]);
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
}

/**
 * Multi-root file tree (VS Code-style "folders in a workspace"). Each root folder is a top-level node with its own
 * Git badge and actions; entries below it are loaded with that root's X-Workspace scope, so roots never mix.
 *
 * Only *visible* folders are ever requested: a folder is visible when its root and every parent are expanded and
 * listed. Expanded folders are remembered across reloads; ones that disappeared are forgotten when their parent is
 * listed (or when they answer 404). Refreshes never overlap: one runs at a time, at most MAX_PARALLEL requests
 * each, and file-change events only reload the folders that changed.
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
  onCreated,
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
  /** a file or folder was created from the tree (open new files, …) */
  onCreated?: (workspace: string, path: string, kind: "file" | "dir", openable: boolean) => void;
  /** bumped for changes that can affect any folder (commit, lock, agent turn finished): reload what is shown */
  refreshKey: number;
}) {
  const [dirs, setDirs] = useState<Record<string, DirState>>({});
  const dirsRef = useRef(dirs); // read synchronously by the refresh loop
  const [expanded, setExpandedState] = useState<Set<string>>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("waw.treeExpanded") || "null");
      if (Array.isArray(saved)) return new Set(saved as string[]);
    } catch {
      /* ignore */
    }
    return new Set<string>();
  });
  const expandedRef = useRef(expanded);
  const workspacesRef = useRef(workspaces);
  workspacesRef.current = workspaces;
  const multi = workspaces.length > 1;

  const putDir = (k: string, st: DirState | null) => {
    const next = { ...dirsRef.current };
    if (st) next[k] = st;
    else delete next[k];
    dirsRef.current = next;
    setDirs(next);
  };
  const setExpanded = (fn: (s: Set<string>) => Set<string>) => {
    const next = fn(expandedRef.current);
    if (next === expandedRef.current) return;
    expandedRef.current = next;
    setExpandedState(next);
  };

  // every available root is expanded by default the first time it is seen
  const wsSignature = workspaces.map((w) => `${w.id}:${w.available ? 1 : 0}`).join(",");
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsSignature]);

  useEffect(() => {
    localStorage.setItem("waw.treeExpanded", JSON.stringify([...expanded]));
  }, [expanded]);

  /** Forget a folder and everything remembered below it (it no longer exists). */
  const forget = (ws: string, path: string) => {
    if (path === "") return; // a root's availability is reported by the server, not guessed here
    setExpanded((s) => {
      const n = new Set([...s].filter((k) => {
        const [w, p] = k.split("\u0000");
        return !(w === ws && isUnder(p, path));
      }));
      return n.size === s.size ? s : n;
    });
    for (const k of Object.keys(dirsRef.current)) {
      const [w, p] = k.split("\u0000");
      if (w === ws && isUnder(p, path)) putDir(k, null);
    }
  };

  const load = useCallback(async (ws: string, path: string) => {
    const k = key(ws, path);
    const prev = dirsRef.current[k];
    putDir(k, { items: prev?.items ?? [], loading: true, error: null, loaded: prev?.loaded ?? false });
    try {
      const r = await getIn<{ items: FileItem[]; git: GitSummary }>(ws, `/files?path=${encodeURIComponent(path)}`, { timeoutMs: LOAD_TIMEOUT_MS });
      putDir(k, { items: r.items, loading: false, error: null, loaded: true });
      // remembered sub-folders that are gone (deleted, renamed, moved away) are forgotten
      const present = new Set(r.items.filter((it) => it.type === "dir").map((it) => it.path));
      for (const ek of expandedRef.current) {
        const [w, p] = ek.split("\u0000");
        if (w === ws && p !== path && !ek.endsWith("\u0001seen") && parentOf(p) === path && !present.has(p)) forget(ws, p);
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        forget(ws, path);
        return;
      }
      // keep showing what we had; the next refresh tries again
      putDir(k, { items: prev?.items ?? [], loading: false, error: (e as Error).message, loaded: prev?.loaded ?? false });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Expanded folders whose root and parents are expanded and listed, parents before children. */
  const visibleLevels = (): { ws: string; path: string }[][] => {
    const exp = expandedRef.current;
    const levels: { ws: string; path: string }[][] = [];
    let level = workspacesRef.current.filter((w) => w.available && exp.has(key(w.id, ""))).map((w) => ({ ws: w.id, path: "" }));
    while (level.length) {
      levels.push(level);
      const next: { ws: string; path: string }[] = [];
      for (const { ws, path } of level) {
        const st = dirsRef.current[key(ws, path)];
        if (!st?.loaded) continue;
        for (const it of st.items) if (it.type === "dir" && exp.has(key(ws, it.path))) next.push({ ws, path: it.path });
      }
      level = next;
    }
    return levels;
  };

  /**
   * Reload visible folders level by level (fresh parent listings decide which children are still there).
   * `only`: just these folders; `trees`: these folders (even if collapsed) and the visible folders inside them.
   */
  const reloadVisible = async (only: Map<string, Set<string>> | null, trees: Map<string, Set<string>> = new Map()) => {
    const done = new Set<string>();
    const roots = [...trees].flatMap(([ws, set]) => [...set].map((path) => ({ ws, path })));
    // the folders asked for by name first, whether expanded or not (a collapsed folder's cached listing is renewed)
    if (roots.length) {
      roots.forEach((t) => done.add(key(t.ws, t.path)));
      await inParallel(roots, MAX_PARALLEL, (t) => load(t.ws, t.path));
    }
    const inTree = (ws: string, path: string) => [...(trees.get(ws) ?? [])].some((t) => isUnder(path, t));
    for (;;) {
      const targets = visibleLevels().flat().filter(({ ws, path }) => {
        const k = key(ws, path);
        if (done.has(k)) return false;
        if (inTree(ws, path)) return true;
        if (only && !only.get(ws)?.has(path)) return false; // `only` empty: nothing outside the requested folders
        return true;
      });
      if (!targets.length) return;
      // parents first: the shallowest pending level, then look again
      const depth = (p: string) => (p === "" ? 0 : p.split("/").length);
      const min = Math.min(...targets.map((t) => depth(t.path)));
      const batch = targets.filter((t) => depth(t.path) === min);
      batch.forEach((t) => done.add(key(t.ws, t.path)));
      await inParallel(batch, MAX_PARALLEL, (t) => load(t.ws, t.path));
    }
  };

  // ---- one refresh at a time; requests made meanwhile are merged into the next run
  const running = useRef(false);
  const pendingAll = useRef(false);
  const pendingDirs = useRef(new Map<string, Set<string>>());
  const pendingTrees = useRef(new Map<string, Set<string>>());
  const waiters = useRef<(() => void)[]>([]);
  const throttle = useRef<number | undefined>(undefined);

  const pump = async () => {
    if (running.current) return;
    running.current = true;
    try {
      while (pendingAll.current || pendingDirs.current.size || pendingTrees.current.size) {
        const all = pendingAll.current;
        const only = all ? null : pendingDirs.current;
        const trees = pendingTrees.current;
        pendingAll.current = false;
        pendingDirs.current = new Map();
        pendingTrees.current = new Map();
        const mine = waiters.current;
        waiters.current = [];
        try {
          await reloadVisible(all ? null : only, trees);
        } finally {
          mine.forEach((r) => r());
        }
      }
    } finally {
      running.current = false;
    }
  };

  /** ↻ button: reload everything shown; resolves when that reload finished (at most one queued run later). */
  const refreshNow = () =>
    new Promise<void>((resolve) => {
      pendingAll.current = true;
      waiters.current.push(resolve);
      void pump();
    });

  /** ↻ on a folder: that folder (even collapsed) and the expanded folders inside it, nothing else. */
  const refreshFolder = (ws: string, path: string) =>
    new Promise<void>((resolve) => {
      const set = pendingTrees.current.get(ws) ?? new Set<string>();
      set.add(path);
      pendingTrees.current.set(ws, set);
      waiters.current.push(resolve);
      void pump();
    });

  const requestRefresh = (change: TreeChange | null) => {
    if (!change || change.dirs === null || change.ws === null) pendingAll.current = true;
    else {
      // the folder that changed and its parents (their Git badges summarize it)
      const set = pendingDirs.current.get(change.ws) ?? new Set<string>();
      for (const d of change.dirs) for (let p = d; ; p = parentOf(p)) {
        set.add(p);
        if (p === "") break;
      }
      pendingDirs.current.set(change.ws, set);
    }
    if (throttle.current === undefined) {
      throttle.current = window.setTimeout(() => {
        throttle.current = undefined;
        void pump();
      }, EVENT_THROTTLE_MS);
    }
  };

  useEffect(() => {
    const l = (c: TreeChange) => requestRefresh(c);
    treeListeners.add(l);
    return () => {
      treeListeners.delete(l);
      window.clearTimeout(throttle.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const firstRefresh = useRef(true);
  useEffect(() => {
    if (firstRefresh.current) {
      firstRefresh.current = false; // the first load comes from the effect below
      return;
    }
    requestRefresh(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey, wsSignature]);

  // newly visible folders (expanded by the user, or uncovered by a parent's listing) are loaded once
  useEffect(() => {
    const missing = visibleLevels().flat().filter(({ ws, path }) => !dirsRef.current[key(ws, path)]);
    void inParallel(missing, MAX_PARALLEL, (t) => load(t.ws, t.path));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded, dirs, wsSignature]);

  // ---- New file / New folder (VS Code explorer): an inline name box at the top of the folder
  const [creating, setCreating] = useState<{ ws: string; dir: string; kind: "file" | "dir" } | null>(null);
  const [newName, setNewName] = useState("");
  const [newError, setNewError] = useState<string | null>(null);
  const [newBusy, setNewBusy] = useState(false);
  const startCreate = (ws: string, dir: string, kind: "file" | "dir") => {
    setExpanded((s) => {
      const k = key(ws, dir);
      if (s.has(k)) return s;
      const n = new Set(s);
      n.add(k);
      return n;
    });
    setNewName("");
    setNewError(null);
    setCreating({ ws, dir, kind });
  };
  const submitCreate = async () => {
    if (!creating || !newName.trim() || newBusy) return;
    const rel = (creating.dir ? creating.dir + "/" : "") + newName.trim().replace(/^\/+|\/+$/g, "");
    setNewBusy(true);
    try {
      const r = await post<{ path: string; kind: "file" | "dir"; openable: boolean }>("/files", { path: rel, kind: creating.kind }, { workspace: creating.ws });
      const { ws, kind } = creating;
      setCreating(null);
      // show it: expand the folders on the way, then re-read the folder it was created in
      const parts = r.path.split("/");
      setExpanded((s) => {
        const n = new Set(s);
        for (let i = 1; i < parts.length; i++) n.add(key(ws, parts.slice(0, i).join("/")));
        if (kind === "dir") n.add(key(ws, r.path));
        return n;
      });
      await refreshFolder(ws, creating.dir);
      onCreated?.(ws, r.path, kind, r.openable);
    } catch (e) {
      setNewError((e as ApiError).message);
    } finally {
      setNewBusy(false);
    }
  };
  const newBox = (ws: string, dir: string, depth: number) =>
    creating && creating.ws === ws && creating.dir === dir ? (
      <div key="\u0000new">
        <div className="tree-new" style={{ paddingLeft: depth * 14 + 8 }} data-testid="tree-new">
          <FileIcon item={{ type: creating.kind, kind: creating.kind === "dir" ? "dir" : "file" }} />
          <input
            autoFocus
            value={newName}
            placeholder={creating.kind === "dir" ? "folder name" : "file name, e.g. notes.md (a/b.md creates folders)"}
            onChange={(e) => { setNewName(e.target.value); setNewError(null); }}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submitCreate();
              if (e.key === "Escape") setCreating(null);
            }}
            onBlur={() => { if (!newName.trim()) setCreating(null); }}
            aria-label={creating.kind === "dir" ? "New folder name" : "New file name"}
            data-testid="tree-new-name"
          />
        </div>
        {newError && <div className="tree-new-error" style={{ paddingLeft: depth * 14 + 8 }} role="alert" data-testid="tree-new-error">{newError}</div>}
      </div>
    ) : null;
  const newButtons = (ws: string, dir: string, testSuffix: string | undefined) => (
    <>
      <button className="tree-action new-entry" onClick={() => startCreate(ws, dir, "file")} title="New file" aria-label="New file" data-testid={testSuffix !== undefined ? `tree-new-file${testSuffix}` : undefined}><NewFileIcon /></button>
      <button className="tree-action new-entry" onClick={() => startCreate(ws, dir, "dir")} title="New folder" aria-label="New folder" data-testid={testSuffix !== undefined ? `tree-new-dir${testSuffix}` : undefined}><NewFolderIcon /></button>
    </>
  );

  const toggle = (ws: string, path: string) => {
    const k = key(ws, path);
    const opening = !expandedRef.current.has(k);
    setExpanded((s) => {
      const n = new Set(s);
      if (n.has(k)) n.delete(k);
      else n.add(k);
      return n;
    });
    // a cached listing may be old (changes only refresh visible folders): show it, and fetch it again
    if (opening && dirsRef.current[k]) void refreshFolder(ws, path);
  };

  /** A click on a row's empty space or badges acts like a click on its name; its buttons keep their own action. */
  const onRowClick = (e: React.MouseEvent, act: () => void) => {
    if ((e.target as Element).closest("button")) return;
    act();
  };

  const renderDir = (ws: WorkspaceInfo, path: string, depth: number, first: boolean): React.ReactNode => {
    const st = dirs[key(ws.id, path)];
    const pad = { paddingLeft: depth * 14 + 8 };
    if (!st || (!st.loaded && st.loading)) return <div className="tree-loading" style={pad}>Loading…</div>;
    const retry = <button className="link-btn" onClick={() => void load(ws.id, path)} data-testid="tree-retry">Retry</button>;
    if (st.error && !st.loaded) return <div className="tree-error" style={pad} data-testid="tree-error">Could not list this folder: {st.error}. {retry}</div>;
    const stale = st.error ? <div key="\u0000stale" className="tree-error stale" style={pad} data-testid="tree-error" title={st.error}>Could not refresh; showing the earlier listing. {retry}</div> : null;
    const box = newBox(ws.id, path, depth);
    if (st.items.length === 0) return box ?? stale ?? <div className="tree-empty" style={pad}>(empty)</div>;
    // test ids stay path-based for the first root; other roots are distinguished by data-ws
    const tid = (p: string) => (first ? `tree-file-${p}` : `tree-file-${ws.name}:${p}`);
    return [box, stale, ...st.items.map((it) => {
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
            <div className={`tree-row dir ${it.escapes ? "escapes" : ""}`} style={{ paddingLeft: depth * 14 + 8 }} title={tip} data-ws={ws.id} onClick={(e) => onRowClick(e, () => toggle(ws.id, it.path))}>
              <button className="tree-main" onClick={() => toggle(ws.id, it.path)}>
                <span className="chev">{isOpen ? "⌄" : "›"}</span>
                <FileIcon item={it} open={isOpen} />
                <span className="name">{it.name}</span>
              </button>
              {it.locked && <span className={`lock-badge ${it.lock_inherited ? "inherited" : ""}`} title={it.lock_inherited ? "Locked by a parent folder" : "Locked"}>🔒</span>}
              {it.git_status && it.git_status !== "clean" && <span className={`git-badge ${it.git_status}`}>{gitLabel(it.git_status)}</span>}
              <span className="tree-actions">
                {newButtons(ws.id, it.path, first ? `-${it.path}` : undefined)}
                <RefreshButton className="tree-action" onRefresh={() => refreshFolder(ws.id, it.path)} title={`Refresh ${it.name}`} testId={first ? `tree-refresh-${it.path}` : undefined} />
                {onOpenDiff && it.git_status && it.git_status !== "clean" && (
                  <button className="tree-action" onClick={() => onOpenDiff(ws.id, it.path, true)} title="Review all changes in this folder" data-testid={first ? `tree-diff-${it.path}` : undefined}>±</button>
                )}
                {onToggleLock && !it.lock_inherited && (
                  <button className="tree-action" onClick={() => onToggleLock(ws.id, it.path, !it.locked)} title={it.locked ? "Unlock this folder" : "Lock this folder (read-only for the app and app-driven agents)"} data-testid={first ? `tree-lock-${it.path}` : undefined}>{it.locked ? "🔓" : "🔒"}</button>
                )}
              </span>
            </div>
            {isOpen && renderDir(ws, it.path, depth + 1, first)}
          </div>
        );
      }
      const openable = it.openable === true;
      const selected = activeWorkspace === ws.id && selectedPath === it.path;
      return (
        <div key={it.path} className={`tree-row file ${selected ? "selected" : ""} ${openable ? "" : "inert"} ${it.kind}`} style={{ paddingLeft: depth * 14 + 22 }} title={tip} data-testid={tid(it.path)} data-kind={it.kind} data-ws={ws.id} onClick={(e) => openable && onRowClick(e, () => onOpen(ws.id, it.path))}>
          <button className="tree-main" onClick={() => (openable ? onOpen(ws.id, it.path) : undefined)} disabled={!openable} aria-disabled={!openable}>
            <FileIcon item={it} />
            <span className="name">{it.name}</span>
          </button>
          {it.locked && <span className={`lock-badge ${it.lock_inherited ? "inherited" : ""}`} title={it.lock_inherited ? "Locked by a parent folder" : "Locked"}>🔒</span>}
          {it.git_status && <span className={`git-badge ${it.git_status}`}>{gitLabel(it.git_status)}</span>}
          {((onOpenDiff && it.git_status && it.git_status !== "clean" && it.git_status !== "untracked") || (onToggleLock && !it.lock_inherited)) && (
            <span className="tree-actions">
              {onOpenDiff && it.git_status && it.git_status !== "clean" && it.git_status !== "untracked" && (
                <button className="tree-action" onClick={() => onOpenDiff(ws.id, it.path, false)} title="Review changes in this file" data-testid={first ? `tree-diff-${it.path}` : undefined}>±</button>
              )}
              {onToggleLock && !it.lock_inherited && (
                <button className="tree-action" onClick={() => onToggleLock(ws.id, it.path, !it.locked)} title={it.locked ? "Unlock this file" : "Lock this file (read-only for the app and app-driven agents)"} data-testid={first ? `tree-lock-${it.path}` : undefined}>{it.locked ? "🔓" : "🔒"}</button>
              )}
            </span>
          )}
        </div>
      );
    })];
  };

  return (
    <div className="file-tree">
      <div className="tree-header">
        <span className="ws-name">{multi ? `${workspaces.filter((w) => w.available).length} folders` : workspaces[0]?.name ?? ""}</span>
        <span className="spacer" />
        {onAddFolder && <button className="icon-btn" onClick={onAddFolder} title="Add a folder (another wiki) to this window" data-testid="tree-add-folder"><PlusIcon size={15} /></button>}
        <RefreshButton onRefresh={refreshNow} title="Refresh file tree" testId="tree-refresh" />
      </div>
      <div className="tree-body">
        {workspaces.map((ws, i) => {
          const open = expanded.has(key(ws.id, ""));
          const active = ws.id === activeWorkspace;
          return (
            <div key={ws.id} className={`tree-root-group ${active ? "active" : ""}`} data-testid="tree-root" data-ws={ws.id}>
              <div className={`tree-root ${ws.available ? "" : "unavailable"}`} onClick={(e) => ws.available && onRowClick(e, () => toggle(ws.id, ""))} title={`${ws.root}${ws.git?.branch ? `\nbranch: ${ws.git.branch}` : ""}${ws.configured ? "\n(defined in config.toml)" : ""}`}>
                <button className="tree-main" onClick={() => ws.available && toggle(ws.id, "")} disabled={!ws.available}>
                  <span className="chev">{open ? "⌄" : "›"}</span>
                  <FileIcon item={{ type: "dir", kind: "dir" }} open={open} />
                  <span className="name root-name">{ws.name}</span>
                  {ws.git?.branch && <span className="git-badge clean" title="current branch">{ws.git.branch}</span>}
                  {!ws.available && <span className="git-badge deleted">missing</span>}
                </button>
                <span className="tree-actions root-actions">
                  {onRemoveFolder && (
                    <button className="tree-action remove hover-only" onClick={() => onRemoveFolder(ws)} title="Close this folder (it stays closed; its discussions and history are kept — add it again with + to reopen)" data-testid={`tree-remove-${ws.name}`}>✕</button>
                  )}
                  {ws.available && (
                    <span className="hover-only-group">{newButtons(ws.id, "", i === 0 ? "-root" : `-root-${ws.name}`)}</span>
                  )}
                  {ws.available && (
                    <RefreshButton className="tree-action hover-only" onRefresh={() => refreshFolder(ws.id, "")} title={`Refresh ${ws.name}`} testId={i === 0 ? "tree-refresh-root" : `tree-refresh-root-${ws.name}`} />
                  )}
                  {ws.available && onOpenDiff && (
                    <button className="tree-action" onClick={() => onOpenDiff(ws.id, "", true)} title="Review all uncommitted changes in this folder" data-testid={i === 0 ? "tree-diff-root" : `tree-diff-root-${ws.name}`}>±</button>
                  )}
                </span>
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
