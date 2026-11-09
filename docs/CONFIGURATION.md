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

Setup, once: on GitHub, **Settings → Developer settings → OAuth Apps → New OAuth App**. *Homepage URL*: the
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
name (DEPLOYMENT.md §8) or reach it through an SSH tunnel as `http://localhost:8765`.

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
