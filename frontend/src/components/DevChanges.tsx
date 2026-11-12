import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, DevCompare, DevDiffFile, DevGitStatus, DevRefs, DevWorkspace, get, post } from "../api";
import { writeClipboard } from "../clipboard";
import { COMPOSER_HINT, composerKeyDown } from "./keys";
import { Line, SplitCols, UnifiedCols, buildSplitRows, numColStyle, parseUnified } from "./DiffReview";
import { Gap, expandLines } from "./PrDiff";
import { useJobStream } from "./DiscussionPanel";
import { isArtifact } from "./DevTabs";
import { CodeAnchor } from "./DevAgentPanel";

/** What the agent is asked about from the Changes view. */
export interface ChangesAsk { text: string; anchor?: CodeAnchor; context: Record<string, unknown> }

/** What to compare: the uncommitted work, the whole branch, a single commit, or the worktree against a branch. */
type Mode = string; // "wt" | "branch" | "branch-committed" | "c:<sha>" | "b:<branch>"

const EXPAND_STEP = 20;
const TRAILING = -1;
const ALL: Record<number, { top: number; bottom: number }> = new Proxy({}, { get: () => ({ top: Number.MAX_SAFE_INTEGER, bottom: 0 }) });

function query(mode: Mode): Record<string, string> {
  if (mode === "branch") return { base: "branch" };
  if (mode === "branch-committed") return { base: "branch", target: "HEAD" };
  if (mode.startsWith("c:")) return { commit: mode.slice(2) };
  if (mode.startsWith("b:")) return { base: mode.slice(2) };
  return {};
}

const inScope = (p: string, scope: string) => p === scope || p.startsWith(scope.replace(/\/$/, "") + "/");

/** A short fingerprint of a file's diff: a "Viewed" mark is dropped when the change changes. */
function fingerprint(s: string): string {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return `${s.length}:${h}`;
}

/**
 * The Changes view of a development workspace, like the wiki's Diff view: every changed file with its own diff
 * (unified or side by side, more context on request), what to compare (uncommitted work, the whole branch, one
 * commit, another branch), "Viewed" marks, questions to the agent about a file or some lines, and committing the
 * files you pick with a message the agent can draft, then pushing.
 */
