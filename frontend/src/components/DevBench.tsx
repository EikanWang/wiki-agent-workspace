import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, BenchCompare, BenchDef, BenchRun, BenchStat, DevWorkspace, del, get, post, put, subscribeGlobal } from "../api";
import { confirmAction } from "./Confirm";

const NEW_TOML = `[[bench]]
name = "my-bench"
command = "python bench.py --json"
warmup = 1
repeat = 5
timeout = 1800
metrics = [
  { name = "latency_ms", json = "$.latency_ms", better = "lower" },
]
exclusive = true
threshold = 5
`;

export function fmt(v: number | undefined | null): string {
  if (v === undefined || v === null || Number.isNaN(v)) return "—";
  const a = Math.abs(v);
  return a !== 0 && (a < 0.001 || a >= 1e6) ? v.toExponential(3) : String(Number(v.toPrecision(5)));
}

function short(c: string | null): string {
  return c ? c.slice(0, 10) : "?";
}

function when(iso: string): string {
  return new Date(iso).toLocaleString();
}

/** A small line of a metric's medians, oldest first. */
export function Sparkline({ values, better }: { values: number[]; better?: string }) {
  if (values.length < 2) return null;
  const w = 90, h = 22;
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = hi - lo || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * (w - 4) + 2},${h - 2 - ((v - lo) / span) * (h - 4)}`).join(" ");
  return (
    <svg className="bench-spark" width={w} height={h} aria-label={`trend (${better ?? ""} is better)`} data-testid="bench-spark">
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

function trend(runs: BenchRun[], name: string, metric: string): number[] {
  return runs.filter((r) => r.name === name && r.status === "passed" && r.summary[metric]?.median !== undefined)
    .slice().reverse().map((r) => r.summary[metric].median as number);
}

function mainMetrics(r: BenchRun): string[] {
  const names = Object.keys(r.summary);
  const own = names.filter((n) => n !== "wall_s");
  return (own.length ? own : names).slice(0, 3);
}

function statusChip(r: BenchRun) {
  return <span className={`chip status ${r.status === "passed" ? "" : r.status}`} data-testid="bench-status">{r.status}</span>;
}

/** Runs of benchmarks as a table, newest first, with a trend per metric. */
export function BenchHistory({ runs, selected, onSelect }: { runs: BenchRun[]; selected?: string | null; onSelect?: (r: BenchRun) => void }) {
  if (runs.length === 0) return <div className="muted small dev-hint">No benchmark runs yet.</div>;
  return (
    <table className="bench-table" data-testid="bench-history">
      <thead><tr><th>Benchmark</th><th>When</th><th>What</th><th>Machine</th><th>Status</th><th>Results (median)</th></tr></thead>
      <tbody>
        {runs.map((r) => (
          <tr key={r.id} className={r.id === selected ? "selected" : ""} onClick={() => onSelect?.(r)} data-testid="bench-run" data-status={r.status}>
            <td>{r.name}{r.kind === "ab" && <span className="chip small">{r.variant === "a" ? "A · baseline" : "B · candidate"}</span>}</td>
            <td className="small">{when(r.started_at)}</td>
            <td className="mono small">{r.label ?? r.branch ?? ""} {short(r.commit)}{r.dirty ? " +changes" : ""}</td>
            <td className="small">{r.machine === "local" ? "this server" : r.machine}</td>
            <td>{statusChip(r)}</td>
            <td className="small">
              {mainMetrics(r).map((m) => (
                <span key={m} className="bench-metric" title={`${m}: ${r.summary[m].better} is better`}>
                  {m} <b>{fmt(r.summary[m].median)}</b>{r.summary[m].unit ? ` ${r.summary[m].unit}` : ""}
                  <Sparkline values={trend(runs, r.name, m)} better={r.summary[m].better} />
                </span>
              ))}
              {r.error && <div className="warn small">{r.error}</div>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function stat(s: BenchStat | null | undefined): string {
  return s && s.n ? `${fmt(s.median)} ± ${fmt(s.stdev)}` : "—";
}

/** A run against its baseline: change per metric, regressions, warnings; output; explain. */
function RunDetail({ run, runs, onAsked, onChanged }: { run: BenchRun; runs: BenchRun[]; onAsked: () => void; onChanged: () => void }) {
  const [baseline, setBaseline] = useState<string>("");
  const [cmp, setCmp] = useState<BenchCompare | null>(null);
  const [full, setFull] = useState<BenchRun | null>(null);
  const [log, setLog] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { setBaseline(""); setLog(null); }, [run.id]);
  useEffect(() => {
    let alive = true;
    setErr(null);
    void get<BenchRun>(`/bench/runs/${run.id}`).then((r) => alive && setFull(r)).catch(() => undefined);
    if (run.status === "running") { setCmp(null); return () => { alive = false; }; }
    void get<BenchCompare>(`/bench/compare?b=${run.id}${baseline ? `&a=${baseline}` : ""}`)
      .then((c) => { if (alive) { setCmp(c); } }).catch((e) => alive && setErr((e as ApiError).message));
    return () => { alive = false; };
  }, [run.id, run.status, baseline]);
  const others = runs.filter((r) => r.id !== run.id && r.name === run.name && r.status === "passed");
  const showLog = async () => {
    if (!run.task_run_id) return;
    try {
      const r = await get<{ text: string }>(`/dev/tasks/${run.task_run_id}/log?offset=0`);
      setLog(r.text);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const explain = async () => {
    try {
      await post(`/bench/runs/${run.id}/explain`, { baseline: cmp?.a?.id ?? null });
      onAsked();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const remove = async () => {
    if (!(await confirmAction({ title: "Delete this run?", message: run.ab_id ? "Both sides of the A/B run go." : "Its results go.", confirmLabel: "Delete", danger: true }))) return;
    try { await del(`/bench/runs/${run.id}`); onChanged(); } catch (e) { setErr((e as ApiError).message); }
  };
  return (
    <div className="bench-detail" data-testid="bench-detail">
      <div className="row nowrap">
        <b>{run.name}</b> {statusChip(run)}
        <span className="muted small mono">{short(run.commit)}{run.dirty ? " +changes" : ""} · {run.machine === "local" ? "this server" : run.machine}</span>
        <span className="spacer" />
        {run.status === "running" && <button className="btn small" onClick={() => void post(`/bench/runs/${run.id}/cancel`).then(onChanged).catch((e) => setErr((e as ApiError).message))} data-testid="bench-cancel">Stop</button>}
        {run.task_run_id && <button className="btn small" onClick={() => void showLog()} data-testid="bench-show-log">Output</button>}
        {run.status !== "running" && <button className="btn small danger" onClick={() => void remove()}>Delete</button>}
      </div>
      {run.error && <div className="warn small">{run.error}</div>}
      {err && <div className="error-text small">{err}</div>}
      {run.status !== "running" && (
        <div className="row nowrap small">
          Compared with
          <select value={baseline} onChange={(e) => setBaseline(e.target.value)} data-testid="bench-baseline">
            <option value="">{cmp?.a && !baseline ? `${cmp.a.label ?? cmp.a.branch ?? ""} ${short(cmp.a.commit)} (default)` : "the default baseline"}</option>
            {others.map((o) => <option key={o.id} value={o.id}>{when(o.started_at)} · {o.label ?? o.branch ?? ""} {short(o.commit)}</option>)}
          </select>
        </div>
      )}
      {cmp && !cmp.a && <div className="muted small">No baseline yet: run it on the base branch, or run an A/B.</div>}
      {cmp?.warnings.map((w) => <div key={w} className="warn small" data-testid="bench-warning">{w}</div>)}
      {cmp?.a && (
        <table className="bench-table" data-testid="bench-compare">
          <thead><tr><th>Metric</th><th>Baseline</th><th>This run</th><th>Change</th><th /></tr></thead>
          <tbody>
            {cmp.metrics.map((m) => (
              <tr key={m.name} data-testid="bench-compare-row" data-verdict={m.verdict}>
                <td>{m.name}{m.unit ? ` (${m.unit})` : ""} <span className="muted small">{m.better} is better</span></td>
                <td className="mono small">{stat(m.a)}</td>
                <td className="mono small">{stat(m.b)}</td>
                <td className="mono small">{m.change_pct === null ? "—" : `${m.change_pct > 0 ? "+" : ""}${m.change_pct.toFixed(2)}%`}</td>
                <td><span className={`chip bench-${m.verdict}`} title={m.limit_pct !== undefined ? `beyond ±${m.limit_pct.toFixed(1)}% (threshold or 2 standard deviations)` : ""}>{m.verdict}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {cmp?.a && (
        <button className={`btn small ${cmp.regressions ? "primary" : ""}`} onClick={() => void explain()} data-testid="bench-explain"
          title="Send both runs' results and output to the agent: it looks for the cause in the changes between them">
          {cmp.regressions ? "Explain the regression" : "Explain the difference"}
        </button>
      )}
      {full?.samples && full.samples.length > 0 && (
        <details className="small">
          <summary>Each repeat</summary>
          <table className="bench-table">
            <thead><tr><th>#</th>{Object.keys(full.samples[0].values).map((k) => <th key={k}>{k}</th>)}</tr></thead>
            <tbody>{full.samples.map((s) => (
              <tr key={s.i}><td>{s.i + 1}</td>{Object.entries(s.values).map(([k, v]) => <td key={k} className="mono">{fmt(v)}</td>)}</tr>
            ))}</tbody>
          </table>
        </details>
      )}
      {log !== null && <pre className="dev-log-text bench-log" data-testid="bench-log">{log}</pre>}
    </div>
  );
}

function DefEditor({ dev, existing, onClose, onSaved }: { dev: DevWorkspace; existing: BenchDef | null; onClose: () => void; onSaved: () => void }) {
  const [text, setText] = useState(existing?.toml ?? NEW_TOML);
  const [scope, setScope] = useState<"workspace" | "repo">(existing?.source === "repo" ? "repo" : "workspace");
  const [err, setErr] = useState<string | null>(null);
  const save = async () => {
    try {
      if (existing?.id) await put(`/bench/defs/${existing.id}`, { toml: text });
      else await post(`/bench/defs`, { scope, scope_key: scope === "repo" ? dev.github_repo : dev.id, toml: text });
      onSaved();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  return (
    <div className="bench-editor" data-testid="bench-editor">
      <textarea className="mono" rows={14} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} data-testid="bench-toml" />
      <div className="row nowrap small">
        {!existing && (
          <>Keep it for
            <select value={scope} onChange={(e) => setScope(e.target.value as "workspace" | "repo")} data-testid="bench-scope">
              <option value="workspace">this workspace</option>
              {dev.github_repo && <option value="repo">every workspace of {dev.github_repo}</option>}
            </select>
          </>
        )}
        <span className="spacer" />
        <button className="btn small" onClick={onClose}>Cancel</button>
        <button className="btn small primary" onClick={() => void save()} data-testid="bench-save">Save</button>
      </div>
      <div className="muted small">The same <span className="mono">[[bench]]</span> table works in the repository's <span className="mono">.waw/bench.toml</span>, which wins by name.</div>
      {err && <div className="error-text small" data-testid="bench-editor-error">{err}</div>}
    </div>
  );
}

/** Benchmarks of a development workspace: definitions, run / A/B, history and comparison. */
export function BenchTab({ dev, onAsked }: { dev: DevWorkspace; onAsked: () => void }) {
  const [defs, setDefs] = useState<BenchDef[]>([]);
  const [defErrors, setDefErrors] = useState<string[]>([]);
  const [runs, setRuns] = useState<BenchRun[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [edit, setEdit] = useState<BenchDef | "new" | null>(null);
  const [ab, setAb] = useState<string | null>(null);
  const [abBase, setAbBase] = useState("");
  const [abCand, setAbCand] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const loadDefs = useCallback(async () => {
    try {
      const r = await get<{ defs: BenchDef[]; errors: string[] }>(`/dev/workspaces/${dev.id}/bench/defs`);
      setDefs(r.defs);
      setDefErrors(r.errors);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [dev.id]);
  const loadRuns = useCallback(async () => {
    try {
      const r = await get<{ runs: BenchRun[] }>(`/bench/runs?dev_id=${dev.id}`);
      setRuns(r.runs);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [dev.id]);
  useEffect(() => { void loadDefs(); void loadRuns(); }, [loadDefs, loadRuns]);
  useEffect(() => subscribeGlobal((ev) => {
    if (ev.type === "bench_run") void loadRuns();
    if (ev.type === "bench_defs_changed") void loadDefs();
  }), [loadRuns, loadDefs]);
  const cur = useMemo(() => runs.find((r) => r.id === sel) ?? null, [runs, sel]);
  const base = (dev.base_ref ?? "").replace(/^refs\/waw\/base\//, "");
  const start = async (name: string) => {
    setErr(null);
    try {
      const r = await post<BenchRun>(`/dev/workspaces/${dev.id}/bench/run`, { name });
      setSel(r.id);
      await loadRuns();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const startAb = async (name: string) => {
    setErr(null);
    try {
      const r = await post<{ a: BenchRun; b: BenchRun }>(`/dev/workspaces/${dev.id}/bench/ab`, { name, baseline: abBase.trim() || base, candidate: abCand.trim() || null });
      setSel(r.b.id);
      setAb(null);
      await loadRuns();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  const removeDef = async (d: BenchDef) => {
    if (!d.id || !(await confirmAction({ title: `Delete ${d.name}?`, message: "Its runs stay.", confirmLabel: "Delete", danger: true }))) return;
    try { await del(`/bench/defs/${d.id}`); await loadDefs(); } catch (e) { setErr((e as ApiError).message); }
  };
  return (
    <div className="dev-bench" data-testid="dev-bench">
      <div className="dev-bench-side">
        <div className="row nowrap">
          <b>Benchmarks</b><span className="spacer" />
          <button className="btn small" onClick={() => setEdit("new")} data-testid="bench-new">New</button>
        </div>
        {defErrors.map((e) => <div key={e} className="warn small">{e}</div>)}
        {defs.map((d) => (
          <div key={`${d.source}:${d.name}`} className={`bench-def ${d.shadowed ? "muted" : ""}`} data-testid="bench-def" data-name={d.name}>
            <div className="row nowrap">
              <span className="mono">{d.name}</span>
              <span className="chip small" title={d.source === "file" ? ".waw/bench.toml in the repository" : d.source === "repo" ? "kept in the app for the repository" : "kept in the app for this workspace"}>
                {d.source === "file" ? "bench.toml" : d.source}</span>
              {d.shadowed && <span className="small" title="bench.toml has one with the same name">(overridden)</span>}
              <span className="spacer" />
              {!d.shadowed && <button className="btn small primary" onClick={() => void start(d.name)} data-testid="bench-run-btn">Run</button>}
              {!d.shadowed && <button className="btn small" onClick={() => { setAb(ab === d.name ? null : d.name); setAbBase(base); }} data-testid="bench-ab-btn">A/B</button>}
              {d.id && <button className="link-btn small" onClick={() => setEdit(d)}>edit</button>}
              {d.id && <button className="link-btn small" onClick={() => void removeDef(d)}>delete</button>}
              {!d.id && <button className="link-btn small" onClick={() => setEdit({ ...d, id: null, source: "workspace" })} title="Copy it into the app to change it">copy</button>}
            </div>
            <div className="mono small muted">{d.spec.command} · {d.spec.repeat}×{d.spec.exclusive ? " · whole machine" : d.spec.gpus.length ? ` · GPU ${d.spec.gpus.join(", ")}` : ""}</div>
            {ab === d.name && (
              <form className="bench-ab" onSubmit={(e) => { e.preventDefault(); void startAb(d.name); }} data-testid="bench-ab-form">
                <label>Baseline <input className="mono" value={abBase} onChange={(e) => setAbBase(e.target.value)} placeholder="branch, tag or commit" data-testid="bench-ab-base" /></label>
                <label>Candidate <input className="mono" value={abCand} onChange={(e) => setAbCand(e.target.value)} placeholder="this workspace as it is" data-testid="bench-ab-cand" /></label>
                <div className="muted small">Each side runs in its own worktree, alternating repeat by repeat.</div>
                <button className="btn small primary" disabled={!(abBase.trim() || base)} data-testid="bench-ab-start">Start A/B</button>
              </form>
            )}
          </div>
        ))}
        {defs.length === 0 && <div className="muted small dev-hint">No benchmarks: add one here, or a <span className="mono">.waw/bench.toml</span> to the repository.</div>}
        {edit && <DefEditor dev={dev} existing={edit === "new" ? null : edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); void loadDefs(); }} />}
        {err && <div className="error-text small" data-testid="bench-error">{err}</div>}
      </div>
      <div className="dev-bench-main">
        <div className="row nowrap small">
          <span className="spacer" />
          <a className="link-btn" href={`/api/v1/bench/runs/export?dev_id=${dev.id}&format=csv`} download>CSV</a>
          <a className="link-btn" href={`/api/v1/bench/runs/export?dev_id=${dev.id}&format=json`} download>JSON</a>
        </div>
        {cur && <RunDetail run={cur} runs={runs} onAsked={onAsked} onChanged={() => void loadRuns()} />}
        <BenchHistory runs={runs} selected={sel} onSelect={(r) => setSel(r.id)} />
      </div>
    </div>
  );
}

/** The machine page's benchmark history (every workspace's runs there). */
export function MachineBench({ machine }: { machine: string }) {
  const [runs, setRuns] = useState<BenchRun[]>([]);
  const load = useCallback(async () => {
    try {
      setRuns((await get<{ runs: BenchRun[] }>(`/bench/runs?machine=${encodeURIComponent(machine)}&limit=100`)).runs);
    } catch {
      setRuns([]);
    }
  }, [machine]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => subscribeGlobal((ev) => { if (ev.type === "bench_run") void load(); }), [load]);
  if (runs.length === 0) return null;
  return (
    <div className="machine-section" data-testid="machine-bench">
      <h4>Benchmarks <span className="muted small">run here</span>
        <a className="link-btn small" href={`/api/v1/bench/runs/export?machine=${encodeURIComponent(machine)}&format=csv`} download>CSV</a></h4>
      <BenchHistory runs={runs} />
    </div>
  );
}
