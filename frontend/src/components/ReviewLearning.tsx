import { useCallback, useEffect, useState } from "react";
import { ApiError, del, get, patch, post, put } from "../api";
import ModelChoice, { ModelChoiceValue } from "./ModelChoice";

/**
 * Review learning: what you do with the agent's review suggestions (apply, edit, discard with a reason) and the
 * points it missed become lessons — proposed for you to accept — that go into its next PR reviews.
 */

export interface Lesson {
  id: string; scope: string; paths: string[]; text: string; status: "proposed" | "active" | "off" | "rejected"; source: "distilled" | "taught" | "manual";
  rationale: string; evidence: number; replaces: string | null; replaces_text: string | null; used: number; confirmed: number; contradicted: number;
  flagged: string | null; created_at: string | null; updated_at: string | null;
}
interface Case {
  id: string; repo: string; number: number; path: string; line: number | null; source: string; outcome: string; agent_body: string | null;
  final_body: string | null; reason_label: string | null; author: string | null; created_at: string | null;
}
interface Model { mode: "agent" | "api"; provider: string | null; model: string | null; variant: string | null }
interface Settings { enabled: boolean; auto: boolean; reviewers: boolean; model: Model; max_lessons: number; max_chars: number; examples: number; min_cases: number }
interface RepoStats { suggested: number; applied: number; edited: number; discarded: number; missed: number; taught: number }
interface Overview {
  settings: Settings; state: { running: boolean; error: string | null; last_run: string | null; last: Record<string, number> | null };
  pending_cases: number; lessons: Lesson[]; proposed: number; flagged: number; stats: Record<string, RepoStats>; reasons: Record<string, string>;
}

/** Why a suggestion is discarded: one click after Discard. */
export const DISCARD_REASONS: [string, string][] = [["wrong", "Wrong"], ["nitpick", "Nitpick"], ["minor", "Not important"], ["discussed", "Already discussed"], ["scope", "Out of scope"], ["other", "Other"]];

// the ⚙ badge: proposed lessons (and flagged ones) wait for you. Whenever the summary says a distill is running —
// started here by a publish, or already going when the page loaded — it is asked again every few seconds until the
// run ends, so its lessons show up on their own. Bounded: one window of POLL_MAX per run seen.
type Summary = { proposed: number; flagged: number; running?: boolean };
let summary: Summary = { proposed: 0, flagged: 0 };
const listeners = new Set<(s: Summary) => void>();
let poll: number | undefined;
const POLL_MS = 2500;
const POLL_MAX = 10 * 60_000; // a distill (one agent turn per repository) is given up on after this
let pollUntil = 0;
let wasRunning = false;
/** `follow`: a publish just started a distill in the background (it may not be running yet at the first answer). */
export async function refreshLearningSummary(follow = false): Promise<void> {
  if (follow) pollUntil = Date.now() + POLL_MAX;
  try {
    summary = await get<Summary>("/review-learning/summary");
    listeners.forEach((f) => f(summary));
  } catch {
    /* not the GitHub owner, or offline */
  }
  const running = !!summary.running;
  if (running && !wasRunning && !follow && pollUntil < Date.now()) pollUntil = Date.now() + POLL_MAX; // a run this page did not start
  wasRunning = running;
  window.clearTimeout(poll);
  poll = undefined;
  if ((running || follow) && Date.now() < pollUntil) poll = window.setTimeout(() => void refreshLearningSummary(), POLL_MS);
}
export function useLearningSummary(active: boolean) {
  const [s, setS] = useState(summary);
  useEffect(() => {
    listeners.add(setS);
    if (active) void refreshLearningSummary();
    return () => { listeners.delete(setS); };
  }, [active]);
  return s;
}

const pct = (n: number, d: number) => (d ? `${Math.round((100 * n) / d)}%` : "–");
const scopeLabel = (s: string) => (s === "*" ? "every repository" : s);
const toChoice = (m: Model): ModelChoiceValue | null => (m.provider || m.model || m.variant ? { provider: m.provider ?? undefined, model: m.model ?? undefined, variant: m.variant ?? undefined } : null);

