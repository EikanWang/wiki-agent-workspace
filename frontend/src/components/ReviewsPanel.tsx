import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { ApiError, get, put } from "../api";
import RefreshButton from "./RefreshButton";
import { confirmAction } from "./Confirm";

export interface ReviewPR {
  repo: string;
  private: boolean;
  number: number;
  title: string;
  url: string;
  draft: boolean;
  author: string | null;
  created_at: string;
  updated_at: string;
  additions: number;
  deletions: number;
  changed_files: number;
  review_decision: string | null;
  ci: "SUCCESS" | "FAILURE" | "ERROR" | "PENDING" | "EXPECTED" | null;
  labels: { name: string; color: string | null }[];
  requested: "you" | "team" | null; // null: not requested — shown because its repository is watched
  teams: string[];
  requested_by?: string | null;
  requested_by_company?: string | null;
  priority?: boolean;
  watched?: boolean;
}
export interface HubPrefs {
  hidden_repos: string[];
  watched_repos: string[];
  priority_requesters: string[];
  priority_meta: boolean;
}
interface Queue {
  items: ReviewPR[];
  total: number;
  hidden: number;
  note: string | null;
  truncated: boolean;
  fetched_at: string;
  age_seconds: number;
  refreshing: boolean;
  refresh_error: { code: string; message: string } | null;
  watched?: ReviewPR[];
  watch_problems?: string[];
  hidden_count?: number;
  prefs?: HubPrefs;
}

// ---- one shared copy of the queue (the activity-bar badge and the panel use the same data)
let state: { queue: Queue | null; error: string | null; loading: boolean } = { queue: null, error: null, loading: false };
const subs = new Set<() => void>();
const set = (s: Partial<typeof state>) => {
  state = { ...state, ...s };
  subs.forEach((f) => f());
};
let poll: number | undefined;

/** Load the queue; while the server refreshes it in the background, poll until the fresh result is in. */
export async function loadReviews(refresh = false): Promise<void> {
  window.clearTimeout(poll);
  set({ loading: true });
  try {
    const q = await get<Queue>(`/github/reviews${refresh ? "?refresh=true" : ""}`);
    set({ queue: q, error: null, loading: false });
    if (q.refreshing) poll = window.setTimeout(() => void loadReviews(false), 3000);
  } catch (e) {
    set({ error: (e as ApiError).message, loading: false });
  }
}

/** Save Reviews preferences (hidden / watched repositories, priority requesters), then reload the list. */
export async function savePrefs(change: Partial<HubPrefs>): Promise<void> {
  await put<HubPrefs>("/github/prefs", change);
  await loadReviews(false);
}

export function useReviews() {
  return useSyncExternalStore(
    (f) => {
      subs.add(f);
      return () => subs.delete(f);
    },
    () => state,
  );
}

/** Number shown on the activity bar: PRs that are ready for review (not drafts). */
export function reviewBadge(q: Queue | null): number {
  return q ? q.items.filter((p) => !p.draft).length : 0;
}

const ago = (iso: string) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  if (s < 86400 * 60) return `${Math.round(s / 86400)}d`;
  return `${Math.round(s / (86400 * 30))}mo`;
};
const CI_LABEL: Record<string, string> = { SUCCESS: "checks passed", FAILURE: "checks failing", ERROR: "checks errored", PENDING: "checks running", EXPECTED: "checks expected" };

type Sort = "updated" | "waiting" | "size";

