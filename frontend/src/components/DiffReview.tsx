import RefreshButton from "./RefreshButton";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { COMPOSER_HINT, composerKeyDown } from "./keys";
import { AgentSession, ApiError, DiffComment, DiffFile, DiffResult, GitRefs, Review, ReviewComment, del, get, post, put } from "../api";
import { FileIcon } from "./FileIcon";
import CommitPanel from "./CommitPanel";
import MarkdownView from "./MarkdownView";
import { useJobStream } from "./DiscussionPanel";

export interface Line {
  kind: "add" | "del" | "ctx" | "hunk" | "file";
  text: string; // without the +/-/space marker
  raw: string;
  oldNo: number | null;
  newNo: number | null;
  hunkIndex: number;
}

export function parseUnified(unified: string): { lines: Line[]; hunks: string[] } {
  const lines: Line[] = [];
  const hunks: string[] = [];
  let oldNo = 0;
  let newNo = 0;
  let hunkIndex = -1;
  let cur: string[] = [];
  for (const raw of unified.split("\n")) {
    // "--- a/x" / "+++ b/x" are file headers only before the first hunk: inside a hunk, "----" is a deleted
    // "---" line (Markdown rule, front matter, "-- comment") and "+++" an added "++i" line
    if (hunkIndex === -1 && (raw.startsWith("+++") || raw.startsWith("---"))) {
      lines.push({ kind: "file", text: raw, raw, oldNo: null, newNo: null, hunkIndex });
      continue;
    }
    // "\ No newline at end of file" belongs to the line before it: not a line of either file
    if (raw.startsWith("\\")) continue;
    const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
    if (m) {
      if (cur.length) hunks.push(cur.join("\n"));
      cur = [raw];
      hunkIndex += 1;
      oldNo = Number(m[1]);
      newNo = Number(m[2]);
      lines.push({ kind: "hunk", text: raw, raw, oldNo: null, newNo: null, hunkIndex });
      continue;
    }
    if (raw === "" && lines.length && lines[lines.length - 1].kind !== "hunk") continue;
    cur.push(raw);
    if (raw.startsWith("+")) lines.push({ kind: "add", text: raw.slice(1), raw, oldNo: null, newNo: newNo++, hunkIndex });
    else if (raw.startsWith("-")) lines.push({ kind: "del", text: raw.slice(1), raw, oldNo: oldNo++, newNo: null, hunkIndex });
    else lines.push({ kind: "ctx", text: raw.slice(1), raw, oldNo: oldNo++, newNo: newNo++, hunkIndex });
  }
  if (cur.length) hunks.push(cur.join("\n"));
  return { lines, hunks };
}

/** Side-by-side rows: context lines share a row; a run of deletions is paired with the following run of
 *  additions (GitHub-style), unpaired lines get an empty cell on the other side. Each cell keeps its index into
 *  the unified `lines` array so range selection and comments stay identical between the two views. */
export interface SplitRow {
  left: number | null; // index into lines (del or ctx)
  right: number | null; // index into lines (add or ctx)
  hunk?: number; // index into lines for a hunk header row
}

/** Width of the line-number columns, as on GitHub: at least 50px, wider for long files (numbers never wrap). */
export function numColStyle(lines: Line[]): React.CSSProperties {
  let max = 0;
  for (const l of lines) max = Math.max(max, l.oldNo ?? 0, l.newNo ?? 0);
  return { ["--no-w" as string]: `max(50px, calc(${String(max).length}ch + 20px))` } as React.CSSProperties;
}

export const UnifiedCols = () => <colgroup><col className="no-col" /><col className="no-col" /><col /></colgroup>;
export const SplitCols = () => <colgroup><col className="no-col" /><col /><col className="no-col" /><col /></colgroup>;

export function buildSplitRows(lines: Line[]): SplitRow[] {
  const rows: SplitRow[] = [];
  let i = 0;
  while (i < lines.length) {
    const ln = lines[i];
    if (ln.kind === "file") {
      i++;
      continue;
    }
    if (ln.kind === "hunk") {
      rows.push({ left: null, right: null, hunk: i });
      i++;
      continue;
    }
    if (ln.kind === "ctx") {
      rows.push({ left: i, right: i });
      i++;
      continue;
    }
    const dels: number[] = [];
    const adds: number[] = [];
    while (i < lines.length && lines[i].kind === "del") dels.push(i++);
    while (i < lines.length && lines[i].kind === "add") adds.push(i++);
    const n = Math.max(dels.length, adds.length);
    for (let k = 0; k < n; k++) rows.push({ left: dels[k] ?? null, right: adds[k] ?? null });
  }
  return rows;
}

type Range = { file: string; from: number; to: number }; // indices into lines, inclusive, from <= to

