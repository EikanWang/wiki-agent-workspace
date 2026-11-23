import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import DeferMenu, { Deferral, deferLabel } from "./DeferMenu";
import GithubRate from "./GithubRate";
import { ApiError, get, post, put } from "../api";
import RefreshButton from "./RefreshButton";
import MonitorsPanel from "./MonitorsPanel";
import { confirmAction } from "./Confirm";
import GhUser from "./GhUser";
import { useRestoredScroll } from "../persist";
import { GhRefreshSelect } from "../ghRefresh";
import { LearningSettings, useLearningSummary } from "./ReviewLearning";
import { ReviewSkills } from "./ReviewSkills";
import { AgentStatus, CATEGORY_HINT, CacheSettings, Category, LevelMenu, PriorityAgent, PriorityLevel, PriorityRule, PrioritySettings } from "./ReviewPriority";

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
  /** null: not requested — shown because its repository is watched; codeowner: not requested, but it touches
   *  files you own in CODEOWNERS (e.g. a draft: GitHub requests code owners only once a PR is ready) */
  requested: "you" | "team" | "codeowner" | null;
  review_requested?: boolean;
  mentioned?: boolean;
  /** only mentioning you (no review request): who wrote the mention — a person, or only a bot ("cc'd by bot") */
  mention_by?: "person" | "bot";
  mention_who?: string;
  /** the files of this PR you own in CODEOWNERS (up to 20) and how many there are */
  owned_paths?: string[];
  owned_count?: number;
  teams: string[];
  requested_by?: string | null;
  requested_by_company?: string | null;
  /** what the repository publishes about the requester / author (maintainer list, CODEOWNERS, your list) */
  requested_by_roles?: string;
  /** deferred by you (only in `deferred`); a deferral that just ended (back in the list) */
  deferred?: Deferral;
  deferred_ended?: Deferral;
  /** hidden by you (only in `hidden_prs`); hidden, but back because your review was requested or someone mentioned you since */
  hidden_pr?: HiddenPr;
  hidden_ended?: HiddenPr & { why: string[] };
  requested_by_core?: boolean;
  author_roles?: string;
  priority?: boolean;
  watched?: boolean;
  /** OPEN | CLOSED | MERGED */
  state?: string;
  /** your latest standing review (APPROVED, CHANGES_REQUESTED, COMMENTED…) and what happened since an approval */
  my_review?: string | null;
  approved_no_news?: boolean;
  since_approval?: string[];
  /** todo: you have not reviewed / commented yet; news: something happened since your latest review or comment;
   *  quiet: nothing since */
  my_activity?: "todo" | "news" | "quiet";
  my_last_at?: string | null;
  since_mine?: string[];
  /** the agent's review of this PR: still running, or done and waiting for you to apply / discard its suggestions */
  agent_review?: { state: "running" | "waiting"; proposed: number; unread?: boolean } | null;
  /** priority: the level (id), its rank (0 = most urgent), where it comes from, and why */
  level?: string;
  level_rank?: number;
  level_source?: "manual" | "rule" | "agent" | "default";
  level_reasons?: string[];
}

/** The queue's sections inside each repository, in your order (⚙ → Priority → Categories). */
const DEFAULT_CATEGORIES: Category[] = [
  { key: "todo", title: "Not reviewed yet", level: "top" },
  { key: "news", title: "New activity since your comments", level: "high" },
  { key: "quiet", title: "Commented, nothing new", level: "medium" },
];
const categoryOf = (p: ReviewPR) => p.my_activity ?? "todo";
/** the category choice above the list: short names (the full title is the tooltip) */

export interface MaintainerSource { repo: string; people: number; sources: string[]; fetched: number | null; error?: string | null }
/** a repository checked for open PRs touching your files: matches, your CODEOWNERS rules, the latest scan */
export interface CodeownedSource { repo: string; prs: number; rules: number | null; source: string | null; scanned_at: number | null; scanned: number | null; error: string | null; in_progress: boolean }
const ownedTitle = (p: ReviewPR) => p.owned_paths?.length
  ? `\nyou own ${p.owned_count ?? p.owned_paths.length} of its files (CODEOWNERS):\n  ${p.owned_paths.slice(0, 8).join("\n  ")}${(p.owned_count ?? 0) > 8 ? "\n  …" : ""}`
  : "";

/** A checkbox kept per browser. */
function usePersistedFlag(key: string): [boolean, (v: boolean) => void] {
  const [v, setV] = useState(() => localStorage.getItem(`waw.${key}`) === "1");
  return [v, (x: boolean) => { setV(x); localStorage.setItem(`waw.${key}`, x ? "1" : "0"); }];
}
export interface HubPrefs {
  hidden_repos: string[];
  watched_repos: string[];
  priority_requesters: string[];
  core_maintainers?: string[];
  priority_meta: boolean;
  show_approved: boolean;
  /** also list PRs that touch files you own in CODEOWNERS (not requested from you) */
  codeowned?: boolean;
  starred?: string[];
  unstarred?: string[];
  priority_levels: PriorityLevel[];
  priority_default: string;
  priority_rules: PriorityRule[];
  priority_agent: PriorityAgent;
  priority_manual: Record<string, string>;
  categories: Category[];
  /** PRs you hid ("owner/repo#n", lower case): out of Reviews until your review is requested or someone mentions you */
  hidden_prs?: Record<string, HiddenPr>;
}
export interface HiddenPr { at: string; title: string }
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
  maintainers?: MaintainerSource[];
  codeowned?: CodeownedSource[];
  codeowned_problems?: string[];
  deferred?: ReviewPR[];
  /** PRs that mention you only in a bot-written cc line (e.g. pytorch-bot's label subscribers): hidden unless asked for */
  bot_cc?: ReviewPR[];
  /** PRs you hid that are open and would be listed */
  hidden_prs?: ReviewPR[];
  hidden_count?: number;
  approved_hidden?: number;
  prefs?: HubPrefs;
  priority_agent?: AgentStatus;
  batch_review?: BatchStatus;
}
/** Agent reviews started for several PRs at once (POST /github/batch-review): waiting in order, running, failed. */
export interface BatchStatus {
  queued: string[];
  running: Record<string, "preparing" | "reviewing">;
  failed: Record<string, { error: string; code?: string | null; at: string }>;
  concurrency: number;
  max_prs?: number;
}
const prKey = (p: { repo: string; number: number }) => `${p.repo}#${p.number}`.toLowerCase();

type Sort = "updated" | "waiting" | "size";
type ReviewSource = "all" | "requested" | "mentioned" | "both";

