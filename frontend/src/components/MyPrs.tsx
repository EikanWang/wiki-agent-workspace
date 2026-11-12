import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, DevWorkspace, get, post } from "../api";
import FolderPicker from "./FolderPicker";
import { DevSetupPreset } from "./DevSetupDialog";
import { confirmAction } from "./Confirm";
import DevPrState from "./DevPrState";

export interface MyPr {
  number: number; title: string; url: string; isDraft: boolean; state: string; headRefName: string; baseRefName: string;
  updatedAt: string; reviewDecision: string | null; ci: "SUCCESS" | "FAILURE" | "PENDING" | null;
  workspaces: { id: string; name: string; mode: string }[];
}
interface KnownRepo { repo: string; source: string | null; workspaces: number }
interface Group { repo: string; source: string | null; prs: MyPr[] | null; err: string | null; loading: boolean }

const ago = (iso: string) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  return s < 3600 ? `${Math.max(1, Math.round(s / 60))}m` : s < 86400 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`;
};
const home = (p: string) => p.replace(/^\/home\/[^/]+|^\/root/, "~");
const lc = (s: string) => s.toLowerCase();
const EXTRA = "waw.dev.myprs.extra"; // repositories you added here: [{repo, source}]
const FOLDED = "waw.dev.myprs.folded";
const HIDDEN = "waw.dev.myprs.hidden"; // "owner/repo#n" removed from this list (nothing changes on GitHub)
const readJson = <T,>(k: string, d: T): T => { try { return JSON.parse(localStorage.getItem(k) ?? "") as T; } catch { return d; } };

/**
 * Your pull requests, grouped by repository and its local folder: every repository you develop here (and those you
 * add by name or from a local checkout). Click a PR to open it: its workspace if it has one, otherwise the
 * repository's local folder switches to its branch (no clone), otherwise the setup dialog.
 */
export default function MyPrs({ list, activeId, onOpen, onSetup, onCreated }: {
  list: DevWorkspace[];
  activeId: string | null;
  onOpen: (id: string) => void;
  onSetup: (preset: DevSetupPreset) => void;
  onCreated: (d: DevWorkspace) => void;
}) {
  const [open, setOpen] = useState(() => localStorage.getItem("waw.dev.myprs.open") !== "0");
  useEffect(() => { localStorage.setItem("waw.dev.myprs.open", open ? "1" : "0"); }, [open]);
  const [known, setKnown] = useState<KnownRepo[]>([]);
  const [extra, setExtra] = useState<{ repo: string; source: string | null }[]>(() => readJson(EXTRA, []));
  useEffect(() => { localStorage.setItem(EXTRA, JSON.stringify(extra)); }, [extra]);
  const [folded, setFolded] = useState<Set<string>>(() => new Set(readJson<string[]>(FOLDED, [])));
  useEffect(() => { localStorage.setItem(FOLDED, JSON.stringify([...folded])); }, [folded]);
  const [hidden, setHidden] = useState<Set<string>>(() => new Set(readJson<string[]>(HIDDEN, [])));
  useEffect(() => { localStorage.setItem(HIDDEN, JSON.stringify([...hidden])); }, [hidden]);
  const [showHidden, setShowHidden] = useState<Set<string>>(new Set()); // groups showing their removed PRs
  const hideKey = (repo: string, n: number) => `${lc(repo)}#${n}`;
  const setHiddenOne = (repo: string, n: number, on: boolean) => setHidden((h) => { const x = new Set(h); if (on) x.add(hideKey(repo, n)); else x.delete(hideKey(repo, n)); return x; });
  const [state, setState] = useState<"open" | "all">("open");
  const [groups, setGroups] = useState<Record<string, Group>>({});
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [pick, setPick] = useState(false);
  const [filter, setFilter] = useState("");
  // again when a workspace's repository becomes known (it is read while the workspace is prepared)
  const repoKey = list.map((d) => `${d.id}:${d.github_repo ?? ""}`).join(",");
  useEffect(() => { void get<{ repos: KnownRepo[] }>("/dev/repos").then((r) => setKnown(r.repos)).catch(() => undefined); }, [repoKey]);

  // the repositories shown: the ones you develop here, then the ones you added (each once)
  const repos = useMemo(() => {
    const out: { repo: string; source: string | null; added: boolean }[] = [];
    for (const k of known) out.push({ repo: k.repo, source: k.source, added: false });
    for (const e of extra) if (!out.some((o) => lc(o.repo) === lc(e.repo))) out.push({ ...e, added: true });
    return out;
  }, [known, extra]);

  const loadOne = useCallback(async (q: { repo?: string; source?: string }, key?: string) => {
    const k = key ?? lc(q.repo ?? "");
    if (k) setGroups((g) => ({ ...g, [k]: { ...(g[k] ?? { repo: q.repo ?? "", source: q.source ?? null, prs: null }), err: null, loading: true } }));
    try {
      const r = await get<{ repo: string; source: string | null; prs: MyPr[] }>(`/dev/my-prs?${new URLSearchParams({ ...q, state } as Record<string, string>)}`);
      setGroups((g) => ({ ...g, [lc(r.repo)]: { repo: r.repo, source: r.source, prs: r.prs, err: null, loading: false } }));
      return r;
    } catch (e) {
      if (k) setGroups((g) => ({ ...g, [k]: { ...(g[k] ?? { repo: q.repo ?? k, source: null, prs: null }), err: (e as ApiError).message, loading: false } }));
      throw e;
    }
  }, [state]);
  const loadAll = useCallback(() => { for (const r of repos) void loadOne({ repo: r.repo }).catch(() => undefined); }, [repos, loadOne]);
  useEffect(() => { if (open) loadAll(); }, [open, state, repos.map((r) => r.repo).join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  const add = async (q: { repo?: string; source?: string }) => {
    setErr(null);
    try {
      const r = await loadOne(q);
      setExtra((x) => (x.some((e) => lc(e.repo) === lc(r.repo)) || known.some((k) => lc(k.repo) === lc(r.repo)) ? x : [...x, { repo: r.repo, source: r.source }]));
      setFolded((f) => { const n = new Set(f); n.delete(lc(r.repo)); return n; });
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };

  const active = list.find((d) => d.id === activeId) ?? null;
  const sameRepo = (d: DevWorkspace, repo: string) => lc(d.github_repo ?? "") === lc(repo);
  /** the repository's local folder workspace (your checkout, used directly): PRs switch its branch there */
  const folderOf = (repo: string, source: string | null) =>
    (active && active.mode === "folder" && active.status === "ready" && sameRepo(active, repo) ? active : null)
    ?? list.find((d) => d.mode === "folder" && d.status === "ready" && sameRepo(d, repo))
    ?? (source ? list.find((d) => d.mode === "folder" && d.status === "ready" && d.worktree_path === source) : undefined) ?? null;

  const checkoutIn = async (ws: DevWorkspace, repo: string, p: MyPr) => {
    setBusy(`${repo}#${p.number}`);
    setErr(null);
    try {
      await post(`/dev/workspaces/${ws.id}/checkout-pr`, { number: p.number });
      onOpen(ws.id);
      void loadOne({ repo }).catch(() => undefined);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };
  const openPr = async (g: Group, p: MyPr) => {
    const existing = p.workspaces[0];
    if (existing) return onOpen(existing.id);
    setErr(null);
    const ws = folderOf(g.repo, g.source);
    if (ws) return void checkoutIn(ws, g.repo, p);
    if (!g.source) return onSetup({ mode: "pr", github_repo: g.repo, pr_number: p.number });
    if (!(await confirmAction({ title: `Switch ${g.source} to #${p.number}?`, message: `Your local checkout of ${g.repo} switches to the pull request's branch ${p.headRefName} (uncommitted changes go along; git refuses if they would be overwritten). It opens as a workspace that works in that folder directly.`, confirmLabel: "Switch the folder", danger: false }))) return;
    setBusy(`${g.repo}#${p.number}`);
    try {
      const wiki = localStorage.getItem("waw.devWiki") || undefined;
      onCreated(await post<DevWorkspace>("/dev/workspaces", { host: "local", source_repo: g.source, mode: "folder", github_repo: g.repo, pr_number: p.number, wiki_workspace_id: wiki }));
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };

  const f = filter.trim().toLowerCase();
  const keep = (p: MyPr) => !f || `#${p.number} ${p.title} ${p.headRefName}`.toLowerCase().includes(f);
  const total = repos.reduce((n, r) => n + (groups[lc(r.repo)]?.prs ?? []).filter((p) => !hidden.has(hideKey(r.repo, p.number))).length, 0);
  const toggle = (k: string) => setFolded((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n; });

  return (
    <div className={`my-prs ${open ? "" : "folded"}`} data-testid="my-prs">
      <div className="my-prs-head">
        <button className="my-prs-title" onClick={() => setOpen(!open)} aria-expanded={open} data-testid="my-prs-toggle">
          <span className="chev">{open ? "⌄" : "›"}</span> <b>My pull requests</b> <span className="muted small">({total})</span>
        </button>
        <span className="spacer" />
        {open && <>
          <select value={state} onChange={(e) => setState(e.target.value as "open" | "all")} title="Which pull requests" data-testid="my-prs-state">
            <option value="open">open</option>
            <option value="all">all</option>
          </select>
          <button className="icon-btn small" onClick={loadAll} title="Refresh from GitHub" data-testid="my-prs-refresh">↻</button>
        </>}
      </div>
      {open && (
        <div className="my-prs-body">
          <form className="row nowrap" onSubmit={(e) => { e.preventDefault(); const t = typed.trim().replace(/^https:\/\/github\.com\//, "").replace(/\.git$|\/$/g, ""); if (t) { setTyped(""); void add({ repo: t }); } }}>
            <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="add a repository: owner/repo" className="mono" data-testid="my-prs-repo-input" />
            <button className="btn small" disabled={!typed.trim()}>Add</button>
            <button type="button" className="btn small" onClick={() => setPick(true)} title="Add the repository of a local checkout or folder" data-testid="my-prs-folder">Folder…</button>
          </form>
          {total > 6 && <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="filter: title, #number, branch" data-testid="my-prs-filter" />}
          {err && <div className="error-text small" role="alert" data-testid="my-prs-error">{err}</div>}
          {repos.length === 0 && <div className="muted small">No repository yet: create a workspace, or add one above.</div>}
          <div className="my-prs-list">
            {repos.map((r) => {
              const k = lc(r.repo);
              const g: Group = groups[k] ?? { repo: r.repo, source: r.source, prs: null, err: null, loading: true };
              const source = g.source ?? r.source;
              const ws = folderOf(r.repo, source);
              const removed = (g.prs ?? []).filter((p) => hidden.has(hideKey(r.repo, p.number)));
              const shown = (g.prs ?? []).filter((p) => keep(p) && (showHidden.has(k) || !hidden.has(hideKey(r.repo, p.number))));
              const isOpen = !folded.has(k);
              return (
                <div key={k} className="my-prs-group" data-testid="my-prs-group" data-repo={r.repo}>
                  <div className="my-prs-group-head">
                    <button className="my-prs-group-title" onClick={() => toggle(k)} aria-expanded={isOpen}
                      title={`${r.repo}${ws ? `\nLocal folder: ${ws.worktree_path} (on ${ws.branch})` : source ? `\nLocal checkout: ${source}` : "\nNo local checkout known"}`}>
                      <span className="chev">{isOpen ? "⌄" : "›"}</span>
                      <span className="my-prs-group-text">
                        <span className="my-prs-repo"><b>{r.repo}</b></span>
                        <span className="muted small mono">{ws ? `📁 ${home(ws.worktree_path)} · ${ws.branch}` : source ? `📁 ${home(source)}` : "no local folder"}</span>
                      </span>
                      <span className="git-badge clean">{g.loading && !g.prs ? "…" : g.prs ? g.prs.length - removed.length : "?"}</span>
                    </button>
                    {r.added && <button className="icon-btn small" onClick={() => setExtra((x) => x.filter((e) => lc(e.repo) !== k))} title="Remove from this list" aria-label={`Remove ${r.repo}`}>×</button>}
                  </div>
                  {isOpen && <>
                    {g.err && <div className="error-text small">{g.err}</div>}
                    {removed.length > 0 && (
                      <button className="link-btn small my-prs-empty" onClick={() => setShowHidden((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n; })} data-testid="my-prs-show-removed">
                        {showHidden.has(k) ? "hide removed" : `${removed.length} removed from this list · show`}
                      </button>
                    )}
                    {g.prs && shown.length === 0 && <div className="muted small my-prs-empty" data-testid="my-prs-empty">{f ? "Nothing matches." : `No ${state === "open" ? "open " : ""}pull requests of yours.`}</div>}
                    {shown.map((p) => {
                      const here = !!ws && ws.pr_number === p.number;
                      const label = busy === `${r.repo}#${p.number}`;
                      return (
                        <div key={p.number} className={`my-pr ${here ? "here" : ""} ${p.state !== "OPEN" ? "closed" : ""} ${hidden.has(hideKey(r.repo, p.number)) ? "removed" : ""}`} data-testid="my-pr" data-number={p.number}>
                          <button className="my-pr-main" onClick={() => void openPr(g, p)} disabled={busy !== null}
                            title={`${p.headRefName} → ${p.baseRefName}\n${p.workspaces.length ? `Open its workspace: ${p.workspaces.map((w) => w.name).join(", ")}` : ws ? `Switch ${ws.worktree_path} to its branch` : source ? `Switch your checkout ${source} to its branch` : "Open it in a new workspace"}`}>
                            <span className={`ci-dot ${(p.ci ?? "none").toLowerCase()}`} aria-hidden="true" />
                            <span className="my-pr-text">
                              <span className="my-pr-title">{p.isDraft && <span className="chip">draft</span>} {p.state !== "OPEN" && <span className="chip">{p.state.toLowerCase()}</span>} {p.title}</span>
                              <span className="muted small mono">#{p.number} · {p.headRefName} · {ago(p.updatedAt)}{p.reviewDecision === "APPROVED" ? " · approved" : p.reviewDecision === "CHANGES_REQUESTED" ? " · changes requested" : ""}</span>
                            </span>
                            {label ? <span className="muted small">switching…</span> : p.workspaces.length > 0 && <span className="chip" title="Has a workspace here">{here ? "here" : "open"}</span>}
                          </button>
                          {hidden.has(hideKey(r.repo, p.number)) ? (
                            <button className="btn small" onClick={() => setHiddenOne(r.repo, p.number, false)} title="Show it in this list again" data-testid="my-pr-restore">Restore</button>
                          ) : (
                            <button className="icon-btn small my-pr-close" onClick={() => setHiddenOne(r.repo, p.number, true)}
                              title="Remove from this list (nothing changes on GitHub; its workspace, if any, stays — “N removed · show” brings it back)" aria-label={`Remove #${p.number} from the list`} data-testid="my-pr-remove">×</button>
                          )}
                          <span className="my-pr-close"><DevPrState compact repo={r.repo} number={p.number} state={p.state}
                            onDone={(st) => setGroups((gs) => ({ ...gs, [k]: { ...g, prs: (g.prs ?? []).map((x) => (x.number === p.number ? { ...x, state: st } : x)) } }))} /></span>
                          {active && active.mode === "branch" && active.status === "ready" && sameRepo(active, r.repo) && active.pr_number !== p.number && p.state === "OPEN" && (
                            <button className="btn small" disabled={busy !== null} onClick={() => void checkoutIn(active, r.repo, p)} title={`Check it out in “${active.name}” (${active.worktree_path})`} data-testid="my-pr-here">Check out here</button>
                          )}
                        </div>
                      );
                    })}
                  </>}
                </div>
              );
            })}
          </div>
        </div>
      )}
      {pick && <FolderPicker title="Add the repository of…" hint="A local Git checkout of the repository." requireGit confirmLabel="Use this checkout"
        onClose={() => setPick(false)} onChoose={(p) => { setPick(false); void add({ source: p }); }} />}
    </div>
  );
}
