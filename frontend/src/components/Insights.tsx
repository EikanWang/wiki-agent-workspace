import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, del, get, post, put } from "../api";
import MarkdownView from "./MarkdownView";
import ModelChoice, { ModelChoiceValue } from "./ModelChoice";
import { utc8 } from "../time";
import { confirmAction } from "./Confirm";

/** Repo Insights: what happens in GitHub repositories — summaries of issues and pull requests, commits, RFCs and XPU
 * highlights — over a day, week, month, quarter, year or any dates. */

export interface InsightsRepo {
  repo: string;
  sources: { issues: boolean; commits: boolean; rfcs: boolean };
  highlights: boolean;
  schedule: { enabled: boolean; time: string };
  rfc: { repos: string[]; labels: string[]; title_patterns: string[] };
}
interface TaskModel { mode: "api" | "agent"; provider: string | null; model: string | null; variant: string | null }
interface Settings {
  data_dir: string; timezone_offset: number; repos: InsightsRepo[]; llm: Record<string, TaskModel>;
  highlight: { threshold: number; number_of_ccer: number }; max_chars: number; concurrency: number;
  presets: { id: string; name: string; text: string }[];
  endpoint: { base_url: string; model: string; has_key: boolean };
}
interface FileRow { path: string; name: string; since: string; until: string; daily: boolean; partial: boolean; size: number; mtime: number }
interface ReportMeta { id: string; repo: string; since: string; until: string; label: string; kind?: string; status: string; step?: string; created_at: string;
  sources: Record<string, boolean>; focus: string; preset: string | null; error?: string; facts?: string[]; log?: string[]; text?: string | null }
interface Job { id: string; kind: string; status: string; step: string; error: string | null; log: string[] }
interface Overview {
  repo: string; config: InsightsRepo; daily: FileRow[]; daily_count: number; first_day: string | null; last_day: string | null;
  longer: FileRow[]; highlight_files: FileRow[]; reports: ReportMeta[]; jobs: Job[];
}
interface Highlight { title: string; url: string; xpu_relevance_score: number; xpu_relevance_justification: string; body: string; submitter: string; state: string; vendor_involvement?: string[] }

const TASK_LABEL: Record<string, string> = { daily: "Daily summaries", rollup: "Weekly roll-ups", commits: "Commit summaries", report: "Reports", highlight: "XPU highlight scoring" };
const PERIODS = ["day", "week", "month", "quarter", "year", "custom"] as const;
const PERIOD_LABEL: Record<string, string> = { day: "Day", week: "Week", month: "Month", quarter: "Quarter", year: "Year", custom: "Dates" };
const KINDS = [["daily", "Daily"], ["weekly", "Weekly"], ["monthly", "Monthly"], ["quarterly", "Quarterly"], ["yearly", "Yearly"], ["dates", "Dates"]] as const;
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const iso = (d: Date) => d.toISOString().slice(0, 10);
const fmt = (d: Date) => `${MONTHS[d.getUTCMonth()].slice(0, 3)} ${d.getUTCDate()}`;
/** The calendar weeks (Mon–Sun, ISO numbering) of a year that have started: [monday, label], newest first. */
function weeksOf(year: number): [string, string][] {
  const jan4 = new Date(Date.UTC(year, 0, 4));
  let mon = new Date(jan4.getTime() - ((jan4.getUTCDay() + 6) % 7) * 86400_000);
  const out: [string, string][] = [];
  const now = Date.now();
  for (let n = 1; mon.getTime() <= now && n <= 53; n++) {
    const sun = new Date(mon.getTime() + 6 * 86400_000);
    if (n === 53 && new Date(Date.UTC(year, 11, 28)).getTime() < mon.getTime()) break;
    out.push([iso(mon), `Week ${n} · ${fmt(mon)} – ${fmt(sun)}`]);
    mon = new Date(mon.getTime() + 7 * 86400_000);
  }
  return out.reverse();
}
const todayIso = () => new Date().toISOString().slice(0, 10);
const daysAgo = (n: number) => new Date(Date.now() - n * 86400_000).toISOString().slice(0, 10);

