import { useCallback, useEffect, useState } from "react";
import { ApiError, del, get, patch, post } from "../api";
import { confirmAction } from "./Confirm";
import GhUser from "./GhUser";

/** A saved GitHub search of one repository (Reviews → Monitors); independent of the review queue. */
export interface MonitorRule {
  id: string;
  repo: string;
  query: string;
  name: string;
  seen_at: number | null;
  unseen: number | null;
}

export interface MonitorItem {
  kind: "pr" | "issue";
  repo: string;
  number: number;
  title: string;
  url: string;
  state: string;
  draft: boolean;
  author: string | null;
  labels: { name: string; color?: string }[];
  comments: number;
  created_at: string;
  updated_at: string;
  new: boolean;
  updated: boolean;
}

interface Items {
  query: string;
  total: number;
  items: MonitorItem[];
  unseen: number;
}

const ago = (iso: string) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  if (s < 86400 * 60) return `${Math.round(s / 86400)}d`;
  return `${Math.round(s / (86400 * 30))}mo`;
};

const EXAMPLES = 'is:open label:"module: xpu" · is:issue is:open no:assignee · is:pr author:someone · is:pr review:required updated:>@today-7d';

/** Monitors: per repository, rules written as GitHub searches; each lists its pull requests and issues and marks
 * what is new or updated since the rule was last opened. */
