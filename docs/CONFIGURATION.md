# Configuration reference

One TOML file holds all settings. Location, in order of precedence: `--config PATH` (CLI), `WAW_CONFIG`
environment variable, default `~/.config/wiki-agent-workspace/config.toml`. `scripts/setup.sh` creates it with
mode 0600; `config.example.toml` is the annotated template. Restart the server after editing.

Environment variables override the file for a few deployment-time values (useful for containers/services):
`WAW_ACCESS_TOKEN`, `WAW_SERVER_SECRET`, `WAW_GITHUB_CLIENT_SECRET`, `WAW_GOOGLE_CLIENT_SECRET`, `WAW_WORKSPACE`, `WAW_STATE_DIR`, `WAW_BIND`,
`WAW_PORT`, `WAW_FRONTEND_DIST`, `WAW_CONFIG`.

Show the effective configuration with secrets redacted:
`.venv/bin/python -m backend.app.cli print-config`.

## `[server]`

| Key | Default | Meaning |
| --- | --- | --- |
| `bind` | `"127.0.0.1"` | Listen address. `0.0.0.0` for all interfaces (only on trusted networks or behind a proxy). |
| `port` | `8765` | Listen port. |
| `state_dir` | `~/.local/share/wiki-agent-workspace` | Self-contained data folder: SQLite DB, `snapshots/`, `tmux-socket`, `tmux.conf`, preview temp copies. Relocatable (see OPERATIONS.md). |
| `cookie_secure` | `false` | Set `true` when the browser reaches the app over HTTPS (cookie gets the `Secure` flag). Also auto-enabled when a trusted proxy reports `X-Forwarded-Proto: https`. |
| `allowed_origins` | `[]` | Extra browser origins accepted for CSRF/WebSocket checks besides the request `Host` (e.g. `["https://wiki.example.internal"]`). |
| `trusted_proxies` | `[]` | IPs/CIDRs whose `X-Forwarded-Host/Proto/*` headers are trusted (e.g. `["127.0.0.1"]`). Also enables uvicorn `proxy_headers`. |
| `gateway_auth` | `false` | Accept requests as authenticated when they come from `trusted_proxies` **and** carry `gateway_user_header`. Only for already-authenticated gateways. |
| `gateway_user_header` | `""` | Header name the gateway sets, e.g. `X-Forwarded-User`. Required when `gateway_auth = true`. |
| `session_days` | `30` | Login cookie lifetime; renewed while active. |
| `log_level` | `"INFO"` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. Logs never contain tokens or document contents. |
| `frontend_dist` | `<repo>/frontend/dist` | Directory of the built UI, if you keep it elsewhere. |

## `[workspace]`

| Key | Default | Meaning |
| --- | --- | --- |
| `root` | (required; set by `setup.sh --workspace`) | The **primary** wiki folder, usually a Git checkout. It is the first folder of the window and the default for requests without a folder. The file tree of each folder is restricted to it; terminals are not. |
| `max_document_bytes` | `20971520` (20 MiB) | Documents larger than this return HTTP 413 (applies to every folder). |

## `[[workspaces]]` — additional folders of the window

More wiki folders opened next to the primary one (VS Code-style multi-root). Each is fully isolated: documents,
discussions, agent sessions, locks, reviews and Git operations are per folder. Folders listed here are always open
and cannot be removed from the UI; folders added from the UI (**＋ Add folder**) are stored in the database instead and
need no config entry.

| Key | Default | Meaning |
| --- | --- | --- |
| `root` | (required) | Absolute path of the folder on the server. |
| `name` | folder name | Display name in the file tree and folder switcher. |

```toml
[[workspaces]]
root = "/home/me/notes-wiki"
name = "Notes"

[[workspaces]]
root = "/home/me/team-wiki"
```

The browser selects the folder per request with the `X-Workspace` header (or `?workspace=<id>`, used for images and
PDFs). Requests that address an object (document, discussion, review, session…) always use that object's folder.

## `[office]`

Layout view of Word / PowerPoint / Visio / OpenDocument files: rendered to PDF by LibreOffice on the server.

| Key | Default | Meaning |
| --- | --- | --- |
| `soffice` | found on `PATH` | Path to LibreOffice's `soffice`. Without LibreOffice the Layout view says so and the Text view still works. |
| `max_parallel` | `2` | LibreOffice conversions running at the same time (each takes one CPU core and ~200–500 MB while it runs). |
| `timeout` | `180` | Seconds before a conversion is stopped and reported as failed (**Try again** retries). |
| `max_bytes` | `209715200` (200 MiB) | Largest file the Layout view renders. Independent of `workspace.max_document_bytes`, which limits the Text view. |
| `cache_mb` | `2048` | Size cap of the render cache (`<state_dir>/renders`); least recently viewed renders are removed first. |