let settingsCache: Settings | null = null;
export async function loadInsightsSettings(): Promise<Settings> {
  settingsCache = await get<Settings>("/insights/settings");
  return settingsCache;
}

/** Sidebar: the repositories, and the settings. */
export function InsightsPanel({ active, onOpen }: { active: string | null; onOpen: (repo: string | null) => void }) {
  const [s, setS] = useState<Settings | null>(settingsCache);
  const [add, setAdd] = useState("");
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { void loadInsightsSettings().then(setS).catch((e) => setErr((e as ApiError).message)); }, []);
  const addRepo = async () => {
    const r = add.trim().replace(/^https:\/\/github\.com\//, "").replace(/\.git$|\/$/g, "");
    if (!s || !r) return;
    try {
      const next = await put<Settings>("/insights/settings", { repos: [...s.repos, { repo: r }] });
      settingsCache = next;
      setS(next);
      setAdd("");
      onOpen(r);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  };
  return (
    <div className="insights-panel" data-testid="insights-panel">
      <div className="local-head"><b>Repo Insights</b><span className="spacer" />
        <button className={`icon-btn small ${active === null ? "active" : ""}`} onClick={() => onOpen(null)} title="Settings" data-testid="insights-settings-open">⚙</button>
      </div>
      <div className="insights-repos">
        {s?.repos.map((r) => (
          <button key={r.repo} className={`list-item ${active === r.repo ? "selected" : ""}`} onClick={() => onOpen(r.repo)} data-testid="insights-repo" data-repo={r.repo}>
            <span className="insights-repo-name">{r.repo}</span>
            <span className="muted small">{r.schedule.enabled ? `⏰ ${r.schedule.time}` : ""}{r.highlights ? " · XPU" : ""}</span>
          </button>
        ))}
      </div>
      <form className="row nowrap pad-x" onSubmit={(e) => { e.preventDefault(); void addRepo(); }}>
        <input value={add} onChange={(e) => setAdd(e.target.value)} placeholder="add: owner/repo" className="mono" data-testid="insights-add" />
        <button className="btn small" disabled={!add.trim()}>Add</button>
      </form>
      {err && <div className="error-text small pad-x">{err}</div>}
    </div>
  );
}

/** Main page: a repository (reports, daily summaries, highlights) or the settings. */
export default function InsightsView({ repo, onOpenChat }: { repo: string | null; onOpenChat: (ws: string, path: string) => void }) {
  if (!repo) return <InsightsSettings />;
  return <RepoPage key={repo} repo={repo} onOpenChat={onOpenChat} />;
}

function RepoPage({ repo, onOpenChat }: { repo: string; onOpenChat: (ws: string, path: string) => void }) {
  const [ov, setOv] = useState<Overview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<"reports" | "daily" | "highlights">(() => (localStorage.getItem("waw.insights.tab") as "reports") || "reports");
  useEffect(() => localStorage.setItem("waw.insights.tab", tab), [tab]);
  const [open, setOpen] = useState<{ kind: "report"; id: string } | { kind: "file"; path: string } | null>(null);
  const load = useCallback(async () => {
    try {
      setOv(await get<Overview>(`/insights/repos/${repo}`));
      setErr(null);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [repo]);
  useEffect(() => { void load(); }, [load]);
  const running = (ov?.reports ?? []).some((r) => r.status === "running") || (ov?.jobs ?? []).some((j) => j.status === "running");
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => void load(), 2000);
    return () => clearInterval(t);
  }, [running, load]);
  if (!ov) return <div className="muted pad">{err ?? "Loading…"}</div>;
  return (
    <div className="insights-view" data-testid="insights-view">
      <div className="insights-head">
        <h2>{repo}</h2>
        <span className="muted small">
          {ov.daily_count ? `daily summaries ${ov.first_day} … ${ov.last_day} (${ov.daily_count})` : "no daily summaries yet"}
          {ov.highlight_files.length ? ` · ${ov.highlight_files.length} highlight files` : ""}
        </span>
        <span className="spacer" />
        <a className="btn small" href={`https://github.com/${repo}`} target="_blank" rel="noreferrer">GitHub</a>
      </div>
      {err && <div className="banner warn small">{err}</div>}
      <NewReport repo={repo} cfg={ov.config} onCreated={(id) => { setTab("reports"); setOpen({ kind: "report", id }); void load(); }} />
      <div className="insights-body">
        <div className="insights-list">
          <span className="mode-switch" role="tablist">
            {(["reports", "daily", "highlights"] as const).map((t) => (
              <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "active" : ""} onClick={() => setTab(t)} data-testid={`insights-tab-${t}`}>
                {t === "reports" ? `Reports (${ov.reports.length})` : t === "daily" ? `Daily (${ov.daily_count})` : "XPU highlights"}
              </button>
            ))}
          </span>
          {tab === "reports" && <ReportList reports={ov.reports} openId={open?.kind === "report" ? open.id : null} onOpen={(id) => setOpen({ kind: "report", id })} />}
          {tab === "daily" && <DailyList ov={ov} openPath={open?.kind === "file" ? open.path : null} onOpen={(p) => setOpen({ kind: "file", path: p })} onRan={load} />}
          {tab === "highlights" && <div className="muted small pad">Pick dates on the right.</div>}
        </div>
        <div className="insights-main">
          {tab === "highlights" ? <Highlights repo={repo} onRan={load} enabled={ov.config.highlights} />
            : open?.kind === "report" ? <ReportViewer repo={repo} id={open.id} onOpenChat={onOpenChat} onDeleted={() => { setOpen(null); void load(); }} />
            : open?.kind === "file" ? <FileViewer path={open.path} />
            : <div className="muted pad">Choose a report or a day.</div>}
        </div>
      </div>
    </div>
  );
}

