import { Fragment, useEffect, useMemo, useState } from "react";
import { AgentSession, ApiError, DiffComment, DiffFile, DiffResult, GitRefs, get, post } from "../api";
import { FileIcon } from "./FileIcon";

interface Line {
  kind: "add" | "del" | "ctx" | "hunk" | "file";
  text: string; // without the +/-/space marker
  raw: string;
  oldNo: number | null;
  newNo: number | null;
  hunkIndex: number;
}

function parseUnified(unified: string): { lines: Line[]; hunks: string[] } {
  const lines: Line[] = [];
  const hunks: string[] = [];
  let oldNo = 0;
  let newNo = 0;
  let hunkIndex = -1;
  let cur: string[] = [];
  for (const raw of unified.split("\n")) {
    if (raw.startsWith("+++") || raw.startsWith("---")) {
      lines.push({ kind: "file", text: raw, raw, oldNo: null, newNo: null, hunkIndex });
      continue;
    }
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

export default function DiffReview({
  path,
  isDir,
  sessions,
  defaultProvider,
  onOpenDocument,
  onOpenThread,
  onClose,
  refreshKey,
}: {
  path: string;
  isDir: boolean;
  sessions: AgentSession[];
  defaultProvider: string | null;
  onOpenDocument: (path: string) => void;
  onOpenThread: (docPath: string, threadId: string) => void;
  onClose: () => void;
  refreshKey: number;
}) {
  const [base, setBase] = useState("HEAD");
  const [target, setTarget] = useState<string>("worktree");
  const [markdownOnly, setMarkdownOnly] = useState(false);
  const [result, setResult] = useState<DiffResult | null>(null);
  const [refs, setRefs] = useState<GitRefs | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [composer, setComposer] = useState<{ file: string; index: number; line: Line; hunk: string } | null>(null);
  const [question, setQuestion] = useState("");
  const [sessionChoice, setSessionChoice] = useState<string>("__default__");
  const [busy, setBusy] = useState(false);
  const [posted, setPosted] = useState<string | null>(null);

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

  const parsed = useMemo(() => Object.fromEntries((result?.files ?? []).map((f) => [f.path, parseUnified(f.unified)])), [result]);

  const send = async () => {
    if (!composer || !question.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const r = await post<{ thread_id: string; document_id: string }>("/diff/comments", {
        path: composer.file,
        base,
        target,
        side: composer.line.kind === "add" ? "added" : composer.line.kind === "del" ? "deleted" : "context",
        line_no: composer.line.newNo ?? composer.line.oldNo,
        line_text: composer.line.text,
        hunk: composer.hunk,
        question: question.trim(),
        session_id: sessionChoice === "__default__" ? null : sessionChoice,
      });
      setPosted(r.thread_id);
      setQuestion("");
      setComposer(null);
      await load();
      onOpenThread(composer.file, r.thread_id);
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
          <label className="small" title="Hide changes in non-Markdown files"><input type="checkbox" checked={markdownOnly} onChange={(e) => setMarkdownOnly(e.target.checked)} /> Markdown only</label>
          <button className="btn small" onClick={() => void load()} title="Reload">↻</button>
          <button className="icon-btn" onClick={onClose} title="Close diff review">×</button>
        </div>
      </div>
      {error && <div className="banner warn">{error}</div>}
      {result && (
        <div className="diff-summary muted small">
          {result.summary.files} file{result.summary.files === 1 ? "" : "s"} · <span className="add">+{result.summary.additions}</span> <span className="del">−{result.summary.deletions}</span>
          {result.files.length > 1 && (
            <>
              {" · "}
              <button className="link-btn" onClick={() => setCollapsed(new Set(result.files.map((f) => f.path)))}>collapse all</button>
              {" · "}
              <button className="link-btn" onClick={() => setCollapsed(new Set())}>expand all</button>
            </>
          )}
          {" · "}click a line to comment on it
        </div>
      )}
      <div className="diff-body">
        {loading && !result && <div className="muted pad">Loading…</div>}
        {result && result.files.length === 0 && <div className="muted pad">No differences between {base} and {target === "worktree" ? "the working tree" : target}{isDir ? ` under ${path || "the workspace"}` : ""}.</div>}
        {result?.files.map((f) => (
          <FileDiff
            key={f.path}
            file={f}
            lines={parsed[f.path].lines}
            hunks={parsed[f.path].hunks}
            comments={result.comments[f.path] ?? []}
            collapsed={collapsed.has(f.path)}
            onToggle={() => toggle(f.path)}
            onOpen={() => onOpenDocument(f.path)}
            onOpenThread={(tid) => onOpenThread(f.path, tid)}
            onComment={(index, line, hunk) => {
              setComposer({ file: f.path, index, line, hunk });
              setPosted(null);
            }}
            composerIndex={composer?.file === f.path ? composer.index : null}
            composerNode={
              composer?.file === f.path ? (
                <tr className="diff-composer-row">
                  <td colSpan={3}>
                    <div className="diff-composer" data-testid="diff-composer">
                      <div className="label">Comment on {composer.line.kind === "del" ? "removed" : composer.line.kind === "add" ? "added" : "unchanged"} line {composer.line.newNo ?? composer.line.oldNo}: <code>{composer.line.text.slice(0, 80)}</code></div>
                      <textarea autoFocus rows={3} value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="Your comment or question about this change (Ctrl/⌘+Enter to send)" data-testid="diff-comment-input"
                        onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") void send(); if (e.key === "Escape") setComposer(null); }} />
                      <div className="row">
                        <select value={sessionChoice} onChange={(e) => setSessionChoice(e.target.value)}>
                          <option value="__default__">{defaultProvider ? `Document session (${defaultProvider})` : "No agent provider configured"}</option>
                          <option value="new">New session</option>
                          {sessions.filter((s) => s.origin !== "preview").map((s) => (
                            <option key={s.id} value={s.id}>{s.provider} · {s.display_name ?? s.short_id ?? s.id.slice(0, 8)}</option>
                          ))}
                        </select>
                        <button className="btn primary small" disabled={busy || !question.trim()} onClick={() => void send()} data-testid="diff-comment-send">Send</button>
                        <button className="btn small" onClick={() => setComposer(null)}>Cancel</button>
                      </div>
                      {!f.commentable && <div className="warn small">Comments can only be attached to files the reader can open (Markdown, text/source, .docx).</div>}
                    </div>
                  </td>
                </tr>
              ) : null
            }
          />
        ))}
      </div>
      {posted && <div className="toast">Comment posted as a discussion; the agent is answering.</div>}
    </div>
  );
}

function FileDiff({ file, lines, hunks, comments, collapsed, onToggle, onOpen, onOpenThread, onComment, composerIndex, composerNode }: {
  file: DiffFile;
  lines: Line[];
  hunks: string[];
  comments: DiffComment[];
  collapsed: boolean;
  onToggle: () => void;
  onOpen: () => void;
  onOpenThread: (threadId: string) => void;
  onComment: (index: number, line: Line, hunk: string) => void;
  composerIndex: number | null;
  composerNode: React.ReactNode;
}) {
  const byLine = useMemo(() => {
    const m = new Map<string, DiffComment[]>();
    for (const c of comments) {
      const key = `${c.diff.side}:${(c.diff.line_text ?? "").trim()}`;
      m.set(key, [...(m.get(key) ?? []), c]);
    }
    return m;
  }, [comments]);
  const kind = file.path.split("/").pop() ?? file.path;
  return (
    <section className={`diff-file ${collapsed ? "collapsed" : ""}`} data-testid="diff-file" data-path={file.path}>
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
        <span className="muted small">{kind}</span>
      </header>
      {!collapsed && (
        <table className="diff-table">
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
              const side = ln.kind === "add" ? "added" : ln.kind === "del" ? "deleted" : "context";
              const cs = byLine.get(`${side}:${ln.text.trim()}`) ?? [];
              const isComposer = composerIndex === i;
              return (
                <Fragment key={i}>
                  <tr className={`diff-line ${ln.kind} ${isComposer ? "composing" : ""} ${cs.length ? "has-comments" : ""}`} onClick={() => file.commentable && onComment(i, ln, hunks[ln.hunkIndex] ?? "")} title={file.commentable ? "Click to comment on this line" : undefined} data-testid="diff-row">
                    <td className="no">{ln.oldNo ?? ""}</td>
                    <td className="no">{ln.newNo ?? ""}</td>
                    <td className="code">
                      <span className="marker">{ln.kind === "add" ? "+" : ln.kind === "del" ? "−" : " "}</span>
                      {ln.text || " "}
                    </td>
                  </tr>
                  {cs.map((c) => (
                    <tr key={c.thread_id} className="diff-comment-row">
                      <td colSpan={3}>
                        <button className="diff-comment" onClick={() => onOpenThread(c.thread_id)} data-testid="diff-comment-marker" title="Open this discussion">
                          <span className="who">You:</span> {c.question.slice(0, 160)}
                          <span className="muted small"> · {c.message_count} messages{c.last_status && c.last_status !== "completed" ? ` · ${c.last_status}` : ""} ›</span>
                        </button>
                      </td>
                    </tr>
                  ))}
                  {isComposer ? composerNode : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
