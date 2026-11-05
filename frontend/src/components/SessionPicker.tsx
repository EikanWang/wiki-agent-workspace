import { useEffect, useState } from "react";
import { AgentSession, ApiError, ProviderSession, SessionList, get, post, put } from "../api";
import { SessionChip, statusLabel } from "./DiscussionPanel";

export default function SessionPicker({
  docId,
  defaultSessionId,
  sessions,
  defaultProvider,
  onChanged,
  onClose,
}: {
  docId: string;
  defaultSessionId: string | null;
  sessions: AgentSession[];
  defaultProvider: string | null;
  onChanged: () => Promise<void>;
  onClose: () => void;
}) {
  const [list, setList] = useState<SessionList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [manualId, setManualId] = useState("");
  const [manualProvider, setManualProvider] = useState(defaultProvider ?? "opencode");

  const load = async () => {
    try {
      setList(await get<SessionList>("/agent-sessions"));
    } catch (e) {
      setError((e as ApiError).message);
    }
  };
  useEffect(() => {
    void load();
  }, []);

  const setDefault = async (sid: string | null) => {
    setBusy(true);
    setError(null);
    try {
      await put(`/documents/${docId}/default-session`, { session_id: sid });
      await onChanged();
      onClose();
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const createNew = async (provider: string) => {
    setBusy(true);
    setError(null);
    try {
      const s = await post<AgentSession>("/agent-sessions", { provider, display_name: `Wiki session ${new Date().toLocaleString()}` });
      await put(`/documents/${docId}/default-session`, { session_id: s.id });
      await onChanged();
      onClose();
    } catch (e) {
      const ae = e as ApiError;
      setError(ae.code === "PROVIDER_UNAVAILABLE" ? `${provider} unavailable：${ae.message}` : ae.message);
    } finally {
      setBusy(false);
    }
  };

  const link = async (provider: string, nativeId: string, title?: string | null) => {
    setBusy(true);
    setError(null);
    try {
      const s = await post<AgentSession>("/agent-sessions", { provider, native_id: nativeId, display_name: title ?? undefined });
      await put(`/documents/${docId}/default-session`, { session_id: s.id });
      await onChanged();
      onClose();
    } catch (e) {
      const ae = e as ApiError;
      if (ae.code === "SESSION_NOT_FOUND") setError(`${provider} does not report that session id; nothing was linked.`);
      else if (ae.code === "PROVIDER_CANNOT_CONFIRM") setError(ae.message);
      else setError(ae.message);
    } finally {
      setBusy(false);
    }
  };

  const providers = list ? Object.keys(list.capabilities) : [];
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} data-testid="session-picker">
        <div className="modal-head">
          <span>Document session</span>
          <button className="icon-btn" onClick={onClose}>×</button>
        </div>
        <div className="muted small">New discussions default to the session selected here; existing discussions and their history are unchanged.</div>
        <section>
          <div className="label">Current</div>
          <SessionChip session={sessions.find((s) => s.id === defaultSessionId)} />
        </section>
        <section>
          <div className="label">New session</div>
          <div className="row">
            {(providers.length ? providers : ["opencode", "codex"]).map((p) => {
              const cap = list?.capabilities[p];
              return (
                <button key={p} className="btn" disabled={busy || (cap ? !cap.available : false)} onClick={() => void createNew(p)} title={cap?.error ?? cap?.version ?? ""} data-testid={`new-session-${p}`}>
                  ＋ {p}{cap && !cap.available ? " (unavailable)" : ""}
                </button>
              );
            })}
          </div>
          {list && Object.entries(list.errors).map(([p, e]) => <div key={p} className="warn small">{p}: {e}</div>)}
        </section>
        <section>
          <div className="label">Sessions in this app</div>
          <div className="list">
            {sessions.filter((s) => s.origin !== "preview").map((s) => (
              <button key={s.id} className={`list-item ${s.id === defaultSessionId ? "selected" : ""}`} disabled={busy} onClick={() => void setDefault(s.id)}>
                <span>{s.provider} · {s.display_name ?? "session"}</span>
                <span className="muted small">{s.native_id ? `#${s.short_id}` : statusLabel(s.status)}{s.status !== "ready" && s.native_id ? ` · ${statusLabel(s.status)}` : ""}</span>
              </button>
            ))}
            {sessions.length === 0 && <div className="muted small">No sessions yet.</div>}
          </div>
        </section>
        <section>
          <div className="label">Link an existing provider session (e.g. the terminal conversation that created this wiki)</div>
          <div className="list tall">
            {list?.provider_sessions.map((ps: ProviderSession) => (
              <button key={ps.provider + ps.native_id} className="list-item" disabled={busy || ps.linked} onClick={() => void link(ps.provider, ps.native_id, ps.title)} title={ps.directory ?? ""}>
                <span>{ps.provider} · {ps.title ?? ps.native_id}</span>
                <span className="muted small">{ps.native_id.slice(-8)} {ps.updated_at ? `· ${ps.updated_at.slice(0, 16)}` : ""} {ps.linked ? "· linked" : ""}</span>
              </button>
            ))}
            {list && list.provider_sessions.length === 0 && <div className="muted small">The provider returned no session list (listing may be unsupported, or you are not logged in).</div>}
          </div>
          <div className="row">
            <select value={manualProvider} onChange={(e) => setManualProvider(e.target.value)}>
              <option value="opencode">opencode</option>
              <option value="codex">codex</option>
            </select>
            <input value={manualId} onChange={(e) => setManualId(e.target.value)} placeholder="Enter a provider session id" />
            <button className="btn" disabled={busy || !manualId.trim()} onClick={() => void link(manualProvider, manualId.trim())}>Link</button>
          </div>
        </section>
        {error && <div className="error-text" role="alert">{error}</div>}
      </div>
    </div>
  );
}