export function LearningSettings() {
  const [o, setO] = useState<Overview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<Lesson["status"] | null>(null);
  const [adding, setAdding] = useState<{ text: string; scope: string } | null>(null);
  const load = useCallback(async () => {
    try {
      const x = await get<Overview>("/review-learning");
      setO(x);
      setFilter((f) => f ?? (x.proposed ? "proposed" : "active"));
      void refreshLearningSummary();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!o?.state.running) return;
    const t = window.setInterval(() => void load(), 2000);
    return () => window.clearInterval(t);
  }, [o?.state.running, load]);
  const run = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  if (!o) return <div className="muted small">{err ?? "Loading…"}</div>;
  const s = o.settings;
  const save = (p: Partial<Settings>) => {
    setO((x) => (x ? { ...x, settings: { ...x.settings, ...p } } : x)); // at once; the answer confirms it
    return run(() => put("/review-learning/settings", p));
  };
  const count = (st: Lesson["status"]) => o.lessons.filter((l) => l.status === st).length;
  const shown = o.lessons.filter((l) => l.status === filter);
  return (
    <div className="learning" data-testid="learning-settings">
      <div className="muted small">
        What you do with the agent's review suggestions — apply, edit, discard (with a reason) — and the points it missed become
        lessons. You accept them here; active ones go into its next agent reviews, with examples and its track record.
      </div>
      {err && <div className="error-text small" role="alert">{err}</div>}
      <label className="small"><input type="checkbox" checked={s.enabled} onChange={(e) => void save({ enabled: e.target.checked })} data-testid="learning-enabled" /> Use what it learned in agent reviews</label>
      <label className="small"><input type="checkbox" checked={s.auto} onChange={(e) => void save({ auto: e.target.checked })} data-testid="learning-auto" /> Learn after each published review</label>
      <label className="small"><input type="checkbox" checked={s.reviewers} onChange={(e) => void save({ reviewers: e.target.checked })} data-testid="learning-reviewers" /> Learn from other reviewers' comments where it said nothing</label>
      <div className="row small learning-model">
        Learns with
        <select value={s.model.mode} onChange={(e) => void save({ model: { ...s.model, mode: e.target.value as Model["mode"] } })} data-testid="learning-model-mode">
          <option value="agent">the app's agent</option>
          <option value="api">the Repo Insights model endpoint</option>
        </select>
        {s.model.mode === "agent" && <ModelChoice value={toChoice(s.model)} onChange={(v) => void save({ model: { mode: "agent", provider: v?.provider ?? null, model: v?.model ?? null, variant: v?.variant ?? null } })} provider={s.model.provider} />}
      </div>
      {s.model.mode === "api" && <div className="muted small">Set in Repo Insights ⚙. Your review comments and the agent's suggestions are sent to that endpoint.</div>}
      <div className="row small">
        <button className="btn small" disabled={o.state.running || !o.pending_cases} onClick={() => void run(() => post("/review-learning/distill"))} data-testid="learning-distill"
          title="Read what happened since the last time and propose lessons">{o.state.running ? "Learning…" : "Learn now"}</button>
        <span className="muted" data-testid="learning-state">
          {o.pending_cases} new case(s){o.state.last_run ? ` · last ${new Date(o.state.last_run).toLocaleString()}` : ""}
          {o.state.last ? ` (${o.state.last.added ?? 0} new, ${o.state.last.revised ?? 0} revised)` : ""}
        </span>
      </div>
      {o.state.error && <div className="warn small" data-testid="learning-error">Last run failed: {o.state.error}</div>}
      {Object.entries(o.stats).map(([repo, x]) => (
        <div key={repo} className="small learning-stat" data-testid="learning-stats" data-repo={repo}>
          <b>{repo}</b> <span className="muted">(90 days)</span>: {x.suggested} suggested · applied {pct(x.applied, x.suggested)} · edited {pct(x.edited, x.suggested)} · discarded {pct(x.discarded, x.suggested)} · {x.missed} missed
        </div>
      ))}
      <span className="mode-switch learning-filter" role="tablist">
        {(["proposed", "active", "off", "rejected"] as const).map((st) => (
          <button key={st} role="tab" aria-selected={filter === st} className={filter === st ? "active" : ""} onClick={() => setFilter(st)} data-testid={`learning-filter-${st}`}>
            {st === "proposed" ? "Proposed" : st === "active" ? "Active" : st === "off" ? "Off" : "Rejected"} ({count(st)})
          </button>
        ))}
      </span>
      {shown.length === 0 && <div className="muted small">{filter === "proposed" ? "Nothing to decide. Lessons are proposed after you publish reviews with agent suggestions." : "None."}</div>}
      {shown.map((l) => <LessonItem key={l.id} lesson={l} run={run} />)}
      {adding === null ? (
        <button className="link-btn small" onClick={() => setAdding({ text: "", scope: "*" })} data-testid="learning-add">+ Add a lesson</button>
      ) : (
        <div className="learning-add">
          <textarea rows={2} autoFocus value={adding.text} onChange={(e) => setAdding({ ...adding, text: e.target.value })} placeholder="e.g. Ask for an XPU test when a kernel changes." data-testid="learning-add-text" />
          <div className="row small">
            for <input value={adding.scope} onChange={(e) => setAdding({ ...adding, scope: e.target.value })} placeholder="owner/repo or *" title="owner/repo, or * for every repository" data-testid="learning-add-scope" />
            <button className="btn small primary" disabled={!adding.text.trim()} onClick={() => void run(async () => { await post("/review-learning/lessons", { text: adding.text, scope: adding.scope || "*" }); setAdding(null); setFilter("active"); })} data-testid="learning-add-save">Add</button>
            <button className="btn small" onClick={() => setAdding(null)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

function LessonItem({ lesson: l, run }: { lesson: Lesson; run: (fn: () => Promise<unknown>) => Promise<void> }) {
  const [edit, setEdit] = useState<string | null>(null);
  const [cases, setCases] = useState<Case[] | null>(null);
  const set = (b: Record<string, unknown>) => run(() => patch(`/review-learning/lessons/${l.id}`, b));
  const evidence = async () => {
    if (cases) return setCases(null);
    try {
      setCases((await get<{ cases: Case[] }>(`/review-learning/lessons/${l.id}/evidence`)).cases);
    } catch {
      setCases([]);
    }
  };
  return (
    <div className={`learning-lesson ${l.status}`} data-testid="learning-lesson" data-status={l.status}>
      {edit === null ? <div className="learning-text" data-testid="learning-lesson-text">{l.text}</div> : (
        <textarea rows={3} autoFocus value={edit} onChange={(e) => setEdit(e.target.value)} data-testid="learning-edit-text" />
      )}
      {l.replaces_text && <div className="muted small">Revises: “{l.replaces_text}”</div>}
      {l.flagged && l.status === "active" && <div className="warn small" data-testid="learning-flagged">Recent reviews disagree: {l.flagged}</div>}
      <div className="muted small">
        <span className="chip">{scopeLabel(l.scope)}</span>{l.paths.length ? ` ${l.paths.join(", ")}` : ""} · {l.source === "distilled" ? "learned" : l.source === "taught" ? "taught" : "yours"}
        {l.used ? ` · used ${l.used}×` : ""}{l.confirmed ? ` · confirmed ${l.confirmed}` : ""}{l.contradicted ? ` · contradicted ${l.contradicted}` : ""}
      </div>
      {l.rationale && <div className="muted small">Why: {l.rationale}</div>}
      <div className="row small learning-actions">
        {edit !== null ? (
          <>
            <button className="btn small primary" disabled={!edit.trim()} onClick={() => void set({ text: edit }).then(() => setEdit(null))} data-testid="learning-edit-save">Save</button>
            <button className="btn small" onClick={() => setEdit(null)}>Cancel</button>
          </>
        ) : (
          <>
            {l.status === "proposed" && <button className="btn small primary" onClick={() => void set({ status: "active" })} data-testid="learning-accept">{l.replaces ? "Accept revision" : "Accept"}</button>}
            {l.status === "proposed" && <button className="btn small" onClick={() => void set({ status: "rejected" })} data-testid="learning-reject">Reject</button>}
            {l.status === "active" && l.flagged && <button className="btn small" onClick={() => void set({ clear_flag: true })} data-testid="learning-keep">Keep it</button>}
            {l.status === "active" && <button className="btn small" onClick={() => void set({ status: "off" })} data-testid="learning-off">Turn off</button>}
            {l.status === "off" && <button className="btn small" onClick={() => void set({ status: "active" })} data-testid="learning-on">Turn on</button>}
            {l.status === "rejected" && <button className="btn small" onClick={() => void set({ status: "proposed" })}>Reconsider</button>}
            <button className="link-btn small" onClick={() => setEdit(l.text)} data-testid="learning-edit">Edit</button>
            {l.evidence > 0 && <button className="link-btn small" onClick={() => void evidence()} data-testid="learning-evidence">{cases ? "Hide" : "Evidence"} ({l.evidence})</button>}
            {l.status !== "proposed" && <button className="link-btn small danger" onClick={() => void run(() => del(`/review-learning/lessons/${l.id}`))} data-testid="learning-delete">Delete</button>}
          </>
        )}
      </div>
      {cases && (
        <div className="learning-cases" data-testid="learning-cases">
          {cases.length === 0 && <div className="muted small">The cases are gone.</div>}
          {cases.map((c) => (
            <div key={c.id} className="small learning-case">
              <span className={`chip outcome ${c.outcome}`}>{c.outcome}{c.reason_label ? `: ${c.reason_label}` : ""}</span>{" "}
              <span className="muted">{c.repo}#{c.number} {c.path}{c.line ? `:${c.line}` : ""}{c.author ? ` · ${c.author}` : ""}</span>
              {c.agent_body && <div>Agent: {c.agent_body.slice(0, 240)}</div>}
              {c.final_body && <div>{c.outcome === "missed" || c.outcome === "taught" ? "Comment" : "Published"}: {c.final_body.slice(0, 240)}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** "Remember for future reviews": a lesson from one comment (the model can word it), for this repository or all. */
export function TeachBox({ apiBase, body, path, line, lineText, author, onError }: {
  apiBase: string; body: string; path: string; line?: number | null; lineText?: string; author?: string | null; onError: (m: string) => void;
}) {
  const m = /^\/github\/pr\/([^/]+)\/([^/]+)\/(\d+)/.exec(apiBase);
  const repo = m ? `${m[1]}/${m[2]}` : "";
  const [text, setText] = useState<string | null>(null);
  const [scope, setScope] = useState<"repo" | "all">("repo");
  const [glob, setGlob] = useState("");
  const [busy, setBusy] = useState<"draft" | "save" | null>(null);
  const [done, setDone] = useState(false);
  if (!repo) return null;
  if (done) return <span className="muted small" data-testid="teach-done">Remembered for future reviews.</span>;
  if (text === null) return <button className="link-btn small" onClick={() => setText("")} title="Turn this comment into a lesson for the agent's future reviews" data-testid="teach">Remember…</button>;
  const draft = async () => {
    setBusy("draft");
    try {
      setText((await post<{ text: string }>("/review-learning/teach/draft", { repo, body, path, line_text: lineText ?? "", author: author ?? null })).text);
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };
  const save = async () => {
    setBusy("save");
    try {
      await post("/review-learning/teach", { repo, scope, text, paths: glob.trim() ? glob.split(",").map((g) => g.trim()) : [], number: Number(m![3]), path, line: line ?? null,
        line_text: lineText ?? "", body, author: author ?? null });
      setDone(true);
      void refreshLearningSummary();
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="gh-reply-box teach-box" data-testid="teach-box">
      <div className="muted small">A lesson for the agent's future reviews, learned from this comment:</div>
      <textarea autoFocus rows={2} value={text} onChange={(e) => setText(e.target.value)} placeholder="e.g. Check the output dtype of new kernels." data-testid="teach-text" />
      <div className="row small">
        <button className="btn small" disabled={busy !== null} onClick={() => void draft()} title="Let the model word a general lesson from this comment" data-testid="teach-draft">{busy === "draft" ? "Drafting…" : "Draft it for me"}</button>
        <select value={scope} onChange={(e) => setScope(e.target.value as "repo" | "all")} data-testid="teach-scope">
          <option value="repo">for {repo}</option>
          <option value="all">for every repository</option>
        </select>
        <input value={glob} onChange={(e) => setGlob(e.target.value)} placeholder="files (optional), e.g. aten/*" title="Only for PRs that touch matching files (comma-separated globs)" data-testid="teach-paths" />
      </div>
      <div className="row">
        <button className="btn small primary" disabled={busy !== null || !text.trim()} onClick={() => void save()} data-testid="teach-save">Remember</button>
        <button className="btn small" onClick={() => setText(null)}>Cancel</button>
      </div>
    </div>
  );
}