Each conversion copies the file to a temporary folder and runs headless LibreOffice there with a private profile
(`<state_dir>/lo-profiles`): your wiki is never written to, and your own LibreOffice settings are not touched.
Renders are cached by content hash, so an unchanged file is converted once.

## `[terminal]`

| Key | Default | Meaning |
| --- | --- | --- |
| `tmux_history_limit` | `10000` | Lines kept per pane in the app-owned tmux server (`history-limit`). Also the maximum for the "tmux history" snapshot. |
| `shell` | `$SHELL` | Shell started in new terminals (`default-shell`). |

The app-owned tmux server uses its own socket (`<state_dir>/tmux-socket`) and config (`<state_dir>/tmux.conf`,
regenerated at startup); your personal `~/.tmux.conf` is never touched. Generated settings: 256-colour inner
`$TERM` (`tmux-256color` if installed, else `screen-256color`), RGB for the outer `xterm-256color`, mouse on
(wheel scrolls history, drag selects), `set-clipboard on` plus the `clipboard` terminal feature (copies reach
the browser via OSC 52), `window-size latest`, status bar off, UTF-8 locale when the server's isn't. The config is re-applied
(`source-file`) to a tmux server that is already running at startup.

## `[auth]`

| Key | Default | Meaning |
| --- | --- | --- |
| `access_token` | generated by setup | The single login secret (32 random bytes, URL-safe). Never printed by the server; `setup.sh --show-token` prints it. |
| `server_secret` | generated by setup | HMAC key deriving CSRF tokens from session cookies. Changing it invalidates CSRF tokens (users re-login). |

Both may instead come from `WAW_ACCESS_TOKEN` / `WAW_SERVER_SECRET`.

### `[auth.github]` — Continue with GitHub (optional)

Adds a **Continue with GitHub** button to the login page (the access token keeps working). Enabled when all three
of `client_id`, `client_secret` and `allowed_users` are set.

