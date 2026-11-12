import { useCallback, useEffect, useState } from "react";
import { ApiError, DevWorkspace, get, post } from "../api";
import FolderPicker from "./FolderPicker";
import { DevSetupPreset } from "./DevSetupDialog";

export interface MyPr {
  number: number; title: string; url: string; isDraft: boolean; state: string; headRefName: string; baseRefName: string;
  updatedAt: string; reviewDecision: string | null; ci: "SUCCESS" | "FAILURE" | "PENDING" | null;
  workspaces: { id: string; name: string; mode: string }[];
}
interface KnownRepo { repo: string; source: string | null; workspaces: number }

const ago = (iso: string) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  return s < 3600 ? `${Math.max(1, Math.round(s / 60))}m` : s < 86400 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`;
};

/**
 * Your pull requests in a repository (picked from the ones you develop here, typed, or taken from a local
 * checkout): open one in its own workspace (the one it already has, or a new one from the repository's checkout),
 * or check it out in the active workspace's own checkout — then edit, commit and push to update it.
 */
export default function MyPrs({ list, activeId, onOpen, onSetup, onCreated }: {
  list: DevWorkspace[];
  activeId: string | null;
  onOpen: (id: string) => void;
  /** no checkout of the repository known: the setup dialog, prefilled */
  onSetup: (preset: DevSetupPreset) => void;
  onCreated: (d: DevWorkspace) => void;
}) {
  const [open, setOpen] = useState(() => localStorage.getItem("waw.dev.myprs.open") !== "0");
  useEffect(() => { localStorage.setItem("waw.dev.myprs.open", open ? "1" : "0"); }, [open]);
  const [repos, setRepos] = useState<KnownRepo[]>([]);
  const [repo, setRepo] = useState(() => localStorage.getItem("waw.dev.myprs.repo") ?? "");
  const [typed, setTyped] = useState("");
  const [state, setState] = useState<"open" | "all">("open");
  const [data, setData] = useState<{ repo: string; source: string | null; prs: MyPr[] } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [pick, setPick] = useState(false);
  const [filter, setFilter] = useState("");
  useEffect(() => { void get<{ repos: KnownRepo[] }>("/dev/repos").then((r) => { setRepos(r.repos); setRepo((x) => x || r.repos[0]?.repo || ""); }).catch(() => undefined); }, [list.length]);
  useEffect(() => { if (repo) localStorage.setItem("waw.dev.myprs.repo", repo); }, [repo]);
  // follow the workspace you open: its repository's pull requests
  const activeRepo = list.find((d) => d.id === activeId)?.github_repo ?? null;
  useEffect(() => { if (activeRepo) setRepo(activeRepo); }, [activeRepo]);
  const load = useCallback(async (q: { repo?: string; source?: string }) => {
    setBusy("load");
    setErr(null);
    try {
      const r = await get<{ repo: string; source: string | null; prs: MyPr[] }>(`/dev/my-prs?${new URLSearchParams({ ...q, state } as Record<string, string>)}`);
      setData(r);
      if (r.repo !== repo) setRepo(r.repo);
      if (r.source) setRepos((rs) => (rs.some((x) => x.repo.toLowerCase() === r.repo.toLowerCase()) ? rs : [...rs, { repo: r.repo, source: r.source, workspaces: 0 }]));
    } catch (e) {
      setErr((e as ApiError).message);
      setData(null);
    } finally {
      setBusy(null);
    }
  }, [state, repo]);
  useEffect(() => { if (open && repo) void load({ repo }); }, [open, repo, state]); // eslint-disable-line react-hooks/exhaustive-deps

  const active = list.find((d) => d.id === activeId) ?? null;
  // the active workspace's own checkout can take another PR of the same repository (not a PR's own workspace)
  const here = active && active.status === "ready" && active.mode !== "pr" && data && (active.github_repo ?? "").toLowerCase() === data.repo.toLowerCase() ? active : null;
  const source = data?.source ?? repos.find((r) => r.repo.toLowerCase() === (data?.repo ?? "").toLowerCase())?.source ?? null;

  const openPr = async (p: MyPr) => {
    const existing = p.workspaces.find((w) => w.mode === "pr") ?? p.workspaces[0];
    if (existing) return onOpen(existing.id);
    if (!data) return;
    if (!source) return onSetup({ mode: "pr", github_repo: data.repo, pr_number: p.number });
    setBusy(`open:${p.number}`);
    setErr(null);
    try {
      const wiki = localStorage.getItem("waw.devWiki") || undefined;
      onCreated(await post<DevWorkspace>("/dev/workspaces", { host: "local", source_repo: source, mode: "pr", github_repo: data.repo, pr_number: p.number, wiki_workspace_id: wiki }));
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };
  const checkoutHere = async (p: MyPr) => {
    if (!here) return;
    setBusy(`here:${p.number}`);
    setErr(null);
    try {
      await post(`/dev/workspaces/${here.id}/checkout-pr`, { number: p.number });
      onOpen(here.id);
      void load({ repo: data!.repo });
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };
  const f = filter.trim().toLowerCase();
  const shown = (data?.prs ?? []).filter((p) => !f || `#${p.number} ${p.title} ${p.headRefName}`.toLowerCase().includes(f));

  return (
    <div className={`my-prs ${open ? "" : "folded"}`} data-testid="my-prs">
      <button className="my-prs-head" onClick={() => setOpen(!open)} aria-expanded={open} data-testid="my-prs-toggle">
        <span className="chev">{open ? "⌄" : "›"}</span> <b>My pull requests</b>
        {data && <span className="muted small"> · {data.repo} ({data.prs.length})</span>}
      </button>
      {open && (
        <div className="my-prs-body">
          <div className="row nowrap">
            <select value={repos.some((r) => r.repo === repo) ? repo : ""} onChange={(e) => setRepo(e.target.value)} title="Repository" data-testid="my-prs-repo">
              {!repos.some((r) => r.repo === repo) && <option value="">{repo || "choose a repository…"}</option>}
              {repos.map((r) => <option key={r.repo} value={r.repo}>{r.repo}</option>)}
            </select>
            <select value={state} onChange={(e) => setState(e.target.value as "open" | "all")} title="Which pull requests" data-testid="my-prs-state">
              <option value="open">open</option>
              <option value="all">all</option>
            </select>
            <button className="icon-btn small" disabled={!repo || busy === "load"} onClick={() => void load({ repo })} title="Refresh from GitHub" data-testid="my-prs-refresh">{busy === "load" ? "…" : "↻"}</button>
          </div>
          <form className="row nowrap" onSubmit={(e) => { e.preventDefault(); const t = typed.trim().replace(/^https:\/\/github\.com\//, "").replace(/\.git$|\/$/g, ""); if (t) { setTyped(""); void load({ repo: t }); } }}>
            <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="owner/repo" className="mono" data-testid="my-prs-repo-input" />
            <button className="btn small" disabled={!typed.trim()}>Show</button>
            <button type="button" className="btn small" onClick={() => setPick(true)} title="The repository of a local checkout or folder" data-testid="my-prs-folder">Folder…</button>
          </form>
          {err && <div className="error-text small" role="alert" data-testid="my-prs-error">{err}</div>}
          {data && data.prs.length > 6 && <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="filter: title, #number, branch" data-testid="my-prs-filter" />}
          {data && !source && data.prs.length > 0 && <div className="muted small">No local checkout of {data.repo} known yet: opening a PR asks for one.</div>}
          <div className="my-prs-list">
            {shown.map((p) => {
              const ws = p.workspaces;
              const isHere = !!here && here.pr_number === p.number;
              return (
                <div key={p.number} className={`my-pr ${isHere ? "here" : ""}`} data-testid="my-pr" data-number={p.number}>
                  <button className="my-pr-main" onClick={() => void openPr(p)} disabled={busy !== null}
                    title={`${p.headRefName} → ${p.baseRefName}\n${ws.length ? `Open its workspace: ${ws.map((w) => w.name).join(", ")}` : source ? "Open it in a new workspace (a worktree of your checkout)" : "Open it in a new workspace"}`}>
                    <span className={`ci-dot ${(p.ci ?? "none").toLowerCase()}`} aria-hidden="true" />
                    <span className="my-pr-text">
                      <span className="my-pr-title">{p.isDraft && <span className="chip">draft</span>} {p.state !== "OPEN" && <span className="chip">{p.state.toLowerCase()}</span>} {p.title}</span>
                      <span className="muted small mono">#{p.number} · {p.headRefName} · {ago(p.updatedAt)}{p.reviewDecision === "APPROVED" ? " · approved" : p.reviewDecision === "CHANGES_REQUESTED" ? " · changes requested" : ""}</span>
                    </span>
                    {busy === `open:${p.number}` ? <span className="muted small">opening…</span> : ws.length > 0 && <span className="chip" title="Has a workspace here">{isHere ? "here" : "open"}</span>}
                  </button>
                  {here && !isHere && p.state === "OPEN" && (
                    <button className="btn small" disabled={busy !== null} onClick={() => void checkoutHere(p)} title={`Check it out in “${here.name}” (${here.worktree_path}); uncommitted changes go along`} data-testid="my-pr-here">
                      {busy === `here:${p.number}` ? "…" : "Check out here"}
                    </button>
                  )}
                </div>
              );
            })}
            {data && data.prs.length === 0 && <div className="muted small" data-testid="my-prs-empty">No {state === "open" ? "open " : ""}pull requests of yours in {data.repo}.</div>}
          </div>
        </div>
      )}
      {pick && <FolderPicker title="Pull requests of the repository in…" hint="A local Git checkout of the repository." requireGit confirmLabel="Use this checkout"
        onClose={() => setPick(false)} onChoose={(p) => { setPick(false); void load({ source: p }); }} />}
    </div>
  );
}
