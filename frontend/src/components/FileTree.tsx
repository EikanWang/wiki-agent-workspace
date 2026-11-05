import { useCallback, useEffect, useState } from "react";
import { FileItem, GitSummary, get } from "../api";

interface DirState {
  items: FileItem[];
  loading: boolean;
  error: string | null;
}

export default function FileTree({
  workspaceName,
  selectedPath,
  onOpen,
  refreshKey,
  onGit,
}: {
  workspaceName: string;
  selectedPath: string | null;
  onOpen: (path: string) => void;
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
    return st.items.map((it) =>
      it.type === "dir" ? (
        <div key={it.path}>
          <button className="tree-row dir" style={{ paddingLeft: depth * 14 + 8 }} onClick={() => toggle(it.path)} title={it.path}>
            <span className="chev">{expanded.has(it.path) ? "⌄" : "›"}</span>
            <span className="name">{it.name}</span>
            {it.git_status && it.git_status !== "clean" && <span className={`git-badge ${it.git_status}`}>{gitLabel(it.git_status)}</span>}
          </button>
          {expanded.has(it.path) && renderDir(it.path, depth + 1)}
        </div>
      ) : (
        <button
          key={it.path}
          className={`tree-row file ${selectedPath === it.path ? "selected" : ""}`}
          style={{ paddingLeft: depth * 14 + 22 }}
          onClick={() => onOpen(it.path)}
          title={it.path}
          data-testid={`tree-file-${it.path}`}
        >
          <span className="name">{it.name}</span>
          {it.git_status && <span className={`git-badge ${it.git_status}`}>{gitLabel(it.git_status)}</span>}
        </button>
      ),
    );
  };

  return (
    <div className="file-tree">
      <div className="tree-header">
        <span className="ws-name" title={workspaceName}>▣ {workspaceName}</span>
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
