import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, del, patch, post } from "../api";
import { renderMarkdown, sanitizeHtml } from "../markdown";
import { COMPOSER_HINT, composerKeyDown } from "./keys";
import { Line, buildSplitRows, parseUnified } from "./DiffReview";

/** A review comment on GitHub. */
export interface GhComment {
  id: number; path: string | null; line: number | null; side: "LEFT" | "RIGHT"; start_line: number | null; start_side: string | null;
  original_line: number | null; outdated: boolean; body: string; author: string | null; created_at: string | null;
  in_reply_to_id: number | null; url: string | null;
}
/** A comment written here, published with the next review. */
export interface GhDraft { id: string; path: string; side: "LEFT" | "RIGHT"; line: number; start_side: string | null; start_line: number | null; line_text: string; body: string }
export interface PrFile { path: string; previous_path: string | null; status: string; additions: number; deletions: number; unified: string; binary_or_large: boolean }

type Sel = { from: number; to: number }; // indices into the parsed lines, inclusive

const side = (ln: Line): "LEFT" | "RIGHT" => (ln.kind === "del" ? "LEFT" : "RIGHT");
const lineNo = (ln: Line): number => (ln.kind === "del" ? ln.oldNo! : ln.newNo!);
const md = (s: string) => sanitizeHtml(renderMarkdown(s || "", ""), "");
const ago = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "");

/**
 * One changed file of a pull request, GitHub-style: unified or side-by-side; click a line (drag / shift-click for
 * several) to write a review comment (published later with the review) or to ask the agent about those lines;
 * existing GitHub threads are shown at their lines and can be replied to.
 */