/** Sidebar view: the pull requests waiting for your review, grouped by repository. */
export default function ReviewsPanel({ onOpen }: { onOpen: (pr: ReviewPR) => void }) {
  const { queue, error, loading } = useReviews();
  const [filter, setFilter] = useState("");
  const [showDrafts, setShowDrafts] = useState(() => localStorage.getItem("waw.reviews.drafts") === "1");
  const [sort, setSort] = useState<Sort>(() => (localStorage.getItem("waw.reviews.sort") as Sort) || "updated");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set(JSON.parse(localStorage.getItem("waw.reviews.collapsed") || "[]")));
  useEffect(() => localStorage.setItem("waw.reviews.drafts", showDrafts ? "1" : "0"), [showDrafts]);
  useEffect(() => localStorage.setItem("waw.reviews.sort", sort), [sort]);
  useEffect(() => localStorage.setItem("waw.reviews.collapsed", JSON.stringify([...collapsed])), [collapsed]);
  useEffect(() => {
    if (!state.queue && !state.loading) void loadReviews();
  }, []);

  const [settings, setSettings] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const prefs = queue?.prefs;
  const { groups, priority, watchedGroups } = useMemo(() => {
    const f = filter.trim().toLowerCase();
    const keep = (p: ReviewPR) => (showDrafts || !p.draft) && (!f || `${p.repo} #${p.number} ${p.title} ${p.author ?? ""} ${p.requested_by ?? ""} ${p.labels.map((l) => l.name).join(" ")}`.toLowerCase().includes(f));
    const key = (p: ReviewPR) => (sort === "updated" ? -new Date(p.updated_at).getTime() : sort === "waiting" ? new Date(p.created_at).getTime() : p.additions + p.deletions);
    const group = (list: ReviewPR[]) => {
      const by = new Map<string, ReviewPR[]>();
      for (const p of list) by.set(p.repo, [...(by.get(p.repo) ?? []), p]);
      return [...by.entries()].map(([repo, prs]) => ({ repo, prs: prs.sort((a, b) => key(a) - key(b)) })).sort((a, b) => b.prs.length - a.prs.length || a.repo.localeCompare(b.repo));
    };
    const items = (queue?.items ?? []).filter(keep);
    return {
      priority: items.filter((p) => p.priority).sort((a, b) => key(a) - key(b)),
      groups: group(items.filter((p) => !p.priority)),
      watchedGroups: group((queue?.watched ?? []).filter(keep)),
    };
  }, [queue, filter, showDrafts, sort]);
  const drafts = queue?.items.filter((p) => p.draft).length ?? 0;
  const shown = priority.length + groups.reduce((n, g) => n + g.prs.length, 0);
  const change = async (what: string, c: Partial<HubPrefs>) => {
    setBusy(what);
    try {
      await savePrefs(c);
    } catch (e) {
      set({ error: (e as ApiError).message });
    } finally {
      setBusy(null);
    }
  };
  const hideRepo = async (repo: string) => {
    if (!prefs) return;
    const n = (queue?.items ?? []).filter((p) => p.repo === repo).length;
    const ok = await confirmAction({
      title: `Hide ${repo} from Reviews?`,
      message: `Its ${n ? `${n} review request(s) are` : "open PRs are"} no longer listed${prefs.watched_repos.some((r) => r.toLowerCase() === repo.toLowerCase()) ? " and it is no longer watched" : ""}. Nothing changes on GitHub. Show it again under ⚙ → Hidden repositories.`,
      confirmLabel: "Hide repository",
    });
    if (ok) void change(`hide:${repo}`, { hidden_repos: [...prefs.hidden_repos, repo], watched_repos: prefs.watched_repos.filter((r) => r.toLowerCase() !== repo.toLowerCase()) });
  };
  const toggle = (repo: string) => setCollapsed((s) => { const n = new Set(s); if (n.has(repo)) n.delete(repo); else n.add(repo); return n; });
  const prRow = (p: ReviewPR, showRepo = false) => (
    <button key={`${p.repo}#${p.number}`} className={`review-pr ${p.draft ? "draft" : ""} ${p.priority ? "priority" : ""}`} onClick={() => onOpen(p)}
      title={`${p.repo}#${p.number}\n${p.title}\n${CI_LABEL[p.ci ?? ""] ?? "no checks"}${p.requested_by ? `\nreview requested by ${p.requested_by}${p.requested_by_company ? ` (${p.requested_by_company})` : ""}` : ""}${p.teams.length ? `\nalso requested from: ${p.teams.join(", ")}` : ""}`}
      data-testid="review-pr" data-number={p.number} data-priority={p.priority ? "1" : "0"}>
      <span className={`ci-dot ${(p.ci ?? "none").toLowerCase()}`} aria-label={CI_LABEL[p.ci ?? ""] ?? "no checks"} />
      <span className="review-pr-main">
        <span className="review-pr-title">{p.title}</span>
        <span className="review-pr-meta muted small">
          {showRepo ? `${p.repo}` : ""}#{p.number} · {p.author ?? "?"} · {ago(p.updated_at)} · <span className="add">+{p.additions}</span> <span className="del">−{p.deletions}</span>
          {p.requested_by && <span className="review-by" data-testid="review-by"> · asked by {p.requested_by}</span>}
          {p.draft && <span className="git-badge"> draft</span>}
          {p.labels.slice(0, 2).map((l) => (
            <span key={l.name} className="review-label" style={l.color ? { borderColor: `#${l.color}` } : undefined}>{l.name}</span>
          ))}
        </span>
      </span>
    </button>
  );
  const groupBox = (repo: string, prs: ReviewPR[], watched: boolean) => {
    const open = !collapsed.has((watched ? "w:" : "") + repo);
    return (
      <div key={(watched ? "w:" : "") + repo} className="review-group" data-testid={watched ? "review-group-watched" : "review-group"} data-repo={repo}>
        <div className="review-group-head-row">
          <button className="review-group-head" onClick={() => toggle((watched ? "w:" : "") + repo)} aria-expanded={open}>
            <span className="chev">{open ? "⌄" : "›"}</span>
            <span className="review-repo">{repo}</span>
            {prs[0]?.private && <span className="git-badge" title="private repository">private</span>}
            {watched && <span className="git-badge" title="Open PRs of a repository you added (not requested from you)">watching</span>}
            <span className="spacer" />
            <span className="git-badge clean">{prs.length}</span>
          </button>
          {prefs && (
            <button className="icon-btn small review-hide" onClick={() => void hideRepo(repo)} disabled={busy !== null} title={`Hide ${repo} from Reviews (undo under ⚙)`} aria-label={`Hide ${repo}`} data-testid="review-hide-repo">×</button>
          )}
        </div>
        {open && prs.map((p) => prRow(p))}
      </div>
    );
  };

  return (
    <div className="reviews-panel" data-testid="reviews-panel">
      <div className="tree-header">
        <span className="ws-name">Reviews{queue ? ` · ${reviewBadge(queue)}` : ""}</span>
        <span className="spacer" />
        {prefs && (
          <button className={`icon-btn ${settings ? "active" : ""}`} onClick={() => setSettings((v) => !v)} title="Repositories and priority" aria-label="Reviews settings" aria-pressed={settings} data-testid="reviews-settings">⚙</button>
        )}
        <RefreshButton onRefresh={() => loadReviews(true)} title="Refresh from GitHub" testId="reviews-refresh" />
      </div>
      {settings && prefs && <ReviewsSettings prefs={prefs} busy={busy} change={change} problems={queue?.watch_problems ?? []} />}
      <div className="reviews-controls">
        <input className="reviews-filter" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter: repo, title, author, label…" data-testid="reviews-filter" />
        <div className="reviews-row">
          <label className="small" title={`${drafts} draft PR(s)`}>
            <input type="checkbox" checked={showDrafts} onChange={(e) => setShowDrafts(e.target.checked)} data-testid="reviews-drafts" /> drafts ({drafts})
          </label>
          <select className="reviews-sort" value={sort} onChange={(e) => setSort(e.target.value as Sort)} data-testid="reviews-sort" aria-label="Sort">
            <option value="updated">Recently updated</option>
            <option value="waiting">Waiting longest</option>
            <option value="size">Smallest first</option>
          </select>
        </div>
      </div>
      <div className="tree-body" data-testid="reviews-list">
        {!queue && loading && <div className="tree-loading">Loading your review requests from GitHub… (the first load can take ~30 s)</div>}
        {error && <div className="tree-error" role="alert" data-testid="reviews-error">{error}</div>}
        {queue?.refresh_error && <div className="tree-error stale">Could not refresh ({queue.refresh_error.message}); showing the earlier list.</div>}
        {queue?.note && <div className="reviews-note small" data-testid="reviews-note">{queue.note}</div>}
        {queue && (
          <div className="muted small reviews-meta">
            Requested from you: {queue.items.length}{queue.truncated ? `+ (of ${queue.total})` : ""} · showing {shown}
            {queue.refreshing ? " · refreshing…" : ` · updated ${ago(queue.fetched_at)} ago`}
          </div>
        )}
        {queue && shown === 0 && <div className="tree-empty">{queue.items.length ? "Nothing matches the filter." : "No pull requests are waiting for your review. 🎉"}</div>}
        {priority.length > 0 && (
          <div className="review-group priority" data-testid="review-priority">
            <button className="review-group-head" onClick={() => toggle("!priority")} aria-expanded={!collapsed.has("!priority")}
              title={`Requests from ${prefs?.priority_meta ? "PyTorch Meta maintainers (GitHub profile company / public email)" : ""}${prefs?.priority_meta && prefs.priority_requesters.length ? " and " : ""}${prefs?.priority_requesters.length ? prefs.priority_requesters.join(", ") : ""}`}>
              <span className="chev">{collapsed.has("!priority") ? "›" : "⌄"}</span>
              <span className="review-repo">★ Priority</span>
              <span className="spacer" />
              <span className="git-badge clean">{priority.length}</span>
            </button>
            {!collapsed.has("!priority") && priority.map((p) => prRow(p, true))}
          </div>
        )}
        {groups.map(({ repo, prs }) => groupBox(repo, prs, false))}
        {watchedGroups.length > 0 && <div className="muted small reviews-meta" data-testid="reviews-watched-head">Watched repositories (all open PRs)</div>}
        {watchedGroups.map(({ repo, prs }) => groupBox(repo, prs, true))}
        {(queue?.hidden_count ?? 0) > 0 && <div className="muted small reviews-meta">{queue!.hidden_count} request(s) in hidden repositories (⚙ to show them)</div>}
      </div>
    </div>
  );
}

