import { useEffect, useMemo, useRef, useState } from "react";
import { AgentSession, ApiError, WorkspaceInfo, get, put } from "../api";
import { renderMarkdown, sanitizeHtml } from "../markdown";
import { SelectionAnchor, wholeDocument } from "../textmodel";
import DiffView from "./DiffView";
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
  files: { path: string; previous_path: string | null; status: string; additions: number; deletions: number; unified: string; binary_or_large: boolean }[];
  wiki_workspace_id: string | null;
  doc_path: string;
}

const FOLD_LINES = 400; // longer diffs start folded

const STATE_CLASS: Record<string, string> = { SUCCESS: "success", NEUTRAL: "success", SKIPPED: "none", FAILURE: "failure", ERROR: "failure", TIMED_OUT: "failure", CANCELLED: "failure", ACTION_REQUIRED: "pending", PENDING: "pending", IN_PROGRESS: "pending", QUEUED: "pending", EXPECTED: "pending" };
const REVIEW_LABEL: Record<string, string> = { APPROVED: "approved", CHANGES_REQUESTED: "requested changes", COMMENTED: "commented", DISMISSED: "dismissed", PENDING: "pending" };

/**
 * A pull request as a tab: Overview (description, checks, reviews) and Files (diff). Questions go to the agent in
 * the repository's wiki (chosen once per repository): about the whole PR, or about lines selected in the diff.
 */
export default function PrView({ repo, number, workspaces, docReady, onReady, onSend, sessions, defaultSessionId, defaultProvider }: {
  repo: string;
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
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const [sheet, setSheet] = useState<SelectionAnchor | null>(null);
  const [chip, setChip] = useState<{ x: number; y: number; quote: string; path: string } | null>(null);
  const [pick, setPick] = useState("");
  const filesBox = useRef<HTMLDivElement>(null);
  const [owner, name] = repo.split("/");

  const load = async (refresh = false) => {
    try {
      const d = await get<PrData>(`/github/pr/${owner}/${name}/${number}${refresh ? "?refresh=true" : ""}`);
      setData(d);
      setError(null);
      setFolded(new Set(d.files.filter((f) => f.unified.split("\n").length > FOLD_LINES).map((f) => f.path)));
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

  // lines selected in a file's diff → "Ask about selection"
  const onDiffMouseUp = (e: React.MouseEvent) => {
    window.setTimeout(() => {
      const sel = window.getSelection();
      const text = sel?.toString() ?? "";
      const node = sel?.anchorNode ? (sel.anchorNode.nodeType === 1 ? (sel.anchorNode as Element) : sel.anchorNode.parentElement) : null;
      const file = node?.closest<HTMLElement>("[data-file]");
      if (!text.trim() || !file || !filesBox.current) return setChip(null);
      const r = filesBox.current.getBoundingClientRect();
      setChip({ x: e.clientX - r.left + filesBox.current.scrollLeft, y: e.clientY - r.top + filesBox.current.scrollTop + 12, quote: text.replace(/\n+$/, ""), path: file.dataset.file! });
    }, 10);
  };

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
      {error && <div className="banner warn">{error}</div>}
      {tab === "overview" ? (
        <div className="pr-body" data-testid="pr-overview">
          <div className="pr-overview-grid">
            <article className="markdown-body pr-description" dangerouslySetInnerHTML={{ __html: body }} data-testid="pr-description" />
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
        <div className="pr-body pr-files" ref={filesBox} onMouseUp={onDiffMouseUp} data-testid="pr-files">
          <div className="muted small pad">Select lines in a diff to ask the agent about them.</div>
          {data.files.map((f) => {
            const isFolded = folded.has(f.path);
            return (
              <section key={f.path} className="pr-file" data-file={f.path} data-testid="pr-file">
                <button className="pr-file-head" onClick={() => setFolded((s) => { const n = new Set(s); if (n.has(f.path)) n.delete(f.path); else n.add(f.path); return n; })} aria-expanded={!isFolded}>
                  <span className="chev">{isFolded ? "›" : "⌄"}</span>
                  <span className="pr-file-path">{f.previous_path && f.previous_path !== f.path ? `${f.previous_path} → ` : ""}{f.path}</span>
                  <span className="muted small">{f.status}</span>
                  <span className="spacer" />
                  <span className="small"><span className="add">+{f.additions}</span> <span className="del">−{f.deletions}</span></span>
                </button>
                {!isFolded && (f.binary_or_large ? <div className="muted small pad">No textual diff (binary or too large) — see it on GitHub.</div> : <DiffView unified={f.unified} />)}
              </section>
            );
          })}
          {chip && (
            <button className="btn small primary pdfjs-chip" style={{ left: chip.x, top: chip.y }} onMouseDown={(e) => e.preventDefault()} disabled={!docReady}
              onClick={() => { setSheet({ quote: chip.quote, prefix: "", suffix: "", start: 0, end: 0, heading: null, target: { kind: "text", page: null, unit: "page", label: chip.path } }); setChip(null); }}
              data-testid="pr-ask-selection">
              Ask about selection
            </button>
          )}
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
