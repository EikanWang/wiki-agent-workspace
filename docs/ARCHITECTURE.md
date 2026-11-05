# Architecture and development guide

For people maintaining or extending the code. The behavioural specification is [DESIGN.md](../DESIGN.md); this
page explains how the implementation is laid out and how the pieces talk to each other.

## Repository map

```
backend/app/
  main.py          FastAPI app factory, lifespan (migrate DB, mark interrupted jobs, reconcile tmux, detect
                   providers, start watcher), /auth, /bootstrap, /events (global SSE), /export, SPA serving
  cli.py           `setup` / `serve` / `print-config` entry points (python -m backend.app.cli …)
  config.py        TOML + env loading, Settings/ProviderConfig dataclasses, secret generation
  db.py            SQLite (WAL) engine, serialized writes (RLock), Alembic runner, WAL checkpoint
  models.py        SQLAlchemy models (schema reference: docs/SCHEMA.md)
  auth.py          token login, HttpOnly cookie, HMAC CSRF token, Origin checks, WebSocket auth, gateway mode
  deps.py          AppContext (all services) + FastAPI dependencies require_auth / require_auth_mutating
  errors.py        ApiError and the stable error codes; JSON error shape
  documents.py     DocumentService: safe paths, file tree + Git status, open/version snapshots, reading state,
                   review marks, diff, relink, recent; routes under /files, /documents, /recent, /raw
  textmodel.py     markdown-it-py rendering + the deterministic text model (mirrors frontend/src/textmodel.ts)
  anchors.py       anchor resolution algorithm (attached/moved/ambiguous/orphaned) + offset validation
  annotations.py   ThreadService: threads, messages, retry, reanchor; prompt building; /jobs routes (SSE,
                   cancel, permission decisions)
  sessions.py      SessionService: app sessions, create/link provider sessions; /agent-sessions routes
  previews.py      preview-before-apply in an isolated workspace copy; SHA-checked apply
  watcher.py       child-process filesystem watcher → global events, version recording
  events.py        in-process pub/sub (EventBus) feeding SSE
  idempotency.py   ASGI middleware for Idempotency-Key
  gitinfo.py       read-only git helpers (status/branch/commit/show)
  agents/
    base.py        adapter contract (capabilities/list/create/resume/cancel), event types, subprocess helper
    opencode.py    OpenCode CLI adapter (JSON lines) and server adapter (HTTP + SSE)
    codex.py       Codex `exec --json` adapter (new + resume), session listing from $CODEX_HOME
    registry.py    builds adapters from config, caches capabilities
    jobs.py        JobManager: per-session serialization, event persistence (seq), SSE replay, cancel,
                   permission relay, interrupted-on-restart
  terminal/
    tmux.py        app-owned tmux server (socket from <state_dir>/tmux-socket, generated config)
    pty.py         PTY wrapper around `tmux attach` with asyncio reader/writer, resize
    manager.py     terminal tabs CRUD, reconcile on startup, history snapshot, WebSocket relay
backend/migrations/   Alembic env + versions (0001_initial, 0002_active_thread, …)
frontend/src/
  api.ts           fetch wrapper (CSRF header, error shape), types, SSE subscriptions
  markdown.ts      markdown-it + DOMPurify, GitHub slugs, link rewriting (#frag, page.md#frag, images → /raw)
  textmodel.ts     DOM text model, selection → anchor, offsets ↔ DOM Range, client-side resolution
  App.tsx          state container: auth, document, threads, sessions, terminals, jobs, prefs, Recent
  components/      Login, FileTree, Reader (render/highlights/selection popover/reading position/fragments),
                   DiscussionPanel (+ useJobStream), SessionPicker, PreviewDialog, TerminalPanel (xterm.js),
                   RecentList, Layout (prefs, resizer, appearance), DiffView, MarkdownView
tests/backend/     pytest: real Git + tmux, fake providers (tests/fakes/fake-opencode, fake-codex)
tests/frontend/    Playwright specs + start_server.py (spawns a backend with a temp workspace and fake agents)
scripts/           setup.sh, start.sh, test.sh
docs/              DEPLOYMENT, CONFIGURATION, OPERATIONS, SCHEMA, ARCHITECTURE (this file)
```

## Runtime picture

```
Browser (React, xterm.js) ──HTTP/JSON──► FastAPI (uvicorn, one process, asyncio)
   │  SSE /events, /jobs/{id}/events     │   ├─ SQLite (WAL) via SQLAlchemy, writes serialized
   │  WebSocket /terminals/{id}/ws       │   ├─ EventBus → SSE subscribers
   └─────────────────────────────────────┘   ├─ JobManager → adapter subprocess (opencode/codex) or HTTP to opencode serve
                                             ├─ PTY → `tmux -L <socket> attach` → user's shell/agents (survive the server)
                                             └─ watcher child process (watchfiles) → EventBus
```

### A discussion turn, end to end

1. Reader builds the text model from the sanitized DOM, converts the selection to `{quote, prefix, suffix, start,
   end, heading}` and `POST /documents/{id}/threads` with the document's `version_sha256`.
