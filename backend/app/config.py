"""Configuration loading for Wiki Agent Workspace.

One TOML file (default ``~/.config/wiki-agent-workspace/config.toml``) holds all
settings. Secrets (access token, server secret) live only in that mode-0600 file or
in environment variables; they are never logged.
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

ENV_CONFIG = "WAW_CONFIG"
ENV_ACCESS_TOKEN = "WAW_ACCESS_TOKEN"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "wiki-agent-workspace" / "config.toml"
DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "wiki-agent-workspace"


@dataclass
class ProviderConfig:
    enabled: bool = True
    bin: str | None = None          # executable path; default resolved with shutil.which
    # opencode only: "cli" uses `opencode run --format json`; "server" talks to a
    # known loopback `opencode serve` instance (server_url) via its HTTP API.
    mode: str = "cli"
    server_url: str | None = None
    # codex only: persistent sandbox used for app-driven turns (DESIGN §8).
    sandbox: str = "workspace-write"
    extra_args: list[str] = field(default_factory=list)
    profile: str | None = None       # codex --profile / opencode --agent


@dataclass
class Settings:
    config_path: Path
    bind: str = "127.0.0.1"
    port: int = 8765
    state_dir: Path = DEFAULT_STATE_DIR
    workspace_root: Path = field(default_factory=Path.cwd)
    # extra root folders opened in the same window: [{"root": "/path", "name": "optional"}]
    workspaces: list[dict[str, str]] = field(default_factory=list)
    default_provider: str | None = None
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    access_token: str = ""
    server_secret: str = ""
    cookie_secure: bool = False
    allowed_origins: list[str] = field(default_factory=list)
    trusted_proxies: list[str] = field(default_factory=list)
    gateway_auth: bool = False       # explicitly configured already-authenticated gateway
    gateway_user_header: str = ""     # header the gateway sets, required when gateway_auth
    max_document_bytes: int = 20 * 1024 * 1024
    # [auth.github] — "Sign in with GitHub" (OAuth app); enabled when client id, secret and allowed users are set
    github_client_id: str = ""
    github_client_secret: str = ""
    github_allowed_users: list[str] = field(default_factory=list)
    github_callback_url: str = ""  # default: <this server's address>/api/v1/auth/github/callback
    # endpoints (only changed by tests)
    github_oauth_base: str = "https://github.com"
    github_api_base: str = "https://api.github.com"
    # [auth.google] — "Continue with Google" (OpenID Connect); enabled when client id, secret and allowed emails are set
    google_client_id: str = ""
    google_client_secret: str = ""
    google_allowed_emails: list[str] = field(default_factory=list)
    google_callback_url: str = ""
    google_auth_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_token_url: str = "https://oauth2.googleapis.com/token"
    google_userinfo_url: str = "https://openidconnect.googleapis.com/v1/userinfo"
    tmux_history_limit: int = 10000
    # [office] — Layout view of Word / PowerPoint / Visio files, rendered to PDF by LibreOffice
    office_soffice: str | None = None  # path to soffice; default: found on PATH
    office_max_parallel: int = 2
    office_timeout: int = 180
    office_max_bytes: int = 200 * 1024 * 1024
    office_cache_mb: int = 2048
    shell: str | None = None
    session_days: int = 30
    frontend_dist: Path | None = None
    log_level: str = "INFO"

    # ------------------------------------------------------------------ helpers
    @property
    def db_path(self) -> Path:
        return self.state_dir / "workspace.sqlite3"

    @property
    def snapshots_dir(self) -> Path:
        return self.state_dir / "snapshots"

    @property
    def tmux_socket_name(self) -> str:
        """App-owned tmux socket name. Stored in the state directory (``tmux-socket``) so it stays
        the same when the folder is moved or copied, and differs between two state directories so two
        instances never see or kill each other's sessions."""
        import secrets as _secrets

        marker = self.state_dir / "tmux-socket"
        try:
            name = marker.read_text("utf-8").strip()
            if name.startswith("waw-") and name.isascii() and len(name) <= 40:
                return name
        except OSError:
            pass
        name = "waw-" + _secrets.token_hex(4)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            marker.write_text(name + "\n", encoding="utf-8")
        except OSError:
            # read-only or missing state dir: fall back to a path-derived name
            import hashlib

            return "waw-" + hashlib.sha1(str(self.state_dir.resolve()).encode("utf-8")).hexdigest()[:8]
        return name

    @property
    def tmux_conf_path(self) -> Path:
        return self.state_dir / "tmux.conf"

    def provider(self, name: str) -> ProviderConfig:
        return self.providers.get(name) or ProviderConfig()

    def to_toml_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "server": {
                "bind": self.bind,
                "port": self.port,
                "state_dir": str(self.state_dir),
                "cookie_secure": self.cookie_secure,
                "allowed_origins": self.allowed_origins,
                "trusted_proxies": self.trusted_proxies,
                "gateway_auth": self.gateway_auth,
                "gateway_user_header": self.gateway_user_header,
                "log_level": self.log_level,
                "session_days": self.session_days,
            },
            "workspace": {
                "root": str(self.workspace_root),
                "max_document_bytes": self.max_document_bytes,
            },
            "terminal": {
                "tmux_history_limit": self.tmux_history_limit,
            },
            "office": {
                "max_parallel": self.office_max_parallel,
                "timeout": self.office_timeout,
                "max_bytes": self.office_max_bytes,
                "cache_mb": self.office_cache_mb,
            },
            "auth": {
                "access_token": self.access_token,
                "server_secret": self.server_secret,
            },
            "agents": {"default_provider": self.default_provider or ""},
        }
        if self.shell:
            d["terminal"]["shell"] = self.shell
        if self.office_soffice:
            d["office"]["soffice"] = self.office_soffice
        if self.github_client_id or self.github_allowed_users:
            gh: dict[str, Any] = {"client_id": self.github_client_id, "allowed_users": self.github_allowed_users}
            if self.github_client_secret:
                gh["client_secret"] = self.github_client_secret
            if self.github_callback_url:
                gh["callback_url"] = self.github_callback_url
            d["auth"]["github"] = gh
        if self.google_client_id or self.google_allowed_emails:
            go: dict[str, Any] = {"client_id": self.google_client_id, "allowed_emails": self.google_allowed_emails}
            if self.google_client_secret:
                go["client_secret"] = self.google_client_secret
            if self.google_callback_url:
                go["callback_url"] = self.google_callback_url
            d["auth"]["google"] = go
        if self.workspaces:
            d["workspaces"] = [{k: v for k, v in w.items() if v} for w in self.workspaces]
        if self.frontend_dist:
            d["server"]["frontend_dist"] = str(self.frontend_dist)
        for name, p in self.providers.items():
            entry: dict[str, Any] = {"enabled": p.enabled, "mode": p.mode, "sandbox": p.sandbox,
                                     "extra_args": p.extra_args}
            if p.bin:
                entry["bin"] = p.bin
            if p.server_url:
                entry["server_url"] = p.server_url
            if p.profile:
                entry["profile"] = p.profile
            d["agents"][name] = entry
        return d