function ReviewsSettings({ prefs, busy, change, problems }: {
  prefs: HubPrefs; busy: string | null; change: (what: string, c: Partial<HubPrefs>) => Promise<void>; problems: string[];
}) {
  const [repo, setRepo] = useState("");
  const [people, setPeople] = useState(prefs.priority_requesters.join(", "));
  useEffect(() => setPeople(prefs.priority_requesters.join(", ")), [prefs.priority_requesters]);
  const valid = /^[\w.-]+\/[\w.-]+$/.test(repo.trim());
  const watch = () => {
    const r = repo.trim();
    if (!valid) return;
    void change("watch", { watched_repos: [...prefs.watched_repos.filter((x) => x.toLowerCase() !== r.toLowerCase()), r], hidden_repos: prefs.hidden_repos.filter((x) => x.toLowerCase() !== r.toLowerCase()) }).then(() => setRepo(""));
  };
  return (
    <div className="reviews-settings" data-testid="reviews-settings-box">
      <div className="small strong">Priority requests</div>
      <label className="small">
        <input type="checkbox" checked={prefs.priority_meta} disabled={busy !== null} onChange={(e) => void change("meta", { priority_meta: e.target.checked })} data-testid="reviews-priority-meta" />{" "}
        PyTorch Meta maintainers (company or public email on their GitHub profile)
      </label>
      <div className="reviews-settings-row">
        <input value={people} onChange={(e) => setPeople(e.target.value)} placeholder="Also these GitHub logins, comma-separated" data-testid="reviews-priority-people"
          onKeyDown={(e) => { if (e.key === "Enter") void change("people", { priority_requesters: people.split(/[\s,]+/).filter(Boolean) }); }} />
        <button className="btn small" disabled={busy !== null} onClick={() => void change("people", { priority_requesters: people.split(/[\s,]+/).filter(Boolean) })} data-testid="reviews-priority-save">Save</button>
      </div>
      <div className="small strong">Watched repositories <span className="muted">(all their open PRs, besides your review requests)</span></div>
      {prefs.watched_repos.map((r) => (
        <div key={r} className="reviews-settings-item" data-testid="reviews-watched-item">
          <span className="mono small">{r}</span>
          <button className="icon-btn small" disabled={busy !== null} onClick={() => void confirmAction({ title: `Stop watching ${r}?`, message: "Its open PRs leave Reviews (review requests from it stay). Watch it again any time.", confirmLabel: "Stop watching" }).then((ok) => { if (ok) void change(`unwatch:${r}`, { watched_repos: prefs.watched_repos.filter((x) => x !== r) }); })} title="Stop watching" aria-label={`Stop watching ${r}`}>×</button>
        </div>
      ))}
      {problems.map((m) => <div key={m} className="tree-error small">{m}</div>)}
      <div className="reviews-settings-row">
        <input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="owner/name, e.g. intel/torch-xpu-ops" onKeyDown={(e) => { if (e.key === "Enter") watch(); }} data-testid="reviews-watch-input" />
        <button className="btn small" disabled={!valid || busy !== null} onClick={watch} data-testid="reviews-watch-add">{busy === "watch" ? "Adding…" : "Watch"}</button>
      </div>
      <div className="small strong">Hidden repositories</div>
      {prefs.hidden_repos.length === 0 && <div className="muted small">None — use × on a repository to hide it.</div>}
      {prefs.hidden_repos.map((r) => (
        <div key={r} className="reviews-settings-item" data-testid="reviews-hidden-item">
          <span className="mono small">{r}</span>
          <button className="btn small" disabled={busy !== null} onClick={() => void change(`unhide:${r}`, { hidden_repos: prefs.hidden_repos.filter((x) => x !== r) })} data-testid="reviews-unhide">Show again</button>
        </div>
      ))}
    </div>
  );
}
