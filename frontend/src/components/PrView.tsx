import { Splitter, usePane } from "./Split";
import { ModelChoiceValue } from "./ModelChoice";
import { useEffect, useMemo, useRef, useState } from "react";
import { confirmAction } from "./Confirm";
import { AgentSession, ApiError, WorkspaceInfo, get, post, put } from "../api";
import { renderGithubMarkdown } from "../markdown";
import { loadReviews, savePrefs, useReviews } from "./ReviewsPanel";
import { usePersistentState, useRestoredScroll } from "../persist";
import { SelectionAnchor, wholeDocument } from "../textmodel";
import { GhDraft, GhThread, PrFile, PrFileDiff, SubmitReview } from "./PrDiff";
import { COMPOSER_HINT, composerKeyDown } from "./keys";
import MentionTextarea from "./MentionTextarea";
import { SelectionPopover } from "./Reader";
import ContextFolders from "./ContextFolders";
import GhUser from "./GhUser";
import LocalRepo from "./LocalRepo";
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
export default function PrView({ repo, number, workspaces, docReady, onReady, onSend, sessions, defaultSessionId, defaultProvider, githubLogin = null, onOpenPr, onDiscussionsChanged, onOpenChat, onDevelop }: {
  repo: string;
  /** PR Development: check this pull request out in a development workspace (editor + agent) */
  onDevelop?: (repo: string, number: number, wikiWorkspaceId: string | null) => void;
  /** open another pull request (a link in a description or comment) as a tab of this app */
  onOpenPr?: (repo: string, number: number) => void;
  /** the discussions changed on the server (an agent review's answer was rewritten): reload them */
  onDiscussionsChanged?: () => void;
  /** open a chat of a wiki folder (e.g. a change summary) */
  onOpenChat?: (workspaceId: string, path: string) => void;
  /** the GitHub account reviews are published as (the server's gh login) */
  githubLogin?: string | null;
  number: number;
  workspaces: WorkspaceInfo[];
  /** the PR's discussion document is open (questions can be sent) */
  docReady: boolean;
  /** data loaded and a wiki is chosen: open the PR's discussion document in that folder */
  onReady: (workspaceId: string, docPath: string, title: string) => void;
  onSend: (anchor: SelectionAnchor, question: string, sessionChoice: string | null, modelChoice?: ModelChoiceValue | null) => Promise<void>;
  sessions: AgentSession[];
  defaultSessionId: string | null;
  defaultProvider: string | null;
}) {
  const [data, setData] = useState<PrData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = usePersistentState<"overview" | "files">(`pr.tab.${repo}#${number}`, "overview");
  const [sheet, setSheet] = useState<(SelectionAnchor & { pr_review?: boolean; about?: string }) | null>(null);
  const [pick, setPick] = useState("");
  const [layout, setLayout] = useState<"unified" | "split">(() => (localStorage.getItem("waw.prDiffLayout") as "unified" | "split") || "unified");
  useEffect(() => localStorage.setItem("waw.prDiffLayout", layout), [layout]);
  const [review, setReview] = useState<Review>({ threads: [], conversation: [], drafts: [], pending_on_github: null, older_hidden: 0 });
  const [convText, setConvText] = usePersistentState(`pr.conv.${repo}#${number}`, ""); // unsent text survives a reload
  const [convBusy, setConvBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [refs, setRefs] = useState(false);
  const sidePane = usePane("pr.side", 260, 180, 700);
  const [owner, name] = repo.split("/");
  const apiBase = `/github/pr/${owner}/${name}/${number}`;
  const [loadedComments, setLoadedComments] = useState(false);
  const loadComments = async () => {
    try {
      setReview(await get<Review>(`${apiBase}/comments`));
      setLoadedComments(true);
    } catch (e) {
      setError((e as ApiError).message);
    }
  };

  const [notPr, setNotPr] = useState(false);
  const load = async (refresh = false) => {
    try {
      const d = await get<PrData>(`/github/pr/${owner}/${name}/${number}${refresh ? "?refresh=true" : ""}`);
      setData(d);
      setError(null);
      void loadComments();
      if (d.wiki_workspace_id) onReady(d.wiki_workspace_id, d.doc_path, `#${d.pr.number} ${d.pr.title}`);
    } catch (e) {
      setError((e as ApiError).message);
      if ((e as ApiError).code === "PR_NOT_FOUND") setNotPr(true); // maybe an issue (#123 links): show it as one
    }
  };
  useEffect(() => {
    setData(null);
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo, number]);

  const available = workspaces.filter((w) => w.available);
  // folders (with a trailing /) and files of the PR, for "ask about a folder / file"
  const prFolders = useMemo(() => {
    const paths = (data?.files ?? []).map((f) => f.path).filter(Boolean);
    const dirs = new Set<string>();
    for (const p of paths) { const parts = p.split("/"); for (let i = 1; i < parts.length; i++) dirs.add(parts.slice(0, i).join("/") + "/"); }
    return [...[...dirs].sort(), ...paths.sort()];
  }, [data?.files]);
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
  const overviewScroll = useRestoredScroll(`pr.overview.${repo}#${number}`, !!data && tab === "overview" && loadedComments);
  const filesScroll = useRestoredScroll(`pr.files.${repo}#${number}`, !!data && tab === "files" && loadedComments);
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
  /** ask the agent about something said on the PR (a comment, a review, a line comment with its code) */
  const askAbout = (quote: string, label: string, question: string) =>
    onSend({ quote, prefix: "", suffix: "", start: 0, end: 0, heading: null, target: { kind: "text", page: null, unit: "page", label } }, question, null);

  if (error && !data && notPr) return <IssueView repo={repo} number={number} />;
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
          {p.state === "OPEN" && (
            <span className={`pr-status ${p.draft ? "draft" : "ready"}`} data-testid="pr-ready-status" title={p.draft ? "A draft: the author has not asked for reviews yet" : "Ready for review"}>
              {p.draft ? "Draft — not ready for review" : "Ready for review"}
            </span>
          )}
          <h2 className="pr-title" data-testid="pr-title">{p.title} <span className="muted">#{p.number}</span></h2>
        </div>
        <div className="pr-meta muted small">
          <b>{p.repo}</b>{p.private ? " (private)" : ""} · <GhUser login={p.author} bold={false} /> wants to merge <code>{p.head}</code> → <code>{p.base}</code> · {p.changed_files} files <span className="add">+{p.additions}</span> <span className="del">−{p.deletions}</span>
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
          {onDevelop && p.state === "OPEN" && (
            <button className="btn small" onClick={() => onDevelop(p.repo, p.number, data.wiki_workspace_id)} title="Check this pull request out in a development workspace: edit, build, test and fix it with the agent, then push to its branch" data-testid="pr-develop">
              Open in Development
            </button>
          )}
          <LocalRepo repo={p.repo} apiBase={apiBase} onOpenChat={onOpenChat} />
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
          {prFolders.length > 0 && (
            <select value="" disabled={!docReady} onChange={(e) => { if (e.target.value) setSheet({ ...wholeDocument(), about: e.target.value }); }}
              title="Ask the agent about a folder or a file of this PR" data-testid="pr-ask-about">
              <option value="">Ask about a folder / file…</option>
              {prFolders.map((f) => <option key={f} value={f}>{f.endsWith("/") ? `📁 ${f}` : f}</option>)}
            </select>
          )}
          <button className="btn small" disabled={!docReady || reviewing} onClick={() => setSheet({ ...wholeDocument(), pr_review: true })}
            title={docReady ? "The agent reviews the PR; its line comments appear in Files as suggestions you apply or discard (nothing is posted until you finish the review)" : "Choose the agent's wiki first"} data-testid="pr-agent-review">
            {reviewing ? "Agent reviewing…" : "Agent review"}
          </button>
          <PrStar repo={p.repo} number={p.number} />
          <PrStateMenu apiBase={apiBase} state={p.state} draft={p.draft} number={p.number} onDone={() => load(true)} onError={setError} />
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
        <div className="pr-body" data-testid="pr-overview" ref={overviewScroll}>
          <div className={`pr-overview-grid ${sidePane.folded ? "side-folded" : ""}`} style={{ ["--pr-side" as string]: `${sidePane.size}px` }}>
            <div className="pr-main-col">
              <article className="markdown-body pr-description" dangerouslySetInnerHTML={{ __html: body }} data-testid="pr-description" />
              <section className="pr-conversation" data-testid="pr-conversation">
                <h3>Conversation</h3>
                {review.older_hidden > 0 && <div className="muted small">{review.older_hidden} older item(s) are only on GitHub.</div>}
                {review.conversation.length === 0 && <div className="muted small">No reviews or comments yet.</div>}
                {conv.items.map((c) => (
                  <div key={`${c.kind}${c.id}`} className={`pr-conv-item ${c.kind} ${(c.state ?? "").toLowerCase()}`} data-testid="pr-conv-item" data-kind={c.kind}>
                    <div className="small">
                      <GhUser login={c.author} />{" "}
                      {c.kind === "review" ? <span className={`pr-review-state ${(c.state ?? "").toLowerCase()}`}>{REVIEW_LABEL[c.state ?? ""] ?? c.state?.toLowerCase()}</span> : <span className="muted">commented</span>}

                      <span className="muted"> · {c.created_at ? new Date(c.created_at).toLocaleString() : ""}</span>
                      {c.url && <> · <a href={c.url} target="_blank" rel="noreferrer">GitHub</a></>}
                    </div>
                    {c.body.trim() && <div className="markdown-body gh-comment-body" dangerouslySetInnerHTML={{ __html: md(c.body) }} />}
                    {c.body.trim() && (
                      <AskComment canAsk={docReady} label={`${c.kind === "review" ? "review" : "comment"} by ${c.author ?? "?"}`} onAsk={askAbout}
                        quote={`${c.author ?? "?"} (${c.kind === "review" ? REVIEW_LABEL[c.state ?? ""] ?? "reviewed" : "commented"}):\n${c.body}`} />
                    )}
                    {(c.comments ?? []).map((lc) => (
                      <div key={lc.id} className="pr-conv-line" data-testid="pr-conv-line">
                        <button className="link-btn small pr-conv-where" onClick={() => jumpTo(lc.path)} title="Show in Files">
                          {lc.reply ? "replied on " : ""}{lc.path}{lc.line ? `:${lc.line}` : ""}{lc.outdated ? " (outdated)" : ""}
                        </button>
                        {lc.hunk && !lc.reply && <pre className="pr-conv-hunk">{lc.hunk.split("\n").map((l, i) => <div key={i} className={l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : ""}>{l || " "}</div>)}</pre>}
                        <div className="markdown-body gh-comment-body" dangerouslySetInnerHTML={{ __html: md(lc.body) }} />
                        {(conv.threads.get(lc.id) ?? []).map((r) => (
                          <div key={r.id} className="pr-conv-reply" data-testid="pr-conv-reply">
                            <div className="small"><GhUser login={r.author} /><span className="muted"> · {r.created_at ? new Date(r.created_at).toLocaleString() : ""}</span>{r.url && <> · <a href={r.url} target="_blank" rel="noreferrer">GitHub</a></>}</div>
                            <div className="markdown-body gh-comment-body" dangerouslySetInnerHTML={{ __html: md(r.body) }} />
                          </div>
                        ))}
                        <AskComment canAsk={docReady} label={`comment by ${lc.author ?? c.author ?? "?"} on ${lc.path}${lc.line ? `:${lc.line}` : ""}`} onAsk={askAbout}
                          quote={`${lc.hunk ? lc.hunk + "\n\n" : ""}${lc.author ?? c.author ?? "?"}: ${lc.body}${(conv.threads.get(lc.id) ?? []).map((r) => `\n\n${r.author}: ${r.body}`).join("")}`} />
                      </div>
                    ))}
                    {c.kind === "review" && (c.line_comments ?? 0) > (c.loaded ?? 0) && <div className="muted small">… {(c.line_comments ?? 0) - (c.loaded ?? 0)} more in Files</div>}
                  </div>
                ))}
                <div className="pr-conv-new">
                  <MentionTextarea apiBase={apiBase} rows={3} value={convText} onValue={setConvText} placeholder={`Add a comment to the conversation (posted on GitHub; ${COMPOSER_HINT})`}
                    onKeyDown={(e) => composerKeyDown(e, () => void postConversation(), setConvText, () => setConvText(""))} data-testid="pr-conv-text" />
                  <div className="row">
                    <button className="btn small primary" disabled={convBusy || !convText.trim()} onClick={() => void postConversation()} data-testid="pr-conv-send">Comment</button>
                    <span className="muted small">For line comments and approval use <b>Files</b> and <b>Finish review</b>.</span>
                  </div>
                </div>
              </section>
            </div>
            <Splitter dir="x" side="after" pane={sidePane} label="side column" className="pr-side-splitter" testId="pr-side-splitter" />
            {!sidePane.folded && <aside className="pr-side">
              <Contributors apiBase={apiBase} />
              <Reviewers apiBase={apiBase} reviews={p.reviews} requested={p.requested.filter(Boolean) as string[]} onChanged={() => void load(true)} onError={setError} />
              <section>
                <h4>Checks {p.checks.length ? <span className={`ci-dot ${STATE_CLASS[p.ci ?? ""] ?? "none"}`} /> : null}</h4>
                {p.checks.length === 0 && <div className="muted small">No checks.</div>}
                <CiControls apiBase={apiBase} onChanged={() => void load(true)} onError={setError} />
                {failing > 0 && (
                  <div className="small del">
                    {failing} failing{" "}
                    <CiAsk apiBase={apiBase} canAsk={docReady} onAsk={askAbout} label="all failing checks" />
                  </div>
                )}
                <ul className="pr-checks" data-testid="pr-checks">
                  {p.checks.slice(0, 40).map((c, i) => (
                    <li key={i}>
                      <span className={`ci-dot ${STATE_CLASS[c.state] ?? "none"}`} title={c.state} /> {c.url ? <a href={c.url} target="_blank" rel="noreferrer">{c.name}</a> : c.name}
                      {STATE_CLASS[c.state] === "failure" && c.name && <CiAsk apiBase={apiBase} canAsk={docReady} onAsk={askAbout} checks={[c.name]} label={c.name} compact />}
                    </li>
                  ))}
                  {p.checks.length > 40 && <li className="muted small">… {p.checks.length - 40} more on GitHub</li>}
                </ul>
              </section>
              {p.labels.length > 0 && (
                <section>
                  <h4>Labels</h4>
                  <div>{p.labels.map((l) => <span key={l.name} className="review-label" style={l.color ? { borderColor: `#${l.color}` } : undefined}>{l.name}</span>)}</div>
                </section>
              )}
            </aside>}
          </div>
        </div>
      ) : (
        <div className="pr-body pr-files" data-testid="pr-files" ref={filesScroll}>
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
            <SelectionPopover anchor={sheet} style={{}} sessions={sessions} initialQuestion={sheet.pr_review ? AGENT_REVIEW_QUESTION : sheet.about ? `About ${sheet.about.endsWith("/") ? "the folder" : "the file"} \`${sheet.about}\` of this PR (its changes and the code around them): ` : undefined} defaultSessionId={defaultSessionId} defaultProvider={defaultProvider} className="sheet"
              onSend={async (q, sid, mc) => { await onSend(sheet, q, sid, mc); setSheet(null); if (sheet.pr_review) await loadComments(); }} onMark={() => setSheet(null)} onReanchor={null} onClose={() => setSheet(null)} />
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
              <div className="pr-person-head">
                {c.avatar ? <img className="pr-avatar" src={`${c.avatar}${c.avatar.includes("?") ? "&" : "?"}s=40`} alt="" loading="lazy" /> : <span className="pr-avatar none" />}
                <span className="pr-person-name">{c.login ? <GhUser login={c.login} /> : <b>{c.name ?? "?"}</b>}{c.login && c.name ? <span className="muted"> {c.name}</span> : null}</span>
                <button className="link-btn small" onClick={() => toggle(k)} aria-expanded={show} title="Show / hide details" data-testid="pr-contributor-toggle">{show ? "▾" : "▸"}</button>
              </div>
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

/** "Ask agent…" under a comment: the question goes to the agent with the comment (and its code) as the quote. */
function AskComment({ quote, label, canAsk, onAsk }: { quote: string; label: string; canAsk: boolean; onAsk: (quote: string, label: string, question: string) => Promise<void> }) {
  const [text, setText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const send = async () => {
    if (!text?.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      await onAsk(quote.slice(0, 20000), label, text.trim());
      setText(null);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  if (text === null) {
    return (
      <div className="row gh-thread-actions">
        <button className="link-btn small" disabled={!canAsk} onClick={() => setText("")} title={canAsk ? "Ask the agent about this comment" : "Choose the agent's wiki first"} data-testid="pr-conv-ask">Ask agent…</button>
      </div>
    );
  }
  return (
    <div className="gh-reply-box">
      <textarea autoFocus rows={2} value={text} onChange={(e) => setText(e.target.value)} placeholder={`Ask the agent about this comment, e.g. "Is the reviewer right?" (${COMPOSER_HINT})`}
        onKeyDown={(e) => composerKeyDown(e, () => void send(), setText, () => setText(null))} data-testid="pr-conv-ask-text" />
      {err && <div className="small del">{err}</div>}
      <div className="row">
        <button className="btn small primary" disabled={busy || !text.trim()} onClick={() => void send()} data-testid="pr-conv-ask-send">Ask agent</button>
        <button className="btn small" onClick={() => setText(null)}>Cancel</button>
      </div>
    </div>
  );
}

interface Candidate { login: string; name: string | null; source: string; requested?: boolean }

/** Reviews given and requested; request a review from someone (with suggestions) or cancel a request — on GitHub. */
function Reviewers({ apiBase, reviews, requested, onChanged, onError }: {
  apiBase: string; reviews: { author: string | null; state: string }[]; requested: string[]; onChanged: () => void; onError: (m: string) => void;
}) {
  const [q, setQ] = useState<string | null>(null);
  const [items, setItems] = useState<Candidate[]>([]);
  const [sel, setSel] = useState(0);
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);
  useEffect(() => {
    if (q === null) return;
    const n = ++seq.current;
    const t = window.setTimeout(() => {
      get<{ candidates: Candidate[] }>(`${apiBase}/reviewer-candidates?q=${encodeURIComponent(q.trim())}`)
        .then((r) => { if (n === seq.current) { setItems(r.candidates); setSel(0); } })
        .catch(() => undefined);
    }, q ? 200 : 0);
    return () => window.clearTimeout(t);
  }, [q, apiBase]);
  const [note, setNote] = useState<string | null>(null);
  const change = async (body: { add?: string[]; remove?: string[] }) => {
    setBusy(true);
    try {
      await post(`${apiBase}/reviewers`, body);
      setNote(body.add?.length ? `Review requested from ${body.add.join(", ")} on GitHub.` : `Request cancelled on GitHub.`);
      onChanged();
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const add = (login: string) => {
    setQ(null);
    void change({ add: [login] });
  };
  const remove = async (login: string) => {
    if (!(await confirmAction({ title: `Cancel the review request for ${login}?`, message: "GitHub removes the pending request (their earlier reviews stay).", confirmLabel: "Cancel request" }))) return;
    void change({ remove: [login] });
  };
  const open = items.filter((c) => !c.requested);
  return (
    <section data-testid="pr-reviewers">
      <h4>Reviewers</h4>
      {reviews.length === 0 && requested.length === 0 && <div className="muted small">No reviews or requests yet.</div>}
      <ul className="pr-reviews" data-testid="pr-reviews">
        {reviews.map((r, i) => (
          <li key={i} data-testid="pr-review-item">
            <GhUser login={r.author} /> {REVIEW_LABEL[r.state] ?? r.state.toLowerCase()}
            {r.author && !requested.includes(r.author) && !r.author.endsWith("[bot]") && (
              <button className="icon-btn small rerequest" disabled={busy} onClick={() => void change({ add: [r.author!] })}
                title={`Re-request review from ${r.author} (GitHub notifies them; e.g. after you addressed their comments)`} aria-label={`Re-request review from ${r.author}`} data-testid="pr-reviewer-rerequest">↻</button>
            )}
            {r.author && requested.includes(r.author) && (
              <>
                <span className="muted small" data-testid="pr-rerequested"> · re-requested</span>{" "}
                <button className="icon-btn small" disabled={busy} onClick={() => void remove(r.author!)} title={`Cancel the review request for ${r.author}`} aria-label={`Remove ${r.author}`} data-testid="pr-reviewer-remove">×</button>
              </>
            )}
          </li>
        ))}
        {requested.filter((x) => !reviews.some((r) => r.author === x)).map((x) => (
          <li key={x} data-testid="pr-requested">
            <GhUser login={x} /> <span className="muted">awaiting review</span>{" "}
            <button className="icon-btn small" disabled={busy} onClick={() => void remove(x)} title={`Cancel the review request for ${x}`} aria-label={`Remove ${x}`} data-testid="pr-reviewer-remove">×</button>
          </li>
        ))}
      </ul>
      {note && <div className="muted small" data-testid="pr-reviewer-note">{note}</div>}
      {q === null ? (
        <button className="link-btn small" onClick={() => setQ("")} data-testid="pr-reviewer-add">+ Request a review…</button>
      ) : (
        <span className="mention-wrap">
          <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Name or login" data-testid="pr-reviewer-input"
            onBlur={() => window.setTimeout(() => setQ(null), 200)}
            onKeyDown={(e) => {
              if (e.key === "Escape") setQ(null);
              else if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); setSel((i) => (open.length ? (i + (e.key === "ArrowDown" ? 1 : open.length - 1)) % open.length : 0)); }
              else if (e.key === "Enter" && !e.nativeEvent.isComposing) { e.preventDefault(); const c = open[sel]; if (c) add(c.login); else if (/^[\w.-]+$/.test(q.trim())) add(q.trim()); }
            }} />
          {open.length > 0 && (
            <ul className="mention-list" role="listbox" data-testid="pr-reviewer-list">
              {open.map((c, i) => (
                <li key={c.login} role="option" aria-selected={i === sel} className={i === sel ? "on" : ""} onMouseDown={(e) => { e.preventDefault(); add(c.login); }} onMouseEnter={() => setSel(i)} data-testid="pr-reviewer-item">
                  <b>{c.login}</b>{c.name ? <span className="muted"> {c.name}</span> : null}{c.source !== "repository" ? <span className="muted small"> · {c.source}</span> : null}
                </li>
              ))}
            </ul>
          )}
        </span>
      )}
    </section>
  );
}

interface CiFailure { name: string; conclusion: string; url: string | null; log_path: string | null; lines: number; excerpt: string | null; error: string | null }

/** Ask the agent about failing CI: the server downloads the failing jobs' logs first, so the agent can read them. */
function CiAsk({ apiBase, canAsk, onAsk, checks, label, compact = false }: {
  apiBase: string; canAsk: boolean; onAsk: (quote: string, label: string, question: string) => Promise<void>; checks?: string[]; label: string; compact?: boolean;
}) {
  const [text, setText] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const send = async () => {
    setBusy("Downloading the CI logs…");
    setErr(null);
    try {
      const r = await post<{ failures: CiFailure[]; total_failed: number; logs_dir: string }>(`${apiBase}/ci-failures`, { checks: checks ?? null });
      if (!r.failures.length) throw new ApiError(404, "NO_FAILURES", "no failing check found (it may have been re-run)", {});
      const quote = [
        `CI failures of this PR (${r.total_failed} failing${r.failures.length < r.total_failed ? `; ${r.failures.length} downloaded` : ""}). Full logs are saved on the server; read them (search for "##[error]", "FAILED", "Traceback").`,
        ...r.failures.map((f) => [
          `### ${f.name} — ${f.conclusion}`, f.url ? `Link: ${f.url}` : "",
          f.log_path ? `Full log (${f.lines} lines): ${f.log_path}` : `No log: ${f.error ?? "not available"}`,
          f.excerpt ? "Excerpt (the output leading to the error):\n" + f.excerpt : "",
        ].filter(Boolean).join("\n")),
      ].join("\n\n");
      setBusy("Asking the agent…");
      const q = (text ?? "").trim() || "Investigate these CI failures.";
      await onAsk(quote.slice(0, 20000), `CI: ${label}`, `${q}\n\nRead the full CI logs listed above, find the root cause of each failure, and say whether it is caused by this PR (point at the change), a flaky or known issue, or the CI infrastructure; suggest a fix or a re-run.`);
      setText(null);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };
  if (text === null) {
    return (
      <button className={`link-btn small ${compact ? "ci-ask-compact" : ""}`} disabled={!canAsk} onClick={() => setText("")}
        title={canAsk ? `Ask the agent about the CI failure${checks ? ` of ${label}` : "s"}: it reads the logs and investigates` : "Choose the agent's wiki first"} data-testid={compact ? "pr-ci-ask-one" : "pr-ci-ask"}>
        {compact ? "Ask" : "Ask agent about the failures…"}
      </button>
    );
  }
  return (
    <div className="gh-reply-box ci-ask-box" data-testid="pr-ci-ask-box">
      <textarea autoFocus rows={2} value={text} onChange={(e) => setText(e.target.value)} placeholder={`Optional: what to look at, e.g. "only the XPU jobs" (${COMPOSER_HINT})`}
        onKeyDown={(e) => composerKeyDown(e, () => void send(), setText, () => setText(null))} data-testid="pr-ci-ask-text" />
      {err && <div className="small del" role="alert" data-testid="pr-ci-ask-error">{err}</div>}
      <div className="row">
        <button className="btn small primary" disabled={!!busy} onClick={() => void send()} data-testid="pr-ci-ask-send">{busy ?? "Investigate"}</button>
        <button className="btn small" onClick={() => setText(null)}>Cancel</button>
      </div>
    </div>
  );
}

/** ★ this PR as a priority PR (or un-star it so it never is) — the same star as in Reviews. */
function PrStar({ repo, number }: { repo: string; number: number }) {
  const { queue } = useReviews();
  const key = `${repo}#${number}`.toLowerCase();
  const prefs = queue?.prefs;
  if (!prefs) return null;
  const inQueue = queue.items.find((x) => `${x.repo}#${x.number}`.toLowerCase() === key);
  const on = (prefs.starred ?? []).includes(key) || (!!inQueue?.priority && !(prefs.unstarred ?? []).includes(key));
  return (
    <button className={`btn small pr-star ${on ? "on" : ""}`} onClick={() => void savePrefs(on ? { unstar: key } : { star: key }).then(() => loadReviews(false))}
      title={on ? "Priority PR — click to un-star (never priority)" : "Star as a priority PR"} data-testid="pr-star" aria-pressed={on}>
      {on ? "★ Priority" : "☆ Star"}
    </button>
  );
}

/** Close / reopen the PR, mark it ready for review or turn it back into a draft — on GitHub at once. */
function PrStateMenu({ apiBase, state, draft, number, onDone, onError }: {
  apiBase: string; state: string; draft: boolean; number: number; onDone: () => Promise<void> | void; onError: (m: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const act = async (action: "close" | "reopen" | "ready" | "draft") => {
    if (action === "close" && !(await confirmAction({ title: `Close pull request #${number}?`, message: "It is closed on GitHub without merging (it can be reopened).", confirmLabel: "Close pull request" }))) return;
    setBusy(true);
    try {
      await post(`${apiBase}/state`, { action });
      await onDone();
      void loadReviews(true);
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  if (state === "MERGED") return null;
  if (state === "CLOSED") return <button className="btn small" disabled={busy} onClick={() => void act("reopen")} data-testid="pr-reopen">Reopen</button>;
  return (
    <>
      {draft
        ? <button className="btn small" disabled={busy} onClick={() => void act("ready")} title="Mark this draft as ready for review" data-testid="pr-mark-ready">Ready for review</button>
        : <button className="btn small" disabled={busy} onClick={() => void act("draft")} title="Convert back to a draft" data-testid="pr-to-draft">Convert to draft</button>}
      <button className="btn small danger" disabled={busy} onClick={() => void act("close")} data-testid="pr-close">Close PR</button>
    </>
  );
}

interface IssueData {
  repo: string; number: number; title: string; url: string; body: string; state: string; state_reason: string | null; author: string | null;
  labels: { name: string; color: string | null }[]; assignees: string[]; comments: { id: number; author: string | null; body: string; created_at: string; url: string | null }[];
  older_hidden: number;
}

/** An issue (a #123 link that is not a pull request): read it, comment, close or reopen it on GitHub. */
function IssueView({ repo, number }: { repo: string; number: number }) {
  const [owner, name] = repo.split("/");
  const base = `/github/issue/${owner}/${name}/${number}`;
  const [d, setD] = useState<IssueData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const load = async () => {
    try {
      setD(await get<IssueData>(base));
      setErr(null);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  useEffect(() => { void load(); }, [base]); // eslint-disable-line react-hooks/exhaustive-deps
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await load();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const close = async (reason: "completed" | "not_planned") => {
    if (!(await confirmAction({ title: `Close issue #${number} as ${reason === "completed" ? "completed" : "not planned"}?`, message: "It is closed on GitHub (it can be reopened).", confirmLabel: "Close issue" }))) return;
    await run(() => post(`${base}/state`, { action: "close", reason }));
  };
  const md = (s: string) => renderGithubMarkdown(s, repo);
  if (!d) return <div className="pr-view"><div className={err ? "banner warn" : "muted pad"} data-testid="pr-error">{err ?? `Loading ${repo}#${number} from GitHub…`} <a className="btn small" href={`https://github.com/${repo}/issues/${number}`} target="_blank" rel="noreferrer">Open on GitHub</a></div></div>;
  return (
    <div className="pr-view" data-testid="issue-view">
      <div className="pr-head">
        <div className="pr-title-row">
          <span className={`pr-status ${d.state === "OPEN" ? "ready" : "closed"}`} data-testid="issue-state">{d.state === "OPEN" ? "Open issue" : `Closed${d.state_reason === "NOT_PLANNED" ? " (not planned)" : ""}`}</span>
          <h2 className="pr-title">{d.title} <span className="muted">#{d.number}</span></h2>
        </div>
        <div className="pr-meta muted small"><b>{d.repo}</b> · opened by <GhUser login={d.author} bold={false} />{d.assignees.length ? <> · assigned to {d.assignees.map((a) => <span key={a}><GhUser login={a} bold={false} /> </span>)}</> : null}</div>
        <div className="pr-actions">
          {d.state === "OPEN" ? (
            <>
              <button className="btn small danger" disabled={busy} onClick={() => void close("completed")} data-testid="issue-close">Close as completed</button>
              <button className="btn small" disabled={busy} onClick={() => void close("not_planned")} data-testid="issue-close-not-planned">Close as not planned</button>
            </>
          ) : (
            <button className="btn small" disabled={busy} onClick={() => void run(() => post(`${base}/state`, { action: "reopen" }))} data-testid="issue-reopen">Reopen</button>
          )}
          <span className="spacer" />
          <a className="btn small" href={d.url} target="_blank" rel="noreferrer">Open on GitHub</a>
        </div>
      </div>
      {err && <div className="banner warn">{err}</div>}
      <div className="pr-body">
        <div className="pr-main-col" style={{ padding: "8px 16px" }}>
          <article className="markdown-body pr-description" dangerouslySetInnerHTML={{ __html: md(d.body || "_No description._") }} data-testid="issue-body" />
          <section className="pr-conversation">
            <h3>Comments</h3>
            {d.older_hidden > 0 && <div className="muted small">{d.older_hidden} older comment(s) are only on GitHub.</div>}
            {d.comments.map((c) => (
              <div key={c.id} className="pr-conv-item comment" data-testid="issue-comment">
                <div className="small"><GhUser login={c.author} /> <span className="muted">· {new Date(c.created_at).toLocaleString()}</span></div>
                <div className="markdown-body gh-comment-body" dangerouslySetInnerHTML={{ __html: md(c.body) }} />
              </div>
            ))}
            <div className="pr-conv-new">
              <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder={`Comment on the issue (posted on GitHub; ${COMPOSER_HINT})`}
                onKeyDown={(e) => composerKeyDown(e, () => void run(async () => { await post(`${base}/comments`, { body: text }); setText(""); }), setText)} data-testid="issue-comment-text" />
              <div className="row"><button className="btn small primary" disabled={busy || !text.trim()} onClick={() => void run(async () => { await post(`${base}/comments`, { body: text }); setText(""); })} data-testid="issue-comment-send">Comment</button></div>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

interface CiRun { id: number; name: string; status: string; conclusion: string | null; url: string | null; attempt: number; event: string }
interface CiInfo { head_sha: string; head: string; runs: CiRun[]; workflows: { id: number; name: string; path: string }[]; ciflow_labels: string[]; ciflow_on: string[] }

/** Trigger CI on GitHub: re-run (failed jobs or all) / cancel the PR's workflow runs, ciflow/* labels, manual workflows. */
function CiControls({ apiBase, onChanged, onError }: { apiBase: string; onChanged: () => void; onError: (m: string) => void }) {
  const [open, setOpen] = useState(false);
  const [info, setInfo] = useState<CiInfo | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [wf, setWf] = useState<number | "">("");
  const load = async () => {
    try {
      setInfo(await get<CiInfo>(`${apiBase}/workflows`));
    } catch (e) {
      onError((e as ApiError).message);
    }
  };
  useEffect(() => { if (open) void load(); }, [open]); // eslint-disable-line react-hooks/exhaustive-deps
  const act = async (key: string, body: Record<string, unknown>, done: string) => {
    if (body.action === "cancel" && !(await confirmAction({ title: "Cancel this workflow run?", message: "Its running jobs stop on GitHub.", confirmLabel: "Cancel run" }))) return;
    setBusy(key);
    try {
      await post(`${apiBase}/ci`, body);
      setNote(done);
      window.setTimeout(() => { void load(); onChanged(); }, 2500); // GitHub needs a moment to show the new state
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };
  if (!open) return <button className="link-btn small" onClick={() => setOpen(true)} data-testid="pr-ci-open">▶ Run CI…</button>;
  return (
    <div className="pr-ci-box" data-testid="pr-ci-box">
      <div className="row"><b className="small">Run CI</b><span className="spacer" /><button className="icon-btn small" onClick={() => setOpen(false)} aria-label="Close">×</button></div>
      {!info && <div className="muted small">Loading…</div>}
      {note && <div className="muted small" data-testid="pr-ci-note">{note}</div>}
      {info && info.ciflow_labels.length > 0 && (
        <div className="small">
          <div className="muted">ciflow labels (adding one starts those workflows):</div>
          <div className="pr-ciflow">
            {info.ciflow_labels.map((lb) => {
              const on = info.ciflow_on.includes(lb);
              return (
                <label key={lb} className="pr-ciflow-item">
                  <input type="checkbox" checked={on} disabled={busy !== null} data-testid="pr-ciflow" data-label={lb}
                    onChange={() => void act(lb, { action: on ? "unlabel" : "label", label: lb }, on ? `Removed ${lb}.` : `Added ${lb}: its workflows start on GitHub.`)} /> {lb.replace("ciflow/", "")}
                </label>
              );
            })}
          </div>
        </div>
      )}
      {info && (
        <ul className="pr-ci-runs small" data-testid="pr-ci-runs">
          {info.runs.length === 0 && <li className="muted">No workflow runs for the head commit.</li>}
          {info.runs.map((r) => {
            const running = r.status !== "completed";
            const failed = ["failure", "timed_out", "cancelled", "startup_failure"].includes(r.conclusion ?? "");
            return (
              <li key={r.id} data-testid="pr-ci-run" data-name={r.name}>
                <span className={`ci-dot ${running ? "pending" : failed ? "failure" : r.conclusion === "success" ? "success" : "none"}`} />{" "}
                {r.url ? <a href={r.url} target="_blank" rel="noreferrer">{r.name}</a> : r.name}
                <span className="muted"> · {running ? r.status.replace("_", " ") : r.conclusion}{r.attempt > 1 ? ` · attempt ${r.attempt}` : ""}</span>{" "}
                {failed && <button className="link-btn small" disabled={busy !== null} onClick={() => void act(`f${r.id}`, { action: "rerun_failed", run_id: r.id }, `Re-running the failed jobs of ${r.name}.`)} data-testid="pr-ci-rerun-failed">re-run failed</button>}{" "}
                {!running && <button className="link-btn small" disabled={busy !== null} onClick={() => void act(`a${r.id}`, { action: "rerun", run_id: r.id }, `Re-running ${r.name}.`)} data-testid="pr-ci-rerun">re-run all</button>}
                {running && <button className="link-btn small danger" disabled={busy !== null} onClick={() => void act(`c${r.id}`, { action: "cancel", run_id: r.id }, `Cancelling ${r.name}.`)} data-testid="pr-ci-cancel">cancel</button>}
              </li>
            );
          })}
        </ul>
      )}
      {info && info.workflows.length > 0 && (
        <div className="row small">
          <select value={wf} onChange={(e) => setWf(e.target.value ? Number(e.target.value) : "")} data-testid="pr-ci-workflow" aria-label="Workflow">
            <option value="">Start a workflow by hand…</option>
            {info.workflows.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
          </select>
          <button className="btn small" disabled={!wf || busy !== null} onClick={() => wf && void act("d", { action: "dispatch", workflow_id: wf }, `Started on ${info.head}.`)}
            title={`Runs on the PR's branch ${info.head} (workflows with a manual trigger only)`} data-testid="pr-ci-dispatch">Run on {info.head}</button>
        </div>
      )}
    </div>
  );
}