export default function MonitorsPanel({ repos, onOpenPr, onUnseen }: {
  /** repositories to suggest for a new rule */
  repos: string[];
  onOpenPr: (repo: string, number: number, title: string) => void;
  /** how many items are new or updated over all rules (for the tab) */
  onUnseen?: (n: number) => void;
}) {
  const [rules, setRules] = useState<MonitorRule[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [items, setItems] = useState<Record<string, Items | { error: string } | "loading">>({});
  const [open, setOpen] = useState<Set<string>>(() => new Set(JSON.parse(localStorage.getItem("waw.monitors.open") || "[]")));
  useEffect(() => localStorage.setItem("waw.monitors.open", JSON.stringify([...open])), [open]);
  const [adding, setAdding] = useState<{ repo: string; query: string; name: string } | null>(null);
  const [editing, setEditing] = useState<{ id: string; query: string; name: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const fetchItems = useCallback(async (rule: MonitorRule, refresh = false) => {
    setItems((m) => ({ ...m, [rule.id]: m[rule.id] && m[rule.id] !== "loading" && !refresh ? m[rule.id] : "loading" }));
    try {
      const d = await get<Items>(`/github/monitors/${rule.id}/items${refresh ? "?refresh=true" : ""}`);
      setItems((m) => ({ ...m, [rule.id]: d }));
      return d;
    } catch (e) {
      setItems((m) => ({ ...m, [rule.id]: { error: (e as ApiError).message } }));
      return null;
    }
  }, []);

  const load = useCallback(async (refresh = false) => {
    try {
      const d = await get<{ rules: MonitorRule[] }>("/github/monitors");
      setRules(d.rules);
      setError(null);
      await Promise.all(d.rules.map((r) => fetchItems(r, refresh)));
    } catch (e) {
      setError((e as ApiError).message);
    }
  }, [fetchItems]);
  useEffect(() => { void load(); }, [load]);

  const unseenOf = (r: MonitorRule) => {
    const it = items[r.id];
    return it && it !== "loading" && "items" in it ? it.unseen : r.unseen ?? 0;
  };
  useEffect(() => { onUnseen?.((rules ?? []).reduce((n, r) => n + (open.has(r.id) ? 0 : unseenOf(r)), 0)); });

  // opening a rule shows what is new since it was last opened, and from then on that counts as seen
  const toggle = async (r: MonitorRule) => {
    const o = new Set(open);
    if (o.has(r.id)) {
      o.delete(r.id);
      setOpen(o);
      void fetchItems(r);
      return;
    }
    o.add(r.id);
    setOpen(o);
    await fetchItems(r);
    try {
      const s = await post<MonitorRule>(`/github/monitors/${r.id}/seen`);
      setRules((rs) => rs && rs.map((x) => (x.id === r.id ? { ...x, seen_at: s.seen_at } : x)));
    } catch {
      /* the marks stay until next time */
    }
  };

  const add = async () => {
    if (!adding) return;
    setBusy(true);
    try {
      const r = await post<MonitorRule>("/github/monitors", { repo: adding.repo.trim(), query: adding.query, name: adding.name.trim() || null });
      setRules((rs) => [...(rs ?? []), r]);
      setAdding(null);
      setError(null);
      void fetchItems(r);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const save = async () => {
    if (!editing) return;
    setBusy(true);
    try {
      const r = await patch<MonitorRule>(`/github/monitors/${editing.id}`, { query: editing.query, name: editing.name });
      setRules((rs) => rs && rs.map((x) => (x.id === r.id ? r : x)));
      setEditing(null);
      setError(null);
      void fetchItems(r, true);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const remove = async (r: MonitorRule) => {
    if (!(await confirmAction({ title: `Delete the monitor “${r.name}”?`, message: `${r.repo}: ${r.query}`, confirmLabel: "Delete", danger: true }))) return;
    try {
      await del(`/github/monitors/${r.id}`);
      setRules((rs) => rs && rs.filter((x) => x.id !== r.id));
    } catch (e) {
      setError((e as ApiError).message);
    }
  };

  const byRepo = new Map<string, MonitorRule[]>();
  for (const r of rules ?? []) byRepo.set(r.repo, [...(byRepo.get(r.repo) ?? []), r]);

  const row = (x: MonitorItem) => {
    const body = (
      <>
        <span className={`mon-kind ${x.kind} ${x.state.toLowerCase()}`} title={`${x.kind === "pr" ? "Pull request" : "Issue"} · ${x.state.toLowerCase()}${x.draft ? " · draft" : ""}`}>{x.kind === "pr" ? "⇄" : "◉"}</span>
        <span className="review-pr-main">
          <span className="review-pr-title">{x.title}</span>
          <span className="review-pr-meta muted small">
            #{x.number} · <GhUser login={x.author} bold={false} inButton /> · {ago(x.updated_at)}{x.comments ? ` · 💬 ${x.comments}` : ""}
            {x.labels.slice(0, 4).map((l) => <span key={l.name} className="review-label" style={l.color ? { borderColor: `#${l.color}` } : undefined}>{l.name}</span>)}
          </span>
        </span>
        {(x.new || x.updated) && <span className={`mon-mark ${x.new ? "new" : "updated"}`} data-testid="monitor-mark">{x.new ? "new" : "updated"}</span>}
      </>
    );
    const common = { className: `review-pr mon-item ${x.new || x.updated ? "unseen" : ""}`, "data-testid": "monitor-item", "data-kind": x.kind, "data-number": x.number };
    return x.kind === "pr" ? (
      <button key={`${x.kind}${x.number}`} {...common} onClick={() => onOpenPr(x.repo, x.number, `#${x.number} ${x.title}`)} title={`${x.repo}#${x.number}\n${x.title}`}>{body}</button>
    ) : (
      <a key={`${x.kind}${x.number}`} {...common} href={x.url} target="_blank" rel="noreferrer" title={`${x.repo}#${x.number} (opens on GitHub)\n${x.title}`}>{body}</a>
    );
  };

  return (
    <div className="monitors" data-testid="monitors">
      <div className="reviews-controls">
        {adding ? (
          <div className="mon-form" data-testid="monitor-form">
            <input list="mon-repos" value={adding.repo} onChange={(e) => setAdding({ ...adding, repo: e.target.value })} placeholder="owner/repository" className="reviews-filter" data-testid="monitor-repo" />
            <datalist id="mon-repos">{repos.map((r) => <option key={r} value={r} />)}</datalist>
            <input autoFocus value={adding.query} onChange={(e) => setAdding({ ...adding, query: e.target.value })} placeholder="GitHub search: is:open label:bug -author:@me"
              onKeyDown={(e) => { if (e.key === "Enter") void add(); if (e.key === "Escape") setAdding(null); }} className="reviews-filter mono" data-testid="monitor-query" />
            <input value={adding.name} onChange={(e) => setAdding({ ...adding, name: e.target.value })} placeholder="Name (optional)" className="reviews-filter"
              onKeyDown={(e) => { if (e.key === "Enter") void add(); }} data-testid="monitor-name" />
            <div className="muted small">Pull requests and issues (add is:pr or is:issue for one kind), as on GitHub. E.g. {EXAMPLES}</div>
            <div className="reviews-row">
              <span className="spacer" />
              <button className="btn small" onClick={() => setAdding(null)}>Cancel</button>
              <button className="btn small primary" disabled={busy || !adding.repo.trim() || !adding.query.trim()} onClick={() => void add()} data-testid="monitor-add-save">Add</button>
            </div>
          </div>
        ) : (
          <div className="reviews-row">
            <button className="link-btn small" onClick={() => setAdding({ repo: repos[0] ?? "", query: "is:open ", name: "" })} data-testid="monitor-add">＋ Add a rule…</button>
            <button className="icon-btn small" onClick={() => void load(true)} title="Refresh every rule from GitHub" aria-label="Refresh monitors" data-testid="monitors-refresh">↻</button>
          </div>
        )}
        {error && <div className="tree-error small" data-testid="monitors-error">{error}</div>}
      </div>
      <div className="tree-body" data-testid="monitors-list">
        {rules && rules.length === 0 && !adding && (
          <div className="tree-empty">No monitors yet. Add a rule: a GitHub search of a repository's pull requests and issues, e.g. <code>is:open label:bug</code>.</div>
        )}
        {[...byRepo.entries()].map(([repo, rs]) => (
          <div key={repo} className="review-group" data-testid="monitor-repo-group" data-repo={repo}>
            <div className="muted small reviews-meta strong">{repo}</div>
            {rs.map((r) => {
              const it = items[r.id];
              const n = unseenOf(r);
              const isOpen = open.has(r.id);
              return (
                <div key={r.id} className="mon-rule" data-testid="monitor-rule" data-id={r.id}>
                  <div className="review-group-head-row">
                    <button className="review-group-head" onClick={() => void toggle(r)} aria-expanded={isOpen} title={`${r.repo}: ${r.query}`} data-testid="monitor-rule-head">
                      <span className="chev">{isOpen ? "⌄" : "›"}</span>
                      <span className="review-repo">{r.name}</span>
                      <span className="spacer" />
                      {!isOpen && n > 0 && <span className="activity-badge mon-unseen" title={`${n} new or updated since you last opened it`} data-testid="monitor-unseen">{n}</span>}
                      {it && it !== "loading" && "items" in it && <span className="git-badge clean" data-testid="monitor-count">{it.total}</span>}
                    </button>
                    <button className="icon-btn small" onClick={() => setEditing({ id: r.id, query: r.query, name: r.name })} title="Edit the rule" aria-label="Edit rule" data-testid="monitor-edit">✎</button>
                    <button className="icon-btn small" onClick={() => void remove(r)} title="Delete the rule" aria-label="Delete rule" data-testid="monitor-delete">×</button>
                  </div>
                  {editing?.id === r.id && (
                    <div className="mon-form pad-x">
                      <input autoFocus value={editing.query} onChange={(e) => setEditing({ ...editing, query: e.target.value })} className="reviews-filter mono"
                        onKeyDown={(e) => { if (e.key === "Enter") void save(); if (e.key === "Escape") setEditing(null); }} data-testid="monitor-edit-query" />
                      <input value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} placeholder="Name" className="reviews-filter"
                        onKeyDown={(e) => { if (e.key === "Enter") void save(); }} data-testid="monitor-edit-name" />
                      <div className="reviews-row">
                        <span className="spacer" />
                        <button className="btn small" onClick={() => setEditing(null)}>Cancel</button>
                        <button className="btn small primary" disabled={busy || !editing.query.trim()} onClick={() => void save()} data-testid="monitor-edit-save">Save</button>
                      </div>
                    </div>
                  )}
                  {isOpen && (
                    <div className="mon-items">
                      <div className="muted small reviews-meta"><code>{r.query}</code></div>
                      {it === "loading" && <div className="muted small reviews-meta">Loading…</div>}
                      {it && it !== "loading" && "error" in it && <div className="tree-error small pad-x" data-testid="monitor-error">{it.error}</div>}
                      {it && it !== "loading" && "items" in it && (
                        <>
                          {it.items.map(row)}
                          {it.items.length === 0 && <div className="muted small reviews-meta">Nothing matches.</div>}
                          {it.total > it.items.length && <div className="muted small reviews-meta">{it.items.length} of {it.total} shown (the most recently updated)</div>}
                        </>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
