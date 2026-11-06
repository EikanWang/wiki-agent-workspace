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
| `docs/SCHEMA.md` | Column-by-column reference of the SQLite database (checked by a test) |
| `frontend/` | React + TypeScript + Vite UI (markdown-it + DOMPurify reader, xterm.js terminal) |
| `tests/backend/` | pytest suite (real Git + real tmux + controllable fake `opencode`/`codex` executables) |
| `tests/frontend/` | Playwright browser tests against the production build |
| `tests/fakes/` | `fake-opencode`, `fake-codex`: emit the providers' documented JSON protocols without credentials |
| `scripts/setup.sh`, `scripts/start.sh`, `scripts/test.sh` | one setup command, one start command, test runner |
| `config.example.toml`, `requirements.lock`, `frontend/package-lock.json` | configuration template and dependency locks |
| `docs/` | deployment, configuration, operations, schema and architecture guides (index below) |

## Documentation

| Guide | Read it when… |
| --- | --- |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | you are installing on a new machine: prerequisites with install commands, setup, configuration decisions, service unit, remote access (tunnel / HTTPS proxy), provider login, upgrade, uninstall |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | you need the meaning and default of every `config.toml` key and environment variable, or want several wikis on one host |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | day-to-day: logs, backup/restore, moving the state directory, maintenance, and a troubleshooting table of every error/banner the app can show |
| [docs/SCHEMA.md](docs/SCHEMA.md) | you want to query or understand the SQLite database (every table and column, kept in sync by a test) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | you are changing the code: module map, request/turn flow, text-model rules, security boundaries, dev workflow, how to add a migration or provider |
| [DESIGN.md](DESIGN.md) | the behavioural specification the implementation follows |

## Requirements (server)

- Linux, the **same OS user** that owns the wiki checkout and the agent logins (never root)
- Python 3.11–3.13, Git, tmux 3.2+ (3.5a tested), Node.js 20+ (22 recommended; only needed to build the frontend)
- Optional: `opencode` and/or `codex` on `PATH`, already logged in. Without them the reader and terminal still work.

`scripts/setup.sh` does **not** install system packages; it names any missing dependency and how to install it.
Install commands per distro, the recommended `fs.inotify.max_user_watches` increase for large wikis, and a
step-by-step walkthrough are in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Setup (one command) and start (one command)

```bash
git clone https://github.com/EikanWang/wiki-agent-workspace.git && cd wiki-agent-workspace
scripts/setup.sh --workspace /path/to/your/wiki        # creates .venv, builds the UI, writes config, prints the token ONCE
scripts/start.sh                                        # http://127.0.0.1:8765
```

`setup.sh` writes `~/.config/wiki-agent-workspace/config.toml` (mode 0600) containing the generated 32-byte access
token and server secret, and creates `~/.local/share/wiki-agent-workspace/` for the SQLite database, document
snapshots and the app-owned tmux configuration — all outside your Git checkout. Options:

