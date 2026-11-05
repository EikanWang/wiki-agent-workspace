import { useCallback, useEffect, useState } from "react";
import { FileItem, GitSummary, get } from "../api";
import { FileIcon, KIND_LABEL, formatSize } from "./FileIcon";

interface DirState {
  items: FileItem[];
  loading: boolean;
  error: string | null;
}

export default function FileTree({
  workspaceName,
  selectedPath,
  onOpen,
  onOpenDiff,
  refreshKey,
  onGit,
}: {
  workspaceName: string;
  selectedPath: string | null;
  onOpen: (path: string) => void;
  /** Open the diff review for a file or folder (undefined disables the action). */
  onOpenDiff?: (path: string, isDir: boolean) => void;
  refreshKey: number;
  onGit?: (g: GitSummary) => void;
}) {
  const [dirs, setDirs] = useState<Record<string, DirState>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set([""]));

  const load = useCallback(
    async (path: string) => {
      setDirs((d) => ({ ...d, [path]: { items: d[path]?.items ?? [], loading: true, error: null } }));
      try {
        const r = await get<{ items: FileItem[]; git: GitSummary }>(`/files?path=${encodeURIComponent(path)}`);
        setDirs((d) => ({ ...d, [path]: { items: r.items, loading: false, error: null } }));
        if (path === "" && onGit) onGit(r.git);
      } catch (e) {
        setDirs((d) => ({ ...d, [path]: { items: [], loading: false, error: (e as Error).message } }));
      }
    },
    [onGit],
  );

  useEffect(() => {
    for (const p of expanded) void load(p);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  useEffect(() => {
    for (const p of expanded) if (!dirs[p]) void load(p);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded]);

  const toggle = (path: string) => {
    setExpanded((s) => {
      const n = new Set(s);
      if (n.has(path)) n.delete(path);
      else n.add(path);
      return n;
    });
  };

  const renderDir = (path: string, depth: number) => {
    const st = dirs[path];
    if (!st) return <div className="tree-loading" style={{ paddingLeft: depth * 14 + 8 }}>Loading…</div>;
    if (st.error) return <div className="tree-error" style={{ paddingLeft: depth * 14 + 8 }}>{st.error}</div>;
    if (st.items.length === 0 && !st.loading) return <div className="tree-empty" style={{ paddingLeft: depth * 14 + 8 }}>(no Markdown files)</div>;
    return st.items.map((it) => {
      const tip = [
        it.path,
        KIND_LABEL[it.type === "dir" ? "dir" : it.kind] ?? it.kind,
        it.symlink ? `symlink → ${it.link_target ?? "?"}${it.escapes ? " (outside the workspace)" : ""}` : "",
        it.type === "file" ? formatSize(it.size) : "",
        it.git_status ? `git: ${it.git_status}` : "",
      ]
        .filter(Boolean)
        .join("\n");
      if (it.type === "dir") {
        const isOpen = expanded.has(it.path);
        return (
          <div key={it.path}>
            <div className={`tree-row dir ${it.escapes ? "escapes" : ""}`} style={{ paddingLeft: depth * 14 + 8 }} title={tip}>
              <button className="tree-main" onClick={() => toggle(it.path)}>
                <span className="chev">{isOpen ? "⌄" : "›"}</span>
                <FileIcon item={it} open={isOpen} />
                <span className="name">{it.name}</span>
              </button>
              {onOpenDiff && it.git_status && it.git_status !== "clean" && (
                <button className="tree-action" onClick={() => onOpenDiff(it.path, true)} title="Review all changes in this folder" data-testid={`tree-diff-${it.path}`}>±</button>
              )}
              {it.git_status && it.git_status !== "clean" && <span className={`git-badge ${it.git_status}`}>{gitLabel(it.git_status)}</span>}
            </div>
            {isOpen && renderDir(it.path, depth + 1)}
          </div>
        );
      }
      const openable = it.openable !== false && it.kind === "markdown";
      return (
        <div key={it.path} className={`tree-row file ${selectedPath === it.path ? "selected" : ""} ${openable ? "" : "inert"} ${it.kind}`} style={{ paddingLeft: depth * 14 + 22 }} title={tip} data-testid={`tree-file-${it.path}`} data-kind={it.kind}>
          <button className="tree-main" onClick={() => (openable ? onOpen(it.path) : undefined)} disabled={!openable} aria-disabled={!openable}>
            <FileIcon item={it} />
            <span className="name">{it.name}</span>
          </button>
          {onOpenDiff && it.git_status && it.git_status !== "clean" && it.git_status !== "untracked" && (
            <button className="tree-action" onClick={() => onOpenDiff(it.path, false)} title="Review changes in this file" data-testid={`tree-diff-${it.path}`}>±</button>
          )}
          {it.git_status && <span className={`git-badge ${it.git_status}`}>{gitLabel(it.git_status)}</span>}
        </div>
      );
    });
  };

  return (
    <div className="file-tree">
      <div className="tree-header">
        <span className="ws-name" title={workspaceName}><FileIcon item={{ type: "dir", kind: "dir" }} open /> {workspaceName}</span>
        {onOpenDiff && <button className="icon-btn" onClick={() => onOpenDiff("", true)} title="Review all uncommitted changes in the workspace" data-testid="tree-diff-root">±</button>}
        <button className="icon-btn" onClick={() => { for (const p of expanded) void load(p); }} title="Refresh file tree" data-testid="tree-refresh">↻</button>
      </div>
      <div className="tree-body">{renderDir("", 0)}</div>
    </div>
  );
}

export function gitLabel(s: string): string {
  switch (s) {
    case "untracked":
      return "untracked";
    case "modified":
      return "modified";
    case "deleted":
      return "deleted";
    case "clean":
      return "clean";
    default:
      return s;
  }
}
