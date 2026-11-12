import { Splitter, usePane } from "./Split";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, DevTaskRun, DevWorkspace, get, post } from "../api";
import { useJobStream } from "./DiscussionPanel";
import { confirmAction } from "./Confirm";
import { GhRefreshSelect, useGhAutoRefresh } from "../ghRefresh";

/** Build outputs and caches: left out of a commit unless you tick them (the checkout may lack a .gitignore for them). */
export const isArtifact = (p: string) => /(^|\/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.cache|build|cmake-build-[^/]*|node_modules|\.venv)(\/|$)|\.(pyc|pyo|o|obj|so|a)$/.test(p);

const strip = (s: string) => s.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "").replace(/\r(?!\n)/g, "\n");

/** Build / test / custom command runs, their output, and "Fix with the agent" for a failed one. */
export function TasksTab({ dev, onAsked }: { dev: DevWorkspace; onAsked: () => void }) {
  const tasksPane = usePane("dev.tasks", 300, 180, 900);
  const [runs, setRuns] = useState<DevTaskRun[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [log, setLog] = useState("");
  const [custom, setCustom] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const offset = useRef(0);
  const pre = useRef<HTMLPreElement>(null);
  const load = useCallback(async () => {
    try {
      const r = await get<{ runs: DevTaskRun[] }>(`/dev/workspaces/${dev.id}/tasks`);
      setRuns(r.runs);
      setSel((s) => s ?? r.runs[0]?.id ?? null);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [dev.id]);
  useEffect(() => { void load(); }, [load]);
  const cur = runs.find((r) => r.id === sel) ?? null;
  useEffect(() => {
    offset.current = 0;
    setLog("");
    if (!sel) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const r = await get<{ text: string; offset: number; run: DevTaskRun }>(`/dev/tasks/${sel}/log?offset=${offset.current}`);
        if (!alive) return;
        offset.current = r.offset;
        if (r.text) setLog((l) => (l + strip(r.text)).slice(-400_000));
        setRuns((rs) => rs.map((x) => (x.id === r.run.id ? r.run : x)));
        if (r.run.status === "running" || r.text) timer = setTimeout(tick, r.run.status === "running" ? 700 : 50);
      } catch {
        /* the run may be gone */
      }
    };
    void tick();
    return () => { alive = false; clearTimeout(timer); };
  }, [sel]);
  useEffect(() => { pre.current?.scrollTo({ top: pre.current.scrollHeight }); }, [log]);
  const start = async (kind: "build" | "test" | "custom", command?: string) => {
    setErr(null);
    try {
      const r = await post<DevTaskRun>(`/dev/workspaces/${dev.id}/tasks`, { kind, command });
      setRuns((rs) => [r, ...rs]);
      setSel(r.id);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const s = dev.settings;
  return (
    <div className="dev-tasks" data-testid="dev-tasks">
      {tasksPane.folded ? null : <div className="dev-tasks-side" style={{ width: tasksPane.size }}>
        <div className="row nowrap">
          <button className="btn small" disabled={!s.build_command} title={s.build_command ?? "No build command: set one in Settings"} onClick={() => void start("build")} data-testid="dev-build">Build</button>
          <button className="btn small" disabled={!s.test_command} title={s.test_command ?? "No test command: set one in Settings"} onClick={() => void start("test")} data-testid="dev-test">Test</button>
        </div>
        <form className="row nowrap" onSubmit={(e) => { e.preventDefault(); if (custom.trim()) void start("custom", custom.trim()); }}>
          <input value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="any command, e.g. pytest -k add" className="mono" data-testid="dev-custom" />
          <button className="btn small" disabled={!custom.trim()}>Run</button>
        </form>
        <div className="dev-runs">
          {runs.map((r) => (
            <button key={r.id} className={`list-item ${r.id === sel ? "selected" : ""}`} onClick={() => setSel(r.id)} data-testid="dev-run" data-status={r.status}>
              <span className="mono small dev-run-cmd">{r.kind}: {r.command}</span>
              <span className={`chip status ${r.status === "passed" ? "" : r.status}`}>{r.status}{r.exit_code !== null && r.status === "failed" ? ` (${r.exit_code})` : ""}</span>
            </button>
          ))}
          {runs.length === 0 && <div className="muted small dev-hint">No runs yet.</div>}
        </div>
        {err && <div className="error-text small">{err}</div>}
      </div>}
      <Splitter dir="x" side="before" pane={tasksPane} label="command list" testId="dev-tasks-splitter" />
      <div className="dev-log">
        {cur && (
          <div className="row nowrap dev-log-head">
            <span className="mono small">{cur.command}</span>
            <span className="spacer" />
            {cur.status === "running" && <button className="btn small" onClick={() => void post(`/dev/tasks/${cur.id}/cancel`).then(load)}>Stop</button>}
            {cur.status === "failed" && (
              <button className="btn small primary" onClick={() => void post(`/dev/tasks/${cur.id}/fix`).then(onAsked).catch((e) => setErr((e as ApiError).message))} data-testid="dev-run-fix"
                title="Send this output to the agent: it finds the cause, fixes it and runs the command again">Fix with the agent</button>
            )}
          </div>
        )}
        <pre ref={pre} className="dev-log-text" data-testid="dev-log">{log || (cur ? "" : "Run the build or the tests; the output appears here.")}</pre>
      </div>
    </div>
  );
}

/** The pull request: create one (description drafted by the agent if you like), or see its checks and hand review
 * comments / failing CI to the agent. */
export function PrTab({ dev, onAsk, onChanged }: { dev: DevWorkspace; onAsk: (kind: string, text: string) => Promise<void>; onChanged: () => void }) {
  const [checks, setChecks] = useState<{ name: string; state: string; link?: string; workflow?: string }[] | null>(null);
  const [checksNote, setChecksNote] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [draft, setDraft] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [draftJob, setDraftJob] = useState<string | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const loadChecks = useCallback(async () => {
    if (!dev.pr_number) return;
    try {
      const r = await get<{ checks: typeof checks; note: string | null }>(`/dev/workspaces/${dev.id}/checks`);
      setChecks(r.checks);
      setChecksNote(r.note);
    } catch (e) {
      setChecksNote((e as ApiError).message);
    }
  }, [dev.id, dev.pr_number]);
  useEffect(() => { void loadChecks(); }, [loadChecks]);
  useGhAutoRefresh("pr", loadChecks, !!dev.pr_number); // the PR's checks, at the interval chosen for open pull requests
  useJobStream(draftJob, (job) => {
    setDraftJob(null);
    if (job.status === "completed" && job.text) {
      const [t, ...rest] = job.text.trim().split("\n");
      setTitle(t.replace(/^#+\s*|^title:\s*/i, "").trim());
      setBody(rest.join("\n").trim());
    } else if (job.error) setErr(job.error.message);
  });
  const prUrl = dev.github_repo && dev.pr_number ? `https://github.com/${dev.github_repo}/pull/${dev.pr_number}` : url;
  if (dev.pr_number) {
    const failing = (checks ?? []).filter((c) => ["FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "fail"].includes(c.state));
    return (
      <div className="dev-pr" data-testid="dev-pr">
        <div className="row">
          <a href={prUrl ?? "#"} target="_blank" rel="noreferrer"><b>{dev.github_repo}#{dev.pr_number}</b></a>
          <span className="muted small">pushes go to <span className="mono">{dev.push_branch}</span></span>
          <span className="spacer" />
          <button className="btn small" onClick={() => void onAsk("review", "Address the pull request's review comments.")} data-testid="dev-ask-review">Address review comments</button>
          <button className="btn small" disabled={!failing.length && checks !== null && checks.length > 0} onClick={() => void onAsk("ci", "Fix the pull request's failing CI.")} data-testid="dev-ask-ci">Fix failing CI</button>
          <button className="btn small" onClick={() => void onAsk("verify", "Verify the changes of this branch against the wiki.")}>Verify against wiki</button>
          <button className="icon-btn small" onClick={() => void loadChecks()} title="Refresh checks">↻</button>
          <GhRefreshSelect kind="pr" testId="dev-checks-auto-refresh" title="How often the checks (and open pull requests in PR Review) refresh from GitHub on its own; ↻ refreshes now" />
        </div>
        <div className="dev-checks">
          {checks?.map((c, i) => (
            <div key={i} className="small"><span className={`chip status ${failing.includes(c) ? "failed" : ""}`}>{c.state.toLowerCase()}</span> {c.workflow ? `${c.workflow} / ` : ""}{c.link ? <a href={c.link} target="_blank" rel="noreferrer">{c.name}</a> : c.name}</div>
          ))}
          {checksNote && <div className="muted small">{checksNote}</div>}
        </div>
      </div>
    );
  }
  return (
    <div className="dev-pr" data-testid="dev-pr">
      {!dev.github_repo && <div className="warn small">The checkout has no github.com remote: push the branch and open the pull request where the repository lives.</div>}
      <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Pull request title" data-testid="dev-pr-title" />
      <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={6} placeholder="Description (Markdown)" />
      <div className="row">
        <button className="btn small" disabled={!!draftJob} onClick={() => void post<{ job_id: string }>(`/dev/workspaces/${dev.id}/draft?what=pr`).then((r) => setDraftJob(r.job_id)).catch((e) => setErr((e as ApiError).message))}>{draftJob ? "Drafting…" : "Draft with agent"}</button>
        <button className="btn small" onClick={() => void onAsk("verify", "Verify the changes of this branch against the wiki.")}>Verify against wiki first</button>
        <label className="small"><input type="checkbox" checked={draft} onChange={(e) => setDraft(e.target.checked)} /> draft</label>
        <span className="spacer" />
        <button className="btn small primary" disabled={!title.trim() || busy || !dev.github_repo} data-testid="dev-pr-create"
          onClick={async () => {
            if (!(await confirmAction({ title: "Create the pull request?", message: `Unpushed commits are pushed to ${dev.push_branch ?? dev.branch} first, then a ${draft ? "draft " : ""}pull request is opened on ${dev.github_repo}.`, confirmLabel: "Create pull request" }))) return;
            setBusy(true);
            setErr(null);
            try {
              const r = await post<{ url: string; number: number | null }>(`/dev/workspaces/${dev.id}/pull-request`, { title, body, draft });
              setUrl(r.url);
              onChanged();
            } catch (e) {
              setErr((e as ApiError).message);
            } finally {
              setBusy(false);
            }
          }}>{busy ? "Creating…" : "Create pull request"}</button>
      </div>
      {url && <div className="small">Created: <a href={url} target="_blank" rel="noreferrer">{url}</a></div>}
      {err && <div className="error-text small" role="alert">{err}</div>}
    </div>
  );
}