| Key | Default | Meaning |
| --- | --- | --- |
| `client_id` | — | Client ID of your GitHub OAuth app. |
| `client_secret` | — | Its client secret (or `WAW_GITHUB_CLIENT_SECRET`, which wins). |
| `allowed_users` | `[]` | GitHub logins allowed to sign in (case-insensitive), e.g. `["EikanWang"]`. Any other account is refused — having a GitHub account is never enough. Empty = GitHub sign-in off. |
| `callback_url` | the address the browser used + `/api/v1/auth/github/callback` | Set it when the server is reached under several addresses (it must equal the OAuth app's *Authorization callback URL*). |

**Easiest: one click from the login page.** If the server user's `gh` CLI is logged in, its account is taken as
the server's owner and the login page shows **Continue with GitHub** even before setup; the first click starts
the setup below, which is accepted only if GitHub reports that this owner created the app, and continues straight
into sign-in. The same is in the account menu (top right) → **Set up “Continue with GitHub”…** when signed in. GitHub shows *Create GitHub App* with everything filled in (an app with no
permissions, public so that every allowed account can use it; its callback is this server's address); click **Create**. GitHub hands the new app's client ID and
secret back to the server, which saves them in `<state_dir>/github-signin.json` (mode 0600), allows only the GitHub
account that created it, and the login page offers **Continue with GitHub** right away — no restart, nothing to
copy. To let more GitHub accounts in, add them to `allowed_users` in that file (or in
`config.toml`) and restart. Keys set in `config.toml` (below) take precedence over that file; delete the file (and the app on GitHub,
*Settings → Developer settings → GitHub Apps*) to undo.

**Manually** instead: on GitHub, **Settings → Developer settings → OAuth Apps → New OAuth App**. *Homepage URL*: the
address you open the app at (e.g. `http://10.239.60.122:8765`); *Authorization callback URL*: that address plus
`/api/v1/auth/github/callback`. Copy the client ID, generate a client secret, put both and your login into
`config.toml`, restart:

```toml
[auth.github]
client_id = "Ov23li…"
client_secret = "…"          # or export WAW_GITHUB_CLIENT_SECRET
allowed_users = ["EikanWang"]
```

The server talks to `github.com` / `api.github.com` (through `https_proxy` if set). It asks for no scopes (public
identity only), uses GitHub's token for one `GET /user` and discards it. Pushing and pull requests do not use
this login: they run as the server's user with its own `git` / `gh` credentials.

### `[auth.google]` — Continue with Google (optional)

Adds a **Continue with Google** button (OpenID Connect). Enabled when `client_id`, `client_secret` and
`allowed_emails` are set. Google identifies you by email: only **verified** addresses in `allowed_emails` get in.

| Key | Default | Meaning |
| --- | --- | --- |
| `client_id` | — | OAuth client ID (type *Web application*) from Google Cloud. |
| `client_secret` | — | Its client secret (or `WAW_GOOGLE_CLIENT_SECRET`, which wins). |
| `allowed_emails` | `[]` | Email addresses allowed to sign in (case-insensitive). Empty = Google sign-in off. |
| `callback_url` | the address the browser used + `/api/v1/auth/google/callback` | Must equal an *Authorized redirect URI* of the client. |

Setup, once: [Google Cloud console](https://console.cloud.google.com/) → *APIs & Services* → *OAuth consent screen*
(external or internal, scope `email`), then *Credentials → Create credentials → OAuth client ID → Web application*,
with *Authorized redirect URI* = your address + `/api/v1/auth/google/callback`. Note: Google accepts plain `http://`
redirect URIs only for `localhost`; for an address such as `http://10.0.0.5:8765` serve the app over HTTPS with a host
name (DEPLOYMENT.md §9) or reach it through an SSH tunnel as `http://localhost:8765`.

```toml
[auth.google]
client_id = "1234-abc.apps.googleusercontent.com"
client_secret = "GOCSPX-…"   # or export WAW_GOOGLE_CLIENT_SECRET
allowed_emails = ["me@example.com"]
```

The app asks for `openid email` only, reads the email once from Google's userinfo endpoint and discards the token.

## `[agents]`

| Key | Default | Meaning |
| --- | --- | --- |
| `default_provider` | auto-detected: `opencode` if on PATH, else `codex`, else empty | Provider used when a document has no session yet. Empty → discussions are disabled with an explanation; reader/terminal still work. |
| `env_script` | — | A shell script sourced (bash) for every agent process the app starts — turns, model lists, session lists — e.g. one that exports API tokens (`AWS_BEARER_TOKEN_BEDROCK`). Re-read whenever the file changes, so a rotated token applies without a restart. SSH machines get its variables only when they opt in (`forward_env` in `[[development.hosts]]` or the machine's **Environment** in the app). |

### `[agents.opencode]`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Register the adapter. |
| `mode` | `"cli"` | `cli`: `opencode run --format json`. `server`: HTTP API of a running `opencode serve` (`server_url`). |
| `server_url` | — | e.g. `"http://127.0.0.1:4096"`; required for `mode = "server"`. |
| `bin` | `opencode` on PATH | Explicit executable path. |
| `profile` | — | Passed as `--agent NAME`. |
| `extra_args` | `[]` | Appended to every `opencode run` (e.g. `["--model", "provider/model"]`). |

### `[agents.codex]`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Register the adapter. |
| `sandbox` | `"workspace-write"` | Sandbox for **app-driven** turns only: `read-only` \| `workspace-write` \| `danger-full-access`. Passed as `-s` (new) / `-c sandbox_mode=…` (resume). Your interactive `codex` config is unaffected. |
| `bin` | `codex` on PATH | Explicit executable path. |
| `profile` | — | Passed as `--profile NAME` (from `~/.codex/config.toml`). |
| `extra_args` | `[]` | Appended to every `codex exec`. |

`CODEX_HOME` (environment) is honoured for session listing.

## `[development]` — PR Development

A development workspace uses the built-in **web editor** (default; per workspace) or **VS Code**. The web editor needs
no configuration; for language intelligence install `clangd` and a Python language server (`basedpyright`,
`pyright` or `python-lsp-server`) on the server and on every SSH host — per workspace, **Settings** can point to
specific executables (`clangd`, `python_lsp`). The VS Code option is [code-server](https://coder.com/docs/code-server/install)
started by the app on a private socket and shown through the app's login; install it on the server and on every SSH
host where you want it.

| Key | Default | Meaning |
| --- | --- | --- |
| `code_server` | `code-server` on PATH | Editor executable on this server. |
| `worktrees_dir` | `<state_dir>/dev/worktrees` | Where the app's Git worktrees go (on this server). |
| `extensions` | clangd, CodeLLDB, Python, debugpy, basedpyright, Ruff | Open VSX ids installed by **Install language support** (into the app's own editor profile, not your VS Code). |
| `ssh` | `ssh` | SSH client. Hosts are reached with `-o BatchMode=yes`: set up key authentication (and `~/.ssh/config` aliases) first. |
| `release_base` | `"https://github.com"` | Where machine setup downloads opencode, code-server, gh and Codex from: GitHub, or a mirror with the same layout (`<base>/<owner>/<repo>/releases/latest` redirecting to `…/tag/<tag>`, archives under `…/releases/download/<tag>/`). |

### `[[development.hosts]]` — develop on another machine

The worktree, the editor, the agent's CLI (opencode / Codex) and builds/tests all run on the host; the app reaches
the editor with `ssh -L` (a Unix socket) and the editor's integration extension reaches the app with `ssh -R`. The
reference wiki is copied there read-only (`<work_dir>/wiki/<folder id>`, refreshed when older than 10 minutes before
a turn, or from the workspace settings). OpenCode must be in `mode = "cli"` for remote turns; turns run the host's
own CLI, so this server needs none. **Machines → Set up** installs the tools the app uses there (see the user guide).

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | — (required) | Shown in the app (letters, digits, `-_.`). |
| `ssh` | — (required) | Destination: `me@build-box` or a `~/.ssh/config` alias. |
| `kind` | `"machine"` | `machine`, or `bastion`: a jump host other hosts are reached through (not offered for work itself). |
| `via` | `[]` | Bastions (their `name`s, `kind = "bastion"`) the host is reached through, first hop first, e.g. `["corp-jump", "lab-jump"]`. |
| `port` | `22` (or `~/.ssh/config`) | SSH port. |
| `user` | from `ssh` / `~/.ssh/config` | Login name. |
| `identity_file` | ssh's default keys | Private key on this server to log in with. |
| `ssh_args` | `[]` | Extra ssh options, e.g. `["-o", "Compression=yes"]`. |
| `work_dir` | `~/.cache/wiki-agent-workspace` | Remote folder for worktrees, editor profile, sockets and wiki snapshots. |
| `code_server`, `opencode`, `codex` | names on the remote PATH | Executables on the host. `<work_dir>/bin` (where machine setup installs them) comes first on PATH for everything the app runs there. |
| `forward_env` | `false` | Give the host the variables `[agents] env_script` sets (API tokens) — all of them, or only those in `env_vars`. Variables that only make sense here (`PATH`, `HOME`, `LD_*`, `XDG_*`, …) are left out unless named in `env_vars`. |
| `env_vars` | `[]` | Only these variables (taken from the env script, else from this server's environment), e.g. `["AWS_BEARER_TOKEN_BEDROCK"]`. |
| `forward_proxy` | `false` | Give the host this server's `http_proxy`, `https_proxy`, `all_proxy` and `no_proxy` (`no_proxy` gains `localhost,127.0.0.1,::1`). |
| `ca_bundle` | — | A PEM file on this server with extra CA certificates (a corporate proxy's). Copied to `<work_dir>/ca-extra.pem`; `<work_dir>/ca.pem` is the host's own bundle plus it, used as `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` and `GIT_SSL_CAINFO` (`NODE_EXTRA_CA_CERTS` gets the extra file). |

Forwarded values go to builds/tests, terminals, the editor and agent turns on the host, never on a command line:
they are written over ssh into a 0600 file under `<work_dir>/run` that the command sources and deletes before it
starts. Values of 8 characters or more show as `****` in the logs the app keeps of runs and setup jobs.

Hosts can also be added in the app (Machines view); those are stored in the database and listed together with the
ones here (which are shown read-only there; on a name clash the config file wins). Commands to a host share one
connection (OpenSSH `ControlMaster`, sockets under `<state_dir>/ssh/cm`, open for 10 minutes after the last use), and
each bastion of a `via` chain keeps its own. Hosts of the config file keep your own ssh host-key settings; machines
added in the app accept only host keys confirmed in the app (stored in `<state_dir>/ssh/known_hosts`, checked after
`~/.ssh/known_hosts`).

### `.waw/bench.toml` — benchmarks of a repository

Not part of the config file: a file in the repository (read from the workspace's worktree) with one `[[bench]]`
table per benchmark. The same table can be kept in the app (**Benchmarks → New**, for a workspace or for every
workspace of a repository); on a name clash the file wins. How runs, comparisons and A/B work: USER_GUIDE.md.

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | — (required) | Letters, digits, `-_.` (up to 64). |
| `command` | — (required) | Shell command run in the worktree, once per warmup and repeat. |
| `setup` | — | Run once before; skipped while the commit, the uncommitted changes and the setup itself are unchanged. |
| `warmup` | `0` | Runs whose results are dropped (0–20). |
| `repeat` | `5` | Measured runs (1–100). |
| `timeout` | `1800` | Seconds, per command. |
| `metrics` | `[]` | `{ name, json = "$.path" \| regex = "…( group )…", better = "lower" \| "higher", unit }`: from JSON in the output, or the last regex match. `wall_s` (how long each run took) is always recorded. |
| `env` | `{}` | Extra environment variables. |
| `exclusive` | `true` | Hold the whole machine: no build, test or other benchmark there meanwhile. |
| `gpus` | `[]` | With `exclusive = false`: only benchmarks sharing one of these GPUs wait for each other. |
| `threshold` | `5` | % change that counts as a regression or improvement (and at least 2 standard deviations). |

### `[development.completion]` — AI inline completion (optional)

Ghost-text suggestions in the editor come from one short request to an **OpenAI-compatible** `/chat/completions`
endpoint (the agent CLIs are too slow for keystrokes). Off until `base_url` and `model` are set.

| Key | Default | Meaning |
| --- | --- | --- |
| `base_url` | — | e.g. `"https://bedrock-runtime.us-east-2.amazonaws.com/openai/v1"`. |
| `model` | — | A fast model, e.g. `"openai.gpt-oss-20b"`. |
| `api_key_env` | — | Name of the environment variable with the key (the key itself is never stored in the config). |
| `max_tokens` | `128` | Longest suggestion. |
| `timeout` | `8` | Seconds. |

```toml
[development]
# code_server = "/opt/code-server/bin/code-server"

[[development.hosts]]
name = "corp-jump"
kind = "bastion"
ssh = "jump.example.com"

[[development.hosts]]
name = "gpu-box"
ssh = "me@gpu-box"
via = ["corp-jump"]
port = 22
user = "me"
identity_file = "~/.ssh/id_ed25519"
work_dir = "~/.cache/wiki-agent-workspace"
forward_env = true
env_vars = ["AWS_BEARER_TOKEN_BEDROCK"]
forward_proxy = true
# ca_bundle = "/etc/ssl/certs/corp-proxy-ca.pem"

[development.completion]
base_url = "https://bedrock-runtime.us-east-2.amazonaws.com/openai/v1"
model = "openai.gpt-oss-20b"
api_key_env = "AWS_BEARER_TOKEN_BEDROCK"
```

## `[github.rate]` — GitHub API rate limits

Thresholds and waits of the central GitHub rate-limit management; defaults and meaning in [GITHUB_RATE_LIMITS.md](GITHUB_RATE_LIMITS.md#configuration).

## Full example

```toml
[server]
bind = "0.0.0.0"
port = 8765
state_dir = "/home/me/.local/share/wiki-agent-workspace"
cookie_secure = false
allowed_origins = []
trusted_proxies = []
gateway_auth = false
gateway_user_header = ""
session_days = 30
log_level = "INFO"

[workspace]
root = "/home/me/my-wiki"
max_document_bytes = 20971520

[[workspaces]]
root = "/home/me/notes-wiki"
name = "Notes"

[terminal]
tmux_history_limit = 10000

[office]
max_parallel = 2
timeout = 180
max_bytes = 209715200
cache_mb = 2048

[auth]
access_token = "…generated…"
server_secret = "…generated…"

[agents]
default_provider = "opencode"

[agents.opencode]
enabled = true
mode = "cli"
extra_args = []

[agents.codex]
enabled = true
sandbox = "workspace-write"
extra_args = []
```

## Running several wikis

Usually: one server, several folders — add them with **＋ Add folder** or list them under `[[workspaces]]`. They share
the login, terminals and preferences but nothing else.

Alternatively run one completely separate instance per wiki (own database, token and tmux server): separate
`--config`, `--port` and `--state-dir`:

```bash
WAW_CONFIG=~/.config/waw-docs.toml  scripts/setup.sh --workspace ~/docs  --port 8765 --state-dir ~/.local/share/waw-docs
WAW_CONFIG=~/.config/waw-notes.toml scripts/setup.sh --workspace ~/notes --port 8766 --state-dir ~/.local/share/waw-notes
WAW_CONFIG=~/.config/waw-docs.toml  scripts/start.sh &
WAW_CONFIG=~/.config/waw-notes.toml scripts/start.sh &
```
