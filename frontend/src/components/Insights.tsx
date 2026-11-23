import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgentSession, ApiError, del, get, post, put } from "../api";
import TrendChat from "./TrendChat";
import { DiscussBody, Started, useInsightsDiscussion } from "./InsightsDiscussions";
import SelectionAsk from "./SelectionAsk";
import ContextFolders from "./ContextFolders";
import FolderPicker from "./FolderPicker";
import MarkdownView from "./MarkdownView";
import GithubRate from "./GithubRate";
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
  reports?: { week: boolean; month: boolean; quarter: boolean; year: boolean };
  rfc: { repos: string[]; labels: string[]; title_patterns: string[] };
  new_baseline?: Baseline;
}
export interface Baseline { mode: BaselineMode; date: string | null; label?: string; days?: number }
type BaselineMode = "previous" | "day" | "week" | "month" | "quarter" | "year" | "before" | "after";
const BASELINES: [BaselineMode, string][] = [["previous", "everything before"], ["day", "only the day before"], ["week", "only the week before"], ["month", "only the month before"],
  ["quarter", "only the quarter before"], ["year", "only the year before"], ["before", "everything before a date"], ["after", "from a date until the period"]];

/** What a report's new things (features, terms, concepts …) are compared with. */
function BaselinePick({ value, onChange, testid }: { value: Baseline; onChange: (b: Baseline) => void; testid: string }) {
  const dated = value.mode === "before" || value.mode === "after";
  return (
    <span className="row nowrap small" title="What counts as new: what no daily summary of this baseline has (by default everything before the period: day to day, month to month …)">
      🆕 new vs
      <select value={value.mode} onChange={(e) => { const mode = e.target.value as BaselineMode; onChange({ mode, date: mode === "before" || mode === "after" ? value.date || daysAgo(90) : null }); }} data-testid={`${testid}-mode`}>
        {BASELINES.map(([m, l]) => <option key={m} value={m}>{l}</option>)}
      </select>
      {dated && <input type="date" value={value.date ?? ""} max={todayIso()} onChange={(e) => onChange({ ...value, date: e.target.value })} data-testid={`${testid}-date`} />}
    </span>
  );
}
interface TaskModel { mode: "api" | "agent"; provider: string | null; model: string | null; variant: string | null }
interface Settings {
  data_dir: string; timezone_offset: number; repos: InsightsRepo[]; llm: Record<string, TaskModel>;
  highlight: { threshold: number; number_of_ccer: number }; max_chars: number; agent_max_chars: number; concurrency: number;
  presets: { id: string; name: string; text: string }[];
  endpoint: { base_url: string; model: string; has_key: boolean };
}
interface FileRow { path: string; name: string; since: string; until: string; daily: boolean; partial: boolean; size: number; mtime: number; new?: number; unread?: number }
interface ReportMeta { id: string; repo: string; since: string; until: string; label: string; kind?: string; status: string; step?: string; created_at: string;
  sources: Record<string, boolean>; focus: string; preset: string | null; error?: string; facts?: string[]; log?: string[]; text?: string | null; new?: number; unread?: number; baseline?: Baseline;
  read_new?: string[] }
interface Job { id: string; kind: string; status: string; step: string; error: string | null; log: string[] }
interface Overview {
  repo: string; config: InsightsRepo; daily: FileRow[]; daily_count: number; first_day: string | null; last_day: string | null;
  longer: FileRow[]; highlight_files: FileRow[]; reports: ReportMeta[]; jobs: Job[];
}
interface Highlight { title: string; url: string; xpu_relevance_score: number; xpu_relevance_justification: string; body: string; submitter: string; state: string; vendor_involvement?: string[] }

