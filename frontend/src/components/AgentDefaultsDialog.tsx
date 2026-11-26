import { useEffect, useState } from "react";
import { ApiError, ModelList, get, put } from "../api";
import { forgetModelLists } from "./ModelChoice";

interface Defaults { provider: string | null; model: string | null; variant: string | null; effective_provider: string | null; configured_provider: string | null }

/** Default agent and model for every new agent session (discussions, chats, PR Development, reviews, previews …).
 * Sessions that exist keep their agent and model. */
export default function AgentDefaultsDialog({ onClose }: { onClose: () => void }) {
  const [d, setD] = useState<Defaults | null>(null);
  const [provs, setProvs] = useState<{ name: string; available: boolean }[]>([]);
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [variant, setVariant] = useState("");
  const [list, setList] = useState<ModelList | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    get<Defaults>("/agent-defaults").then((r) => { setD(r); setProvider(r.provider ?? ""); setModel(r.model ?? ""); setVariant(r.variant ?? ""); })
      .catch((e) => setErr((e as ApiError).message));
    get<{ providers: Record<string, { available: boolean }> }>("/bootstrap")
      .then((b) => setProvs(Object.entries(b.providers ?? {}).map(([name, c]) => ({ name, available: c.available })))).catch(() => undefined);
  }, []);
  useEffect(() => {
    setList(null);
    if (!provider) return;
    let alive = true;
    get<ModelList>(`/providers/${provider}/models`).then((l) => { if (alive) setList(l); }).catch(() => undefined);
    return () => { alive = false; };
  }, [provider]);
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      await put("/agent-defaults", { provider: provider || null, model: provider ? model || null : null, variant: provider ? variant || null : null });
      forgetModelLists();
      onClose();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const ids = list?.models ?? [];
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Default agent and model" data-testid="agent-defaults-dialog">
        <div className="modal-head"><span>Default agent and model</span><button className="icon-btn" onClick={onClose}>×</button></div>
        <div className="muted small">Used for every <b>new</b> agent session: wiki discussions and chats, Local files and PR Development chats,
          reviews, previews, commit messages … Sessions that already exist keep the agent and model they use; you can still choose another
          per question or per session.</div>
        <label className="dev-field"><span className="label">Agent</span>
          <select value={provider} onChange={(e) => { setProvider(e.target.value); setModel(""); setVariant(""); }} data-testid="agent-defaults-provider">
            <option value="">From the configuration{d?.configured_provider ? ` (${d.configured_provider})` : " (none)"}</option>
            {provs.map((p) => <option key={p.name} value={p.name} disabled={!p.available}>{p.name}{p.available ? "" : " (not available)"}</option>)}
          </select>
        </label>
        {provider && (
          <>
            <label className="dev-field"><span className="label">Model</span>
              <input className="mono" list="agent-defaults-models" value={model} onChange={(e) => setModel(e.target.value.trim())}
                placeholder={list?.default_model && !model ? `the agent's own default (${list.default_model})` : "the agent's own default"} data-testid="agent-defaults-model" />
              <datalist id="agent-defaults-models">{ids.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}</datalist>
            </label>
            {list && list.variants.length > 0 && (
              <label className="dev-field"><span className="label">Reasoning level</span>
                <select value={variant} onChange={(e) => setVariant(e.target.value)} data-testid="agent-defaults-variant">
                  <option value="">The agent's own default</option>
                  {list.variants.map((v) => <option key={v} value={v}>{v}</option>)}
                </select>
              </label>
            )}
          </>
        )}
        {err && <div className="error-text" role="alert">{err}</div>}
        <div className="row"><span className="spacer" /><button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy} onClick={() => void save()} data-testid="agent-defaults-save">Save</button></div>
      </div>
    </div>
  );
}
