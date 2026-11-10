import { useEffect, useRef, useState } from "react";
import { confirmAction } from "./Confirm";
import { ApiError, NamedWorkspaceList, del, get, patch, post } from "../api";

/**
 * Top-bar menu of named workspaces (VS Code windows): switch, create, rename, delete. Switching reloads the page:
 * folders, file tree, document tabs and terminals all change with the workspace.
 */
export default function WorkspaceMenu({ initial, onNew, beforeSwitch, onNotify }: { initial: NamedWorkspaceList | undefined; onNew: () => void; beforeSwitch: () => boolean; onNotify: (m: string) => void }) {
  const [list, setList] = useState<NamedWorkspaceList | undefined>(initial);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLSpanElement>(null);
  useEffect(() => setList(initial), [initial]);
  useEffect(() => {
    if (!open) return;
    void get<NamedWorkspaceList>("/named-workspaces").then(setList).catch(() => undefined);
    const close = (e: MouseEvent) => {
      if ((e.target as Element).closest?.(".confirm-backdrop")) return; // answering its confirmation keeps the menu open
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  const current = list?.workspaces.find((w) => w.current);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      onNotify((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const switchTo = (id: string) =>
    run(async () => {
      if (!beforeSwitch()) return;
      await post(`/named-workspaces/${id}/open`);
      localStorage.setItem("waw.namedWorkspace", id);
      window.location.reload();
    });
  const rename = (id: string, name: string) =>
    run(async () => {
      const n = window.prompt("Workspace name", name);
      if (!n || n.trim() === name) return;
      setList(await patch<NamedWorkspaceList>(`/named-workspaces/${id}`, { name: n.trim() }));
    });
  const remove = (id: string, name: string) =>
    run(async () => {
      if (!(await confirmAction({ title: `Delete the workspace "${name}"?`, message: "Only this named set of folders goes. Its folders, their discussions and history are not touched; its terminals move to Default and keep running.", confirmLabel: "Delete workspace" }))) return;
      setList(await del<NamedWorkspaceList>(`/named-workspaces/${id}`));
    });

  return (
    <span className="ws-menu-anchor" ref={box}>
      <button className={`btn small toggle ws-menu-btn ${open ? "on" : ""}`} onClick={() => setOpen((v) => !v)} title="Workspaces: named sets of folders with their own tabs and terminals" data-testid="ws-menu" aria-expanded={open}>
        <span aria-hidden="true">▣</span> {current?.name ?? "Workspace"} <span aria-hidden="true">▾</span>
      </button>
      {open && list && (
        <div className="ws-menu" role="menu" data-testid="ws-menu-list">
          {list.workspaces.map((w) => (
            <div key={w.id} className={`ws-menu-item ${w.current ? "current" : ""}`} data-testid="ws-menu-item" data-name={w.name}>
              <button className="ws-menu-main" role="menuitemradio" aria-checked={w.current} disabled={busy || w.current} onClick={() => void switchTo(w.id)} title={w.folders.map((f) => f.root).join("\n")}>
                <span className="ws-check" aria-hidden="true">{w.current ? "✓" : ""}</span>
                <span className="ws-menu-name">{w.name}</span>
                <span className="muted small ws-menu-folders">{w.folders.map((f) => f.name).join(", ") || "no folders"}</span>
              </button>
              <button className="icon-btn small" onClick={() => void rename(w.id, w.name)} title="Rename" aria-label={`Rename ${w.name}`} data-testid="ws-menu-rename">✎</button>
              {!w.current && !w.is_default && (
                <button className="icon-btn small danger" onClick={() => void remove(w.id, w.name)} title="Delete this workspace (its folders and data stay)" aria-label={`Delete ${w.name}`} data-testid="ws-menu-delete">🗑</button>
              )}
            </div>
          ))}
          <div className="ws-menu-sep" />
          <button className="ws-menu-new" onClick={() => { setOpen(false); onNew(); }} data-testid="ws-menu-new">+ New workspace…</button>
        </div>
      )}
    </span>
  );
}
