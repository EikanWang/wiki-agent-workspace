import ModelChoice, { ModelChoiceValue } from "./ModelChoice";
import SkillsChip from "./SkillsChip";
import { useCallback, useEffect, useRef, useState } from "react";
import { AgentSession, ApiError, OpenDocument, Thread, del, getWorkspace } from "../api";
import DiscussionPanel, { LiveJob, MessageView } from "./DiscussionPanel";
import { Resizer } from "./Layout";
import SelectionAsk from "./SelectionAsk";
import ContextFolders from "./ContextFolders";
import { confirmAction } from "./Confirm";
import { usePersistentState } from "../persist";
import { COMPOSER_HINT, composerKeyDown } from "./keys";
import { PASTE_HINT, PictureStrip, picturesOnly, usePictures } from "./Pictures";

export const CHAT_PREFIX = ".waw-chat/";
export const isChatPath = (p: string | null | undefined) => !!p && p.startsWith(CHAT_PREFIX);

type Action = "distill" | "update";

/**
 * A folder chat: an open conversation with the agent about the wiki folder (no particular document). When it has
 * produced something worth keeping, **Distill into a page** writes a new wiki page from it, or **Update source
 * pages** folds what it established back into the pages its content came from; the edits are reviewed in Diff.
 * Selecting a part of an answer asks a **side question** about it: answered in the discussion panel on the right (its
 * own session; Individual / Flat like a document's discussions), so the chat keeps its focus; a distill / update
 * includes the side questions.
 */
