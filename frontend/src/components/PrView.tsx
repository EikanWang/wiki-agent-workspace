import { useEffect, useMemo, useRef, useState } from "react";
import { AgentSession, ApiError, WorkspaceInfo, get, post, put } from "../api";
import { renderGithubMarkdown } from "../markdown";
import { SelectionAnchor, wholeDocument } from "../textmodel";
import { GhDraft, GhThread, PrFile, PrFileDiff, SubmitReview } from "./PrDiff";
import { COMPOSER_HINT, composerKeyDown } from "./keys";
import { SelectionPopover } from "./Reader";
import ContextFolders from "./ContextFolders";
import RefreshButton from "./RefreshButton";

interface Check { name: string | null; state: string; url: string | null }
export interface PrData {
  pr: {
    repo: string; private: boolean; number: number; title: string; url: string; body: string; state: string; draft: boolean;
    author: string | null; created_at: string; updated_at: string; mergeable: string | null; additions: number; deletions: number;
    changed_files: number; base: string; head: string; review_decision: string | null; labels: { name: string; color: string | null }[];
    reviews: { author: string | null; state: string; submitted_at: string | null; url: string | null }[]; requested: (string | null)[];
    ci: string | null; checks: Check[];
  };
  files: PrFile[];
  wiki_workspace_id: string | null;
  doc_path: string;
}

const FOLD_LINES = 400; // longer diffs start folded

interface ConvLine { id: number; path: string; line: number | null; outdated: boolean; body: string; url: string | null; reply: boolean; reply_to?: number | null; author?: string | null; created_at?: string | null; hunk: string }

/** GitHub's Conversation tab: a reply to a line comment is shown inside the original comment's thread (not as its own
 * review), so a reply-only review without text disappears. Replies whose original comment is not loaded stay put. */
export function groupConversation(items: ConvItem[]): { items: ConvItem[]; threads: Map<number, ConvLine[]> } {
  const byId = new Map<number, ConvLine>();
  for (const c of items) for (const lc of c.comments ?? []) byId.set(lc.id, lc);
  const root = (lc: ConvLine): ConvLine => {
    let r = lc;
    const seen = new Set<number>();
    while (r.reply_to && byId.has(r.reply_to) && !seen.has(r.id)) {
      seen.add(r.id);
      r = byId.get(r.reply_to)!;
    }
    return r;
  };
  const threads = new Map<number, ConvLine[]>();
  const moved = new Set<number>();
  for (const lc of byId.values()) {
    const r = root(lc);
    if (r.id !== lc.id) {
      moved.add(lc.id);
      threads.set(r.id, [...(threads.get(r.id) ?? []), lc]);
    }
  }
  for (const list of threads.values()) list.sort((a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? ""));
  const out = items
    .filter((c) => !(c.kind === "review" && !c.body.trim() && (c.comments ?? []).length > 0 && (c.line_comments ?? 0) <= (c.comments ?? []).length
      && (c.comments ?? []).every((lc) => moved.has(lc.id))))
    .map((c) => (c.comments ? { ...c, loaded: c.comments.length, comments: c.comments.filter((lc) => !moved.has(lc.id)) } : c));
  return { items: out, threads };
}
interface ConvItem { kind: "review" | "comment"; id: number; author: string | null; state?: string; body: string; created_at: string | null; url: string | null; line_comments?: number; comments?: ConvLine[]; loaded?: number }
interface AgentReviewRun { job_id: string; thread_id: string; state: "running" | "done" | "failed"; added: number; problem: string | null; started_at: string }
interface Review { threads: GhThread[]; conversation: ConvItem[]; drafts: GhDraft[]; pending_on_github: number | null; older_hidden: number; agent_reviews?: AgentReviewRun[] }

const AGENT_REVIEW_QUESTION = "Review this pull request: correctness, performance, tests, API and design. Comment on the specific lines that need attention.";

