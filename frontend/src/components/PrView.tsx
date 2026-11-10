import { useEffect, useMemo, useState } from "react";
import { AgentSession, ApiError, WorkspaceInfo, get, post, put } from "../api";
import { renderMarkdown, sanitizeHtml } from "../markdown";
import { SelectionAnchor, wholeDocument } from "../textmodel";
import { GhDraft, GhThread, PrFile, PrFileDiff, SubmitReview } from "./PrDiff";
import { COMPOSER_HINT, composerKeyDown } from "./keys";
import { SelectionPopover } from "./Reader";
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

interface ConvItem { kind: "review" | "comment"; id: number; author: string | null; state?: string; body: string; created_at: string | null; url: string | null; line_comments?: number }
interface Review { threads: GhThread[]; conversation: ConvItem[]; drafts: GhDraft[]; pending_on_github: number | null; older_hidden: number }
const md = (s: string) => sanitizeHtml(renderMarkdown(s || "", ""), "");

const STATE_CLASS: Record<string, string> = { SUCCESS: "success", NEUTRAL: "success", SKIPPED: "none", FAILURE: "failure", ERROR: "failure", TIMED_OUT: "failure", CANCELLED: "failure", ACTION_REQUIRED: "pending", PENDING: "pending", IN_PROGRESS: "pending", QUEUED: "pending", EXPECTED: "pending" };
const REVIEW_LABEL: Record<string, string> = { APPROVED: "approved", CHANGES_REQUESTED: "requested changes", COMMENTED: "commented", DISMISSED: "dismissed", PENDING: "pending" };

/**
 * A pull request as a tab: Overview (description, checks, reviews) and Files (diff). Questions go to the agent in
 * the repository's wiki (chosen once per repository): about the whole PR, or about lines selected in the diff.
 */
export default function PrView({ repo, number, workspaces, docReady, onReady, onSend, sessions, defaultSessionId, defaultProvider, githubLogin = null }: {
  repo: string;
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
  const [sheet, setSheet] = useState<SelectionAnchor | null>(null);
  const [pick, setPick] = useState("");
  const [layout, setLayout] = useState<"unified" | "split">(() => (localStorage.getItem("waw.prDiffLayout") as "unified" | "split") || "unified");
  useEffect(() => localStorage.setItem("waw.prDiffLayout", layout), [layout]);
  const [review, setReview] = useState<Review>({ threads: [], conversation: [], drafts: [], pending_on_github: null, older_hidden: 0 });
  const [convText, setConvText] = useState("");
  const [convBusy, setConvBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
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

  const body = useMemo(() => (data ? sanitizeHtml(renderMarkdown(data.pr.body || "_No description._", ""), "") : ""), [data]);

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

  if (error && !data) return <div className="pr-view"><div className="banner warn" data-testid="pr-error">{error} <button className="btn small" onClick={() => void load(true)}>Retry</button></div></div>;
  if (!data) return <div className="pr-view"><div className="muted pad">Loading {repo}#{number} from GitHub…</div></div>;
  const p = data.pr;
  const wikiName = workspaces.find((w) => w.id === data.wiki_workspace_id)?.name;
  const failing = p.checks.filter((c) => STATE_CLASS[c.state] === "failure").length;

  return (
    <div className="pr-view" data-testid="pr-view">
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
          <SubmitReview apiBase={apiBase} drafts={review.drafts.length} fromGithub={review.pending_on_github ?? 0} owner={githubLogin}
            onDone={async (url) => { setNotice(url ? `Review published on GitHub.` : "Review published."); await loadComments(); await load(true); }}
            onError={(m) => setError(m)} />
          <span className="spacer" />
          {data.wiki_workspace_id && (
            <span className="muted small" title="The agent works in this wiki for this repository's pull requests">
              Agent wiki:{" "}
              <select value={data.wiki_workspace_id} onChange={(e) => { setPick(e.target.value); void put("/github/repo-wiki", { repo: p.repo, workspace_id: e.target.value }).then(() => { setData({ ...data, wiki_workspace_id: e.target.value }); onReady(e.target.value, data.doc_path, `#${p.number} ${p.title}`); }); }} data-testid="pr-wiki-select">
                {available.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
              </select>
            </span>
          )}
          <button className="btn small primary" disabled={!docReady} onClick={() => setSheet(wholeDocument())} title={docReady ? "Ask the agent about this pull request" : "Choose the agent's wiki first"} data-testid="pr-ask">Ask about this PR</button>
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
                {review.conversation.map((c) => (
                  <div key={`${c.kind}${c.id}`} className={`pr-conv-item ${c.kind} ${(c.state ?? "").toLowerCase()}`} data-testid="pr-conv-item" data-kind={c.kind}>
                    <div className="small">
                      <b>{c.author}</b>{" "}
                      {c.kind === "review" ? <span className={`pr-review-state ${(c.state ?? "").toLowerCase()}`}>{REVIEW_LABEL[c.state ?? ""] ?? c.state?.toLowerCase()}</span> : <span className="muted">commented</span>}
                      {c.kind === "review" && c.line_comments ? <span className="muted"> · {c.line_comments} line comment{c.line_comments > 1 ? "s" : ""} (in Files)</span> : null}
                      <span className="muted"> · {c.created_at ? new Date(c.created_at).toLocaleString() : ""}</span>
                      {c.url && <> · <a href={c.url} target="_blank" rel="noreferrer">GitHub</a></>}
                    </div>
                    {c.body.trim() && <div className="markdown-body gh-comment-body" dangerouslySetInnerHTML={{ __html: md(c.body) }} />}
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
            {review.drafts.length > 0 && <> · <b>{review.drafts.length} pending</b></>}
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
            <SelectionPopover anchor={sheet} style={{}} sessions={sessions} defaultSessionId={defaultSessionId} defaultProvider={defaultProvider} className="sheet"
              onSend={async (q, sid) => { await onSend(sheet, q, sid); setSheet(null); }} onMark={() => setSheet(null)} onReanchor={null} onClose={() => setSheet(null)} />
          </div>
        </div>
      )}
      {wikiName && <div className="muted small pr-foot">The agent answers from the <b>{wikiName}</b> wiki. Discussions about this PR are in the panel on the right.</div>}
    </div>
  );
}
