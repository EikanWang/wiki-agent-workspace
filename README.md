# Wiki Agent Workspace

A self-hosted, single-user web application that runs on the Linux machine holding your Markdown wiki and your
command-line agents (OpenCode, Codex). From any browser (desktop or phone) you get:

1. **A real remote terminal** — tmux-backed shells that survive browser disconnects and web-server restarts.
2. **A Markdown reader with anchored discussions** — select words or lines, ask an agent about *that exact text*,
   follow up, and come back months later with discussions, session links, reading position and version history intact.

Design specification: [`DESIGN.md`](DESIGN.md). Visual reference (mockup only): `INTERFACE_PROTOTYPE.html`.

## What is in this repository

| Path | Purpose |
| --- | --- |
| `backend/app/` | FastAPI server: auth, documents, anchors, agent adapters, job runner, tmux terminals, previews, watcher |
| `backend/migrations/` | Alembic migrations (applied automatically at startup) |
| `frontend/` | React + TypeScript + Vite UI (markdown-it + DOMPurify reader, xterm.js terminal) |
| `tests/backend/` | pytest suite (real Git + real tmux + controllable fake `opencode`/`codex` executables) |
| `tests/frontend/` | Playwright browser tests against the production build |
| `tests/fakes/` | `fake-opencode`, `fake-codex`: emit the providers' documented JSON protocols without credentials |
| `scripts/setup.sh`, `scripts/start.sh`, `scripts/test.sh` | one setup command, one start command, test runner |
| `config.example.toml`, `requirements.lock`, `frontend/package-lock.json` | configuration template and dependency locks |

## Requirements (server)

- Linux, the **same OS user** that owns the wiki checkout and the agent logins (never root)
- Python 3.11–3.13, Git, tmux 3.2+ (3.5a tested), Node.js 20+ (22 recommended; only needed to build the frontend)
- Optional: `opencode` and/or `codex` on `PATH`, already logged in. Without them the reader and terminal still work.

`scripts/setup.sh` does **not** install system packages; it names any missing dependency and how to install it.

## Setup (one command) and start (one command)

```bash
git clone <this repository> wiki-agent-workspace && cd wiki-agent-workspace
scripts/setup.sh --workspace /path/to/your/wiki        # creates .venv, builds the UI, writes config, prints the token ONCE
scripts/start.sh                                        # http://127.0.0.1:8765
```

`setup.sh` writes `~/.config/wiki-agent-workspace/config.toml` (mode 0600) containing the generated 32-byte access
token and server secret, and creates `~/.local/share/wiki-agent-workspace/` for the SQLite database, document
snapshots and the app-owned tmux configuration — all outside your Git checkout. Options:

```
scripts/setup.sh --workspace DIR [--port 8765] [--bind 127.0.0.1] [--default-provider opencode|codex]
                 [--codex-sandbox read-only|workspace-write|danger-full-access] [--show-token]
```

Open the printed URL in a browser, paste the token once; the login cookie lasts 30 days and is renewed while you
work. No further prompts are added for documents, messages, shell commands or edits.

Alternative token source: `WAW_ACCESS_TOKEN=... scripts/start.sh`. Other env overrides: `WAW_CONFIG`, `WAW_WORKSPACE`,
`WAW_STATE_DIR`, `WAW_BIND`, `WAW_PORT`. Edit the config file and restart to change providers (no reinstall).

## Daily workflow

1. **Terminal tab** → `+ New terminal` → run `opencode`, `codex`, `git`, editors… exactly as in SSH. Closing the
   browser only detaches the tmux client; the shell and the running agent keep going. Reopen the tab to reattach.
2. Ask the agent in the terminal to write a wiki page. The file tree updates (filesystem watcher, or `↻`) as soon as
   the file exists — no commit/push needed.
3. **Document tab** → open the page → select text → the question box appears next to the selection (desktop, ~350 ms
   after releasing the mouse; on phones use the native selection then **Ask** / **Ask about selection**).
   Choose the session (document default / new / any linked session) and send. The quoted passage, question, streamed
   answer and the session used are stored on the server.
