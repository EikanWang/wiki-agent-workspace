import { useEffect, useMemo, useRef, useState } from "react";
import { COMPOSER_HINT, composerKeyDown } from "./keys";
import { AgentSession, ApiError, JobEvent, Message, Thread, get, subscribeJob } from "../api";
import DiffView from "./DiffView";
import MarkdownView from "./MarkdownView";
import ModelPicker from "./ModelPicker";
import { PlusIcon } from "./Icons";

export interface LiveJob {
  jobId: string;
  threadId: string | null;
  status: string;
  text: string;
  parts: Record<string, string>;
  order: string[];
  activity: string[];
  error: { code: string; message: string; hint?: string } | null;
  permission: Record<string, any> | null;
  lastSeq: number;
}

export function useJobStream(jobId: string | null, onFinished: (job: LiveJob) => void): LiveJob | null {
  const [job, setJob] = useState<LiveJob | null>(null);
  const ref = useRef<LiveJob | null>(null);
  useEffect(() => {
    if (!jobId) {
      setJob(null);
      ref.current = null;
      return;
    }
    const init: LiveJob = { jobId, threadId: null, status: "queued", text: "", parts: {}, order: [], activity: [], error: null, permission: null, lastSeq: 0 };
    ref.current = init;
    setJob(init);
    const unsub = subscribeJob(
      jobId,
      0,
      (ev: JobEvent) => {
        const cur = ref.current;
        if (!cur || ev.seq <= cur.lastSeq) return;
        const next: LiveJob = { ...cur, lastSeq: ev.seq, parts: { ...cur.parts }, order: [...cur.order], activity: [...cur.activity] };
        const p = ev.payload;
        if (p.thread_id) next.threadId = p.thread_id;
        switch (ev.type) {
          case "queued":
          case "running":
          case "started":
            next.status = p.status ?? "running";
            if (p.message) next.activity.push(p.message);
            break;
          case "waiting":
            next.status = "waiting_for_session";
            next.activity.push(p.message ?? "Waiting for the session to become free…");
            break;
          case "delta": {
            const pid = p.part_id ?? "t";
            if (!(pid in next.parts)) next.order.push(pid);
            next.parts[pid] = p.replace ? p.text : (next.parts[pid] ?? "") + p.text;
            next.text = next.order.map((k) => next.parts[k]).join("");
            break;
          }
          case "tool_activity":
            next.activity.push(`${p.tool ?? "tool"}${p.status ? ` · ${p.status}` : ""}${p.title ? ` · ${String(p.title).slice(0, 80)}` : ""}`);
            break;
          case "notice":
            next.activity.push(p.message ?? "");
            break;
          case "permission_request":
            next.status = "waiting_provider_permission";
            next.permission = p;
            break;
          case "completed":
            next.status = "completed";
            next.text = p.text ?? next.text;
            next.permission = null;
            break;
          case "failed":
            next.status = "failed";
            next.error = { code: p.code, message: p.message, hint: p.hint };
            next.permission = null;
            break;
          case "cancelled":
            next.status = "cancelled";
            next.permission = null;
            break;
        }
        ref.current = next;
        setJob(next);
      },
      () => {
        if (ref.current) onFinished(ref.current);
      },
    );
    return unsub;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);
  return job;
}

export default function DiscussionPanel({
  thread,
  threads,
  sessions,
  defaultSessionId,
  liveJob,
  onReply,
  onRetry,
  onCancel,
  onPermission,
  onSelectThread,
  docId,
  onRequestReanchor,
  reanchorArmed,
  onCollapse,
  resizer,
  mode = "individual",
  onModeChange,
  onSessionChanged,
  onAskDocument,
  docFormat,
}: {
  thread: Thread | null;
  threads: Thread[];
  sessions: AgentSession[];
  defaultSessionId: string | null;
  liveJob: LiveJob | null;
  onReply: (threadId: string, text: string, sessionChoice: string | null) => Promise<void>;
  onRetry: (threadId: string, messageId: string, sessionChoice: string | null) => Promise<void>;
  onCancel: (jobId: string) => Promise<void>;
  onPermission: (jobId: string, requestId: string, decision: string) => Promise<void>;
  onSelectThread: (id: string | null) => void;
  docId: string | null;
  onRequestReanchor: () => void;
  reanchorArmed: boolean;
  onCollapse?: () => void;
  resizer?: React.ReactNode;
  mode?: "individual" | "flat";
  onModeChange?: (mode: "individual" | "flat") => void;
  /** Called after a session's model/variant was changed so the caller can refresh its session list. */
  onSessionChanged?: (s: AgentSession) => void;
  /** Ask a question about the open document as a whole (no passage). */
  onAskDocument?: (question: string) => Promise<void>;
  docFormat?: string;
}) {
  const [focusAsk, setFocusAsk] = useState(0); // bumped by "＋ New question": focus the box
  const askBox = onAskDocument ? <AskDocument onAsk={onAskDocument} format={docFormat} focusKey={focusAsk} /> : null;
  const [text, setText] = useState("");
  const [sessionChoice, setSessionChoice] = useState<string>("__last__");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [diff, setDiff] = useState<{ unified: string; identical: boolean; from: string } | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const sessionsById = useMemo(() => Object.fromEntries(sessions.map((s) => [s.id, s])), [sessions]);

  useEffect(() => {
    setSessionChoice("__last__");
    setError(null);
    setDiff(null);
  }, [thread?.id]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [thread?.messages.length, liveJob?.text, liveJob?.status]);

  const modeSwitch = onModeChange ? (
    <span className="mode-switch" role="tablist" aria-label="Discussion panel mode">
      <button role="tab" aria-selected={mode === "individual"} className={mode === "individual" ? "active" : ""} onClick={() => onModeChange("individual")} title="One discussion at a time" data-testid="mode-individual">Individual</button>
      <button role="tab" aria-selected={mode === "flat"} className={mode === "flat" ? "active" : ""} onClick={() => onModeChange("flat")} title="All discussions of this document as one stream" data-testid="mode-flat">Flat</button>
    </span>
  ) : null;

  if (mode === "flat") {
    return (
      <FlatPanel
        threads={threads}
        sessions={sessions}
        sessionsById={sessionsById}
        defaultSessionId={defaultSessionId}
        liveJob={liveJob}
        activeThreadId={thread?.id ?? null}
        onReply={onReply}
        onRetry={onRetry}
        onCancel={onCancel}
        onPermission={onPermission}
        onSelectThread={onSelectThread}
        onCollapse={onCollapse}
        resizer={resizer}
        modeSwitch={modeSwitch}
        onSessionChanged={onSessionChanged}
        askBox={askBox}
      />
    );
  }

  if (!thread) {
    return (
      <aside className="discussion" data-testid="discussion-panel">
        {resizer}
        <div className="panel-head">
          <span>Discussions</span>
          <span className="muted">{threads.length} total</span>
          <span className="spacer" />
          {modeSwitch}
          {onCollapse && <button className="icon-btn" onClick={onCollapse} title="Hide discussions panel" data-testid="panel-collapse">›</button>}
        </div>
        {askBox}
        <div className="thread-list">
          {threads.length === 0 && <div className="muted pad">Ask above about the whole document, or select text in it to ask about a passage.</div>}
          {threads.map((t, i) => (
            <button key={t.id} className="thread-item" onClick={() => onSelectThread(t.id)} data-testid="thread-item">
              <div className="thread-title">Discussion {i + 1} · {t.messages[0]?.content.slice(0, 50)}</div>
              <div className="thread-quote">{quoteLabel(t, 80)}</div>
              <div className="thread-meta">
                <AnchorBadge state={t.anchor_state} />
                <span className="muted">{t.messages.length} messages</span>
              </div>
            </button>
          ))}
        </div>
      </aside>
    );
  }

  const idx = threads.findIndex((t) => t.id === thread.id);
  const lastSess = thread.last_session_id ? sessionsById[thread.last_session_id] : undefined;
  const differs = thread.last_session_id && defaultSessionId && thread.last_session_id !== defaultSessionId;
  const running = liveJob && liveJob.threadId === thread.id && !["completed", "failed", "cancelled"].includes(liveJob.status);
  const liveMessageId = thread.messages.find((m) => m.job_id === liveJob?.jobId)?.id;

  const send = async () => {
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await onReply(thread.id, text.trim(), sessionChoice === "__last__" ? null : sessionChoice);
      setText("");
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const showDiff = async () => {
    if (!docId) return;
    try {
      const from = thread.anchor.version_sha256;
      const r = await get<{ unified: string; identical: boolean }>(`/documents/${docId}/diff?from=${from}&to=current`);
      setDiff({ unified: r.unified, identical: r.identical, from });
    } catch (e) {
      setError((e as ApiError).message);
    }
  };

  return (
    <aside className="discussion" data-testid="discussion-panel">
      {resizer}
      <div className="panel-head">
        <button className="icon-btn" onClick={() => onSelectThread(null)} title="Back to the discussion list">‹</button>
        <span className="panel-title">Discussion {idx + 1}/{threads.length}</span>
        {onAskDocument && (
          <button className="icon-btn" onClick={() => { onSelectThread(null); setFocusAsk((k) => k + 1); }} title="New question about this document" aria-label="New question" data-testid="new-doc-question"><PlusIcon size={15} /></button>
        )}
        <span className="spacer" />
        {modeSwitch}
        <button className="btn small nowrap" onClick={showDiff} title="Show the diff between the version this discussion was created on and the current file" data-testid="thread-diff">Diff</button>
        {onCollapse && <button className="icon-btn" onClick={onCollapse} title="Hide discussions panel" data-testid="panel-collapse">›</button>}
      </div>
      <div className="thread-body">
        <section className="quote-box">
          {thread.anchor_state === "page" ? (
            <div data-testid="thread-target">
              <div className="label">{targetLabel(thread.target)} <AnchorBadge state={thread.anchor_state} /></div>
              {thread.target?.attachments?.map((n) => (
                <a key={n} href={`/api/v1/threads/${thread.id}/attachments/${n}`} target="_blank" rel="noreferrer" title="Open the attached image">
                  <img className="thread-attachment" src={`/api/v1/threads/${thread.id}/attachments/${n}`} alt="attached to this discussion" data-testid="thread-attachment" />
                </a>
              ))}
              {thread.anchor.quote && <blockquote data-testid="thread-quote">{thread.anchor.quote}</blockquote>}
            </div>
          ) : thread.anchor_state === "document" ? (
            <div className="label" data-testid="thread-whole-document">About the whole document <AnchorBadge state={thread.anchor_state} /></div>
          ) : (
            <>
              <div className="label">Quoted passage <AnchorBadge state={thread.anchor_state} /></div>
              <blockquote data-testid="thread-quote">{thread.anchor.quote}</blockquote>
            </>
          )}
          {thread.anchor.heading && <div className="muted small">Section: {thread.anchor.heading}</div>}
          {(thread.anchor_state === "orphaned" || thread.anchor_state === "ambiguous") && (
            <div className="warn small">
              {thread.anchor_state === "orphaned" ? "The original text was modified or deleted; the discussion is not attached to any passage." : `The same text occurs several times (${thread.candidates ?? ""} candidates); the position cannot be determined uniquely.`}
              <button className={`btn small ${reanchorArmed ? "primary" : ""}`} onClick={onRequestReanchor}>
                {reanchorArmed ? "Select the new passage in the document…" : "Reanchor manually"}
              </button>
            </div>
          )}
          {thread.original_anchor.quote !== thread.anchor.quote && <div className="muted small">Original quote: "{thread.original_anchor.quote.slice(0, 80)}"</div>}
          {thread.diff && (
            <div className="diff-origin small" data-testid="thread-diff-origin">
              <span className="chip">diff comment</span> on {{ deleted: "removed", added: "added", context: "unchanged", mixed: "changed" }[thread.diff.side] ?? thread.diff.side}{" "}
              {thread.diff.line_end && thread.diff.line_end !== thread.diff.line_no ? `lines ${thread.diff.line_no}–${thread.diff.line_end}` : `line ${thread.diff.line_no ?? "?"}`} ({thread.diff.base} → {thread.diff.target}):
              <code className={`diff-origin-line ${thread.diff.side}`}>{thread.diff.line_text.split("\n").map((l, i) => <span key={i} className="diff-origin-ln">{thread.diff!.side === "deleted" ? "−" : thread.diff!.side === "added" ? "+" : " "}{l}{"\n"}</span>)}</code>
              {!thread.diff.anchored && <span className="muted"> · this line is not in the current file; the discussion is attached to the document, not to a passage</span>}
            </div>
          )}
        </section>
        <section className="session-box">
          <span className="label">Session for this thread</span>
          <SessionChip session={lastSess} />
          {differs && <span className="chip warn">differs from the document session</span>}
          {lastSess && onSessionChanged && <ModelPicker session={lastSess} onChanged={onSessionChanged} compact />}
        </section>
        <section className="messages">
          {thread.messages.map((m) => (
            <MessageView key={m.id} m={m} session={m.session_id ? sessionsById[m.session_id] : undefined} live={m.id === liveMessageId ? liveJob : null} onRetry={(sid) => onRetry(thread.id, m.id, sid)} onCancel={onCancel} onPermission={onPermission} sessions={sessions} />
          ))}
          <div ref={bottomRef} />
        </section>
        {diff && (
          <section className="diff-box">
            <div className="label">
              Diff ({diff.from.slice(0, 8)} → current)
              <button className="icon-btn" onClick={() => setDiff(null)}>×</button>
            </div>
            {diff.identical ? <div className="muted small">No changes.</div> : <DiffView unified={diff.unified} />}
          </section>
        )}
      </div>
      <div className="composer">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={running ? "The agent is answering… you can keep typing" : `Type your message (${COMPOSER_HINT})`}
          onKeyDown={(e) => composerKeyDown(e, () => void send(), setText)}
          rows={3}
          data-testid="reply-input"
        />
        <div className="composer-row">
          <select value={sessionChoice} onChange={(e) => setSessionChoice(e.target.value)} data-testid="reply-session">
            <option value="__last__">Last session of this thread{lastSess ? ` · ${lastSess.provider} ${lastSess.short_id ?? lastSess.status}` : ""}</option>
            <option value="new">New session</option>
            {sessions.filter((s) => s.id !== thread.last_session_id && s.origin !== "preview").map((s) => (
              <option key={s.id} value={s.id}>
                {s.provider} · {s.display_name ?? s.short_id ?? s.id.slice(0, 8)} {s.status !== "ready" ? `(${s.status})` : ""}
              </option>
            ))}
          </select>
          <button className="btn primary" onClick={() => void send()} disabled={busy || !text.trim()} data-testid="reply-send">Send</button>
        </div>
        {error && <div className="error-text" role="alert">{error}</div>}
      </div>
    </aside>
  );
}

export function SessionChip({ session }: { session: AgentSession | undefined }) {
  if (!session) return <span className="chip">No session bound</span>;
  return (
    <span className={`chip session ${session.status}`} title={`${session.native_id ?? "No native session id from the provider yet"}${session.model ? `\nmodel: ${session.model}${session.variant ? ` (${session.variant})` : ""}` : "\nmodel: provider default"}`}>
      {session.provider} · {session.display_name ?? "session"} {session.short_id ? `#${session.short_id}` : `(${statusLabel(session.status)})`}
      {session.model && <span className="model-tag" data-testid="session-model-tag"> · {session.model.split("/").pop()}{session.variant ? ` (${session.variant})` : ""}</span>}
    </span>
  );
}

export function statusLabel(s: string): string {
  switch (s) {
    case "uninitialized":
      return "created on first question";
    case "ready":
      return "ready";
    case "missing":
      return "missing at provider";
    case "unavailable":
      return "unavailable";
    case "availability_unknown":
      return "availability unknown";
    default:
      return s;
  }
}

function AnchorBadge({ state }: { state: string }) {
  const label = { attached: "attached", moved: "moved", ambiguous: "ambiguous", orphaned: "orphaned", document: "whole document", page: "attached image" }[state] ?? state;
  return <span className={`chip anchor ${state}`} data-testid="anchor-state">{label}</span>;
}

function MessageView({ m, session, live, onRetry, onCancel, onPermission, sessions }: { m: Message; session: AgentSession | undefined; live: LiveJob | null; onRetry: (sid: string | null) => Promise<void>; onCancel: (jobId: string) => Promise<void>; onPermission: (jobId: string, requestId: string, decision: string) => Promise<void>; sessions: AgentSession[] }) {
  const [retrySession, setRetrySession] = useState<string>("__same__");
  const status = live ? live.status : m.status;
  const content = stripAnsi(live && live.text ? live.text : m.content);
  const isAgent = m.role === "agent";
  const failed = status === "failed" || status === "cancelled" || status === "interrupted";
  return (
    <div className={`msg ${m.role} ${status}`} data-testid={`message-${m.role}`} data-status={status}>
      <div className="msg-head">
        <span className="who">{m.role === "user" ? "You" : "Agent"}</span>
        {session && <span className="muted small">{session.provider} #{session.short_id ?? statusLabel(session.status)}</span>}
        {isAgent && status !== "completed" && <span className={`chip status ${status}`}>{jobStatusLabel(status)}</span>}
      </div>
      {content && (isAgent ? <MarkdownView text={content} className="msg-body" /> : <div className="msg-body">{content}</div>)}
      {isAgent && live && live.activity.length > 0 && status !== "completed" && (
        <ul className="activity">
          {live.activity.slice(-6).map((a, i) => (
            <li key={i}>{a}</li>
          ))}
        </ul>
      )}
      {isAgent && live?.permission && (
        <div className="permission-box" data-testid="permission-request">
          <div className="label">The agent requests a permission (the provider's own policy)</div>
          <div>{live.permission.description}</div>
          {live.permission.relay_supported ? (
            <div className="row">
              {(live.permission.options as string[]).map((opt) => (
                <button key={opt} className="btn small" onClick={() => void onPermission(live.jobId, live.permission!.request_id, opt)}>
                  {opt === "once" ? "Allow once" : opt === "always" ? "Always allow" : opt === "reject" ? "Reject" : opt}
                </button>
              ))}
            </div>
          ) : (
            <div className="warn small">{live.permission.message ?? "This mode cannot relay permission requests; answer in the terminal."}</div>
          )}
        </div>
      )}
      {isAgent && (status === "running" || status === "queued" || status === "waiting_for_session" || status === "waiting_provider_permission") && (m.job_id || live) && (
        <div className="row">
          {/* always cancel *this* message's job: the streamed job may lag behind when several turns are queued */}
          <button className="btn small" onClick={() => void onCancel(m.job_id ?? live!.jobId)} data-testid="cancel-job">Cancel</button>
        </div>
      )}
      {isAgent && failed && (
        <div className="fail-box">
          <div className="error-text">
            {(live?.error?.code ?? m.error_code) && <code>{live?.error?.code ?? m.error_code}</code>} {stripAnsi(live?.error?.message ?? m.error_message ?? (status === "cancelled" ? "cancelled" : "interrupted"))}
          </div>
          {live?.error?.hint && <div className="muted small">{live.error.hint}</div>}
          <div className="row">
            <select className="retry-select" value={retrySession} onChange={(e) => setRetrySession(e.target.value)}>
              <option value="__same__">Retry with the same session</option>
              <option value="new">Retry with a new session</option>
              {sessions.filter((s) => s.origin !== "preview").map((s) => (
                <option key={s.id} value={s.id}>{s.provider} · {s.display_name ?? s.short_id ?? s.id.slice(0, 8)}</option>
              ))}
            </select>
            <button className="btn small primary" onClick={() => void onRetry(retrySession === "__same__" ? null : retrySession)} data-testid="retry-job">Retry</button>
          </div>
        </div>
      )}
    </div>
  );
}

export function jobStatusLabel(s: string): string {
  switch (s) {
    case "queued":
      return "queued";
    case "waiting_for_session":
      return "waiting for session";
    case "running":
      return "generating…";
    case "waiting_provider_permission":
      return "waiting for permission decision";
    case "failed":
      return "failed";
    case "cancelled":
      return "cancelled";
    case "interrupted":
      return "interrupted (server restart)";
    default:
      return s;
  }
}


/** Flat mode: every discussion of the document in one chronological stream. Each thread is a group headed by its
 *  quoted passage; messages keep their thread/session. The composer continues the selected thread (default: the
 *  most recent one) so related questions can be asked without switching threads. */
function FlatPanel({ threads, sessions, sessionsById, defaultSessionId, liveJob, activeThreadId, onReply, onRetry, onCancel, onPermission, onSelectThread, onCollapse, resizer, modeSwitch, onSessionChanged, askBox }: {
  askBox?: React.ReactNode;
  threads: Thread[];
  sessions: AgentSession[];
  sessionsById: Record<string, AgentSession>;
  defaultSessionId: string | null;
  liveJob: LiveJob | null;
  activeThreadId: string | null;
  onReply: (threadId: string, text: string, sessionChoice: string | null) => Promise<void>;
  onRetry: (threadId: string, messageId: string, sessionChoice: string | null) => Promise<void>;
  onCancel: (jobId: string) => Promise<void>;
  onPermission: (jobId: string, requestId: string, decision: string) => Promise<void>;
  onSelectThread: (id: string | null) => void;
  onCollapse?: () => void;
  resizer?: React.ReactNode;
  modeSwitch: React.ReactNode;
  onSessionChanged?: (s: AgentSession) => void;
}) {
  const ordered = useMemo(() => [...threads].sort((a, b) => a.created_at.localeCompare(b.created_at)), [threads]);
  const newest = ordered.length ? ordered[ordered.length - 1] : null;
  const [targetId, setTargetId] = useState<string | null>(activeThreadId ?? newest?.id ?? null);
  useEffect(() => {
    // follow the thread the user opened from the document; otherwise stick to the newest
    if (activeThreadId && threads.some((t) => t.id === activeThreadId)) setTargetId(activeThreadId);
    else if (!threads.some((t) => t.id === targetId)) setTargetId(newest?.id ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeThreadId, threads.length]);
  const [text, setText] = useState("");
  const [sessionChoice, setSessionChoice] = useState<string>("__last__");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const totalMessages = threads.reduce((n, t) => n + t.messages.length, 0);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [totalMessages, liveJob?.text, liveJob?.status]);

  const target = threads.find((t) => t.id === targetId) ?? null;
  const targetSess = target?.last_session_id ? sessionsById[target.last_session_id] : undefined;

  const send = async () => {
    if (!text.trim() || !target) return;
    setBusy(true);
    setError(null);
    try {
      await onReply(target.id, text.trim(), sessionChoice === "__last__" ? null : sessionChoice);
      setText("");
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside className="discussion flat" data-testid="discussion-panel" data-mode="flat">
      {resizer}
      <div className="panel-head">
        <span>Discussions</span>
        <span className="muted">{threads.length} · {totalMessages} messages</span>
        <span className="spacer" />
        {modeSwitch}
        {onCollapse && <button className="icon-btn" onClick={onCollapse} title="Hide discussions panel" data-testid="panel-collapse">›</button>}
      </div>
      {askBox}
      <div className="thread-body flat-stream" data-testid="flat-stream">
        {ordered.length === 0 && <div className="muted pad">Ask above about the whole document, or select text in it to ask about a passage. In flat mode all discussions of this document appear here as one stream.</div>}
        {ordered.map((t, i) => {
          const live = liveJob && liveJob.threadId === t.id ? liveJob : null;
          const liveMessageId = t.messages.find((m) => m.job_id === live?.jobId)?.id;
          const differs = t.last_session_id && defaultSessionId && t.last_session_id !== defaultSessionId;
          return (
            <section key={t.id} className={`flat-group ${t.id === targetId ? "target" : ""}`} data-testid="flat-group">
              <header className="flat-group-head">
                <button className="flat-quote" onClick={() => { setTargetId(t.id); onSelectThread(t.id); }} title="Reply to this discussion and scroll the document to its passage">
                  <span className="flat-index">#{i + 1}</span>
                  <AnchorBadge state={t.anchor_state} />
                  <span className="quote-text">{quoteLabel(t, 140)}</span>
                </button>
                <span className="muted small">{new Date(t.created_at).toLocaleString()}{differs ? " · other session" : ""}</span>
              </header>
              {t.messages.map((m) => (
                <MessageView key={m.id} m={m} session={m.session_id ? sessionsById[m.session_id] : undefined} live={m.id === liveMessageId ? live : null} onRetry={(sid) => onRetry(t.id, m.id, sid)} onCancel={onCancel} onPermission={onPermission} sessions={sessions} />
              ))}
            </section>
          );
        })}
        <div ref={bottomRef} />
      </div>
      <div className="composer">
        {target ? (
          <div className="muted small flat-target" data-testid="flat-target">
            Replying to <b>#{ordered.findIndex((t) => t.id === target.id) + 1}</b> {quoteLabel(target, 50)}
            {ordered.length > 1 && (
              <select className="flat-target-select" value={target.id} onChange={(e) => setTargetId(e.target.value)} data-testid="flat-target-select" title="Choose which discussion to continue">
                {ordered.map((t, i) => (
                  <option key={t.id} value={t.id}>#{i + 1} {quoteLabel(t, 40)}</option>
                ))}
              </select>
            )}
            <span> · select text in the document to start a new one</span>
            {targetSess && onSessionChanged && <ModelPicker session={targetSess} onChanged={onSessionChanged} compact />}
          </div>
        ) : (
          <div className="muted small">Select text in the document to start the first discussion.</div>
        )}
        <textarea value={text} onChange={(e) => setText(e.target.value)} disabled={!target} placeholder={target ? `Continue the discussion (${COMPOSER_HINT})` : "No discussion yet"} onKeyDown={(e) => composerKeyDown(e, () => void send(), setText)} rows={3} data-testid="flat-reply-input" />
        <div className="composer-row">
          <select value={sessionChoice} onChange={(e) => setSessionChoice(e.target.value)} data-testid="flat-reply-session">
            <option value="__last__">This discussion's last session{targetSess ? ` · ${targetSess.provider} ${targetSess.short_id ?? targetSess.status}` : ""}</option>
            <option value="new">New session</option>
            {sessions.filter((s) => s.id !== target?.last_session_id && s.origin !== "preview").map((s) => (
              <option key={s.id} value={s.id}>{s.provider} · {s.display_name ?? s.short_id ?? s.id.slice(0, 8)} {s.status !== "ready" ? `(${s.status})` : ""}</option>
            ))}
          </select>
          <button className="btn primary" onClick={() => void send()} disabled={busy || !text.trim() || !target} data-testid="flat-reply-send">Send</button>
        </div>
        {error && <div className="error-text" role="alert">{error}</div>}
      </div>
    </aside>
  );
}

/** How a discussion is named in lists: its quoted passage, or "whole document". */
function targetLabel(t: Thread["target"]): string {
  if (!t) return "";
  const where = t.page ? `${t.unit === "slide" ? "Slide" : "Page"} ${t.page}` : "";
  if (t.kind === "page") return `📑 ${where}`;
  if (t.kind === "area") return `🔲 Area of ${where.toLowerCase()}`;
  if (t.kind === "image") return `🖼 Image${where ? ` · ${where.toLowerCase()}` : ""}`;
  if (!t.page && t.label) return `📝 ${t.label}`;
  return `📑 ${where}`;
}

function quoteLabel(t: { anchor_state: string; anchor: { quote: string }; target?: Thread["target"] }, max: number): string {
  if (t.anchor_state === "page") {
    const q = t.anchor.quote;
    return targetLabel(t.target) + (q ? ` · "${q.slice(0, max)}${q.length > max ? "…" : ""}"` : "");
  }
  if (t.anchor_state === "document" || !t.anchor.quote) return "📄 whole document";
  const q = t.anchor.quote;
  return `"${q.slice(0, max)}${q.length > max ? "…" : ""}"`;
}

/** "Ask about this document": a question box at the top of the panel, always about the whole open document. */
function AskDocument({ onAsk, format, focusKey = 0 }: { onAsk: (q: string) => Promise<void>; format?: string; focusKey?: number }) {
  const [q, setQ] = useState("");
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (focusKey) box.current?.focus();
  }, [focusKey]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const what = format === "pdf" ? "this PDF" : format === "image" ? "this image" : "this document";
  const send = async () => {
    if (!q.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onAsk(q.trim());
      setQ("");
    } catch (e) {
      const ae = e as ApiError;
      setError(ae.code === "DOCUMENT_VERSION_CONFLICT" ? "The document changed meanwhile; refresh it and send again (your question is kept)." : ae.message || "Send failed");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="ask-document" data-testid="ask-document">
      <textarea
        ref={box}
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => composerKeyDown(e, () => void send(), setQ, () => setQ(""))}
        placeholder={`Ask about ${what}… (${COMPOSER_HINT})`}
        rows={2}
        data-testid="ask-document-input"
        aria-label={`Ask about ${what}`}
      />
      <div className="ask-document-row">
        <span className="muted small">{format === "image" ? "The image is attached for the agent." : "The agent reads the whole file."} Select text to ask about a passage.</span>
        <button className="btn small primary" onClick={() => void send()} disabled={busy || !q.trim()} data-testid="ask-document-send">{busy ? "Sending…" : "Ask"}</button>
      </div>
      {error && <div className="error-text small" role="alert">{error}</div>}
    </div>
  );
}

/** Error text from agent CLIs may contain terminal colour codes ("\x1b[91mError:"); show plain text. */
function stripAnsi(s: string): string {
  // eslint-disable-next-line no-control-regex
  return s.replace(/\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]/g, "");
}
