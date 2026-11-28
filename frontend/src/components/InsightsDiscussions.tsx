import { useCallback, useEffect, useRef, useState } from "react";
import type React from "react";
import { AgentSession, ApiError, Thread, get, getIn, newIdempotencyKey, post } from "../api";
import DiscussionPanel, { useJobStream } from "./DiscussionPanel";
import { Resizer } from "./Layout";
import { usePersistentState } from "../persist";

interface DiscussionDoc { workspace_id: string; path: string; document_id: string }
export type Started = DiscussionDoc & { thread_id: string; job_id: string };
/** what starts a discussion about the material: its quote / question / folders (the server adds the material) */
export type DiscussBody = { quote?: string; question?: string; dirs?: string[] };

const PENDING = ["queued", "running", "waiting_provider_permission", "waiting_for_session"];

/**
 * The discussions about one piece of Repo Insights (a report, a daily summary, the highlights of a day…), next to it
 * like a wiki page's discussion panel: each "Discuss this" (or a question about all of it) is one more discussion in
 * the panel, and the material stays in view. `key` names the material (null: nothing to discuss yet); `start` starts a
 * discussion on the server (in the document the material's discussions share).
 */
export function useInsightsDiscussion(repo: string, key: string | null, sessions: AgentSession[], start: (body: DiscussBody & { discussion: string }) => Promise<Started>,
  /** the panel's bar can also fold what is on its left (the report / summary) */
  foldOther?: { label: string; onFold: () => void }) {
  const [doc, setDoc] = useState<DiscussionDoc | null>(null);
  const docRef = useRef<DiscussionDoc | null>(null);
  docRef.current = doc;
  const [threads, setThreads] = useState<Thread[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = usePersistentState("insights.discOpen", true);
  const [mode, setMode] = usePersistentState<"individual" | "flat">("insights.discMode", "individual");
  const [width, setWidth] = usePersistentState("insights.discWidth", 420);
  const resize = useCallback((dx: number) => setWidth((w) => Math.min(900, Math.max(280, w - dx))), [setWidth]);

  const loadThreads = useCallback(async (d: DiscussionDoc) => {
    const t = (await getIn<{ threads: Thread[] }>(d.workspace_id, `/documents/${d.document_id}/threads`)).threads;
    if (docRef.current?.document_id !== d.document_id) return;
    setThreads(t);
    // follow a turn that is still running (e.g. after a reload)
    const pending = t.flatMap((x) => x.messages).find((m) => m.role === "agent" && m.job_id && PENDING.includes(m.status))?.job_id;
    if (pending) setJobId((j) => j ?? pending);
  }, []);
  useEffect(() => {
    setDoc(null);
    docRef.current = null;
    setThreads([]);
    setActive(null);
    setJobId(null);
    setErr(null);
    if (!key) return;
    let alive = true;
    get<{ discussion: DiscussionDoc | null }>(`/insights/repos/${repo}/discussion?key=${encodeURIComponent(key)}`)
      .then(async (r) => {
        if (!alive || !r.discussion) return;
        docRef.current = r.discussion;
        setDoc(r.discussion);
        await loadThreads(r.discussion);
      })
      .catch((e) => { if (alive) setErr((e as ApiError).message); });
    return () => { alive = false; };
  }, [repo, key, loadThreads]);
  const live = useJobStream(jobId, () => {
    setJobId(null);
    if (docRef.current) void loadThreads(docRef.current);
  });
  const reload = async () => { if (docRef.current) await loadThreads(docRef.current); };

  /** a new discussion about the material (a selected part of it, or all of it): opened in the panel */
  const ask = async (body: DiscussBody) => {
    if (!key) return;
    const r = await start({ ...body, discussion: key });
    const d = { workspace_id: r.workspace_id, path: r.path, document_id: r.document_id };
    docRef.current = d;
    setDoc(d);
    await loadThreads(d);
    setActive(r.thread_id);
    setOpen(true);
    setJobId(r.job_id);
  };

  let panel: React.ReactNode = null;
  if (key && !open) {
    panel = (
      <button className="chat-side-tab insights-disc-tab" onClick={() => setOpen(true)} title="Show the discussions" data-testid="insights-disc-expand">
        ‹ Discussions{threads.length ? ` (${threads.length})` : ""}
      </button>
    );
  } else if (key) {
    panel = (
      <div className="insights-disc" style={{ width }} data-testid="insights-disc">
        {err && <div className="banner warn small">{err}</div>}
        <DiscussionPanel thread={threads.find((t) => t.id === active) ?? null} threads={threads} sessions={sessions} defaultSessionId={null} liveJob={live}
          onReply={async (tid, text, sc, images) => {
            const r = await post<{ job_id: string }>(`/threads/${tid}/messages`, { text, session_id: sc ?? undefined, images }, { idempotencyKey: newIdempotencyKey() });
            await reload();
            setJobId(r.job_id);
          }}
          onRetry={async (tid, mid, sc) => {
            const r = await post<{ job_id: string }>(`/threads/${tid}/retry`, { message_id: mid, session_id: sc ?? undefined });
            await reload();
            setJobId(r.job_id);
          }}
          onCancel={async (j) => { await post(`/jobs/${j}/cancel`); }}
          onPermission={async (j, rid, decision) => { await post(`/jobs/${j}/permission-decisions`, { request_id: rid, decision }); }}
          onSelectThread={setActive}
          onAskDocument={(question) => ask({ question })}
          docId={doc?.document_id ?? null} onRequestReanchor={() => undefined} reanchorArmed={false}
          onCollapse={() => setOpen(false)} resizer={<Resizer side="left" onResize={resize} onFold={() => setOpen(false)} label="discussions panel"
            other={foldOther ? { ...foldOther, testId: "insights-disc-fold-other" } : undefined} />}
          mode={mode} onModeChange={setMode} />
      </div>
    );
  }
  return { panel, ask, shown: !!key && open };
}