4. Explicitly ask the agent to edit the document in a discussion → the agent edits under *its* existing permission
   configuration → the reader shows a "document changed" banner, **View diff** shows the real change, and anchors are
   re-resolved. No extra app approval dialog. Prefer to review first? **Preview change** runs the edit in an isolated
   temporary copy and shows the diff; **Apply** writes it only if the file is still at the same SHA-256.
5. Come back later: **Recent** (top bar, or the start screen) lists every document you worked on, newest activity
   first, with where you were and the last discussion; opening an entry restores the document, your reading position
   and the discussion you had open. Discussions whose text moved are re-anchored deterministically, and
   changed/duplicated passages are flagged instead of being attached to the wrong place.

### Layout and appearance

- `Aa` opens the Appearance popover: document font (sans/serif/mono), font size, optional content-width cap (the
  document fills the available width by default), terminal font size.
- The file tree (`☰`) and the discussion panel (`›`) fold; both are resizable by dragging their edge; the outline
  folds with `▾ Outline`. The document area always expands into the freed space. Settings persist per browser.
- Links to sections (`#heading`, `page.md#heading`) scroll inside the reader; heading ids use GitHub's slug rules.
- Agent replies are rendered as sanitized Markdown; your own messages stay verbatim.
- Exited terminals (shell ended) stay visible as `(exited)` so nothing is silently replaced; dismiss them with `×`
  or **Clear exited**. Live terminals ask once before being closed.

### Agent sessions

- Each document has one default session (header). The first question creates it with the default provider; `+ New
  session` creates a new one for future discussions only. Each discussion remembers its last session; every message
  stores the session actually used. The header also lets you **link** an existing provider session (e.g. the terminal
  conversation that wrote the page) from the provider's list or by id — linking succeeds only if the provider confirms
  the id. OpenCode ids are never sent to Codex or vice versa.
- Jobs on the same session are serialized (`waiting for session`); you can cancel, retry with the same or another
  session, and provider errors (`PROVIDER_AUTH_REQUIRED`, `SESSION_NOT_FOUND`, `SESSION_BUSY`, …) are shown with the
  provider's own message and a hint. Your question is never lost.

### Providers

| Provider | How app-driven turns run | Notes |
| --- | --- | --- |
| OpenCode (`mode = "cli"`, default) | `opencode run --format json --dir WS [--session ID] <prompt>` | Uses your existing login/config. JSON events carry whole text parts (not token deltas). Permission prompts cannot be relayed in CLI mode; OpenCode applies its configured rules. |
| OpenCode (`mode = "server"`) | HTTP API of a known `opencode serve` instance (`server_url`) | Streams deltas, relays real permission requests (`permission.v2.asked` → reply endpoint), interrupts the app's own turn. Start `opencode serve --port 4096` once and attach your terminal TUI with `opencode --attach http://127.0.0.1:4096` so both share one backend. A separate `opencode serve` does **not** attach to a TUI's own server. |
| Codex | `codex exec --json -C WS -s <sandbox> -` / `codex exec resume --json -c sandbox_mode="<sandbox>" ID -` (prompt on stdin) | Saved CLI login reused. `agents.codex.sandbox` (default `workspace-write`, set at setup) is applied to app-driven turns only; your interactive `codex` config is untouched. Sessions are listed from `$CODEX_HOME/session_index.jsonl` and rollout files. Codex emits whole messages, not deltas. |

All processes are started with argument arrays; document text never passes through a shell string.

## Remote access (two options; none requires a new account)

The server binds to `127.0.0.1:8765` by default and must never be exposed unauthenticated on a public interface.

**Option A — private network / tunnel (simplest).** Reach the loopback port over something you already have:

```bash
# SSH tunnel from your laptop
ssh -N -L 8765:127.0.0.1:8765 user@remote-host     # then open http://127.0.0.1:8765
```
or bind to a VPN/Tailscale/WireGuard address: `scripts/setup.sh --bind 100.64.0.5` (set `cookie_secure = false`
unless that path is HTTPS). The token login still applies.

**Option B — HTTPS through an authenticated reverse proxy.** Example with Caddy (terminates TLS, forwards WebSockets):

