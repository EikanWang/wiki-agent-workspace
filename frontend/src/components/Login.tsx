import { useState } from "react";
import { ApiError, post, setCsrf } from "../api";

export default function Login({ onDone }: { onDone: () => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await post<{ csrf_token: string }>("/auth/login", { token: token.trim() });
      setCsrf(r.csrf_token);
      onDone();
    } catch (err) {
      const ae = err as ApiError;
      setError(ae.status === 401 ? "The access token is incorrect." : ae.message || "Login failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={submit}>
        <div className="brand">
          <span className="brand-mark">W</span>
          <div>
            <div className="brand-title">Wiki Agent Workspace</div>
            <div className="brand-sub">Enter the access token once (the login lasts 30 days)</div>
          </div>
        </div>
        <label className="field">
          <span>Access token</span>
          <input
            type="password"
            autoFocus
            autoComplete="current-password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Token printed by scripts/setup.sh"
            data-testid="login-token"
          />
        </label>
        {error && <div className="error-text" role="alert">{error}</div>}
        <button className="btn primary" type="submit" disabled={busy || !token.trim()} data-testid="login-submit">
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="hint">The token is stored only in the server user's config.toml (mode 0600). To show it again run <code>scripts/setup.sh --show-token</code>。</p>
      </form>
    </div>
  );
}
