import { useEffect, useState } from "react";
import { ApiError, del, get, post, put } from "../api";
import { confirmAction } from "./Confirm";
import FolderPicker from "./FolderPicker";

interface Link { path: string; update: boolean; remote: string | null }
interface Sync { state: "none" | "idle" | "running" | "done" | "failed"; sha?: string; ref?: string; base?: string; base_updated?: boolean | null; notes?: string[]; message?: string }

/**
 * The repository's connected local git checkout — the agent's default reference for its PRs. Opening a PR fetches
 * its head into the checkout (and, when enabled — the default — brings the base branch up to date first).
 */
type Period = "day" | "week" | "month" | "quarter" | "year";

export default function LocalRepo({ repo, apiBase, onSynced, onOpenChat }: {
  repo: string; apiBase: string; onSynced?: () => void;
  /** a change summary was started: open its chat (folder id, chat path) */
  onOpenChat?: (workspaceId: string, path: string) => void;
}) {
  const [period, setPeriod] = useState<Period>("week");
  const [end, setEnd] = useState("");
  const [focus, setFocus] = useState("");
  const [link, setLink] = useState<Link | null | undefined>(undefined);
  const [sync, setSync] = useState<Sync | null>(null);
  const [open, setOpen] = useState(false);
  const [path, setPath] = useState("");
  const [update, setUpdate] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [browse, setBrowse] = useState(false);

  const startSync = async () => {
    try {
      setSync(await post<Sync>(`${apiBase}/local-sync`));
    } catch (e) {
      setSync({ state: "failed", message: (e as ApiError).message });
    }
  };
  useEffect(() => {
    let live = true;
    get<{ link: Link | null }>(`/github/local-repo?repo=${encodeURIComponent(repo)}`)
      .then((r) => {
        if (!live) return;
        setLink(r.link);
        if (r.link) {
          setPath(r.link.path);
          setUpdate(r.link.update);
          void startSync();
        }
      })
      .catch(() => live && setLink(null));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo, apiBase]);
  useEffect(() => {
    if (sync?.state !== "running") return;
    const t = window.setInterval(() => {
      void get<Sync>(`${apiBase}/local-sync`).then((s) => {
        setSync(s);
        if (s.state === "done") onSynced?.();
      }).catch(() => undefined);
    }, 2500);
    return () => window.clearInterval(t);
  }, [sync?.state, apiBase, onSynced]);

  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await put<{ link: Link; note: string | null }>("/github/local-repo", { repo, path: path.trim(), update });
      setLink(r.link);
      setNote(r.note);
      setOpen(false);
      await startSync();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const disconnect = async () => {
    if (!(await confirmAction({ title: `Disconnect the local checkout of ${repo}?`, message: `${link?.path}\n\nThe checkout itself is not touched; the agent no longer uses it for ${repo} PRs.`, confirmLabel: "Disconnect" }))) return;
    await del(`/github/local-repo?repo=${encodeURIComponent(repo)}`);
    setLink(null);
    setSync(null);
    setOpen(false);
  };
  const summarize = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post<{ workspace_id: string; path: string }>("/github/repo-summary", { repo, period, end: end || undefined, focus });
      setOpen(false);
      onOpenChat?.(r.workspace_id, r.path);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  if (link === undefined) return null;
  const name = link ? link.path.split("/").filter(Boolean).pop() : null;
  const status = !link ? null : sync?.state === "running" ? "fetching the PR…" : sync?.state === "done" ? `PR at ${sync.sha?.slice(0, 7)}${sync.base_updated ? ` · ${sync.base} updated` : ""}` : sync?.state === "failed" ? "fetch failed" : "";
  return (
    <span className="ws-menu-anchor local-repo">
      <button className={`link-btn small ${sync?.state === "failed" ? "del" : ""}`} onClick={() => setOpen((v) => !v)}
        title={link ? `Local checkout: ${link.path}${sync?.notes?.length ? `\n${sync.notes.join("\n")}` : ""}${sync?.message ? `\n${sync.message}` : ""}` : `Connect ${repo} to a local git checkout so the agent reads the real code`}
        data-testid="pr-local-repo" data-state={sync?.state ?? (link ? "idle" : "none")}>
        {link ? <>⎇ {name}{status ? <span className="muted"> · {status}</span> : null}</> : "⎇ Connect local repo"}
      </button>
      {open && (
        <div className="ws-menu pr-local-repo-box" data-testid="pr-local-repo-box">
          <div className="small"><b>Local checkout of {repo}</b> — the agent's default reference for this repository's PRs: opening a PR fetches its
            head there as <code>refs/waw/pr/&lt;n&gt;</code> (never checked out; your working tree is not changed).</div>
          <div className="row">
            <input className="mono grow" value={path} onChange={(e) => setPath(e.target.value)} placeholder="/path/to/pytorch (a git checkout on the server)" data-testid="pr-local-repo-path" />
            <button className="btn small" onClick={() => setBrowse(true)} data-testid="pr-local-repo-browse">Browse…</button>
          </div>
          {browse && (
            <FolderPicker title={`Local checkout of ${repo}`} hint="Choose the top folder of a Git checkout of this repository." start={path.trim() || undefined} requireGit
              confirmLabel="Use this checkout" onClose={() => setBrowse(false)} onChoose={(p) => { setPath(p); setBrowse(false); }} />
          )}
          <label className="small">
            <input type="checkbox" checked={update} onChange={(e) => setUpdate(e.target.checked)} data-testid="pr-local-repo-update" /> Update the local project first
            <span className="muted"> — fetch the PR's base branch, and fast-forward it when it is checked out with no local changes</span>
          </label>
          {sync?.notes?.map((n) => <div key={n} className="muted small">{n}</div>)}
          {sync?.message && <div className="small del">{sync.message}</div>}
          {note && <div className="muted small">{note}</div>}
          {link && (
            <div className="pr-summary-box" data-testid="repo-summary">
              <div className="small"><b>Summarize key changes</b> of {repo} from the checkout's git log (a chat in the agent wiki — continue it, or distill it into a page):</div>
              <div className="row">
                <span className="mode-switch" role="tablist" aria-label="Period">
                  {(["day", "week", "month", "quarter", "year"] as const).map((p) => (
                    <button key={p} role="tab" aria-selected={period === p} className={period === p ? "active" : ""} onClick={() => setPeriod(p)} data-testid={`repo-summary-${p}`}>{p[0].toUpperCase() + p.slice(1)}</button>
                  ))}
                </span>
                <label className="small muted">ending <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} data-testid="repo-summary-end" /></label>
              </div>
              <input value={focus} onChange={(e) => setFocus(e.target.value)} placeholder="Optional focus, e.g. XPU, inductor, distributed" data-testid="repo-summary-focus" />
              <button className="btn small primary" disabled={busy} onClick={() => void summarize()} data-testid="repo-summary-run">{busy ? "Reading the git log…" : `Summarize the ${period}`}</button>
            </div>
          )}
          {err && <div className="small del" role="alert" data-testid="pr-local-repo-error">{err}</div>}
          <div className="row">
            <button className="btn small primary" disabled={busy || !path.trim()} onClick={() => void save()} data-testid="pr-local-repo-save">{link ? "Save" : "Connect"}</button>
            {link && <button className="btn small" disabled={sync?.state === "running"} onClick={() => void startSync()} data-testid="pr-local-repo-sync">Fetch again</button>}
            {link && <button className="btn small danger" onClick={() => void disconnect()} data-testid="pr-local-repo-disconnect">Disconnect</button>}
            <button className="btn small" onClick={() => setOpen(false)}>Close</button>
          </div>
        </div>
      )}
    </span>
  );
}
