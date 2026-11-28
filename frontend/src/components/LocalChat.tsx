import { useCallback, useEffect, useState } from "react";
import { AgentSession, ApiError, OpenDocument, Thread, get, getIn, newIdempotencyKey, post } from "../api";
import { wholeDocument } from "../textmodel";
import ChatView from "./ChatView";
import { NamedFileIcon } from "./FileIcon";
import { useJobStream } from "./DiscussionPanel";
import ModelChoice, { ModelChoiceValue } from "./ModelChoice";
import { COMPOSER_HINT, composerKeyDown } from "./keys";
import { usePersistentState } from "../persist";
import { PASTE_HINT, PictureStrip, picturesOnly, usePictures } from "./Pictures";
import { utc8 } from "../time";
import SessionPicker from "./SessionPicker";

export interface LocalChatTarget { path: string; kind: "folder" | "file" }
interface LocalChatItem { path: string; document_id: string; workspace_id: string; title: string; messages: number; updated_at: string | null }

const sessionLabel = (s: AgentSession) => `${s.provider} · ${s.display_name ?? "session"}${s.short_id ? ` #${s.short_id}` : ""}`;

/**
 * Chat with the agent about a folder or a file of Local files, like a wiki folder chat: the agent works in the opened
 * folder holding it, reads the files to answer and edits them when asked. Shown next to the Local files editor; the
 * chats about this path can be picked at the top (or a new one started). An existing agent session (of the app, or a
 * provider's such as a terminal conversation) can be picked to continue: the chat's next message goes to it.
 */
