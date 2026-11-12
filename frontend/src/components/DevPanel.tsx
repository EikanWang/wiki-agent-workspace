import { useCallback, useEffect, useState } from "react";
import { ApiError, DevInfo, DevWorkspace, WorkspaceInfo, del, get, subscribeGlobal } from "../api";
import { confirmAction } from "./Confirm";
import DevSetupDialog, { DevSetupPreset } from "./DevSetupDialog";
import { PlusIcon } from "./Icons";
import MyPrs from "./MyPrs";

/** Sidebar of PR Development: the development workspaces (newest activity first) and "New". */
export default function DevPanel({ activeId, workspaces, onOpen, preset, onPresetUsed, onFold }: {
  /** fold the list (the sidebar); the workspace's toolbar has » to show it again */
  onFold?: () => void;
  activeId: string | null;
  workspaces: WorkspaceInfo[];
  onOpen: (id: string) => void;
  /** open the setup dialog prefilled (e.g. "Open in Development" from a PR tab) */
  preset: DevSetupPreset | null;
  onPresetUsed: () => void;
}) {
  const [list, setList] = useState<DevWorkspace[] | null>(null);
  const [info, setInfo] = useState<DevInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [setup, setSetup] = useState<DevSetupPreset | null>(null);
  const load = useCallback(async () => {
    try {
      const r = await get<{ workspaces: DevWorkspace[] }>("/dev/workspaces");
      setList(r.workspaces);
      setErr(null);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, []);
  useEffect(() => {
    void load();
    void get<DevInfo>("/dev/info").then(setInfo).catch(() => undefined);
    return subscribeGlobal((ev) => {
      if (ev.type === "dev_changed" || ev.type === "dev_task") void load();
    });
  }, [load]);
  const remove = async (d: DevWorkspace) => {
    if (!(await confirmAction({ title: `Remove “${d.name}” from the list?`, confirmLabel: "Remove", danger: false,
      message: `The workspace is archived: its editor stops; ${d.mode === "folder" ? `the folder ${d.worktree_path}` : `its worktree ${d.worktree_path} and branch ${d.branch}`} and its conversation stay, and nothing changes on GitHub${d.pr_number ? ` (#${d.pr_number} stays open)` : ""}.` }))) return;
    try {
      await del(`/dev/workspaces/${d.id}?remove_worktree=false`);
      await load();
      if (d.id === activeId) onOpen("");
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  useEffect(() => {
    if (preset) {
      setSetup(preset);
      onPresetUsed();
    }
  }, [preset, onPresetUsed]);

  return (
    <div className="dev-panel" data-testid="dev-panel">
      <div className="dev-panel-head">
        <b>PR Development</b>
        <span className="spacer" />
        <button className="icon-btn" onClick={() => setSetup({})} title="New development workspace: a new branch or an existing pull request" aria-label="New development workspace" data-testid="dev-new">
          <PlusIcon />
        </button>
        {onFold && <button className="icon-btn" onClick={onFold} title="Fold the list of workspaces (» in the workspace's toolbar shows it again)" aria-label="Fold the list of workspaces" data-testid="dev-panel-fold">«</button>}
      </div>
      {info && !info.editor.available && <div className="warn small dev-hint">{info.editor.install_hint}</div>}
      {err && <div className="error-text small">{err}</div>}
      <MyPrs list={list ?? []} activeId={activeId} onOpen={onOpen} onSetup={(p) => setSetup(p)} onCreated={(d) => { void load(); onOpen(d.id); }} />
      <div className="dev-list">
        {list?.length === 0 && (
          <div className="muted small dev-hint">
            Develop a new pull request or update an existing one: a Git worktree, a VS Code editor with C++/Python language support,
            builds and tests, and the agent — with a wiki folder as its reference.
            <div className="row"><button className="btn small primary" onClick={() => setSetup({})} data-testid="dev-new-empty">New development workspace</button></div>
          </div>
        )}
        {list?.map((d) => (
          <div key={d.id} className="dev-item-row">
          <button className={`list-item dev-item ${d.id === activeId ? "selected" : ""}`} onClick={() => onOpen(d.id)} data-testid="dev-item" data-status={d.status}>
            <span className="dev-item-main">
              <span className="dev-item-name">{d.name}</span>
              <span className="muted small mono">{d.branch}{d.host !== "local" ? ` · ${d.host}` : ""}</span>
            </span>
            <span className="dev-item-side">
              {d.status !== "ready" && <span className={`chip ${d.status === "failed" ? "warn" : ""}`}>{d.status}</span>}
              {d.pr_number && <span className="chip">#{d.pr_number}</span>}
              {d.editor.state === "running" && <span className="dev-dot" title="The editor is running" />}
              {d.running_tasks > 0 && <span className="chip" title="Commands running">⏵{d.running_tasks}</span>}
            </span>
          </button>
          <button className="icon-btn small dev-item-remove" onClick={() => void remove(d)} title="Remove from this list (archive; its files and branch stay; nothing changes on GitHub)"
            aria-label={`Remove ${d.name}`} data-testid="dev-item-remove">×</button>
          </div>
        ))}
      </div>
      {setup && info && (
        <DevSetupDialog info={info} workspaces={workspaces} preset={setup} onClose={() => setSetup(null)}
          onCreated={(d) => { setSetup(null); void load(); onOpen(d.id); }} />
      )}
    </div>
  );
}
