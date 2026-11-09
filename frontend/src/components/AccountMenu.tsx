import { useEffect, useRef, useState } from "react";
import { ApiError, get } from "../api";

interface SetupInfo {
  enabled: boolean;
  configured_in: "config.toml" | "setup" | null;
  allowed_users: string[];
  saved: { app_url: string | null; slug: string | null; created_at: string | null } | null;
  action: string;
  manifest: string;
}

/** Top-right account menu: who is signed in, one-click "Continue with GitHub" setup, Sign out. */
export default function AccountMenu({ identity, onSignOut, onNotify }: { identity: string | null | undefined; onSignOut: () => void; onNotify: (m: string) => void }) {
  const [open, setOpen] = useState(false);
  const [info, setInfo] = useState<SetupInfo | null>(null);
  const box = useRef<HTMLSpanElement>(null);
  const [via, who] = identity?.includes(":") ? [identity.split(":")[0], identity.slice(identity.indexOf(":") + 1)] : ["token", ""];
  const provider = via === "github" ? "GitHub" : via === "google" ? "Google" : "";

  useEffect(() => {
    if (!open) return;
    void get<SetupInfo>("/auth/github/setup").then(setInfo).catch((e) => onNotify((e as ApiError).message));
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // GitHub only accepts the manifest as a form POST from the browser
  const setUpGithub = async () => {
    const i = await get<SetupInfo>("/auth/github/setup"); // fresh state (and its cookie)
    const form = document.createElement("form");
    form.method = "POST";
    form.action = i.action;
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "manifest";
    input.value = i.manifest;
    form.appendChild(input);
    document.body.appendChild(form);
    form.submit();
  };

  return (
    <span className="ws-menu-anchor account-anchor" ref={box}>
      <button className={`btn small toggle ${open ? "on" : ""}`} onClick={() => setOpen((v) => !v)} data-testid="account-menu" aria-expanded={open} title={provider ? `Signed in with ${provider} as ${who}` : "Signed in with the access token"}>
        <span aria-hidden="true">👤</span> {who || "Account"} <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="ws-menu account-menu" role="menu" data-testid="account-menu-list">
          <div className="account-who muted small">{provider ? `Signed in with ${provider} as ${who}` : "Signed in with the access token"}</div>
          <div className="ws-menu-sep" />
          {info && !info.enabled && (
            <button className="ws-menu-new" onClick={() => void setUpGithub()} data-testid="github-setup" title="Creates a private GitHub App for this server on github.com (you confirm there); only your GitHub account will be allowed to sign in">
              Set up “Continue with GitHub”…
            </button>
          )}
          {info?.enabled && (
            <div className="account-status small" data-testid="github-setup-status">
              ✓ Continue with GitHub is on{info.allowed_users.length ? ` for ${info.allowed_users.join(", ")}` : ""}
              {info.saved?.app_url && (
                <> · <a href={`${info.saved.app_url.replace("/apps/", "/settings/apps/")}`} target="_blank" rel="noreferrer">GitHub App settings</a></>
              )}
            </div>
          )}
          <div className="ws-menu-sep" />
          <button className="ws-menu-new" onClick={onSignOut} data-testid="sign-out">{provider ? `Sign out ${who}` : "Sign out"}</button>
        </div>
      )}
    </span>
  );
}
