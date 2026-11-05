import { useEffect, useMemo, useRef, useState } from "react";
import { AgentSession, ApiError, JobEvent, Message, Thread, get, post, subscribeJob } from "../api";
import DiffView from "./DiffView";
import MarkdownView from "./MarkdownView";

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
  docSha,
  onRequestReanchor,
  reanchorArmed,
  onCollapse,
  resizer,
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
  docSha: string | null;
  onRequestReanchor: () => void;
  reanchorArmed: boolean;
  onCollapse?: () => void;
  resizer?: React.ReactNode;
}) {
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

  if (!thread) {
    return (
      <aside className="discussion" data-testid="discussion-panel">
        {resizer}
        <div className="panel-head">
          <span>Discussions</span>
          <span className="muted">{threads.length} total</span>
          <span className="spacer" />
          {onCollapse && <button className="icon-btn" onClick={onCollapse} title="Hide discussions panel" data-testid="panel-collapse">›</button>}
        </div>
        <div className="thread-list">
          {threads.length === 0 && <div className="muted pad">Select text in the document to start a discussion.</div>}
          {threads.map((t, i) => (
            <button key={t.id} className="thread-item" onClick={() => onSelectThread(t.id)} data-testid="thread-item">
              <div className="thread-title">Discussion {i + 1} · {t.messages[0]?.content.slice(0, 50)}</div>
              <div className="thread-quote">"{t.anchor.quote.slice(0, 80)}"</div>
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
        <span>Discussion {idx + 1} / {threads.length}</span>
        <span className="spacer" />
        <button className="btn small" onClick={showDiff} title="Show the diff between the version this discussion was created on and the current file">View diff</button>
        {onCollapse && <button className="icon-btn" onClick={onCollapse} title="Hide discussions panel" data-testid="panel-collapse">›</button>}
      </div>
      <div className="thread-body">
        <section className="quote-box">
          <div className="label">Quoted passage <AnchorBadge state={thread.anchor_state} /></div>
          <blockquote data-testid="thread-quote">{thread.anchor.quote}</blockquote>
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
        </section>
        <section className="session-box">
          <span className="label">Session for this thread</span>
          <SessionChip session={lastSess} />
          {differs && <span className="chip warn">differs from the document session</span>}
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
          placeholder={running ? "The agent is answering… you can keep typing" : "Type your message (Ctrl/⌘+Enter to send)"}
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === "Enter") void send();
          }}
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
    <span className={`chip session ${session.status}`} title={session.native_id ?? "No native session id from the provider yet"}>
      {session.provider} · {session.display_name ?? "session"} {session.short_id ? `#${session.short_id}` : `(${statusLabel(session.status)})`}
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
  const label = { attached: "attached", moved: "moved", ambiguous: "ambiguous", orphaned: "orphaned" }[state] ?? state;
  return <span className={`chip anchor ${state}`} data-testid="anchor-state">{label}</span>;
}

function MessageView({ m, session, live, onRetry, onCancel, onPermission, sessions }: { m: Message; session: AgentSession | undefined; live: LiveJob | null; onRetry: (sid: string | null) => Promise<void>; onCancel: (jobId: string) => Promise<void>; onPermission: (jobId: string, requestId: string, decision: string) => Promise<void>; sessions: AgentSession[] }) {
  const [retrySession, setRetrySession] = useState<string>("__same__");
  const status = live ? live.status : m.status;
  const content = live && live.text ? live.text : m.content;
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
      {isAgent && (status === "running" || status === "queued" || status === "waiting_for_session" || status === "waiting_provider_permission") && live && (
        <div className="row">
          <button className="btn small" onClick={() => void onCancel(live.jobId)} data-testid="cancel-job">Cancel</button>
        </div>
      )}
      {isAgent && failed && (
        <div className="fail-box">
          <div className="error-text">
            {(live?.error?.code ?? m.error_code) && <code>{live?.error?.code ?? m.error_code}</code>} {live?.error?.message ?? m.error_message ?? (status === "cancelled" ? "cancelled" : "interrupted")}
          </div>
          {live?.error?.hint && <div className="muted small">{live.error.hint}</div>}
          <div className="row">
            <select value={retrySession} onChange={(e) => setRetrySession(e.target.value)}>
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
