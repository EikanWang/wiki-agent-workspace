import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, del, patch, post } from "../api";
import { renderGithubMarkdown } from "../markdown";
import { COMPOSER_HINT, composerKeyDown } from "./keys";
import MentionTextarea from "./MentionTextarea";
import GhUser from "./GhUser";
import { Line, SplitCols, UnifiedCols, buildSplitRows, numColStyle, parseUnified } from "./DiffReview";

/** A review thread on GitHub (line or whole-file), with all its comments. */
export interface GhThread {
  id: string; resolved: boolean; outdated: boolean; resolved_by: string | null; path: string; line: number | null; original_line: number | null;
  start_line: number | null; side: "LEFT" | "RIGHT"; start_side: string | null; subject: "LINE" | "FILE"; total: number; diff_hunk: string | null;
  comments: { id: number; author: string | null; body: string; created_at: string | null; url: string | null }[];
}
/** A comment written here, published with the next review. */
export interface GhDraft {
  id: string; path: string; subject: "line" | "file"; side: "LEFT" | "RIGHT"; line: number; start_side: string | null; start_line: number | null; line_text: string; body: string;
  /** agent: suggested by an agent review; published only once applied (decision accepted) */
  origin?: "user" | "agent"; decision?: "accepted" | "proposed" | "discarded"; thread_id?: string | null;
}
export interface PrFile { path: string; previous_path: string | null; status: string; additions: number; deletions: number; unified: string; binary_or_large: boolean; viewed: "VIEWED" | "UNVIEWED" | "DISMISSED" }

type Sel = { from: number; to: number }; // indices into the parsed lines, inclusive

const side = (ln: Line): "LEFT" | "RIGHT" => (ln.kind === "del" ? "LEFT" : "RIGHT");
const lineNo = (ln: Line): number => (ln.kind === "del" ? ln.oldNo! : ln.newNo!);
/** owner/name of the PR an API base (/github/pr/{owner}/{repo}/{n}) belongs to: `#123` links point there */
const repoOf = (apiBase: string) => apiBase.split("/").slice(3, 5).join("/");
const md = (s: string, apiBase: string) => renderGithubMarkdown(s, repoOf(apiBase));
const ago = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "");

/**
 * One changed file of a pull request, GitHub-style: unified or side-by-side; click a line (drag / shift-click for
 * several) to write a review comment (published later with the review) or to ask the agent about those lines;
 * existing GitHub threads are shown at their lines and can be replied to.
 */