const reviewRequested = (p: ReviewPR) => p.review_requested ?? (p.requested === "you" || p.requested === "team");
const matchesSource = (p: ReviewPR, source: ReviewSource) => source === "all"
  || (source === "requested" && reviewRequested(p))
  || (source === "mentioned" && !!p.mentioned)
  || (source === "both" && reviewRequested(p) && !!p.mentioned);

/** The panel's filters (what the list shows); the activity-bar badge counts with the same ones. */
export interface ReviewView {
  filter: string;
  source: ReviewSource;
  showDrafts: boolean;
  agentOnly: boolean;
  maxRank: number;
}
const storedView = (): ReviewView => {
  const saved = localStorage.getItem("waw.reviews.source");
  return {
    filter: "",
    source: ["requested", "mentioned", "both"].includes(saved ?? "") ? saved as ReviewSource : "all",
    showDrafts: localStorage.getItem("waw.reviews.drafts") === "1",
    agentOnly: localStorage.getItem("waw.reviews.agentOnly") === "1",
    maxRank: Number(localStorage.getItem("waw.reviews.maxRank") ?? "99"),
  };
};
const keepPr = (p: ReviewPR, v: ReviewView) => {
  const f = v.filter.trim().toLowerCase();
  return (v.agentOnly ? p.agent_review?.state === "waiting" : v.showDrafts || !p.draft) && (p.level_rank ?? 0) <= v.maxRank
    && matchesSource(p, v.source)
    && (!f || `${p.repo} #${p.number} ${p.title} ${p.author ?? ""} ${p.requested_by ?? ""} ${p.level ?? ""} ${p.labels.map((l) => l.name).join(" ")}`.toLowerCase().includes(f));
};

// ---- one shared copy of the queue and of the panel's filters (the activity-bar badge and the panel use the same data)
let state: { queue: Queue | null; error: string | null; loading: boolean; view: ReviewView } = { queue: null, error: null, loading: false, view: storedView() };
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
    const deciding = q.priority_agent ? q.priority_agent.queued + q.priority_agent.running.length : 0;
    const batch = q.batch_review ? q.batch_review.queued.length + Object.keys(q.batch_review.running).length : 0;
    if (q.refreshing || deciding || batch) poll = window.setTimeout(() => void loadReviews(false), deciding && !q.refreshing ? 2500 : 3000); // new levels / reviews come in
  } catch (e) {
    set({ error: (e as ApiError).message, loading: false });
  }
}

/** Save Reviews preferences (hidden / watched repositories, priority requesters), then reload the list. */
export async function savePrefs(change: Partial<HubPrefs> & Record<string, unknown>): Promise<void> {
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

/** Number shown on the activity bar: the PRs the list shows with the panel's current filters ("showing N"). */
export function reviewBadge(q: Queue | null, view: ReviewView = state.view): number {
  return q ? q.items.filter((p) => keepPr(p, view)).length : 0;
}

const ago = (iso: string) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  if (s < 86400 * 60) return `${Math.round(s / 86400)}d`;
  return `${Math.round(s / (86400 * 30))}mo`;
};
const CI_LABEL: Record<string, string> = { SUCCESS: "checks passed", FAILURE: "checks failing", ERROR: "checks errored", PENDING: "checks running", EXPECTED: "checks expected" };