function ReportList({ reports, openId, onOpen }: { reports: ReportMeta[]; openId: string | null; onOpen: (id: string) => void }) {
  const [folded, setFolded] = useState<Record<string, boolean>>(() => { try { return JSON.parse(localStorage.getItem("waw.insights.folded") || "{}"); } catch { return {}; } });
  useEffect(() => localStorage.setItem("waw.insights.folded", JSON.stringify(folded)), [folded]);
  if (reports.length === 0) return <div className="insights-items"><div className="muted small pad">No report yet: choose a period above.</div></div>;
  return (
    <div className="insights-items">
      {KINDS.map(([kind, title]) => {
        const rows = reports.filter((r) => (r.kind ?? "dates") === kind).sort((x, y) => (y.since + y.created_at).localeCompare(x.since + x.created_at));
        if (!rows.length) return null;
        const isOpen = !folded[kind] || rows.some((r) => r.id === openId && r.status === "running");
        return (
          <div key={kind} className="insights-group" data-testid="insights-group" data-kind={kind}>
            <button className="insights-group-head" onClick={() => setFolded({ ...folded, [kind]: isOpen })} aria-expanded={isOpen}>
              {isOpen ? "▾" : "▸"} {title} <span className="muted">({rows.length})</span>
            </button>
            {isOpen && rows.map((r) => (
              <button key={r.id} className={`list-item ${openId === r.id ? "selected" : ""}`} onClick={() => onOpen(r.id)} data-testid="insights-report" data-status={r.status}>
                <span className="insights-item-main"><b>{r.label}</b>
                  <span className="muted small">{r.status === "running" ? `⏳ ${r.step ?? "working…"}` : r.status === "done" ? `made ${utc8(r.created_at, false)}` : r.status}{r.preset ? ` · ${r.preset}` : ""}</span></span>
              </button>
            ))}
          </div>
        );
      })}
    </div>
  );
}