export default function LocalChat({ target, sessions, onClose, onSessionsChanged }: {
  target: LocalChatTarget; sessions: AgentSession[]; onClose: () => void; onSessionsChanged?: () => Promise<void>;
}) {
  const [chats, setChats] = useState<LocalChatItem[]>([]);
  const [openPath, setOpenPath] = usePersistentState<string | null>(`local.chat.${target.path}`, null);
  const open = chats.find((c) => c.path === openPath) ?? null;
  const [doc, setDoc] = useState<OpenDocument | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [jobId, setJobId] = useState<string | null>(null);
  const [sideJobId, setSideJobId] = useState<string | null>(null);
  const [first, setFirst] = usePersistentState(`local.chat.new.${target.path}`, "");
  const firstPics = usePictures();
  const [firstModel, setFirstModel] = useState<ModelChoiceValue | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [session, setSession] = useState<AgentSession | null>(null); // the session the next message continues
  const [picking, setPicking] = useState(false);
  const name = target.path.split("/").pop() || target.path;

  const loadList = useCallback(async () => {
    try {
      setChats((await get<{ chats: LocalChatItem[] }>(`/local/chats?path=${encodeURIComponent(target.path)}`)).chats);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setLoaded(true);
    }
  }, [target.path]);
  useEffect(() => { setLoaded(false); setErr(null); void loadList(); }, [loadList]);

  const loadThreads = useCallback(async (d: OpenDocument, ws: string) => {
    setThreads((await getIn<{ threads: Thread[] }>(ws, `/documents/${d.document_id}/threads`)).threads);
  }, []);
  useEffect(() => {
    setDoc(null);
    setThreads([]);
    if (!open) return;
    let alive = true;
    getIn<OpenDocument>(open.workspace_id, `/documents/open?path=${encodeURIComponent(open.path)}`)
      .then(async (d) => {
        if (!alive) return;
        setDoc(d);
        await loadThreads(d, open.workspace_id);
      })
      .catch((e) => alive && setErr((e as ApiError).message));
    return () => { alive = false; };
  }, [open?.path, open?.workspace_id, loadThreads]); // eslint-disable-line react-hooks/exhaustive-deps

  const ws = open?.workspace_id ?? "";
  const live = useJobStream(jobId, () => {
    if (doc && ws) void loadThreads(doc, ws);
    void loadList();
  });
  const sideLive = useJobStream(sideJobId, () => {
    if (doc && ws) void loadThreads(doc, ws);
  });
  const after = async (jid: string) => {
    if (doc && ws) await loadThreads(doc, ws);
    setJobId(jid);
  };

  const startNew = async () => {
    const q = first.trim() || (firstPics.pictures.length ? picturesOnly(firstPics.pictures.length) : "");
    if (!q) return;
    setBusy(true);
    setErr(null);
    try {
      const title = q.replace(/\s+/g, " ").slice(0, 60) + (q.length > 60 ? "…" : "");
      const c = await post<LocalChatItem>("/local/chats", { path: target.path, title });
      const d = await getIn<OpenDocument>(c.workspace_id, `/documents/open?path=${encodeURIComponent(c.path)}`);
      const r = await post<{ job_id: string }>(`/documents/${d.document_id}/threads`,
        { ...wholeDocument(), version_sha256: d.sha256, question: q, model_choice: firstModel ?? undefined, images: firstPics.urls, session_id: session?.id },
        { idempotencyKey: newIdempotencyKey(), workspace: c.workspace_id });
      setFirst("");
      firstPics.clear();
      setFirstModel(null);
      setSession(null);
      await loadList();
      setOpenPath(c.path);
      setJobId(r.job_id);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="local-chat" data-testid="local-chat" data-path={target.path}>
      <div className="local-chat-bar">
        <span className="local-chat-about mono" title={target.path}><NamedFileIcon name={target.path} dir={target.kind === "folder"} /> {name}</span>
        <select value={open?.path ?? ""} onChange={(e) => setOpenPath(e.target.value || null)} title="Chats about this path" data-testid="local-chat-pick">
          <option value="">＋ New chat</option>
          {chats.map((c) => <option key={c.path} value={c.path}>{c.title} ({c.messages}{c.updated_at ? ` · ${utc8(c.updated_at, false)}` : ""})</option>)}
        </select>
        <span className="spacer" />
        {open && doc && <button className="icon-btn small" onClick={() => setPicking(true)} title="Continue in an existing agent session" data-testid="local-chat-session">🔗</button>}
        <button className="icon-btn small" onClick={onClose} title="Close the chat" aria-label="Close the chat" data-testid="local-chat-close">×</button>
      </div>
      {err && <div className="banner warn small" role="alert">{err} <button className="icon-btn small" onClick={() => setErr(null)}>×</button></div>}
      {open && session && (
        <div className="banner small" data-testid="local-chat-session-next">
          <span>The next message continues the session <b>{sessionLabel(session)}</b> (with this conversation so far).</span>
          <button className="icon-btn small" onClick={() => setSession(null)} title="Keep the chat's own session" data-testid="local-chat-session-clear">×</button>
        </div>
      )}
      {!open && (
        <div className="chat-new" data-testid="local-chat-new">
          <h2>Chat about <span className="mono">{name}</span></h2>
          <div className="muted small">
            An open conversation with the agent about the {target.kind} <b className="mono">{target.path}</b>. It works in the
            opened folder that holds it, reads the files to answer and cites them; ask for a change and it edits the files
            (they show up in the editor).
          </div>
          {loaded && chats.length > 0 && <div className="muted small">{chats.length} earlier chat{chats.length === 1 ? "" : "s"} about it — pick one above to continue.</div>}
          <div className="chat-composer">
            <PictureStrip pics={firstPics} />
            <textarea autoFocus rows={5} value={first} onChange={(e) => setFirst(e.target.value)}
              placeholder={`Ask anything about ${name} (${COMPOSER_HINT}; ${PASTE_HINT})`}
              {...firstPics.handlers} onKeyDown={(e) => composerKeyDown(e, () => void startNew(), setFirst)} data-testid="local-chat-first" />
            <div className="chat-composer-bar">
              <ModelChoice value={firstModel} onChange={setFirstModel} session={session} />
              {session ? (
                <span className="chip" title="This chat continues that agent session (it keeps what was discussed there)" data-testid="local-chat-session-chosen">
                  ↪ {sessionLabel(session)} <button className="chip-btn" onClick={() => setSession(null)} title="Start a new session instead" data-testid="local-chat-session-clear">×</button>
                </span>
              ) : (
                <button className="btn small" onClick={() => setPicking(true)} title="Continue an existing agent session (of this app, or a provider's, e.g. a terminal conversation) instead of starting a new one" data-testid="local-chat-session">Continue a session…</button>
              )}
              <span className="spacer" />
              <button className="btn primary" disabled={busy || (!first.trim() && !firstPics.pictures.length)} onClick={() => void startNew()} data-testid="local-chat-send">{busy ? "Starting…" : "Send"}</button>
            </div>
          </div>
        </div>
      )}
      {open && doc && (
        <ChatView
          key={doc.document_id}
          doc={doc}
          folderName={name}
          local={target.path}
          threads={threads}
          sessions={sessions}
          liveJob={live}
          workspaceId={open.workspace_id}
          onStart={async (q, mc, images) => {
            const r = await post<{ job_id: string }>(`/documents/${doc.document_id}/threads`, { ...wholeDocument(), version_sha256: doc.sha256, question: q, model_choice: mc ?? undefined, images, session_id: session?.id },
              { idempotencyKey: newIdempotencyKey(), workspace: ws });
            setSession(null);
            await after(r.job_id);
          }}
          onSend={async (tid, text, mc, images) => {
            const r = await post<{ job_id: string }>(`/threads/${tid}/messages`, { text, model_choice: mc ?? undefined, images, session_id: session?.id }, { idempotencyKey: newIdempotencyKey() });
            setSession(null);
            await after(r.job_id);
          }}
          onAction={async () => undefined}
          onRetry={async (tid, mid, sid) => {
            const r = await post<{ job_id: string }>(`/threads/${tid}/retry`, { message_id: mid, session_id: sid ?? undefined });
            await after(r.job_id);
          }}
          onCancel={async (j) => { await post(`/jobs/${j}/cancel`); }}
          onPermission={async (j, rid, decision) => { await post(`/jobs/${j}/permission-decisions`, { request_id: rid, decision }); }}
          onReviewChanges={() => undefined}
          onArchived={() => {
            setOpenPath(null);
            void loadList();
          }}
          onWatchJob={(j) => setJobId(j)}
          sideLive={sideLive}
          onSideAsk={async (mainId, messageId, quote, question, images) => {
            const r = await post<{ job_id: string; thread_id: string }>(`/documents/${doc.document_id}/threads`, { ...wholeDocument(), version_sha256: doc.sha256, quote, question,
              side_of: mainId, side_message_id: messageId ?? undefined, images }, { idempotencyKey: newIdempotencyKey(), workspace: ws });
            await loadThreads(doc, ws);
            setSideJobId(r.job_id);
            return r.thread_id;
          }}
          onSideReply={async (tid, text, sc, images) => {
            const r = await post<{ job_id: string }>(`/threads/${tid}/messages`, { text, session_id: sc ?? undefined, images }, { idempotencyKey: newIdempotencyKey() });
            await loadThreads(doc, ws);
            setSideJobId(r.job_id);
          }}
          onWatchSideJob={(j) => setSideJobId(j)}
        />
      )}
      {picking && (
        <SessionPicker title="Continue an agent session" defaultSessionId={session?.id ?? null} sessions={sessions} defaultProvider={null}
          hint={`Pick the session to continue: ${open ? "the next message of this chat" : "this chat"} goes to it, so the agent keeps what was discussed there — and works on ${name}.`}
          onChanged={async () => { await onSessionsChanged?.(); }} onPick={(s) => { setSession(s); setErr(null); }} onClose={() => setPicking(false)} />
      )}
    </div>
  );
}
