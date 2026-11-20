import { useCallback, useEffect, useState } from "react";
import type React from "react";
import { AgentSession, ApiError, OpenDocument, Thread, get, getIn, newIdempotencyKey, post } from "../api";
import { wholeDocument } from "../textmodel";
import ChatView from "./ChatView";
import { useJobStream } from "./DiscussionPanel";
import ModelChoice, { ModelChoiceValue } from "./ModelChoice";
import { COMPOSER_HINT, composerKeyDown } from "./keys";
import { usePersistentState } from "../persist";
import { PASTE_HINT, PictureStrip, picturesOnly, usePictures } from "./Pictures";
import { utc8 } from "../time";

interface TrendChatItem { path: string; document_id: string; workspace_id: string; title: string; messages: number; updated_at: string | null }

const day = (offset: number) => new Date(Date.now() - offset * 86400_000).toISOString().slice(0, 10);

/**
 * The Chat tab of a Trend repository: conversations with the agent about the repository's insights data (it works in
 * the data folder and reads the daily summaries, reports, highlights and fetched issues / PRs). Left: the chats; right:
 * a new chat (with a period helper) or the conversation.
 */
export default function TrendChat({ repo, tabs, sessions }: { repo: string; tabs: React.ReactNode; sessions: AgentSession[] }) {
  const [chats, setChats] = useState<TrendChatItem[]>([]);
  const [openPath, setOpenPath] = usePersistentState<string | null>(`trend.chat.${repo}`, null);
  const open = chats.find((c) => c.path === openPath) ?? null;
  const [doc, setDoc] = useState<OpenDocument | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [jobId, setJobId] = useState<string | null>(null);
  const [sideJobId, setSideJobId] = useState<string | null>(null);
  const [first, setFirst] = usePersistentState(`trend.chat.new.${repo}`, "");
  const firstPics = usePictures();
  const [firstModel, setFirstModel] = useState<ModelChoiceValue | null>(null);
  const [since, setSince] = useState(() => day(8));
  const [until, setUntil] = useState(() => day(1));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const loadList = useCallback(async () => {
    try {
      setChats((await get<{ chats: TrendChatItem[] }>(`/insights/repos/${repo}/chats`)).chats);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setLoaded(true);
    }
  }, [repo]);
  useEffect(() => { void loadList(); }, [loadList]);

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
      const c = await post<TrendChatItem>(`/insights/repos/${repo}/chats`, { title });
      const d = await getIn<OpenDocument>(c.workspace_id, `/documents/open?path=${encodeURIComponent(c.path)}`);
      const r = await post<{ job_id: string }>(`/documents/${d.document_id}/threads`,
        { ...wholeDocument(), version_sha256: d.sha256, question: q, model_choice: firstModel ?? undefined, images: firstPics.urls },
        { idempotencyKey: newIdempotencyKey(), workspace: c.workspace_id });
      setFirst("");
      firstPics.clear();
      setFirstModel(null);
      await loadList();
      setOpenPath(c.path);
      setJobId(r.job_id);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const ask = (text: string) => setFirst((t) => (t.trim() ? `${t.trimEnd()}\n${text}` : text));
  const periodAsk = `Summarize all the reports and daily summaries of ${repo} from ${since} to ${until}: the main themes, the most important issues / PRs / RFCs (with links), and what changed over the period.`;

  return (
    <>
      <div className="insights-list">
        {tabs}
        <button className={`btn small ${open ? "" : "primary"}`} onClick={() => setOpenPath(null)} data-testid="trend-chat-new">＋ New chat</button>
        <div className="insights-items" data-testid="trend-chat-list">
          {loaded && chats.length === 0 && <div className="muted small pad">No chat yet.</div>}
          {chats.map((c) => (
            <button key={c.path} className={`list-item ${c.path === openPath ? "selected" : ""}`} onClick={() => setOpenPath(c.path)} data-testid="trend-chat" data-path={c.path}>
              <span className="insights-item-main"><b>{c.title}</b>
                <span className="muted small">{c.messages} messages{c.updated_at ? ` · ${utc8(c.updated_at, false)}` : ""}</span></span>
            </button>
          ))}
        </div>
      </div>
      <div className="insights-main trend-chat-main">
        {err && <div className="banner warn small" role="alert">{err}</div>}
        {!open && (
          <div className="chat-new" data-testid="trend-chat-new-page">
            <h2>Chat about {repo}</h2>
            <div className="muted small">
              The agent reads this repository's Trend data — daily summaries, reports, XPU highlights and the fetched issues / pull
              requests — to answer: summarize any period, compare weeks, follow a topic or a person, find what changed.
            </div>
            <div className="row trend-chat-period">
              <span className="small">Period</span>
              <input type="date" value={since} max={until} onChange={(e) => setSince(e.target.value)} data-testid="trend-chat-since" />
              <span className="small">to</span>
              <input type="date" value={until} min={since} onChange={(e) => setUntil(e.target.value)} data-testid="trend-chat-until" />
              <button className="btn small" disabled={!since || !until || since > until} onClick={() => ask(periodAsk)} data-testid="trend-chat-period-ask">Summarize this period</button>
              <span className="spacer" />
              <button className="link-btn small" onClick={() => ask(`What were the most discussed topics in ${repo} over the last 30 days?`)}>Top topics, 30 days</button>
              <button className="link-btn small" onClick={() => ask(`Which RFCs and design discussions in ${repo} are still open and active?`)}>Open RFCs</button>
            </div>
            <div className="chat-composer">
              <PictureStrip pics={firstPics} />
              <textarea autoFocus rows={5} value={first} onChange={(e) => setFirst(e.target.value)} placeholder={`Ask anything about ${repo} (${COMPOSER_HINT}; ${PASTE_HINT})`}
                {...firstPics.handlers} onKeyDown={(e) => composerKeyDown(e, () => void startNew(), setFirst)} data-testid="trend-chat-first" />
              <div className="chat-composer-bar">
                <ModelChoice value={firstModel} onChange={setFirstModel} />
                <span className="spacer" />
                <button className="btn primary" disabled={busy || (!first.trim() && !firstPics.pictures.length)} onClick={() => void startNew()} data-testid="trend-chat-send">{busy ? "Starting…" : "Send"}</button>
              </div>
            </div>
          </div>
        )}
        {open && doc && (
          <ChatView
            key={doc.document_id}
            doc={doc}
            folderName={repo}
            trend={repo}
            threads={threads}
            sessions={sessions}
            liveJob={live}
            workspaceId={open.workspace_id}
            onStart={async (q, mc, images) => {
              const r = await post<{ job_id: string }>(`/documents/${doc.document_id}/threads`, { ...wholeDocument(), version_sha256: doc.sha256, question: q, model_choice: mc ?? undefined, images },
                { idempotencyKey: newIdempotencyKey(), workspace: ws });
              await after(r.job_id);
            }}
            onSend={async (tid, text, mc, images) => {
              const r = await post<{ job_id: string }>(`/threads/${tid}/messages`, { text, model_choice: mc ?? undefined, images }, { idempotencyKey: newIdempotencyKey() });
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
      </div>
    </>
  );
}
