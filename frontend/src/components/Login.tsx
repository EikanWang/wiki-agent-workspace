import { useEffect, useState } from "react";
import { ApiError, get, post, setCsrf } from "../api";

const GITHUB_ERRORS: Record<string, string> = {
  github_not_allowed: "That GitHub account is not allowed to use this server. Ask the owner to add it to [auth.github] allowed_users.",
  github_cancelled: "GitHub sign-in was cancelled.",
  github_state: "The GitHub sign-in expired or did not start here. Please try again.",
  github_exchange: "GitHub did not confirm the sign-in (the OAuth app's client id/secret or callback URL may be wrong). Please try again.",
  github_unreachable: "The server could not reach GitHub (network or proxy). Use the access token, or try again later.",
  github_disabled: "Sign in with GitHub is not set up on this server.",
  github_error: "GitHub reported an error during sign-in. Please try again.",
};

function GitHubMark() {
  return (
    <svg viewBox="0 0 16 16" width="18" height="18" aria-hidden="true" fill="currentColor">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  );
}

export default function Login({ onDone }: { onDone: () => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [github, setGithub] = useState(false);
  useEffect(() => {
    void get<{ github: boolean }>("/auth/options").then((o) => setGithub(o.github)).catch(() => undefined);
    // coming back from GitHub with an error: explain it, then clean the address bar
    const p = new URLSearchParams(window.location.search);
    const code = p.get("login_error");
    if (code) {
      setError(GITHUB_ERRORS[code] ?? `Sign-in failed (${code}).`);
      p.delete("login_error");
      window.history.replaceState(null, "", window.location.pathname + (p.toString() ? `?${p}` : "") + window.location.hash);
    }
  }, []);

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
            <div className="brand-sub">{github ? "Sign in with GitHub or the access token" : "Enter the access token once"} (the login lasts 30 days)</div>
          </div>
        </div>
        {github && (
          <>
            <a className="btn github-btn" href="/api/v1/auth/github/start" data-testid="login-github">
              <GitHubMark /> Sign in with GitHub
            </a>
            <div className="login-or"><span>or</span></div>
          </>
        )}
        <label className="field">
          <span>Access token</span>
          <input
            type="password"
            autoFocus={!github}
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
        <p className="hint">The token is stored only in the server user's config.toml (mode 0600). To show it again run <code>scripts/setup.sh --show-token</code>.</p>
      </form>
    </div>
  );
}