export function PrFileDiff({ file, layout, comments, drafts, apiBase, canAsk, collapsedDefault, onChanged, onAsk, onError }: {
  file: PrFile;
  layout: "unified" | "split";
  comments: GhComment[];
  drafts: GhDraft[];
  apiBase: string; // /github/pr/{owner}/{repo}/{n}
  canAsk: boolean;
  collapsedDefault: boolean;
  onChanged: () => Promise<void>;
  onAsk: (quote: string, label: string, question: string) => Promise<void>;
  onError: (m: string) => void;
}) {
  const [collapsed, setCollapsed] = useState(collapsedDefault);
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
  const roots = comments.filter((c) => !c.in_reply_to_id);
  const replies = (id: number) => comments.filter((c) => c.in_reply_to_id === id);
  const at = (ln: Line) => {
    const k = `${side(ln)}:${lineNo(ln)}`;
    const alt = ln.kind === "ctx" ? `LEFT:${ln.oldNo}` : null; // context lines can carry LEFT comments too
    return {
      threads: roots.filter((c) => !c.outdated && (`${c.side}:${c.line}` === k || `${c.side}:${c.line}` === alt)),
      drafts: drafts.filter((d) => `${d.side}:${d.line}` === k || `${d.side}:${d.line}` === alt),
    };
  };
  const outdated = roots.filter((c) => c.outdated);
  const total = comments.length + drafts.length;

  const composerNode = composer && (
    <div className="diff-composer" data-testid="pr-composer" onPointerDown={(e) => e.stopPropagation()}>
      <div className="muted small">{selected.length > 1 ? `Lines ${label().slice(file.path.length + 1)}` : `Line ${label().slice(file.path.length + 1)}`}</div>
      <textarea autoFocus rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder={`Leave a review comment, or ask the agent about these lines (${COMPOSER_HINT})`}
        onKeyDown={(e) => composerKeyDown(e, () => void addComment(), setText, cancel)} data-testid="pr-composer-text" />
      <div className="row">
        <button className="btn small primary" disabled={busy || !text.trim()} onClick={() => void addComment()} title="Saved here; published to GitHub when you submit the review" data-testid="pr-add-comment">Add review comment</button>
        <button className="btn small" disabled={busy || !text.trim() || !canAsk} onClick={() => void ask()} title={canAsk ? "Ask the agent about these lines (the answer appears in the discussions panel)" : "Choose the agent's wiki first"} data-testid="pr-ask-lines">Ask agent</button>
        <button className="btn small" onClick={cancel}>Cancel</button>
      </div>
    </div>
  );

  const extras = (ln: Line, colSpan: number) => {
    const { threads, drafts: ds } = at(ln);
    return [
      ...threads.map((c) => (
        <tr key={`t${c.id}`} className="diff-comment-row"><td colSpan={colSpan}><GhThread root={c} replies={replies(c.id)} apiBase={apiBase} onChanged={onChanged} onError={onError} /></td></tr>
      )),
      ...ds.map((d) => (
        <tr key={`d${d.id}`} className="diff-comment-row"><td colSpan={colSpan}><DraftBox draft={d} apiBase={apiBase} onChanged={onChanged} onError={onError} /></td></tr>
      )),
    ];
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
        {total > 0 && <span className="chip" data-testid="pr-file-comments">{total} comment{total > 1 ? "s" : ""}</span>}
      </header>
      {!collapsed && file.binary_or_large && <div className="muted small pad">No textual diff (binary or too large) — see it on GitHub.</div>}
      {!collapsed && outdated.length > 0 && (
        <details className="pr-outdated pad">
          <summary className="muted small">{outdated.length} comment thread{outdated.length > 1 ? "s" : ""} on code that changed since (outdated)</summary>
          {outdated.map((c) => <GhThread key={c.id} root={c} replies={replies(c.id)} apiBase={apiBase} onChanged={onChanged} onError={onError} />)}
        </details>
      )}
      {!collapsed && !file.binary_or_large && layout === "unified" && (
        <table className="diff-table" data-testid="pr-diff-unified">
          <tbody>
            {lines.map((ln, i) => {
              if (ln.kind === "file") return null;
              if (ln.kind === "hunk") return <tr key={i} className="diff-line hunk"><td className="no" colSpan={2} /><td className="code">{ln.raw}</td></tr>;
              return (
                <Fragment key={i}>
                  <tr className={`diff-line ${ln.kind} ${inSel(i) ? "selected" : ""} ${has(ln) ? "has-comments" : ""}`} {...cell(i)} data-testid="pr-diff-row" data-side={side(ln)} data-line={lineNo(ln)} title="Click to comment or ask; drag or shift-click for several lines">
                    <td className="no">{ln.oldNo ?? ""}</td>
                    <td className="no">{ln.newNo ?? ""}</td>
                    <td className="code"><span className="marker">{ln.kind === "add" ? "+" : ln.kind === "del" ? "−" : " "}</span>{ln.text || " "}</td>
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
        <table className="diff-table split" data-testid="pr-diff-split">
          <tbody>
            {rows.map((row, ri) => {
              if (row.hunk !== undefined) return <tr key={ri} className="diff-line hunk"><td className="code" colSpan={4}>{lines[row.hunk].raw}</td></tr>;
              const L = row.left !== null ? lines[row.left] : null;
              const R = row.right !== null ? lines[row.right] : null;
              const ex = [...(L ? extras(L, 4) : []), ...(R && R !== L ? extras(R, 4) : [])];
              return (
                <Fragment key={ri}>
                  <tr className="diff-line split-row" data-testid="pr-diff-row">
                    <td className="no">{L?.oldNo ?? ""}</td>
                    <td className={`code left ${L ? L.kind : "empty"} ${L && inSel(row.left!) ? "selected" : ""}`} {...(L ? cell(row.left!) : {})} data-side="LEFT" data-line={L ? lineNo(L) : ""}>
                      {L ? <><span className="marker">{L.kind === "del" ? "−" : " "}</span>{L.text || " "}</> : ""}
                    </td>
                    <td className="no">{R?.newNo ?? ""}</td>
                    <td className={`code right ${R ? R.kind : "empty"} ${R && inSel(row.right!) ? "selected" : ""}`} {...(R ? cell(row.right!) : {})} data-side="RIGHT" data-line={R ? lineNo(R) : ""}>
                      {R ? <><span className="marker">{R.kind === "add" ? "+" : " "}</span>{R.text || " "}</> : ""}
                    </td>
                  </tr>
                  {ex}
                  {composer && (row.left === composer.to || row.right === composer.to) && <tr className="diff-composer-row"><td colSpan={4}>{composerNode}</td></tr>}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

function GhThread({ root, replies, apiBase, onChanged, onError }: { root: GhComment; replies: GhComment[]; apiBase: string; onChanged: () => Promise<void>; onError: (m: string) => void }) {
  const [reply, setReply] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const send = async () => {
    if (!reply?.trim()) return;
    setBusy(true);
    try {
      await post(`${apiBase}/comments/${root.id}/reply`, { body: reply });
      setReply(null);
      await onChanged();
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="gh-thread" data-testid="gh-thread">
      {[root, ...replies].map((c) => (
        <div key={c.id} className="gh-comment">
          <div className="gh-comment-head small"><b>{c.author}</b> <span className="muted">{ago(c.created_at)}</span>{c.url && <> · <a href={c.url} target="_blank" rel="noreferrer">GitHub</a></>}</div>
          <div className="markdown-body gh-comment-body" dangerouslySetInnerHTML={{ __html: md(c.body) }} />
        </div>
      ))}
      {reply === null ? (
        <button className="link-btn small" onClick={() => setReply("")} data-testid="gh-reply">Reply…</button>
      ) : (
        <div className="gh-reply-box">
          <textarea autoFocus rows={2} value={reply} onChange={(e) => setReply(e.target.value)} placeholder={`Reply on GitHub (${COMPOSER_HINT})`}
            onKeyDown={(e) => composerKeyDown(e, () => void send(), (v) => setReply(v), () => setReply(null))} data-testid="gh-reply-text" />
          <div className="row">
            <button className="btn small primary" disabled={busy || !reply.trim()} onClick={() => void send()} title="Posted to GitHub right away" data-testid="gh-reply-send">Reply on GitHub</button>
            <button className="btn small" onClick={() => setReply(null)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

function DraftBox({ draft, apiBase, onChanged, onError }: { draft: GhDraft; apiBase: string; onChanged: () => Promise<void>; onError: (m: string) => void }) {
  const [edit, setEdit] = useState<string | null>(null);
  const save = async () => {
    try {
      await patch(`${apiBase}/drafts/${draft.id}`, { body: edit });
      setEdit(null);
      await onChanged();
    } catch (e) {
      onError((e as ApiError).message);
    }
  };
  const remove = async () => {
    try {
      await del(`${apiBase}/drafts/${draft.id}`);
      await onChanged();
    } catch (e) {
      onError((e as ApiError).message);
    }
  };
  return (
    <div className="gh-thread draft" data-testid="pr-draft">
      <div className="gh-comment-head small"><span className="chip pending">Pending</span> <span className="muted">published when you submit the review</span></div>
      {edit === null ? (
        <>
          <div className="markdown-body gh-comment-body" dangerouslySetInnerHTML={{ __html: md(draft.body) }} />
          <div className="row">
            <button className="link-btn small" onClick={() => setEdit(draft.body)}>Edit</button>
            <button className="link-btn small danger" onClick={() => void remove()} data-testid="pr-draft-delete">Delete</button>
          </div>
        </>
      ) : (
        <>
          <textarea autoFocus rows={3} value={edit} onChange={(e) => setEdit(e.target.value)} onKeyDown={(e) => composerKeyDown(e, () => void save(), (v) => setEdit(v), () => setEdit(null))} />
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
export function SubmitReview({ apiBase, drafts, owner, onDone, onError }: { apiBase: string; drafts: number; owner: string | null; onDone: (url: string | null) => Promise<void>; onError: (m: string) => void }) {
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
          <textarea rows={4} value={body} onChange={(e) => setBody(e.target.value)} placeholder="Summary (optional, Markdown)" data-testid="pr-submit-body" />
          {(["COMMENT", "APPROVE", "REQUEST_CHANGES"] as const).map((ev) => (
            <label key={ev} className="pr-submit-opt">
              <input type="radio" name="review-event" checked={event === ev} onChange={() => setEvent(ev)} data-testid={`pr-event-${ev.toLowerCase()}`} />
              <span><b>{{ COMMENT: "Comment", APPROVE: "Approve", REQUEST_CHANGES: "Request changes" }[ev]}</b>
                <span className="muted small"> — {{ COMMENT: "general feedback, no decision", APPROVE: "approve merging these changes", REQUEST_CHANGES: "feedback that must be addressed" }[ev]}</span></span>
            </label>
          ))}
          <div className="row">
            <button className="btn small primary" disabled={busy || (!drafts && !body.trim() && event !== "APPROVE")} onClick={() => void submit()} data-testid="pr-submit-send">
              {busy ? "Publishing…" : `Submit review${drafts ? ` with ${drafts} comment${drafts > 1 ? "s" : ""}` : ""}`}
            </button>
            <span className="muted small">posted on GitHub as <b>{owner ?? "you"}</b></span>
          </div>
        </div>
      )}
    </span>
  );
}
