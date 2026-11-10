# Architecture and development guide

For people maintaining or extending the code. The behavioural specification is [DESIGN.md](../DESIGN.md); this
page explains how the implementation is laid out and how the pieces talk to each other.

## Repository map

```
backend/app/
  main.py          FastAPI app factory, lifespan (migrate DB, mark interrupted jobs, reconcile tmux, detect
                   providers, start watchers), /auth, /bootstrap, /events (global SSE), /export, SPA serving
  cli.py           `setup` / `serve` / `print-config` entry points (python -m backend.app.cli …)
  config.py        TOML + env loading, Settings/ProviderConfig dataclasses ([[workspaces]] list), secret generation
  db.py            SQLite (WAL) engine, serialized writes (RLock), Alembic runner (FK-safe), WAL checkpoint
  models.py        SQLAlchemy models (schema reference: docs/SCHEMA.md)
  auth.py          token login, HttpOnly cookie, HMAC CSRF token, Origin checks, WebSocket auth, gateway mode
  social_login.py  Continue with GitHub (OAuth app) / Google (OpenID Connect): state cookie, server-side code
                   exchange, allow-list (GitHub logins / verified Google emails), then a normal login session
  deps.py          AppContext + dependencies require_auth / require_auth_mutating, which also *scope* the context
                   to one root folder (see "Multi-root scoping")
  workspaces.py    WorkspaceRegistry: one WorkspaceRuntime (DocumentService + LockService + watcher) per root
                   folder; add/remove/rename folders; /workspaces, /fs/dirs (folder picker)
  errors.py        ApiError and the stable error codes; JSON error shape
  documents.py     DocumentService (one per root): safe paths, file tree + kinds + Git status, open (all formats),
                   version snapshots, direct editing (conditional save), reading state, review marks, diff,
                   relink, recent, hide/delete history;
                   routes under /files, /documents, /recent, /raw
  textmodel.py     markdown-it-py rendering, text/source → code block, .docx → HTML (mammoth), .pptx → HTML
                   (python-pptx: slide sections, nested bullets, tables, inline images, notes), .vsdx → HTML
                   (pages and shape text in reading order), the deterministic
                   text model (mirrors frontend/src/textmodel.ts)
  github_hub.py    GitHub review hub: owner gate (GitHub sign-in = gh account), review queue via one paginated
                   GraphQL search through the server's gh CLI (40 PRs/page; smaller on GitHub 502), partial
                   results (SAML-protected PRs), stale-while-revalidate cache; /github/hub, /github/reviews;
                   a PR (/github/pr/{owner}/{repo}/{n}: GraphQL details + REST files, 60 s cache) is written as
                   a Markdown copy to snapshots/github/ and discussed as the hidden read-only document
                   .waw-github/<owner>/<repo>/<n>.md of the repo's wiki (github_repo_wikis); deps.py refuses
                   every PR document/discussion to anyone but the owner's GitHub sign-in; review comments:
                   GET pulls/{n}/comments, drafts in github_pr_drafts, one POST pulls/{n}/reviews (with
                   commit_id of the diff shown) on Finish review, replies via comments/{id}/replies
                   · RateGuard: before every gh call, `gh api rate_limit` (re-read each minute) — at <= 100
                   left it waits for a reset <= 90 s away or fails with GH_RATE_LIMITED; mutations >= 1 s apart
                   · hub preferences (hidden / watched repos, priority requesters; kv.py → kv_settings), the
                   requester of each review request (timeline REVIEW_REQUESTED_EVENT; Meta by profile company),
                   watched repos' open PRs (one search each, with the queue)
                   · /contributors: PR author + commit authors / co-authors (GraphQL, no email scope) with a
                   guessed affiliation; details=true adds REST /users/{login} (public email), cached 1 h
                   · agent review: a thread created with pr_review=true gets numbered diffs and the
                   ```waw-review JSON contract in its prompt (annotations.build_pr_prompt); the run is recorded
                   in kv_settings (agent_review:<repo>#<n>) and, once its message completes, GET .../comments
                   turns the block into github_pr_drafts (origin=agent, decision=proposed; lines checked against
                   the diff, else file comments) and replaces the JSON in the message by a note; Finish review
                   publishes only decision=accepted drafts and deletes discarded ones
  chats.py         folder chats: the hidden read-only document .waw-chat/<slug>-<id>.md of a folder (stub in
                   snapshots/chats/<folder id>/, never in the wiki) reusing threads, sessions and streaming;
                   /chats list/create/archive (documents.hidden); build_chat_prompt (the conversation so far, locks,
                   reference folders) and the distill (target page checked against locks / the folder) and
                   update actions (MessageIn.chat_action); edits are reviewed in the Diff view
  local_repos.py   a repository ↔ local git checkout (kv local_repo:<repo>): PR head fetched as refs/waw/pr/<n>
                   (git fetch <remote|URL> pull/<n>/head, never checked out), optional base update (fetch to
                   refs/waw/base/<b>; ff-only merge when on <b> and clean); its prompt text goes first in the PR's
                   reference context (kv.extra_context); change summaries: rolling day/week/month/quarter/year
                   windows of the default branch's git log (subjects, per-area file counts) → a folder chat in the
                   repo's wiki (/github/repo-summary, ThreadCreateIn.chat_brief)
  · github_hub /mentions: PR participants (author, reviewers, requested, contributors, commenters, kept per PR in
                   memory) then GraphQL mentionableUsers(query) (cached 10 min)
  kv.py            kv_settings get/put; extra reference folders (context:repo:<o>/<r>, context:ws:<id>) added
                   to every agent prompt for that PR repository / wiki folder (/agent-context)
  office.py        OfficeRenderer: Layout view, LibreOffice headless → PDF, content-addressed cache, parallel
                   limit, timeout, private profiles; routes /office/render (status, starts work), /office/pdf
  anchors.py       anchor resolution algorithm (attached/moved/ambiguous/orphaned) + offset validation
  annotations.py   ThreadService: threads, messages, retry, reanchor, context relocation; prompt building;
                   /jobs routes (SSE, cancel, permission decisions)
  sessions.py      SessionService: app sessions, create/link provider sessions, model/variant; /agent-sessions,
                   /providers/{p}/models
  locks.py         LockService (one per root): read-only locks on files/folders; /locks
  previews.py      preview-before-apply in an isolated workspace copy; SHA-checked, lock-checked apply
  diffs.py         DiffService: file/folder diffs vs refs (untracked included), line/range comments → threads,
                   Viewed marks (file_reviews); /diff, /diff/comments, /diff/viewed, /refs
  reviews.py       ReviewService: stash comments, submit once, one triage job (address/reply/skip) → threads; /reviews
  commits.py       CommitService: git status, agent-drafted message, selective commit, push, gh pull request; /git
  watcher.py       child-process filesystem watcher (one per root) → global events, version recording
  events.py        in-process pub/sub (EventBus) feeding SSE
  idempotency.py   ASGI middleware for Idempotency-Key
  gitinfo.py       read-only git helpers (status/branch/commit/show, raw git runner, ref validation)
  agents/
    base.py        adapter contract (capabilities/list/create/resume/cancel/list_models), event types, subprocess helper
    opencode.py    OpenCode CLI adapter (JSON lines) and server adapter (HTTP + SSE); --model/--variant
    codex.py       Codex `exec --json` adapter (new + resume), session listing from $CODEX_HOME; -m / effort
    registry.py    builds adapters from config, caches capabilities
    jobs.py        JobManager: per-session serialization, event persistence (seq), SSE replay, cancel,
                   permission relay, lock-aware turns (per session's root), interrupted-on-restart/shutdown
  terminal/
    tmux.py        app-owned tmux server (socket from <state_dir>/tmux-socket, generated config)
    pty.py         PTY wrapper around `tmux attach` with asyncio reader/writer, resize
    manager.py     terminal tabs CRUD, reconcile on startup, history snapshot, WebSocket relay
backend/migrations/   Alembic env + versions 0001 … 0009 (one per schema change)
frontend/src/
  api.ts           fetch wrapper (CSRF + X-Workspace headers, error shape), types, SSE subscriptions
  markdown.ts      markdown-it + DOMPurify, GitHub slugs, link rewriting (#frag, page.md#frag, images → /raw)
  textmodel.ts     DOM text model, selection → anchor, offsets ↔ DOM Range, client-side resolution
  App.tsx          state container: auth, active folder, document, threads, sessions, terminals, jobs, prefs, Recent
  components/      Login, FileTree (multi-root), AddFolderDialog, DocTabs (open documents), PdfView (PDF.js: PDFs and Office Layout view — text layer, page/area asks), Reader (render/highlights/selection popover/
                   reading position/fragments/Read-Discuss), DiscussionPanel (Individual/Flat + useJobStream),
                   SessionPicker, ModelPicker, PreviewDialog, DiffReview (unified/split, ranges, inline threads,
                   review tray), Editor (in-browser editing), CommitPanel, TerminalPanel (xterm.js), RecentList, Layout (prefs, resizer,
                   appearance), FileIcon, DiffView, MarkdownView, keys.ts (composer keys)
tests/backend/     pytest: real Git + tmux, fake providers (tests/fakes/fake-opencode, fake-codex)
tests/frontend/    Playwright specs + start_server.py (spawns a backend with a temp workspace and fake agents)
scripts/           setup.sh, start.sh, test.sh
docs/              USER_GUIDE, DEPLOYMENT, CONFIGURATION, OPERATIONS, SCHEMA, ARCHITECTURE (this file)
```

## Runtime picture

```
Browser (React, xterm.js) ──HTTP/JSON──► FastAPI (uvicorn, one process, asyncio)
   │  SSE /events, /jobs/{id}/events     │   ├─ SQLite (WAL) via SQLAlchemy, writes serialized
   │  WebSocket /terminals/{id}/ws       │   ├─ EventBus → SSE subscribers
   │  X-Workspace: <root id>             │   ├─ WorkspaceRegistry → per root: DocumentService, LockService, watcher process
   └─────────────────────────────────────┘   ├─ JobManager → adapter subprocess (opencode/codex) in the session's root,
                                             │               or HTTP to opencode serve
                                             ├─ PTY → `tmux -L <socket> attach` → user's shell/agents (survive the server)
                                             └─ git / gh subprocesses (diff, commit, push, pull request)
```

### Multi-root scoping

The window can hold several root folders. `WorkspaceRegistry` keeps one `WorkspaceRuntime` per open folder
(`DocumentService`, `LockService`, `WorkspaceWatcher`) and the `workspaces` table records every folder ever opened.
Every per-wiki row (documents, sessions, locks, reviews, file_reviews) carries `workspace_id`.

Named workspaces (`named_workspaces`, VS Code-style) are named sets of folders; one is current and decides which
runtimes the registry holds. `WorkspaceRegistry.switch()` keeps the runtimes of folders both workspaces share (no
watcher re-registration for big trees), stops the others, starts the new ones, saves the folder list, rewires the
process-wide defaults (`on_switch`: primary folder for jobs and terminals) and publishes `named_workspace_changed`;
browsers reload. Terminal tabs carry `named_workspace_id` and are listed per workspace (`NULL` = Default). Document
tabs live in the browser per workspace (`localStorage` `waw.docTabs.<id>`).

Each `WorkspaceWatcher` runs `watchfiles` in a spawned child process and streams batches over a pipe. The server reads
the raw descriptor itself (`_FrameReader` decodes the `multiprocessing` wire format), because uvloop (uvicorn's
default loop) makes the descriptor non-blocking and `Connection.recv()` would fail on a partly arrived batch. If the
child dies or the pipe breaks, the watcher restarts it with backoff (2, 4, 8… s, at most 5 times in a row) and, once
it is ready, publishes `tree_changed` so clients pick up what changed in the gap. A child that reports its own error
(e.g. inotify watch limit) is not restarted; its message is shown instead.

`tree_changed` from the watcher carries `dirs`: the folders whose listing changed (omitted above 200 → reload all).
The FileTree requests only *visible* folders (root and every parent expanded and listed), parents before children,
at most 4 at a time with a 20 s timeout. Refreshes never overlap: requests made during one are merged into the next,
and watcher events are folded for 1 s, then only the changed folders and their parents are reloaded. Expanded
folders are remembered in `localStorage`; ones that vanished are forgotten when their parent is listed or on 404.

`require_auth` / `require_auth_mutating` return a *scoped copy* of `AppContext` (`deps.scoped`) whose `documents`,
`locks`, `watcher`, `workspace_id` and `workspace_root` belong to one root. The root is chosen as:

1. the root of the object the URL addresses (`doc_id`, `thread_id`, `preview_id`, `review_id`, `comment_id`,
   `session_id`) — so a document can never be read through another root's path;
2. otherwise the `X-Workspace` header (sent by `api.ts` on every call) or `?workspace=` (images, PDFs);
3. otherwise the primary root (`[workspace] root`).

`DocumentService.current_content` additionally refuses (`WRONG_WORKSPACE`) to read a document of another root, and
`JobManager` runs each turn in the root of its session's workspace with that root's locks. Route code keeps using
`ctx.documents` / `ctx.locks` unchanged — scoping is entirely in the dependency.

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

Copy: tmux owns the mouse, so a drag selects in tmux copy mode; on release tmux emits OSC 52 (`set-clipboard on`),
which TerminalPanel's `registerOscHandler(52)` decodes and writes to the clipboard (`frontend/src/clipboard.ts`:
Clipboard API in secure contexts, `execCommand("copy")` otherwise). Shift+drag forces an xterm.js selection,
copied on release or with Ctrl+Shift+C. OSC 52 queries are ignored, so programs can never read the clipboard.

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

### Adding a root-scoped feature

Use `ctx.documents`, `ctx.locks`, `ctx.workspace_id` and `ctx.workspace_root` from the injected context (never the
unscoped `request.app.state.ctx`), store `workspace_id` on new per-wiki rows, and if the route addresses an object
by id add that id to `WorkspaceRegistry._workspace_of_object` so the object decides the root.

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
