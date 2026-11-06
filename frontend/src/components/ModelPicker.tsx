import { useEffect, useState } from "react";
import { AgentSession, ApiError, ModelList, get, put } from "../api";

/** Choose the model (and reasoning variant) used for future app-driven turns of one agent session. */
export default function ModelPicker({ session, onChanged, compact = false }: { session: AgentSession; onChanged: (s: AgentSession) => void; compact?: boolean }) {
  const [list, setList] = useState<ModelList | null>(null);
  const [model, setModel] = useState<string>(session.model ?? "");
  const [variant, setVariant] = useState<string>(session.variant ?? "");
  const [custom, setCustom] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    get<ModelList>(`/providers/${session.provider}/models`).then(setList).catch((e) => setError((e as ApiError).message));
  }, [session.provider]);
  useEffect(() => {
    setModel(session.model ?? "");
    setVariant(session.variant ?? "");
  }, [session.id, session.model, session.variant]);

  const save = async (m: string, v: string) => {
    setBusy(true);
    setError(null);
    try {
      const s = await put<AgentSession>(`/agent-sessions/${session.id}/model`, { model: m || null, variant: v || null });
      onChanged(s);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const known = list?.models ?? [];
  const inList = !model || known.some((m) => m.id === model);
  return (
    <div className={`model-picker ${compact ? "compact" : ""}`} data-testid="model-picker">
      <label className="small">
        Model{" "}
        {!custom && inList ? (
          <select
            value={model}
            disabled={busy}
            onChange={(e) => {
              if (e.target.value === "__custom__") {
                setCustom(true);
                return;
              }
              setModel(e.target.value);
              void save(e.target.value, variant);
            }}
            data-testid="model-select"
          >
            <option value="">Provider default</option>
            {known.map((m) => (
              <option key={m.id} value={m.id}>{m.label}</option>
            ))}
            <option value="__custom__">Other… (type an id)</option>
          </select>
        ) : (
          <span className="stepper">
            <input value={model} disabled={busy} placeholder={session.provider === "codex" ? "e.g. gpt-5" : "e.g. provider/model"} onChange={(e) => setModel(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") void save(model.trim(), variant); }} data-testid="model-input" />
            <button className="btn small" disabled={busy} onClick={() => void save(model.trim(), variant)}>Use</button>
            <button className="btn small" disabled={busy} onClick={() => { setCustom(false); setModel(""); void save("", variant); }} title="Back to the provider default">Default</button>
          </span>
        )}
      </label>
      {list && list.variants.length > 0 && (
        <label className="small">
          Effort{" "}
          <select value={variant} disabled={busy} onChange={(e) => { setVariant(e.target.value); void save(model, e.target.value); }} data-testid="variant-select">
            <option value="">default</option>
            {list.variants.map((v) => (
              <option key={v} value={v}>{v}</option>
            ))}
          </select>
        </label>
      )}
      {list && list.note && !compact && <div className="muted small">{list.note}</div>}
      {error && <div className="error-text small">{error}</div>}
    </div>
  );
}
