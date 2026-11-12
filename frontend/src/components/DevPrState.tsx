import { useEffect, useState } from "react";
import { ApiError, get, post } from "../api";

export interface DevPrInfo { repo: string; number: number; state: "OPEN" | "CLOSED" | "MERGED"; title: string | null; url: string | null; head: string | null; base: string | null }

/** The pull request's state, read from GitHub (gh). */
export function usePrInfo(repo: string | null, number: number | null, refresh = 0): [DevPrInfo | null, (i: DevPrInfo) => void] {
  const [info, setInfo] = useState<DevPrInfo | null>(null);
  useEffect(() => {
    setInfo(null);
    if (!repo || !number) return;
    void get<DevPrInfo>(`/dev/prs/${repo}/${number}`).then(setInfo).catch(() => undefined);
  }, [repo, number, refresh]);
  return [info, setInfo];
}

/**
 * Close (with an optional comment) or reopen a pull request, through the server's gh login. Its branch and your
 * workspaces stay; a closed PR can be reopened. Merged PRs cannot change.
 */
export default function DevPrState({ repo, number, state, onDone, compact = false }: {
  repo: string; number: number; state: string; onDone: (state: "OPEN" | "CLOSED") => void; compact?: boolean;
}) {
  const [asking, setAsking] = useState(false);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const run = async (action: "close" | "reopen") => {
    setBusy(true);
    setErr(null);
    try {
      const r = await post<{ state: "OPEN" | "CLOSED" }>(`/dev/prs/${repo}/${number}/state`, { action, comment: comment.trim() || undefined });
      setAsking(false);
      setComment("");
      onDone(r.state);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  if (state === "MERGED") return <span className="chip" title="Merged pull requests cannot be closed or reopened">merged</span>;
  if (state === "CLOSED") {
    return (
      <span className="dev-pr-state">
        {!compact && <span className="chip" data-testid="dev-pr-closed">closed</span>}
        <button className="btn small" disabled={busy} onClick={(e) => { e.stopPropagation(); void run("reopen"); }} data-testid="dev-pr-reopen">{busy ? "…" : "Reopen"}</button>
        {err && <span className="error-text small">{err}</span>}
      </span>
    );
  }
  return (
    <span className="dev-pr-state" onClick={(e) => e.stopPropagation()}>
      {!asking ? (
        <button className={`btn small ${compact ? "" : "danger"}`} onClick={() => setAsking(true)} title={`Close ${repo}#${number} on GitHub (it can be reopened)`} data-testid="dev-pr-close">
          {compact ? "Close" : "Close pull request…"}
        </button>
      ) : (
        <span className="dev-pr-close-box" data-testid="dev-pr-close-box">
          <textarea autoFocus rows={2} value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Comment (optional), e.g. why it is closed" data-testid="dev-pr-close-comment"
            onKeyDown={(e) => { if (e.key === "Escape") setAsking(false); }} />
          <span className="row nowrap">
            <button className="btn small danger" disabled={busy} onClick={() => void run("close")} data-testid="dev-pr-close-confirm">{busy ? "Closing…" : `Close #${number}`}</button>
            <button className="btn small" onClick={() => setAsking(false)}>Cancel</button>
          </span>
          <span className="muted small">The branch and your workspaces stay; you can reopen it.</span>
        </span>
      )}
      {err && <span className="error-text small" role="alert">{err}</span>}
    </span>
  );
}