/** Sidebar view: the pull requests waiting for your review, grouped by repository. */
export default function ReviewsPanel({ onOpen, onOpenPr }: { onOpen: (pr: ReviewPR) => void; onOpenPr: (repo: string, number: number, title: string) => void }) {
  // Review queue | Monitors (saved GitHub searches per repository; they leave the queue, Deferred and the badge alone)
  const [tab, setTab] = useState<"queue" | "monitors">(() => (localStorage.getItem("waw.reviews.tab") === "monitors" ? "monitors" : "queue"));
  useEffect(() => localStorage.setItem("waw.reviews.tab", tab), [tab]);
  const [monUnseen, setMonUnseen] = useState(0);
  const { queue, error, loading } = useReviews();
  const [filter, setFilter] = useState("");
  const [source, setSource] = useState<ReviewSource>(() => {
    const saved = localStorage.getItem("waw.reviews.source");
    return ["requested", "mentioned", "both"].includes(saved ?? "") ? saved as ReviewSource : "all";
  });
  useEffect(() => localStorage.setItem("waw.reviews.source", source), [source]);
  const [showDrafts, setShowDrafts] = useState(() => localStorage.getItem("waw.reviews.drafts") === "1");
  const [agentOnly, setAgentOnly] = usePersistedFlag("reviews.agentOnly");
  const [showBotCc, setShowBotCc] = usePersistedFlag("reviews.botCc");
  const [maxRank, setMaxRank] = useState<number>(() => Number(localStorage.getItem("waw.reviews.maxRank") ?? "99"));
  useEffect(() => localStorage.setItem("waw.reviews.maxRank", String(maxRank)), [maxRank]);
  const agentWaiting = [...(queue?.items ?? []), ...(queue?.watched ?? [])].filter((p) => p.agent_review?.state === "waiting").length;
  const [sort, setSort] = useState<Sort>(() => (localStorage.getItem("waw.reviews.sort") as Sort) || "updated");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set(JSON.parse(localStorage.getItem("waw.reviews.collapsed") || "[]")));
  useEffect(() => localStorage.setItem("waw.reviews.drafts", showDrafts ? "1" : "0"), [showDrafts]);
  // the categories you chose to see (not reviewed yet / new activity / nothing new), kept per browser
  useEffect(() => localStorage.setItem("waw.reviews.sort", sort), [sort]);
  useEffect(() => localStorage.setItem("waw.reviews.collapsed", JSON.stringify([...collapsed])), [collapsed]);
  useEffect(() => {
    if (!state.queue && !state.loading) void loadReviews();
  }, []);

  const [settings, setSettings] = useState(false);
  const learnSummary = useLearningSummary(true);
  const learnWaiting = learnSummary.proposed + learnSummary.flagged; // lessons the agent learned that wait for you
  // search pull requests on GitHub (beyond the review queue)
  const [search, setSearch] = useState<string | null>(null);
  const [searchState, setSearchState] = useState<"open" | "closed" | "all">("open");
  const [results, setResults] = useState<{ items: ReviewPR[]; total: number; query: string } | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchErr, setSearchErr] = useState<string | null>(null);
  const runSearch = async (state = searchState) => {
    if (!search?.trim()) return;
    setSearching(true);
    setSearchErr(null);
    try {
      setResults(await get<{ items: ReviewPR[]; total: number; query: string }>(`/github/search?q=${encodeURIComponent(search.trim())}&state=${state}`));
    } catch (e) {
      setSearchErr((e as ApiError).message);
    } finally {
      setSearching(false);
    }
  };
  const listScroll = useRestoredScroll("reviews.list", !!queue);
  // several PRs selected (checkbox per row) → one Agent review for all of them
  const [selected, setSelected] = useState<Map<string, { repo: string; number: number }>>(() => new Map());
  const lastPick = useRef<string | null>(null);
  const order = useRef<{ key: string; repo: string; number: number }[]>([]); // rows in the order they are shown (for Shift-click)
  order.current = [];
  const [batchNote, setBatchNote] = useState<{ text: string; error?: boolean } | null>(null);
  const [starting, setStarting] = useState(false);
  const batch = queue?.batch_review;
  const batchBusy = batch ? batch.queued.length + Object.keys(batch.running).length : 0;
  const pick = (p: ReviewPR, range: boolean) => {
    const key = prKey(p);
    // the rows as shown now: React may run the update below during a later render, while `order` is being refilled
    const rows = order.current.slice();
    const from = range ? lastPick.current : null;
    setSelected((cur) => {
      const n = new Map(cur);
      const on = !cur.has(key);
      const a = from ? rows.findIndex((r) => r.key === from) : -1;
      const b = rows.findIndex((r) => r.key === key);
      if (a >= 0 && b >= 0) {
        for (const r of rows.slice(Math.min(a, b), Math.max(a, b) + 1)) {
          if (on) n.set(r.key, { repo: r.repo, number: r.number }); else n.delete(r.key);
        }
      } else if (on) n.set(key, { repo: p.repo, number: p.number });
      else n.delete(key);
      return n;
    });
    lastPick.current = key;
  };
  const pickAll = (prs: ReviewPR[], on: boolean) => setSelected((cur) => {
    const n = new Map(cur);
    for (const p of prs) { if (on) n.set(prKey(p), { repo: p.repo, number: p.number }); else n.delete(prKey(p)); }
    return n;
  });
  const startBatch = async () => {
    const prs = [...selected.values()];
    if (!prs.length) return;
    setStarting(true);
    setBatchNote(null);
    try {
      // the server takes at most max_prs per request: a big selection goes in parts, queued in order
      const r: { queued: string[]; skipped: { pr: string; reason: string }[] } = { queued: [], skipped: [] };
      const per = batch?.max_prs ?? 50;
      for (let i = 0; i < prs.length; i += per) {
        const part = await post<typeof r>("/github/batch-review", { prs: prs.slice(i, i + per) });
        r.queued.push(...part.queued);
        r.skipped.push(...part.skipped);
      }
      const skipped = r.skipped.length ? ` · skipped ${r.skipped.length}: ${r.skipped.map((x) => `${x.pr} (${x.reason})`).join(", ")}` : "";
      setBatchNote({ text: `🤖 Agent review started for ${r.queued.length} PR(s)${r.queued.length > (batch?.concurrency ?? 2) ? `, ${batch?.concurrency ?? 2} at a time` : ""}${skipped}. Open a PR to apply or discard its suggestions.` });
      setSelected(new Map());
      lastPick.current = null;
      await loadReviews(false);
    } catch (e) {
      setBatchNote({ text: (e as ApiError).message, error: true });
    } finally {
      setStarting(false);
    }
  };
  const cancelWaiting = async () => {
    try {
      await post("/github/batch-review/cancel", {});
      await loadReviews(false);
    } catch (e) {
      setBatchNote({ text: (e as ApiError).message, error: true });
    }
  };
  const [busy, setBusy] = useState<string | null>(null);
  const prefs = queue?.prefs;
  const levels = prefs?.priority_levels ?? [];
  const categories = prefs?.categories?.length ? prefs.categories : DEFAULT_CATEGORIES;
  // the badge counts with these filters: publish them whenever they change
  useEffect(() => set({ view: { filter, source, showDrafts, agentOnly, maxRank } }), [filter, source, showDrafts, agentOnly, maxRank]);
  useEffect(() => () => set({ view: { ...state.view, filter: "" } }), []); // the text filter is not kept when the panel closes
  const { groups, watchedGroups, botGroups } = useMemo(() => {
    const view = { filter, source, showDrafts, agentOnly, maxRank };
    const keep = (p: ReviewPR) => keepPr(p, view);
    const key = (p: ReviewPR) => (sort === "updated" ? -new Date(p.updated_at).getTime() : sort === "waiting" ? new Date(p.created_at).getTime() : p.additions + p.deletions);
    // inside a repository (and a category): the most urgent level first, then your sort
    const cmp = (a: ReviewPR, b: ReviewPR) => (a.level_rank ?? 99) - (b.level_rank ?? 99) || key(a) - key(b);
    const group = (list: ReviewPR[]) => {
      const by = new Map<string, ReviewPR[]>();
      for (const p of list) by.set(p.repo, [...(by.get(p.repo) ?? []), p]);
      return [...by.entries()].map(([repo, prs]) => ({ repo, prs: prs.sort(cmp) })).sort((a, b) => b.prs.length - a.prs.length || a.repo.localeCompare(b.repo));
    };
    return { groups: group((queue?.items ?? []).filter(keep)), watchedGroups: group((queue?.watched ?? []).filter(keep)),
      botGroups: showBotCc ? group((queue?.bot_cc ?? []).filter(keep)) : [] };
  }, [queue, filter, source, showDrafts, sort, agentOnly, maxRank, showBotCc]);
  const botCc = queue?.bot_cc?.length ?? 0;
  const drafts = queue?.items.filter((p) => p.draft).length ?? 0;
  const deferred = (queue?.deferred ?? []).filter((p) => matchesSource(p, source));
  // per category: the PRs in it (drafts only when shown), for the category choice
  const owned = queue?.items.filter((p) => p.requested === "codeowner").length ?? 0;
  const shown = groups.reduce((n, g) => n + g.prs.length, 0);
  const change = async (what: string, c: Partial<HubPrefs> & Record<string, unknown>) => {
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
  const prRow = (p: ReviewPR, showRepo = false) => {
    const key = prKey(p);
    order.current.push({ key, repo: p.repo, number: p.number });
    const on = selected.has(key);
    const qpos = batch ? batch.queued.indexOf(key) : -1;
    const brun = batch?.running[key];
    const bfail = batch?.failed[key];
    return (
    <button key={`${p.repo}#${p.number}`} className={`review-pr ${p.draft ? "draft" : ""} ${p.priority ? "priority" : ""} ${on ? "selected" : ""}`} onClick={(e) => { if (selected.size && (e.ctrlKey || e.metaKey)) { pick(p, false); return; } onOpen(p); }} role="button"
      title={`${p.repo}#${p.number}\n${p.title}\n${CI_LABEL[p.ci ?? ""] ?? "no checks"}${p.requested_by ? `\nreview requested by ${p.requested_by}${p.requested_by_company ? ` (${p.requested_by_company})` : ""}${p.requested_by_roles ? ` — ${p.requested_by_roles}` : ""}` : ""}${p.author_roles ? `\nauthor ${p.author}: ${p.author_roles}` : ""}${p.teams.length ? `\nalso requested from: ${p.teams.join(", ")}` : ""}${ownedTitle(p)}`}
      data-testid="review-pr" data-number={p.number} data-repo={p.repo} data-priority={p.priority ? "1" : "0"} data-selected={on ? "1" : "0"}>
      <span role="checkbox" aria-checked={on} tabIndex={0} className={`review-check ${on ? "on" : ""}`} aria-label={`Select ${p.repo}#${p.number}`}
        title="Select for Agent review (Shift-click: a range)" data-testid="review-select"
        onClick={(e) => { e.stopPropagation(); pick(p, e.shiftKey); }}
        onKeyDown={(e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); e.stopPropagation(); pick(p, e.shiftKey); } }}>{on ? "✓" : ""}</span>
      <span className={`ci-dot ${(p.ci ?? "none").toLowerCase()}`} aria-label={CI_LABEL[p.ci ?? ""] ?? "no checks"} />
      <span className="review-pr-main">
        <span className="review-pr-title">
          <PrStatus state={p.state} draft={p.draft} />{" "}{p.title}
        </span>
        <span className="review-pr-meta muted small">
          {showRepo ? `${p.repo}` : ""}#{p.number} · <GhUser login={p.author} bold={false} inButton /> · {ago(p.updated_at)} · <span className="add">+{p.additions}</span> <span className="del">−{p.deletions}</span>
          {reviewRequested(p) && <span className="git-badge" title="GitHub formally requests your review" data-testid="review-requested"> review requested</span>}
          {p.mentioned && p.mention_by !== "bot" && <span className="git-badge" title={p.mention_who ? `@${p.mention_who} mentioned you in the description or discussion` : "GitHub's mention search includes you in this PR's description or discussion"} data-testid="review-mentioned"> @mentioned{p.mention_who ? ` by ${p.mention_who}` : ""}</span>}
          {p.mention_by === "bot" && <span className="git-badge clean" title="Only a bot's cc line mentions you (e.g. pytorch-bot adding the subscribers of the PR's labels); no person asked for you" data-testid="review-bot-cc"> cc'd by bot</span>}
          {p.requested === "codeowner" && (
            <span className="git-badge" title={`Not requested from you: it touches ${p.owned_count ?? 0} file(s) you own in CODEOWNERS${p.draft ? " (a draft: GitHub requests code owners only once it is ready for review)" : ""}`} data-testid="review-codeowner"> code owner</span>
          )}
          {p.requested_by && <span className="review-by" data-testid="review-by"> · asked by <GhUser login={p.requested_by} bold={false} inButton />{p.requested_by_roles ? <span className={`review-role ${p.requested_by_core ? "core" : ""}`} data-testid="review-role">{p.requested_by_core ? " · core maintainer" : " · maintainer"}</span> : null}</span>}
          {p.my_review === "APPROVED" && (
            <span className={`git-badge ${p.approved_no_news ? "clean" : "approved-news"}`} title={p.approved_no_news ? "You approved; nothing new since" : `You approved; since then: ${(p.since_approval ?? []).join(", ")}`} data-testid="review-approved">
              {p.approved_no_news ? " ✓ approved" : ` ✓ approved · ${(p.since_approval ?? []).join(", ")}`}
            </span>
          )}
          {p.my_review === "CHANGES_REQUESTED" && <span className="git-badge" title="You requested changes"> changes requested</span>}
          {showRepo && categoryOf(p) === "todo" && <span className="git-badge review-todo" title="You have not reviewed or commented yet" data-testid="review-todo"> not reviewed yet</span>}
          {p.agent_review?.state === "waiting" && (
            <span className="git-badge agent-waiting" title="The agent reviewed this PR: apply or discard its suggested comments (Files tab)" data-testid="review-agent-waiting">
              {" "}🤖 {p.agent_review.proposed ? `${p.agent_review.proposed} suggestion${p.agent_review.proposed > 1 ? "s" : ""} to confirm` : "review to confirm"}
            </span>
          )}
          {(p.agent_review?.state === "running" || brun) && <span className="git-badge" title={brun === "preparing" ? "Loading the PR for the agent's review" : "The agent is reviewing this PR"} data-testid="review-agent-running"> 🤖 {brun === "preparing" ? "starting review…" : "reviewing…"}</span>}
          {qpos >= 0 && !brun && <span className="git-badge" title="Waiting for its turn: the agent reviews a few PRs at a time" data-testid="review-agent-queued"> 🤖 queued ({qpos + 1})</span>}
          {bfail && !brun && qpos < 0 && <span className="git-badge review-agent-failed" title={`The agent review could not start or failed: ${bfail.error}`} data-testid="review-agent-failed"> 🤖 review failed</span>}
          {p.my_activity === "news" && p.my_review !== "APPROVED" && (
            <span className="git-badge approved-news" title={`Since your latest review / comment (${p.my_last_at ? new Date(p.my_last_at).toLocaleString() : ""})`} data-testid="review-since-mine">
              {" "}↻ {(p.since_mine ?? []).join(", ")}
            </span>
          )}
          {p.my_activity === "quiet" && p.my_review !== "APPROVED" && (
            <span className="git-badge clean" title={`Your latest review / comment: ${p.my_last_at ? new Date(p.my_last_at).toLocaleString() : ""}; nothing since`} data-testid="review-quiet"> ✓ commented</span>
          )}
          {p.deferred_ended && <span className="git-badge approved-news" title={`You deferred it (${deferLabel(p.deferred_ended)}); it is back${p.deferred_ended.note ? `\nnote: ${p.deferred_ended.note}` : ""}`} data-testid="review-defer-ended"> ⏰ back</span>}
          {p.deferred && <span className="git-badge" title={p.deferred.note || undefined} data-testid="review-deferred-until"> ⏰ {deferLabel(p.deferred)}{p.deferred.note ? ` · ${p.deferred.note}` : ""}</span>}
          {p.hidden_ended && <span className="git-badge approved-news" title={`You hid it (${new Date(p.hidden_ended.at).toLocaleString()}); it is back: ${p.hidden_ended.why.join(", ")}. Hide it again to start over.`} data-testid="review-hide-ended"> back: {p.hidden_ended.why.join(", ")}</span>}
          {p.labels.slice(0, 2).map((l) => (
            <span key={l.name} className="review-label" style={l.color ? { borderColor: `#${l.color}` } : undefined}>{l.name}</span>
          ))}
        </span>
      </span>
      <span className="review-pr-side" data-testid="review-pr-side">
        {prefs && !p.deferred && (
          <DeferMenu pr={`${p.repo}#${p.number}`} onDefer={(d) => change(`defer:${p.repo}#${p.number}`, { defer: d })} />
        )}
        {prefs && p.deferred && (
          <span role="button" tabIndex={0} className="btn small" onClick={(e) => { e.stopPropagation(); void change(`undefer:${p.repo}#${p.number}`, { undefer: `${p.repo}#${p.number}` }); }}
            title="Back in the list now" data-testid="review-undefer">Back now</span>
        )}
        {prefs && !p.deferred && (
          <span role="button" tabIndex={0} className="icon-btn small review-hide-pr" aria-label={`Hide ${p.repo}#${p.number}`} data-testid="review-hide-pr"
            title="Hide this PR: it leaves Reviews (and the badge) until your review is requested or someone @-mentions you directly (bot cc lines do not count). Show it again under ⚙ → Repositories → Hidden PRs."
            onClick={(e) => { e.stopPropagation(); if (busy === null) void change(`hidepr:${p.repo}#${p.number}`, { hide_pr: { pr: `${p.repo}#${p.number}`, title: p.title } }); }}
            onKeyDown={(e) => { if (e.key === "Enter") { e.stopPropagation(); void change(`hidepr:${p.repo}#${p.number}`, { hide_pr: { pr: `${p.repo}#${p.number}`, title: p.title } }); } }}>⊘</span>
        )}
        {prefs && (
          <LevelMenu pr={`${p.repo}#${p.number}`} level={p.level} source={p.level_source} reasons={p.level_reasons} levels={levels} compact
            agentOn={!!prefs.priority_agent?.enabled}
            onSet={(lv) => change(`level:${p.repo}#${p.number}`, { set_level: { pr: `${p.repo}#${p.number}`, level: lv } }).then(() => { if (search) void runSearch(); })} />
        )}
      </span>
    </button>
    );
  };
  /** a box that selects (or unselects) all of `prs`: a repository's, or one category of it */
  const selectBox = (prs: ReviewPR[], what: string, testId: string, cls: string) => {
    const n = prs.filter((p) => selected.has(prKey(p))).length;
    const all = n === prs.length && n > 0;
    return (
      <span role="checkbox" aria-checked={all ? true : n ? "mixed" : false} tabIndex={0} className={`review-check ${cls} ${all ? "on" : n ? "some" : ""}`}
        aria-label={`Select all PRs of ${what}`} title={all ? `Unselect the ${prs.length} PR(s) of ${what}` : `Select the ${prs.length} PR(s) of ${what} shown, for Agent review`} data-testid={testId}
        onClick={(e) => { e.stopPropagation(); pickAll(prs, !all); }} onKeyDown={(e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); e.stopPropagation(); pickAll(prs, !all); } }}>{all ? "✓" : n ? "–" : ""}</span>
    );
  };
  const groupBox = (repo: string, prs: ReviewPR[], watched: boolean, bot = false) => {
    const id = (bot ? "b:" : watched ? "w:" : "") + repo;
    const open = !collapsed.has(id);
    return (
      <div key={id} className="review-group" data-testid={bot ? "review-group-botcc" : watched ? "review-group-watched" : "review-group"} data-repo={repo}>
        <div className="review-group-head-row">
          {selectBox(prs, repo, "review-select-group", "group")}
          <button className="review-group-head" onClick={() => toggle(id)} aria-expanded={open}>
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
        {open && (watched ? prs.map((p) => prRow(p)) : categories.map((c) => {
          const list = prs.filter((p) => categoryOf(p) === c.key);
          if (!list.length) return null;
          const k = `${repo}|${c.key}`;
          const subOpen = !collapsed.has(k);
          return (
            <div key={c.key} className={`review-section ${["todo", "news", "quiet"].indexOf(c.key) === 0 ? "top" : c.key === "news" ? "high" : "medium"}`} data-testid="review-section" data-category={c.key}>
              <div className="review-section-head-row">
                {selectBox(list, `“${c.title}” in ${repo}`, "review-select-category", "category")}
                <button className="review-section-head" onClick={() => toggle(k)} aria-expanded={subOpen} title={CATEGORY_HINT[c.key]}>
                  <span className="chev">{subOpen ? "⌄" : "›"}</span>
                  <span className="review-section-title">{c.title}</span>
                  {c.level && <span className={`review-level ${["top", "high", "medium"].includes(c.level) ? c.level : "medium"}`}>{c.level}</span>}
                  <span className="spacer" />
                  <span className="git-badge clean">{list.length}</span>
                </button>
              </div>
              {subOpen && list.map((p) => prRow(p))}
            </div>
          );
        }))}
      </div>
    );
  };

  return (
    <div className={`reviews-panel ${selected.size ? "selecting" : ""}`} data-testid="reviews-panel">
      <div className="tree-header">
        <span className="ws-name">Reviews{queue ? ` · ${reviewBadge(queue)}` : ""}</span>
        <span className="spacer" />
        {prefs && tab === "queue" && (
          <button className={`icon-btn settings-badged ${settings ? "active" : ""}`} onClick={() => setSettings((v) => !v)} title={learnWaiting ? `Repositories and priority · ${learnWaiting} lesson(s) the agent learned wait for you (Agent learning)` : "Repositories, priority and agent learning"} aria-label="Reviews settings" aria-pressed={settings} data-testid="reviews-settings">
            ⚙{learnWaiting > 0 && <span className="activity-badge" data-testid="reviews-learning-badge">{learnWaiting}</span>}
          </button>
        )}
        {tab === "queue" && <RefreshButton onRefresh={() => loadReviews(true)} title="Refresh from GitHub" testId="reviews-refresh" />}
      </div>
      <div className="pad-x reviews-tabs">
        <span className="mode-switch" role="tablist" aria-label="Reviews view">
          <button role="tab" aria-selected={tab === "queue"} className={tab === "queue" ? "active" : ""} onClick={() => setTab("queue")} title="Pull requests waiting for your review" data-testid="reviews-tab-queue">Review queue</button>
          <button role="tab" aria-selected={tab === "monitors"} className={tab === "monitors" ? "active" : ""} onClick={() => setTab("monitors")} title="Pull requests and issues picked by your rules (GitHub searches), per repository" data-testid="reviews-tab-monitors">
            Monitors{monUnseen > 0 && tab !== "monitors" && <span className="activity-badge" data-testid="reviews-monitors-badge">{monUnseen}</span>}
          </button>
        </span>
      </div>
      {/* kept mounted while hidden: its count of new / updated items shows on the tab */}
      <div className="monitors-wrap" hidden={tab !== "monitors"}>
        <MonitorsPanel repos={[...new Set([...(queue?.items ?? []).map((p) => p.repo), ...(prefs?.watched_repos ?? [])])]} onOpenPr={onOpenPr} onUnseen={setMonUnseen} />
      </div>
      {tab === "queue" && (<>
      <div className="pad-x"><GithubRate /></div>
      {settings && prefs && <ReviewsSettings prefs={prefs} busy={busy} change={change} problems={[...(queue?.watch_problems ?? []), ...(queue?.codeowned_problems ?? [])]} status={queue?.priority_agent ?? null} maintainers={queue?.maintainers ?? []} codeowned={queue?.codeowned ?? []}
        back={Object.fromEntries([...(queue?.items ?? []), ...(queue?.watched ?? []), ...(queue?.bot_cc ?? [])].filter((p) => p.hidden_ended).map((p) => [prKey(p), p.hidden_ended!.why]))} />}
      <div className="reviews-search">
        {search === null ? (
          <button className="link-btn small" onClick={() => setSearch("")} data-testid="reviews-search-open">🔍 Search pull requests on GitHub…</button>
        ) : (
          <>
            <div className="reviews-row">
              <input autoFocus className="reviews-filter" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="xpu repo:pytorch/pytorch · author:me · owner/repo#123 · a PR link"
                onKeyDown={(e) => { if (e.key === "Enter") void runSearch(); if (e.key === "Escape") { setSearch(null); setResults(null); } }} data-testid="reviews-search" />
              <select value={searchState} onChange={(e) => { const st = e.target.value as typeof searchState; setSearchState(st); void runSearch(st); }} aria-label="State" data-testid="reviews-search-state">
                <option value="open">Open</option>
                <option value="closed">Closed / merged</option>
                <option value="all">All</option>
              </select>
              <button className="icon-btn small" onClick={() => { setSearch(null); setResults(null); }} aria-label="Close search">×</button>
            </div>
            {searching && <div className="muted small">Searching…</div>}
            {searchErr && <div className="tree-error small">{searchErr}</div>}
            {results && (
              <div className="reviews-results" data-testid="reviews-results">
                <div className="muted small">{results.items.length} of {results.total} · <code>{results.query}</code></div>
                {results.items.map((p) => prRow(p, true))}
                {results.items.length === 0 && <div className="muted small">Nothing found{searchState === "open" ? " among open PRs (choose Closed / merged to include those)" : ""}.</div>}
              </div>
            )}
          </>
        )}
      </div>
      <div className="reviews-controls">
        <input className="reviews-filter" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter: repo, title, author, label…" data-testid="reviews-filter" />
        <div className="reviews-row">
          <label className="small" htmlFor="reviews-source">Review source</label>
          <select id="reviews-source" className="reviews-sort" value={source} onChange={(e) => setSource(e.target.value as ReviewSource)} data-testid="reviews-source">
            <option value="all">All sources</option>
            <option value="requested">Review requested</option>
            <option value="mentioned">Mentioned</option>
            <option value="both">Requested and mentioned</option>
          </select>
        </div>
        <div className="reviews-row">
          <label className="small" title={`${drafts} draft PR(s)`}>
            <input type="checkbox" checked={showDrafts} onChange={(e) => setShowDrafts(e.target.checked)} data-testid="reviews-drafts" /> drafts ({drafts})
          </label>
          {queue?.prefs && (
            <label className="small" title="PRs you approved where nothing happened since (no new commits, no comments or reviews from people — bots and GitHub Actions do not count)">
              <input type="checkbox" checked={queue.prefs.show_approved} disabled={busy !== null}
                onChange={(e) => void change("approved", { show_approved: e.target.checked })} data-testid="reviews-show-approved" /> approved ({queue.prefs.show_approved ? queue.items.filter((p) => p.approved_no_news).length : queue.approved_hidden ?? 0})
            </label>
          )}
          <label className="small" title="PRs that mention you only in a bot-written cc line (e.g. pytorch-bot cc-ing the subscribers of a label), without a review request or a person @-mentioning you. Hidden by default.">
            <input type="checkbox" checked={showBotCc} onChange={(e) => setShowBotCc(e.target.checked)} data-testid="reviews-bot-cc" /> cc'd by bot ({botCc})
          </label>
          <label className="small" title="Only the PRs the agent reviewed whose suggested comments wait for you to apply or discard (drafts included)">
            <input type="checkbox" checked={agentOnly} onChange={(e) => setAgentOnly(e.target.checked)} data-testid="reviews-agent-waiting" /> 🤖 agent reviewed ({agentWaiting})
          </label>
          {levels.length > 0 && (
            <select className="reviews-sort" value={maxRank} onChange={(e) => setMaxRank(Number(e.target.value))} title="Only PRs at least this urgent" aria-label="Levels" data-testid="reviews-level-filter">
              <option value={99}>all levels</option>
              {levels.slice(0, -1).map((l, i) => <option key={l.id} value={i}>{i === 0 ? `only ${l.id}` : `${levels[0].id}–${l.id}`}</option>)}
            </select>
          )}
          <select className="reviews-sort" value={sort} onChange={(e) => setSort(e.target.value as Sort)} data-testid="reviews-sort" aria-label="Sort">
            <option value="updated">Recently updated</option>
            <option value="waiting">Waiting longest</option>
            <option value="size">Smallest first</option>
          </select>
        </div>
      </div>
      {(selected.size > 0 || batchBusy > 0 || batchNote) && (
        <div className="reviews-batch-bar" data-testid="reviews-batch-bar">
          {selected.size > 0 && (
            <>
              <span className="small strong" data-testid="reviews-selected-count">{selected.size} selected</span>
              <button className="btn small primary" disabled={starting} onClick={() => void startBatch()} title={`The agent reviews each selected PR like its Agent review button (${batch?.concurrency ?? 2} at a time); its comments wait in the PR's Files tab for you to apply or discard. A repository without an agent wiki uses this wiki.`} data-testid="reviews-batch-review">
                {starting ? "Starting…" : `🤖 Agent review ${selected.size}`}
              </button>
              <button className="link-btn small" onClick={() => { setSelected(new Map()); lastPick.current = null; }} data-testid="reviews-select-clear">Clear</button>
            </>
          )}
          {batchBusy > 0 && (
            <span className="muted small" data-testid="reviews-batch-progress">
              {selected.size > 0 ? " · " : ""}🤖 reviewing {Object.keys(batch!.running).length}, waiting {batch!.queued.length}
              {batch!.queued.length > 0 && <> · <button className="link-btn small" onClick={() => void cancelWaiting()} title="Take the PRs that have not started off the queue (running reviews go on)" data-testid="reviews-batch-cancel">cancel waiting</button></>}
            </span>
          )}
          {batchNote && (
            <div className={`small ${batchNote.error ? "tree-error" : "muted"}`} role={batchNote.error ? "alert" : "status"} data-testid="reviews-batch-note">
              {batchNote.text} <button className="icon-btn small" onClick={() => setBatchNote(null)} aria-label="Dismiss">×</button>
            </div>
          )}
        </div>
      )}
      <div className="tree-body" data-testid="reviews-list" ref={listScroll}>
        {!queue && loading && <div className="tree-loading">Loading review requests and mentions from GitHub… (the first load can take ~30 s)</div>}
        {error && <div className="tree-error" role="alert" data-testid="reviews-error">{error}</div>}
        {queue?.refresh_error && <div className="tree-error stale">Could not refresh ({queue.refresh_error.message}); showing the earlier list.</div>}
        {queue?.note && <div className="reviews-note small" data-testid="reviews-note">{queue.note}</div>}
        {queue && (
          <div className="muted small reviews-meta">
            Requests and mentions: {queue.items.length - owned}{queue.truncated ? `+ (of ${queue.total})` : ""}
            {owned > 0 && <span title="Not requested from you, but they touch files you own in CODEOWNERS" data-testid="reviews-codeowned-count"> · touching your files: {owned}</span>} · showing {shown}
            {deferred.length > 0 && <span data-testid="reviews-deferred-count"> · {deferred.length} deferred</span>}
            {(queue.hidden_prs?.length ?? 0) > 0 && <span title="PRs you hid: back when your review is requested or someone @-mentions you (⚙ → Repositories → Hidden PRs to show them again)" data-testid="reviews-hidden-prs-count"> · {queue.hidden_prs!.length} hidden</span>}
            {!showBotCc && botCc > 0 && <span title="Tick “cc'd by bot” to show them" data-testid="reviews-bot-cc-hidden"> · {botCc} cc'd by bot hidden</span>}
            {!showDrafts && !agentOnly && drafts > 0 && <span title="Tick “drafts” to show them"> · {drafts} draft{drafts > 1 ? "s" : ""} hidden</span>}
            {filter.trim() && <span> · filtered</span>}
            {agentOnly && <span> · only those the agent reviewed, waiting for you</span>}
            {maxRank < 99 && <span> · only {levels[0]?.id}–{levels[maxRank]?.id}</span>}
            {queue.priority_agent && queue.priority_agent.running.length + queue.priority_agent.queued > 0 && (
              <span data-testid="reviews-classifying"> · 🤖 deciding levels of {queue.priority_agent.running.length + queue.priority_agent.queued} PR(s){queue.priority_agent.files_summarizing ? ` (summarizing ${queue.priority_agent.files_summarizing} files)` : ""}…</span>
            )}
            {queue.refreshing ? " · refreshing…" : ` · updated ${ago(queue.fetched_at)} ago`}
            {" · "}<GhRefreshSelect kind="reviews" testId="reviews-auto-refresh" title="How often the Reviews list (and its badge) refreshes from GitHub on its own; ↻ refreshes now" />
          </div>
        )}
        {queue && shown === 0 && <div className="tree-empty">{queue.items.length ? "Nothing matches the filter." : "No pull requests are waiting for your review. 🎉"}</div>}
        {groups.map(({ repo, prs }) => groupBox(repo, prs, false))}
        {watchedGroups.length > 0 && <div className="muted small reviews-meta" data-testid="reviews-watched-head">Watched repositories (all open PRs)</div>}
        {watchedGroups.map(({ repo, prs }) => groupBox(repo, prs, true))}
        {botGroups.length > 0 && <div className="muted small reviews-meta" data-testid="reviews-bot-cc-head" title="No review request, and no person @-mentioned you: only a bot's cc line">cc'd by bot (only an automatic cc line mentions you)</div>}
        {botGroups.map(({ repo, prs }) => groupBox(repo, prs, true, true))}
        {deferred.length > 0 && (
          <div className="review-group" data-testid="reviews-deferred">
            <div className="review-group-head-row">
              <button className="review-group-head" onClick={() => toggle("deferred:open")} aria-expanded={collapsed.has("deferred:open")} title="Reviews you deferred: back by themselves at their time, or when someone does something on the PR (if you chose that)">
                <span className="chev">{collapsed.has("deferred:open") ? "⌄" : "›"}</span>
                <span className="review-repo">⏰ Deferred</span>
                <span className="spacer" />
                <span className="git-badge clean">{deferred.length}</span>
              </button>
            </div>
            {collapsed.has("deferred:open") && [...deferred].sort((a, b) => (a.deferred?.until ?? "9").localeCompare(b.deferred?.until ?? "9")).map((p) => prRow(p, true))}
          </div>
        )}
        {(queue?.approved_hidden ?? 0) > 0 && <div className="muted small reviews-meta" data-testid="reviews-approved-hidden">{queue!.approved_hidden} PR(s) you approved with nothing new since are not listed (tick “approved” to show them).</div>}
        {(queue?.hidden_count ?? 0) > 0 && <div className="muted small reviews-meta">{queue!.hidden_count} request(s) in hidden repositories (⚙ to show them)</div>}
      </div>
      </>)}
    </div>
  );
}

function ReviewsSettings({ prefs, busy, change, problems, status, maintainers, codeowned, back }: {
  prefs: HubPrefs; busy: string | null; change: (what: string, c: Partial<HubPrefs> & Record<string, unknown>) => Promise<void>; problems: string[]; status: AgentStatus | null;
  maintainers: MaintainerSource[]; codeowned: CodeownedSource[];
  /** hidden PRs that are back in the list, and why */
  back: Record<string, string[]>;
}) {
  const [repo, setRepo] = useState("");
  const [cm, setCm] = useState((prefs.core_maintainers ?? []).join(", "));
  const [tab, setTab] = useState<"priority" | "repos" | "cache" | "learning" | "skills">(() => { const t = localStorage.getItem("waw.reviews.settingsTab"); return t === "repos" || t === "cache" || t === "learning" || t === "skills" ? t : "priority"; });
  const learning = useLearningSummary(true);
  useEffect(() => localStorage.setItem("waw.reviews.settingsTab", tab), [tab]);
  const valid = /^[\w.-]+\/[\w.-]+$/.test(repo.trim());
  const watch = () => {
    const r = repo.trim();
    if (!valid) return;
    void change("watch", { watched_repos: [...prefs.watched_repos.filter((x) => x.toLowerCase() !== r.toLowerCase()), r], hidden_repos: prefs.hidden_repos.filter((x) => x.toLowerCase() !== r.toLowerCase()) }).then(() => setRepo(""));
  };
  return (
    <div className="reviews-settings" data-testid="reviews-settings-box">
      <span className="mode-switch" role="tablist">
        <button role="tab" aria-selected={tab === "priority"} className={tab === "priority" ? "active" : ""} onClick={() => setTab("priority")} data-testid="reviews-settings-priority">Priority</button>
        <button role="tab" aria-selected={tab === "repos"} className={tab === "repos" ? "active" : ""} onClick={() => setTab("repos")} data-testid="reviews-settings-repos">Repositories</button>
        <button role="tab" aria-selected={tab === "cache"} className={tab === "cache" ? "active" : ""} onClick={() => setTab("cache")} data-testid="reviews-settings-cache">Cache</button>
        <button role="tab" aria-selected={tab === "learning"} className={tab === "learning" ? "active" : ""} onClick={() => setTab("learning")} title="What the agent learned from your reviews"
          data-testid="reviews-settings-learning">Agent learning{learning.proposed + learning.flagged ? ` (${learning.proposed + learning.flagged})` : ""}</button>
        <button role="tab" aria-selected={tab === "skills"} className={tab === "skills" ? "active" : ""} onClick={() => setTab("skills")} title="The skills agent reviews get; edit them here"
          data-testid="reviews-settings-skills">Skills</button>
      </span>
      {tab === "learning" && <LearningSettings />}
      {tab === "skills" && <ReviewSkills />}
      {tab === "priority" && <PrioritySettings key="priority" prefs={prefs} status={status} save={async (c) => {
        await savePrefs(c as Partial<HubPrefs>);
      }} />}
      {tab === "cache" && <CacheSettings />}
      {tab === "repos" && <>
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
      <div className="small strong">Your files <span className="muted">(CODEOWNERS)</span></div>
      <label className="small" title="GitHub requests code owners only once a PR is ready for review, and not the person who marks it ready or already reviewed it">
        <input type="checkbox" checked={prefs.codeowned ?? true} disabled={busy !== null} onChange={(e) => void change("codeowned", { codeowned: e.target.checked })} data-testid="reviews-codeowned" />
        {" "}Also list open PRs that touch files you own, even when GitHub did not request you (drafts included)
      </label>
      {(prefs.codeowned ?? true) && <div className="muted small">Checked in the repositories of your list whose CODEOWNERS names you (teams are not expanded). The first check walks all open PRs (a few minutes for a big repository); later ones only what changed.</div>}
      {(prefs.codeowned ?? true) && codeowned.map((c) => (
        <div key={c.repo} className="reviews-settings-item small" data-testid="reviews-codeowned-repo">
          <span className="mono">{c.repo}</span>
          <span className="muted">{c.error ? `not checked: ${c.error}` : c.in_progress ? `first check in progress… (${c.prs} found so far)` : `${c.prs} PR(s) · ${c.rules ?? 0} rule(s) name you${c.scanned_at ? ` · checked ${ago(new Date(c.scanned_at * 1000).toISOString())} ago` : ""}`}</span>
        </div>
      ))}
      <div className="small strong">Hidden repositories</div>
      {prefs.hidden_repos.length === 0 && <div className="muted small">None — use × on a repository to hide it.</div>}
      {prefs.hidden_repos.map((r) => (
        <div key={r} className="reviews-settings-item" data-testid="reviews-hidden-item">
          <span className="mono small">{r}</span>
          <button className="btn small" disabled={busy !== null} onClick={() => void change(`unhide:${r}`, { hidden_repos: prefs.hidden_repos.filter((x) => x !== r) })} data-testid="reviews-unhide">Show again</button>
        </div>
      ))}
      <div className="small strong">Hidden PRs <span className="muted">(back by themselves when your review is requested or someone @-mentions you directly)</span></div>
      {Object.keys(prefs.hidden_prs ?? {}).length === 0 && <div className="muted small">None — use ⊘ on a PR to hide it.</div>}
      {Object.entries(prefs.hidden_prs ?? {}).reverse().map(([k, h]) => (
        <div key={k} className="reviews-settings-item" data-testid="reviews-hidden-pr-item">
          <span className="small" title={`hidden ${new Date(h.at).toLocaleString()}`}><span className="mono">{k}</span>{h.title ? ` ${h.title}` : ""}{back[k] ? <span className="muted"> · back: {back[k].join(", ")}</span> : null}</span>
          <button className="btn small" disabled={busy !== null} onClick={() => void change(`unhidepr:${k}`, { unhide_pr: k })} data-testid="reviews-unhide-pr">Show again</button>
        </div>
      ))}
      <div className="small strong">Maintainers <span className="muted">(who asked for your review: for priority rules and the agent)</span></div>
      <div className="muted small">Read from each repository once a day: its governance page (docs/source/community/persons_of_interest.md) and CODEOWNERS.</div>
      {maintainers.map((m) => (
        <div key={m.repo} className="reviews-settings-item small" data-testid="reviews-maintainers-repo">
          <span className="mono">{m.repo}</span>
          <span className="muted">{m.error ? `not read: ${m.error}` : m.sources.length ? `${m.people} people · ${m.sources.map((x) => x.split("/").pop()).join(", ")}` : "publishes no maintainer list or CODEOWNERS"}</span>
        </div>
      ))}
      <div className="small">Also core maintainers, in every repository (GitHub logins):</div>
      <div className="reviews-settings-row">
        <input value={cm} onChange={(e) => setCm(e.target.value)} placeholder="login, login, …" data-testid="reviews-core-maintainers" />
        <button className="btn small" disabled={busy !== null} onClick={() => void change("maintainers", { core_maintainers: cm.split(/[\s,]+/).map((x) => x.replace(/^@/, "")).filter(Boolean) })} data-testid="reviews-core-maintainers-save">Save</button>
      </div>
      </>}
    </div>
  );
}

/** Where a PR stands, next to its CI signal: Draft / Ready for review (open), Merged, Closed. */
export function PrStatus({ state, draft }: { state?: string | null; draft?: boolean }) {
  const st = (state ?? "OPEN").toUpperCase();
  const [cls, label] = st === "MERGED" ? ["merged", "Merged"] : st === "CLOSED" ? ["closed", "Closed"] : draft ? ["draft", "Draft"] : ["ready", "Ready"];
  return <span className={`pr-status ${cls}`} title={st === "OPEN" ? (draft ? "Draft: not ready for review yet" : "Open and ready for review") : label} data-testid="pr-status" data-status={cls}>{label}</span>;
}