export default function ChatView({ doc, folderName, threads, sessions, liveJob, sideLive = null, onStart, onSend, onAction, onRetry, onCancel, onPermission, onReviewChanges, onArchived, onWatchJob,
  onSideAsk, onSideReply, onWatchSideJob, workspaceId, trend }: {
  doc: OpenDocument;
  folderName: string;
  threads: Thread[];
  sessions: AgentSession[];
  liveJob: LiveJob | null;
  /** the running side question's turn */
  sideLive?: LiveJob | null;
  onStart: (question: string, modelChoice?: ModelChoiceValue | null, images?: string[]) => Promise<void>;
  onSend: (threadId: string, text: string, modelChoice?: ModelChoiceValue | null, images?: string[]) => Promise<void>;
  onAction: (threadId: string, action: Action, target: string | null, note: string) => Promise<void>;
  onRetry: (threadId: string, messageId: string, sessionChoice: string | null) => Promise<void>;
  onCancel: (jobId: string) => Promise<void>;
  onPermission: (jobId: string, requestId: string, decision: string) => Promise<void>;
  onReviewChanges: () => void;
  onArchived: () => void;
  /** follow a running turn (e.g. one started elsewhere: a change summary, or after a reload) */
  onWatchJob?: (jobId: string) => void;
  /** a side question about `quote` (a part of the answer `messageId` of the chat `mainId`): its thread's id */
  onSideAsk?: (mainId: string, messageId: string | null, quote: string, question: string, images?: string[]) => Promise<string | null>;
  onSideReply?: (threadId: string, text: string, sessionChoice: string | null, images?: string[]) => Promise<void>;
  onWatchSideJob?: (jobId: string) => void;
  /** the chat's project (folder) when it is not the window's active one */
  workspaceId?: string;
  /** a Trend chat about this repository (the agent reads its insights data; nothing to distill into the wiki) */
  trend?: string;
}) {
  const thread = threads.find((t) => t.target?.kind !== "side") ?? null; // a chat is one conversation…
  const sides = thread ? threads.filter((t) => t.target?.kind === "side" && t.target.of === thread.id) : []; // …and its side questions
  const [sideOpen, setSideOpen] = usePersistentState("chat.sideOpen", true);
  const [sideMode, setSideMode] = usePersistentState<"individual" | "flat">("chat.sideMode", "individual");
  const [sideWidth, setSideWidth] = usePersistentState("chat.sideWidth", 420);
  const resizeSide = useCallback((dx: number) => setSideWidth((w) => Math.min(900, Math.max(280, w - dx))), [setSideWidth]);
  const [activeSideId, setActiveSideId] = useState<string | null>(null);
  const activeSide = sides.find((t) => t.id === activeSideId) ?? null;
  const messagesRef = useRef<HTMLElement>(null);
  const title = (doc.text.split("\n", 1)[0] || "Chat").replace(/^#\s*/, "");
  const control = doc.chat_scope === "control_center";
  const [text, setText] = usePersistentState(`chat.${doc.document_id}`, ""); // an unsent message survives a reload
  const [busy, setBusy] = useState(false);
  const [refs, setRefs] = useState<"chat" | "repo" | null>(null);
  const insightsRepo = doc.insights_repo ?? trend ?? null;
  const [err, setErr] = useState<string | null>(null);
  const pics = usePictures();
  const [action, setAction] = useState<Action | null>(null);
  const [target, setTarget] = useState("");
  const [note, setNote] = useState("");
  const [acted, setActed] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const sessionsById = Object.fromEntries(sessions.map((s) => [s.id, s]));
  const live = liveJob && thread && liveJob.threadId === thread.id ? liveJob : null;
  const liveMessageId = thread?.messages.find((m) => m.job_id === liveJob?.jobId)?.id;
  const running = !!thread?.messages.some((m) => m.role === "agent" && ["queued", "running", "waiting_for_session"].includes(m.id === liveMessageId && live ? live.status : m.status));
  const pendingJob = thread?.messages.find((m) => m.role === "agent" && m.job_id && ["queued", "running", "waiting_provider_permission", "waiting_for_session"].includes(m.status))?.job_id ?? null;
  useEffect(() => {
    if (pendingJob && pendingJob !== liveJob?.jobId) onWatchJob?.(pendingJob);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingJob]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [thread?.messages.length, live?.text.length]);
  const sidePending = sides.flatMap((t) => t.messages).find((m) => m.role === "agent" && m.job_id && ["queued", "running", "waiting_provider_permission", "waiting_for_session"].includes(m.status))?.job_id ?? null;
  useEffect(() => {
    if (sidePending && sidePending !== sideLive?.jobId) onWatchSideJob?.(sidePending);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sidePending]);
  const showMessage = (id: string | null | undefined) => {
    const el = id ? messagesRef.current?.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(id)}"]`) : null;
    el?.scrollIntoView({ block: "center", behavior: "smooth" });
  };
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
  const [modelChoice, setModelChoice] = useState<ModelChoiceValue | null>(null);
  const chatSession = thread?.last_session_id ? sessions.find((x) => x.id === thread.last_session_id) ?? null : null;
  const send = () => {
    const q = text.trim() || (pics.pictures.length ? picturesOnly(pics.pictures.length) : "");
    if (!q || running) return;
    void run(async () => {
      if (thread) await onSend(thread.id, q, modelChoice, pics.urls);
      else await onStart(q, modelChoice, pics.urls);
      setModelChoice(null);
      setText("");
      pics.clear();
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
      await del(`/chats?path=${encodeURIComponent(doc.path)}`, workspaceId ? { workspace: workspaceId } : undefined);
      onArchived();
    });
  };

  return (
    <div className="chat-wrap">
    <div className="chat-view" data-testid="chat-view">
      <header className="chat-head">
        <div>
          <h2 className="chat-title" data-testid="chat-title">{title}</h2>
          <div className="small" data-testid="chat-scope-label">{control ? "Control Center · Read-only" : "Wiki"}</div>
          {control ? <div className="muted small" data-testid="chat-enabled-sources">{(doc.chat_sources ?? []).map((source) => ({ reviews: "Reviews", insights: "Insights", agents: "Agent jobs" }[source] ?? source)).join(" · ")}</div>
            : trend
            ? <div className="muted small">Chat with the agent about <b>{trend}</b> — it reads the daily summaries, reports and fetched issues / PRs to answer.</div>
            : <div className="muted small">Chat with the agent about <b>{folderName}</b> — it searches and reads the wiki to answer.</div>}
        </div>
        <span className="spacer" />
        {!control && <SkillsChip workspace={workspaceId ?? getWorkspace()} docId={doc.document_id} />}
        {!control && !trend && <button className="btn small" onClick={onReviewChanges} title="Review the folder's uncommitted changes (what a distill / update wrote)" data-testid="chat-review-changes">Review changes</button>}
        {!control && <button className="btn small" onClick={() => setRefs("chat")} title="Extra folders on the server the agent reads for this chat" data-testid="chat-refs">Reference folders</button>}
        <button className="btn small" onClick={archive} disabled={busy} data-testid="chat-archive">Archive</button>
      </header>
      {refs === "chat" && (
        <ContextFolders key="chat" scope={`chat:${workspaceId ?? getWorkspace()}:${doc.path}`} onClose={() => setRefs(null)}
          title={insightsRepo ? `For this discussion only (${insightsRepo}'s reference folders apply too).` : "For this chat only (the wiki folder's reference folders apply too)."}
          footer={insightsRepo ? <button className="link-btn small" onClick={() => setRefs("repo")} data-testid="chat-refs-repo">Folders for every {insightsRepo} discussion…</button> : undefined} />
      )}
      {refs === "repo" && insightsRepo && (
        <ContextFolders key="repo" scope={`insights:${insightsRepo}`} title={`For every discussion about ${insightsRepo}'s reports, and its trend chats.`} onClose={() => setRefs(null)}
          footer={<button className="link-btn small" onClick={() => setRefs("chat")}>Folders for this discussion only…</button>} />
      )}
      <div className="chat-main">
      <section className="chat-messages messages" ref={messagesRef}>
        {!thread && control && <div className="chat-empty muted">No messages yet.</div>}
        {!thread && trend && <div className="chat-empty muted">Ask anything about {trend}'s activity — e.g. summarize a period from its daily summaries and reports.</div>}
        {!thread && !control && !trend && <div className="chat-empty muted">Ask anything about this wiki — questions across pages, comparisons, open issues. When the conversation is worth keeping, distill it into a page or fold it back into the pages it came from.</div>}
        {thread?.messages.map((m) => (
          <MessageView key={m.id} m={m} session={m.session_id ? sessionsById[m.session_id] : undefined} live={m.id === liveMessageId ? live : null}
            onRetry={(sid) => onRetry(thread.id, m.id, sid)} onCancel={onCancel} onPermission={onPermission} sessions={sessions} />
        ))}
        <div ref={bottom} />
      </section>
      {thread && onSideAsk && (
        <SelectionAsk container={messagesRef} hint={control ? "Ask a side question" : trend ? "Answered in the side questions panel" : "Answered in the side questions panel; included when you distill"} testId="chat-sel-ask"
          onAsk={async (p, q, images) => {
            const id = await onSideAsk(thread.id, p.messageId, p.quote, q, images);
            if (id) setActiveSideId(id);
            setSideOpen(true);
          }} />
      )}
      {err && <div className="banner warn small" role="alert" data-testid="chat-error">{err}</div>}
      {acted && !running && (
        <div className="banner small" data-testid="chat-acted">
          The agent changed the wiki: <button className="link-btn" onClick={onReviewChanges}>review the changes</button> (keep, edit or revert them; commit when happy).
        </div>
      )}
      {thread && !control && !trend && (
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
        <PictureStrip pics={pics} />
        <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} disabled={busy}
          placeholder={`${thread ? "Continue the conversation" : `Ask about ${trend ?? folderName}`} (${COMPOSER_HINT}; ${PASTE_HINT})`}
          {...pics.handlers} onKeyDown={(e) => composerKeyDown(e, send, setText, () => setText(""))} data-testid="chat-input" autoFocus />
        <div className="chat-composer-bar">
          <ModelChoice value={modelChoice} onChange={setModelChoice} session={chatSession} />
          <span className="spacer" />
          <button className="btn primary" disabled={busy || running || (!text.trim() && !pics.pictures.length)} onClick={send} data-testid="chat-send">{running ? "Agent is answering…" : "Send"}</button>
        </div>
      </div>
      </div>
    </div>
      {sides.length > 0 && !sideOpen && (
        <button className="chat-side-tab" onClick={() => setSideOpen(true)} title="Show the side questions" data-testid="chat-side-expand">‹ Side questions ({sides.length})</button>
      )}
      {sides.length > 0 && sideOpen && (
        <div className="chat-side" style={{ width: sideWidth }} data-testid="chat-side">
          <DiscussionPanel side thread={activeSide} threads={sides} sessions={sessions} defaultSessionId={null} liveJob={sideLive}
            onReply={(tid, q, sc, images) => (onSideReply ? onSideReply(tid, q, sc, images) : onSend(tid, q, null, images))}
            onRetry={onRetry} onCancel={onCancel} onPermission={onPermission}
            onSelectThread={(id) => {
              setActiveSideId(id);
              const t = sides.find((x) => x.id === id);
              if (t) showMessage(t.target?.message_id);
            }}
            onShowQuote={(t) => showMessage(t.target?.message_id)}
            docId={null} onRequestReanchor={() => undefined} reanchorArmed={false}
            onCollapse={() => setSideOpen(false)} resizer={<Resizer side="left" onResize={resizeSide} />}
            mode={sideMode} onModeChange={setSideMode} />
        </div>
      )}
    </div>
  );
}