export default function DiffReview({
  path,
  isDir,
  sessions,
  defaultProvider,
  onOpenDocument,
  onOpenThread,
  onClose,
  refreshKey,
  onNotify,
}: {
  path: string;
  isDir: boolean;
  sessions: AgentSession[];
  defaultProvider: string | null;
  onOpenDocument: (path: string) => void;
  onOpenThread: (docPath: string, threadId: string) => void;
  onClose: () => void;
  refreshKey: number;
  onNotify?: (msg: string) => void;
}) {
  const [base, setBase] = useState("HEAD");
  const [target, setTarget] = useState<string>("worktree");
  const [markdownOnly, setMarkdownOnly] = useState(false);
  const [layout, setLayout] = useState<"unified" | "split">(() => (localStorage.getItem("waw.diffLayout") as "unified" | "split") || "unified");
  const [result, setResult] = useState<DiffResult | null>(null);
  const [refs, setRefs] = useState<GitRefs | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [range, setRange] = useState<Range | null>(null); // current selection (may be mid-drag)
  const [composer, setComposer] = useState<Range | null>(null); // confirmed selection with the comment box open
  const [question, setQuestion] = useState("");
  const [sessionChoice, setSessionChoice] = useState<string>("__default__");
  const [busy, setBusy] = useState(false);
  const [posted, setPosted] = useState<{ thread_id: string; file: string } | null>(null);
  const [showCommit, setShowCommit] = useState(false);
  const [review, setReview] = useState<{ pending: Review; last: Review | null } | null>(null);
  const [reviewSummary, setReviewSummary] = useState("");
  const [showReviewTray, setShowReviewTray] = useState(false);
  const dragging = useRef(false);

  const loadReview = async () => {
    try {
      const params = new URLSearchParams({ base, target });
      setReview(await get<{ pending: Review; last: Review | null }>(`/reviews/current?${params}`));
    } catch {
      setReview(null);
    }
  };
  useEffect(() => {
    void loadReview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, target, refreshKey]);
  const lastReviewJob = useJobStream(review?.last?.status === "submitted" ? review.last.job_id ?? null : null, () => {
    void loadReview();
    void load();
  });

  useEffect(() => {
    localStorage.setItem("waw.diffLayout", layout);
  }, [layout]);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ path, base, markdown_only: String(markdownOnly) });
      if (target !== "worktree") params.set("target", target);
      setResult(await get<DiffResult>(`/diff?${params}`));
    } catch (e) {
      const ae = e as ApiError;
      setError(ae.code === "NOT_A_GIT_REPO" ? "This workspace is not a Git repository, so there is no committed state to diff against." : ae.message);
      setResult(null);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    if (composer) return; // never reload underneath an open comment box
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, base, target, markdownOnly, refreshKey]);
  useEffect(() => {
    get<GitRefs>("/refs").then(setRefs).catch(() => setRefs(null));
  }, [refreshKey]);
  useEffect(() => {
    const up = () => {
      if (!dragging.current) return;
      dragging.current = false;
      // finish a drag: open the composer on the selected range
      setRange((r) => {
        if (r) setComposer(r);
        return r;
      });
    };
    window.addEventListener("pointerup", up);
    return () => window.removeEventListener("pointerup", up);
  }, []);

  const parsed = useMemo(() => Object.fromEntries((result?.files ?? []).map((f) => [f.path, parseUnified(f.unified)])), [result]);

  /** Selection gestures: click = single line, shift+click = extend, press-and-drag over lines = range. */
  const startSelect = (file: string, index: number, shift: boolean) => {
    if (shift && range && range.file === file) {
      const r = { file, from: Math.min(range.from, index), to: Math.max(range.to, index) };
      setRange(r);
      setComposer(r);
      return;
    }
    dragging.current = true;
    setRange({ file, from: index, to: index });
    setComposer(null);
    setPosted(null);
  };
  const extendSelect = (file: string, index: number) => {
    if (!dragging.current || !range || range.file !== file) return;
    const anchor = range.from === range.to ? range.from : index > range.to ? range.from : range.to;
    setRange({ file, from: Math.min(anchor, index), to: Math.max(anchor, index) });
  };

  const selectedLines = (r: Range): Line[] => {
    const ls = parsed[r.file]?.lines ?? [];
    return ls.slice(r.from, r.to + 1).filter((l) => l.kind === "add" || l.kind === "del" || l.kind === "ctx");
  };

  const send = async (mode: "now" | "review" = "now") => {
    if (!composer || !question.trim()) return;
    const sel = selectedLines(composer);
    if (!sel.length) return;
    const kinds = new Set(sel.map((l) => l.kind));
    const side = kinds.size > 1 ? "mixed" : sel[0].kind === "add" ? "added" : sel[0].kind === "del" ? "deleted" : "context";
    const first = sel[0];
    const last = sel[sel.length - 1];
    setBusy(true);
    setError(null);
    try {
      if (mode === "review") {
        // GitHub-style: stash the comment in the pending review; nothing runs until "Submit review"
        await post("/reviews/comments", {
          path: composer.file, base, target, side,
          line_no: first.newNo ?? first.oldNo,
          line_end: sel.length > 1 ? last.newNo ?? last.oldNo : null,
          line_text: sel.map((l) => l.text).join("\n"),
          hunk: parsed[composer.file]?.hunks[first.hunkIndex] ?? "",
          body: question.trim(),
        });
        setQuestion("");
        setComposer(null);
        setRange(null);
        await loadReview();
        setShowReviewTray(true);
        return;
      }
      const r = await post<{ thread_id: string; document_id: string }>("/diff/comments", {
        path: composer.file,
        base,
        target,
        side,
        line_no: first.newNo ?? first.oldNo,
        line_end: sel.length > 1 ? last.newNo ?? last.oldNo : null,
        line_text: sel.map((l) => l.text).join("\n"),
        hunk: parsed[composer.file]?.hunks[first.hunkIndex] ?? "",
        question: question.trim(),
        session_id: sessionChoice === "__default__" ? null : sessionChoice,
      });
      // Stay in the diff: the comment appears inline under the line and the agent's answer streams there too.
      // The user can jump to the full discussion with the marker's "Open discussion" link.
      setPosted({ thread_id: r.thread_id, file: composer.file });
      setQuestion("");
      setComposer(null);
      setRange(null);
      await load();
      window.setTimeout(() => setPosted(null), 6000);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const toggle = (p: string) =>
    setCollapsed((s) => {
      const n = new Set(s);
      if (n.has(p)) n.delete(p);
      else n.add(p);
      return n;
    });

  // Files already marked viewed start collapsed (GitHub behaviour); the user can still expand them.
  const seenResult = useRef<DiffResult | null>(null);
  useEffect(() => {
    if (!result || result === seenResult.current) return;
    seenResult.current = result;
    setCollapsed((s) => {
      const n = new Set(s);
      for (const f of result.files) if (f.viewed) n.add(f.path);
      return n;
    });
  }, [result]);

  const setViewed = async (f: DiffFile, viewed: boolean) => {
    try {
      await put("/diff/viewed", { path: f.path, base, viewed, target_sha256: f.target_sha256 });
      setResult((r) => {
        if (!r) return r;
        const files = r.files.map((x) => (x.path === f.path ? { ...x, viewed, viewed_stale: false } : x));
        return { ...r, files, viewed_count: files.filter((x) => x.viewed).length };
      });
      setCollapsed((s) => {
        const n = new Set(s);
        if (viewed) n.add(f.path);
        else n.delete(f.path);
        return n;
      });
    } catch (e) {
      setError((e as ApiError).message);
    }
  };

  const composerNode = (file: DiffFile) => {
    if (!composer || composer.file !== file.path) return null;
    const sel = selectedLines(composer);
    const first = sel[0];
    const last = sel[sel.length - 1];
    const a = first?.newNo ?? first?.oldNo;
    const b = last?.newNo ?? last?.oldNo;
    return (
      <div className="diff-composer" data-testid="diff-composer">
        <div className="label">
          Comment on {sel.length > 1 ? `${sel.length} lines (${a}–${b})` : `line ${a}`}:
          <code className="diff-sel-preview">{sel.map((l) => l.text).join("\n").slice(0, 160)}{sel.map((l) => l.text).join("\n").length > 160 ? "…" : ""}</code>
        </div>
        <textarea autoFocus rows={3} value={question} onChange={(e) => setQuestion(e.target.value)} placeholder={`Your comment or question about this change (${COMPOSER_HINT})`} data-testid="diff-comment-input"
          onKeyDown={(e) => composerKeyDown(e, () => void send("review"), setQuestion, () => { setComposer(null); setRange(null); })} />
        <div className="row">
          <select value={sessionChoice} onChange={(e) => setSessionChoice(e.target.value)}>
            <option value="__default__">{defaultProvider ? `Document session (${defaultProvider})` : "No agent provider configured"}</option>
            <option value="new">New session</option>
            {sessions.filter((s) => s.origin !== "preview").map((s) => (
              <option key={s.id} value={s.id}>{s.provider} · {s.display_name ?? s.short_id ?? s.id.slice(0, 8)}</option>
            ))}
          </select>
          <button className="btn primary small" disabled={busy || !question.trim()} onClick={() => void send("review")} data-testid="diff-comment-stash" title="Stash this comment in the pending review; submit all comments at once later">Add to review</button>
          <button className="btn small" disabled={busy || !question.trim()} onClick={() => void send("now")} data-testid="diff-comment-send" title="Start a discussion for this comment right away">Ask now</button>
          <button className="btn small" onClick={() => { setComposer(null); setRange(null); }}>Cancel</button>
        </div>
        {!file.commentable && <div className="warn small">Comments can only be attached to files the reader can open (Markdown, text/source, .docx).</div>}
      </div>
    );
  };

  return (
    <div className="diff-review" data-testid="diff-review">
      <div className="doc-header">
        <div className="crumbs">
          <span className="chip">DIFF</span> {path || "(whole workspace)"} {isDir ? "/" : ""}
        </div>
        <div className="doc-meta">
          <label className="small">
            base{" "}
            <select value={base} onChange={(e) => setBase(e.target.value)} data-testid="diff-base">
              <option value="HEAD">HEAD (last commit)</option>
              {refs?.branches.map((b) => (
                <option key={b} value={b}>{b}</option>
              ))}
              {refs?.commits.map((c) => (
                <option key={c.sha} value={c.sha}>{c.short} · {c.subject.slice(0, 50)}</option>
              ))}
            </select>
          </label>
          <label className="small">
            target{" "}
            <select value={target} onChange={(e) => setTarget(e.target.value)} data-testid="diff-target">
              <option value="worktree">working tree (uncommitted)</option>
              <option value="HEAD">HEAD</option>
              {refs?.commits.map((c) => (
                <option key={c.sha} value={c.sha}>{c.short} · {c.subject.slice(0, 50)}</option>
              ))}
            </select>
          </label>
          <span className="mode-switch" role="tablist" aria-label="Diff layout">
            <button role="tab" aria-selected={layout === "unified"} className={layout === "unified" ? "active" : ""} onClick={() => setLayout("unified")} data-testid="diff-layout-unified" title="Unified view">Unified</button>
            <button role="tab" aria-selected={layout === "split"} className={layout === "split" ? "active" : ""} onClick={() => setLayout("split")} data-testid="diff-layout-split" title="Side-by-side view">Side by side</button>
          </span>
          <label className="small" title="Hide changes in non-Markdown files"><input type="checkbox" checked={markdownOnly} onChange={(e) => setMarkdownOnly(e.target.checked)} /> Markdown only</label>
          <button className={`btn small toggle ${showReviewTray ? "on" : ""}`} onClick={() => setShowReviewTray((v) => !v)} data-testid="review-toggle" aria-pressed={showReviewTray} title="Pending review comments; submit them together">
            Review{review?.pending.comments.length ? ` (${review.pending.comments.length})` : ""}
          </button>
          <button className={`btn small toggle ${showCommit ? "on" : ""}`} onClick={() => setShowCommit((v) => !v)} title="Select changed files, draft a commit message with the agent, commit, push, open a pull request" data-testid="commit-toggle" aria-pressed={showCommit}>Commit…</button>
          <RefreshButton className="btn small" onRefresh={load} title="Reload the diff" testId="diff-reload" />
          <button className="icon-btn" onClick={onClose} title="Close diff review">×</button>
        </div>
      </div>
      {error && <div className="banner warn">{error}</div>}
      {showReviewTray && review && (
        <ReviewTray
          review={review.pending}
          last={review.last}
          lastJob={lastReviewJob}
          summary={reviewSummary}
          onSummary={setReviewSummary}
          sessions={sessions}
          onChanged={async () => { await loadReview(); await load(); }}
          onOpenThread={(p, tid) => onOpenThread(p, tid)}
          onError={setError}
        />
      )}
      {showCommit && (
        <CommitPanel
          refreshKey={refreshKey + (result?.files.length ?? 0)}
          preselect={result ? result.files.map((f) => f.path) : []}
          onDone={() => void load()}
          onNotify={(m) => onNotify?.(m)}
        />
      )}
      {result && (
        <div className="diff-summary muted small">
          {result.summary.files} file{result.summary.files === 1 ? "" : "s"} · <span className="add">+{result.summary.additions}</span> <span className="del">−{result.summary.deletions}</span>
          {result.files.length > 0 && <span data-testid="diff-viewed-count"> · {result.viewed_count ?? 0} / {result.files.length} viewed</span>}
          {result.files.length > 1 && (
            <>
              {" · "}
              <button className="link-btn" onClick={() => setCollapsed(new Set(result.files.map((f) => f.path)))}>collapse all</button>
              {" · "}
              <button className="link-btn" onClick={() => setCollapsed(new Set())}>expand all</button>
            </>
          )}
          {" · "}click a line to comment, drag or shift-click to select several lines
        </div>
      )}
      <div className="diff-body" onPointerLeave={() => { if (dragging.current) { dragging.current = false; setRange((r) => { if (r) setComposer(r); return r; }); } }}>
        {loading && !result && <div className="muted pad">Loading…</div>}
        {result && result.files.length === 0 && <div className="muted pad">No differences between {base} and {target === "worktree" ? "the working tree" : target}{isDir ? ` under ${path || "the workspace"}` : ""}.</div>}
        {result?.files.map((f) => (
          <FileDiff
            key={f.path}
            file={f}
            lines={parsed[f.path].lines}
            comments={result.comments[f.path] ?? []}
            pending={(review?.pending.comments ?? []).filter((c) => c.path === f.path)}
            lastReview={(review?.last?.comments ?? []).filter((c) => c.path === f.path)}
            collapsed={collapsed.has(f.path)}
            layout={layout}
            range={range?.file === f.path ? range : null}
            composerAfter={composer?.file === f.path ? composer.to : null}
            composerNode={composerNode(f)}
            onToggle={() => toggle(f.path)}
            onOpen={() => onOpenDocument(f.path)}
            onOpenThread={(tid) => onOpenThread(f.path, tid)}
            onStart={(i, shift) => f.commentable && startSelect(f.path, i, shift)}
            onExtend={(i) => extendSelect(f.path, i)}
            onViewed={(v) => void setViewed(f, v)}
            onReload={() => void load()}
          />
        ))}
      </div>
      {posted && (
        <div className="toast" data-testid="diff-posted-toast">
          Comment posted; the agent is answering below the line.{" "}
          <button className="link-btn toast-link" onClick={() => onOpenThread(posted.file, posted.thread_id)} data-testid="diff-open-discussion">Open discussion</button>
        </div>
      )}
    </div>
  );
}

function commentKey(side: string, text: string) {
  return `${side}:${text.trim()}`;
}

function FileDiff({ file, lines, comments, pending, lastReview, collapsed, layout, range, composerAfter, composerNode, onToggle, onOpen, onOpenThread, onStart, onExtend, onViewed, onReload }: {
  file: DiffFile;
  lines: Line[];
  comments: DiffComment[];
  pending: ReviewComment[];
  lastReview: ReviewComment[];
  collapsed: boolean;
  layout: "unified" | "split";
  range: Range | null;
  composerAfter: number | null;
  composerNode: React.ReactNode;
  onToggle: () => void;
  onOpen: () => void;
  onOpenThread: (threadId: string) => void;
  onStart: (index: number, shift: boolean) => void;
  onExtend: (index: number) => void;
  onViewed: (viewed: boolean) => void;
  onReload: () => void;
}) {
  // comments are keyed by the first selected line's side+text; range comments also know their line span
  const byLine = useMemo(() => {
    const m = new Map<string, DiffComment[]>();
    for (const c of comments) {
      const firstLine = (c.diff.line_text ?? "").split("\n")[0];
      const side = c.diff.side === "mixed" ? "mixed" : c.diff.side;
      m.set(commentKey(side, firstLine), [...(m.get(commentKey(side, firstLine)) ?? []), c]);
    }
    return m;
  }, [comments]);
  const commentsFor = (ln: Line): DiffComment[] => {
    const side = ln.kind === "add" ? "added" : ln.kind === "del" ? "deleted" : "context";
    return [...(byLine.get(commentKey(side, ln.text)) ?? []), ...(byLine.get(commentKey("mixed", ln.text)) ?? [])];
  };
  const pendingFor = (ln: Line): ReviewComment[] => {
    const side = ln.kind === "add" ? "added" : ln.kind === "del" ? "deleted" : "context";
    return pending.filter((c) => (c.side === side || c.side === "mixed") && (c.line_text.split("\n")[0] ?? "").trim() === ln.text.trim());
  };
  const decisionFor = (threadId: string): ReviewComment | undefined => lastReview.find((c) => c.thread_id === threadId);
  const inRange = (i: number) => !!range && i >= range.from && i <= range.to;
  const rangeLen = range ? range.to - range.from + 1 : 0;
  const splitRows = useMemo(() => (layout === "split" ? buildSplitRows(lines) : []), [layout, lines]);

  const commentRows = (cs: DiffComment[], ps: ReviewComment[], colSpan: number) => [
    ...ps.map((c) => (
      <tr key={"p" + c.id} className="diff-comment-row">
        <td colSpan={colSpan}>
          <div className="diff-pending" data-testid="diff-pending-comment">
            <span className="chip pending">pending review</span> {c.body}
          </div>
        </td>
      </tr>
    )),
    ...cs.map((c) => (
      <tr key={c.thread_id} className="diff-comment-row">
        <td colSpan={colSpan}>
          <InlineThread comment={c} decision={decisionFor(c.thread_id)} onOpen={() => onOpenThread(c.thread_id)} onChanged={onReload} />
        </td>
      </tr>
    )),
  ];

  const cellProps = (i: number) => ({
    onPointerDown: (e: React.PointerEvent) => {
      if (e.button !== 0) return;
      e.preventDefault();
      onStart(i, e.shiftKey);
    },
    onPointerEnter: () => onExtend(i),
  });

  return (
    <section className={`diff-file ${collapsed ? "collapsed" : ""} ${file.viewed ? "viewed" : ""} ${file.viewed_stale ? "viewed-stale" : ""}`} data-testid="diff-file" data-path={file.path} data-viewed={file.viewed ? "1" : "0"}>
      <header className="diff-file-head" onClick={onToggle}>
        <span className="chev">{collapsed ? "›" : "⌄"}</span>
        <FileIcon item={{ type: "file", kind: file.is_markdown ? "markdown" : file.commentable ? "code" : "file" }} />
        <span className="diff-path" title={file.path}>{file.path}</span>
        <span className={`chip git ${file.status}`}>{file.status}</span>
        {file.lock && <span className="chip locked" title={`Locked${file.lock.reason ? ` — ${file.lock.reason}` : ""}`}>🔒</span>}
        <span className="muted small"><span className="add">+{file.additions}</span> <span className="del">−{file.deletions}</span></span>
        {comments.length > 0 && <span className="chip" title="Comments on this file's diff">{comments.length} comment{comments.length > 1 ? "s" : ""}</span>}
        <span className="spacer" />
        {file.commentable && file.status !== "deleted" && (
          <button className="btn small" onClick={(e) => { e.stopPropagation(); onOpen(); }} title="Open in the reader">Open</button>
        )}
        <label className={`viewed-box ${file.viewed ? "on" : ""}`} onClick={(e) => e.stopPropagation()} title={file.viewed_stale ? "You marked this file as viewed, but it changed since — review again" : "Mark this file's changes as viewed (clears automatically if the file changes)"}>
          <input type="checkbox" checked={!!file.viewed} onChange={(e) => onViewed(e.target.checked)} data-testid="diff-viewed" />
          <span>Viewed{file.viewed_stale ? " · changed since" : ""}</span>
        </label>
      </header>
      {!collapsed && layout === "unified" && (
        <table className="diff-table" data-testid="diff-table-unified" style={numColStyle(lines)}>
          <UnifiedCols />
          <tbody>
            {lines.map((ln, i) => {
              if (ln.kind === "file") return null;
              if (ln.kind === "hunk") {
                return (
                  <tr key={i} className="diff-line hunk">
                    <td className="no" colSpan={2} />
                    <td className="code">{ln.raw}</td>
                  </tr>
                );
              }
              const cs = commentsFor(ln);
              const ps = pendingFor(ln);
              return (
                <Fragment key={i}>
                  <tr className={`diff-line ${ln.kind} ${inRange(i) ? "selected" : ""} ${cs.length || ps.length ? "has-comments" : ""}`} {...cellProps(i)} title={file.commentable ? "Click to comment; drag or shift-click to select several lines" : undefined} data-testid="diff-row" data-selected={inRange(i) ? "1" : "0"}>
                    <td className="no">{ln.oldNo ?? ""}</td>
                    <td className="no">{ln.newNo ?? ""}</td>
                    <td className="code">
                      <span className="marker">{ln.kind === "add" ? "+" : ln.kind === "del" ? "-" : " "}</span>
                      {ln.text || " "}
                    </td>
                  </tr>
                  {commentRows(cs, ps, 3)}
                  {composerAfter === i ? (
                    <tr className="diff-composer-row">
                      <td colSpan={3}>{composerNode}</td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
      {!collapsed && layout === "split" && (
        <table className="diff-table split" data-testid="diff-table-split" style={numColStyle(lines)}>
          <SplitCols />
          <tbody>
            {splitRows.map((row, ri) => {
              if (row.hunk !== undefined) {
                return (
                  <tr key={ri} className="diff-line hunk">
                    <td className="no" />
                    <td className="code" colSpan={3}>{lines[row.hunk].raw}</td>
                  </tr>
                );
              }
              const L = row.left !== null ? lines[row.left] : null;
              const R = row.right !== null ? lines[row.right] : null;
              const lastIdx = Math.max(row.left ?? -1, row.right ?? -1);
              const cs = [...(L ? commentsFor(L) : []), ...(R && R !== L ? commentsFor(R) : [])];
              const ps = [...(L ? pendingFor(L) : []), ...(R && R !== L ? pendingFor(R) : [])];
              return (
                <Fragment key={ri}>
                  <tr className="diff-line split-row" data-testid="diff-row">
                    <td className={`no ${L ? L.kind : "empty"} ${L && inRange(row.left!) ? "selected" : ""}`}>{L?.oldNo ?? ""}</td>
                    <td className={`code left ${L ? L.kind : "empty"} ${L && inRange(row.left!) ? "selected" : ""}`} {...(L ? cellProps(row.left!) : {})} data-selected={L && inRange(row.left!) ? "1" : "0"}>
                      {L ? <><span className="marker">{L.kind === "del" ? "-" : " "}</span>{L.text || " "}</> : ""}
                    </td>
                    <td className={`no ${R ? R.kind : "empty"} ${R && inRange(row.right!) ? "selected" : ""}`}>{R?.newNo ?? ""}</td>
                    <td className={`code right ${R ? R.kind : "empty"} ${R && inRange(row.right!) ? "selected" : ""}`} {...(R ? cellProps(row.right!) : {})} data-selected={R && inRange(row.right!) ? "1" : "0"}>
                      {R ? <><span className="marker">{R.kind === "add" ? "+" : " "}</span>{R.text || " "}</> : ""}
                    </td>
                  </tr>
                  {(cs.length > 0 || ps.length > 0) && commentRows(cs, ps, 4)}
                  {composerAfter !== null && composerAfter === lastIdx ? (
                    <tr className="diff-composer-row">
                      <td colSpan={4}>{composerNode}</td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
      {!collapsed && rangeLen > 1 && <div className="muted small pad">{rangeLen} lines selected</div>}
    </section>
  );
}


/** A diff comment shown in place: the question, the agent's answer (streamed while running) and a reply box, so a
 *  review never has to leave the diff. "Open discussion" jumps to the full thread in the document. */
function InlineThread({ comment, decision, onOpen, onChanged }: { comment: DiffComment; decision?: ReviewComment; onOpen: () => void; onChanged: () => void }) {
  const msgs = comment.messages ?? [];
  const running = msgs.find((m) => m.role === "agent" && ["queued", "running", "waiting_provider_permission"].includes(m.status) && m.job_id);
  // reload the diff once per finished job (the stream re-subscribes when `comment` is replaced; guard against loops)
  const finishedJobs = useRef<Set<string>>(new Set());
  const live = useJobStream(running?.job_id ?? null, (job) => {
    if (finishedJobs.current.has(job.jobId)) return;
    finishedJobs.current.add(job.jobId);
    onChanged();
  });
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState(true);
  const send = async () => {
    if (!reply.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      await post(`/threads/${comment.thread_id}/messages`, { text: reply.trim() });
      setReply("");
      onChanged();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const range = comment.diff.line_end && comment.diff.line_no && comment.diff.line_end !== comment.diff.line_no ? ` · lines ${comment.diff.line_no}–${comment.diff.line_end}` : "";
  return (
    <div className="diff-thread" data-testid="diff-comment-marker">
      <div className="diff-thread-head">
        <button className="link-btn" onClick={() => setOpen((o) => !o)} title={open ? "Collapse" : "Expand"}>{open ? "▾" : "▸"}</button>
        <span className="muted small">{msgs.length} message{msgs.length === 1 ? "" : "s"}{range}</span>
        {decision?.decision && <span className={`chip decision ${decision.decision}`} data-testid="review-decision" title={decision.response ?? ""}>{{ address: "addressed", reply: "replied", skip: "skipped" }[decision.decision]}</span>}
        <span className="spacer" />
        <button className="link-btn small" onClick={onOpen} data-testid="diff-open-thread">Open discussion ›</button>
      </div>
      {open && (
        <>
          {msgs.map((m) => {
            const isLive = live && m.job_id === live.jobId;
            const status = isLive ? live.status : m.status;
            const content = isLive && live.text ? live.text : m.content;
            return (
              <div key={m.id} className={`diff-msg ${m.role} ${status}`} data-testid={`diff-msg-${m.role}`} data-status={status}>
                <span className="who">{m.role === "user" ? "You" : "Agent"}</span>
                {m.role === "agent" ? (
                  content ? <MarkdownView text={content} className="small" /> : <span className="muted small">{status === "failed" ? "failed" : "thinking…"}</span>
                ) : (
                  <span className="diff-msg-text">{content}</span>
                )}
                {m.role === "agent" && status !== "completed" && <span className={`chip status ${status}`}>{status}</span>}
              </div>
            );
          })}
          <div className="diff-reply">
            <input value={reply} onChange={(e) => setReply(e.target.value)} placeholder="Reply… (Enter to send)" disabled={busy} onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing) void send(); }} data-testid="diff-reply-input" />
            <button className="btn small" disabled={busy || !reply.trim()} onClick={() => void send()}>Reply</button>
          </div>
          {err && <div className="error-text small">{err}</div>}
        </>
      )}
    </div>
  );
}

/** The pending review: stashed comments (editable/deletable), an overall note, and Submit. Shows the last submitted
 *  review's progress and the agent's per-comment decisions. */
function ReviewTray({ review, last, lastJob, summary, onSummary, sessions, onChanged, onOpenThread, onError }: {
  review: Review;
  last: Review | null;
  lastJob: ReturnType<typeof useJobStream>;
  summary: string;
  onSummary: (s: string) => void;
  sessions: AgentSession[];
  onChanged: () => Promise<void>;
  onOpenThread: (path: string, threadId: string) => void;
  onError: (e: string | null) => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [sessionChoice, setSessionChoice] = useState<string>("__default__");
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    onError(null);
    try {
      await fn();
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const submit = () =>
    run(async () => {
      if (!review.id) return;
      await post(`/reviews/${review.id}/submit`, { summary: summary.trim() || null, session_id: sessionChoice === "__default__" ? null : sessionChoice });
      onSummary("");
      await onChanged();
    });
  const running = last && (last.status === "submitted" || (lastJob && !["completed", "failed", "cancelled"].includes(lastJob.status)));
  return (
    <div className="review-tray" data-testid="review-tray">
      <div className="label">
        Pending review · {review.comments.length} comment{review.comments.length === 1 ? "" : "s"}
        {review.id && review.comments.length > 0 && (
          <button className="link-btn small" disabled={busy} onClick={() => run(async () => { if (window.confirm("Discard all pending review comments?")) { await del(`/reviews/${review.id}`); await onChanged(); } })} data-testid="review-discard">discard</button>
        )}
      </div>
      {review.comments.length === 0 && <div className="muted small">Select lines in the diff and use <b>Add to review</b> to stash comments here, then submit them together. The agent will decide per comment whether to address it, reply, or skip.</div>}
      {review.comments.map((c) => (
        <div key={c.id} className="review-item" data-testid="review-item">
          <div className="review-item-head">
            <code>{c.path}</code>
            <span className="muted small">{c.line_end && c.line_end !== c.line_no ? `lines ${c.line_no}–${c.line_end}` : `line ${c.line_no ?? "?"}`} · {c.side}</span>
            <span className="spacer" />
            <button className="link-btn small" disabled={busy} onClick={() => { setEditing(c.id); setDraft(c.body); }}>edit</button>
            <button className="link-btn small" disabled={busy} onClick={() => run(async () => { await del(`/reviews/comments/${c.id}`); await onChanged(); })} data-testid="review-item-delete">delete</button>
          </div>
          <code className="review-item-line">{c.line_text.split("\n")[0].slice(0, 120)}</code>
          {editing === c.id ? (
            <div className="row">
              <textarea rows={2} value={draft} onChange={(e) => setDraft(e.target.value)} />
              <button className="btn small primary" disabled={busy || !draft.trim()} onClick={() => run(async () => { await put(`/reviews/comments/${c.id}`, { body: draft.trim() }); setEditing(null); await onChanged(); })}>Save</button>
              <button className="btn small" onClick={() => setEditing(null)}>Cancel</button>
            </div>
          ) : (
            <div className="review-item-body">{c.body}</div>
          )}
        </div>
      ))}
      {review.comments.length > 0 && (
        <div className="review-submit">
          <textarea rows={2} value={summary} onChange={(e) => onSummary(e.target.value)} placeholder="Overall note for the agent (optional)" data-testid="review-summary" />
          <div className="row">
            <select value={sessionChoice} onChange={(e) => setSessionChoice(e.target.value)}>
              <option value="__default__">Document sessions (default)</option>
              {sessions.filter((s) => s.origin !== "preview").map((s) => (
                <option key={s.id} value={s.id}>{s.provider} · {s.display_name ?? s.short_id ?? s.id.slice(0, 8)}</option>
              ))}
            </select>
            <button className="btn primary" disabled={busy || !!running} onClick={() => void submit()} data-testid="review-submit">
              Submit review ({review.comments.length})
            </button>
            <span className="muted small">One agent run handles all comments: it edits for the ones it addresses, replies to questions, skips the rest.</span>
          </div>
        </div>
      )}
      {last && (
        <div className={`review-last ${last.status}`} data-testid="review-last">
          <div className="label">
            Last review · {last.status === "submitted" ? (lastJob ? `running (${lastJob.status})` : "running") : last.status}
            {last.finished_at && <span className="muted small"> · {new Date(last.finished_at).toLocaleString()}</span>}
          </div>
          {last.status === "submitted" && lastJob?.activity.length ? <ul className="activity">{lastJob.activity.slice(-4).map((a, i) => <li key={i}>{a}</li>)}</ul> : null}
          {last.result?.summary && <div className="small">{String(last.result.summary)}</div>}
          {last.job_error && <div className="error-text small">{last.job_error}</div>}
          <ul className="review-decisions">
            {last.comments.map((c) => (
              <li key={c.id} data-testid="review-decision-item">
                <span className={`chip decision ${c.decision ?? "pending"}`}>{c.decision ? { address: "addressed", reply: "replied", skip: "skipped" }[c.decision] : "…"}</span>{" "}
                <code>{c.path}</code> <span className="muted small">line {c.line_no ?? "?"}</span> — {c.body.slice(0, 80)}
                {c.response && <div className="muted small review-response">{c.response}</div>}
                {c.thread_id && <button className="link-btn small" onClick={() => onOpenThread(c.path, c.thread_id!)}>open discussion ›</button>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}