def _provider_from(d: dict[str, Any]) -> ProviderConfig:
    return ProviderConfig(
        enabled=bool(d.get("enabled", True)),
        bin=d.get("bin") or None,
        mode=str(d.get("mode", "cli")),
        server_url=d.get("server_url") or None,
        sandbox=str(d.get("sandbox", "workspace-write")),
        extra_args=list(d.get("extra_args", []) or []),
        profile=d.get("profile") or None,
    )


def detect_default_provider(providers: dict[str, ProviderConfig]) -> str | None:
    """DESIGN §15: opencode if detected, otherwise codex if detected, otherwise unset."""
    for name in ("opencode", "codex"):
        p = providers.get(name, ProviderConfig())
        if not p.enabled:
            continue
        if shutil.which(p.bin or name):
            return name
    return None


def load_settings(path: Path | None = None, env: dict[str, str] | None = None) -> Settings:
    env = env if env is not None else os.environ
    cfg_path = Path(env.get(ENV_CONFIG) or path or DEFAULT_CONFIG_PATH).expanduser()
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh)
    server = raw.get("server", {})
    ws = raw.get("workspace", {})
    term = raw.get("terminal", {})
    office = raw.get("office", {}) or {}
    auth = raw.get("auth", {})
    agents = raw.get("agents", {})

    providers: dict[str, ProviderConfig] = {}
    for name in ("opencode", "codex"):
        providers[name] = _provider_from(agents.get(name, {}) or {})

    default_provider = agents.get("default_provider") or None
    if not default_provider:
        default_provider = detect_default_provider(providers)

    access_token = env.get(ENV_ACCESS_TOKEN) or auth.get("access_token") or ""
    server_secret = auth.get("server_secret") or env.get("WAW_SERVER_SECRET") or ""
    gh = auth.get("github", {}) or {}
    go = auth.get("google", {}) or {}

    workspace_root = Path(env.get("WAW_WORKSPACE") or ws.get("root") or os.getcwd()).expanduser()
    state_dir = Path(env.get("WAW_STATE_DIR") or server.get("state_dir") or DEFAULT_STATE_DIR).expanduser()
    frontend_dist = env.get("WAW_FRONTEND_DIST") or server.get("frontend_dist") or None

    extra_roots = [
        {"root": str(Path(str(w.get("root"))).expanduser()), "name": str(w.get("name") or "")}
        for w in (raw.get("workspaces") or []) if isinstance(w, dict) and w.get("root")
    ]

    return Settings(
        config_path=cfg_path,
        workspaces=extra_roots,
        bind=str(env.get("WAW_BIND") or server.get("bind", "127.0.0.1")),
        port=int(env.get("WAW_PORT") or server.get("port", 8765)),
        state_dir=state_dir,
        workspace_root=workspace_root,
        default_provider=default_provider,
        providers=providers,
        access_token=access_token,
        server_secret=server_secret,
        cookie_secure=bool(server.get("cookie_secure", False)),
        allowed_origins=list(server.get("allowed_origins", []) or []),
        trusted_proxies=list(server.get("trusted_proxies", []) or []),
        gateway_auth=bool(server.get("gateway_auth", False)),
        gateway_user_header=str(server.get("gateway_user_header", "")),
        max_document_bytes=int(ws.get("max_document_bytes", 20 * 1024 * 1024)),
        github_client_id=str(gh.get("client_id", "") or ""),
        github_client_secret=str(env.get("WAW_GITHUB_CLIENT_SECRET") or gh.get("client_secret", "") or ""),
        github_allowed_users=[str(u).strip() for u in (gh.get("allowed_users") or []) if str(u).strip()],
        github_callback_url=str(gh.get("callback_url", "") or ""),
        google_client_id=str(go.get("client_id", "") or ""),
        google_client_secret=str(env.get("WAW_GOOGLE_CLIENT_SECRET") or go.get("client_secret", "") or ""),
        google_allowed_emails=[str(u).strip().lower() for u in (go.get("allowed_emails") or []) if str(u).strip()],
        google_callback_url=str(go.get("callback_url", "") or ""),
        tmux_history_limit=int(term.get("tmux_history_limit", 10000)),
        office_soffice=str(office.get("soffice") or "") or None,
        office_max_parallel=int(office.get("max_parallel", 2)),
        office_timeout=int(office.get("timeout", 180)),
        office_max_bytes=int(office.get("max_bytes", 200 * 1024 * 1024)),
        office_cache_mb=int(office.get("cache_mb", 2048)),
        shell=term.get("shell") or None,
        session_days=int(server.get("session_days", 30)),
        frontend_dist=Path(frontend_dist).expanduser() if frontend_dist else None,
        log_level=str(server.get("log_level", "INFO")),
    )


def write_settings(settings: Settings) -> None:
    settings.config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = settings.config_path.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        tomli_w.dump(settings.to_toml_dict(), fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, settings.config_path)
    os.chmod(settings.config_path, 0o600)


def ensure_secrets(settings: Settings) -> tuple[Settings, bool]:
    """Generate the access token and server secret on first setup.

    Returns (settings, token_was_generated). The token is printed exactly once by the
    setup script, never by the server.
    """
    generated = False
    if not settings.access_token:
        settings.access_token = secrets.token_urlsafe(32)
        generated = True
    if not settings.server_secret:
        settings.server_secret = secrets.token_hex(32)
        generated = True
    return settings, generated


def ensure_state_dir(settings: Settings) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.snapshots_dir.mkdir(parents=True, exist_ok=True, mode=0o700)


def print_config_warning(msg: str) -> None:
    print(f"[wiki-agent-workspace] {msg}", file=sys.stderr)
