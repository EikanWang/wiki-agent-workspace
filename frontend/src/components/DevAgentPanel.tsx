import ModelChoice, { ModelChoiceValue } from "./ModelChoice";
import { useEffect, useRef, useState } from "react";
import { AgentSession, ApiError, DevWorkspace, Thread, get, post, put } from "../api";
import { LiveJob, MessageView, SessionChip, useJobStream } from "./DiscussionPanel";
import ModelPicker from "./ModelPicker";
import { usePersistentState } from "../persist";
import { utc8 } from "../time";
import { COMPOSER_HINT, composerKeyDown } from "./keys";

export const DEV_KINDS: { id: string; label: string; hint: string; pr?: boolean }[] = [
  { id: "implement", label: "Implement", hint: "Plan from the wiki, edit, add tests, build and test" },
  { id: "ask", label: "Ask", hint: "A question about the code (changes only when asked)" },
  { id: "fix", label: "Fix", hint: "Fix a problem with a focused change, then test" },
  { id: "tests", label: "Write tests", hint: "Add or extend tests and run them" },
  { id: "explain", label: "Explain", hint: "Explain code; no changes" },
  { id: "verify", label: "Verify against wiki", hint: "Check the branch's changes against the wiki's requirements; no changes" },
  { id: "review", label: "Address review comments", hint: "Read the PR's review comments and address them", pr: true },
  { id: "ci", label: "Fix failing CI", hint: "Find the failing checks' logs, reproduce and fix", pr: true },
];

export interface FileRef { root: string; path: string }
/** What the web editor has open, for "discuss with … files". */
export interface EditorFiles { active: FileRef | null; open: FileRef[]; selected: FileRef[] }
export type FileScope = "none" | "current" | "open" | "selected";
export interface CodeAnchor { root: string; file: string; start_line?: number | null; end_line?: number | null; start_col?: number; end_col?: number; quote: string; language?: string; folder?: boolean }

export function scopeFiles(scope: FileScope, files: EditorFiles | null): FileRef[] {
  if (!files) return [];
  if (scope === "current") return files.active ? [files.active] : [];
  if (scope === "open") return files.open;
  if (scope === "selected") return files.selected;
  return [];
}

export const anchorOf = (t: Thread): CodeAnchor | null => {
  const a = t.target as unknown as (CodeAnchor & { kind?: string }) | null | undefined;
  return a && a.kind === "code" ? a : null;
};

interface Skill { name: string; description: string; path: string; folder: string }

/**
 * The agent of a development workspace. Each request is a *task* (a thread of the workspace's conversation); a
 * discussion started on a selection in the editor is a task about those lines (marked in the editor). The composer
 * continues the chosen task or starts a new one, with the current file, all open files or the files selected in the
 * explorer as context. The agent gets the skills of every connected folder and project.
 */