const TASK_LABEL: Record<string, string> = { daily: "Daily summaries", rollup: "Weekly roll-ups", commits: "Commit summaries", report: "Reports", highlight: "XPU highlight scoring", terms: "What a day is about (baseline of new things)", check: "New-things check (repeat / extension / new)" };
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
const cacheSettings = (next: Settings) => { settingsCache = next; window.dispatchEvent(new CustomEvent("waw-insights-settings")); };
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
  useEffect(() => {
    const sync = () => setS(settingsCache);
    window.addEventListener("waw-insights-settings", sync);
    return () => window.removeEventListener("waw-insights-settings", sync);
  }, []);
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
  const [drag, setDrag] = useState<string | null>(null);
  const [over, setOver] = useState<{ repo: string; after: boolean } | null>(null);
  const reorder = async (order: string[]) => {
    if (!s || order.join("\n") === s.repos.map((r) => r.repo).join("\n")) return;
    const repos = order.map((n) => s.repos.find((r) => r.repo === n)!);
    setS({ ...s, repos });
    try {
      const next = await put<Settings>("/insights/settings", { repos });
      settingsCache = next;
      setS(next);
    } catch (e) {
      setErr((e as ApiError).message);
      setS(s);
    }
  };
  const place = (from: string, to: string, after: boolean) => {
    if (!s || from === to) return;
    const order = s.repos.map((r) => r.repo).filter((n) => n !== from);
    order.splice(order.indexOf(to) + (after ? 1 : 0), 0, from);
    void reorder(order);
  };
  const step = (repo: string, d: number) => {
    const list = s?.repos.map((r) => r.repo) ?? [];
    const other = list[list.indexOf(repo) + d];
    if (other) place(repo, other, d > 0);
  };
  return (
    <div className="insights-panel" data-testid="insights-panel">
      <div className="local-head"><b>Repo Insights</b><span className="spacer" />
        <button className={`icon-btn small ${active === null ? "active" : ""}`} onClick={() => onOpen(null)} title="Settings" data-testid="insights-settings-open">⚙</button>
      </div>
      <div className="insights-repos">
        {s?.repos.map((r) => (
          <button key={r.repo} className={`list-item ${active === r.repo ? "selected" : ""} ${drag === r.repo ? "dragging" : ""} ${over?.repo === r.repo && drag !== r.repo ? (over.after ? "drop-after" : "drop-before") : ""}`}
            onClick={() => onOpen(r.repo)} data-testid="insights-repo" data-repo={r.repo}
            title="Drag to change the order (or Alt+↑ / Alt+↓)" draggable
            onKeyDown={(e) => { if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) { e.preventDefault(); step(r.repo, e.key === "ArrowUp" ? -1 : 1); } }}
            onDragStart={(e) => { setDrag(r.repo); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/x-waw-repo", r.repo); }}
            onDragEnd={() => { setDrag(null); setOver(null); }}
            onDragOver={(e) => {
              if (!drag) return;
              e.preventDefault();
              e.dataTransfer.dropEffect = "move";
              const b = e.currentTarget.getBoundingClientRect();
              const after = e.clientY > b.top + b.height / 2;
              if (over?.repo !== r.repo || over.after !== after) setOver({ repo: r.repo, after });
            }}
            onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOver((o) => (o?.repo === r.repo ? null : o)); }}
            onDrop={(e) => {
              e.preventDefault();
              const from = e.dataTransfer.getData("text/x-waw-repo") || drag;
              const b = e.currentTarget.getBoundingClientRect();
              if (from) place(from, r.repo, e.clientY > b.top + b.height / 2);
              setDrag(null); setOver(null);
            }}>
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
type OpenPr = (repo: string, number: number, title?: string) => void;

export default function InsightsView({ repo, onOpenPrReview, sessions = [] }: { repo: string | null; onOpenPrReview?: OpenPr; sessions?: AgentSession[] }) {
  if (!repo) return <InsightsSettings />;
  return <RepoPage key={repo} repo={repo} onOpenPrReview={onOpenPrReview} sessions={sessions} />;
}

type Tab = "reports" | "daily" | "highlights" | "chat";
type Opened = { kind: "report"; id: string } | { kind: "reports"; ids: string[] } | { kind: "file"; path: string };

function RepoPage({ repo, onOpenPrReview, sessions }: { repo: string; onOpenPrReview?: OpenPr; sessions: AgentSession[] }) {
  const [ov, setOv] = useState<Overview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>(() => (localStorage.getItem("waw.insights.tab") as Tab) || "reports");
  useEffect(() => localStorage.setItem("waw.insights.tab", tab), [tab]);
  const [open, setOpen] = useState<Opened | null>(null);
  const [refs, setRefs] = useState(false);
  // the discussions of what is open (a finished report, several of them, a daily summary): in a panel on its right
  const done = (id: string) => ov?.reports.find((r) => r.id === id)?.status === "done";
  const discKey = tab === "chat" || tab === "highlights" || !open ? null
    : open.kind === "report" ? (done(open.id) ? `report:${open.id}` : null)
    : open.kind === "reports" ? (open.ids.every(done) ? `reports:${[...open.ids].sort().join(",")}` : null)
    : `file:${open.path}`;
  const disc = useInsightsDiscussion(repo, discKey, sessions, (body) => (
    open?.kind === "report" ? post<Started>(`/insights/reports/${repo}/${open.id}/chat`, body)
      : open?.kind === "reports" ? post<Started>(`/insights/reports/${repo}/chat`, { ...body, ids: open.ids })
      : post<Started>("/insights/file/chat", { ...body, path: open?.kind === "file" ? open.path : "" })));
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
  // Reports · Daily · XPU highlights: across the whole width of the list
  const tabs = (
    <span className="mode-switch insights-tabs" role="tablist">
      {(["reports", "daily", "highlights", "chat"] as const).map((t) => (
        <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "active" : ""} onClick={() => setTab(t)} data-testid={`insights-tab-${t}`}>
          {t === "reports" ? `Reports (${ov.reports.length})` : t === "daily" ? `Daily (${ov.daily_count})` : t === "highlights" ? "XPU highlights" : "Chat"}
        </button>
      ))}
    </span>
  );
  return (
    <div className="insights-view" data-testid="insights-view">
      <div className="insights-head">
        <h2>{repo}</h2>
        <span className="muted small">
          {ov.daily_count ? `daily summaries ${ov.first_day} … ${ov.last_day} (${ov.daily_count})` : "no daily summaries yet"}
          {ov.highlight_files.length ? ` · ${ov.highlight_files.length} highlight files` : ""}
        </span>
        <span className="spacer" />
        <GithubRate />
        <button className="btn small" onClick={() => setRefs(true)} title={`Extra folders on the server the agent reads in every discussion about ${repo}'s reports and in its trend chats`} data-testid="insights-refs">Reference folders</button>
        <a className="btn small" href={`https://github.com/${repo}`} target="_blank" rel="noreferrer">GitHub</a>
      </div>
      {refs && <ContextFolders scope={`insights:${repo}`} title={`For every discussion about ${repo}'s reports, and its trend chats (a discussion can add its own).`} onClose={() => setRefs(false)} />}
      {err && <div className="banner warn small">{err}</div>}
      {tab !== "chat" && <NewReport repo={repo} cfg={ov.config} onCreated={(id) => { setTab("reports"); setOpen({ kind: "report", id }); void load(); }} />}
      <div className="insights-body">
        {tab === "chat" ? <TrendChat repo={repo} tabs={tabs} sessions={sessions} /> : tab === "highlights" ? <Highlights repo={repo} tabs={tabs} onRan={load} enabled={ov.config.highlights} onOpenPr={onOpenPrReview} sessions={sessions} /> : (
          <>
            <div className="insights-list">
              {tabs}
              {tab === "reports" && <ReportList repo={repo} reports={ov.reports} openId={open?.kind === "report" ? open.id : null} onOpen={(id) => setOpen({ kind: "report", id })}
                onOpenTogether={(ids) => setOpen({ kind: "reports", ids })}
                onChanged={(gone) => { if ((open?.kind === "report" && gone.includes(open.id)) || (open?.kind === "reports" && open.ids.some((x) => gone.includes(x)))) setOpen(null); void load(); }} />}
              {tab === "daily" && <DailyList ov={ov} openPath={open?.kind === "file" ? open.path : null} onOpen={(p) => setOpen({ kind: "file", path: p })} onRan={load} />}
            </div>
            <div className="insights-main">
              {open?.kind === "report" ? <ReportViewer repo={repo} id={open.id} onDiscuss={disc.ask} onDeleted={() => { setOpen(null); void load(); }} onRead={() => void load()} />
                : open?.kind === "reports" ? <ReportsViewer repo={repo} ids={open.ids} onDiscuss={disc.ask} onClose={() => setOpen(null)} />
                : open?.kind === "file" ? <FileViewer repo={repo} path={open.path} onDiscuss={disc.ask} onRead={() => void load()} />
                : <div className="muted pad">Choose a report or a day.</div>}
            </div>
            {disc.panel}
          </>
        )}
      </div>
    </div>
  );
}

function ReportList({ repo, reports, openId, onOpen, onOpenTogether, onChanged }: { repo: string; reports: ReportMeta[]; openId: string | null; onOpen: (id: string) => void;
  onOpenTogether: (ids: string[]) => void; onChanged: (deleted: string[]) => void }) {
  const [folded, setFolded] = useState<Record<string, boolean>>(() => { try { return JSON.parse(localStorage.getItem("waw.insights.folded") || "{}"); } catch { return {}; } });
  useEffect(() => localStorage.setItem("waw.insights.folded", JSON.stringify(folded)), [folded]);
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ids = new Set(reports.map((r) => r.id));
  const sel = picked.filter((id) => ids.has(id));
  const pick = (id: string, on: boolean) => setPicked((p) => (on ? [...p.filter((x) => x !== id), id] : p.filter((x) => x !== id)));
  if (reports.length === 0) return <div className="insights-items"><div className="muted small pad">No report yet: choose a period above.</div></div>;
  const ready = sel.every((id) => reports.find((r) => r.id === id)?.status === "done");
  const chat = () => {
    if (sel.length === 1) onOpen(sel[0]);
    else onOpenTogether(sel);
    setPicked([]);
  };
  const remove = async () => {
    if (!(await confirmAction({ title: `Delete ${sel.length} report${sel.length === 1 ? "" : "s"}?`, message: "Their daily summaries and highlights stay.", confirmLabel: "Delete" }))) return;
    setBusy(true); setErr(null);
    const gone: string[] = [];
    try {
      for (const id of sel) { await del(`/insights/reports/${repo}/${id}`); gone.push(id); }
    } catch (e) { setErr((e as ApiError).message); } finally {
      setBusy(false);
      setPicked((p) => p.filter((x) => !gone.includes(x)));
      onChanged(gone);
    }
  };
  return (
    <div className="insights-items">
      {sel.length > 0 && (
        <div className="row nowrap small pad-x insights-picked" data-testid="insights-picked">
          <b>{sel.length} selected</b>
          <span className="spacer" />
          <button className="btn small" disabled={busy || !ready} onClick={chat} title={ready ? (sel.length === 1 ? "Open this report and discuss it with the agent" : "Open these reports together and discuss them with the agent (e.g. to compare them)") : "Only finished reports can be discussed"} data-testid="insights-picked-chat">Discuss</button>
          <button className="btn small danger" disabled={busy} onClick={() => void remove()} data-testid="insights-picked-delete">Delete</button>
          <button className="btn small" disabled={busy} onClick={() => setPicked([])} data-testid="insights-picked-clear">Clear</button>
        </div>
      )}
      {err && <div className="error-text small pad-x">{err}</div>}
      {KINDS.map(([kind, title]) => {
        const rows = reports.filter((r) => (r.kind ?? "dates") === kind).sort((x, y) => (y.since + y.created_at).localeCompare(x.since + x.created_at));
        if (!rows.length) return null;
        const isOpen = !folded[kind] || rows.some((r) => r.id === openId && r.status === "running");
        return (
          <div key={kind} className="insights-group" data-testid="insights-group" data-kind={kind}>
            <button className="insights-group-head" onClick={() => setFolded({ ...folded, [kind]: isOpen })} aria-expanded={isOpen}>
              {isOpen ? "▾" : "▸"} {title} <span className="muted">({rows.length})</span>
            </button>
            {isOpen && <ByYear items={rows} group={`r:${kind}`} year={(r) => r.since.slice(0, 4)} open={(r) => r.id === openId} unread={(r) => r.unread ?? r.new ?? 0} render={(r) => (
              <div key={r.id} className={`list-item insights-pick-row ${openId === r.id ? "selected" : ""}`} data-testid="insights-report" data-status={r.status} data-id={r.id}>
                <input type="checkbox" checked={sel.includes(r.id)} onChange={(e) => pick(r.id, e.target.checked)} title="Select (to chat about or delete several reports)" aria-label={`Select ${r.label}`} data-testid="insights-report-pick" />
                <button className="insights-pick-open" onClick={() => onOpen(r.id)} data-testid="insights-report-open">
                  <span className="insights-item-main"><b>{r.label}</b>
                    <span className="muted small">{r.status === "running" ? `⏳ ${r.step ?? "working…"}` : r.status === "done" ? `made ${utc8(r.created_at, false)}` : r.status}{r.preset ? ` · ${r.preset}` : ""}</span></span>
                  <NewCount n={r.new} unread={r.unread} />
                </button>
              </div>
            )} />}
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
  const [baseline, setBaseline] = useState<Baseline>(cfg.new_baseline ?? { mode: "previous", date: null });
  useEffect(() => setBaseline(cfg.new_baseline ?? { mode: "previous", date: null }), [cfg.new_baseline?.mode, cfg.new_baseline?.date]); // eslint-disable-line react-hooks/exhaustive-deps
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
      const r = await post<ReportMeta>("/insights/reports", { ...body, sources: src, focus, preset: preset || undefined, baseline: { mode: baseline.mode, date: baseline.date } });
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
      <BaselinePick value={baseline} onChange={setBaseline} testid="insights-baseline" />
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

function ReportViewer({ repo, id, onDiscuss, onDeleted, onRead }: { repo: string; id: string; onDiscuss: (b: DiscussBody) => Promise<void>; onDeleted: () => void; onRead: () => void }) {
  const [r, setR] = useState<ReportMeta | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const textRef = useRef<HTMLDivElement>(null);
  const [dirs, setDirs] = useState<string[]>([]);
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
        {r.baseline && <span className="chip new-vs" title="What counts as new in this report" data-testid="insights-report-baseline">🆕 new vs {r.baseline.label ?? BASELINES.find(([m]) => m === r.baseline!.mode)?.[1]}{r.baseline.mode === "before" || r.baseline.mode === "after" ? (r.baseline.label ? "" : ` ${r.baseline.date}`) : ""}</span>}
        <span className="spacer" />
        {r.status === "done" && (
          <button className="btn small" disabled={busy} onClick={async () => { setBusy(true); setErr(null); try { await onDiscuss({}); } catch (e) { setErr((e as ApiError).message); } finally { setBusy(false); } }}
            title="Ask the agent what to look at first in this report: a new discussion in the panel on the right (or select a part of it: Discuss this)" data-testid="insights-chat">Ask about all of it</button>
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
      {text && <div ref={textRef} className="insights-report-text"><NewThings repo={repo} readKey={`r:${id}`} text={text} read={r.read_new ?? []} onRead={onRead} /></div>}
      {text && r.status === "done" && (
        <SelectionAsk container={textRef} within=".insights-report-text" label="Discuss this" sendLabel="Start discussion" pictures={false} optional testId="insights-sel"
          hint="A new discussion in the panel on the right, with the report as its material"
          extra={<DiscussFolders repo={repo} dirs={dirs} onChange={setDirs} />}
          onAsk={async (part, question) => { await onDiscuss({ quote: part.quote, question, dirs }); setDirs([]); }} />
      )}
      {err && <div className="error-text small">{err}</div>}
    </div>
  );
}

/** Several reports, one after the other: discussed together (e.g. to compare them). */
function ReportsViewer({ repo, ids, onDiscuss, onClose }: { repo: string; ids: string[]; onDiscuss: (b: DiscussBody) => Promise<void>; onClose: () => void }) {
  const [reps, setReps] = useState<ReportMeta[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const textRef = useRef<HTMLDivElement>(null);
  const [dirs, setDirs] = useState<string[]>([]);
  useEffect(() => {
    setReps(null);
    Promise.all(ids.map((id) => get<ReportMeta>(`/insights/reports/${repo}/${id}`)))
      .then((rs) => setReps(rs.sort((a, b) => (a.since + a.until).localeCompare(b.since + b.until))))
      .catch((e) => setErr((e as ApiError).message));
  }, [repo, ids]);
  if (!reps) return <div className="muted pad">{err ?? "Loading…"}</div>;
  return (
    <div className="insights-report" data-testid="insights-reports-view">
      <div className="row nowrap">
        <b>{reps.length} reports</b><span className="muted small">{reps.map((r) => r.label).join(" · ")}</span>
        <span className="spacer" />
        <button className="btn small" disabled={busy} onClick={async () => { setBusy(true); setErr(null); try { await onDiscuss({}); } catch (e) { setErr((e as ApiError).message); } finally { setBusy(false); } }}
          title="Ask the agent to compare them: a new discussion in the panel on the right (or select a part: Discuss this)" data-testid="insights-reports-chat">Compare them</button>
        <button className="icon-btn small" onClick={onClose} title="Close" aria-label="Close">×</button>
      </div>
      {err && <div className="error-text small">{err}</div>}
      <div ref={textRef} className="insights-report-text">
        {reps.map((r) => (
          <section key={r.id} data-testid="insights-reports-part">
            <h2>{r.label} <span className="muted small">{r.since} – {r.until}</span></h2>
            <MarkdownView text={(r.text ?? "").split("\n").slice(1).join("\n")} className="insights-md" />
          </section>
        ))}
      </div>
      <SelectionAsk container={textRef} within=".insights-report-text" label="Discuss this" sendLabel="Start discussion" pictures={false} optional testId="insights-sel"
        hint="A new discussion in the panel on the right, with these reports as its material"
        extra={<DiscussFolders repo={repo} dirs={dirs} onChange={setDirs} />}
        onAsk={async (part, question) => { await onDiscuss({ quote: part.quote, question, dirs }); setDirs([]); }} />
    </div>
  );
}

/** Reference folders for one new discussion (besides its repository's). */
function DiscussFolders({ repo, dirs, onChange }: { repo: string; dirs: string[]; onChange: (dirs: string[]) => void }) {
  const [browse, setBrowse] = useState(false);
  return (
    <div className="insights-discuss-folders small" data-testid="insights-sel-folders">
      <div className="row nowrap">
        <span className="muted">Reference folders for this discussion</span><span className="spacer" />
        <button className="link-btn small" onClick={() => setBrowse(true)} data-testid="insights-sel-folder-add">+ Add…</button>
      </div>
      {dirs.map((d) => (
        <div key={d} className="row nowrap" data-testid="insights-sel-folder">
          <span className="mono ellipsis" title={d}>{d}</span><span className="spacer" />
          <button className="icon-btn small" onClick={() => onChange(dirs.filter((x) => x !== d))} aria-label={`Remove ${d}`}>×</button>
        </div>
      ))}
      <div className="muted">{dirs.length ? "" : "None. "}{repo}'s reference folders apply too.</div>
      {browse && (
        <FolderPicker title="Add a reference folder" hint="The agent reads this folder for background in this discussion." start={dirs[dirs.length - 1]}
          confirmLabel="Add this folder" onClose={() => setBrowse(false)}
          onChoose={(p) => { setBrowse(false); if (!dirs.includes(p)) onChange([...dirs, p]); }} />
      )}
    </div>
  );
}

/** A daily summary or a highlights file: select a part of it to discuss it with the agent, or chat about all of it. */
function FileViewer({ repo, path, onDiscuss, onRead }: { repo: string; path: string; onDiscuss: (b: DiscussBody) => Promise<void>; onRead: () => void }) {
  const [f, setF] = useState<{ text: string; name?: string; repo?: string | null; read_new?: string[]; failed?: boolean } | null>(null);
  const textRef = useRef<HTMLDivElement>(null);
  const [dirs, setDirs] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { setF(null); void get<{ text: string; name: string; repo: string | null; read_new: string[] }>(`/insights/file?path=${encodeURIComponent(path)}`).then(setF).catch((e) => setF({ text: `(${(e as ApiError).message})`, failed: true })); }, [path]);
  if (f === null) return <div className="muted pad">Loading…</div>;
  const [head, ...rest] = f.text.split("\n");
  const chat = onDiscuss;
  return (
    <div className="insights-report" data-testid="insights-file-view">
      <div className="row nowrap"><span className="muted small">{head}</span><span className="spacer" />
        {!f.failed && <button className="btn small" disabled={busy} onClick={async () => { setBusy(true); setErr(null); try { await chat({}); } catch (e) { setErr((e as ApiError).message); } finally { setBusy(false); } }}
          title="Ask the agent about all of it: a new discussion in the panel on the right (or select a part of it: Discuss this)" data-testid="insights-file-chat">Ask about all of it</button>}
      </div>
      {err && <div className="error-text small">{err}</div>}
      <div ref={textRef} className="insights-report-text">
        {f.repo && f.name ? <NewThings repo={f.repo} readKey={`f:${f.name}`} text={rest.join("\n")} read={f.read_new ?? []} onRead={onRead} />
          : <MarkdownView text={rest.join("\n")} className="insights-md" />}
      </div>
      {!f.failed && (
        <SelectionAsk container={textRef} within=".insights-report-text" label="Discuss this" sendLabel="Start discussion" pictures={false} optional testId="insights-sel"
          hint="A new discussion in the panel on the right, with this as its material"
          extra={<DiscussFolders repo={f.repo ?? repo} dirs={dirs} onChange={setDirs} />}
          onAsk={async (part, question) => { await chat({ quote: part.quote, question, dirs }); setDirs([]); }} />
      )}
    </div>
  );
}

/** A summary or report whose new things can be marked read (click a NEW badge), or all at once; the lists count the unread ones. */
function NewThings({ repo, readKey, text, read: initial, onRead }: { repo: string; readKey: string; text: string; read: string[]; onRead: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  const [read, setRead] = useState<string[]>(initial);
  const [names, setNames] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => setRead(initial), [readKey, initial.join("\n")]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const done = new Set(read);
    const all = new Set<string>();
    el.querySelectorAll<HTMLElement>(".waw-new-badge[data-new-name]").forEach((b) => {
      const n = b.dataset.newName!;
      if (!n) return;
      all.add(n);
      const on = done.has(n);
      b.classList.add("toggle");
      b.classList.toggle("read", on);
      b.textContent = on ? "READ" : "NEW";
      b.title = on ? "Read: click to mark it unread" : "New to the project: click to mark it read";
      b.closest("li, p, td")?.classList.toggle("waw-new-read", on);
    });
    setNames((old) => (old.join("\n") === [...all].join("\n") ? old : [...all]));
  }, [text, read]);
  const mark = async (list: string[], on: boolean) => {
    if (!list.length) return;
    setErr(null);
    try {
      const r = await put<{ read_new: string[] }>("/insights/read", { repo, key: readKey, names: list, read: on });
      setRead(r.read_new);
      onRead();
    } catch (e) { setErr((e as ApiError).message); }
  };
  const onClick = (e: React.MouseEvent) => {
    const b = (e.target as HTMLElement).closest<HTMLElement>(".waw-new-badge[data-new-name]");
    if (!b?.dataset.newName) return;
    e.preventDefault();
    void mark([b.dataset.newName], !read.includes(b.dataset.newName));
  };
  const unread = names.filter((n) => !read.includes(n)).length;
  return (
    <>
      {names.length > 0 && (
        <div className="row nowrap small insights-new-bar" data-testid="insights-new-bar">
          <span className="chip new-count" data-testid="insights-new-unread">🆕 {unread} of {names.length} unread</span>
          <span className="muted">click a NEW badge to mark it read</span>
          <span className="spacer" />
          <button className="btn small" disabled={unread === 0} onClick={() => void mark(names.filter((n) => !read.includes(n)), true)} data-testid="insights-new-all-read">Mark all read</button>
          <button className="btn small" disabled={unread === names.length} onClick={() => void mark(names.filter((n) => read.includes(n)), false)} data-testid="insights-new-all-unread">Mark all unread</button>
        </div>
      )}
      {err && <div className="error-text small">{err}</div>}
      <div ref={box} onClick={onClick}><MarkdownView text={text} className="insights-md" /></div>
    </>
  );
}

/** How many new things (new features, terms, concepts …) a summary or report marks and are not read yet; nothing when none. */
function NewCount({ n, unread, testId = "insights-new-count" }: { n?: number; unread?: number; testId?: string }) {
  const left = unread ?? n;
  if (!left) return null;
  return <span className="chip new-count" title={`${left} unread of ${n ?? left} new thing${n === 1 ? "" : "s"} (features, terms, concepts …) marked in it`} data-testid={testId}>🆕 {left}</span>;
}

const yearFolds = (): Record<string, boolean> => { try { return JSON.parse(localStorage.getItem("waw.insights.years") || "{}"); } catch { return {}; } };

/** Items grouped by year, newest year first, each year foldable (kept per browser); the year holding the open item stays open. */
function ByYear<T>({ items, year, group, render, open, unread }: { items: T[]; year: (x: T) => string; group: string; render: (x: T) => React.ReactNode; open?: (x: T) => boolean; unread?: (x: T) => number }) {
  const [folded, setFolded] = useState<Record<string, boolean>>(yearFolds);
  const years = [...new Set(items.map(year))].sort((a, b) => b.localeCompare(a));
  return (
    <>
      {years.map((y) => {
        const rows = items.filter((x) => year(x) === y);
        const key = `${group}:${y}`;
        const isOpen = !folded[key] || (!!open && rows.some(open));
        const left = unread ? rows.reduce((n, x) => n + unread(x), 0) : 0;
        const toggle = () => { const next = { ...yearFolds(), [key]: isOpen }; localStorage.setItem("waw.insights.years", JSON.stringify(next)); setFolded(next); };
        return (
          <div key={y} className="insights-year" data-testid="insights-year-group" data-year={y}>
            <button className="insights-year-head" onClick={toggle} aria-expanded={isOpen} data-testid="insights-year-head">
              {isOpen ? "▾" : "▸"} {y} <span className="muted">({rows.length})</span><span className="spacer" /><NewCount n={left} testId="insights-year-new" />
            </button>
            {isOpen && rows.map(render)}
          </div>
        );
      })}
    </>
  );
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
      <ByYear items={ov.daily} group="daily" year={(f) => f.since.slice(0, 4)} open={(f) => f.path === openPath} unread={(f) => f.unread ?? f.new ?? 0} render={(f) => (
        <button key={f.path} className={`list-item ${openPath === f.path ? "selected" : ""}`} onClick={() => onOpen(f.path)} data-testid="insights-day" data-day={f.since}>
          <span className="mono small">{f.since}</span>{f.partial && <span className="chip">so far</span>}<span className="muted small">{Math.ceil(f.size / 1024)} KB</span><NewCount n={f.new} unread={f.unread} />
        </button>
      )} />
    </div>
  );
}

interface HlDay { day: string; files: { name: string; since: string; until: string }[]; items: (Highlight & { seen_before?: boolean })[] }

/** owner/repo and number of a github.com pull request / issue link */
function ghRef(url: string): [string, number, "pr" | "issue"] | null {
  const m = /^https:\/\/github\.com\/([\w.-]+\/[\w.-]+)\/(pull|issues)\/(\d+)/.exec(url);
  return m ? [m[1], Number(m[3]), m[2] === "pull" ? "pr" : "issue"] : null;
}

function HlItem({ x, extra, onOpenPr }: { x: Highlight & { seen_before?: boolean; days?: string[] }; extra?: React.ReactNode; onOpenPr?: OpenPr }) {
  const ref = ghRef(x.url);
  return (
    <div className="insights-hl" data-testid="insights-hl" data-seen={x.seen_before ? "1" : "0"}>
      <span className={`insights-score ${x.xpu_relevance_score >= 90 ? "crit" : x.xpu_relevance_score >= 75 ? "high" : ""}`}>{x.xpu_relevance_score < 0 ? "?" : x.xpu_relevance_score}</span>
      <div className="insights-hl-main">
        {ref && onOpenPr ? (
          <a href={x.url} onClick={(e) => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return; e.preventDefault(); onOpenPr(ref[0], ref[1], x.title); }}
            title={`Open ${ref[0]}#${ref[1]} in PR Review (Ctrl/⌘-click: on GitHub)`} data-testid="insights-hl-open"><b>{x.title}</b></a>
        ) : <a href={x.url} target="_blank" rel="noreferrer"><b>{x.title}</b></a>}
        {ref && <span className="muted small"> {ref[2] === "pr" ? "PR" : "issue"} #{ref[1]}</span>}
        {ref && onOpenPr && <a className="link-btn small" href={x.url} target="_blank" rel="noreferrer" title="Open on GitHub" data-testid="insights-hl-github"> ↗</a>}
        {extra}
        <div className="small">{x.body}</div>
        <details className="small muted"><summary>why</summary>{x.xpu_relevance_justification}</details>
        <div className="muted small">{x.submitter} · {x.state}{x.vendor_involvement?.length ? ` · ${x.vendor_involvement.join(", ")}` : ""}</div>
      </div>
    </div>
  );
}

/** XPU highlights of a period, two views: per day (each day's run; items an earlier day already had are marked and
 * can be hidden), or flat (each item once, duplicates across days removed, with the days it was on). Like Daily, the
 * list (dates, the days) is on the left and the chosen day's highlights on the right. */
function Highlights({ repo, tabs, onRan, enabled, onOpenPr, sessions }: { repo: string; tabs: React.ReactNode; onRan: () => void; enabled: boolean; onOpenPr?: OpenPr;
  sessions: AgentSession[] }) {
  const textRef = useRef<HTMLDivElement>(null);
  const [dirs, setDirs] = useState<string[]>([]);
  const [chatting, setChatting] = useState(false);
  // the dates and the score are kept while the tab is open (e.g. back from a discussion of them)
  const kept = useMemo(() => { try { return JSON.parse(sessionStorage.getItem(`waw.insights.hl.${repo}`) ?? "{}") as { since?: string; until?: string; min?: number }; } catch { return {}; } }, [repo]);
  const [since, setSince] = useState(kept.since ?? daysAgo(7));
  const [until, setUntil] = useState(kept.until ?? todayIso());
  const [min, setMin] = useState(kept.min ?? 70);
  useEffect(() => sessionStorage.setItem(`waw.insights.hl.${repo}`, JSON.stringify({ since, until: until === todayIso() ? undefined : until, min })), [repo, since, until, min]);
  const [view, setView] = useState<"day" | "flat">(() => (localStorage.getItem("waw.insights.hlView") === "flat" ? "flat" : "day"));
  useEffect(() => localStorage.setItem("waw.insights.hlView", view), [view]);
  const [onlyNew, setOnlyNew] = useState(() => localStorage.getItem("waw.insights.hlOnlyNew") === "1");
  useEffect(() => localStorage.setItem("waw.insights.hlOnlyNew", onlyNew ? "1" : "0"), [onlyNew]);
  const [items, setItems] = useState<(Highlight & { days?: string[] })[] | null>(null);
  const [days, setDays] = useState<HlDay[] | null>(null);
  // the day shown on the right: the newest one (today's, or the latest day there are highlights for) unless you pick another
  const [picked, setPicked] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(async () => {
    setErr(null);
    try {
      const q = `/insights/repos/${repo}/highlights?since=${since}&until=${until}&view=${view}`;
      if (view === "day") {
        const d = (await get<{ days: HlDay[] }>(q)).days;
        setDays(d);
        setPicked((p) => (p && d.some((x) => x.day === p) ? p : d[0]?.day ?? null));
      }
      else setItems((await get<{ items: Highlight[] }>(q)).items);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [repo, since, until, view]);
  useEffect(() => { void load(); }, [load]);
  const keep = (x: Highlight) => x.xpu_relevance_score >= min || x.xpu_relevance_score < 0;
  const shown = useMemo(() => (items ?? []).filter(keep), [items, min]); // eslint-disable-line react-hooks/exhaustive-deps
  const dayList = useMemo(() => (days ?? []).map((d) => ({ ...d, shown: d.items.filter((x) => keep(x) && (!onlyNew || !x.seen_before)) })), [days, min, onlyNew]); // eslint-disable-line react-hooks/exhaustive-deps
  const day = view === "day" ? dayList.find((d) => d.day === picked) ?? null : null;
  const total = view === "day" ? day?.shown.length ?? 0 : shown.length;
  // discussions about what is shown: the day (per day) or the dates (with the score and "only new" filters), in a
  // panel on the right; those of one day (or of the same dates) are kept together
  const discKey = view === "day" ? (day ? `hl:${day.day}` : null) : `hl:${since}..${until}`;
  const disc = useInsightsDiscussion(repo, discKey, sessions, (body) => post<Started>(`/insights/repos/${repo}/highlights/chat`,
    { since, until, day: view === "day" ? day?.day : null, min_score: min, only_new: view === "day" && onlyNew, ...body }));
  const chat = disc.ask;
  return (
    <>
      <div className="insights-list">
        {tabs}
        <div className="insights-items">
          <div className="row nowrap small pad-x">
            <input type="date" value={since} onChange={(e) => setSince(e.target.value)} data-testid="insights-hl-since" /> – <input type="date" value={until} onChange={(e) => setUntil(e.target.value)} data-testid="insights-hl-until" />
          </div>
          <div className="pad-x">
            <span className="mode-switch insights-tabs" role="tablist">
              <button role="tab" aria-selected={view === "day"} className={view === "day" ? "active" : ""} onClick={() => setView("day")} data-testid="insights-hl-view-day">Per day</button>
              <button role="tab" aria-selected={view === "flat"} className={view === "flat" ? "active" : ""} onClick={() => setView("flat")} data-testid="insights-hl-view-flat">All, no duplicates</button>
            </span>
          </div>
          {view === "day" && dayList.length === 0 && days && <div className="muted small pad">No highlights in these days.</div>}
          {view === "day" && <ByYear items={dayList} group="hl" year={(d) => d.day.slice(0, 4)} open={(d) => d.day === picked} render={(d) => {
            const seen = d.shown.filter((x) => x.seen_before).length;
            return (
              <button key={d.day} className={`list-item ${d.day === picked ? "selected" : ""}`} onClick={() => setPicked(d.day)} aria-selected={d.day === picked}
                title={d.files.map((f) => `${f.name}: ${f.since} – ${f.until}`).join("\n")} data-testid="insights-hl-day" data-day={d.day}>
                <span className="mono small">{d.day}</span>{d.day === todayIso() && <span className="chip">today</span>}
                <span className="muted small">{d.shown.length}{d.shown.length !== d.items.length ? ` of ${d.items.length}` : ""}{seen ? ` · ${seen} seen before` : ""}</span>
              </button>
            );
          }} />}
          {view === "flat" && <div className="muted small pad">Each item once, over all the days of these dates.</div>}
        </div>
      </div>
      <div className="insights-main">
        <div className="insights-report" data-testid="insights-highlights">
          <div className="row nowrap small">
            {view === "day" && <b data-testid="insights-hl-day-title">{day ? `${day.day}${day.day === todayIso() ? " (today)" : ""}` : "No day"}</b>}
            {view === "flat" && <b>{since} – {until}</b>}
            score ≥ <input type="number" min={0} max={100} value={min} onChange={(e) => setMin(Number(e.target.value) || 0)} className="insights-num" data-testid="insights-hl-min" />
            {view === "day" && (
              <label title="Hide the items an earlier day of the period already had (the daily runs' windows overlap)">
                <input type="checkbox" checked={onlyNew} onChange={(e) => setOnlyNew(e.target.checked)} data-testid="insights-hl-only-new" /> only new each day
              </label>
            )}
            <span className="muted" data-testid="insights-hl-count">{total} shown</span>
            <span className="spacer" />
            <button className="btn small" disabled={chatting || total === 0} onClick={async () => { setChatting(true); setErr(null); try { await chat({}); } catch (e) { setErr((e as ApiError).message); } finally { setChatting(false); } }}
              title="Ask the agent about the highlights shown: a new discussion in the panel on the right (or select a part of them: Discuss this)" data-testid="insights-hl-chat">Ask about all of it</button>
            <button className="btn small" onClick={async () => { setErr(null); try { await post("/insights/run", { repo, kind: "highlights", since, until }); onRan(); } catch (e) { setErr((e as ApiError).message); } }}
              title={enabled ? "Score every issue and PR active in these days now" : "XPU highlights are off for this repository (⚙); you can still score now"} data-testid="insights-run-hl">Score now</button>
          </div>
          {err && <div className="error-text small">{err}</div>}
          <div ref={textRef} className="insights-report-text">
          {view === "flat" && shown.map((x) => (
            <HlItem key={x.url} x={x} onOpenPr={onOpenPr} extra={x.days && x.days.length > 1 ? <span className="chip" title={`On ${x.days.join(", ")}`} data-testid="insights-hl-days"> on {x.days.length} days</span> : null} />
          ))}
          {view === "day" && !day && days && <div className="muted small">No highlights in these days.</div>}
          {day && (
            <div className="insights-hl-day-list" data-testid="insights-hl-day-list" data-day={day.day}>
              {day.shown.map((x) => (
                <HlItem key={x.url} x={x} onOpenPr={onOpenPr} extra={x.seen_before ? <span className="chip muted" title="An earlier day of this period already had it" data-testid="insights-hl-seen"> seen before</span> : null} />
              ))}
              {day.shown.length === 0 && <div className="muted small">Nothing above the score{onlyNew ? " that is new this day" : ""}.</div>}
            </div>
          )}
          </div>
          {total > 0 && (
            <SelectionAsk container={textRef} within=".insights-report-text" label="Discuss this" sendLabel="Start discussion" pictures={false} optional testId="insights-sel"
              hint="A new discussion in the panel on the right, with the highlights shown as its material"
              extra={<DiscussFolders repo={repo} dirs={dirs} onChange={setDirs} />}
              onAsk={async (part, question) => { await chat({ quote: part.quote, question, dirs }); setDirs([]); }} />
          )}
        </div>
      </div>
      {disc.panel}
    </>
  );
}

interface SchedulePlan { running: string | null; due: string[]; reports_due?: string[]; deferred: string | null }
function ScheduleState() {
  const [p, setP] = useState<SchedulePlan | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => void get<{ schedule: SchedulePlan }>("/insights/status").then((r) => alive && setP(r.schedule)).catch(() => undefined);
    load();
    const t = setInterval(load, 20_000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  return (
    <div className="muted small" data-testid="insights-schedule-state">
      Scheduled runs go one repository at a time, start only with at least half of the GitHub quota left, and use at most 40% of
      each hourly quota, evenly paced — the rest stays for your own work.
      {p?.running && <div>Running now: <b>{p.running}</b>{p.due.filter((r) => r !== p.running).length ? ` · next: ${p.due.filter((r) => r !== p.running).join(", ")}` : ""}</div>}
      {!p?.running && p?.deferred && <div className="warn">Waiting: {p.deferred} · due: {[...p.due, ...(p.reports_due ?? [])].join(", ")}</div>}
      {(p?.reports_due?.length ?? 0) > 0 && <div data-testid="insights-reports-due">Reports due: {p!.reports_due!.join(", ")}</div>}
    </div>
  );
}

function InsightsSettings() {
  const [s, setS] = useState<Settings | null>(null);
  const [key, setKey] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { void loadInsightsSettings().then(setS).catch((e) => setErr((e as ApiError).message)); }, []);
  useEffect(() => {
    const sync = () => setS(settingsCache);
    window.addEventListener("waw-insights-settings", sync);
    return () => window.removeEventListener("waw-insights-settings", sync);
  }, []);
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
        cacheSettings(next);
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
      <div className="row nowrap small" title="Longer material is split in parts of at most this size, each answered, then combined">characters per request: API
        <input type="number" className="insights-chars" data-testid="insights-max-chars" defaultValue={s.max_chars} onBlur={(e) => void save({ max_chars: Number(e.target.value) })} />
        · agent <input type="number" className="insights-chars" data-testid="insights-agent-max-chars" defaultValue={s.agent_max_chars} onBlur={(e) => void save({ agent_max_chars: Number(e.target.value) })} /></div>

      <div className="small strong">Repositories <span className="muted">(daily times in UTC+{s.timezone_offset}; each summarizes the previous UTC day)</span></div>
      <ScheduleState />
      {s.repos.map((r, i) => (
        <div key={r.repo} className="insights-repo-cfg" data-testid="insights-repo-cfg" data-repo={r.repo}>
          <div className="row nowrap"><b className="mono">{r.repo}</b><span className="spacer" />
            <button className="icon-btn small" disabled={i === 0} onClick={() => setS({ ...s, repos: s.repos.map((x, k) => (k === i - 1 ? r : k === i ? s.repos[i - 1] : x)) })} title="Move up (Save repositories to keep the order)" data-testid="insights-repo-up">↑</button>
            <button className="icon-btn small" disabled={i === s.repos.length - 1} onClick={() => setS({ ...s, repos: s.repos.map((x, k) => (k === i + 1 ? r : k === i ? s.repos[i + 1] : x)) })} title="Move down (Save repositories to keep the order)" data-testid="insights-repo-down">↓</button>
            <button className="icon-btn small" onClick={() => setS({ ...s, repos: s.repos.filter((_, k) => k !== i) })} title="Remove (its files stay)">×</button></div>
          <div className="row nowrap small">
            {(["issues", "commits", "rfcs"] as const).map((k) => <label key={k}><input type="checkbox" checked={r.sources[k]} onChange={(e) => setRepo(i, { sources: { ...r.sources, [k]: e.target.checked } })} /> {k}</label>)}
            <label><input type="checkbox" checked={r.highlights} onChange={(e) => setRepo(i, { highlights: e.target.checked })} /> XPU highlights</label>
            <label title="Make the daily summary (and highlights) every day at this time"><input type="checkbox" checked={r.schedule.enabled} onChange={(e) => setRepo(i, { schedule: { ...r.schedule, enabled: e.target.checked } })} data-testid="insights-schedule" /> every day at</label>
            <input type="time" value={r.schedule.time} onChange={(e) => setRepo(i, { schedule: { ...r.schedule, time: e.target.value } })} />
          </div>
          <div className="row nowrap small" title="When a week (Mon–Sun) / month / quarter / year has ended, its report is made by itself — after this repository's time of day and its daily summary of the period's last day. Only the latest finished period, once each.">
            reports when a period ends:
            {(["week", "month", "quarter", "year"] as const).map((k) => {
              const rp = r.reports ?? { week: false, month: false, quarter: false, year: false };
              return <label key={k}><input type="checkbox" checked={rp[k]} onChange={(e) => setRepo(i, { reports: { ...rp, [k]: e.target.checked } })} data-testid={`insights-report-${k}`} /> {k === "week" ? "weekly" : `${k}ly`}</label>;
            })}
          </div>
          <div className="row nowrap small">reports compare what is new with:
            <BaselinePick value={r.new_baseline ?? { mode: "previous", date: null }} onChange={(b) => setRepo(i, { new_baseline: b })} testid="insights-repo-baseline" /></div>
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