const STATE_CLASS: Record<string, string> = { SUCCESS: "success", NEUTRAL: "success", SKIPPED: "none", FAILURE: "failure", ERROR: "failure", TIMED_OUT: "failure", CANCELLED: "failure", ACTION_REQUIRED: "pending", PENDING: "pending", IN_PROGRESS: "pending", QUEUED: "pending", EXPECTED: "pending" };
const REVIEW_LABEL: Record<string, string> = { APPROVED: "approved", CHANGES_REQUESTED: "requested changes", COMMENTED: "commented", DISMISSED: "dismissed", PENDING: "pending" };

/**
 * A pull request as a tab: Overview (description, checks, reviews) and Files (diff). Questions go to the agent in
 * the repository's wiki (chosen once per repository): about the whole PR, or about lines selected in the diff.
 */
export default function PrView({ repo, number, workspaces, docReady, onReady, onSend, sessions, defaultSessionId, defaultProvider, githubLogin = null, onOpenPr, onDiscussionsChanged }: {
  repo: string;
  /** open another pull request (a link in a description or comment) as a tab of this app */
  onOpenPr?: (repo: string, number: number) => void;
  /** the discussions changed on the server (an agent review's answer was rewritten): reload them */
  onDiscussionsChanged?: () => void;
  /** the GitHub account reviews are published as (the server's gh login) */
  githubLogin?: string | null;
  number: number;
  workspaces: WorkspaceInfo[];
  /** the PR's discussion document is open (questions can be sent) */
  docReady: boolean;
  /** data loaded and a wiki is chosen: open the PR's discussion document in that folder */
  onReady: (workspaceId: string, docPath: string, title: string) => void;
  onSend: (anchor: SelectionAnchor, question: string, sessionChoice: string | null) => Promise<void>;
  sessions: AgentSession[];
  defaultSessionId: string | null;
  defaultProvider: string | null;
}) {
  const [data, setData] = useState<PrData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"overview" | "files">("overview");
  const [sheet, setSheet] = useState<(SelectionAnchor & { pr_review?: boolean }) | null>(null);
  const [pick, setPick] = useState("");
  const [layout, setLayout] = useState<"unified" | "split">(() => (localStorage.getItem("waw.prDiffLayout") as "unified" | "split") || "unified");
  useEffect(() => localStorage.setItem("waw.prDiffLayout", layout), [layout]);
  const [review, setReview] = useState<Review>({ threads: [], conversation: [], drafts: [], pending_on_github: null, older_hidden: 0 });
  const [convText, setConvText] = useState("");
  const [convBusy, setConvBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [refs, setRefs] = useState(false);
  const [owner, name] = repo.split("/");
  const apiBase = `/github/pr/${owner}/${name}/${number}`;
  const loadComments = async () => {
    try {
      setReview(await get<Review>(`${apiBase}/comments`));
    } catch (e) {
      setError((e as ApiError).message);
    }
  };

  const load = async (refresh = false) => {
    try {
      const d = await get<PrData>(`/github/pr/${owner}/${name}/${number}${refresh ? "?refresh=true" : ""}`);
      setData(d);
      setError(null);
      void loadComments();
      if (d.wiki_workspace_id) onReady(d.wiki_workspace_id, d.doc_path, `#${d.pr.number} ${d.pr.title}`);
    } catch (e) {
      setError((e as ApiError).message);
    }
  };
  useEffect(() => {
    setData(null);
    setTab("overview");
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo, number]);

  const available = workspaces.filter((w) => w.available);
  useEffect(() => setPick((p) => p || available[0]?.id || ""), [available.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const chooseWiki = async () => {
    if (!pick || !data) return;
    try {
      await put("/github/repo-wiki", { repo: data.pr.repo, workspace_id: pick });
      setData({ ...data, wiki_workspace_id: pick });
      onReady(pick, data.doc_path, `#${data.pr.number} ${data.pr.title}`);
    } catch (e) {
      setError((e as ApiError).message);
    }
  };

  const md = (s: string) => renderGithubMarkdown(s, repo);
  const reviewing = (review.agent_reviews ?? []).some((r) => r.state === "running");
  useEffect(() => {
    if (!reviewing) return;
    const t = window.setInterval(() => void loadComments(), 4000); // its comments appear in the diff when it finishes
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reviewing, apiBase]);
  const lastRun = (review.agent_reviews ?? []).slice(-1)[0];
  const seenDone = useRef<Set<string> | null>(null);
  useEffect(() => {
    const done = (review.agent_reviews ?? []).filter((r) => r.state !== "running").map((r) => r.job_id);
    if (seenDone.current === null) {
      if (review.agent_reviews) seenDone.current = new Set(done);
      return;
    }
    if (done.some((j) => !seenDone.current!.has(j))) onDiscussionsChanged?.(); // its JSON became a note in the answer
    done.forEach((j) => seenDone.current!.add(j));
  }, [review.agent_reviews, onDiscussionsChanged]);
  const accepted = review.drafts.filter((d) => (d.decision ?? "accepted") === "accepted").length;
  const undecided = review.drafts.filter((d) => d.decision === "proposed").length;
  const body = useMemo(() => (data ? renderGithubMarkdown(data.pr.body || "_No description._", repo) : ""), [data, repo]);
  const conv = useMemo(() => groupConversation(review.conversation), [review.conversation]);

  const threadsFor = (path: string) => review.threads.filter((t) => t.path === path);
  const setViewed = async (path: string, viewed: boolean) => {
    const mark = (v: PrFile["viewed"]) => setData((d) => (d ? { ...d, files: d.files.map((f) => (f.path === path ? { ...f, viewed: v } : f)) } : d));
    const before = data?.files.find((f) => f.path === path)?.viewed ?? "UNVIEWED";
    mark(viewed ? "VIEWED" : "UNVIEWED"); // at once; undone if GitHub refuses
    try {
      await put(`${apiBase}/viewed`, { path, viewed });
    } catch (e) {
      mark(before);
      setError((e as ApiError).message);
    }
  };
  const jumpTo = (path: string) => {
    setTab("files");
    window.setTimeout(() => document.querySelector(`[data-testid="pr-file"][data-file="${CSS.escape(path)}"]`)?.scrollIntoView({ block: "start" }), 50);
  };
  const postConversation = async () => {
    if (!convText.trim()) return;
    setConvBusy(true);
    try {
      await post(`${apiBase}/conversation`, { body: convText });
      setConvText("");
      await loadComments();
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setConvBusy(false);
    }
  };
  const draftsFor = (path: string) => review.drafts.filter((d) => d.path === path);

  if (error && !data) return <div className="pr-view"><div className="banner warn" data-testid="pr-error">{error} <button className="btn small" onClick={() => void load(true)}>Retry</button> <a className="btn small" href={`https://github.com/${repo}/issues/${number}`} target="_blank" rel="noreferrer">Open on GitHub</a></div></div>;
  if (!data) return <div className="pr-view"><div className="muted pad">Loading {repo}#{number} from GitHub…</div></div>;
  const p = data.pr;
  const wikiName = workspaces.find((w) => w.id === data.wiki_workspace_id)?.name;
  const failing = p.checks.filter((c) => STATE_CLASS[c.state] === "failure").length;

  return (
    <div className="pr-view" data-testid="pr-view" onClick={(e) => {
      // links to pull requests (#123, owner/repo#123, github.com/…/pull/123) open here; Ctrl/⌘/Shift-click: on GitHub
      const a = (e.target as HTMLElement).closest?.("a[data-pr]");
      if (!a || !onOpenPr || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
      const m = (a.getAttribute("data-pr") || "").match(/^([\w.-]+\/[\w.-]+)\/(\d+)$/);
      if (!m) return;
      e.preventDefault();
      onOpenPr(m[1], Number(m[2]));
    }}>
      <div className="pr-head">
        <div className="pr-title-row">
          <span className={`pr-state ${p.draft ? "draft" : p.state.toLowerCase()}`}>{p.draft ? "Draft" : p.state === "OPEN" ? "Open" : p.state === "MERGED" ? "Merged" : "Closed"}</span>
          <h2 className="pr-title" data-testid="pr-title">{p.title} <span className="muted">#{p.number}</span></h2>
        </div>
        <div className="pr-meta muted small">
          <b>{p.repo}</b>{p.private ? " (private)" : ""} · {p.author} wants to merge <code>{p.head}</code> → <code>{p.base}</code> · {p.changed_files} files <span className="add">+{p.additions}</span> <span className="del">−{p.deletions}</span>
        </div>
        <div className="pr-actions">
          <span className="mode-switch" role="tablist" aria-label="Pull request view">
            <button role="tab" aria-selected={tab === "overview"} className={tab === "overview" ? "active" : ""} onClick={() => setTab("overview")} data-testid="pr-tab-overview">Overview</button>
            <button role="tab" aria-selected={tab === "files"} className={tab === "files" ? "active" : ""} onClick={() => setTab("files")} data-testid="pr-tab-files">Files ({data.files.length})</button>
          </span>
          {tab === "files" && (
            <span className="mode-switch" role="tablist" aria-label="Diff layout">
              <button role="tab" aria-selected={layout === "unified"} className={layout === "unified" ? "active" : ""} onClick={() => setLayout("unified")} data-testid="pr-layout-unified">Unified</button>
              <button role="tab" aria-selected={layout === "split"} className={layout === "split" ? "active" : ""} onClick={() => setLayout("split")} data-testid="pr-layout-split">Side by side</button>
            </span>
          )}
          <SubmitReview apiBase={apiBase} drafts={accepted} undecided={undecided} fromGithub={review.pending_on_github ?? 0} owner={githubLogin}
            onDone={async (url) => { setNotice(url ? `Review published on GitHub.` : "Review published."); await loadComments(); await load(true); }}
            onError={(m) => setError(m)} />
          <span className="spacer" />
          {data.wiki_workspace_id && (
            <span className="muted small" title="The agent works in this wiki for this repository's pull requests">
              Agent wiki:{" "}
              <select value={data.wiki_workspace_id} onChange={(e) => { setPick(e.target.value); void put("/github/repo-wiki", { repo: p.repo, workspace_id: e.target.value }).then(() => { setData({ ...data, wiki_workspace_id: e.target.value }); onReady(e.target.value, data.doc_path, `#${p.number} ${p.title}`); }); }} data-testid="pr-wiki-select">
                {available.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
              </select>{" "}
              <button className="link-btn small" onClick={() => setRefs(true)} title={`Extra folders the agent reads for ${p.repo} pull requests (e.g. a checkout of the repository)`} data-testid="pr-context-folders">+ folders</button>
            </span>
          )}
          <button className="btn small primary" disabled={!docReady} onClick={() => setSheet(wholeDocument())} title={docReady ? "Ask the agent about this pull request" : "Choose the agent's wiki first"} data-testid="pr-ask">Ask about this PR</button>
          <button className="btn small" disabled={!docReady || reviewing} onClick={() => setSheet({ ...wholeDocument(), pr_review: true })}
            title={docReady ? "The agent reviews the PR; its line comments appear in Files as suggestions you apply or discard (nothing is posted until you finish the review)" : "Choose the agent's wiki first"} data-testid="pr-agent-review">
            {reviewing ? "Agent reviewing…" : "Agent review"}
          </button>
          <a className="btn small" href={p.url} target="_blank" rel="noreferrer">Open on GitHub</a>
          <RefreshButton className="btn small" onRefresh={() => load(true)} title="Reload from GitHub" testId="pr-refresh" />
        </div>
      </div>
      {!data.wiki_workspace_id && (
        <div className="banner" data-testid="pr-choose-wiki">
          Which wiki should the agent use for <b>{p.repo}</b> pull requests? It reads that folder for background (asked once per repository).
          <select value={pick} onChange={(e) => setPick(e.target.value)} data-testid="pr-wiki-choice">
            {available.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
          </select>
          <button className="btn small primary" onClick={() => void chooseWiki()} disabled={!pick} data-testid="pr-wiki-use">Use this wiki</button>
        </div>
      )}
      {error && <div className="banner warn" data-testid="pr-banner-error">{error} <button className="icon-btn" onClick={() => setError(null)} aria-label="Dismiss">×</button></div>}
      {lastRun && lastRun.state !== "running" && undecided > 0 && (
        <div className="banner" data-testid="pr-agent-review-done">
          The agent suggested {lastRun.added} comment(s){undecided !== lastRun.added ? ` (${undecided} undecided)` : ""}: open <button className="link-btn" onClick={() => setTab("files")}>Files</button> and choose <b>Apply</b> or <b>Discard</b> for each; only applied ones are published with your review.
          {lastRun.problem && <div className="muted small">Note: {lastRun.problem}</div>}
        </div>
      )}
      {lastRun && lastRun.state === "failed" && <div className="banner warn" data-testid="pr-agent-review-failed">Agent review: {lastRun.problem}</div>}
      {lastRun && lastRun.state === "done" && lastRun.added === 0 && lastRun.problem && <div className="banner warn small" data-testid="pr-agent-review-empty">Agent review added no comments: {lastRun.problem}</div>}
      {reviewing && <div className="banner small" data-testid="pr-agent-reviewing">The agent is reviewing this PR (follow it in the discussion panel); its comments appear in Files when it is done.</div>}
      {notice && <div className="banner" data-testid="pr-notice">{notice} <a href={`${p.url}/files`} target="_blank" rel="noreferrer">See it on GitHub</a> <button className="icon-btn" onClick={() => setNotice(null)} aria-label="Dismiss">×</button></div>}
      {tab === "overview" ? (
        <div className="pr-body" data-testid="pr-overview">
          <div className="pr-overview-grid">
            <div className="pr-main-col">
              <article className="markdown-body pr-description" dangerouslySetInnerHTML={{ __html: body }} data-testid="pr-description" />
              <section className="pr-conversation" data-testid="pr-conversation">
                <h3>Conversation</h3>
                {review.older_hidden > 0 && <div className="muted small">{review.older_hidden} older item(s) are only on GitHub.</div>}
                {review.conversation.length === 0 && <div className="muted small">No reviews or comments yet.</div>}
                {conv.items.map((c) => (
                  <div key={`${c.kind}${c.id}`} className={`pr-conv-item ${c.kind} ${(c.state ?? "").toLowerCase()}`} data-testid="pr-conv-item" data-kind={c.kind}>
                    <div className="small">
                      <b>{c.author}</b>{" "}
                      {c.kind === "review" ? <span className={`pr-review-state ${(c.state ?? "").toLowerCase()}`}>{REVIEW_LABEL[c.state ?? ""] ?? c.state?.toLowerCase()}</span> : <span className="muted">commented</span>}

                      <span className="muted"> · {c.created_at ? new Date(c.created_at).toLocaleString() : ""}</span>
                      {c.url && <> · <a href={c.url} target="_blank" rel="noreferrer">GitHub</a></>}
                    </div>
                    {c.body.trim() && <div className="markdown-body gh-comment-body" dangerouslySetInnerHTML={{ __html: md(c.body) }} />}
                    {(c.comments ?? []).map((lc) => (
                      <div key={lc.id} className="pr-conv-line" data-testid="pr-conv-line">
                        <button className="link-btn small pr-conv-where" onClick={() => jumpTo(lc.path)} title="Show in Files">
                          {lc.reply ? "replied on " : ""}{lc.path}{lc.line ? `:${lc.line}` : ""}{lc.outdated ? " (outdated)" : ""}
                        </button>
                        {lc.hunk && !lc.reply && <pre className="pr-conv-hunk">{lc.hunk.split("\n").map((l, i) => <div key={i} className={l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : ""}>{l || " "}</div>)}</pre>}
                        <div className="markdown-body gh-comment-body" dangerouslySetInnerHTML={{ __html: md(lc.body) }} />
                        {(conv.threads.get(lc.id) ?? []).map((r) => (
                          <div key={r.id} className="pr-conv-reply" data-testid="pr-conv-reply">
                            <div className="small"><b>{r.author}</b><span className="muted"> · {r.created_at ? new Date(r.created_at).toLocaleString() : ""}</span>{r.url && <> · <a href={r.url} target="_blank" rel="noreferrer">GitHub</a></>}</div>
                            <div className="markdown-body gh-comment-body" dangerouslySetInnerHTML={{ __html: md(r.body) }} />
                          </div>
                        ))}
                      </div>
                    ))}
                    {c.kind === "review" && (c.line_comments ?? 0) > (c.loaded ?? 0) && <div className="muted small">… {(c.line_comments ?? 0) - (c.loaded ?? 0)} more in Files</div>}
                  </div>
                ))}
                <div className="pr-conv-new">
                  <textarea rows={3} value={convText} onChange={(e) => setConvText(e.target.value)} placeholder={`Add a comment to the conversation (posted on GitHub; ${COMPOSER_HINT})`}
                    onKeyDown={(e) => composerKeyDown(e, () => void postConversation(), setConvText, () => setConvText(""))} data-testid="pr-conv-text" />
                  <div className="row">
                    <button className="btn small primary" disabled={convBusy || !convText.trim()} onClick={() => void postConversation()} data-testid="pr-conv-send">Comment</button>
                    <span className="muted small">For line comments and approval use <b>Files</b> and <b>Finish review</b>.</span>
                  </div>
                </div>
              </section>
            </div>
            <aside className="pr-side">
              <section>
                <h4>Checks {p.checks.length ? <span className={`ci-dot ${STATE_CLASS[p.ci ?? ""] ?? "none"}`} /> : null}</h4>
                {p.checks.length === 0 && <div className="muted small">No checks.</div>}
                {failing > 0 && <div className="small del">{failing} failing</div>}
                <ul className="pr-checks" data-testid="pr-checks">
                  {p.checks.slice(0, 40).map((c, i) => (
                    <li key={i}><span className={`ci-dot ${STATE_CLASS[c.state] ?? "none"}`} title={c.state} /> {c.url ? <a href={c.url} target="_blank" rel="noreferrer">{c.name}</a> : c.name}</li>
                  ))}
                  {p.checks.length > 40 && <li className="muted small">… {p.checks.length - 40} more on GitHub</li>}
                </ul>
              </section>
              <section>
                <h4>Reviews</h4>
                {p.reviews.length === 0 && <div className="muted small">No reviews yet.</div>}
                <ul className="pr-reviews" data-testid="pr-reviews">
                  {p.reviews.map((r, i) => <li key={i}><b>{r.author}</b> {REVIEW_LABEL[r.state] ?? r.state.toLowerCase()}</li>)}
                </ul>
                {p.requested.filter(Boolean).length > 0 && <div className="muted small">Requested: {p.requested.filter(Boolean).join(", ")}</div>}
              </section>
              <Contributors apiBase={apiBase} />
              {p.labels.length > 0 && (
                <section>
                  <h4>Labels</h4>
                  <div>{p.labels.map((l) => <span key={l.name} className="review-label" style={l.color ? { borderColor: `#${l.color}` } : undefined}>{l.name}</span>)}</div>
                </section>
              )}
            </aside>
          </div>
        </div>
      ) : (
        <div className="pr-body pr-files" data-testid="pr-files">
          <div className="muted small pad">
            Click a line to comment or ask the agent (drag or shift-click for several lines). Comments stay pending here until you
            <b> Finish review</b>; replies, resolving and Viewed go to GitHub at once.
            {accepted > 0 && <> · <b>{accepted} pending</b></>}
            {undecided > 0 && <> · <b data-testid="pr-undecided">{undecided} agent suggestion(s) to decide</b></>}
          </div>
          <div className="pr-files-bar">
            <span className="small" data-testid="pr-viewed-count"><b>{data.files.filter((f) => f.viewed === "VIEWED").length}</b> / {data.files.length} files viewed</span>
            <span className="small muted">{review.threads.filter((t) => !t.resolved).length} unresolved thread(s)</span>
            <select className="pr-jump" value="" onChange={(e) => { const el = document.querySelector(`[data-testid="pr-file"][data-file="${CSS.escape(e.target.value)}"]`); el?.scrollIntoView({ block: "start" }); }} aria-label="Jump to file" data-testid="pr-jump">
              <option value="">Jump to file…</option>
              {data.files.map((f) => <option key={f.path} value={f.path}>{f.viewed === "VIEWED" ? "✓ " : ""}{f.path}{threadsFor(f.path).length ? ` (${threadsFor(f.path).reduce((n, t) => n + t.comments.length, 0)})` : ""}</option>)}
            </select>
          </div>
          {data.files.map((f) => (
            <PrFileDiff key={f.path} file={f} layout={layout} threads={threadsFor(f.path)} drafts={draftsFor(f.path)} apiBase={apiBase} canAsk={docReady}
              collapsedDefault={f.unified.split("\n").length > FOLD_LINES && !threadsFor(f.path).length && !draftsFor(f.path).length}
              onChanged={loadComments} onError={(m) => setError(m)} onViewed={(v) => setViewed(f.path, v)}
              onAsk={(quote, label, question) => onSend({ quote, prefix: "", suffix: "", start: 0, end: 0, heading: null, target: { kind: "text", page: null, unit: "page", label } }, question, null)} />
          ))}
        </div>
      )}
      {sheet && (
        <div className="sheet-backdrop" onClick={() => setSheet(null)}>
          <div onClick={(e) => e.stopPropagation()}>
            <SelectionPopover anchor={sheet} style={{}} sessions={sessions} initialQuestion={sheet.pr_review ? AGENT_REVIEW_QUESTION : undefined} defaultSessionId={defaultSessionId} defaultProvider={defaultProvider} className="sheet"
              onSend={async (q, sid) => { await onSend(sheet, q, sid); setSheet(null); if (sheet.pr_review) await loadComments(); }} onMark={() => setSheet(null)} onReanchor={null} onClose={() => setSheet(null)} />
          </div>
        </div>
      )}
      {refs && <ContextFolders scope={`repo:${p.repo}`} title={`For ${p.repo} pull requests (besides the wiki).`} onClose={() => setRefs(false)} />}
      {wikiName && <div className="muted small pr-foot">The agent answers from the <b>{wikiName}</b> wiki. Discussions about this PR are in the panel on the right.</div>}
    </div>
  );
}

interface Contributor {
  login: string | null; name: string | null; url: string | null; avatar: string | null; roles: string[]; commits: number;
  public_email?: string | null; commit_emails: string[]; commit_names: string[]; company: string | null; location: string | null;
  bio: string | null; website: string | null; twitter: string | null; joined: string | null; followers: number | null;
  orgs: { login: string; name: string | null }[]; affiliation: string[]; hireable?: boolean | null; error?: string;
}
interface People { contributors: Contributor[]; commits_total: number; commits_listed: number; details?: boolean }

/** Who made the PR (author, commit authors, co-authors); per person, optional details from the public GitHub profile. */
function Contributors({ apiBase }: { apiBase: string }) {
  const [data, setData] = useState<People | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [details, setDetails] = useState(() => localStorage.getItem("waw.prPeopleDetails") === "1");
  const [busy, setBusy] = useState(false);
  useEffect(() => localStorage.setItem("waw.prPeopleDetails", details ? "1" : "0"), [details]);
  useEffect(() => {
    let live = true;
    setBusy(true);
    get<People>(`${apiBase}/contributors${details ? "?details=true" : ""}`)
      .then((d) => { if (live) { setData(d); setErr(null); } })
      .catch((e) => { if (live) setErr((e as ApiError).message); })
      .finally(() => { if (live) setBusy(false); });
    return () => { live = false; };
  }, [apiBase, details]);
  const key = (c: Contributor) => c.login ?? `~${c.name}`;
  const toggle = (k: string) => setOpen((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n; });
  return (
    <section data-testid="pr-contributors">
      <h4>Contributors {data ? <span className="muted small">· {data.contributors.length}</span> : null}</h4>
      <label className="small muted" title="Also load each person's public email, website, … (one GitHub request per person)">
        <input type="checkbox" checked={details} onChange={(e) => setDetails(e.target.checked)} data-testid="pr-contributors-details" /> details (email, orgs, company…)
      </label>
      {err && <div className="small del">{err}</div>}
      {!data && busy && <div className="muted small">Loading…</div>}
      <ul className="pr-people">
        {data?.contributors.map((c) => {
          const k = key(c);
          const show = details || open.has(k);
          return (
            <li key={k} className="pr-person" data-testid="pr-contributor" data-login={c.login ?? ""}>
              <button className="link-btn pr-person-head" onClick={() => toggle(k)} aria-expanded={show} title="Show / hide details">
                {c.avatar ? <img className="pr-avatar" src={`${c.avatar}${c.avatar.includes("?") ? "&" : "?"}s=40`} alt="" loading="lazy" /> : <span className="pr-avatar none" />}
                <span className="pr-person-name"><b>{c.login ?? c.name ?? "?"}</b>{c.login && c.name ? <span className="muted"> {c.name}</span> : null}</span>
              </button>
              <div className="small muted">
                {c.roles.join(", ")}{c.commits ? ` · ${c.commits} commit${c.commits > 1 ? "s" : ""}` : ""}
                {c.affiliation.map((a) => <span key={a} className="chip pr-aff" title="Guessed from company, emails and organizations">{a}</span>)}
              </div>
              {show && (
                <dl className="pr-person-details small" data-testid="pr-contributor-details">
                  {c.public_email && <><dt>Email</dt><dd><a href={`mailto:${c.public_email}`}>{c.public_email}</a></dd></>}
                  {c.commit_emails.filter((e) => e !== c.public_email).length > 0 && <><dt>Commit email</dt><dd>{c.commit_emails.filter((e) => e !== c.public_email).map((e) => <div key={e}><a href={`mailto:${e}`}>{e}</a></div>)}</dd></>}
                  {c.company && <><dt>Company</dt><dd>{c.company}</dd></>}
                  {c.orgs.length > 0 && <><dt>Orgs</dt><dd>{c.orgs.map((o) => <a key={o.login} className="pr-org" href={`https://github.com/${o.login}`} target="_blank" rel="noreferrer" title={o.name ?? o.login}>{o.login}</a>)}</dd></>}
                  {c.location && <><dt>Location</dt><dd>{c.location}</dd></>}
                  {c.website && <><dt>Website</dt><dd><a href={/^https?:/.test(c.website) ? c.website : `https://${c.website}`} target="_blank" rel="noreferrer">{c.website}</a></dd></>}
                  {c.twitter && <><dt>X / Twitter</dt><dd>@{c.twitter}</dd></>}
                  {c.bio && <><dt>Bio</dt><dd>{c.bio}</dd></>}
                  {c.joined && <><dt>Joined</dt><dd>{new Date(c.joined).toLocaleDateString()}{c.followers != null ? ` · ${c.followers} followers` : ""}</dd></>}
                  {c.commit_names.filter((n) => n !== c.name).length > 0 && <><dt>Git name</dt><dd>{c.commit_names.filter((n) => n !== c.name).join(", ")}</dd></>}
                  {c.url && <><dt>Profile</dt><dd><a href={c.url} target="_blank" rel="noreferrer">{c.url.replace("https://", "")}</a></dd></>}
                  {!c.login && <><dt>Note</dt><dd>Commit email not linked to a GitHub account</dd></>}
                  {!details && c.login && <dd className="muted">Public email: tick “details”.</dd>}
                </dl>
              )}
            </li>
          );
        })}
      </ul>
      {data && data.commits_total > data.commits_listed && <div className="muted small">From the first {data.commits_listed} of {data.commits_total} commits.</div>}
    </section>
  );
}
