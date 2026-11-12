# Wiki Agent Workspace

A self-hosted, single-user web application that runs on the Linux machine holding your Markdown wikis, your code
and your command-line agents (OpenCode, Codex, Claude Code). From any browser — desktop or phone — you get:

- **Wiki** — read Markdown, Office and PDF documents; select a passage and ask an agent about *that exact text*;
  discussions follow the passage across edits; chat with a whole wiki; edit, review the diff, commit, push.
- **Local files** — browse and edit any folder or file on the server, like a file manager with VS Code's editor.
- **PR Review** — the GitHub pull requests waiting for your review, grouped by repository and by where you stand;
  review in the app, with an agent that suggests comments you apply or discard.
- **PR Development** — develop a new pull request or update an existing one: a web IDE (or VS Code), the agent
  working in the code with a wiki as its reference, build / test, diff, commit, push, CI.
- **Agents** — monitor every OpenCode, Codex and Claude Code session on the machine: who needs you, errors, continue
  any of them.
- **Terminal** — real tmux-backed shells that survive browser disconnects and server restarts.

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/EikanWang/wiki-agent-workspace/main/scripts/install.sh | bash
```

One command from an empty Linux account: it installs what is missing (with `sudo` if you allow it, otherwise
user-level Python and Node.js in your home folder), gets the code, creates your wiki folder if needed, builds
everything, starts the server as a user service, checks it and prints the **URL** and the **access token**.
In a clone: `scripts/install.sh` (`--yes` to ask nothing; `--help` for the options). Then:

1. Open the URL (another machine: `ssh -N -L 8765:127.0.0.1:8765 you@server`, then `http://127.0.0.1:8765`) and
   paste the token.
2. Log in to your agents once if needed: `opencode auth login`, `codex login`.
3. For PR Review / PR Development: `gh auth login`, then in the app: account menu → **Set up “Continue with
   GitHub”…** (one click).
4. Anything missing? `scripts/doctor.sh` checks every feature and prints the fix.