```
wiki.example.internal {
    basic_auth { you <hash from `caddy hash-password`> }   # optional second factor in front of the token login
    reverse_proxy 127.0.0.1:8765
}
```
Then in `config.toml`: `cookie_secure = true`, `trusted_proxies = ["127.0.0.1"]`, and add the public origin to
`allowed_origins` if the proxy rewrites `Host`. If the gateway already authenticates the user and sets a header
(e.g. Authelia/oauth2-proxy → `X-Forwarded-User`), you may set `gateway_auth = true` and `gateway_user_header =
"X-Forwarded-User"`; only requests from `trusted_proxies` carrying that header skip the token login.

An optional user-level systemd unit (not required):

```ini
# ~/.config/systemd/user/wiki-agent-workspace.service
[Service]
ExecStart=%h/wiki-agent-workspace/scripts/start.sh
Restart=on-failure
[Install]
WantedBy=default.target
```
`systemctl --user enable --now wiki-agent-workspace` (run `loginctl enable-linger $USER` to keep it alive after logout).

## Where your data lives, and how to move it

Everything the app stores is in **one self-contained folder**, the state directory (`server.state_dir`, default
`~/.local/share/wiki-agent-workspace`), separate from the wiki checkout and from the config file:

```
<state_dir>/
├── workspace.sqlite3      # documents, discussions (threads/messages), agent sessions, jobs, reading state,
│                          # review marks, previews, terminal tabs, login sessions
├── snapshots/             # exact bytes of every document version a discussion or preview refers to
├── tmux-socket            # name of the app-owned tmux socket (kept across moves so terminals reconnect)
├── tmux.conf              # generated tmux config for that socket
└── server.log             # if you redirect the server output there (scripts/start.sh does not by default)
```

All references inside the database are relative to this folder, so to relocate or clone your setup:

1. Stop the server (`Ctrl-C` / `pkill -f "backend.app.cli serve"`); a clean stop folds the SQLite write-ahead log
   into `workspace.sqlite3`. (If you must copy while running, use the **Backup** button / `GET /api/v1/export`,
   which produces a consistent `tar.gz` of the database and `snapshots/`, and extract that instead.)
2. Copy the folder: `cp -a ~/.local/share/wiki-agent-workspace /new/place/state`.
3. Edit the config file (`~/.config/wiki-agent-workspace/config.toml`, or any file passed with `--config` /
   `WAW_CONFIG`): set `server.state_dir = "/new/place/state"` and, if the wiki checkout moved too,
   `workspace.root = "/new/place/wiki"`. Copy the config file as well if you move to another machine — it holds
   the access token and the server secret (your login cookie stays valid only with the same secret).
4. `scripts/start.sh`. Documents, discussions, reading positions, version history and *Recent* are all back. If
   `workspace.root` changed, the single workspace record is relocated to the new path automatically (logged at
   startup), so document identities and discussions follow the moved checkout. Live tmux terminals survive a
   move on the same host because the socket name is stored in the folder.

`wiki-agent-workspace print-config` (via `.venv/bin/python -m backend.app.cli print-config`) shows the effective
paths with secrets redacted.

## Security model (summary)

- Single access token → HttpOnly, SameSite=Strict cookie (Secure when HTTPS). Mutating requests need `X-CSRF-Token`
  (HMAC-SHA-256 of the cookie with the server secret, returned by `/bootstrap`) and a matching `Origin`. WebSockets
  require the cookie, the CSRF token as subprotocol `waw-csrf.<token>` and an allowed origin.
- Rendered Markdown is sanitized with DOMPurify (no raw HTML, scripts, event handlers or `javascript:` URLs); the API
  rejects path traversal and symlinks escaping the workspace; `Idempotency-Key` prevents duplicate threads/jobs.
- Tokens are never logged or placed in URLs; `/api/v1/health` exposes process health only; `/api/v1/export` downloads
  a backup (SQLite + snapshots) for the authenticated user.
- The terminal is the user's own shell with the user's own rights — no artificial sandbox, no per-command prompts.
  **Close terminal** (after an ordinary confirmation) is the only action that kills processes.

## Running the tests

