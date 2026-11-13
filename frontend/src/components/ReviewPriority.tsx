import { useEffect, useRef, useState } from "react";
import { ApiError, get, post, put } from "../api";
import FolderPicker from "./FolderPicker";
import ModelChoice, { ModelChoiceValue } from "./ModelChoice";
import { utc8 } from "../time";

export interface PriorityLevel { id: string; name: string; color: string; descriptions: string[] }
export interface RuleCondition { field: string; op: string; value: string[] | string | number | boolean }
export interface PriorityRule { id: string; enabled: boolean; level: string; name?: string; conditions: RuleCondition[] }
export interface AgentModel { provider: string | null; model: string | null; variant: string | null }
export interface PriorityAgent { enabled: boolean; summary: AgentModel; decision: AgentModel }
export interface Category { key: "todo" | "news" | "quiet"; title: string; level: string }
export interface AgentStatus { queued: number; running: string[]; last_run: string | null; classified: number; files_summarizing: number; errors: number }

export const CATEGORY_HINT: Record<string, string> = {
  todo: "You have not reviewed or commented on these yet",
  news: "Since your latest review or comment: a review, reply or comment from someone else, new commits, or your review requested again (bots do not count)",
  quiet: "You reviewed or commented, and nothing happened since",
};

type Kind = "list" | "text" | "number" | "bool";
export const FIELDS: { key: string; label: string; kind: Kind; hint?: string }[] = [
  { key: "requested_by", label: "review asked by", kind: "list", hint: "GitHub logins" },
  { key: "requested_by_meta", label: "asked by a PyTorch Meta maintainer", kind: "bool" },
  { key: "author", label: "author", kind: "list", hint: "GitHub logins" },
  { key: "repo", label: "repository", kind: "list", hint: "owner/name" },
  { key: "label", label: "label", kind: "list", hint: "label names" },
  { key: "title", label: "title", kind: "text" },
  { key: "size", label: "size (+/− lines)", kind: "number" },
  { key: "age_days", label: "days since it was opened", kind: "number" },
  { key: "idle_days", label: "days since its last update", kind: "number" },
  { key: "ci", label: "CI", kind: "list", hint: "failing, passing, running, none" },
  { key: "draft", label: "draft", kind: "bool" },
  { key: "review_decision", label: "review decision", kind: "list", hint: "APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED" },
  { key: "my_activity", label: "your activity", kind: "list", hint: "todo (not reviewed), news (new activity), quiet" },
];
const OPS: Record<Kind, [string, string][]> = {
  list: [["in", "is one of"], ["not_in", "is not one of"]], text: [["contains", "contains"], ["not_contains", "does not contain"]],
  number: [[">=", "≥"], ["<=", "≤"]], bool: [["is", "is"]],
};
const kindOf = (f: string): Kind => FIELDS.find((x) => x.key === f)?.kind ?? "list";

const toChoice = (m: AgentModel): ModelChoiceValue | null => (m.provider || m.model || m.variant ? { provider: m.provider ?? undefined, model: m.model ?? undefined, variant: m.variant ?? undefined } : null);
const fromChoice = (v: ModelChoiceValue | null): AgentModel => ({ provider: v?.provider ?? null, model: v?.model ?? null, variant: v?.variant ?? null });

/**
 * ⚙ → Priority: your levels (name, colour, the descriptions the agent judges against), the default level, hard rules,
 * the agent (on/off, the models that summarize the changes and decide), and the order and names of the categories.
 * Edits are kept here until Save.
 */
