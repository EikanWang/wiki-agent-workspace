import { useEffect, useState } from "react";
import { AgentSession, ModelList, get } from "../api";

/** A model / reasoning level chosen for one discussion; null = what the session (or the provider) uses by default. */
export interface ModelChoiceValue { provider?: string; model?: string; variant?: string }

const cache = new Map<string, Promise<ModelList>>();
function models(provider: string): Promise<ModelList> {
  let p = cache.get(provider);
  if (!p) {
    p = get<ModelList>(`/providers/${provider}/models`);
    p.catch(() => cache.delete(provider));
    cache.set(provider, p);
  }
  return p;
}

let provList: Promise<{ names: string[]; dflt: string | null }> | null = null;
function availableProviders() {
  if (!provList) {
    provList = get<{ providers: Record<string, { available: boolean }>; default_provider: string | null }>("/bootstrap")
      .then((b) => ({ names: Object.entries(b.providers ?? {}).filter(([, c]) => c.available).map(([n]) => n), dflt: b.default_provider }));
    provList.catch(() => { provList = null; });
  }
  return provList;
}

/**
 * Model and reasoning level for a question, in every agent dialog. It starts on the default — the session's model
 * (or the provider's configured default) — and shows what that is; choosing another runs the discussion in a
 * session with that model (a new one unless the session already uses it), so other discussions keep theirs.
 */
export default function ModelChoice({ value, onChange, session, provider, providers }: {
  value: ModelChoiceValue | null;
  onChange: (v: ModelChoiceValue | null) => void;
  /** the session the question would use by default (its model is the default shown) */
  session?: AgentSession | null;
  /** the default provider when there is no session yet */
  provider?: string | null;
  /** providers to choose from (more than one: a provider menu) */
  providers?: string[];
}) {
  const [avail, setAvail] = useState<{ names: string[]; dflt: string | null } | null>(null);
  useEffect(() => { if (!providers) void availableProviders().then(setAvail).catch(() => undefined); }, [providers]);
  const provs = providers ?? avail?.names;
  const baseProvider = session?.provider ?? provider ?? avail?.dflt ?? provs?.[0] ?? null;
  const prov = value?.provider ?? baseProvider;
  const [list, setList] = useState<ModelList | null>(null);
  const [custom, setCustom] = useState(false);
  useEffect(() => {
    setList(null);
    if (!prov) return;
    let alive = true;
    models(prov).then((l) => { if (alive) setList(l); }).catch(() => undefined);
    return () => { alive = false; };
  }, [prov]);
  if (!prov) return null;
  // what "default" means here: the session's model, else the provider's configured one
  const sameProv = !session || session.provider === prov;
  const dModel = (sameProv ? session?.model : null) ?? list?.default_model ?? null;
  const dVariant = (sameProv ? session?.variant : null) ?? list?.default_variant ?? null;
  const set = (patch: ModelChoiceValue) => {
    const next: ModelChoiceValue = { provider: prov, model: value?.model, variant: value?.variant, ...patch };
    const isDefault = (next.provider ?? baseProvider) === baseProvider && !next.model && !next.variant;
    onChange(isDefault ? null : { provider: next.provider, model: next.model || undefined, variant: next.variant || undefined });
  };
  const ids = list?.models ?? [];
  const model = value?.model ?? "";
  const known = !model || ids.some((m) => m.id === model);
  return (
    <span className="model-choice" data-testid="model-choice" title="Model and reasoning level for this question (default: the session's)">
      {provs && provs.length > 1 && (
        <select value={prov} onChange={(e) => set({ provider: e.target.value, model: undefined, variant: undefined })} data-testid="model-choice-provider">
          {provs.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
      )}
      {custom || !known ? (
        <input value={model} placeholder={dModel ?? "model id"} onChange={(e) => set({ model: e.target.value.trim() })} onBlur={() => !model && setCustom(false)}
          className="mono" data-testid="model-choice-input" />
      ) : (
        <select value={model} onChange={(e) => (e.target.value === "__custom__" ? setCustom(true) : set({ model: e.target.value }))} data-testid="model-choice-model">
          <option value="">Default{dModel ? ` (${dModel.split("/").pop()})` : ""}</option>
          {ids.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
          <option value="__custom__">Other…</option>
        </select>
      )}
      {list && list.variants.length > 0 && (
        <select value={value?.variant ?? ""} onChange={(e) => set({ variant: e.target.value })} title="Reasoning level" data-testid="model-choice-effort">
          <option value="">{dVariant ? `Effort: default (${dVariant})` : "Effort: default"}</option>
          {list.variants.map((v) => <option key={v} value={v}>Effort: {v}</option>)}
        </select>
      )}
      {value && <button className="link-btn small" onClick={() => { setCustom(false); onChange(null); }} title="Back to the default" data-testid="model-choice-reset">default</button>}
    </span>
  );
}