```bash
scripts/test.sh            # backend: pytest (51 tests; real git + tmux + fake agents), ~25 s
scripts/test.sh --e2e      # + Playwright browser suite (13 scenarios) against the production build
# or individually
.venv/bin/python -m pytest tests/backend -q
cd frontend && npx playwright install chromium && npm run test:e2e
```

Mapping to the acceptance tests in DESIGN §17: A1–A3 `test_auth.py`, `test_documents.py`, `test_threads.py`
(restart, missing provider); B4–B8 `test_threads.py`, `test_anchors.py`, `test_documents.py`, browser spec B4/B5/B6/B8;
C9–C11 `test_terminal.py` (real tmux: disconnect/reconnect/restart, resize, Ctrl-C, 256/true colour, Unicode, binary
input, full-screen `less`, two terminals, killed session → `exited`) and browser spec C9/C10; D12–D13
`test_edits_previews.py`, `test_auth.py`, browser spec D12/D13.

## Manual checklist with real OpenCode / Codex installations

Record the results on your own host (this is what was verified on the implementation machine, OpenCode 1.18.31,
Codex CLI 0.155.1, tmux 3.5a, Python 3.13, Node 20.18):

- [x] `opencode run --format json` turn from the reader: lazy native session id (`ses_…`) bound on the first event, follow-up resumed with `--session`, answer stored. *(verified here)*
- [x] `opencode session list --format json` populates the "link existing session" list. *(verified here)*
- [x] `opencode serve` API probed: `/api/session`, `/api/session/{id}/prompt` (200 = accepted, not completed), `/api/event` SSE with `session.next.text.delta`, `session.next.step.failed`, `session.idle`, `permission.v2.asked`. Adapter written against these; **a successful end-to-end server-mode turn and TUI/app concurrency on one server were not verified** (this host's headless server lacked a valid model credential).
- [x] `codex exec --json`: `thread.started` id captured, a real provider authentication error (HTTP 401) surfaced verbatim as `PROVIDER_AUTH_REQUIRED` instead of a fake success. *(verified here)* A successful Codex answer/edit with a working login, and `codex exec resume` on a real session, still need to be confirmed on your host.
- [ ] Your terminal: true colour (`printf '\e[38;2;255;100;0mRGB\e[0m\n'`), 256 colours, a full-screen TUI (`htop`, `vim`, `opencode`), IME input, scrolling into tmux history (mouse wheel → copy mode, `q` to leave), font rendering on your phone.
- [ ] Reconnect to a running `opencode`/`codex` TUI after closing the browser; restart `scripts/start.sh` while it runs.
- [ ] If you use OpenCode server mode: respond to a real permission request from the discussion panel.

## Known limitations / honest notes

- If the host's inotify watch limit is exhausted (other long-running tools such as an agent's own file watcher can hold hundreds of thousands of watches), the watcher
  reports it in the UI banner; fix with `sudo sysctl -w fs.inotify.max_user_watches=1048576` (persist it under
  `/etc/sysctl.d/`) and restart. Manual refresh keeps working meanwhile.
- The filesystem watcher runs in a child process; on large trees (e.g. a `raw/` corpus with many thousands of files)
  registering can take a minute. The UI shows a banner until it is ready and the `↻` button always works meanwhile.

- Browser xterm scrollback does not survive a page reload; use **tmux history** (snapshot) or scroll inside tmux. A
  restored screen is the current tmux screen, not a guaranteed lossless replay.
- Server-side anchor validation uses `markdown-it-py` with the same text-model rules as the browser; if a renderer
  edge case produces different offsets, the server accepts the thread when the quote occurs exactly once and the
  browser re-resolves highlights locally using the same algorithm.
- Preview mode copies the workspace (ignoring `.git`, `node_modules`, `.venv`; limit 512 MiB); larger trees return
  `PREVIEW_UNAVAILABLE` and the direct-edit flow remains available.
- Codex `exec` cannot relay interactive approvals; actions requiring approval fail visibly with the provider's message
  and the terminal is offered as the fallback.
- Node 22+ is recommended by the design; the frontend was built here with Node 20.18 (Vite 5 supports both).