export function PrioritySettings({ prefs, save, status }: {
  prefs: { priority_levels: PriorityLevel[]; priority_default: string; priority_rules: PriorityRule[]; priority_agent: PriorityAgent; categories: Category[] };
  save: (c: Record<string, unknown>) => Promise<void>;
  status: AgentStatus | null;
}) {
  const [levels, setLevels] = useState<PriorityLevel[]>(prefs.priority_levels);
  const [def, setDef] = useState(prefs.priority_default);
  const [rules, setRules] = useState<PriorityRule[]>(prefs.priority_rules);
  const [agent, setAgent] = useState<PriorityAgent>(prefs.priority_agent);
  const [cats, setCats] = useState<Category[]>(prefs.categories);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const snap = (x: typeof prefs) => JSON.stringify({ priority_levels: x.priority_levels, priority_default: x.priority_default, priority_rules: x.priority_rules, priority_agent: x.priority_agent, categories: x.categories });
  const base = useRef(snap(prefs));   // the server's settings the editor started from
  const adopt = useRef(false);        // take the server's settings at the next change (after Save / Reset)
  const current = JSON.stringify({ priority_levels: levels, priority_default: def, priority_rules: rules, priority_agent: agent, categories: cats });
  const dirty = current !== base.current;
  useEffect(() => { // the server's copy changed (Save, Reset, another tab): take it unless you are editing
    const now = snap(prefs);
    if (now === base.current) return;
    if (adopt.current || !dirty) {
      setLevels(prefs.priority_levels); setDef(prefs.priority_default); setRules(prefs.priority_rules); setAgent(prefs.priority_agent); setCats(prefs.categories);
      base.current = now;
      adopt.current = false;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefs]);
  const run = async (fn: () => Promise<void>, done: string) => {
    adopt.current = true;
    setBusy(true);
    setErr(null);
    setNote(null);
    try {
      await fn();
      setNote(done);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const ids = levels.map((l) => l.id);
  const move = <T,>(xs: T[], i: number, d: number): T[] => { const n = [...xs]; const j = i + d; if (j < 0 || j >= n.length) return xs; [n[i], n[j]] = [n[j], n[i]]; return n; };
  const setLevel = (i: number, patch: Partial<PriorityLevel>) => setLevels((ls) => ls.map((l, k) => (k === i ? { ...l, ...patch } : l)));
  const setRule = (i: number, patch: Partial<PriorityRule>) => setRules((rs) => rs.map((r, k) => (k === i ? { ...r, ...patch } : r)));
  const setCond = (i: number, j: number, patch: Partial<RuleCondition>) =>
    setRule(i, { conditions: rules[i].conditions.map((c, k) => (k === j ? { ...c, ...patch } : c)) });
  const newCond = (field = "label"): RuleCondition => {
    const k = kindOf(field);
    return { field, op: OPS[k][0][0], value: k === "bool" ? true : k === "number" ? 0 : k === "text" ? "" : [] };
  };
  const described = levels.some((l) => l.descriptions.length > 0);

  return (
    <div className="priority-settings" data-testid="priority-settings">
      <div className="small strong">Priority levels <span className="muted">(most urgent first)</span></div>
      <div className="muted small">
        A PR's level: the one you set by hand; otherwise the <b>more urgent</b> of the hard rules' level and the agent's level
        (from your descriptions); otherwise the default level.
      </div>
      {levels.map((lv, i) => (
        <div key={i} className="priority-level" data-testid="priority-level">
          <div className="row nowrap">
            <input type="color" value={lv.color} onChange={(e) => setLevel(i, { color: e.target.value })} title="Colour" aria-label={`Colour of ${lv.id}`} />
            <input className="mono priority-id" value={lv.id} onChange={(e) => {
              const v = e.target.value.trim();
              const old = lv.id;
              setLevel(i, { id: v });
              setRules((rs) => rs.map((r) => (r.level === old ? { ...r, level: v } : r)));
              if (def === old) setDef(v);
            }} aria-label="Level id" data-testid="priority-level-id" />
            <input value={lv.name} onChange={(e) => setLevel(i, { name: e.target.value })} placeholder="name" aria-label="Level name" />
            <label className="small" title="PRs that no rule, the agent or you put elsewhere"><input type="radio" checked={def === lv.id} onChange={() => setDef(lv.id)} /> default</label>
            <button className="icon-btn small" onClick={() => setLevels((ls) => move(ls, i, -1))} title="More urgent" disabled={i === 0}>↑</button>
            <button className="icon-btn small" onClick={() => setLevels((ls) => move(ls, i, 1))} title="Less urgent" disabled={i === levels.length - 1}>↓</button>
            <button className="icon-btn small" onClick={() => setLevels((ls) => ls.filter((_, k) => k !== i))} disabled={levels.length <= 1} title="Remove this level">×</button>
          </div>
          <div className="priority-descs">
            {lv.descriptions.map((d, j) => (
              <div key={j} className="row nowrap">
                <textarea rows={1} value={d} onChange={(e) => setLevel(i, { descriptions: lv.descriptions.map((x, k) => (k === j ? e.target.value : x)) })}
                  placeholder="e.g. Fixes a crash or a memory error in the XPU runtime" data-testid="priority-desc" />
                <button className="icon-btn small" onClick={() => setLevel(i, { descriptions: lv.descriptions.filter((_, k) => k !== j) })} title="Remove">×</button>
              </div>
            ))}
            <button className="link-btn small" onClick={() => setLevel(i, { descriptions: [...lv.descriptions, ""] })} data-testid="priority-desc-add">+ description (for the agent)</button>
          </div>
        </div>
      ))}
      <button className="link-btn small" onClick={() => setLevels((ls) => [...ls, { id: `P${ls.length}`, name: "", color: "#6e7781", descriptions: [] }])}>+ level</button>

      <div className="small strong">Hard rules</div>
      <div className="muted small">All conditions of a rule must match. When several rules match, the most urgent level wins.</div>
      {rules.map((r, i) => (
        <div key={i} className={`priority-rule ${r.enabled ? "" : "off"}`} data-testid="priority-rule">
          <div className="row nowrap">
            <input type="checkbox" checked={r.enabled} onChange={(e) => setRule(i, { enabled: e.target.checked })} title="On / off" />
            <span className="small">then</span>
            <select value={r.level} onChange={(e) => setRule(i, { level: e.target.value })} aria-label="Level" data-testid="priority-rule-level">
              {ids.map((x) => <option key={x} value={x}>{x}</option>)}
            </select>
            <input value={r.name ?? ""} onChange={(e) => setRule(i, { name: e.target.value })} placeholder="name (optional)" />
            <span className="spacer" />
            <button className="icon-btn small" onClick={() => setRules((rs) => rs.filter((_, k) => k !== i))} title="Remove this rule" data-testid="priority-rule-remove">×</button>
          </div>
          {r.conditions.map((c, j) => {
            const k = kindOf(c.field);
            return (
              <div key={j} className="row nowrap priority-cond">
                <span className="muted small">{j === 0 ? "if" : "and"}</span>
                <select value={c.field} onChange={(e) => setCond(i, j, newCond(e.target.value))} aria-label="Field" data-testid="priority-cond-field">
                  {FIELDS.map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
                </select>
                <select value={c.op} onChange={(e) => setCond(i, j, { op: e.target.value })} aria-label="Operator">
                  {OPS[k].map(([o, l]) => <option key={o} value={o}>{l}</option>)}
                </select>
                {k === "bool" ? (
                  <select value={c.value ? "yes" : "no"} onChange={(e) => setCond(i, j, { value: e.target.value === "yes" })} aria-label="Value"><option value="yes">yes</option><option value="no">no</option></select>
                ) : (
                  <input value={Array.isArray(c.value) ? c.value.join(", ") : String(c.value)} placeholder={FIELDS.find((f) => f.key === c.field)?.hint ?? ""}
                    onChange={(e) => setCond(i, j, { value: k === "list" ? e.target.value.split(",").map((x) => x.trim()).filter(Boolean) : k === "number" ? Number(e.target.value) || 0 : e.target.value })}
                    aria-label="Value" data-testid="priority-cond-value" />
                )}
                <button className="icon-btn small" onClick={() => setRule(i, { conditions: r.conditions.filter((_, x) => x !== j) })} disabled={r.conditions.length <= 1} title="Remove this condition">×</button>
              </div>
            );
          })}
          <button className="link-btn small" onClick={() => setRule(i, { conditions: [...r.conditions, newCond()] })}>+ condition</button>
        </div>
      ))}
      <button className="link-btn small" onClick={() => setRules((rs) => [...rs, { id: `r${Date.now()}`, enabled: true, level: ids[0], name: "", conditions: [newCond()] }])} data-testid="priority-rule-add">+ hard rule</button>

      <div className="small strong">The agent</div>
      <label className="small">
        <input type="checkbox" checked={agent.enabled} onChange={(e) => setAgent({ ...agent, enabled: e.target.checked })} data-testid="priority-agent-on" />{" "}
        Let the agent decide levels from your descriptions (new and changed PRs, in the background)
      </label>
      {agent.enabled && !described && <div className="warn small">Add descriptions to your levels: the agent judges PRs against them.</div>}
      <div className="muted small">
        It reads each PR's details, description, changed files and diff; a large diff is first summarized file by file (one turn per
        ~25k characters, at most 60 files), then it decides. Summaries are reused until a file changes. Each new or changed PR costs
        a few agent turns; turns only read.
      </div>
      <div className="priority-models">
        <span className="small">Summaries:</span> <ModelChoice value={toChoice(agent.summary)} onChange={(v) => setAgent({ ...agent, summary: fromChoice(v) })} provider={agent.summary.provider} />
        <span className="small">Decision:</span> <ModelChoice value={toChoice(agent.decision)} onChange={(v) => setAgent({ ...agent, decision: fromChoice(v) })} provider={agent.decision.provider} />
      </div>
      {status && (
        <div className="muted small" data-testid="priority-agent-status">
          {status.running.length + status.queued ? `classifying ${status.running.length + status.queued} PR(s)${status.files_summarizing ? ` · summarizing ${status.files_summarizing} file(s)` : ""}…` : "idle"}
          {status.last_run ? ` · last decision ${utc8(status.last_run)}` : ""}{status.errors ? ` · ${status.errors} failed (retried at the next refresh)` : ""}
        </div>
      )}
      {prefs.priority_agent.enabled && (
        <button className="btn small" disabled={busy} onClick={() => void run(async () => { await post("/github/priority/classify", { all: true }); }, "The agent decides every PR's level again.")} data-testid="priority-reclassify-all">Re-classify all</button>
      )}

      <div className="small strong">Categories <span className="muted">(their order inside each repository)</span></div>
      {cats.map((c, i) => (
        <div key={c.key} className="row nowrap" data-testid="priority-category" data-key={c.key}>
          <input value={c.title} onChange={(e) => setCats((cs) => cs.map((x, k) => (k === i ? { ...x, title: e.target.value } : x)))} title={CATEGORY_HINT[c.key]} aria-label="Category name" />
          <input className="priority-cat-level" value={c.level} onChange={(e) => setCats((cs) => cs.map((x, k) => (k === i ? { ...x, level: e.target.value } : x)))} placeholder="tag" aria-label="Category tag" />
          <button className="icon-btn small" onClick={() => setCats((cs) => move(cs, i, -1))} disabled={i === 0} title="Earlier" data-testid="priority-category-up">↑</button>
          <button className="icon-btn small" onClick={() => setCats((cs) => move(cs, i, 1))} disabled={i === cats.length - 1} title="Later">↓</button>
        </div>
      ))}

      <div className="row">
        <button className="btn small primary" disabled={!dirty || busy} data-testid="priority-save"
          onClick={() => void run(() => save({ priority_levels: levels.map((l) => ({ ...l, descriptions: l.descriptions.filter((d) => d.trim()) })), priority_default: def, priority_rules: rules, priority_agent: agent, categories: cats }), "Saved.")}>Save priority settings</button>
        {dirty && <button className="btn small" onClick={() => { setLevels(prefs.priority_levels); setDef(prefs.priority_default); setRules(prefs.priority_rules); setAgent(prefs.priority_agent); setCats(prefs.categories); }}>Undo changes</button>}
        <span className="spacer" />
        <button className="link-btn small" disabled={busy} onClick={() => void run(() => save({ reset_priority: true }), "Back to the defaults.")} data-testid="priority-reset">Reset to defaults</button>
      </div>
      {note && <div className="small" data-testid="priority-note">{note}</div>}
      {err && <div className="error-text small" role="alert" data-testid="priority-error">{err}</div>}
    </div>
  );
}

/** A PR's level as a coloured badge; click: set it by hand, back to rules / agent, ask the agent again. */
export function LevelMenu({ pr, level, source, reasons, levels, agentOn, onSet, compact = false }: {
  pr: string; level: string | undefined; source: string | undefined; reasons: string[] | undefined; levels: PriorityLevel[];
  agentOn: boolean; onSet: (level: string | null) => Promise<void>; compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const off = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", off);
    return () => document.removeEventListener("mousedown", off);
  }, [open]);
  const lv = levels.find((l) => l.id === level);
  const act = async (fn: () => Promise<unknown>) => { setBusy(true); try { await fn(); } finally { setBusy(false); setOpen(false); } };
  const why = (reasons ?? []).join("\n");
  return (
    <span className="level-menu" ref={ref} onClick={(e) => e.stopPropagation()}>
      <span role="button" tabIndex={0} className={`level-badge ${source === "manual" ? "manual" : ""} ${source === "default" ? "default" : ""}`}
        style={lv ? { background: lv.color } : undefined} title={`${level ?? "?"}${lv?.name ? ` · ${lv.name}` : ""}\n${why}\n\nClick to set the level by hand`}
        onClick={() => setOpen((v) => !v)} onKeyDown={(e) => { if (e.key === "Enter") setOpen((v) => !v); }} data-testid="review-level" data-level={level} data-source={source}>
        {level ?? "?"}{compact ? "" : lv?.name ? ` ${lv.name}` : ""}{source === "manual" ? " ✎" : source === "agent" ? " 🤖" : ""}
      </span>
      {open && (
        <span className="level-pop" data-testid="level-pop">
          {why && <span className="muted small level-why">{why}</span>}
          {levels.map((l) => (
            <button key={l.id} className={`level-opt ${l.id === level && source === "manual" ? "on" : ""}`} disabled={busy} onClick={() => void act(() => onSet(l.id))} data-testid="level-set" data-level={l.id}>
              <span className="level-dot" style={{ background: l.color }} /> {l.id} {l.name}
            </button>
          ))}
          {source === "manual" && <button className="level-opt" disabled={busy} onClick={() => void act(() => onSet(null))} data-testid="level-auto">↺ Back to rules / agent</button>}
          {agentOn && source !== "manual" && (
            <button className="level-opt" disabled={busy} onClick={() => void act(() => post("/github/priority/classify", { pr }))} data-testid="level-reclassify">🤖 Ask the agent again</button>
          )}
        </span>
      )}
    </span>
  );
}

interface CacheInfo { dir: string; cap_mb: number; prefetch: boolean; bytes: number; prs: number; queued: number; prefetching: number; prefetched: number }

/** ⚙ → Cache: where PR Review keeps its local copies (opened at once, checked on GitHub right after), size, prefetch. */
export function CacheSettings() {
  const [info, setInfo] = useState<CacheInfo | null>(null);
  const [dir, setDir] = useState("");
  const [cap, setCap] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pick, setPick] = useState(false);
  const load = async () => {
    try {
      const r = await get<CacheInfo>("/github/cache");
      setInfo(r);
      setDir((d) => d || r.dir);
      setCap((c) => c || String(r.cap_mb));
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  useEffect(() => { void load(); }, []);
  const save = async (body: Record<string, unknown>, done: string) => {
    setBusy(true);
    setErr(null);
    setNote(null);
    try {
      const r = await put<CacheInfo>("/github/cache", body);
      setInfo(r);
      setDir(r.dir);
      setCap(String(r.cap_mb));
      setNote(done);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const mb = (b: number) => (b < 1024 * 1024 ? `${Math.ceil(b / 1024)} KB` : `${(b / 1024 / 1024).toFixed(1)} MB`);
  if (!info) return <div className="muted small">{err ?? "Loading…"}</div>;
  return (
    <div className="priority-settings" data-testid="cache-settings">
      <div className="muted small">
        Pull requests open at once from these local copies; GitHub is checked right after and the tab updates itself when
        something changed. Your actions, ↻ and polling always go to GitHub.
      </div>
      <div className="small" data-testid="cache-usage">
        {info.prs} pull request(s) · {mb(info.bytes)}{info.queued + info.prefetching > 0 ? ` · prefetching ${info.queued + info.prefetching}…` : ""}
      </div>
      <div className="small strong">Folder</div>
      <div className="row nowrap">
        <input className="mono" value={dir} onChange={(e) => setDir(e.target.value)} data-testid="cache-dir" />
        <button className="btn small" onClick={() => setPick(true)}>Browse…</button>
        <button className="btn small" disabled={busy || !dir.trim() || dir.trim() === info.dir} onClick={() => void save({ dir: dir.trim() }, "Moved the cache to the new folder.")} data-testid="cache-dir-save">Move here</button>
      </div>
      <div className="row nowrap small">
        Size limit <input className="priority-cat-level" value={cap} onChange={(e) => setCap(e.target.value.replace(/\D/g, ""))} data-testid="cache-cap" /> MB (0: none)
        <button className="btn small" disabled={busy || cap === String(info.cap_mb)} onClick={() => void save({ cap_mb: Number(cap) || 0 }, "Saved.")}>Save</button>
      </div>
      <div className="muted small">The pull requests opened longest ago go first; closed and merged ones after a week.</div>
      <label className="small">
        <input type="checkbox" checked={info.prefetch} disabled={busy} onChange={(e) => void save({ prefetch: e.target.checked }, e.target.checked ? "Prefetch on." : "Prefetch off.")} data-testid="cache-prefetch" />{" "}
        Prefetch the PRs of the review list in the background (new or changed ones; pauses when GitHub's quota runs low)
      </label>
      <div className="row">
        <button className="btn small" disabled={busy} onClick={async () => { setBusy(true); try { setInfo(await post<CacheInfo>("/github/cache/clear")); setNote("Cleared."); } catch (e) { setErr((e as ApiError).message); } finally { setBusy(false); } }} data-testid="cache-clear">Clear cache</button>
        <button className="icon-btn small" onClick={() => void load()} title="Refresh">↻</button>
      </div>
      {note && <div className="small" data-testid="cache-note">{note}</div>}
      {err && <div className="error-text small" role="alert" data-testid="cache-error">{err}</div>}
      {pick && <FolderPicker title="Cache folder" hint="Where PR Review keeps its local copies (private: mode 0700)." confirmLabel="Use this folder"
        onClose={() => setPick(false)} onChoose={(p) => { setPick(false); setDir(p); }} />}
    </div>
  );
}
