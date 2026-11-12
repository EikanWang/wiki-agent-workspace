"""`wiki-agent-workspace doctor`: check an installation, feature by feature, and say how to fix what is missing.

Each check gives a level — ok, warn (an optional feature is off, or something to improve) or fail (the app cannot
work as configured) — a short message and, when not ok, the command or setting that fixes it. Nothing is changed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import ProviderConfig, Settings

REPO = Path(__file__).resolve().parents[2]


@dataclass
class Check:
    area: str
    name: str
    level: str  # ok | warn | fail
    message: str
    fix: str = ""


def _run(argv: list[str], timeout: float = 15) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def _version(argv: list[str]) -> str:
    rc, out = _run(argv)
    m = re.search(r"\d+(\.\d+)+[\w.-]*", out) if rc == 0 else None
    return m.group(0) if m else (out.splitlines()[0][:60] if rc == 0 and out else "")


def _tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def run_checks(settings: Settings, *, url: str | None = None, quick: bool = False) -> list[Check]:
    """All checks, in the order they are shown. `quick`: skip the slower ones (versions, gh, the running server)."""
    out: list[Check] = []

    def add(area: str, name: str, level: str, message: str, fix: str = "") -> None:
        out.append(Check(area, name, level, message, fix if level != "ok" else ""))

    # ---------------------------------------------------------------- core
    v = sys.version_info
    add("core", "Python", "ok" if v >= (3, 11) else "fail", f"{v.major}.{v.minor}.{v.micro} ({sys.executable})",
        "Python 3.11+ is needed: scripts/install.sh installs one (uv) when the system has none")
    missing = [m for m in ("fastapi", "uvicorn", "sqlalchemy", "alembic", "markdown_it", "watchfiles") if not _importable(m)]
    add("core", "Python packages", "fail" if missing else "ok", f"missing: {', '.join(missing)}" if missing else "installed (requirements.lock)",
        "scripts/setup.sh (re-installs the locked dependencies into .venv)")
    cfg = settings.config_path
    if not cfg.exists():
        add("core", "Configuration", "fail", f"{cfg} does not exist", "scripts/setup.sh --workspace /path/to/your/wiki")
    else:
        mode = stat.S_IMODE(cfg.stat().st_mode)
        add("core", "Configuration", "ok" if mode & 0o077 == 0 else "warn", f"{cfg} (mode {mode:o})",
            f"chmod 600 {cfg} (it holds the access token)")
    add("core", "Access token", "ok" if settings.access_token or os.environ.get("WAW_ACCESS_TOKEN") else "fail",
        "set" if settings.access_token or os.environ.get("WAW_ACCESS_TOKEN") else "not set", "scripts/setup.sh (generates it)")
    roots = [("primary", settings.workspace_root)] + [(w.get("name") or w["root"], Path(w["root"]).expanduser()) for w in settings.workspaces]
    for label, root in roots:
        ok = root.is_dir()
        add("core", f"Wiki folder ({label})", "ok" if ok else "fail", str(root) + ("" if ok else " does not exist"),
            "create it (mkdir -p … && git init …) or point workspace.root / [[workspaces]] at an existing folder")
    sd = settings.state_dir
    writable = sd.is_dir() and os.access(sd, os.W_OK)
    add("core", "State directory", "ok" if writable else "fail", str(sd) + ("" if writable else " is missing or not writable"),
        "scripts/setup.sh creates it; or set server.state_dir to a writable folder")
    dist = settings.frontend_dist or (REPO / "frontend" / "dist")
    index = dist / "index.html"
    if not index.exists():
        add("core", "Browser UI", "fail", f"{index} not built", "scripts/setup.sh (needs Node.js 20+ and npm; builds frontend/dist)")
    else:
        src = REPO / "frontend" / "src"
        newest = max((p.stat().st_mtime for p in src.rglob("*") if p.is_file()), default=0) if src.is_dir() else 0
        stale = newest > index.stat().st_mtime + 1
        add("core", "Browser UI", "warn" if stale else "ok", f"{dist}" + (" (older than frontend/src)" if stale else ""),
            "cd frontend && npm run build (then reload the page)")
    git = shutil.which("git")
    add("core", "Git", "ok" if git else "fail", _version(["git", "--version"]) if git and not quick else (git or "not found"),
        "install Git (apt install git / dnf install git)")
    tmux = shutil.which("tmux")
    if not tmux:
        add("core", "tmux (terminals)", "warn", "not found: terminals are disabled (the rest works)", "install tmux 3.2+ (apt install tmux / dnf install tmux)")
    else:
        tv = _version(["tmux", "-V"]) if not quick else ""
        old = bool(tv) and _tuple(tv) < (3, 2)
        add("core", "tmux (terminals)", "warn" if old else "ok", tv or tmux, "tmux 3.2 or later is recommended")
    try:
        watches = int(Path("/proc/sys/fs/inotify/max_user_watches").read_text().strip())
    except (OSError, ValueError):
        watches = 0
    if watches:
        add("core", "inotify watches", "ok" if watches >= 524288 else "warn", f"fs.inotify.max_user_watches = {watches}",
            "large wikis need more: echo fs.inotify.max_user_watches=1048576 | sudo tee /etc/sysctl.d/90-inotify-watches.conf && sudo sysctl --system")

    # ---------------------------------------------------------------- the running server
    if not quick:
        host = "127.0.0.1" if settings.bind in ("0.0.0.0", "::", "") else settings.bind
        base = url or f"http://{host}:{settings.port}"
        try:
            with urllib.request.urlopen(f"{base}/api/v1/health", timeout=5) as r:  # noqa: S310 - our own server
                body = json.loads(r.read() or b"{}")
            add("server", "Server", "ok" if body.get("status") == "ok" else "warn", f"{base} answers ({body.get('status')})",
                "see the server log (scripts/service.sh logs)")
        except Exception as exc:  # noqa: BLE001
            add("server", "Server", "warn", f"{base} is not answering ({exc.__class__.__name__})",
                "start it: scripts/service.sh start (or scripts/start.sh in a terminal)")

    # ---------------------------------------------------------------- agents
    found_any = False
    for name, label in (("opencode", "OpenCode"), ("codex", "Codex")):
        p = settings.providers.get(name, ProviderConfig())
        exe = shutil.which(p.bin or name) if p.enabled else None
        found_any = found_any or bool(exe)
        if not p.enabled:
            add("agents", label, "warn", "disabled in the configuration", f"[agents.{name}] enabled = true")
            continue
        ver = _version([exe, "--version"]) if exe and not quick else ""
        add("agents", label, "ok" if exe else "warn", f"{exe}" + (f" ({ver})" if ver else "") if exe else "not found on PATH",
            "curl -fsSL https://opencode.ai/install | bash && opencode auth login" if name == "opencode"
            else "npm install -g @openai/codex && codex login")
    dp = settings.default_provider
    if dp:
        p = settings.providers.get(dp, ProviderConfig())
        add("agents", "Default agent", "ok" if shutil.which(p.bin or dp) else "fail", dp,
            f"install {dp}, or set agents.default_provider to an installed one")
    else:
        add("agents", "Default agent", "warn" if found_any else "fail", "not set: discussions and chats are unavailable",
            "install OpenCode or Codex, then scripts/setup.sh (detects it) or set agents.default_provider")
    claude = shutil.which("claude")
    add("agents", "Claude Code (Agents monitor)", "ok" if claude else "warn", claude or "not found: its sessions are not monitored (optional)",
        "npm install -g @anthropic-ai/claude-code (only to monitor / continue Claude Code sessions)")
    if settings.agent_env_script:
        es = Path(settings.agent_env_script).expanduser()
        add("agents", "Agent env script", "ok" if es.is_file() else "fail", str(es) + ("" if es.is_file() else " does not exist"),
            "fix agents.env_script in the configuration (or remove it)")

    # ---------------------------------------------------------------- GitHub (PR Review, PR Development)
    gh = shutil.which("gh")
    login = ""
    if not gh:
        add("github", "GitHub CLI (gh)", "warn", "not found: PR Review and PR Development's pull requests are unavailable",
            "install gh (https://cli.github.com) and run: gh auth login")
    elif quick:
        add("github", "GitHub CLI (gh)", "ok", gh)
    else:
        rc, txt = _run(["gh", "api", "user", "--jq", ".login"], timeout=20)
        login = txt.strip() if rc == 0 else ""
        add("github", "GitHub CLI (gh)", "ok" if login else "warn", f"signed in as {login}" if login else "not signed in",
            "gh auth login (the app uses this login for PR Review and PR Development)")
    signin = _github_signin(settings)
    if signin:
        users = [u.lower() for u in signin.get("allowed_users", [])]
        mismatch = bool(login) and login.lower() not in users
        add("github", "Continue with GitHub (sign-in)", "warn" if mismatch else "ok",
            f"on, for {', '.join(signin.get('allowed_users', [])) or '(nobody)'}" + (f" — but gh is signed in as {login}" if mismatch else ""),
            "PR Review shows only to the gh account signed in with GitHub: add it to allowed_users")
    else:
        add("github", "Continue with GitHub (sign-in)", "warn", "off: PR Review needs you signed in with GitHub (as the gh account)",
            "in the app: account menu (top right) → Set up “Continue with GitHub”… (one click), or [auth.github] in the configuration")

    # ---------------------------------------------------------------- optional features
    so = settings.office_soffice or shutil.which("soffice") or shutil.which("libreoffice")
    add("optional", "LibreOffice (Word / PowerPoint / Visio layout)", "ok" if so else "warn", so or "not found: only the text view of those files",
        "apt install --no-install-recommends libreoffice-writer libreoffice-impress libreoffice-draw (optional)")
    cs = settings.development.code_server or shutil.which("code-server")
    add("optional", "code-server (PR Development: VS Code option)", "ok" if cs else "warn", cs or "not found: the built-in web editor is used",
        "curl -fsSL https://code-server.dev/install.sh | sh -s -- --method=standalone --prefix=$HOME/.local (optional)")
    clangd = shutil.which("clangd")
    add("optional", "clangd (PR Development: C/C++ language server)", "ok" if clangd else "warn", clangd or "not found: no C/C++ completion / navigation",
        "apt install clangd (optional)")
    pyls = next((x for x in (shutil.which("basedpyright-langserver"), shutil.which("pyright-langserver"), shutil.which("pylsp"),
                             _venv_bin("basedpyright-langserver")) if x), None)
    add("optional", "Python language server (PR Development)", "ok" if pyls else "warn", pyls or "not found: no Python completion / navigation",
        ".venv/bin/pip install basedpyright (optional)")
    if settings.development.hosts:
        ssh = shutil.which(settings.development.ssh or "ssh")
        add("optional", "ssh (PR Development hosts)", "ok" if ssh else "fail", ssh or "not found",
            "install the OpenSSH client; hosts are reached with key authentication")
    return out


def _importable(mod: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(mod) is not None


def _venv_bin(name: str) -> str | None:
    p = Path(sys.prefix) / "bin" / name
    return str(p) if p.exists() else None


def _github_signin(settings: Settings) -> dict | None:
    if settings.github_client_id and settings.github_client_secret and settings.github_allowed_users:
        return {"allowed_users": list(settings.github_allowed_users)}
    f = settings.state_dir / "github-signin.json"
    try:
        d = json.loads(f.read_text())
        return {"allowed_users": list(d.get("allowed_users") or [])} if d.get("client_id") else None
    except (OSError, ValueError, KeyError):
        return None


ICON = {"ok": "✓", "warn": "!", "fail": "✗"}
TITLE = {"core": "Core", "server": "Server", "agents": "Agents", "github": "GitHub (PR Review, PR Development)",
         "optional": "Optional features"}


def report(checks: list[Check], color: bool) -> str:
    def paint(level: str, s: str) -> str:
        code = {"ok": "32", "warn": "33", "fail": "31"}[level]
        return f"\033[{code}m{s}\033[0m" if color else s

    lines: list[str] = []
    area = None
    for c in checks:
        if c.area != area:
            area = c.area
            lines.append(f"\n{TITLE.get(area, area)}")
        lines.append(f"  {paint(c.level, ICON[c.level])} {c.name}: {c.message}")
        if c.fix:
            lines.append(f"      → {c.fix}")
    n = {lv: sum(1 for c in checks if c.level == lv) for lv in ICON}
    lines.append(f"\n{n['ok']} ok, {n['warn']} warning(s), {n['fail']} problem(s)"
                 + ("" if n["fail"] else " — ready" if not n["warn"] else " — ready (warnings are optional features or advice)"))
    return "\n".join(lines).lstrip("\n")


def as_json(checks: list[Check]) -> str:
    return json.dumps([asdict(c) for c in checks], indent=2)
