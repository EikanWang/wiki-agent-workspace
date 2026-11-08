# Deployment guide — from an empty machine to a running service

This guide assumes nothing is installed except a Linux user account. Every command runs as **your own user**
(the one that owns the wiki checkout and is logged in to OpenCode/Codex). Nothing requires root except the
optional OS package installation and the optional inotify limit.

Contents: [1 Prerequisites](#1-prerequisites) · [2 Get the code](#2-get-the-code) · [3 Install](#3-install) ·
[4 Configure](#4-configure) · [5 Start and log in](#5-start-and-log-in) · [6 Verify](#6-verify) ·
[7 Run as a service](#7-run-as-a-service-optional) · [8 Remote access](#8-remote-access) ·
[9 Agent providers](#9-agent-providers) · [10 Upgrade](#10-upgrade) · [11 Uninstall](#11-uninstall)

## 1. Prerequisites

| Requirement | Version | Why | Check |
| --- | --- | --- | --- |
| Linux (x86_64/arm64) | any modern distro | server platform (running the server on Windows/macOS is out of scope; browsers on them are fine) | `uname -a` |
| Python | 3.11 – 3.13 | backend | `python3 --version` |
| `python3-venv` / `pip` | matching | creates `.venv` | `python3 -m venv --help` |
| Git | any 2.x | reads branch/status of the wiki; the wiki itself must be a Git checkout or plain directory | `git --version` |
| tmux | **3.2+** (3.5a tested) | persistent terminals; without it the reader works but terminals are disabled | `tmux -V` |
| Node.js + npm | **20+** (22 recommended) | builds the browser UI once (not needed at runtime) | `node --version` |
| OpenCode and/or Codex CLI | current | agent providers, logged in as the same user | `opencode --version`, `codex --version` |
| LibreOffice (Writer, Impress, Draw) | 7.x+ (25.2 tested) | optional: the original-layout view of Word, PowerPoint and Visio files; without it only their text view is available | `soffice --version` |
| `infocmp`/ncurses terminfo | usual | tmux picks `tmux-256color`/`screen-256color` for the inner `$TERM` | `infocmp tmux-256color` |

Install commands (choose your distro):

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y git tmux python3 python3-venv python3-pip curl
# optional: original layout of Word / PowerPoint / Visio files
sudo apt install -y --no-install-recommends libreoffice-writer libreoffice-impress libreoffice-draw
# Node 22 via NodeSource (or use nvm: https://github.com/nvm-sh/nvm)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs

# Fedora / RHEL
sudo dnf install -y git tmux python3 python3-pip nodejs npm
sudo dnf install -y libreoffice-writer libreoffice-impress libreoffice-draw   # optional, see above

# No root? Python via uv/pyenv, Node via nvm, tmux built from source into ~/.local all work:
#   curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.12
#   curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash && nvm install 22
```

Agent CLIs (only what you use):

```bash
# OpenCode  — https://opencode.ai/docs/
curl -fsSL https://opencode.ai/install | bash && opencode auth login
# Codex CLI — https://developers.openai.com/codex/cli/
npm install -g @openai/codex && codex login
```

**inotify limit (recommended for large wikis).** The file watcher registers every directory of the workspace;
other tools (OpenCode's own server, IDEs) consume the same per-user budget. If the limit is exhausted the UI shows
"inotify watch limit … exhausted" and live change detection stops (manual refresh still works). Raise it once:

```bash
echo fs.inotify.max_user_watches=1048576 | sudo tee /etc/sysctl.d/90-inotify-watches.conf
sudo sysctl --system
```

## 2. Get the code

```bash
git clone https://github.com/EikanWang/wiki-agent-workspace.git ~/wiki-agent-workspace
cd ~/wiki-agent-workspace
```

The repository must **not** live inside the wiki checkout (the app excludes its own state from the file tree but
will happily index its source otherwise).

## 3. Install

```bash
scripts/setup.sh --workspace /path/to/your/wiki
```

What it does (idempotent, re-run any time):

1. Checks `git`, `tmux`, `node`, `npm`, Python ≥ 3.11 and names anything missing with install hints
   (it never installs system packages itself).
2. Creates `.venv` and installs the **locked** Python dependencies (`requirements.lock`) plus the project itself.
3. `npm ci` + `npm run build` in `frontend/` → `frontend/dist/` (served by the Python server; no Node at runtime).
4. Writes the configuration file (default `~/.config/wiki-agent-workspace/config.toml`, mode **0600**) with a
   freshly generated 32-byte access token and server secret, creates the state directory
   (default `~/.local/share/wiki-agent-workspace/`), and **prints the access token once**.

Options: `--port N` `--bind ADDR` `--state-dir DIR` `--default-provider opencode|codex`
`--codex-sandbox read-only|workspace-write|danger-full-access` `--show-token`.
Use `WAW_CONFIG=/other/path/config.toml scripts/setup.sh …` to keep the config elsewhere (e.g. several
instances for several wikis — give each its own `--port` and `--state-dir`).

Typical duration: 20–60 s (mostly `npm ci`).

## 4. Configure

Everything is in the one TOML file written by setup; `config.example.toml` in the repo is the annotated template
and [docs/CONFIGURATION.md](CONFIGURATION.md) describes every key. Decisions to make now:

| Question | Setting |
| --- | --- |
| Only from this machine / through an SSH tunnel? | keep `server.bind = "127.0.0.1"` (default) |
| From other machines on a trusted LAN/VPN? | `server.bind = "0.0.0.0"` (or the LAN/VPN address). Token login still applies; traffic is plain HTTP unless you add a reverse proxy (§8). |
| Behind an HTTPS reverse proxy? | `server.cookie_secure = true`, `server.trusted_proxies = ["127.0.0.1"]`, add the public origin to `allowed_origins` |
| Which agent answers by default? | `agents.default_provider = "opencode"` or `"codex"` (auto-detected at setup) |
| Codex may edit the wiki when you explicitly ask? | `agents.codex.sandbox = "workspace-write"` (default); `read-only` to forbid edits |
| OpenCode shared with your terminal TUI? | `agents.opencode.mode = "server"`, `server_url = "http://127.0.0.1:4096"` (see §9) |
| More than one wiki? | Nothing to do now: add folders later with **＋ Add folder** in the file tree, or list them under `[[workspaces]]` (see CONFIGURATION.md). Each folder needs its own inotify watches (§1). |

Edit the file and restart the server after changes (`setup.sh` can also be re-run with flags; it preserves the
token and secret).

## 5. Start and log in

```bash
scripts/start.sh                 # foreground; Ctrl-C stops it. Log goes to stdout.
# or detached without a service manager:
nohup scripts/start.sh > ~/.local/share/wiki-agent-workspace/server.log 2>&1 &
```

Open `http://<host>:8765/` in a desktop or mobile browser and paste the access token. The login cookie lasts
30 days and is renewed while you use the app; you will not be asked again for documents, messages, shell
commands or edits. Forgot the token? `scripts/setup.sh --show-token`.

## 6. Verify

```bash
curl -s http://127.0.0.1:8765/api/v1/health            # {"status":"ok"}
.venv/bin/python -m backend.app.cli print-config        # effective config, secrets redacted
```

In the browser: the file tree lists your wiki folder (and any additional folders) with Git badges → open one → select text → the question
box appears → **Terminal** tab → **+ New terminal** → run `opencode` or `codex` as usual. The startup log shows
provider detection, e.g. `provider opencode: available=True version=1.18.31`, the terminal reconcile summary and
`filesystem watcher ready for …`.

Run the test suites once on the target machine to confirm tmux/Git/PTY behaviour there (they use fake agents,
no credentials needed): `scripts/test.sh` (backend, ~30 s) and `scripts/test.sh --e2e` (needs
`cd frontend && npx playwright install chromium` once).

## 7. Run as a service (optional)

User-level systemd, no root:

```ini
# ~/.config/systemd/user/wiki-agent-workspace.service
[Unit]
Description=Wiki Agent Workspace
After=network.target

[Service]
WorkingDirectory=%h/wiki-agent-workspace
ExecStart=%h/wiki-agent-workspace/scripts/start.sh
Restart=on-failure
RestartSec=3
# Environment=WAW_CONFIG=%h/.config/wiki-agent-workspace/config.toml

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now wiki-agent-workspace
loginctl enable-linger $USER          # keep it running after you log out
journalctl --user -u wiki-agent-workspace -f
```

A restart of the web server never kills your terminals: tmux sessions live on the app-owned tmux server and are
reattached on startup; unfinished agent jobs are marked `interrupted` (never re-run automatically).

## 8. Remote access

The server must never be exposed unauthenticated on a public interface; the token login protects the shell, but
plain HTTP on an untrusted network exposes the session cookie.

**A. Private network or tunnel (simplest).**

```bash
ssh -N -L 8765:127.0.0.1:8765 user@remote-host      # then browse http://127.0.0.1:8765
```
or bind to a VPN/Tailscale/WireGuard address (`--bind 100.64.0.5`) or to the LAN (`--bind 0.0.0.0`) when the
network itself is trusted. Corporate proxies: add the host to your browser's proxy bypass list (`no_proxy`) —
the "Access Denied" page of a DMZ proxy means the browser routed the private address through it.

**B. HTTPS through an authenticated reverse proxy.** Example with Caddy:

```
wiki.example.internal {
    basic_auth { you <hash from `caddy hash-password`> }   # optional extra factor
    reverse_proxy 127.0.0.1:8765
}
```
Then set `cookie_secure = true`, `trusted_proxies = ["127.0.0.1"]` and, if the proxy rewrites `Host`, add
`https://wiki.example.internal` to `allowed_origins`. WebSockets (`/api/v1/terminals/*/ws`) and SSE
(`/api/v1/events`, `/api/v1/jobs/*/events`) must be proxied without buffering (Caddy/nginx defaults are fine;
nginx needs `proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection
"upgrade"; proxy_buffering off;`).

If the gateway already authenticates you and sets a header (Authelia, oauth2-proxy, Cloudflare Access), set
`gateway_auth = true` and `gateway_user_header = "X-Forwarded-User"`; only requests arriving from
`trusted_proxies` with that header skip the token login.

## 9. Agent providers

| | OpenCode | Codex |
| --- | --- | --- |
| Login | `opencode auth login` in a terminal as your user | `codex login` |
| How app turns run | `opencode run --format json --dir WS [--session ID]` (CLI mode, default) | `codex exec --json -C WS -s <sandbox> -` and `codex exec resume … ID -` |
| Session list for "link existing session" | `opencode session list --format json` | read from `$CODEX_HOME/session_index.jsonl` + rollout files |
| Permission prompts | CLI mode: handled by OpenCode's configured rules (cannot be relayed). Server mode: relayed into the discussion panel. | `codex exec` cannot ask interactively: actions needing approval fail visibly, with the terminal as fallback |
| Sandbox | OpenCode's own permission config | `agents.codex.sandbox` for app turns only (default `workspace-write`, scoped to the workspace) |

**OpenCode server mode** (share one backend between your terminal TUI and the app): start one server and attach
both to it.

```bash
opencode serve --port 4096 --hostname 127.0.0.1      # e.g. in a tmux terminal of the app, or a user service
opencode --attach http://127.0.0.1:4096               # your interactive TUI, same backend
```
```toml
[agents.opencode]
mode = "server"
server_url = "http://127.0.0.1:4096"
```
Starting a second `opencode serve` does **not** attach to a TUI's own server; always point both at the same
instance. Server mode streams token deltas and relays OpenCode's real permission requests.

Providers are optional: with none installed the reader and terminal work, and the UI explains why discussions are
unavailable.

## 10. Upgrade

```bash
cd ~/wiki-agent-workspace
git pull
scripts/setup.sh                     # re-installs locked deps if changed, rebuilds the UI, keeps your config/token
systemctl --user restart wiki-agent-workspace   # or Ctrl-C + scripts/start.sh
```
Database migrations (Alembic) run automatically on startup; they are additive and keep your data. Take a backup
first if you like (`Backup` button or `curl -b <cookie> http://127.0.0.1:8765/api/v1/export > backup.tar.gz`).
See [docs/OPERATIONS.md](OPERATIONS.md) for backup/restore and moving the state directory.

## 11. Uninstall

```bash
systemctl --user disable --now wiki-agent-workspace 2>/dev/null
tmux -L "$(cat ~/.local/share/wiki-agent-workspace/tmux-socket)" kill-server   # ends the app's terminals only
rm -rf ~/wiki-agent-workspace                          # code + .venv + frontend build
rm -rf ~/.local/share/wiki-agent-workspace             # database, snapshots (your Q&A history!)
rm -rf ~/.config/wiki-agent-workspace                  # config incl. access token
```
Your wiki checkout and your OpenCode/Codex installations and logins are untouched.