function NewReport({ repo, cfg, onCreated }: { repo: string; cfg: InsightsRepo; onCreated: (id: string) => void }) {
  const [period, setPeriod] = useState<(typeof PERIODS)[number]>("week");
  const now = new Date();
  const thisYear = now.getUTCFullYear();
  const [year, setYear] = useState(thisYear);
  const [day, setDay] = useState(daysAgo(1));
  const weekList = useMemo(() => weeksOf(year), [year]);
  const [week, setWeek] = useState("");
  const lastMonth = year === thisYear ? now.getUTCMonth() : 11;
  const [month, setMonth] = useState(now.getUTCMonth());
  const lastQ = year === thisYear ? Math.floor(now.getUTCMonth() / 3) + 1 : 4;
  const [quarter, setQuarter] = useState(Math.floor(now.getUTCMonth() / 3) + 1);
  const years = Array.from({ length: 4 }, (_, i) => thisYear - i);
  const [since, setSince] = useState(daysAgo(30));
  const [until, setUntil] = useState(todayIso());
  const [src, setSrc] = useState({ issues: cfg.sources.issues, commits: cfg.sources.commits, rfcs: cfg.sources.rfcs, highlights: cfg.highlights });
  const [preset, setPreset] = useState("");
  const [focus, setFocus] = useState("");
  const [presets, setPresets] = useState(settingsCache?.presets ?? []);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { void loadInsightsSettings().then((s) => setPresets(s.presets)).catch(() => undefined); }, []);
  const create = async () => {
    setBusy(true);
    setErr(null);
    try {
      const value = period === "day" ? day : period === "week" ? (week || weekList[0]?.[0]) : period === "month" ? `${year}-${String(Math.min(month, lastMonth) + 1).padStart(2, "0")}`
        : period === "quarter" ? `${year}-Q${Math.min(quarter, lastQ)}` : String(year);
      const body = period === "custom" ? { repo, period, since, until } : { repo, period, value };
      const r = await post<ReportMeta>("/insights/reports", { ...body, sources: src, focus, preset: preset || undefined });
      onCreated(r.id);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="insights-new" data-testid="insights-new">
      <span className="mode-switch" role="tablist">
        {PERIODS.map((p) => <button key={p} role="tab" aria-selected={period === p} className={period === p ? "active" : ""} onClick={() => setPeriod(p)} data-testid={`insights-period-${p}`}>{PERIOD_LABEL[p]}</button>)}
      </span>
      {period === "custom" && (
        <span className="row nowrap small">from <input type="date" value={since} max={todayIso()} onChange={(e) => setSince(e.target.value)} data-testid="insights-since" /> to <input type="date" value={until} max={todayIso()} onChange={(e) => setUntil(e.target.value)} data-testid="insights-until" /></span>
      )}
      {period === "day" && <input type="date" value={day} max={todayIso()} onChange={(e) => setDay(e.target.value)} data-testid="insights-day-pick" />}
      {(period === "week" || period === "month" || period === "quarter" || period === "year") && (
        <select value={year} onChange={(e) => { setYear(Number(e.target.value)); setWeek(""); }} title="Year" data-testid="insights-year">
          {years.map((y) => <option key={y} value={y}>{y}</option>)}
        </select>
      )}
      {period === "week" && (
        <select value={week || weekList[0]?.[0] || ""} onChange={(e) => setWeek(e.target.value)} data-testid="insights-week">
          {weekList.map(([m, l]) => <option key={m} value={m}>{l}</option>)}
        </select>
      )}
      {period === "month" && (
        <select value={Math.min(month, lastMonth)} onChange={(e) => setMonth(Number(e.target.value))} data-testid="insights-month">
          {MONTHS.slice(0, lastMonth + 1).map((m, i) => <option key={m} value={i}>{m}</option>)}
        </select>
      )}
      {period === "quarter" && (
        <select value={Math.min(quarter, lastQ)} onChange={(e) => setQuarter(Number(e.target.value))} data-testid="insights-quarter">
          {[1, 2, 3, 4].slice(0, lastQ).map((q) => <option key={q} value={q}>Q{q} ({MONTHS[3 * q - 3].slice(0, 3)} – {MONTHS[3 * q - 1].slice(0, 3)})</option>)}
        </select>
      )}
      <span className="row nowrap small insights-sources">
        {(["issues", "commits", "rfcs", "highlights"] as const).map((k) => (
          <label key={k}><input type="checkbox" checked={src[k]} onChange={(e) => setSrc({ ...src, [k]: e.target.checked })} data-testid={`insights-src-${k}`} />
            {" "}{k === "issues" ? "Issues & PRs" : k === "commits" ? "Commits" : k === "rfcs" ? "RFCs" : "XPU highlights"}</label>
        ))}
      </span>
      <select value={preset} onChange={(e) => setPreset(e.target.value)} title="Focus preset" data-testid="insights-preset">
        <option value="">no preset</option>
        {presets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select>
      <input className="insights-focus" value={focus} onChange={(e) => setFocus(e.target.value)} placeholder="focus (optional), e.g. inductor, distributed" data-testid="insights-focus" />
      <button className="btn small primary" disabled={busy || (!src.issues && !src.commits && !src.rfcs && !src.highlights)} onClick={() => void create()} data-testid="insights-create">{busy ? "Starting…" : "Summarize"}</button>
      {err && <span className="error-text small" role="alert">{err}</span>}
    </div>
  );
}

function ReportViewer({ repo, id, onOpenChat, onDeleted }: { repo: string; id: string; onOpenChat: (ws: string, path: string) => void; onDeleted: () => void }) {
  const [r, setR] = useState<ReportMeta | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      setR(await get<ReportMeta>(`/insights/reports/${repo}/${id}`));
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [repo, id]);
  useEffect(() => { setR(null); void load(); }, [load]);
  useEffect(() => {
    if (r?.status !== "running") return;
    const t = setInterval(() => void load(), 1500);
    return () => clearInterval(t);
  }, [r?.status, load]);
  if (!r) return <div className="muted pad">{err ?? "Loading…"}</div>;
  const text = r.text ? r.text.split("\n").slice(1).join("\n") : "";
  return (
    <div className="insights-report" data-testid="insights-report-view" data-status={r.status}>
      <div className="row nowrap">
        <b>{r.label}</b><span className="muted small">{r.since} – {r.until} · {Object.entries(r.sources).filter(([, v]) => v).map(([k]) => k).join(", ")}</span>
        <span className="spacer" />
        {r.status === "done" && (
          <button className="btn small" disabled={busy} onClick={async () => { setBusy(true); try { const c = await post<{ workspace_id: string; path: string }>(`/insights/reports/${repo}/${id}/chat`, {}); onOpenChat(c.workspace_id, c.path); } catch (e) { setErr((e as ApiError).message); } finally { setBusy(false); } }}
            title="Ask the agent about this report in a chat" data-testid="insights-chat">Continue in chat</button>
        )}
        <button className="icon-btn small" onClick={async () => { if (await confirmAction({ title: "Delete this report?", message: "Its daily summaries and highlights stay.", confirmLabel: "Delete" })) { await del(`/insights/reports/${repo}/${id}`); onDeleted(); } }} title="Delete" data-testid="insights-report-delete">🗑</button>
      </div>
      {r.status === "running" && (
        <div className="insights-progress" data-testid="insights-progress">
          <div>⏳ {r.step}</div>
          <pre className="muted small">{(r.log ?? []).slice(-8).join("\n")}</pre>
        </div>
      )}
      {r.status === "failed" && <div className="banner warn small" role="alert">Failed: {r.error}</div>}
      {r.status === "interrupted" && <div className="banner warn small">Interrupted (the server restarted): make it again.</div>}
      {r.facts && <details className="small muted"><summary>Material</summary><ul>{r.facts.map((f) => <li key={f}>{f}</li>)}</ul></details>}
      {text && <MarkdownView text={text} className="insights-md" />}
      {err && <div className="error-text small">{err}</div>}
    </div>
  );
}