export function PrFileDiff({ file, layout, threads, drafts, apiBase, canAsk, collapsedDefault, onChanged, onAsk, onError, onViewed }: {
  file: PrFile;
  layout: "unified" | "split";
  threads: GhThread[];
  onViewed: (viewed: boolean) => Promise<void>;
  drafts: GhDraft[];
  apiBase: string; // /github/pr/{owner}/{repo}/{n}
  canAsk: boolean;
  collapsedDefault: boolean;
  onChanged: () => Promise<void>;
  onAsk: (quote: string, label: string, question: string) => Promise<void>;
  onError: (m: string) => void;
}) {
  const [collapsed, setCollapsed] = useState(collapsedDefault || file.viewed === "VIEWED");
  useEffect(() => { if (file.viewed === "VIEWED") setCollapsed(true); }, [file.viewed]);
  const [fileComposer, setFileComposer] = useState(false);
  const [fileText, setFileText] = useState("");
  const { lines } = useMemo(() => parseUnified(file.unified), [file.unified]);
  const rows = useMemo(() => (layout === "split" ? buildSplitRows(lines) : []), [layout, lines]);
  const [sel, setSel] = useState<Sel | null>(null);
  const [composer, setComposer] = useState<Sel | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const anchor = useRef<number | null>(null);
  const dragging = useRef(false);

  useEffect(() => {
    const up = () => {
      if (!dragging.current) return;
      dragging.current = false;
      setSel((s) => {
        if (s) setComposer(s);
        return s;
      });
    };
    window.addEventListener("pointerup", up);
    return () => window.removeEventListener("pointerup", up);
  }, []);

  const commentable = (i: number) => lines[i] && ["add", "del", "ctx"].includes(lines[i].kind);
  const start = (i: number, shift: boolean) => {
    if (!commentable(i)) return;
    if (shift && sel) {
      const a = anchor.current ?? sel.from;
      const r = { from: Math.min(a, i), to: Math.max(a, i) };
      setSel(r);
      setComposer(r);
      return;
    }
    anchor.current = i;
    dragging.current = true;
    setSel({ from: i, to: i });
    setComposer(null);
  };
  const extend = (i: number) => {
    if (!dragging.current || anchor.current === null || !commentable(i)) return;
    setSel({ from: Math.min(anchor.current, i), to: Math.max(anchor.current, i) });
  };
  const inSel = (i: number) => !!sel && i >= sel.from && i <= sel.to;
  const cancel = () => { setComposer(null); setSel(null); setText(""); };

  const selected = composer ? lines.slice(composer.from, composer.to + 1).filter((l) => ["add", "del", "ctx"].includes(l.kind)) : [];
  const label = () => {
    if (!selected.length) return file.path;
    const a = selected[0], b = selected[selected.length - 1];
    const n = (l: Line) => `${side(l) === "LEFT" ? "L" : "R"}${lineNo(l)}`;
    return `${file.path} ${selected.length === 1 ? n(a) : `${n(a)}–${n(b)}`}`;
  };

  const askFile = async () => {
    if (!fileText.trim()) return;
    setBusy(true);
    try {
      await onAsk((file.unified || "").slice(0, 20000), `${file.path} (all of this file's changes)`, fileText.trim());
      setFileText("");
      setFileComposer(false);
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const addFileComment = async () => {
    if (!fileText.trim()) return;
    setBusy(true);
    try {
      await post(`${apiBase}/drafts`, { path: file.path, subject: "file", body: fileText });
      setFileText("");
      setFileComposer(false);
      await onChanged();
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  // GitHub's "suggested change": the selected new-side lines, to be edited into the replacement
  const suggestable = selected.length > 0 && selected.every((l) => l.kind !== "del");
  const insertSuggestion = () => {
    const block = "```suggestion\n" + selected.map((l) => l.text).join("\n") + "\n```";
    setText((t) => (t.trim() ? `${t.replace(/\s+$/, "")}\n\n${block}` : block));
  };
  const addComment = async () => {
    if (!text.trim() || !selected.length) return;
    const first = selected[0], last = selected[selected.length - 1];
    setBusy(true);
    try {
      await post(`${apiBase}/drafts`, {
        path: file.path, side: side(last), line: lineNo(last),
        start_side: selected.length > 1 ? side(first) : null, start_line: selected.length > 1 ? lineNo(first) : null,
        line_text: selected.map((l) => l.raw).join("\n"), body: text,
      });
      cancel();
      await onChanged();
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const ask = async () => {
    if (!text.trim() || !selected.length) return;
    setBusy(true);
    try {
      await onAsk(selected.map((l) => l.raw).join("\n"), label(), text.trim());
      cancel();
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  // GitHub threads and drafts, keyed by "SIDE:line" of the line they end on
  const lineThreads = threads.filter((t) => t.subject !== "FILE" && !t.outdated && t.line);
  const fileThreads = threads.filter((t) => t.subject === "FILE");
  const outdated = threads.filter((t) => t.subject !== "FILE" && (t.outdated || !t.line));
  const lineDrafts = drafts.filter((d) => d.subject !== "file");
  const fileDrafts = drafts.filter((d) => d.subject === "file");
  const at = (ln: Line) => {
    const k = `${side(ln)}:${lineNo(ln)}`;
    const alt = ln.kind === "ctx" ? `LEFT:${ln.oldNo}` : null; // context lines can carry LEFT comments too
    return {
      threads: lineThreads.filter((t) => `${t.side}:${t.line}` === k || `${t.side}:${t.line}` === alt),
      drafts: lineDrafts.filter((d) => `${d.side}:${d.line}` === k || `${d.side}:${d.line}` === alt),
    };
  };
  const total = threads.reduce((n, t) => n + t.comments.length, 0) + drafts.length;
  const unresolved = threads.filter((t) => !t.resolved).length;
  const threadProps = { apiBase, onChanged, onError, canAsk, onAsk };

  const composerNode = composer && (
    <div className="diff-composer" data-testid="pr-composer" onPointerDown={(e) => e.stopPropagation()}>
      <div className="muted small">{selected.length > 1 ? `Lines ${label().slice(file.path.length + 1)}` : `Line ${label().slice(file.path.length + 1)}`}</div>
      <MentionTextarea apiBase={apiBase} autoFocus rows={3} value={text} onValue={setText} placeholder={`Leave a review comment, or ask the agent about these lines (${COMPOSER_HINT})`}
        onKeyDown={(e) => composerKeyDown(e, () => void addComment(), setText, cancel)} data-testid="pr-composer-text" />
      <div className="row">
        <button className="btn small primary" disabled={busy || !text.trim()} onClick={() => void addComment()} title="Saved here; published to GitHub when you submit the review" data-testid="pr-add-comment">Add review comment</button>
        <button className="btn small" disabled={busy || !text.trim() || !canAsk} onClick={() => void ask()} title={canAsk ? "Ask the agent about these lines (the answer appears in the discussions panel)" : "Choose the agent's wiki first"} data-testid="pr-ask-lines">Ask agent</button>
        <button className="btn small" disabled={!suggestable} onClick={insertSuggestion} title={suggestable ? "Propose replacement code (GitHub 'suggested change')" : "Suggestions apply to new or unchanged lines"} data-testid="pr-suggest">Suggest change</button>
        <button className="btn small" onClick={cancel}>Cancel</button>
      </div>
    </div>
  );

  const extras = (ln: Line, colSpan: number) => {
    const { threads, drafts: ds } = at(ln);
    return [
      ...threads.map((t) => (
        <tr key={`t${t.id}`} className="diff-comment-row"><td colSpan={colSpan}><ThreadBox thread={t} {...threadProps} /></td></tr>
      )),
      ...ds.map((d) => (
        <tr key={`d${d.id}`} className="diff-comment-row"><td colSpan={colSpan}><DraftBox draft={d} apiBase={apiBase} onChanged={onChanged} onError={onError} canAsk={canAsk} onAsk={onAsk} /></td></tr>
      )),
    ];
  };
  /** Side by side, as on GitHub: a comment sits under its own half (old file left, new file right). */
  const splitExtras = (L: Line | null, R: Line | null, ri: number, composerHere: boolean) => {
    const items = (ln: Line | null, want: "LEFT" | "RIGHT") => {
      if (!ln) return [];
      const { threads, drafts: ds } = at(ln);
      const mine = (x: { side: string }) => x.side === want;
      return [
        ...threads.filter(mine).map((t) => <ThreadBox key={`t${t.id}`} thread={t} {...threadProps} />),
        ...ds.filter(mine).map((d) => <DraftBox key={`d${d.id}`} draft={d} apiBase={apiBase} onChanged={onChanged} onError={onError} canAsk={canAsk} onAsk={onAsk} />),
      ];
    };
    const left = items(L, "LEFT");
    const right = items(R, "RIGHT");
    const cl = composerHere && composer ? (lines[composer.to].kind === "del" ? "left" : "right") : null;
    if (!left.length && !right.length && !cl) return null;
    return (
      <tr key={`x${ri}`} className="diff-comment-row split">
        <td colSpan={2} className="side-left">{left}{cl === "left" && composerNode}</td>
        <td colSpan={2} className="side-right">{right}{cl === "right" && composerNode}</td>
      </tr>
    );
  };
  const cell = (i: number) => ({
    onPointerDown: (e: React.PointerEvent) => { if (e.button !== 0) return; e.preventDefault(); start(i, e.shiftKey); },
    onPointerEnter: () => extend(i),
  });
  const has = (ln: Line) => { const x = at(ln); return x.threads.length + x.drafts.length > 0; };

  return (
    <section className={`diff-file ${collapsed ? "collapsed" : ""}`} data-testid="pr-file" data-file={file.path}>
      <header className="diff-file-head" onClick={() => setCollapsed((c) => !c)}>
        <span className="chev">{collapsed ? "›" : "⌄"}</span>
        <span className="diff-path" title={file.path}>{file.previous_path && file.previous_path !== file.path ? `${file.previous_path} → ` : ""}{file.path}</span>
        <span className={`chip git ${file.status}`}>{file.status}</span>
        <span className="muted small"><span className="add">+{file.additions}</span> <span className="del">−{file.deletions}</span></span>
        {total > 0 && <span className="chip" data-testid="pr-file-comments" title={`${unresolved} unresolved thread(s)`}>{total} comment{total > 1 ? "s" : ""}</span>}
        {file.viewed === "DISMISSED" && <span className="chip pending" title="You marked this file viewed, but it changed since">changed since viewed</span>}
        <span className="spacer" />
        {canAsk && !file.binary_or_large && (
          <button className="btn small" onClick={(e) => { e.stopPropagation(); setCollapsed(false); setFileComposer(true); }} title="Ask the agent about this file's changes (or comment on the whole file)" data-testid="pr-file-ask">Ask agent</button>
        )}
        <button className="btn small" onClick={(e) => { e.stopPropagation(); setCollapsed(false); setFileComposer(true); }} title="Comment on the whole file" data-testid="pr-file-comment">Comment on file</button>
        <label className={`viewed-box ${file.viewed === "VIEWED" ? "on" : ""}`} onClick={(e) => e.stopPropagation()} title="Mark as viewed (also on GitHub); viewed files fold">
          <input type="checkbox" checked={file.viewed === "VIEWED"} onChange={(e) => { const v = e.target.checked; if (v) setCollapsed(true); void onViewed(v); }} data-testid="pr-viewed" />
          <span>Viewed</span>
        </label>
      </header>
      {!collapsed && (fileThreads.length > 0 || fileDrafts.length > 0 || fileComposer) && (
        <div className="pr-file-level pad" data-testid="pr-file-level">
          {fileThreads.map((t) => <ThreadBox key={t.id} thread={t} {...threadProps} />)}
          {fileDrafts.map((d) => <DraftBox key={d.id} draft={d} apiBase={apiBase} onChanged={onChanged} onError={onError} canAsk={canAsk} onAsk={onAsk} />)}
          {fileComposer && (
            <div className="diff-composer" data-testid="pr-file-composer">
              <div className="muted small">The whole file: comment on it (published with your review), or ask the agent about its changes</div>
              <MentionTextarea apiBase={apiBase} autoFocus rows={3} value={fileText} onValue={setFileText} placeholder={`Comment on ${file.path} (${COMPOSER_HINT})`}
                onKeyDown={(e) => composerKeyDown(e, () => void addFileComment(), setFileText, () => setFileComposer(false))} data-testid="pr-file-composer-text" />
              <div className="row">
                <button className="btn small primary" disabled={busy || !fileText.trim()} onClick={() => void addFileComment()} data-testid="pr-file-composer-add">Add review comment</button>
                <button className="btn small" disabled={busy || !fileText.trim() || !canAsk} onClick={() => void askFile()} title={canAsk ? "Ask the agent about this file's changes; the answer is in the discussions panel" : "Choose the agent's wiki first"} data-testid="pr-file-composer-ask">Ask agent</button>
                <button className="btn small" onClick={() => setFileComposer(false)}>Cancel</button>
              </div>
            </div>
          )}
        </div>
      )}
      {!collapsed && file.binary_or_large && <div className="muted small pad">No textual diff (binary or too large) — see it on GitHub.</div>}
      {!collapsed && outdated.length > 0 && (
        <details className="pr-outdated pad">
          <summary className="muted small">{outdated.length} comment thread{outdated.length > 1 ? "s" : ""} on code that changed since (outdated)</summary>
          {outdated.map((t) => <ThreadBox key={t.id} thread={t} {...threadProps} />)}
        </details>
      )}
      {!collapsed && !file.binary_or_large && layout === "unified" && (
        <table className="diff-table" data-testid="pr-diff-unified" style={numColStyle(lines)}>
          <UnifiedCols />
          <tbody>
            {lines.map((ln, i) => {
              if (ln.kind === "file") return null;
              if (ln.kind === "hunk") return <tr key={i} className="diff-line hunk"><td className="no" colSpan={2} /><td className="code">{ln.raw}</td></tr>;
              return (
                <Fragment key={i}>
                  <tr className={`diff-line ${ln.kind} ${inSel(i) ? "selected" : ""} ${has(ln) ? "has-comments" : ""}`} {...cell(i)} data-testid="pr-diff-row" data-side={side(ln)} data-line={lineNo(ln)} title="Click to comment or ask; drag or shift-click for several lines">
                    <td className="no">{ln.oldNo ?? ""}</td>
                    <td className="no">{ln.newNo ?? ""}</td>
                    <td className="code"><span className="marker">{ln.kind === "add" ? "+" : ln.kind === "del" ? "-" : " "}</span>{ln.text || " "}</td>
                  </tr>
                  {extras(ln, 3)}
                  {composer?.to === i && <tr className="diff-composer-row"><td colSpan={3}>{composerNode}</td></tr>}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
      {!collapsed && !file.binary_or_large && layout === "split" && (
        <table className="diff-table split" data-testid="pr-diff-split" style={numColStyle(lines)}>
          <SplitCols />
          <tbody>
            {rows.map((row, ri) => {
              if (row.hunk !== undefined) return <tr key={ri} className="diff-line hunk"><td className="no" /><td className="code" colSpan={3}>{lines[row.hunk].raw}</td></tr>;
              const L = row.left !== null ? lines[row.left] : null;
              const R = row.right !== null ? lines[row.right] : null;
              return (
                <Fragment key={ri}>
                  <tr className="diff-line split-row" data-testid="pr-diff-row">
                    <td className={`no ${L ? L.kind : "empty"}`}>{L?.oldNo ?? ""}</td>
                    <td className={`code left ${L ? L.kind : "empty"} ${L && inSel(row.left!) ? "selected" : ""}`} {...(L ? cell(row.left!) : {})} data-side="LEFT" data-line={L ? lineNo(L) : ""}>
                      {L ? <><span className="marker">{L.kind === "del" ? "-" : " "}</span>{L.text || " "}</> : ""}
                    </td>
                    <td className={`no ${R ? R.kind : "empty"}`}>{R?.newNo ?? ""}</td>
                    <td className={`code right ${R ? R.kind : "empty"} ${R && inSel(row.right!) ? "selected" : ""}`} {...(R ? cell(row.right!) : {})} data-side="RIGHT" data-line={R ? lineNo(R) : ""}>
                      {R ? <><span className="marker">{R.kind === "add" ? "+" : " "}</span>{R.text || " "}</> : ""}
                    </td>
                  </tr>
                  {splitExtras(L, R, ri, !!composer && (row.left === composer.to || row.right === composer.to))}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

function ThreadBox({ thread, apiBase, onChanged, onError, canAsk, onAsk }: {
  thread: GhThread; apiBase: string; onChanged: () => Promise<void>; onError: (m: string) => void;
  canAsk: boolean; onAsk: (quote: string, label: string, question: string) => Promise<void>;
}) {
  const [reply, setReply] = useState<string | null>(null);
  const [asking, setAsking] = useState<string | null>(null);
  const [show, setShow] = useState(!thread.resolved);
  const [busy, setBusy] = useState(false);
  useEffect(() => setShow(!thread.resolved), [thread.resolved]);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const first = thread.comments[0];
  const where = thread.subject === "FILE" ? thread.path : `${thread.path} ${thread.side === "LEFT" ? "L" : "R"}${thread.line ?? thread.original_line ?? "?"}`;
  const transcript = () => {
    const code = thread.diff_hunk ? thread.diff_hunk.split("\n").slice(-8).join("\n") + "\n\n" : "";
    return code + thread.comments.map((c) => `${c.author}: ${c.body}`).join("\n\n");
  };
  return (
    <div className={`gh-thread ${thread.resolved ? "resolved" : ""}`} data-testid="gh-thread" data-resolved={thread.resolved ? "1" : "0"}>
      {thread.resolved && (
        <button className="link-btn small gh-resolved-head" onClick={() => setShow((v) => !v)} data-testid="gh-thread-toggle">
          {show ? "▾" : "▸"} Resolved{thread.resolved_by ? ` by ${thread.resolved_by}` : ""} · {first?.author}: {first?.body.slice(0, 60)}
        </button>
      )}
      {show && (
        <>
          {thread.comments.map((c) => (
            <div key={c.id} className="gh-comment">
              <div className="gh-comment-head small"><GhUser login={c.author} /> <span className="muted">{ago(c.created_at)}</span>{c.url && <> · <a href={c.url} target="_blank" rel="noreferrer">GitHub</a></>}</div>
              <div className="markdown-body gh-comment-body" dangerouslySetInnerHTML={{ __html: md(c.body, apiBase) }} />
            </div>
          ))}
          {thread.total > thread.comments.length && <div className="muted small">… {thread.total - thread.comments.length} more on GitHub</div>}
          {reply === null && asking === null && (
            <div className="row gh-thread-actions">
              <button className="link-btn small" onClick={() => setReply("")} data-testid="gh-reply">Reply…</button>
              <button className="link-btn small" disabled={busy} onClick={() => void run(async () => { await post(`${apiBase}/threads/${thread.id}/resolve`, { resolved: !thread.resolved }); await onChanged(); })} data-testid="gh-resolve">
                {thread.resolved ? "Unresolve" : "Resolve conversation"}
              </button>
              <button className="link-btn small" disabled={!canAsk} onClick={() => setAsking("")} title={canAsk ? "Ask the agent about this thread" : "Choose the agent's wiki first"} data-testid="gh-ask">Ask agent…</button>
            </div>
          )}
          {reply !== null && (
            <div className="gh-reply-box">
              <MentionTextarea apiBase={apiBase} autoFocus rows={2} value={reply} onValue={setReply} placeholder={`Reply on GitHub (${COMPOSER_HINT})`}
                onKeyDown={(e) => composerKeyDown(e, () => void run(async () => { await post(`${apiBase}/comments/${first.id}/reply`, { body: reply }); setReply(null); await onChanged(); }), (v) => setReply(v), () => setReply(null))} data-testid="gh-reply-text" />
              <div className="row">
                <button className="btn small primary" disabled={busy || !reply.trim()} onClick={() => void run(async () => { await post(`${apiBase}/comments/${first.id}/reply`, { body: reply }); setReply(null); await onChanged(); })} title="Posted to GitHub right away" data-testid="gh-reply-send">Reply on GitHub</button>
                <button className="btn small" onClick={() => setReply(null)}>Cancel</button>
              </div>
            </div>
          )}
          {asking !== null && (
            <div className="gh-reply-box">
              <textarea autoFocus rows={2} value={asking} onChange={(e) => setAsking(e.target.value)} placeholder={`Ask the agent about this thread, e.g. "Is the reviewer right?" (${COMPOSER_HINT})`}
                onKeyDown={(e) => composerKeyDown(e, () => void run(async () => { await onAsk(transcript(), `thread on ${where}`, asking); setAsking(null); }), (v) => setAsking(v), () => setAsking(null))} data-testid="gh-ask-text" />
              <div className="row">
                <button className="btn small primary" disabled={busy || !asking.trim()} onClick={() => void run(async () => { await onAsk(transcript(), `thread on ${where}`, asking); setAsking(null); })} data-testid="gh-ask-send">Ask agent</button>
                <button className="btn small" onClick={() => setAsking(null)}>Cancel</button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function DraftBox({ draft, apiBase, onChanged, onError, canAsk = false, onAsk }: {
  draft: GhDraft; apiBase: string; onChanged: () => Promise<void>; onError: (m: string) => void;
  canAsk?: boolean; onAsk?: (quote: string, label: string, question: string) => Promise<void>;
}) {
  const [edit, setEdit] = useState<string | null>(null);
  const [asking, setAsking] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const agent = draft.origin === "agent";
  const decision = draft.decision ?? "accepted";
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const save = () => run(async () => { await patch(`${apiBase}/drafts/${draft.id}`, { body: edit }); setEdit(null); await onChanged(); });
  const remove = () => run(async () => { await del(`${apiBase}/drafts/${draft.id}`); await onChanged(); });
  const decide = (d: "accepted" | "proposed" | "discarded") => run(async () => { await patch(`${apiBase}/drafts/${draft.id}`, { decision: d }); await onChanged(); });
  const where = draft.subject === "file" ? draft.path : `${draft.path} ${draft.side === "LEFT" ? "L" : "R"}${draft.start_line ? `${draft.start_line}–` : ""}${draft.line}`;
  const ask = () => onAsk && run(async () => {
    await onAsk(`${draft.line_text ? draft.line_text + "\n\n" : ""}Suggested review comment (by the agent): ${draft.body}`, `suggested comment on ${where}`, asking ?? "");
    setAsking(null);
  });
  return (
    <div className={`gh-thread draft ${agent ? `agent ${decision}` : ""}`} data-testid="pr-draft" data-origin={draft.origin ?? "user"} data-decision={decision}>
      <div className="gh-comment-head small">
        {agent ? <span className="chip agent">Agent suggestion</span> : <span className="chip pending">Pending</span>}{" "}
        <span className="muted">{agent ? (decision === "accepted" ? "applied: published when you submit the review" : decision === "discarded" ? "discarded: not published" : "not published unless you apply it") : "published when you submit the review"}</span>
      </div>
      {agent && (
        <div className="pr-decision" role="radiogroup" aria-label="Decision" data-testid="pr-draft-decision">
          {([["accepted", "Apply"], ["proposed", "Undecided"], ["discarded", "Discard"]] as const).map(([v, label]) => (
            <label key={v} className={`pr-decision-opt ${v} ${decision === v ? "on" : ""}`}>
              <input type="radio" name={`decision-${draft.id}`} checked={decision === v} disabled={busy} onChange={() => void decide(v)} data-testid={`pr-draft-${v}`} /> {label}
            </label>
          ))}
        </div>
      )}
      {edit === null ? (
        <>
          {!(agent && decision === "discarded") && <div className="markdown-body gh-comment-body" dangerouslySetInnerHTML={{ __html: md(draft.body, apiBase) }} />}
          {agent && decision === "discarded" && <div className="muted small gh-discarded">{draft.body.slice(0, 120)}{draft.body.length > 120 ? "…" : ""}</div>}
          {asking === null && (
            <div className="row gh-thread-actions">
              <button className="link-btn small" onClick={() => setEdit(draft.body)}>Edit</button>
              {agent && onAsk && <button className="link-btn small" disabled={!canAsk} onClick={() => setAsking("")} title="Discuss this suggestion with the agent" data-testid="pr-draft-ask">Discuss with agent…</button>}
              <button className="link-btn small danger" onClick={() => void remove()} data-testid="pr-draft-delete">Delete</button>
            </div>
          )}
          {asking !== null && (
            <div className="gh-reply-box">
              <textarea autoFocus rows={2} value={asking} onChange={(e) => setAsking(e.target.value)} placeholder={`e.g. "Is this really a bug? Show me where." (${COMPOSER_HINT})`}
                onKeyDown={(e) => composerKeyDown(e, () => void ask(), (v) => setAsking(v), () => setAsking(null))} data-testid="pr-draft-ask-text" />
              <div className="row">
                <button className="btn small primary" disabled={busy || !asking.trim()} onClick={() => void ask()} data-testid="pr-draft-ask-send">Ask agent</button>
                <button className="btn small" onClick={() => setAsking(null)}>Cancel</button>
              </div>
            </div>
          )}
        </>
      ) : (
        <>
          <MentionTextarea apiBase={apiBase} autoFocus rows={3} value={edit} onValue={setEdit} onKeyDown={(e) => composerKeyDown(e, () => void save(), (v) => setEdit(v), () => setEdit(null))} />
          <div className="row">
            <button className="btn small primary" onClick={() => void save()} disabled={!edit.trim()}>Save</button>
            <button className="btn small" onClick={() => setEdit(null)}>Cancel</button>
          </div>
        </>
      )}
    </div>
  );
}

/** "Finish your review": summary + Comment / Approve / Request changes → one review on GitHub. */
export function SubmitReview({ apiBase, drafts, undecided = 0, fromGithub = 0, owner, onDone, onError }: { apiBase: string; drafts: number; undecided?: number; fromGithub?: number; owner: string | null; onDone: (url: string | null) => Promise<void>; onError: (m: string) => void }) {
  const [open, setOpen] = useState(false);
  const [body, setBody] = useState("");
  const [event, setEvent] = useState<"COMMENT" | "APPROVE" | "REQUEST_CHANGES">("COMMENT");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      const r = await post<{ url: string | null }>(`${apiBase}/review`, { event, body });
      setOpen(false);
      setBody("");
      await onDone(r.url);
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <span className="ws-menu-anchor">
      <button className={`btn small ${drafts ? "primary" : ""}`} onClick={() => setOpen((v) => !v)} aria-expanded={open} data-testid="pr-finish-review">
        Finish review{drafts ? ` (${drafts})` : ""} ▾
      </button>
      {open && (
        <div className="ws-menu pr-submit" data-testid="pr-submit">
          {undecided > 0 && <div className="small reviews-note" data-testid="pr-submit-undecided">{undecided} agent suggestion(s) are undecided and will not be published (choose Apply to include them).</div>}
          {fromGithub > 0 && <div className="small reviews-note" data-testid="pr-submit-github">Your pending review on github.com has {fromGithub} comment(s); they are submitted with this review.</div>}
          <MentionTextarea apiBase={apiBase} rows={4} value={body} onValue={setBody} placeholder="Summary (optional, Markdown)" data-testid="pr-submit-body" />
          {(["COMMENT", "APPROVE", "REQUEST_CHANGES"] as const).map((ev) => (
            <label key={ev} className="pr-submit-opt">
              <input type="radio" name="review-event" checked={event === ev} onChange={() => setEvent(ev)} data-testid={`pr-event-${ev.toLowerCase()}`} />
              <span><b>{{ COMMENT: "Comment", APPROVE: "Approve", REQUEST_CHANGES: "Request changes" }[ev]}</b>
                <span className="muted small"> — {{ COMMENT: "general feedback, no decision", APPROVE: "approve merging these changes", REQUEST_CHANGES: "feedback that must be addressed" }[ev]}</span></span>
            </label>
          ))}
          <div className="row">
            <button className="btn small primary" disabled={busy || (!drafts && !fromGithub && !body.trim() && event !== "APPROVE")} onClick={() => void submit()} data-testid="pr-submit-send">
              {busy ? "Publishing…" : `Submit review${drafts ? ` with ${drafts} comment${drafts > 1 ? "s" : ""}` : ""}`}
            </button>
            <span className="muted small">posted on GitHub as <b>{owner ?? "you"}</b></span>
          </div>
        </div>
      )}
    </span>
  );
}
