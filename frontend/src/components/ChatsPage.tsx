import { useCallback, useEffect, useState } from "react";
import { AgentSession, ApiError, OpenDocument, Thread, WorkspaceInfo, getIn, newIdempotencyKey, post } from "../api";
import { wholeDocument } from "../textmodel";
import ChatView from "./ChatView";
import { useJobStream } from "./DiscussionPanel";
import { COMPOSER_HINT, composerKeyDown } from "./keys";
import { usePersistentState } from "../persist";

interface ChatItem { path: string; title: string; messages: number; updated_at: string | null }
export interface ChatTarget { ws: string; path: string | null } // path null: a new chat (nothing sent yet)

/**
 * The Chats page: talk with a project (a folder of this window) directly — pick the project and type; the first
 * message starts a chat. Left: every project's chats; right: the conversation, with Distill / Update.
 */
export default function ChatsPage({ workspaces, target, onTarget, sessions, onReviewChanges, onNotify }: {
  workspaces: WorkspaceInfo[];
  target: ChatTarget | null;
  onTarget: (t: ChatTarget) => void;
  sessions: AgentSession[];
  onReviewChanges: (ws: string) => void;
  onNotify: (m: string) => void;
}) {
  const projects = workspaces.filter((w) => w.available);
  const ws = target && projects.some((w) => w.id === target.ws) ? target.ws : projects[0]?.id ?? null;
  const [lists, setLists] = useState<Record<string, ChatItem[]>>({});
  const [doc, setDoc] = useState<OpenDocument | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [jobId, setJobId] = useState<string | null>(null);
  const [first, setFirst] = usePersistentState(`chat.new.${ws ?? ""}`, "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const loadList = useCallback(async (id: string) => {
    try {
      const r = await getIn<{ chats: ChatItem[] }>(id, "/chats");
      setLists((l) => ({ ...l, [id]: r.chats }));
    } catch {
      /* the folder may be gone */
    }
  }, []);
  useEffect(() => {
    for (const w of projects) void loadList(w.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projects.map((w) => w.id).join(",")]);

  const loadThreads = useCallback(async (d: OpenDocument, id: string) => {
    const r = await getIn<{ threads: Thread[] }>(id, `/documents/${d.document_id}/threads`);
    setThreads(r.threads);
  }, []);
  useEffect(() => {
    setDoc(null);
    setThreads([]);
    setErr(null);
    if (!ws || !target?.path) return;
    let alive = true;
    getIn<OpenDocument>(ws, `/documents/open?path=${encodeURIComponent(target.path)}`)
      .then(async (d) => {
        if (!alive) return;
        setDoc(d);
        await loadThreads(d, ws);
      })
      .catch((e) => alive && setErr((e as ApiError).message));
    return () => { alive = false; };
  }, [ws, target?.path, loadThreads]);

  const live = useJobStream(jobId, () => {
    if (doc && ws) {
      void loadThreads(doc, ws);
      void loadList(ws);
    }
  });

  const startNew = async () => {
    const q = first.trim();
    if (!q || !ws) return;
    setBusy(true);
    setErr(null);
    try {
      const title = q.replace(/\s+/g, " ").slice(0, 60) + (q.length > 60 ? "…" : "");
      const c = await post<{ path: string }>("/chats", { title }, { workspace: ws });
      const d = await getIn<OpenDocument>(ws, `/documents/open?path=${encodeURIComponent(c.path)}`);
      const r = await post<{ job_id: string }>(`/documents/${d.document_id}/threads`, { ...wholeDocument(), version_sha256: d.sha256, question: q },
        { idempotencyKey: newIdempotencyKey(), workspace: ws });
      setFirst("");
      onTarget({ ws, path: c.path });
      setJobId(r.job_id);
      void loadList(ws);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const after = async (jid: string) => {
    if (doc && ws) await loadThreads(doc, ws);
    setJobId(jid);
  };
  const project = projects.find((w) => w.id === ws);

  return (
    <div className="chats-page" data-testid="chats-page">
      <div className="chats-bar" data-testid="chats-bar">
        <label className="small">Project{" "}
          <select value={ws ?? ""} onChange={(e) => onTarget({ ws: e.target.value, path: null })} data-testid="chat-project" title={project?.root}>
            {projects.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
          </select>
        </label>
        <label className="small">Chat{" "}
          <select value={target?.path ?? ""} onChange={(e) => ws && onTarget({ ws, path: e.target.value || null })} data-testid="chat-history">
            <option value="">＋ New chat</option>
            {(ws ? lists[ws] ?? [] : []).map((c) => <option key={c.path} value={c.path}>{c.title} ({c.messages})</option>)}
          </select>
        </label>
        {target?.path && <button className="btn small" onClick={() => ws && onTarget({ ws, path: null })} data-testid="chats-new">＋ New chat</button>}
        <span className="muted small chats-root">{project?.root}</span>
      </div>
      <main className="chats-main">
        {err && <div className="banner warn small" role="alert">{err}</div>}
        {ws && !target?.path && (
          <div className="chat-new" data-testid="chat-new-page">
            <h2>Chat with {project?.name ?? "a project"}</h2>
            <div className="muted small">
              An open conversation with the agent about the whole project — it searches and reads the files to answer and cites them.
              When the conversation is worth keeping, distill it into a page or fold it back into the pages it came from.
            </div>
            <div className="chat-composer">
              <textarea autoFocus rows={4} value={first} onChange={(e) => setFirst(e.target.value)} placeholder={`Ask anything about ${project?.name ?? "the project"} (${COMPOSER_HINT})`}
                onKeyDown={(e) => composerKeyDown(e, () => void startNew(), setFirst)} data-testid="chat-first" />
              <button className="btn primary" disabled={busy || !first.trim()} onClick={() => void startNew()} data-testid="chat-first-send">{busy ? "Starting…" : "Send"}</button>
            </div>
          </div>
        )}
        {ws && target?.path && doc && (
          <ChatView
            doc={doc}
            folderName={project?.name ?? ""}
            threads={threads}
            sessions={sessions}
            liveJob={live}
            onStart={async (q) => {
              const r = await post<{ job_id: string }>(`/documents/${doc.document_id}/threads`, { ...wholeDocument(), version_sha256: doc.sha256, question: q }, { idempotencyKey: newIdempotencyKey(), workspace: ws });
              await after(r.job_id);
            }}
            onSend={async (tid, text) => {
              const r = await post<{ job_id: string }>(`/threads/${tid}/messages`, { text }, { idempotencyKey: newIdempotencyKey() });
              await after(r.job_id);
            }}
            onAction={async (tid, action, tgt, note) => {
              const r = await post<{ job_id: string }>(`/threads/${tid}/messages`, { text: note, chat_action: action, target_path: tgt ?? undefined }, { idempotencyKey: newIdempotencyKey() });
              await after(r.job_id);
            }}
            onRetry={async (tid, mid, sid) => {
              const r = await post<{ job_id: string }>(`/threads/${tid}/retry`, { message_id: mid, session_id: sid ?? undefined });
              await after(r.job_id);
            }}
            onCancel={async (j) => { await post(`/jobs/${j}/cancel`); }}
            onPermission={async (j, rid, decision) => { await post(`/jobs/${j}/permission-decisions`, { request_id: rid, decision }); }}
            onReviewChanges={() => onReviewChanges(ws)}
            onArchived={() => {
              onNotify("Chat archived.");
              onTarget({ ws, path: null });
              void loadList(ws);
            }}
            onWatchJob={(j) => setJobId(j)}
            workspaceId={ws}
          />
        )}
      </main>
    </div>
  );
}