function FileViewer({ path }: { path: string }) {
  const [t, setT] = useState<string | null>(null);
  useEffect(() => { setT(null); void get<{ text: string }>(`/insights/file?path=${encodeURIComponent(path)}`).then((r) => setT(r.text)).catch((e) => setT(`(${(e as ApiError).message})`)); }, [path]);
  if (t === null) return <div className="muted pad">Loading…</div>;
  const [head, ...rest] = t.split("\n");
  return <div className="insights-report" data-testid="insights-file-view"><div className="muted small">{head}</div><MarkdownView text={rest.join("\n")} className="insights-md" /></div>;
}

function DailyList({ ov, openPath, onOpen, onRan }: { ov: Overview; openPath: string | null; onOpen: (p: string) => void; onRan: () => void }) {
  const [since, setSince] = useState(daysAgo(1));
  const [until, setUntil] = useState(daysAgo(1));
  const [err, setErr] = useState<string | null>(null);
  const job = ov.jobs.filter((j) => j.kind === "daily").slice(-1)[0];
  return (
    <div className="insights-items">
      <div className="row nowrap small pad-x">
        <input type="date" value={since} onChange={(e) => setSince(e.target.value)} /> – <input type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
        <button className="btn small" onClick={async () => { setErr(null); try { await post("/insights/run", { repo: ov.repo, kind: "daily", since, until }); onRan(); } catch (e) { setErr((e as ApiError).message); } }}
          title="Make the daily summaries of these days now (days that have one are kept)" data-testid="insights-run-daily">Make</button>
      </div>
      {job && job.status === "running" && <div className="muted small pad-x" data-testid="insights-daily-job">⏳ {job.step}</div>}
      {err && <div className="error-text small pad-x">{err}</div>}
      {ov.daily.map((f) => (
        <button key={f.path} className={`list-item ${openPath === f.path ? "selected" : ""}`} onClick={() => onOpen(f.path)} data-testid="insights-day" data-day={f.since}>
          <span className="mono small">{f.since}</span>{f.partial && <span className="chip">so far</span>}<span className="muted small">{Math.ceil(f.size / 1024)} KB</span>
        </button>
      ))}
    </div>
  );
}