export default function DevAgentPanel({ dev, sessions, threads, fileSessions = {}, onReload, onSessionsChanged, onTurnFinished, files, focusThread, onRevealAnchor, onFold }: {
  /** fold the panel (a strip at the right edge shows it again) */
  onFold?: () => void;
  dev: DevWorkspace;
  sessions: AgentSession[];
  threads: Thread[];
  /** "<root>:<path>" → the agent session of that file's discussions */
  fileSessions?: Record<string, string>;
  onReload: () => Promise<void>;
  onSessionsChanged: () => void;
  /** a turn ended (the worktree may have changed) */
  onTurnFinished: () => void;
  /** the web editor's files (null: the VS Code editor, which has its own Agent commands) */
  files: EditorFiles | null;
  /** a task to show and continue (clicked in the editor's margin) */
  focusThread: { id: string; at: number } | null;
  onRevealAnchor?: (a: CodeAnchor) => void;
}) {
  const [text, setText] = usePersistentState(`dev.compose.${dev.id}`, "");
  const [kind, setKind] = usePersistentState(`dev.kind.${dev.id}`, "implement");
  const [scope, setScope] = usePersistentState<FileScope>(`dev.scope.${dev.id}`, "current");
  const [target, setTarget] = useState<string>("__last__"); // a thread id, "__new__", or the latest task
  // like a wiki document's discussions: those of the file shown in the editor, or all tasks of the workspace
  const [view, setView] = usePersistentState<"file" | "all">(`dev.view.${dev.id}`, "file");
  const activeFile = files?.active ?? null;
  const fileView = view === "file" && !!activeFile;
  const [busy, setBusy] = useState(false);
  const [modelChoice, setModelChoice] = useState<ModelChoiceValue | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [skills, setSkills] = useState<{ skills: Skill[]; note: string | null } | null>(null);
  const [showSkills, setShowSkills] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const taskRefs = useRef(new Map<string, HTMLDivElement>());
  useEffect(() => {
    void get<{ skills: Skill[]; note: string | null }>(`/dev/workspaces/${dev.id}/skills`).then(setSkills).catch(() => setSkills(null));
  }, [dev.id, dev.wiki_workspace_id]);
  const live: LiveJob | null = useJobStream(jobId, () => {
    void onReload();
    onTurnFinished();
  });
  const pending = threads.flatMap((t) => t.messages).find((m) => m.role === "agent" && m.job_id && ["queued", "running", "waiting_provider_permission", "waiting_for_session"].includes(m.status));
  useEffect(() => {
    if (pending?.job_id && pending.job_id !== jobId) setJobId(pending.job_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending?.job_id]);
  useEffect(() => {
    if (!focusThread) bottom.current?.scrollIntoView({ block: "end" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threads.length, threads[threads.length - 1]?.messages.length, live?.text.length]);
  useEffect(() => {
    if (!focusThread) return;
    setTarget(focusThread.id);
    const ft = threads.find((t) => t.id === focusThread.id);
    const fa = ft ? anchorOf(ft) : null;
    if (!fa || !activeFile || fa.root !== activeFile.root || fa.file !== activeFile.path) setView("all");
    taskRefs.current.get(focusThread.id)?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [focusThread]);

  const isOfFile = (t: Thread) => { const a = anchorOf(t); return !!a && !!activeFile && a.root === activeFile.root && a.file === activeFile.path; };
  const shown = fileView ? threads.filter(isOfFile) : threads;
  useEffect(() => { setTarget("__last__"); }, [activeFile?.root, activeFile?.path, view]);
  const last = shown[shown.length - 1] ?? null;
  const targetThread = target === "__new__" ? null : target === "__last__" ? last : threads.find((t) => t.id === target) ?? last;
  const running = !!pending || (!!live && !["completed", "failed", "cancelled"].includes(live.status));
  const sessionsById = Object.fromEntries(sessions.map((s) => [s.id, s]));
  // the file view shows (and new discussions use) the file's own session; a continued discussion keeps its session
  const fileKey = activeFile ? `${activeFile.root}:${activeFile.path}` : null;
  const fileSessionId = fileView && fileKey ? fileSessions[fileKey] : undefined;
  const sessionId = targetThread?.last_session_id ?? (fileView ? fileSessionId : (last ?? null)?.last_session_id) ?? undefined;
  const lastSession = sessionId ? sessionsById[sessionId] : undefined;
  const chooseFileSession = async (sid: string) => {
    if (!activeFile) return;
    setErr(null);
    try {
      await put(`/dev/workspaces/${dev.id}/file-session`, { root: activeFile.root, file: activeFile.path, session_id: sid || null });
      setTarget("__new__");
      await onReload();
      onSessionsChanged();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const kinds = DEV_KINDS.filter((k) => !k.pr || dev.pr_number);
  const chosen = scopeFiles(scope, files);

  const send = async () => {
    const body = text.trim();
    if (!body || running) return;
    setBusy(true);
    setErr(null);
    try {
      // a new discussion in the file view is about that file (like asking about a wiki document)
      const anchor = !targetThread && fileView && activeFile ? { root: activeFile.root, file: activeFile.path } : undefined;
      const r = await post<{ job_id: string; thread_id: string }>(`/dev/workspaces/${dev.id}/ask`, {
        text: body, kind, thread_id: targetThread?.id, anchor, context: chosen.length ? { files: chosen } : undefined,
        model_choice: modelChoice ?? undefined,
      });
      setModelChoice(null);
      setText("");
      setJobId(r.job_id);
      if (target === "__new__") setTarget("__last__");
      await onReload();
      onSessionsChanged();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const label = (t: Thread, i: number) => {
    const a = anchorOf(t);
    const first = t.messages[0]?.content.split("\n")[0].slice(0, 40) ?? "";
    return `${fileView ? "Discussion" : "Task"} ${i + 1}${a && !fileView ? ` · ${a.file.split("/").pop()}${a.start_line ? `:${a.start_line}` : ""}` : a?.start_line ? ` · line ${a.start_line}` : ""} · ${first}`;
  };
  return (
    <div className="dev-agent" data-testid="dev-agent">
      <div className="dev-agent-head">
        <b>Agent</b>
        {files && (
          <span className="mode-switch" role="tablist" aria-label="Which discussions">
            <button role="tab" aria-selected={view === "file"} className={view === "file" ? "active" : ""} onClick={() => setView("file")}
              title="The discussions of the file shown in the editor" data-testid="dev-view-file">This file</button>
            <button role="tab" aria-selected={view === "all"} className={view === "all" ? "active" : ""} onClick={() => setView("all")}
              title="All tasks and discussions of this workspace" data-testid="dev-view-all">All ({threads.length})</button>
          </span>
        )}
        <SessionChip session={lastSession} />
        <span className="spacer" />
        {skills && (
          <button className="chip chip-btn" onClick={() => setShowSkills(!showSkills)} title="Skills the agent gets from the connected folders and projects" data-testid="dev-skills">
            {skills.skills.length} skill{skills.skills.length === 1 ? "" : "s"}
          </button>
        )}
        {dev.wiki_workspace_id ? <span className="chip" title="The agent reads the whole wiki folder before it changes code">wiki reference</span> : <span className="chip warn" title="No reference wiki: set one in the workspace settings">no wiki</span>}
        {onFold && <button className="icon-btn small" onClick={onFold} title="Fold the agent panel (the Agent strip at the right edge shows it again)" aria-label="Fold the agent panel" data-testid="dev-agent-fold">»</button>}
      </div>
      {showSkills && skills && (
        <div className="dev-skills" data-testid="dev-skills-list">
          {skills.skills.length === 0 && <div className="muted small">No skills in the connected folders (<code>.opencode/skills</code>, <code>.claude/skills</code>, <code>.agents/skills</code>, <code>.codex/skills</code>).</div>}
          {skills.skills.map((s) => (
            <div key={s.path} className="small" title={s.path}><b>{s.name}</b> <span className="muted">— {s.description || "no description"}</span> <span className="muted mono">{s.folder.split("/").pop()}</span></div>
          ))}
          {skills.note && <div className="muted small">{skills.note}</div>}
        </div>
      )}
      {fileView && (
        <div className="row nowrap dev-file-session" data-testid="dev-file-session">
          <span className="muted small">Session of {activeFile!.path.split("/").pop()}:</span>
          <select value={fileSessionId ?? ""} onChange={(e) => void chooseFileSession(e.target.value)} title="The agent session this file's discussions use; switching files switches the session" data-testid="dev-file-session-select">
            <option value="">{fileSessionId ? "Forget (next: a new one)" : "New session on the first question"}</option>
            {fileSessionId && !sessionsById[fileSessionId] && <option value={fileSessionId}>{fileSessionId.slice(0, 8)}</option>}
            {sessions.filter((x) => x.origin !== "preview" && x.origin !== "priority" && x.origin !== "commit").map((x) => (
              <option key={x.id} value={x.id}>{x.provider} · {x.display_name ?? x.short_id ?? x.id.slice(0, 8)}{x.short_id ? ` #${x.short_id}` : ""}</option>
            ))}
            <option value="new">+ New session for this file</option>
          </select>
        </div>
      )}
      {lastSession && <ModelPicker session={lastSession} onChanged={() => onSessionsChanged()} compact />}
      <div className="dev-agent-stream">
        {fileView && (
          <div className="dev-file-head" data-testid="dev-file-head">
            <b className="mono">{activeFile!.path.split("/").pop()}</b>
            <span className="muted small">{shown.length} discussion{shown.length === 1 ? "" : "s"}{activeFile!.root !== "wt" ? " · wiki" : ""}</span>
          </div>
        )}
        {fileView && shown.length === 0 && (
          <div className="muted small dev-hint" data-testid="dev-file-empty">
            No discussions about <b>{activeFile!.path}</b> yet. Ask below about the whole file, or select code in it and click <b>Discuss</b>.
          </div>
        )}
        {view === "file" && !activeFile && files && (
          <div className="muted small dev-hint">Open a file to see its discussions, or switch to <b>All</b>.</div>
        )}
        {!fileView && threads.length === 0 && (
          <div className="muted small dev-hint">
            Describe what to build or fix. The agent searches the reference wiki, works in this branch's worktree, runs the build and
            tests, and lists the wiki pages it relied on. In the editor: select code and click <b>Discuss</b> (or right-click → <b>Agent</b>);{" "}
            <kbd>Ctrl</kbd>-click files in the explorer to discuss several.
          </div>
        )}
        {(fileView || view === "all" || !files ? shown : []).map((t, i) => {
          const a = anchorOf(t);
          return (
            <div key={t.id} ref={(el) => { if (el) taskRefs.current.set(t.id, el); }} className={`dev-task ${targetThread?.id === t.id ? "current" : ""}`} data-testid="dev-task" data-thread={t.id}>
              <div className="dev-task-head muted small">
                <button className="link-btn small" onClick={() => setTarget(t.id)} title="Continue this discussion">{fileView ? "Discussion" : "Task"} {i + 1}</button> · {utc8(t.created_at)}
                {a && !a.start_line && <span className="chip" data-testid="dev-task-about">{a.folder ? `📁 ${a.file}` : fileView ? "whole file" : a.file.split("/").pop()}</span>}
              </div>
              {a && a.start_line && (
                <button className="dev-anchor" onClick={() => onRevealAnchor?.(a)} title="Show these lines in the editor" data-testid="dev-anchor">
                  <span className="mono small">{fileView ? "" : a.file}{fileView ? "lines " : ":"}{a.start_line}{a.end_line && a.end_line !== a.start_line ? `–${a.end_line}` : ""}</span>
                  <pre className="dev-anchor-quote">{a.quote.split("\n").slice(0, 6).join("\n")}{a.quote.split("\n").length > 6 ? "\n…" : ""}</pre>
                </button>
              )}
              {t.messages.map((m) => (
                <MessageView key={m.id} m={m} session={sessionsById[m.session_id ?? ""]} sessions={sessions}
                  live={live && m.job_id === live.jobId ? live : null}
                  onRetry={async (sid) => {
                    try {
                      const r = await post<{ job_id: string }>(`/threads/${t.id}/retry`, { message_id: m.id, session_id: sid ?? undefined });
                      setJobId(r.job_id);
                      await onReload();
                    } catch (e) { setErr((e as ApiError).message); }
                  }}
                  onCancel={async (jid) => { await post(`/jobs/${jid}/cancel`).catch((e) => setErr((e as ApiError).message)); }}
                  onPermission={async (jid, rid, decision) => { await post(`/jobs/${jid}/permission-decisions`, { request_id: rid, decision }).catch((e) => setErr((e as ApiError).message)); }} />
              ))}
            </div>
          );
        })}
        <div ref={bottom} />
      </div>
      <div className="dev-composer">
        <div className="row nowrap">
          <select value={kind} onChange={(e) => setKind(e.target.value)} title={DEV_KINDS.find((k) => k.id === kind)?.hint} data-testid="dev-kind">
            {kinds.map((k) => <option key={k.id} value={k.id} title={k.hint}>{k.label}</option>)}
          </select>
          <select value={targetThread ? (target === "__last__" ? "__last__" : targetThread.id) : "__new__"} onChange={(e) => setTarget(e.target.value)} title="Continue a task (the agent keeps its context) or start a new one" data-testid="dev-target">
            <option value="__new__">{fileView ? `New discussion about ${activeFile!.path.split("/").pop()}` : "New task"}</option>
            {last && <option value="__last__">Continue the latest {fileView ? "discussion" : "task"}</option>}
            {shown.map((t, i) => <option key={t.id} value={t.id}>Continue {label(t, i)}</option>)}
          </select>
        </div>
        {files && (
          <div className="row nowrap">
            <select value={scope} onChange={(e) => setScope(e.target.value as FileScope)} title="Files sent with the request (their content when it fits)" data-testid="dev-scope">
              <option value="none">No files</option>
              <option value="current">Current file{files.active ? `: ${files.active.path.split("/").pop()}` : " (none)"}</option>
              <option value="open">Open files ({files.open.length})</option>
              <option value="selected">Selected files ({files.selected.length})</option>
            </select>
            <span className="muted small dev-scope-list" title={chosen.map((f) => f.path).join("\n")}>{chosen.length ? chosen.map((f) => f.path.split("/").pop()).join(", ") : scope === "selected" ? "Ctrl-click files in the explorer" : ""}</span>
          </div>
        )}
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={3} placeholder={DEV_KINDS.find((k) => k.id === kind)?.hint}
          onKeyDown={(e) => composerKeyDown(e, () => void send(), setText)} data-testid="dev-input" />
        <div className="row">
          <ModelChoice value={modelChoice} onChange={setModelChoice} session={lastSession ?? null} />
          <span className="spacer" />
          <button className="btn small primary" disabled={!text.trim() || running || busy} onClick={() => void send()} data-testid="dev-send">{running ? "Working…" : "Send"}</button>
        </div>
        {err && <div className="error-text small" role="alert">{err}</div>}
      </div>
    </div>
  );
}
