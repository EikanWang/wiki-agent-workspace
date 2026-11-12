# Deployment guide — from an empty machine to a running service

Everything runs as **your own Linux user**: the one that owns the wiki and is logged in to the agent CLIs
(OpenCode, Codex, Claude Code) and to GitHub (`gh`). Root is only needed — optionally — to install system packages;
without it the installer puts Python and Node.js in your home folder.

Contents: [0 One-click install](#0-one-click-install) · [1 Prerequisites](#1-prerequisites) ·
[2 Manual install](#2-manual-install-step-by-step) · [3 Configure](#3-configure) · [4 Start, stop, keep running](#4-start-stop-keep-running) ·
[5 Log in and check](#5-log-in-and-check) · [6 Agents](#6-agents) · [7 GitHub: PR Review and PR Development](#7-github-pr-review-and-pr-development) ·
[8 PR Development tools](#8-pr-development-tools) · [9 Remote access](#9-remote-access) · [10 Upgrade](#10-upgrade) ·
[11 Uninstall](#11-uninstall) · [12 Troubleshooting](#12-troubleshooting)

## 0. One-click install

On the Linux machine that holds your wiki (or where you want one):

```bash
curl -fsSL https://raw.githubusercontent.com/EikanWang/wiki-agent-workspace/main/scripts/install.sh | bash
```

or, in a clone of this repository: `scripts/install.sh`. It asks a few questions (all have defaults; `--yes` takes
them all), then:

1. **Prerequisites** — Git, curl, tmux, Python 3.11+, Node.js 20+. Missing ones are installed with your package
   manager (apt, dnf, yum, zypper, pacman, apk) if you allow `sudo`; otherwise, without root, Python comes from
   [uv](https://docs.astral.sh/uv/) and Node.js 22 from nodejs.org, both into your home folder. (tmux needs the
   package manager; without it everything but the terminals works.)
2. **The code** — cloned to `~/wiki-agent-workspace` (`--dir`) when the script runs outside a checkout.
3. **Your wiki folder** — `--workspace DIR`; created as a new Git repository with a first page if it does not exist.
4. **Agent CLIs** (optional, `--agents opencode,codex,claude|all|none`) — installed in your home folder if missing.
5. **Optional features** (`--with gh,office,dev|all|none`) — the GitHub CLI (PR Review, PR Development's pull
   requests), LibreOffice (original layout of Word / PowerPoint / Visio files), PR Development's tools (clangd,
   basedpyright, code-server).
6. **Setup** — `scripts/setup.sh`: the Python environment (`.venv`, locked dependencies), the browser UI
   (`frontend/dist`), the configuration `~/.config/wiki-agent-workspace/config.toml` (mode 0600) with a generated
   access token, and the data folder `~/.local/share/wiki-agent-workspace/`.
7. **Start** — a systemd *user* service (`scripts/service.sh install`; starts at boot and keeps running after you
   log out if you agree), or a background process where systemd user services are unavailable (`--no-service`: do
   not start).
8. **Check** — `scripts/doctor.sh`, then it prints the **URL** and the **access token**.

All options:

| Option | Meaning |
| --- | --- |
| `--workspace DIR` | your wiki folder (default: asked; `~/wiki` with `--yes`; an existing configuration's folder on a re-run) |
| `--agents LIST` | `opencode`, `codex`, `claude` (comma-separated), `all` or `none` |
| `--with LIST` | `gh`, `office`, `dev` (comma-separated), `all` or `none` |
| `--port N`, `--bind ADDR`, `--state-dir DIR` | listen port (8765), address (127.0.0.1), data folder |
| `--dir DIR`, `--repo URL` | where to clone the code, and from where |
| `--no-system-packages` | never use sudo / the package manager |
| `--no-service` | do not start the server |
| `-y`, `--yes` | ask nothing |

Examples:

```bash
scripts/install.sh --yes --workspace ~/notes                        # quiet install for an existing wiki
scripts/install.sh --workspace ~/team-wiki --agents opencode --with all
scripts/install.sh --yes --no-system-packages --port 8800           # shared machine without root
```

Re-running the installer is safe: it upgrades dependencies, rebuilds the UI and keeps your configuration, token and
data. Then do the few logins it lists (§5–§7) and you are done.

## 1. Prerequisites

| Requirement | Version | Why | Check |
| --- | --- | --- | --- |
| Linux (x86_64/arm64) | any modern distro | the server (browsers on any system — desktop or phone — use it) | `uname -a` |
| Python + `venv` | 3.11 – 3.13 | the backend | `python3 --version` |
| Git | 2.x | wiki folders are Git checkouts (or plain folders); diffs, commits, PR Development | `git --version` |
| tmux | **3.2+** (3.5a tested) | persistent terminals; without it the rest works | `tmux -V` |
| Node.js + npm | **20+** (22 recommended) | builds the browser UI once (not used at run time) | `node --version` |
| OpenCode and/or Codex | current | agents for discussions, chats, PR reviews, PR Development | `opencode --version`, `codex --version` |
| Claude Code | current | optional: its sessions in the Agents monitor | `claude --version` |
| GitHub CLI `gh` | 2.x | optional: PR Review, PR Development's pull requests (uses its login) | `gh auth status` |
| LibreOffice (Writer, Impress, Draw) | 7.x+ | optional: original layout of Word, PowerPoint and Visio files (else text view only) | `soffice --version` |
| clangd, basedpyright | current | optional: C/C++ and Python intelligence in PR Development's web editor | `clangd --version` |
| code-server | 4.x | optional: PR Development's *VS Code* editor option (the built-in web editor needs nothing) | `code-server --version` |

The installer handles all of these. To install them yourself:

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y git curl tmux python3 python3-venv python3-pip
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs
sudo apt install -y gh clangd                                                     # optional
sudo apt install -y --no-install-recommends libreoffice-writer libreoffice-impress libreoffice-draw   # optional

# Fedora / RHEL
sudo dnf install -y git curl tmux python3 python3-pip nodejs npm gh clang-tools-extra
sudo dnf install -y libreoffice-writer libreoffice-impress libreoffice-draw         # optional

# No root: Python via uv, Node via nvm (or the installer's user-level installs)
curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.12
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash && nvm install 22

# Agents and tools in your home folder
curl -fsSL https://opencode.ai/install | bash                                      # OpenCode
npm install -g --prefix ~/.local @openai/codex                                     # Codex
npm install -g --prefix ~/.local @anthropic-ai/claude-code                         # Claude Code
curl -fsSL https://code-server.dev/install.sh | sh -s -- --method=standalone --prefix=$HOME/.local   # code-server
```

**inotify limit (large wikis).** The file watcher needs one inotify watch per directory, shared with other tools
(IDEs, OpenCode's server). If the UI says "inotify watch limit … exhausted", live change detection stops (↻ still
works). Raise it once:

```bash
echo fs.inotify.max_user_watches=1048576 | sudo tee /etc/sysctl.d/90-inotify-watches.conf && sudo sysctl --system
```

## 2. Manual install (step by step)

What the installer does, by hand:

```bash
git clone https://github.com/EikanWang/wiki-agent-workspace.git ~/wiki-agent-workspace
cd ~/wiki-agent-workspace
scripts/setup.sh --workspace /path/to/your/wiki      # .venv, browser UI, config + token (printed once)
scripts/service.sh install                           # start it in the background, kept running
scripts/doctor.sh                                    # check everything
```

Keep the code **outside** the wiki folder. `setup.sh` is idempotent (re-run it any time; it keeps the token) and
takes `--port N --bind ADDR --state-dir DIR --default-provider opencode|codex
--codex-sandbox read-only|workspace-write|danger-full-access --show-token`. It never installs system packages: it
names what is missing. `WAW_PYTHON=/path/to/python3.12 scripts/setup.sh …` picks a specific interpreter.

Several instances (e.g. one per team): give each its own `WAW_CONFIG=…/config.toml`, `--port` and `--state-dir`,
and `WAW_SERVICE_NAME=…` for `scripts/service.sh`. Usually one instance with several folders is simpler (§3).

## 3. Configure

One TOML file, `~/.config/wiki-agent-workspace/config.toml` (or `$WAW_CONFIG`); `config.example.toml` is the
annotated template and [CONFIGURATION.md](CONFIGURATION.md) describes every key. Restart after editing
(`scripts/service.sh restart`). The decisions most people make:

| Question | Setting |
| --- | --- |
| Reach it only from this machine / an SSH tunnel? | keep `server.bind = "127.0.0.1"` (default) |
| From a trusted LAN/VPN? | `server.bind = "0.0.0.0"` or that address — plain HTTP; prefer §9 B for anything else |
| Behind an HTTPS reverse proxy? | `server.cookie_secure = true`, `server.trusted_proxies = ["127.0.0.1"]`, the public origin in `allowed_origins` |
| More wiki folders? | nothing: **＋ Add folder** in the app (or `[[workspaces]]`) |
| Default agent? | `agents.default_provider = "opencode"` or `"codex"` (detected at setup) |
| Agents need API tokens from a script? | `agents.env_script = "~/set_tokens.sh"` (sourced for every agent process; re-read when it changes) |
| Codex may edit when you ask? | `agents.codex.sandbox = "workspace-write"` (default) or `"read-only"` |
| Sign in with GitHub / Google instead of the token? | §7 (one click for GitHub), `[auth.google]` |
| PR Development on another machine? | `[[development.hosts]]` (§8) |

## 4. Start, stop, keep running

```bash
scripts/service.sh install     # systemd user service: now, at boot, after logout (asks about "lingering")
scripts/service.sh status      # running? answering?
scripts/service.sh logs [-f]   # the server log
scripts/service.sh restart     # after a config change or an upgrade
scripts/service.sh stop | start
scripts/service.sh uninstall   # remove the service (data and terminals are kept)
scripts/start.sh               # or: in the foreground (Ctrl-C stops it)
```

Without systemd user services (containers, some HPC login nodes) `service.sh` runs the server detached with a pid
file and its log in the data folder; `scripts/service.sh start` again after a reboot (or a `@reboot` crontab line).
The service file keeps your `PATH`, so the agent CLIs and `gh` are found as in your shell.

Restarting the server never kills your terminals: they live on the app's own tmux server and are reattached at
startup; agent turns that were running are marked *interrupted* (never re-run by themselves).

## 5. Log in and check

Open the URL the installer printed (default `http://127.0.0.1:8765`; from another machine see §9) and paste the
access token once. The login lasts 30 days and renews while you use it. Forgot the token?
`scripts/setup.sh --show-token`.

```bash
scripts/doctor.sh              # every feature: ✓ ok, ! optional/advice, ✗ problem — each with the fix
scripts/doctor.sh --quick      # without the slower checks;  --json for scripts
curl -s http://127.0.0.1:8765/api/v1/health        # {"status":"ok"}
```

A first tour: the **Wiki** sidebar lists your folder → open a page → select text → ask. **Terminal** (activity bar)
→ **+** → run `opencode`, `codex`, `git` as usual. [USER_GUIDE.md](USER_GUIDE.md) explains every feature.

## 6. Agents

| | OpenCode | Codex | Claude Code |
| --- | --- | --- | --- |
| Log in once, as your user | `opencode auth login` | `codex login` | `claude` (first run) |
| Used for | discussions, chats, PR reviews, PR Development | same | the Agents monitor (watch, continue its sessions) |
| How the app runs it | `opencode run --format json --dir WS [--session ID]` (or `mode = "server"`) | `codex exec --json …` / `codex exec resume …` | `claude -p --resume ID` when you continue a session |
| Its sessions (Agents monitor) | `opencode session list` | `$CODEX_HOME/sessions` | `$CLAUDE_CONFIG_DIR/projects` (`~/.claude`) |

Agents are optional: without any, reading, editing, diffs, commits and terminals work and the UI explains what is
unavailable. If your agents get their API keys from environment variables, put the exports in a script and set
`agents.env_script` (§3). **OpenCode server mode** (one backend shared with your terminal TUI):

```bash
opencode serve --port 4096 --hostname 127.0.0.1       # e.g. in a terminal of the app
opencode --attach http://127.0.0.1:4096                # your TUI, same backend
```
```toml
[agents.opencode]
mode = "server"
server_url = "http://127.0.0.1:4096"
```

## 7. GitHub: PR Review and PR Development

**PR Review** (your review queue, PR tabs, agent reviews) is shown only to the GitHub account the server's `gh` CLI
is logged in as, signed in to the app with GitHub (the queue can include private repositories):

1. `gh auth login` on the server (as your user) — `scripts/doctor.sh` shows "signed in as <you>".
2. In the app: account menu (top right) → **Set up “Continue with GitHub”…** → GitHub shows *Create GitHub App*
   with everything filled in → **Create**. Nothing to copy, no restart; the login page now offers **Continue with
   GitHub** (only that account is allowed; more in `[auth.github] allowed_users`).
3. Sign out and **Continue with GitHub**: the **Reviews** icon appears in the activity bar.

**PR Development** uses the same `gh` login to fetch pull requests, push and create PRs, and your Git identity
(`git config --global user.name/user.email`) to commit.

## 8. PR Development tools

| Feature | Needs | Where it comes from |
| --- | --- | --- |
| Web editor (default) | nothing | built in (Monaco) |
| C/C++ intelligence | `clangd` (+ a `compile_commands.json`) | `apt install clangd`; set the folder in the workspace's Settings |
| Python intelligence | `basedpyright` (or pyright / pylsp) | `.venv/bin/pip install basedpyright` (installer: `--with dev`) |
| *VS Code* editor option | code-server | installer `--with dev`, or §1; **Install language support** in the app adds its extensions |
| Develop on another machine | key-based `ssh`, the agent CLI (and code-server for VS Code) there | `[[development.hosts]]` in the configuration |
| AI inline completion | an OpenAI-compatible endpoint | `[development.completion]` |

```toml
[[development.hosts]]
name = "gpu-box"
ssh = "me@gpu-box"                 # key authentication (BatchMode); test with: ssh me@gpu-box true
work_dir = "~/.cache/wiki-agent-workspace"
```

## 9. Remote access

The server must never be reachable unauthenticated from a public network; plain HTTP on an untrusted network would
expose the login cookie.

**A. SSH tunnel or private network (simplest).**

```bash
ssh -N -L 8765:127.0.0.1:8765 you@server       # on your laptop; then browse http://127.0.0.1:8765
```
or `--bind` a VPN / Tailscale / WireGuard address. Corporate proxies: add the server to the browser's proxy
bypass list (`no_proxy`).

**B. HTTPS through a reverse proxy** (Caddy example):

```
wiki.example.internal {
    reverse_proxy 127.0.0.1:8765
}
```
Then `cookie_secure = true`, `trusted_proxies = ["127.0.0.1"]` and, if the proxy rewrites `Host`,
`allowed_origins = ["https://wiki.example.internal"]`. WebSockets (`/api/v1/terminals/*/ws`, `/dev-editor/…`) and
SSE (`/api/v1/events`, `/api/v1/jobs/*/events`) must pass unbuffered (nginx: `proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; proxy_buffering off;`).
Behind a gateway that already authenticates you (Authelia, oauth2-proxy, Cloudflare Access): `gateway_auth = true`
and `gateway_user_header = "X-Forwarded-User"`. With HTTPS, **Continue with GitHub/Google** work from anywhere.

## 10. Upgrade

```bash
cd ~/wiki-agent-workspace && git pull && scripts/install.sh --yes --no-service && scripts/service.sh restart
```
(or `git pull && scripts/setup.sh && scripts/service.sh restart`). Database migrations run at startup and keep your
data; for a backup first see [OPERATIONS.md](OPERATIONS.md) (stop, copy the data folder; or download
`/api/v1/export` while logged in).

## 11. Uninstall

```bash
scripts/service.sh uninstall
tmux -L "$(cat ~/.local/share/wiki-agent-workspace/tmux-socket)" kill-server   # the app's terminals
rm -rf ~/wiki-agent-workspace                      # code, .venv, UI build
rm -rf ~/.local/share/wiki-agent-workspace         # database and snapshots (your discussions!), user-level Node
rm -rf ~/.config/wiki-agent-workspace              # configuration and token
loginctl disable-linger "$USER"                    # if nothing else of yours needs to run after logout
```
Your wiki folders, agent CLIs and their logins are untouched.

## 12. Troubleshooting

Start with `scripts/doctor.sh`: it names what is wrong and the fix. Then `scripts/service.sh logs`.

| Symptom | Fix |
| --- | --- |
| The page does not load | `scripts/service.sh status`; on another machine use the tunnel (§9 A); corporate proxy → bypass list |
| "unknown API route" after an upgrade | the server is older than the page: `scripts/service.sh restart` |
| Terminals disabled | install tmux 3.2+, restart |
| "No provider" / discussions unavailable | install and log in to OpenCode or Codex (§6), `scripts/setup.sh`, restart |
| Agents fail with authentication errors | log in again (`opencode auth login`, `codex login`); tokens from a script → `agents.env_script` |
| No Reviews icon | §7: `gh auth login`, **Continue with GitHub** with that account |
| File changes not noticed | inotify limit (§1); ↻ in the file tree works meanwhile |
| Office files: only the text view | install LibreOffice (§1) |

More (every banner and error the app can show, logs, backups, moving the data folder): [OPERATIONS.md](OPERATIONS.md).