```
scripts/setup.sh --workspace DIR [--port 8765] [--bind 127.0.0.1] [--state-dir DIR] [--default-provider opencode|codex]
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
- The discussion panel has two modes: **Individual** (one discussion at a time, with its diff and anchor tools) and
  **Flat** (all discussions of the document as one chronological stream, grouped by quoted passage; the composer
  continues the chosen discussion — default the newest — so related questions flow without switching).
- The selection box has a **Copy** button (Ctrl/⌘+C works too — opening the box never clears the selection).
- **Model switching**: each agent session has a model (and reasoning effort) used for its future turns — pick it in
  the session dialog or next to the session in the discussion panel (`opencode models` list / Codex config +
  common ids, or type any id). Past answers keep the model they were produced with.
- In every question/reply box **Enter sends** and **Ctrl/⌘+Enter** (or Shift+Enter) inserts a new paragraph; Enter
  during IME composition never sends.
- Exited terminals (shell ended) stay visible as `(exited)` so nothing is silently replaced; dismiss them with `×`
  or **Clear exited**. Live terminals ask once before being closed.

### File types, locks and diff review

- The file tree lists every file with a kind-specific icon (folder, Markdown, image, PDF, notebook, code, script,
  data, text, archive, binary, symlink badge, broken link). The reader opens Markdown, any text/source file
  (C/C++, Python, YAML, XML, JSON, shell, …, shown as numbered code with the same select-and-ask workflow),
  Word `.docx` (converted to HTML) and PDF (browser viewer; discussions attach to the document).
- **Lock** a file or folder (🔒 in the tree or the header toggle): the app and app-driven agent turns treat it as
  read-only — preview Apply, relink and history deletion are refused (`423 PATH_LOCKED`), the agent is told the
  path is locked, Codex runs with the `read-only` sandbox, and any change that still happens during a turn is
  reported as a `lock_violation` (never silently reverted). Your terminal is never restricted.
- **Diff** view (± next to changed files/folders, or the Diff tab): review a file, a folder or the whole workspace
  against HEAD or any commit/branch, including untracked Markdown drafts. Click a line to comment; the comment
  becomes a discussion anchored to that text, with the diff context shown and the agent answering as usual.
- **Recent** supports filtering, hiding/unhiding entries (history kept) and deleting a document's app history
  (discussions, versions, reading state — the file itself is untouched).

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

## Remote access

The server binds to `127.0.0.1:8765` by default and must never be exposed unauthenticated on a public interface.
Two supported options, neither requiring a new account — details and proxy snippets in
[docs/DEPLOYMENT.md §8](docs/DEPLOYMENT.md#8-remote-access):

- **Private network / tunnel**: `ssh -N -L 8765:127.0.0.1:8765 user@remote-host`, or `--bind` a VPN/LAN address.
- **HTTPS via an authenticated reverse proxy** (Caddy/nginx): set `cookie_secure = true`, `trusted_proxies`,
  `allowed_origins`; optional `gateway_auth` for gateways that already authenticate you.

## Where your data lives

Everything the app stores is in one self-contained, relocatable folder — the state directory
(`server.state_dir`, default `~/.local/share/wiki-agent-workspace`): `workspace.sqlite3` (see
[docs/SCHEMA.md](docs/SCHEMA.md)), `snapshots/`, `tmux-socket`, `tmux.conf`. The config file with the token lives
separately (`~/.config/wiki-agent-workspace/config.toml`). Backup, restore and moving are described in
[docs/OPERATIONS.md](docs/OPERATIONS.md); in short: stop, copy the folder, point `server.state_dir` (and
`workspace.root` if the wiki moved) at the new place, start.

## Security model (summary)

- Single access token → HttpOnly, SameSite=Strict cookie (Secure when HTTPS). Mutating requests need `X-CSRF-Token`
  (HMAC-SHA-256 of the cookie with the server secret, returned by `/bootstrap`) and a matching `Origin`. WebSockets
  require the cookie, the CSRF token as subprotocol `waw-csrf.<token>` and an allowed origin.
- Rendered Markdown is sanitized with DOMPurify (no raw HTML, scripts, event handlers or `javascript:` URLs); the API
  rejects `..` traversal everywhere and refuses to *write* through a symlink that leaves the workspace. Reading
  through such symlinks is allowed (wikis commonly link to source corpora) and the tree marks those entries;
  `Idempotency-Key` prevents duplicate threads/jobs.
- Tokens are never logged or placed in URLs; `/api/v1/health` exposes process health only; `/api/v1/export` downloads
  a backup (SQLite + snapshots) for the authenticated user.
- The terminal is the user's own shell with the user's own rights — no artificial sandbox, no per-command prompts.
  **Close terminal** (after an ordinary confirmation) is the only action that kills processes.

## Running the tests

```bash
scripts/test.sh            # backend: pytest (69 tests; real git + tmux + fake agents), ~25 s
scripts/test.sh --e2e      # + Playwright browser suite (19 scenarios) against the production build
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
