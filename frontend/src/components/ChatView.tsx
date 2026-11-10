import { useEffect, useRef, useState } from "react";
import { AgentSession, ApiError, OpenDocument, Thread, del } from "../api";
import { LiveJob, MessageView } from "./DiscussionPanel";
import { confirmAction } from "./Confirm";
import { COMPOSER_HINT, composerKeyDown } from "./keys";

export const CHAT_PREFIX = ".waw-chat/";
export const isChatPath = (p: string | null | undefined) => !!p && p.startsWith(CHAT_PREFIX);

type Action = "distill" | "update";

/**
 * A folder chat: an open conversation with the agent about the wiki folder (no particular document). When it has
 * produced something worth keeping, **Distill into a page** writes a new wiki page from it, or **Update source
 * pages** folds what it established back into the pages its content came from; the edits are reviewed in Diff.
 */
export default function ChatView({ doc, folderName, threads, sessions, liveJob, onStart, onSend, onAction, onRetry, onCancel, onPermission, onReviewChanges, onArchived }: {
  doc: OpenDocument;
  folderName: string;
  threads: Thread[];
  sessions: AgentSession[];
  liveJob: LiveJob | null;
  onStart: (question: string) => Promise<void>;
  onSend: (threadId: string, text: string) => Promise<void>;
  onAction: (threadId: string, action: Action, target: string | null, note: string) => Promise<void>;
  onRetry: (threadId: string, messageId: string, sessionChoice: string | null) => Promise<void>;
  onCancel: (jobId: string) => Promise<void>;
  onPermission: (jobId: string, requestId: string, decision: string) => Promise<void>;
  onReviewChanges: () => void;
  onArchived: () => void;
}) {
  const thread = threads[0] ?? null; // a chat is one conversation
  const title = (doc.text.split("\n", 1)[0] || "Chat").replace(/^#\s*/, "");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [action, setAction] = useState<Action | null>(null);
  const [target, setTarget] = useState("");
  const [note, setNote] = useState("");
  const [acted, setActed] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const sessionsById = Object.fromEntries(sessions.map((s) => [s.id, s]));
  const live = liveJob && thread && liveJob.threadId === thread.id ? liveJob : null;
  const liveMessageId = thread?.messages.find((m) => m.job_id === liveJob?.jobId)?.id;
  const running = !!thread?.messages.some((m) => m.role === "agent" && ["queued", "running", "waiting_for_session"].includes(m.id === liveMessageId && live ? live.status : m.status));
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [thread?.messages.length, live?.text.length]);
  useEffect(() => {
    const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 50) || "notes";
    setTarget((t) => t || `${slug}.md`);
  }, [title]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const send = () => {
    const q = text.trim();
    if (!q || running) return;
    void run(async () => {
      if (thread) await onSend(thread.id, q);
      else await onStart(q);
      setText("");
    });
  };
  const doAction = () => {
    if (!thread || !action) return;
    void run(async () => {
      await onAction(thread.id, action, action === "distill" ? target.trim() : null, note.trim() || "-");
      setAction(null);
      setNote("");
      setActed(true);
    });
  };
  const archive = () => {
    void run(async () => {
      if (!(await confirmAction({ title: `Archive the chat "${title}"?`, message: "It leaves the folder's chat list. Its conversation is kept (and stays in Recent).", confirmLabel: "Archive chat" }))) return;
      await del(`/chats?path=${encodeURIComponent(doc.path)}`);
      onArchived();
    });
  };

  return (
    <div className="chat-view" data-testid="chat-view">
      <header className="chat-head">
        <div>
          <h2 className="chat-title" data-testid="chat-title">{title}</h2>
          <div className="muted small">Chat with the agent about <b>{folderName}</b> — it searches and reads the wiki to answer.</div>
        </div>
        <span className="spacer" />
        <button className="btn small" onClick={onReviewChanges} title="Review the folder's uncommitted changes (what a distill / update wrote)" data-testid="chat-review-changes">Review changes</button>
        <button className="btn small" onClick={archive} disabled={busy} data-testid="chat-archive">Archive</button>
      </header>
      <section className="chat-messages messages">
        {!thread && <div className="chat-empty muted">Ask anything about this wiki — questions across pages, comparisons, open issues. When the conversation is worth keeping, distill it into a page or fold it back into the pages it came from.</div>}
        {thread?.messages.map((m) => (
          <MessageView key={m.id} m={m} session={m.session_id ? sessionsById[m.session_id] : undefined} live={m.id === liveMessageId ? live : null}
            onRetry={(sid) => onRetry(thread.id, m.id, sid)} onCancel={onCancel} onPermission={onPermission} sessions={sessions} />
        ))}
        <div ref={bottom} />
      </section>
      {err && <div className="banner warn small" role="alert" data-testid="chat-error">{err}</div>}
      {acted && !running && (
        <div className="banner small" data-testid="chat-acted">
          The agent changed the wiki: <button className="link-btn" onClick={onReviewChanges}>review the changes</button> (keep, edit or revert them; commit when happy).
        </div>
      )}
      {thread && (
        <div className="chat-actions" data-testid="chat-actions">
          <span className="small strong">Turn into wiki content:</span>
          <label className="small"><input type="radio" name="chat-action" checked={action === "distill"} onChange={() => setAction("distill")} data-testid="chat-action-distill" /> Distill into a new page</label>
          <label className="small"><input type="radio" name="chat-action" checked={action === "update"} onChange={() => setAction("update")} data-testid="chat-action-update" /> Update the pages it came from</label>
          {action && (
            <div className="chat-action-box">
              {action === "distill" && (
                <label className="small">Page <input className="mono" value={target} onChange={(e) => setTarget(e.target.value)} placeholder="folder/page.md" data-testid="chat-target" /></label>
              )}
              <input value={note} onChange={(e) => setNote(e.target.value)} placeholder={action === "distill" ? "Optional: what to focus on, audience, structure…" : "Optional: which pages, what to leave alone…"} data-testid="chat-action-note" />
              <button className="btn small primary" disabled={busy || running || (action === "distill" && !/\.md$/i.test(target.trim()))} onClick={doAction} data-testid="chat-action-run">
                {action === "distill" ? "Distill" : "Update pages"}
              </button>
              <button className="btn small" onClick={() => setAction(null)}>Cancel</button>
            </div>
          )}
        </div>
      )}
      <div className="chat-composer">
        <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} disabled={busy}
          placeholder={`${thread ? "Continue the conversation" : `Ask about ${folderName}`} (${COMPOSER_HINT})`}
          onKeyDown={(e) => composerKeyDown(e, send, setText, () => setText(""))} data-testid="chat-input" autoFocus />
        <button className="btn primary" disabled={busy || running || !text.trim()} onClick={send} data-testid="chat-send">{running ? "Agent is answering…" : "Send"}</button>
      </div>
    </div>
  );
}
