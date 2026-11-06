import { useEffect, useMemo, useState } from "react";
import { ApiError, GitStatus, get, post } from "../api";
import { FileIcon } from "./FileIcon";
import { useJobStream } from "./DiscussionPanel";

/**
 * Commit workflow for the Diff view: choose changed files, let the agent draft a commit message from the real diff
 * (editable), commit with the user's Git identity, then optionally push and open a pull request with `gh`.
 */
export default function CommitPanel({ refreshKey, preselect, onDone, onNotify }: { refreshKey: number; preselect?: string[]; onDone: () => void; onNotify: (msg: string) => void }) {
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set(preselect ?? []));
  const [message, setMessage] = useState("");
  const [draftJob, setDraftJob] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [lastCommit, setLastCommit] = useState<{ short: string; subject: string; branch: string | null } | null>(null);
  const [prTitle, setPrTitle] = useState("");
  const [prBase, setPrBase] = useState("");
  const [prUrl, setPrUrl] = useState<string | null>(null);

  const load = async () => {
    try {
      const st = await get<GitStatus>("/git/status");
      setStatus(st);
      setSelected((s) => new Set([...s].filter((p) => st.files.some((f) => f.path === p))));
      setError(null);
    } catch (e) {
      setError((e as ApiError).message);
    }
  };
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  const draft = useJobStream(draftJob, (job) => {
    if (job.status === "completed" && job.text.trim()) {
      setMessage(job.text.trim());
      if (!prTitle) setPrTitle(job.text.trim().split("\n")[0].slice(0, 120));
    } else if (job.error) setError(`${job.error.code}: ${job.error.message}`);
    setDraftJob(null);
  });

  const files = status?.files ?? [];
  const committable = useMemo(() => files.filter((f) => !f.lock), [files]);
  const allSelected = committable.length > 0 && committable.every((f) => selected.has(f.path));

  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(committable.map((f) => f.path)));
  const toggleOne = (p: string) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(p)) n.delete(p);
      else n.add(p);
      return n;
    });

  const run = async (label: string, fn: () => Promise<void>) => {
    setBusy(label);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };

  const draftMessage = () =>
    run("draft", async () => {
      const r = await post<{ job_id: string }>("/git/draft-message", { paths: [...selected] });
      setDraftJob(r.job_id);
    });

  const commit = () =>
    run("commit", async () => {
      const r = await post<{ short: string; subject: string; branch: string | null }>("/git/commit", { paths: [...selected], message });
      setLastCommit(r);
      if (!prTitle) setPrTitle(r.subject);
      setMessage("");
      setSelected(new Set());
      onNotify(`Committed ${r.short}: ${r.subject}`);
      await load();
      onDone();
    });

  const push = () =>
    run("push", async () => {
      const r = await post<{ remote: string; branch: string; upstream: string | null }>("/git/push", {});
      onNotify(`Pushed ${r.branch} to ${r.remote}${r.upstream ? ` (${r.upstream})` : ""}.`);
      await load();
    });

  const openPr = () =>
    run("pr", async () => {
      const r = await post<{ url: string }>("/git/pull-request", { title: prTitle, body: message || lastCommit?.subject || "", base: prBase || null });
      setPrUrl(r.url);
      onNotify(`Pull request created: ${r.url}`);
    });

  if (!status) return <div className="muted pad">{error ?? "Loading Git status…"}</div>;

  return (
    <div className="commit-panel" data-testid="commit-panel">
      <div className="commit-head">
        <div>
          <b>{status.branch}</b>
          {status.upstream && (
            <span className="muted small"> → {status.upstream}{status.ahead ? ` · ${status.ahead} ahead` : ""}{status.behind ? ` · ${status.behind} behind` : ""}</span>
          )}
          {!status.upstream && status.origin && <span className="muted small"> · no upstream yet (first push sets it)</span>}
          {!status.origin && <span className="muted small"> · no remote</span>}
        </div>
        <div className="muted small" title={`${status.identity.name} <${status.identity.email}>`}>
          {status.identity.configured ? `as ${status.identity.name}` : "⚠ git user.name/email not configured"}
        </div>
      </div>

      <section>
        <div className="label">
          <label><input type="checkbox" checked={allSelected} onChange={toggleAll} disabled={!committable.length} data-testid="commit-select-all" /> {files.length ? `${selected.size} of ${files.length} changed files selected` : "Working tree is clean"}</label>
        </div>
        <div className="commit-files">
          {files.map((f) => (
            <label key={f.path} className={`commit-file ${f.lock ? "locked" : ""}`} title={f.lock ? `Locked${f.lock.reason ? ` — ${f.lock.reason}` : ""}; unlock to commit` : f.path}>
              <input type="checkbox" checked={selected.has(f.path)} disabled={!!f.lock} onChange={() => toggleOne(f.path)} data-testid={`commit-file-${f.path}`} />
              <FileIcon item={{ type: "file", kind: f.is_markdown ? "markdown" : "file" }} />
              <span className="path">{f.path}</span>
              <span className={`chip git ${f.status}`}>{f.status}</span>
              {f.lock && <span className="chip locked">🔒</span>}
            </label>
          ))}
        </div>
      </section>

      <section>
        <div className="label">
          Commit message
          <button className="btn small" disabled={!selected.size || busy !== null || !!draftJob} onClick={() => void draftMessage()} data-testid="commit-draft" title="Let the agent summarise the selected changes into a commit message (you can edit it)">
            {draftJob ? "Drafting…" : "Draft with agent"}
          </button>
          {draft && draftJob && <span className="muted small">{draft.status}</span>}
        </div>
        <textarea value={message} onChange={(e) => setMessage(e.target.value)} rows={6} placeholder="Subject line (≤72 chars), blank line, then details. Or click 'Draft with agent'." data-testid="commit-message" />
        <div className="row">
          <button className="btn primary" disabled={!selected.size || !message.trim() || busy !== null || !status.identity.configured} onClick={() => void commit()} data-testid="commit-button">
            {busy === "commit" ? "Committing…" : `Commit ${selected.size ? `${selected.size} file${selected.size > 1 ? "s" : ""}` : ""}`}
          </button>
          <span className="muted small">Commits only the selected files, with your Git identity. Nothing is pushed.</span>
        </div>
      </section>

      {(status.origin || lastCommit) && (
        <section>
          <div className="label">Share</div>
          <div className="row">
            <button className="btn" disabled={busy !== null || !status.origin || (status.ahead === 0 && !!status.upstream)} onClick={() => void push()} data-testid="push-button" title={status.upstream ? `git push ${status.upstream.split("/")[0]}` : "git push --set-upstream origin <branch>"}>
              {busy === "push" ? "Pushing…" : status.ahead ? `Push ${status.ahead} commit${status.ahead > 1 ? "s" : ""}` : "Push"}
            </button>
            {status.behind ? <span className="warn small">The remote has {status.behind} commit{status.behind > 1 ? "s" : ""} you don't have; pull/rebase in the terminal first (the app never force-pushes).</span> : null}
          </div>
          {status.origin_is_github && (
            <div className="pr-box">
              <div className="row">
                <input value={prTitle} onChange={(e) => setPrTitle(e.target.value)} placeholder="Pull request title" data-testid="pr-title" />
                <input value={prBase} onChange={(e) => setPrBase(e.target.value)} placeholder="base branch (default)" title="Leave empty for the repository's default branch" className="short" />
                <button className="btn" disabled={busy !== null || !status.gh_available || !prTitle.trim()} onClick={() => void openPr()} data-testid="pr-button" title={status.gh_available ? "gh pr create --head <branch>" : "GitHub CLI (gh) not available or not logged in"}>
                  {busy === "pr" ? "Creating…" : "Create pull request"}
                </button>
              </div>
              <div className="muted small">
                Uses the GitHub CLI with your login{status.gh_available ? "" : " — run `gh auth login` in the terminal to enable"}. The PR body is the commit message above.
                {status.branch && ["main", "master"].includes(status.branch) && " You are on the default branch: create a feature branch in the terminal first (git switch -c my-change) if you want a PR."}
              </div>
              {prUrl && <div className="small">PR: <a href={prUrl} target="_blank" rel="noopener noreferrer">{prUrl}</a></div>}
            </div>
          )}
        </section>
      )}

      {status.recent_commits.length > 0 && (
        <section>
          <div className="label">Recent commits</div>
          <ul className="recent-commits">
            {status.recent_commits.slice(0, 5).map((c) => (
              <li key={c.short}><code>{c.short}</code> {c.subject} <span className="muted small">· {c.when} · {c.author}</span></li>
            ))}
          </ul>
        </section>
      )}
      {error && <div className="error-text" role="alert" data-testid="commit-error">{error}</div>}
    </div>
  );
}
