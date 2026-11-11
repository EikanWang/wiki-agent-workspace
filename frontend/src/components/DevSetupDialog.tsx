import { useEffect, useState } from "react";
import { ApiError, DevInfo, DevWorkspace, WorkspaceInfo, get, post } from "../api";
import FolderPicker from "./FolderPicker";

export interface DevSetupPreset {
  mode?: "branch" | "pr" | "folder";
  github_repo?: string;
  pr_number?: number;
  wiki_workspace_id?: string | null;
}

/** "owner/repo#12", "https://github.com/owner/repo/pull/12" → repo + number */
export function parsePrRef(text: string): { repo: string; number: number } | null {
  const t = text.trim();
  const m = t.match(/github\.com\/([\w.-]+\/[\w.-]+)\/pull\/(\d+)/) || t.match(/^([\w.-]+\/[\w.-]+)#(\d+)$/);
  return m ? { repo: m[1], number: Number(m[2]) } : null;
}

/**
 * New development workspace: where it runs (this server or an SSH host), the repository checkout to branch from,
 * new work or an existing pull request, and the wiki folder the agent uses as its reference.
 */
export default function DevSetupDialog({ info, workspaces, preset, onClose, onCreated }: {
  info: DevInfo;
  workspaces: WorkspaceInfo[];
  preset: DevSetupPreset;
  onClose: () => void;
  onCreated: (d: DevWorkspace) => void;
}) {
  const [host, setHost] = useState(() => localStorage.getItem("waw.devHost") || "local");
  const [mode, setMode] = useState<"branch" | "pr" | "folder">(preset.mode ?? "branch");
  const [initGit, setInitGit] = useState(true);
  const [source, setSource] = useState("");
  const [branch, setBranch] = useState("");
  const [base, setBase] = useState("");
  const [repo, setRepo] = useState(preset.github_repo ?? "");
  const [prRef, setPrRef] = useState(preset.github_repo && preset.pr_number ? `${preset.github_repo}#${preset.pr_number}` : "");
  const [wiki, setWiki] = useState<string>(preset.wiki_workspace_id ?? localStorage.getItem("waw.devWiki") ?? workspaces.find((w) => w.available)?.id ?? "");
  const [name, setName] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [python, setPython] = useState("");
  const [ccd, setCcd] = useState("");
  const [build, setBuild] = useState("");
  const [test, setTest] = useState("");
  const [browse, setBrowse] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // the repository's connected local checkout (PR Review → "Local checkout") is the natural source
  const pr = parsePrRef(prRef);
  const repoForLookup = mode === "pr" ? pr?.repo : repo.trim();
  useEffect(() => {
    if (!repoForLookup || source || host !== "local") return;
    let alive = true;
    get<{ link: { path: string } | null }>(`/github/local-repo?repo=${encodeURIComponent(repoForLookup)}`)
      .then((r) => { if (alive && r.link?.path) setSource((s) => s || r.link!.path); })
      .catch(() => undefined);
    return () => { alive = false; };
  }, [repoForLookup, host, source]);
  useEffect(() => {
    if (!source) setSource(localStorage.getItem(`waw.devSource.${host}`) || "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [host]);

  const create = async () => {
    setErr(null);
    if (mode === "pr" && !pr) return setErr("Give the pull request as owner/repo#123 or its GitHub URL.");
    setBusy(true);
    try {
      const settings: Record<string, string> = {};
      if (python.trim()) settings.python = python.trim();
      if (ccd.trim()) settings.compile_commands_dir = ccd.trim();
      if (build.trim()) settings.build_command = build.trim();
      if (test.trim()) settings.test_command = test.trim();
      const d = await post<DevWorkspace>("/dev/workspaces", {
        host, source_repo: source.trim(), mode, name: name.trim() || undefined,
        branch: mode !== "pr" ? branch.trim() || undefined : undefined, base: mode === "branch" ? base.trim() || undefined : undefined,
        init_git: mode === "folder" ? initGit : undefined,
        github_repo: mode === "pr" ? pr!.repo : repo.trim() || undefined, pr_number: mode === "pr" ? pr!.number : undefined,
        wiki_workspace_id: wiki || undefined, settings,
      });
      localStorage.setItem("waw.devHost", host);
      localStorage.setItem(`waw.devSource.${host}`, source.trim());
      if (wiki) localStorage.setItem("waw.devWiki", wiki);
      onCreated(d);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const ready = !!source.trim() && (mode === "pr" ? !!pr : mode === "folder" ? true : !!branch.trim());
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal dev-setup" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="New development workspace" data-testid="dev-setup">
        <div className="modal-head"><span>New development workspace</span><button className="icon-btn" onClick={onClose} aria-label="Close">×</button></div>
        <div className="muted small">
          {mode === "folder"
            ? "The app works directly in this folder (its files and branch change; no separate worktree), opens it in the editor and lets the agent work there with the whole wiki folder as its reference."
            : "The app creates its own Git worktree (your checkout is not switched or changed), opens it in a VS Code editor and lets the agent work there with the whole wiki folder as its reference."}
        </div>
        <label className="dev-field">
          <span className="label">Runs on</span>
          <select value={host} onChange={(e) => { setHost(e.target.value); setSource(""); }} data-testid="dev-host">
            {info.hosts.map((h) => <option key={h.name} value={h.name}>{h.label}{h.ssh ? ` (ssh ${h.ssh})` : ""}</option>)}
          </select>
          {host !== "local" && <span className="muted small">editor, agent, builds and tests run on that host; the wiki is copied there read-only</span>}
        </label>
        <div className="dev-field">
          <span className="label">Work on</span>
          <span className="mode-switch" role="tablist">
            <button role="tab" aria-selected={mode === "branch"} className={mode === "branch" ? "active" : ""} onClick={() => setMode("branch")} data-testid="dev-mode-branch">New branch</button>
            <button role="tab" aria-selected={mode === "pr"} className={mode === "pr" ? "active" : ""} onClick={() => setMode("pr")} data-testid="dev-mode-pr">Existing pull request</button>
            <button role="tab" aria-selected={mode === "folder"} className={mode === "folder" ? "active" : ""} onClick={() => setMode("folder")} title="A Git checkout or any folder, used as it is" data-testid="dev-mode-folder">Local folder</button>
          </span>
        </div>
        {mode === "folder" ? (
          <label className="dev-field">
            <span className="label">Branch to create there (optional)</span>
            <input value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="keep the current branch; you can create one later" data-testid="dev-branch" />
          </label>
        ) : mode === "pr" ? (
          <label className="dev-field">
            <span className="label">Pull request</span>
            <input value={prRef} onChange={(e) => setPrRef(e.target.value)} placeholder="owner/repo#123 or https://github.com/owner/repo/pull/123" data-testid="dev-pr" />
          </label>
        ) : (
          <>
            <label className="dev-field">
              <span className="label">New branch</span>
              <input value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="e.g. feature/faster-lowering" data-testid="dev-branch" />
            </label>
            <div className="dev-pair">
              <label className="dev-field grow"><span className="label">From (base)</span>
                <input value={base} onChange={(e) => setBase(e.target.value)} placeholder="the checkout's current branch" data-testid="dev-base" /></label>
              <label className="dev-field grow"><span className="label">GitHub repository (for the PR)</span>
                <input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="detected from origin" /></label>
            </div>
          </>
        )}
        <label className="dev-field">
          <span className="label">{mode === "folder" ? "Folder" : "Repository checkout"} {host === "local" ? "on this server" : `on ${host}`}</span>
          <span className="row nowrap">
            <input value={source} onChange={(e) => setSource(e.target.value)} placeholder={mode === "folder" ? "/path/to/a/checkout/or/folder" : "/path/to/your/clone"} className="mono" data-testid="dev-source" />
            {host === "local" && <button className="btn small" onClick={() => setBrowse(true)}>Browse…</button>}
          </span>
        </label>
        {mode === "folder" && (
          <label className="small" title="Git keeps track of the changes: the diff, commits and branches need it">
            <input type="checkbox" checked={initGit} onChange={(e) => setInitGit(e.target.checked)} data-testid="dev-init-git" /> If the folder is not in a Git repository, make it one (git init)
          </label>
        )}
        <label className="dev-field">
          <span className="label">Reference wiki (the agent reads the whole folder)</span>
          <select value={wiki} onChange={(e) => setWiki(e.target.value)} data-testid="dev-wiki">
            <option value="">None</option>
            {workspaces.filter((w) => w.available).map((w) => <option key={w.id} value={w.id}>{w.name} — {w.root}</option>)}
          </select>
        </label>
        <button className="btn small link-btn" onClick={() => setAdvanced((a) => !a)} aria-expanded={advanced}>{advanced ? "▾" : "▸"} Name, language and build settings</button>
        {advanced && (
          <div className="dev-advanced">
            <label className="dev-field"><span className="label">Name</span><input value={name} onChange={(e) => setName(e.target.value)} placeholder="default: the branch or PR title" /></label>
            <label className="dev-field"><span className="label">Python interpreter</span><input value={python} onChange={(e) => setPython(e.target.value)} placeholder="python3, or /path/to/venv/bin/python" className="mono" /></label>
            <label className="dev-field"><span className="label">compile_commands.json folder (C/C++)</span><input value={ccd} onChange={(e) => setCcd(e.target.value)} placeholder="build (detected for CMake)" className="mono" /></label>
            <label className="dev-field"><span className="label">Build command</span><input value={build} onChange={(e) => setBuild(e.target.value)} placeholder="detected (CMake / Make)" className="mono" /></label>
            <label className="dev-field"><span className="label">Test command</span><input value={test} onChange={(e) => setTest(e.target.value)} placeholder="detected (ctest / pytest)" className="mono" /></label>
          </div>
        )}
        {err && <div className="error-text" role="alert" data-testid="dev-setup-error">{err}</div>}
        <div className="row">
          <span className="spacer" />
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={!ready || busy} onClick={() => void create()} data-testid="dev-create">{busy ? "Creating…" : "Create"}</button>
        </div>
        {browse && (
          <FolderPicker title={mode === "folder" ? "Folder" : "Repository checkout"} requireGit={mode !== "folder"} start={source || undefined}
            hint={mode === "folder" ? "A Git checkout or any folder on this server; the app works in it directly." : "A Git clone on this server; the app adds a worktree to it."}
            confirmLabel={mode === "folder" ? "Use this folder" : "Use this checkout"} onClose={() => setBrowse(false)} onChoose={(p) => { setBrowse(false); setSource(p); }} />
        )}
      </div>
    </div>
  );
}