function Highlights({ repo, onRan, enabled }: { repo: string; onRan: () => void; enabled: boolean }) {
  const [since, setSince] = useState(daysAgo(7));
  const [until, setUntil] = useState(todayIso());
  const [min, setMin] = useState(70);
  const [items, setItems] = useState<Highlight[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setItems((await get<{ items: Highlight[] }>(`/insights/repos/${repo}/highlights?since=${since}&until=${until}`)).items);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [repo, since, until]);
  useEffect(() => { void load(); }, [load]);
  const shown = useMemo(() => (items ?? []).filter((x) => x.xpu_relevance_score >= min || x.xpu_relevance_score < 0), [items, min]);
  return (
    <div className="insights-report" data-testid="insights-highlights">
      <div className="row nowrap small">
        <input type="date" value={since} onChange={(e) => setSince(e.target.value)} /> – <input type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
        score ≥ <input type="number" min={0} max={100} value={min} onChange={(e) => setMin(Number(e.target.value) || 0)} className="insights-num" data-testid="insights-hl-min" />
        <span className="muted">{shown.length} of {items?.length ?? 0}</span>
        <span className="spacer" />
        <button className="btn small" onClick={async () => { setErr(null); try { await post("/insights/run", { repo, kind: "highlights", since, until }); onRan(); } catch (e) { setErr((e as ApiError).message); } }}
          title={enabled ? "Score every issue and PR active in these days now" : "XPU highlights are off for this repository (⚙); you can still score now"} data-testid="insights-run-hl">Score now</button>
      </div>
      {err && <div className="error-text small">{err}</div>}
      {shown.map((x) => (
        <div key={x.url} className="insights-hl" data-testid="insights-hl">
          <span className={`insights-score ${x.xpu_relevance_score >= 90 ? "crit" : x.xpu_relevance_score >= 75 ? "high" : ""}`}>{x.xpu_relevance_score < 0 ? "?" : x.xpu_relevance_score}</span>
          <div className="insights-hl-main">
            <a href={x.url} target="_blank" rel="noreferrer"><b>{x.title}</b></a>
            <div className="small">{x.body}</div>
            <details className="small muted"><summary>why</summary>{x.xpu_relevance_justification}</details>
            <div className="muted small">{x.submitter} · {x.state}{x.vendor_involvement?.length ? ` · ${x.vendor_involvement.join(", ")}` : ""}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

function InsightsSettings() {
  const [s, setS] = useState<Settings | null>(null);
  const [key, setKey] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { void loadInsightsSettings().then(setS).catch((e) => setErr((e as ApiError).message)); }, []);
  // saves run one after another, and each takes from the answer only what it saved: a quick second change (or an
  // unsaved edit elsewhere on the page) is not overwritten by an earlier answer
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  if (!s) return <div className="muted pad">{err ?? "Loading…"}</div>;
  const save = (patch: Record<string, unknown>) => {
    setErr(null);
    setNote(null);
    const run = queue.current.then(async () => {
      try {
        const next = await put<Settings>("/insights/settings", patch);
        settingsCache = next;
        setS((prev) => {
          if (!prev) return next;
          const out: Settings = { ...prev, endpoint: next.endpoint };
          for (const k of Object.keys(patch)) if (k !== "endpoint" && k in next) (out as unknown as Record<string, unknown>)[k] = (next as unknown as Record<string, unknown>)[k];
          return out;
        });
        setNote("Saved.");
      } catch (e) {
        setErr((e as ApiError).message);
        void loadInsightsSettings().then((n) => setS((prev) => (prev ? { ...prev, llm: n.llm } : n))).catch(() => undefined);
      }
    });
    queue.current = run;
    return run;
  };
  /** one task's model: shown at once, and only that task is sent */
  const setTask = (task: string, v: TaskModel) => {
    setS((prev) => (prev ? { ...prev, llm: { ...prev.llm, [task]: v } } : prev));
    void save({ llm: { [task]: v } });
  };
  const setRepo = (i: number, patch: Partial<InsightsRepo>) => setS({ ...s, repos: s.repos.map((r, k) => (k === i ? { ...r, ...patch } : r)) });
  const list = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);
  return (
    <div className="insights-view insights-settings" data-testid="insights-settings">
      <h2>Repo Insights settings</h2>
      <div className="small strong">Folder</div>
      <div className="row nowrap"><input className="mono" value={s.data_dir} onChange={(e) => setS({ ...s, data_dir: e.target.value })} data-testid="insights-dir" />
        <button className="btn small" onClick={() => void save({ data_dir: s.data_dir })}>Save</button></div>
      <div className="muted small">Summaries (summary/), highlights (highlight/), reports, building blocks, local copies of issues and RFCs, and clones.</div>

      <div className="small strong">Model endpoint (OpenAI-compatible)</div>
      <div className="row nowrap">
        <input className="mono" defaultValue={s.endpoint.base_url} onBlur={(e) => void save({ endpoint: { base_url: e.target.value } })} title="Base URL" />
        <input className="mono" defaultValue={s.endpoint.model} onBlur={(e) => void save({ endpoint: { model: e.target.value } })} title="Model" />
        <input type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder={s.endpoint.has_key ? "API key set (type to replace)" : "API key"} data-testid="insights-key" />
        <button className="btn small" disabled={!key} onClick={() => void save({ endpoint: { api_key: key } }).then(() => setKey(""))}>Set key</button>
      </div>
      <div className="small strong">Who does each task</div>
      {Object.entries(s.llm).map(([task, m]) => (
        <div key={task} className="row nowrap small" data-testid="insights-task" data-task={task}>
          <span className="insights-task-name">{TASK_LABEL[task] ?? task}</span>
          <select value={m.mode} onChange={(e) => setTask(task, { ...m, mode: e.target.value as TaskModel["mode"] })} data-testid="insights-task-mode">
            <option value="api">model endpoint ({s.endpoint.model})</option>
            <option value="agent">the app's agent</option>
          </select>
          {m.mode === "agent" && <ModelChoice value={m.provider || m.model ? { provider: m.provider ?? undefined, model: m.model ?? undefined, variant: m.variant ?? undefined } : null}
            onChange={(v: ModelChoiceValue | null) => setTask(task, { mode: "agent", provider: v?.provider ?? null, model: v?.model ?? null, variant: v?.variant ?? null })} />}
        </div>
      ))}
      <div className="row nowrap small">XPU highlight threshold <input type="number" className="insights-num" defaultValue={s.highlight.threshold} onBlur={(e) => void save({ highlight: { ...s.highlight, threshold: Number(e.target.value) } })} />
        · model requests at once <input type="number" className="insights-num" defaultValue={s.concurrency} onBlur={(e) => void save({ concurrency: Number(e.target.value) })} /></div>

      <div className="small strong">Repositories <span className="muted">(daily times in UTC+{s.timezone_offset}; each summarizes the previous UTC day)</span></div>
      {s.repos.map((r, i) => (
        <div key={r.repo} className="insights-repo-cfg" data-testid="insights-repo-cfg" data-repo={r.repo}>
          <div className="row nowrap"><b className="mono">{r.repo}</b><span className="spacer" />
            <button className="icon-btn small" onClick={() => setS({ ...s, repos: s.repos.filter((_, k) => k !== i) })} title="Remove (its files stay)">×</button></div>
          <div className="row nowrap small">
            {(["issues", "commits", "rfcs"] as const).map((k) => <label key={k}><input type="checkbox" checked={r.sources[k]} onChange={(e) => setRepo(i, { sources: { ...r.sources, [k]: e.target.checked } })} /> {k}</label>)}
            <label><input type="checkbox" checked={r.highlights} onChange={(e) => setRepo(i, { highlights: e.target.checked })} /> XPU highlights</label>
            <label title="Make the daily summary (and highlights) every day at this time"><input type="checkbox" checked={r.schedule.enabled} onChange={(e) => setRepo(i, { schedule: { ...r.schedule, enabled: e.target.checked } })} data-testid="insights-schedule" /> every day at</label>
            <input type="time" value={r.schedule.time} onChange={(e) => setRepo(i, { schedule: { ...r.schedule, time: e.target.value } })} />
          </div>
          <div className="row nowrap small">RFCs: repos <input className="mono" defaultValue={r.rfc.repos.join(", ")} onBlur={(e) => setRepo(i, { rfc: { ...r.rfc, repos: list(e.target.value) } })} />
            labels <input defaultValue={r.rfc.labels.join(", ")} onBlur={(e) => setRepo(i, { rfc: { ...r.rfc, labels: list(e.target.value) } })} />
            titles <input defaultValue={r.rfc.title_patterns.join(", ")} onBlur={(e) => setRepo(i, { rfc: { ...r.rfc, title_patterns: list(e.target.value) } })} /></div>
        </div>
      ))}
      <button className="btn small primary" onClick={() => void save({ repos: s.repos })} data-testid="insights-repos-save">Save repositories</button>

      <div className="small strong">Focus presets</div>
      {s.presets.map((p, i) => (
        <div key={i} className="insights-preset">
          <input value={p.name} onChange={(e) => setS({ ...s, presets: s.presets.map((x, k) => (k === i ? { ...x, name: e.target.value } : x)) })} />
          <textarea rows={3} value={p.text} onChange={(e) => setS({ ...s, presets: s.presets.map((x, k) => (k === i ? { ...x, text: e.target.value } : x)) })} />
          <button className="icon-btn small" onClick={() => setS({ ...s, presets: s.presets.filter((_, k) => k !== i) })}>×</button>
        </div>
      ))}
      <div className="row"><button className="link-btn small" onClick={() => setS({ ...s, presets: [...s.presets, { id: `p${Date.now()}`, name: "New preset", text: "" }] })}>+ preset</button>
        <button className="btn small" onClick={() => void save({ presets: s.presets })} data-testid="insights-presets-save">Save presets</button></div>
      {note && <div className="small" data-testid="insights-note">{note}</div>}
      {err && <div className="error-text small" role="alert">{err}</div>}
    </div>
  );
}