Day to day: `scripts/service.sh status | logs | restart | stop`. Step-by-step, manual install, HTTPS, upgrades:
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). How to use each feature: [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

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
| `tests/fakes/` | `fake-opencode`, `fake-codex`, `fake-claude`, `fake-code-server`, `fake-lsp`, `fake-ssh`: the external programs' protocols, without credentials |
| `scripts/install.sh` | one-click install from an empty account (prerequisites, code, wiki folder, setup, service, check) |
| `scripts/setup.sh`, `scripts/start.sh` | Python environment + UI build + configuration; start in the foreground |
| `scripts/service.sh`, `scripts/doctor.sh` | run in the background as a user service (status, logs, restart…); check every feature with fixes |
| `scripts/test.sh` | test runner |
| `config.example.toml`, `requirements.lock`, `frontend/package-lock.json` | configuration template and dependency locks |
| `docs/` | deployment, configuration, operations, schema and architecture guides (index below) |

## Documentation

| Guide | Read it when… |
| --- | --- |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | you want to know how to use a feature: folders, reading, asking, discussions, models, locks, diff, reviews, commits, Recent, terminal, keyboard |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | you are installing on a new machine: the one-click installer and its options, prerequisites with install commands (also without root), manual install, configuration decisions, running as a service, agents, GitHub (PR Review / PR Development), PR Development tools, remote access (tunnel / HTTPS proxy), upgrade, uninstall, troubleshooting |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | you need the meaning and default of every `config.toml` key and environment variable, or want several wikis on one host |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | day-to-day: logs, backup/restore, moving the state directory, maintenance, and a troubleshooting table of every error/banner the app can show |
| [docs/SCHEMA.md](docs/SCHEMA.md) | you want to query or understand the SQLite database (every table and column, kept in sync by a test) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | you are changing the code: module map, request/turn flow, text-model rules, security boundaries, dev workflow, how to add a migration or provider |
| [DESIGN.md](DESIGN.md) | the behavioural specification the implementation follows |

## Requirements (server)

- Linux, the **same OS user** that owns the wiki and the agent logins (never root)
- Python 3.11–3.13, Git, tmux 3.2+ (3.5a tested), Node.js 20+ (22 recommended; only to build the UI) —
  `scripts/install.sh` installs what is missing
- Optional: `opencode` / `codex` (agents), `claude` (Agents monitor), `gh` (PR Review, PR Development), LibreOffice
  (original layout of Word / PowerPoint / Visio), `clangd` / `basedpyright` / code-server (PR Development)

The full table with install commands per distro, and the no-root alternatives: [docs/DEPLOYMENT.md §1](docs/DEPLOYMENT.md#1-prerequisites).

## Manual setup

```bash
git clone https://github.com/EikanWang/wiki-agent-workspace.git && cd wiki-agent-workspace
scripts/setup.sh --workspace /path/to/your/wiki        # creates .venv, builds the UI, writes config, prints the token ONCE
scripts/service.sh install                              # background user service (or scripts/start.sh in the foreground)
scripts/doctor.sh                                       # check everything
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

1. **Terminal** → **+** → run `opencode`, `codex`, `git`, editors… exactly as in SSH. Closing the browser only
   detaches the tmux client; the shell and the running agent keep going.
2. Ask the agent in the terminal to write a wiki page. It shows up in the file tree as soon as the file exists — no
   commit/push needed.
3. **Wiki** → open the page → select text → ask. The quoted passage, question, streamed answer and the session
   used are stored on the server; discussions follow the passage across edits.
4. Ask the agent to edit in a discussion (or **Preview change**), then review in **Diff** — comment on lines, stash
   comments into a **review** the agent triages in one go — and **Commit…** / push / open a pull request.
5. **PR Review** shows what waits for you; **Agents** shows which agent sessions need you.
6. Come back later: **Recent** restores the document, your reading position and the discussion you had open.

### Feature overview

Everything below is explained step by step in **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)**.

| Area | What you get |
| --- | --- |
| PR Review | GitHub review queue next to the wikis (owner's GitHub sign-in only): PRs requesting your review across all repos, grouped by repo and by where you stand (not reviewed yet / new activity since your comments / nothing new), ★ priority, CI/draft/size, filter, sort, auto-refresh interval; "🤖 agent reviewed" filter; a PR opens as a tab (overview, checks, reviews; conversation, all reviewers' threads with resolve, viewed per file, line / file comments and suggestions published as one review, replies); agent reviews become suggested comments you apply or discard, via the server's `gh` login |
| PR Development | Develop a new pull request, update an existing one, or work in a local checkout / folder directly (create and switch branches): an app-owned Git worktree, the built-in VS Code-like web editor (Monaco + clangd / basedpyright language servers: completion, go to definition, references, rename, diagnostics; explorer with per-file/folder diff, search, problems, quick open) or VS Code (code-server); AI inline completion; the agent working in the code with a whole wiki folder as its reference; build / test runs with "Fix with the agent"; a Changes view like the wiki's Diff (per-file diffs, side by side, compare a commit or the branch, ask about lines) with commit and push; create PR, address review comments, fix failing CI; on this server or an SSH host; resizable / foldable panes |
| Agents | Monitor OpenCode, Codex and Claude Code sessions (also those started in a terminal): needs you / errors / working / idle, alarms and notifications, a session on stage with its conversation, continue it, rename, delete; sessions of chosen folders monitored automatically |
| Local files | Any folder or file on the server: open folders, new / rename / delete, VS Code's editor with conflict-safe saves, Markdown preview, images |
| Folders | Several wiki folders in one window ("add folder", like VS Code), each fully isolated: documents, discussions, sessions, locks, reviews, Git state; named workspaces (sets of folders with their own tabs and terminals); create folders on the server, clone a repository by URL, close any folder for good, files/folders in the tree |
| Reading | Markdown, any text/source file (numbered code), Word, PowerPoint, Visio in their original layout (LibreOffice) plus a selectable text view (`.docx`, `.pptx`, `.vsdx`), PDF; kind icons; section links; Read / Discuss modes; reading position restore; tabs for open documents (switch, close, reorder, across folders) |
| Asking | Select words or lines → question box (copy icon, Enter to send), ask about a page, slide, area or picture of a PDF / Office document (sent to the agent as an image), or ask about the whole document from the discussions panel (any format, images attached for the agent); anchors survive edits; orphaned/ambiguous detection and manual reanchor; review marks |
| Discussions | Individual or Flat (all discussions as one stream) panel; streaming answers rendered as Markdown; retry / cancel |
| Chats | Talk with a whole wiki, a folder or a file; distill the conversation into a page or fold it back into the pages it came from |
| Models | OpenCode and Codex with your existing login; per-session **model and effort**, another model for one discussion or chat; link provider sessions; per-session job queueing; skills from connected folders |
| Editing | Edit Markdown and any text/source file in the browser: Ctrl/⌘+S, live Markdown preview, conflict-safe saves, versions + diff |
| Safety | **Locks** on files/folders (read-only for the app and app-driven agents); preview-before-apply; no auto-commit, no force-push |
| Diff | File / folder / whole-folder diffs vs any ref; unified or side-by-side; multi-line comments; inline answers; **Viewed** checkbox |
| Reviews | Stash comments, **Submit review** once; the agent addresses / replies / skips each comment |
| Git | **Commit…** selected files with an agent-drafted message, push, create a GitHub pull request |
| Recent | All folders, newest first; filter, hide, delete history |
| Terminal | tmux-backed tabs that survive disconnects and server restarts, also as their own activity (terminals in your home folder or any folder); history snapshot; exited tabs dismissable; full-screen keyboard capture and a Keys menu / sticky Ctrl for browser-reserved shortcuts (Ctrl+T, Ctrl+W…); Home/End (⌘←/⌘→ on a Mac); copy to the browser clipboard (drag via tmux, Shift+drag, Ctrl+Shift+C, Copy button, programs via OSC 52, history Copy all) |
| Layout | Activity bar with foldable lists, resizable / foldable panes everywhere, foldable outline, fonts and sizes (`Aa`) |

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
[docs/DEPLOYMENT.md §9](docs/DEPLOYMENT.md#9-remote-access):

- **Private network / tunnel**: `ssh -N -L 8765:127.0.0.1:8765 user@remote-host`, or `--bind` a VPN/LAN address.
- **HTTPS via an authenticated reverse proxy** (Caddy/nginx): set `cookie_secure = true`, `trusted_proxies`,
  `allowed_origins`; optional `gateway_auth` for gateways that already authenticate you.

## Where your data lives

Your wiki folders are only read, except for edits you ask the agent to make, previews you apply and commits you make.
Everything the app itself stores is in one self-contained, relocatable folder — the state directory
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
- PR Development's editor (code-server) has the same rights as the terminal. It listens only on a 0600 Unix socket
  with its own login disabled and is reached through `/dev-editor/<id>/` with the app's cookie (stripped before it is
  forwarded); writes and WebSockets need a same-origin `Origin`. Its pages are same-origin with the app (framed by
  the app only), so trust its extensions like programs you run in the terminal. The integration extension talks to
  `/api/v1/dev/ext/<id>/` with a random per-editor token (cookies are not accepted there). SSH hosts are reached
  with key authentication only (`BatchMode=yes`); every remote command is a shlex-quoted `sh -c` script.

## Running the tests

```bash
scripts/test.sh            # backend: pytest (181 tests; real git + tmux + fake agents), ~3 min
scripts/test.sh --e2e      # + Playwright browser suite (71 scenarios) against the production build
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
  If the watcher process dies it is restarted automatically (up to 5 times in a row) and the file tree is refreshed.

- Browser xterm scrollback does not survive a page reload; use **tmux history** (snapshot) or scroll inside tmux. A
  restored screen is the current tmux screen, not a guaranteed lossless replay.
- Server-side anchor validation uses `markdown-it-py` with the same text-model rules as the browser; if a renderer
  edge case produces different offsets, the server accepts the thread when the quote occurs exactly once and the
  browser re-resolves highlights locally using the same algorithm.
- Preview mode copies the workspace (ignoring `.git`, `node_modules`, `.venv`; limit 512 MiB); larger trees return
  `PREVIEW_UNAVAILABLE` and the direct-edit flow remains available.
- Codex `exec` cannot relay interactive approvals; actions requiring approval fail visibly with the provider's message
  and the terminal is offered as the fallback.
- PR Development: the editor is code-server, so extensions come from Open VSX (not Microsoft's marketplace); some
  browser-reserved shortcuts only work when the editor is opened in its own tab (**Open in tab**). The AI inline
  completion needs a separately configured OpenAI-compatible endpoint. Task logs, editor processes and the SSH
  tunnels end with the server (runs still marked running at startup become `interrupted`).
- Node 22+ is recommended by the design; the frontend was built here with Node 20.18 (Vite 5 supports both).
