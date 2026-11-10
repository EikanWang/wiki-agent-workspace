import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { ApiError, get } from "../api";
import RefreshButton from "./RefreshButton";

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
  requested: "you" | "team";
  teams: string[];
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

  const groups = useMemo(() => {
    const f = filter.trim().toLowerCase();
    const items = (queue?.items ?? []).filter((p) => (showDrafts || !p.draft) && (!f || `${p.repo} #${p.number} ${p.title} ${p.author ?? ""} ${p.labels.map((l) => l.name).join(" ")}`.toLowerCase().includes(f)));
    const key = (p: ReviewPR) => (sort === "updated" ? -new Date(p.updated_at).getTime() : sort === "waiting" ? new Date(p.created_at).getTime() : p.additions + p.deletions);
    const by = new Map<string, ReviewPR[]>();
    for (const p of items) by.set(p.repo, [...(by.get(p.repo) ?? []), p]);
    return [...by.entries()].map(([repo, prs]) => ({ repo, prs: prs.sort((a, b) => key(a) - key(b)) })).sort((a, b) => b.prs.length - a.prs.length || a.repo.localeCompare(b.repo));
  }, [queue, filter, showDrafts, sort]);
  const drafts = queue?.items.filter((p) => p.draft).length ?? 0;
  const shown = groups.reduce((n, g) => n + g.prs.length, 0);

  return (
    <div className="reviews-panel" data-testid="reviews-panel">
      <div className="tree-header">
        <span className="ws-name">Reviews{queue ? ` · ${reviewBadge(queue)}` : ""}</span>
        <span className="spacer" />
        <RefreshButton onRefresh={() => loadReviews(true)} title="Refresh from GitHub" testId="reviews-refresh" />
      </div>
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
        {groups.map(({ repo, prs }) => {
          const open = !collapsed.has(repo);
          return (
            <div key={repo} className="review-group" data-testid="review-group" data-repo={repo}>
              <button className="review-group-head" onClick={() => setCollapsed((s) => { const n = new Set(s); if (n.has(repo)) n.delete(repo); else n.add(repo); return n; })} aria-expanded={open}>
                <span className="chev">{open ? "⌄" : "›"}</span>
                <span className="review-repo">{repo}</span>
                {prs[0]?.private && <span className="git-badge" title="private repository">private</span>}
                <span className="spacer" />
                <span className="git-badge clean">{prs.length}</span>
              </button>
              {open &&
                prs.map((p) => (
                  <button key={p.number} className={`review-pr ${p.draft ? "draft" : ""}`} onClick={() => onOpen(p)} title={`${p.repo}#${p.number}\n${p.title}\n${CI_LABEL[p.ci ?? ""] ?? "no checks"}${p.teams.length ? `\nalso requested from: ${p.teams.join(", ")}` : ""}`} data-testid="review-pr" data-number={p.number}>
                    <span className={`ci-dot ${(p.ci ?? "none").toLowerCase()}`} aria-label={CI_LABEL[p.ci ?? ""] ?? "no checks"} />
                    <span className="review-pr-main">
                      <span className="review-pr-title">{p.title}</span>
                      <span className="review-pr-meta muted small">
                        #{p.number} · {p.author ?? "?"} · {ago(p.updated_at)} · <span className="add">+{p.additions}</span> <span className="del">−{p.deletions}</span>
                        {p.draft && <span className="git-badge"> draft</span>}
                        {p.labels.slice(0, 2).map((l) => (
                          <span key={l.name} className="review-label" style={l.color ? { borderColor: `#${l.color}` } : undefined}>{l.name}</span>
                        ))}
                      </span>
                    </span>
                  </button>
                ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}