2. `ThreadService.create_thread` re-reads the file, rejects on hash mismatch (409 `DOCUMENT_VERSION_CONFLICT`),
   validates the anchor against the server-side text model (400 `INVALID_ANCHOR`), picks the session
   (explicit → document default → new default via `default_provider`), writes thread + user message + empty agent
   message, and enqueues a job with the built prompt.
3. `JobManager._run` waits for the session lock, emits `queued/waiting/running`, calls
   `adapter.resume_turn()`, and translates adapter events into persisted `job_events` (`started` binds the native
   session id; `delta`; `tool_activity`; `permission_request`; final `completed|failed|cancelled`). The agent
   message content/status is updated at the end (`_finish`).
4. The browser subscribes to `GET /jobs/{id}/events?after=N`; stored events are replayed, then live ones are
   streamed; a reconnect resumes from the last `seq`.
5. If the agent edited the file, the watcher records a new `document_versions` row, publishes `file_changed`, and
   the UI shows the banner / diff; anchors are re-resolved on the next `GET /documents/{id}/threads`.

### Text model and anchors

Both sides implement the same rules (DESIGN §10): concatenate text nodes; one `\n` between block elements; `<br>`
= one `\n` (not doubled with a following serialized newline); CRLF→LF; whitespace-only text nodes directly inside
block containers are ignored. Offsets are UTF-16 code units. `backend/app/textmodel.py` renders with
markdown-it-py (`commonmark` + table + strikethrough, `html: false`), the browser with markdown-it with identical
options; `tests/backend/test_anchors.py` and the Playwright suite exercise cross-block selections. Heading ids use
GitHub's slug algorithm on both sides so wiki links resolve.

### Terminals

One tmux session per tab on the app's own socket. Each browser connection spawns `tmux attach` in a PTY; input
is base64 raw bytes (`onData` UTF-8, `onBinary` low-8-bit), output is raw bytes written to xterm.js. Closing the
socket terminates only the attach client. `TerminalManager.reconcile()` re-matches DB tabs with live tmux
sessions at startup. `window-size latest` makes the most recently active client control the size.

### Security boundaries

- Auth: `require_auth` (reads) and `require_auth_mutating` (Origin + CSRF) dependencies; WebSocket requires the
  cookie, `waw-csrf.<token>` subprotocol and Origin. Gateway mode only trusts `trusted_proxies`.
- Paths: `DocumentService.resolve()` normalizes, rejects `..`, and refuses symlinks escaping the root; writes go
  through `write_atomic` (temp file + rename, mode preserved).
- Rendering: DOMPurify profile forbids scripts/styles/forms/svg/math/event handlers; links limited to
  http(s)/mailto/relative; images served through `/raw` with sniffing disabled.
- Processes: argument arrays only; prompts via argv (OpenCode) or stdin (Codex), never a shell string.

## Development workflow

```bash
scripts/setup.sh --workspace /path/to/wiki          # once
scripts/start.sh                                     # backend on :8765 serving frontend/dist
cd frontend && npm run dev                           # Vite dev server on :5173 proxying /api to :8765 (HMR)
```
Log in on the Vite URL with the same token (Origin check accepts the request Host, so `localhost:5173` works
because the proxy keeps `Host`).

Tests:

```bash
scripts/test.sh                      # backend, ~30 s
scripts/test.sh --e2e                # + Playwright (needs `npx playwright install chromium` once)
.venv/bin/python -m pytest tests/backend -k terminal -q
cd frontend && npx playwright test -g "Recent"
```
Backend tests build a temp Git workspace, point the providers at `tests/fakes/fake-*` (controlled with prompt
directives like `[[fail:msg]]`, `[[sleep:N]]`, `[[edit:path:::old:::new]]`, `[[auth]]`, `[[busy]]`), and use the
real tmux. `tests/frontend/start_server.py` does the same for the browser suite on port 8799.

Lint/type checks used in review: `.venv/bin/ruff check --select F,E9 backend tests` and
`cd frontend && npx tsc --noEmit -p tsconfig.json --noUnusedLocals --noUnusedParameters`.

### Changing the schema

1. Edit `backend/app/models.py`.
2. Add a migration in `backend/migrations/versions/` (hand-written, `batch_alter_table` for SQLite), chained via
   `down_revision`.
3. Update `docs/SCHEMA.md` — `tests/backend/test_schema_doc.py` fails on undocumented tables/columns.

### Adding a provider

Implement `AgentAdapter` (`agents/base.py`): `capabilities()`, `list_sessions()`, `create_session()`,
`resume_turn()` returning a `TurnHandle` whose `events()` yields `started/delta/tool_activity/permission_request/
completed/failed/cancelled`. Register it in `agents/registry.py` and add a `ProviderConfig` section. Add a fake
executable under `tests/fakes/` so the behaviour is testable without credentials.

### Frontend conventions

English UI strings inline; preferences in `localStorage` (`waw.prefs`); server state is the source of truth for
everything that must survive a device change (reading position, open discussion, sessions). Components receive
data and callbacks from `App.tsx`; there is no global store. All user-facing errors come from the API's
`{error:{code,message}}` shape and are shown next to the action that failed, never as silent failures.
