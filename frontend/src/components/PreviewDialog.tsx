import { useEffect, useState } from "react";
import { AgentSession, ApiError, Capabilities, Preview, get, post } from "../api";
import DiffView from "./DiffView";
import MarkdownView from "./MarkdownView";
import { useJobStream } from "./DiscussionPanel";

export default function PreviewDialog({
  docId,
  docPath,
  baseSha,
  sessions,
  defaultSessionId,
  providers,
  onApplied,
  onClose,
}: {
  docId: string;
  docPath: string;
  baseSha: string;
  sessions: AgentSession[];
  defaultSessionId: string | null;
  providers: Record<string, Capabilities>;
  onApplied: (sha: string) => void;
  onClose: () => void;
}) {
  const [instruction, setInstruction] = useState("");
  const [sessionChoice, setSessionChoice] = useState<string>(defaultSessionId ?? "__default__");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const job = useJobStream(preview?.job_id ?? null, () => void refresh());

  const refresh = async () => {
    if (!preview) return;
    try {
      const p = await get<Preview>(`/previews/${preview.preview_id}`);
      setPreview(p);
    } catch (e) {
      setError((e as ApiError).message);
    }
  };

  useEffect(() => {
    if (!preview || preview.status !== "pending") return;
    const t = window.setInterval(() => void refresh(), 1500);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preview?.preview_id, preview?.status]);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await post<{ preview_id: string; job_id: string; base_sha256: string }>(`/documents/${docId}/previews`, {
        instruction: instruction.trim(),
        base_sha256: baseSha,
        session_id: sessionChoice === "__default__" ? null : sessionChoice,
      });
      setPreview({ preview_id: r.preview_id, document_id: docId, job_id: r.job_id, status: "pending", base_sha256: r.base_sha256, proposed_sha256: null, job_status: "queued", job_error: null });
    } catch (e) {
      const ae = e as ApiError;
      setError(ae.code === "PREVIEW_UNAVAILABLE" ? `Preview unavailable: ${ae.message}. Use the direct-edit flow (ask the agent explicitly in a discussion) or the terminal instead.` : ae.message);
    } finally {
      setBusy(false);
    }
  };

  const apply = async () => {
    if (!preview) return;
    setBusy(true);
    setError(null);
    try {
      const r = await post<{ sha256: string }>(`/previews/${preview.preview_id}/apply`, { base_sha256: preview.base_sha256 });
      onApplied(r.sha256);
      onClose();
    } catch (e) {
      const ae = e as ApiError;
      if (ae.code === "DOCUMENT_VERSION_CONFLICT") {
        setError("The file was modified (e.g. in the terminal) after the preview was generated. The preview was not applied and the newer change is preserved. Refresh the document and generate a new preview.");
        void refresh();
      } else setError(ae.message);
    } finally {
      setBusy(false);
    }
  };

  const providerOf = sessionChoice === "__default__" ? sessions.find((s) => s.id === defaultSessionId)?.provider : sessions.find((s) => s.id === sessionChoice)?.provider;
  const caps = providerOf ? providers[providerOf] : undefined;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} data-testid="preview-dialog">
        <div className="modal-head">
          <span>Preview a change (review the diff before applying)</span>
          <button className="icon-btn" onClick={onClose}>×</button>
        </div>
        <div className="muted small">
          The agent edits <code>{docPath}</code> in an isolated temporary copy of the workspace; your worktree is untouched until you click Apply, which verifies the file was not changed externally. Apply never commits or pushes.
          {caps && caps.preview_isolation !== "copy" && <span className="warn"> The selected provider does not support isolated previews.</span>}
        </div>
        {!preview && (
          <>
            <textarea value={instruction} onChange={(e) => setInstruction(e.target.value)} rows={4} placeholder="Describe the change to make…" data-testid="preview-instruction" />
            <div className="row">
              <select value={sessionChoice} onChange={(e) => setSessionChoice(e.target.value)}>
                <option value="__default__">Default provider (new isolated session)</option>
                {sessions.filter((s) => s.origin !== "preview").map((s) => (
                  <option key={s.id} value={s.id}>Provider of {s.provider}</option>
                ))}
              </select>
              <button className="btn primary" disabled={busy || !instruction.trim()} onClick={() => void start()} data-testid="preview-start">Generate preview</button>
            </div>
          </>
        )}
        {preview && (
          <div className="preview-body">
            <div className="row">
              <span className={`chip status ${preview.status}`}>{previewStatusLabel(preview.status)}</span>
              {job && job.status !== "completed" && <span className="muted small">{job.status}</span>}
              {preview.stale && <span className="chip warn">file changed after preview</span>}
            </div>
            {job && job.activity.length > 0 && preview.status === "pending" && (
              <ul className="activity">{job.activity.slice(-5).map((a, i) => <li key={i}>{a}</li>)}</ul>
            )}
            {job?.error && <div className="error-text">{job.error.code}: {job.error.message}</div>}
            {preview.status === "ready" && (preview.identical ? <div className="muted">The agent did not modify this file.</div> : <DiffView unified={preview.unified ?? ""} />)}
            {job?.text && <MarkdownView text={job.text} className="small" />}
            <div className="row">
              {preview.status === "ready" && !preview.identical && !preview.stale && (
                <button className="btn primary" disabled={busy} onClick={() => void apply()} data-testid="preview-apply">Apply to file</button>
              )}
              <button className="btn" onClick={onClose}>{preview.status === "ready" ? "Discard" : "Close"}</button>
            </div>
          </div>
        )}
        {error && <div className="error-text" role="alert">{error}</div>}
      </div>
    </div>
  );
}

function previewStatusLabel(s: string) {
  return { pending: "generating…", ready: "preview ready", applied: "applied", failed: "failed", stale: "stale" }[s] ?? s;
}