export default function DevChanges({ dev, refreshKey, onChanged, onAsk, onOpenFile, wide, onToggleWide, scope, onScope }: {
  /** only this folder / file of the worktree; null: everything */
  scope: string | null;
  onScope: (p: string | null) => void;
  dev: DevWorkspace;
  refreshKey: number;
  onChanged: () => void;
  onAsk: (a: ChangesAsk) => Promise<void>;
  /** open a worktree file in the editor (at a line); absent with VS Code (code-server) */
  onOpenFile?: (path: string, line?: number) => void;
  /** shown in the editor area (true) or in the bottom panel */
  wide: boolean;
  onToggleWide: () => void;
}) {
  const [mode, setMode] = useState<Mode>(() => sessionStorage.getItem(`waw.dev.compare.${dev.id}`) ?? "wt");
  useEffect(() => { sessionStorage.setItem(`waw.dev.compare.${dev.id}`, mode); }, [mode, dev.id]);
  const [layout, setLayout] = useState<"unified" | "split">(() => (localStorage.getItem("waw.diffLayout") === "split" ? "split" : "unified"));
  useEffect(() => { localStorage.setItem("waw.diffLayout", layout); }, [layout]);
  const [cmp, setCmp] = useState<DevCompare | null>(null);
  const [st, setSt] = useState<DevGitStatus | null>(null);
  const [refs, setRefs] = useState<DevRefs | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [msg, setMsg] = useState(() => sessionStorage.getItem(`waw.dev.commitmsg.${dev.id}`) ?? "");
  useEffect(() => { sessionStorage.setItem(`waw.dev.commitmsg.${dev.id}`, msg); }, [msg, dev.id]);
  const [busy, setBusy] = useState<string | null>(null);
  const [draftJob, setDraftJob] = useState<string | null>(null);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  // per-file folding; "all": every file folded / unfolded at once
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const viewedKey = `waw.dev.viewed.${dev.id}`;
  const [viewed, setViewedMap] = useState<Record<string, string>>(() => { try { return JSON.parse(localStorage.getItem(viewedKey) ?? "{}"); } catch { return {}; } });
  useEffect(() => { localStorage.setItem(viewedKey, JSON.stringify(viewed)); }, [viewed, viewedKey]);
  const fileRefs = useRef<Record<string, HTMLElement | null>>({});

  const seq = useRef(0); // only the latest load is shown (an earlier, slower one must not overwrite it)
  const load = useCallback(async () => {
    const n = ++seq.current;
    setLoading(true);
    try {
      const q = new URLSearchParams({ ...query(mode), ...(scope ? { path: scope } : {}) }).toString();
      const [c, s, r] = await Promise.all([
        get<DevCompare>(`/dev/workspaces/${dev.id}/compare${q ? `?${q}` : ""}`),
        get<DevGitStatus>(`/dev/workspaces/${dev.id}/status`),
        get<DevRefs>(`/dev/workspaces/${dev.id}/refs`),
      ]);
      if (n !== seq.current) return;
      setCmp(c);
      setSt(s);
      setRefs(r);
      setExcluded((prev) => new Set(s.files.map((f) => f.path).filter((p) => prev.has(p) || (s.files.find((f) => f.path === p)?.status === "untracked" && isArtifact(p)))));
      setErr(null);
    } catch (e) {
      if (n !== seq.current) return;
      setErr((e as ApiError).message);
      if ((e as ApiError).code === "NO_BASE" || (e as ApiError).code === "INVALID_REF") setMode("wt");
    } finally {
      if (n === seq.current) setLoading(false);
    }
  }, [dev.id, mode, scope]);
  useEffect(() => { void load(); }, [load, refreshKey]);
  // the worktree changes behind our back (editor saves, the agent, the terminal): look again when coming back
  useEffect(() => {
    const onFocus = () => { if (document.visibilityState === "visible") void load(); };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [load]);

  useJobStream(draftJob, (job) => {
    setDraftJob(null);
    if (job.status === "completed" && job.text) setMsg(job.text.trim().replace(/^```\w*\n?|```$/g, "").trim());
    else if (job.error) setErr(job.error.message);
  });
  const run = async (name: string, fn: () => Promise<void>) => {
    setBusy(name);
    setErr(null);
    setNote(null);
    try {
      await fn();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };

  const files = cmp?.files ?? [];
  const isWt = !!cmp?.worktree;
  const uncommitted = st?.files ?? [];
  const selected = uncommitted.map((f) => f.path).filter((p) => !excluded.has(p));
  const isViewed = (f: DevDiffFile) => viewed[`${mode}:${f.path}`] === fingerprint(f.unified);
  const setViewed = (f: DevDiffFile, on: boolean) => {
    setViewedMap((m) => { const n = { ...m }; if (on) n[`${mode}:${f.path}`] = fingerprint(f.unified); else delete n[`${mode}:${f.path}`]; return n; });
    setCollapsed((c) => ({ ...c, [f.path]: on }));
  };
  const isCollapsed = (f: DevDiffFile) => collapsed[f.path] ?? isViewed(f);
  const viewedCount = files.filter(isViewed).length;
  const commit = (sha: string) => refs?.branch_commits.find((c) => c.sha === sha) ?? refs?.commits.find((c) => c.sha === sha);
  const label = mode === "wt" ? "the uncommitted changes" : mode === "branch" ? `everything on the branch (vs ${refs?.base_label ?? "its base"})`
    : mode === "branch-committed" ? `the branch's commits (vs ${refs?.base_label ?? "its base"})`
    : mode.startsWith("c:") ? `commit ${commit(mode.slice(2))?.short ?? mode.slice(2, 10)} (${commit(mode.slice(2))?.subject ?? ""})` : `the worktree vs ${mode.slice(2)}`;
  const targetRef = cmp?.target_sha ?? "worktree";
  const goTo = (p: string) => {
    setCollapsed((c) => ({ ...c, [p]: false }));
    requestAnimationFrame(() => fileRefs.current[p]?.scrollIntoView({ block: "start", behavior: "smooth" }));
  };
  const statusOf = (p: string) => uncommitted.find((f) => f.path === p)?.status;

  // the commit panel, as on the wiki's Diff page: opened with "Commit…" at the top
  const [showCommit, setShowCommit] = useState(() => sessionStorage.getItem(`waw.dev.commitOpen.${dev.id}`) === "1");
  useEffect(() => { sessionStorage.setItem(`waw.dev.commitOpen.${dev.id}`, showCommit ? "1" : "0"); }, [showCommit, dev.id]);
  const allSelected = uncommitted.length > 0 && selected.length === uncommitted.length;
  const commitPanel = st && (
    <div className="commit-panel dev-commit-panel" data-testid="dev-commit-panel">
      <div className="commit-head">
        <div>
          <b className="mono">{st.branch}</b> <span className="muted small">at {st.head || "(no commit yet)"}{st.subject ? ` — ${st.subject}` : ""}</span>
          <span className="muted small">
            {st.push_remote ? <> → {st.push_remote === "origin" ? "origin" : "remote"}/{st.push_branch}</> : " · no remote"}
            {st.commits_on_branch !== null ? ` · ${st.commits_on_branch} commit(s) on the branch` : ""}
            {st.unpushed ? <> · <span className="warn">{st.unpushed} not pushed</span></> : st.push_remote ? " · all pushed" : ""}
          </span>
        </div>
        <div className="muted small" title={`${st.identity.name} <${st.identity.email}>`}>
          {st.identity.configured ? `as ${st.identity.name}` : "⚠ git user.name/email not configured"}
        </div>
      </div>

      <section>
        <div className="label">
          <label><input type="checkbox" checked={allSelected} ref={(el) => { if (el) el.indeterminate = selected.length > 0 && !allSelected; }} disabled={!uncommitted.length}
            onChange={(e) => setExcluded(e.target.checked ? new Set() : new Set(uncommitted.map((f) => f.path)))} data-testid="dev-select-all" />{" "}
            {uncommitted.length ? `${selected.length} of ${uncommitted.length} changed files selected` : "Working tree is clean"}</label>
        </div>
        <div className="commit-files">
          {uncommitted.map((f) => (
            <label key={f.path} className={`commit-file ${excluded.has(f.path) ? "excluded" : ""} ${scope && !inScope(f.path, scope) ? "out-of-scope" : ""}`} title={isArtifact(f.path) ? "Looks like a build output or cache" : f.path}>
              <input type="checkbox" checked={!excluded.has(f.path)} data-testid="dev-file" aria-label={`Commit ${f.path}`}
                onChange={(e) => setExcluded((x) => { const n = new Set(x); if (e.target.checked) n.delete(f.path); else n.add(f.path); return n; })} />
              <button className="link-btn path mono" onClick={(e) => { e.preventDefault(); if (mode !== "wt" && mode !== "branch" && !mode.startsWith("b:")) setMode("wt"); if (scope && !inScope(f.path, scope)) onScope(null); goTo(f.path); }} title="Show its diff" data-testid="dev-file-go">{f.path}</button>
              <span className={`chip git ${f.status}`}>{f.status}</span>
            </label>
          ))}
        </div>
      </section>

      <section>
        <div className="label">
          Commit message
          <button className="btn small" disabled={!selected.length || !!busy || !!draftJob} data-testid="dev-commit-draft" title="Let the agent summarise the selected changes into a commit message (you can edit it)"
            onClick={() => void run("draft", async () => { setDraftJob((await post<{ job_id: string }>(`/dev/workspaces/${dev.id}/draft?what=commit`, { paths: selected })).job_id); })}>{draftJob ? "Drafting…" : "Draft with agent"}</button>
        </div>
        <textarea value={msg} onChange={(e) => setMsg(e.target.value)} rows={6} placeholder="Subject line (≤72 chars), blank line, then details. Or click 'Draft with agent'." data-testid="dev-commit-msg" />
        <div className="row">
          <button className="btn primary" disabled={!selected.length || !msg.trim() || !!busy || !st.identity.configured} data-testid="dev-commit"
            onClick={() => void run("commit", async () => {
              const r = await post<{ commit: string; files: number }>(`/dev/workspaces/${dev.id}/commit`, { message: msg, paths: selected });
              setMsg("");
              setNote(`Committed ${r.files} file(s) as ${r.commit}.`);
              await load();
              onChanged();
            })}>{busy === "commit" ? "Committing…" : `Commit ${selected.length === uncommitted.length ? "all" : `${selected.length} file(s)`}`}</button>
          <span className="muted small">Commits only the selected files, with your Git identity. Nothing is pushed.</span>
        </div>
      </section>

      <section>
        <div className="label">Share</div>
        <div className="row">
          <button className="btn" disabled={!st.unpushed || !!busy || !st.push_remote} data-testid="dev-push" title={st.push_remote ? `git push ${st.push_remote} HEAD:${st.push_branch}` : "No remote to push to"}
            onClick={() => void run("push", async () => { await post(`/dev/workspaces/${dev.id}/push`); setNote(`Pushed to ${st.push_branch}.`); await load(); onChanged(); })}>
            {busy === "push" ? "Pushing…" : st.unpushed ? `Push ${st.unpushed} commit${st.unpushed > 1 ? "s" : ""}` : "Push"}</button>
          <span className="muted small">{st.push_remote ? `To ${st.push_branch} (never forced). The pull request is in the Pull request tab below the editor.` : "This checkout has no remote (add an origin in the terminal)."}</span>
        </div>
      </section>
      {note && <div className="small" data-testid="dev-note">{note}</div>}

      {refs && (refs.branch_commits.length > 0 || refs.commits.length > 0) && (
        <section>
          <div className="label">{refs.branch_commits.length ? "Commits on this branch" : "Recent commits"} <span className="muted small">— click one to see its changes</span></div>
          <ul className="recent-commits">
            {(refs.branch_commits.length ? refs.branch_commits : refs.commits.slice(0, 5)).map((c) => (
              <li key={c.sha}>
                <button className={`link-btn ${mode === `c:${c.sha}` ? "selected" : ""}`} onClick={() => setMode(`c:${c.sha}`)} title={`${c.author} · ${new Date(c.date).toLocaleString()}`} data-testid="dev-branch-commit">
                  <code>{c.short}</code> {c.subject}
                </button> <span className="muted small">· {c.author}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );

  return (
    <div className={`dev-changes ${wide ? "wide" : ""}`} data-testid="dev-changes">
      <div className="dev-diff" data-testid="dev-diff">
        <div className="dev-diff-bar">
          <span className="crumbs small"><span className="chip">DIFF</span> {scope ? null : <span className="muted">(whole {dev.mode === "folder" ? "folder" : "worktree"})</span>}</span>
          <select value={mode} onChange={(e) => setMode(e.target.value)} title="What to compare" data-testid="dev-compare">
            <option value="wt">Uncommitted changes</option>
            {refs?.base_ref && <option value="branch">Everything on this branch (vs {refs.base_label ?? "base"})</option>}
            {refs?.base_ref && <option value="branch-committed">Committed on this branch (vs {refs.base_label ?? "base"})</option>}
            {refs && refs.branch_commits.length > 0 && (
              <optgroup label="A commit of this branch">
                {refs.branch_commits.map((c) => <option key={c.sha} value={`c:${c.sha}`}>{c.short} {c.subject.slice(0, 70)}</option>)}
              </optgroup>
            )}
            {refs && (
              <optgroup label="An earlier commit">
                {refs.commits.filter((c) => !refs.branch_commits.some((b) => b.sha === c.sha)).slice(0, 30).map((c) => <option key={c.sha} value={`c:${c.sha}`}>{c.short} {c.subject.slice(0, 70)}</option>)}
              </optgroup>
            )}
            {refs && refs.branches.length > 0 && (
              <optgroup label="The worktree against a branch">
                {refs.branches.slice(0, 100).map((b) => <option key={b} value={`b:${b}`}>vs {b}</option>)}
              </optgroup>
            )}
          </select>
          {scope && (
            <span className="chip dev-scope" title={`Only the changes of ${scope}`} data-testid="dev-diff-scope">
              <span className="mono">{scope}</span>
              <button className="icon-btn small" onClick={() => onScope(null)} title="Show all changes" aria-label="Show all changes" data-testid="dev-diff-scope-clear">×</button>
            </span>
          )}
          <span className="spacer" />
          <span className="mode-switch" role="tablist" aria-label="Diff layout">
            <button role="tab" aria-selected={layout === "unified"} className={layout === "unified" ? "active" : ""} onClick={() => setLayout("unified")} data-testid="dev-layout-unified">Unified</button>
            <button role="tab" aria-selected={layout === "split"} className={layout === "split" ? "active" : ""} onClick={() => setLayout("split")} data-testid="dev-layout-split">Side by side</button>
          </span>
          <button className={`btn small toggle ${showCommit ? "on" : ""}`} onClick={() => setShowCommit((v) => !v)} aria-pressed={showCommit} data-testid="dev-commit-toggle"
            title="Select changed files, draft a commit message with the agent, commit, push">Commit…{uncommitted.length ? ` (${uncommitted.length})` : ""}</button>
          <button className="btn small" onClick={() => void load()} disabled={loading} title="Reload the diff" data-testid="dev-diff-reload">{loading ? "…" : "↻"}</button>
          <button className="btn small" onClick={onToggleWide} title={wide ? "Back to the editor (the changes stay in the bottom panel)" : "Show the changes in the editor area"} data-testid="dev-changes-wide">{wide ? "Back to editor" : "⤢ Expand"}</button>
        </div>
        {err && <div className="banner warn small" role="alert">{err}</div>}
        <div className="dev-diff-files">
        {showCommit && commitPanel}
        {cmp && files.length > 0 && (
          <div className="diff-summary muted small" data-testid="dev-diff-summary">
            {cmp.summary.files} file{cmp.summary.files === 1 ? "" : "s"} · <span className="add">+{cmp.summary.additions}</span> <span className="del">−{cmp.summary.deletions}</span>
            {" · "}{viewedCount} / {files.length} viewed
            {files.length > 1 && <>
              {" · "}<button className="link-btn" onClick={() => setCollapsed(Object.fromEntries(files.map((f) => [f.path, true])))}>collapse all</button>
              {" · "}<button className="link-btn" onClick={() => setCollapsed(Object.fromEntries(files.map((f) => [f.path, false])))}>expand all</button>
            </>}
            {" · "}click a line to ask the agent, drag or shift-click to select several lines
          </div>
        )}
        {cmp && files.length === 0 && <div className="muted small dev-hint" data-testid="dev-diff-empty">{scope ? `No changes in ${scope}. ` : ""}{!scope && isWt && mode === "wt" ? "The worktree has no uncommitted changes." : !scope ? "No differences." : ""}{scope && <button className="link-btn" onClick={() => onScope(null)}>Show all changes</button>}</div>}
          {files.map((f) => (
            <div key={f.path} ref={(el) => { fileRefs.current[f.path] = el; }}>
              <DevFileDiff file={f} layout={layout} devId={dev.id} targetRef={targetRef} collapsed={isCollapsed(f)} viewed={isViewed(f)}
                onCollapsed={(c) => setCollapsed((m) => ({ ...m, [f.path]: c }))} onViewed={(v) => setViewed(f, v)}
                inCommit={isWt && statusOf(f.path) !== undefined ? !excluded.has(f.path) : null}
                onInCommit={(on) => setExcluded((x) => { const n = new Set(x); if (on) n.delete(f.path); else n.add(f.path); return n; })}
                onOpen={onOpenFile && f.status !== "deleted" && isWt ? (line) => onOpenFile(f.path, line) : undefined}
                onAsk={(text, sel) => onAsk({
                  text,
                  // lines of the current file (new side) anchor the discussion there; otherwise it is about the file
                  anchor: f.status === "deleted" || !isWt ? undefined
                    : sel && sel.start ? { root: "wt", file: f.path, start_line: sel.start, end_line: sel.end, quote: sel.quote } : { root: "wt", file: f.path, quote: "" },
                  context: { diff: sel ? sel.diff : f.unified.slice(0, 30000), diff_label: `${sel ? `lines of ${f.path}` : f.path} in ${label}`, ...(isWt ? {} : { file: f.path }) },
                })}
                onError={setErr} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

type Sel = { from: number; to: number };
export interface AskSel { start: number | null; end: number | null; quote: string; diff: string }

/** One changed file: header (fold, copy path, open, ask, viewed, in the commit) and its diff with more context on request. */
function DevFileDiff({ file, layout, devId, targetRef, collapsed, viewed, inCommit, onCollapsed, onViewed, onInCommit, onOpen, onAsk, onError }: {
  file: DevDiffFile;
  layout: "unified" | "split";
  devId: string;
  targetRef: string;
  collapsed: boolean;
  viewed: boolean;
  /** null: not an uncommitted file (cannot be picked for the commit) */
  inCommit: boolean | null;
  onCollapsed: (c: boolean) => void;
  onViewed: (v: boolean) => void;
  onInCommit: (on: boolean) => void;
  onOpen?: (line?: number) => void;
  onAsk: (text: string, sel: AskSel | null) => Promise<void>;
  onError: (m: string) => void;
}) {
  const base = useMemo(() => parseUnified(file.unified).lines, [file.unified]);
  const [content, setContent] = useState<string[] | null>(null);
  const [reveal, setReveal] = useState<Record<number, { top: number; bottom: number }>>({});
  const [loadingContent, setLoadingContent] = useState(false);
  useEffect(() => { setContent(null); setReveal({}); }, [file.unified, targetRef]);
  const canExpand = !file.binary && !file.too_large && !["added", "untracked", "deleted"].includes(file.status) && base.length > 0;
  const { lines, expanders } = useMemo(() => expandLines(base, content, reveal, canExpand), [base, content, reveal, canExpand]);
  const rows = useMemo(() => (layout === "split" ? buildSplitRows(lines) : []), [layout, lines]);
  const [copied, setCopied] = useState(false);
  const [sel, setSel] = useState<Sel | null>(null);
  const [composer, setComposer] = useState<Sel | null>(null);
  const [fileComposer, setFileComposer] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const anchorIdx = useRef<number | null>(null);
  const dragging = useRef(false);
  const prevLines = useRef(lines);
  useEffect(() => { // expanding inserts lines: keep a selection on the same lines
    const prev = prevLines.current;
    prevLines.current = lines;
    if (prev === lines) return;
    const map = (x: Sel | null) => { if (!x) return x; const a = lines.indexOf(prev[x.from]); const b = lines.indexOf(prev[x.to]); return a >= 0 && b >= 0 ? { from: a, to: b } : null; };
    setSel(map);
    setComposer(map);
  }, [lines]);
  useEffect(() => {
    const up = () => { if (!dragging.current) return; dragging.current = false; setSel((s) => { if (s) setComposer(s); return s; }); };
    window.addEventListener("pointerup", up);
    return () => window.removeEventListener("pointerup", up);
  }, []);

  const loadContent = async (): Promise<string[] | null> => {
    if (content) return content;
    setLoadingContent(true);
    try {
      const r = await get<{ lines: string[] }>(`/dev/workspaces/${devId}/compare/file?path=${encodeURIComponent(file.path)}&ref=${encodeURIComponent(targetRef)}`);
      setContent(r.lines);
      return r.lines;
    } catch (e) {
      onError((e as ApiError).message);
      return null;
    } finally {
      setLoadingContent(false);
    }
  };
  const expand = async (g: Gap, how: "up" | "down" | "all") => {
    const c = await loadContent();
    if (!c) return;
    setReveal((r) => {
      const cur = r[g.k] ?? { top: 0, bottom: 0 };
      const to = g.k === TRAILING ? c.length : g.to;
      const remaining = Math.max(0, to - g.from + 1);
      if (how === "all" || remaining <= EXPAND_STEP) return { ...r, [g.k]: { top: cur.top + remaining, bottom: cur.bottom } };
      return { ...r, [g.k]: how === "down" ? { ...cur, top: cur.top + EXPAND_STEP } : { ...cur, bottom: cur.bottom + EXPAND_STEP } };
    });
  };
  const fullyExpanded = !!content && expanders.size === 0;

  const selectable = (i: number) => !!lines[i] && ["add", "del", "ctx"].includes(lines[i].kind);
  const start = (i: number, shift: boolean) => {
    if (!selectable(i)) return;
    if (shift && sel) {
      const a = anchorIdx.current ?? sel.from;
      const r = { from: Math.min(a, i), to: Math.max(a, i) };
      setSel(r);
      setComposer(r);
      return;
    }
    anchorIdx.current = i;
    dragging.current = true;
    setSel({ from: i, to: i });
    setComposer(null);
  };
  const extend = (i: number) => {
    if (!dragging.current || anchorIdx.current === null || !selectable(i)) return;
    setSel({ from: Math.min(anchorIdx.current, i), to: Math.max(anchorIdx.current, i) });
  };
  const inSel = (i: number) => !!sel && i >= sel.from && i <= sel.to;
  const cancel = () => { setComposer(null); setSel(null); setText(""); };
  const chosen: Line[] = composer ? lines.slice(composer.from, composer.to + 1).filter((l) => ["add", "del", "ctx"].includes(l.kind)) : [];
  const newSide = chosen.filter((l) => l.newNo !== null);
  const range = chosen.length ? (() => {
    const n = (l: Line) => (l.newNo !== null ? `${l.newNo}` : `old ${l.oldNo}`);
    return chosen.length === 1 ? `Line ${n(chosen[0])}` : `Lines ${n(chosen[0])}–${n(chosen[chosen.length - 1])}`;
  })() : "";
  const askSel = (): AskSel => ({
    start: newSide[0]?.newNo ?? null, end: newSide[newSide.length - 1]?.newNo ?? null,
    quote: newSide.map((l) => l.text).join("\n"), diff: chosen.map((l) => l.raw).join("\n"),
  });
  const send = async (sel: AskSel | null) => {
    const q = text.trim();
    if (!q) return;
    setBusy(true);
    try {
      await onAsk(q, sel);
      cancel();
      setFileComposer(false);
    } catch (e) {
      onError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const cell = (i: number) => ({
    onPointerDown: (e: React.PointerEvent) => { if (e.button !== 0) return; e.preventDefault(); start(i, e.shiftKey); },
    onPointerEnter: () => extend(i),
  });
  const composerNode = composer && (
    <div className="diff-composer" data-testid="dev-diff-composer" onPointerDown={(e) => e.stopPropagation()}>
      <div className="muted small">{range} of {file.path}</div>
      <textarea autoFocus rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder={`Ask the agent about these lines (${COMPOSER_HINT})`}
        onKeyDown={(e) => composerKeyDown(e, () => void send(askSel()), setText, cancel)} data-testid="dev-diff-question" />
      <div className="row">
        <button className="btn small primary" disabled={busy || !text.trim()} onClick={() => void send(askSel())} title="The answer appears in the agent panel" data-testid="dev-diff-ask">Ask agent</button>
        {onOpen && newSide.length > 0 && <button className="btn small" onClick={() => onOpen(newSide[0].newNo!)} data-testid="dev-diff-open-line">Open in editor</button>}
        <button className="btn small" onClick={cancel}>Cancel</button>
      </div>
    </div>
  );
  const expanderButtons = (g: Gap | undefined) => {
    if (!g || !canExpand) return null;
    const n = g.k === TRAILING ? (content ? content.length - g.from + 1 : null) : g.to - g.from + 1;
    if (n !== null && n <= 0) return null;
    const b = (how: "up" | "down" | "all", glyph: string, title: string) => (
      <button className="expander" disabled={loadingContent} onClick={(e) => { e.stopPropagation(); void expand(g, how); }} title={title} data-testid={`dev-expand-${how}`}>{glyph}</button>
    );
    if (n !== null && n <= EXPAND_STEP) return b("all", "↕", `Show ${n} hidden line${n === 1 ? "" : "s"}`);
    return (
      <span className="expanders">
        {g.k !== 0 && b("down", "↓", `Show ${EXPAND_STEP} more lines below the previous change`)}
        {g.k !== TRAILING && b("up", "↑", `Show ${EXPAND_STEP} more lines above this change`)}
        {n !== null && b("all", "↕", `Show all ${n} hidden lines`)}
      </span>
    );
  };
  const noText = file.binary || file.too_large || base.length === 0;

  return (
    <section className={`diff-file ${collapsed ? "collapsed" : ""}`} data-testid="dev-diff-file" data-path={file.path} data-viewed={viewed ? "1" : "0"}>
      <header className="diff-file-head" onClick={() => onCollapsed(!collapsed)}>
        <span className="chev">{collapsed ? "›" : "⌄"}</span>
        {inCommit !== null && (
          <input type="checkbox" checked={inCommit} onClick={(e) => e.stopPropagation()} onChange={(e) => onInCommit(e.target.checked)} title="Include in the commit" aria-label="Include in the commit" data-testid="dev-diff-in-commit" />
        )}
        <span className="diff-path" title={file.path}>{file.previous_path ? `${file.previous_path} → ` : ""}{file.path}</span>
        <button className={`icon-btn small copy-path ${copied ? "done" : ""}`} title={copied ? "Copied" : "Copy the file path"} aria-label="Copy the file path"
          onClick={(e) => { e.stopPropagation(); void writeClipboard(file.path).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }); }}>{copied ? "✓" : "⧉"}</button>
        <span className={`chip git ${file.status}`}>{file.status}</span>
        <span className="muted small"><span className="add">+{file.additions}</span> <span className="del">−{file.deletions}</span></span>
        <span className="spacer" />
        {!noText && <button className="btn small" onClick={(e) => { e.stopPropagation(); onCollapsed(false); setFileComposer(true); }} title="Ask the agent about this file's changes" data-testid="dev-diff-ask-file">Ask agent</button>}
        {canExpand && (fullyExpanded
          ? <button className="btn small" onClick={(e) => { e.stopPropagation(); setReveal({}); }} title="Show only the changed lines again">Collapse expanded</button>
          : <button className="btn small" disabled={loadingContent} onClick={(e) => { e.stopPropagation(); onCollapsed(false); void loadContent().then((c) => { if (c) setReveal(ALL); }); }} title="Show the whole file around the changes" data-testid="dev-diff-expand-all">{loadingContent ? "Loading…" : "Expand all"}</button>)}
        {onOpen && <button className="btn small" onClick={(e) => { e.stopPropagation(); onOpen(); }} title="Open the file in the editor" data-testid="dev-diff-open">Open</button>}
        <label className={`viewed-box ${viewed ? "on" : ""}`} onClick={(e) => e.stopPropagation()} title="Mark as viewed: the file folds; the mark goes when its change changes">
          <input type="checkbox" checked={viewed} onChange={(e) => onViewed(e.target.checked)} data-testid="dev-diff-viewed" />
          <span>Viewed</span>
        </label>
      </header>
      {!collapsed && fileComposer && (
        <div className="pr-file-level pad">
          <div className="diff-composer" data-testid="dev-diff-file-composer">
            <div className="muted small">All of this file's changes</div>
            <textarea autoFocus rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder={`Ask the agent about ${file.path} (${COMPOSER_HINT})`}
              onKeyDown={(e) => composerKeyDown(e, () => void send(null), setText, () => setFileComposer(false))} data-testid="dev-diff-file-question" />
            <div className="row">
              <button className="btn small primary" disabled={busy || !text.trim()} onClick={() => void send(null)} data-testid="dev-diff-file-send">Ask agent</button>
              <button className="btn small" onClick={() => { setFileComposer(false); setText(""); }}>Cancel</button>
            </div>
          </div>
        </div>
      )}
      {!collapsed && noText && (
        <div className="muted small pad">{file.binary ? "Binary file." : file.too_large ? "The diff is too large to show here." : file.status === "renamed" ? "Renamed without changes." : "No textual change (e.g. file mode)."}</div>
      )}
      {!collapsed && !noText && layout === "unified" && (
        <table className="diff-table" data-testid="dev-diff-unified" style={numColStyle(lines)}>
          <UnifiedCols />
          <tbody>
            {lines.map((ln, i) => {
              if (ln.kind === "file") return null;
              if (ln.kind === "hunk") return <tr key={i} className="diff-line hunk"><td className="no expander-cell" colSpan={2}>{expanderButtons(expanders.get(i))}</td><td className="code">{ln.raw}</td></tr>;
              return (
                <Fragment key={i}>
                  <tr className={`diff-line ${ln.kind} ${ln.expanded ? "expanded" : ""} ${inSel(i) ? "selected" : ""}`} {...cell(i)} data-testid="dev-diff-row" data-new={ln.newNo ?? ""} title="Click to ask about this line; drag or shift-click for several"
                    onDoubleClick={() => { if (onOpen && ln.newNo) onOpen(ln.newNo); }}>
                    <td className="no">{ln.oldNo ?? ""}</td>
                    <td className="no">{ln.newNo ?? ""}</td>
                    <td className="code"><span className="marker">{ln.kind === "add" ? "+" : ln.kind === "del" ? "-" : " "}</span>{ln.text || " "}</td>
                  </tr>
                  {composer?.to === i && <tr className="diff-composer-row"><td colSpan={3}>{composerNode}</td></tr>}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
      {!collapsed && !noText && layout === "split" && (
        <table className="diff-table split" data-testid="dev-diff-split" style={numColStyle(lines)}>
          <SplitCols />
          <tbody>
            {rows.map((row, ri) => {
              if (row.hunk !== undefined) return <tr key={ri} className="diff-line hunk"><td className="no expander-cell">{expanderButtons(expanders.get(row.hunk))}</td><td className="code" colSpan={3}>{lines[row.hunk].raw}</td></tr>;
              const L = row.left !== null ? lines[row.left] : null;
              const R = row.right !== null ? lines[row.right] : null;
              const here = !!composer && (row.left === composer.to || row.right === composer.to);
              return (
                <Fragment key={ri}>
                  <tr className={`diff-line split-row ${L?.expanded ? "expanded" : ""}`} data-testid="dev-diff-row">
                    <td className={`no ${L ? L.kind : "empty"}`}>{L?.oldNo ?? ""}</td>
                    <td className={`code left ${L ? L.kind : "empty"} ${L && inSel(row.left!) ? "selected" : ""}`} {...(L ? cell(row.left!) : {})}>
                      {L ? <><span className="marker">{L.kind === "del" ? "-" : " "}</span>{L.text || " "}</> : ""}
                    </td>
                    <td className={`no ${R ? R.kind : "empty"}`}>{R?.newNo ?? ""}</td>
                    <td className={`code right ${R ? R.kind : "empty"} ${R && inSel(row.right!) ? "selected" : ""}`} {...(R ? cell(row.right!) : {})}
                      onDoubleClick={() => { if (onOpen && R?.newNo) onOpen(R.newNo); }}>
                      {R ? <><span className="marker">{R.kind === "add" ? "+" : " "}</span>{R.text || " "}</> : ""}
                    </td>
                  </tr>
                  {here && <tr className="diff-composer-row"><td colSpan={4}>{composerNode}</td></tr>}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
