# Wiki Agent Workspace — Implementation Design

**Status:** implementation specification, not an implemented service.  
**Audience:** a coding agent implementing the complete application.  
**Date:** 2026-11-05.  
**Priority:** working, low-friction single-user tool for a real workflow; avoid product-style setup and approval overhead.

## 1. What to build

Build a self-hosted web application that runs on the same remote Linux machine as the user's Git working tree and existing command-line agents. A browser, on a computer or mobile device, connects to this server. The application has two connected capabilities:

1. A genuine interactive remote terminal, where the user may run their normal shell, OpenCode, Codex, Git, editors and other programs permitted to their existing OS account.
2. A rendered Markdown reader in which the user selects any words or lines, asks an agent about that exact text, continues the discussion, and revisits persistent annotations while the Wiki evolves.

The core workflow is:

1. Start in the remote terminal and discuss/explore a problem with an agent.
2. The user asks the agent to create a Markdown Wiki draft in the existing Git working tree. The agent creates it using its ordinary facilities; the app detects it or lets the user refresh the file tree. Do not require the user to review the entire terminal transcript before the draft is available.
3. Open the draft in rendered reading mode. Select words, a sentence, or several lines; a floating question box appears at the selection. Ask and follow up without losing the quoted context.
4. If the user explicitly asks the agent to change the Wiki, make the requested change according to the agent's existing configuration, then show the actual diff and preserve the discussion. A separate preview-before-applying mode is available when the user chooses it; do not add an extra application approval after every explicit instruction to edit.
5. Return to the document repeatedly over days or months, retaining discussions, session associations, reading position and document history. A document change must not silently attach an old annotation to the wrong passage.

The interface mockup in `INTERFACE_PROTOTYPE.html` is a local, simulated design reference. It is not the working service. If it conflicts with this document, **this document is authoritative for behavior**. Do not ship sample answers or simulated terminals as real features.

## 2. Scope, assumptions, and usability principles

### 2.1 Initial operating environment

- Server: a single user-controlled Linux host, Python 3.11–3.13, Node.js 22 or later for the frontend build, Git, a POSIX shell and tmux 3.2 or later. Windows/macOS browser clients are supported; running the server on Windows is out of scope for the first implementation.
- The application runs under the **same OS user** that owns/uses the Wiki checkout and the existing agent configuration. Preserve that user's `HOME`, shell startup files, `PATH`, Git credentials and agent configuration. Never silently switch to root.
- A configured workspace root is the existing local directory containing the Wiki. It may itself be a Git worktree. The user may browse files under that root. Starting an agent or changing directory in a terminal is not restricted to that root: the terminal has the normal rights of the server OS account.
- The app is single-user in v1. A client may be disconnected, changed or closed while the server remains running. Browser-side local storage is not the sole storage for required state.
- GitHub is the Git remote; the working tree on the server is the immediately readable/editable document source. A Markdown file need not have been committed or pushed before it appears in the reader.

### 2.2 Required product choices

- Make the common path require no account creation, provider configuration wizard, project import wizard or per-action permission dialog. Reuse the user's current agent sign-in and configuration whenever that agent supports it.
- Use existing configured agent permissions/approval settings. Do not add an application-level role system or wrap the terminal in a restrictive artificial sandbox. If the agent itself requests confirmation, show the request through its native terminal or supported API; do not silently bypass it and do not leave the user with an unexplained spinner. Codex non-interactive execution can default to read-only even when the terminal user expects edits; apply the explicit, persistent adapter configuration in §8 instead of treating an authorization failure as a successful edit.
- The user's direct instruction to edit is authorization for that edit. Show the resulting changes and provide Git-based recovery; do not ask for the same approval a second time. The optional preview mode has an explicit Apply button because the user chose a preview.
- Keep setup minimal: one configuration file, one setup command, and one start command. No root, container, cloud account, database server or separate model API key should be required when the server user's existing agent login suffices.
- Authentication protects the remote shell but must not interrupt ordinary usage. Use one server access token at first login and a persistent session cookie; do not re-prompt for each document, message, shell command or API call. Support an already authenticated private gateway as an explicitly configured deployment option. No mandatory public domain, TLS certificate purchase, OAuth, or multi-factor service.
- Give actionable errors and keep the rest of the application usable: an unavailable agent must not prevent opening the reader or terminal; a failed annotation must not erase the user's question.

## 3. Definition of done

The deliverable is a runnable source project, not a static mockup. It includes the web server, frontend, database migrations, tmux-based terminal, agent adapters, API, automated tests, dependency locks, installation scripts and a README with exact commands. The implementation must satisfy the acceptance tests in §17. Do not mark a feature complete using hard-coded sample data, a simulated terminal, console-only output, or a stubbed production agent.

## 4. Concrete implementation stack and repository layout

Use this stack for v1 to eliminate implementation ambiguity:

- Backend: Python, FastAPI, Uvicorn, Pydantic, SQLite in WAL mode, SQLAlchemy and Alembic. Use async request handling; serialize SQLite writes appropriately. Provide locked Python dependencies.
- Frontend: React + TypeScript + Vite, markdown-it, DOMPurify, xterm.js and its fit addon. Provide `package-lock.json`; serve production assets from the same web server and origin.
- Terminal persistence: tmux and a server-side PTY. The web server relays PTY bytes to/from xterm.js over a WebSocket.
- Agent adapters: subprocess calls or the provider's documented local service API. Start processes with argument arrays, never construct shell command strings from document text or session IDs.
- Tests: pytest for backend/API/PTY integration and Playwright for browser interaction. Use real Git and tmux in the integration environment. Use controllable fake Codex/OpenCode executables to test adapter event/error behavior without credentials; additionally document a manual test with the user's actual installations.

```text
workspace/
  README.md
  DESIGN.md
  pyproject.toml                 # dependency and entry-point configuration
  requirements.lock
  config.example.toml
  scripts/setup.sh               # user-level installation, no sudo
  scripts/start.sh               # start the service
  backend/app/main.py
  backend/app/auth.py
  backend/app/config.py
  backend/app/db.py
  backend/app/documents.py
  backend/app/annotations.py
  backend/app/agents/
  backend/app/terminal/
  backend/migrations/
  frontend/package.json
  frontend/package-lock.json
  frontend/src/
  tests/backend/
  tests/frontend/
```

Equivalent internal decomposition is acceptable, but these entry points, behaviors and tests must exist. The project source must not be embedded inside the user's Wiki checkout unless the user puts it there.

## 5. UX and screens

The initial UI is Chinese, consistent with the included prototype; source identifiers and this document are English. Prioritize a focused work surface over marketing, onboarding pages, metrics, or settings-heavy navigation.

### 5.1 Main shell

- A file-tree panel lists directories and `.md`/`.markdown` files under the configured workspace; exclude `.git` and application state. Refresh and open a file without GitHub OAuth. Show path, active branch if known, and `untracked` / `modified` / `clean` from local Git where available.
- A document area renders Markdown, heading navigation, code fences, tables, links and local images. Internal relative links resolve within the workspace. Links that leave the workspace open only as ordinary external links.
- A terminal tab area behaves like a terminal, supports multiple terminal tabs, and remembers their order, labels and active tab across browser reconnects. Switching between document and terminal is immediate.
- The document header shows the current default agent session and actions: select a session, create a new session, and optionally associate the session that created the Wiki. Clearly show provider and session title/short ID. Do not present a successful association unless the provider confirms the ID.
- The discussion panel shows the selected quoted passage, messages, streaming output, error state and session used. A discussion can be reopened from a mark at the passage or a list of discussions.

### 5.2 Selection and discussion

- Desktop: after a non-empty selection in the rendered document and a 350 ms pause following pointer release, display a question popover adjacent to the selected range. Constrain it to the viewport and move it above the selection if there is insufficient space below. Copying text must continue to work. Escape closes the popover without creating a thread.
- Mobile: after a native text selection, provide a visible `提问` action when browser selection APIs expose the range. If a platform suppresses the custom action, the document's persistent `选择文字后提问` action must let the user submit the current native selection. Do not require hover on touch devices.
- Popover contains the quote, question input, session selector defaulted to the document's current session, and Send. A selection is preserved even when focus moves into the input.
- Sending creates a persistent discussion and enqueues an agent turn. Show streaming output and an explicit failed/cancelled status; retain the question if the agent fails. Retrying is explicit and creates a new job attached to the same discussion.
- Discussion/Q&A alone does not modify the document by instruction. An explicit instruction to the agent to edit is allowed under the user's existing agent permissions; no application-generated repetitive permission request.
- Save reading position automatically after scrolling is idle for one second, and on page hide. Provide a one-click optional `mark reviewed` action; scrolling past content never marks it reviewed automatically.

### 5.3 Document changes and review

- Detect external modifications and new files by filesystem watcher, plus a manual refresh fallback. Debounce bursts for 500 ms. On a changed file, retain the reader's current position where possible and show a `document changed` banner; user chooses to refresh. An in-flight unsent question remains in the input.
- Before/after diff is visible after agent edits, on demand, and in the optional preview-before-apply mode. Local application of a reviewed preview does not silently commit or push to GitHub.
- Existing annotations are reanchored using §10. A reviewed section that changes is flagged `needs review`.
- A question can be answered without requiring the user to edit, commit, push, or resolve unrelated discussions.

## 6. State and lifetime: three distinct things

1. **Terminal session:** a tmux session/window and live shell processes on the remote host. Closing a browser detaches the browser's tmux client; it must not terminate the tmux session or its programs. Reattach to the same terminal after reconnecting.
2. **Agent conversation session:** a provider-native OpenCode or Codex conversation identified by the provider and its native session ID. It may be associated with a document and/or discussed in one or more annotations. A terminal session ID is not an agent session ID.
3. **Application discussion:** a stored UI thread anchored to a document passage, containing user/agent messages and the agent session used for each turn. Several discussions may use the same agent conversation.

Do not assume the entire screen history of an arbitrary TUI can be parsed into an agent session. Do not type into an arbitrary running terminal in order to send a reader question. Use a documented provider interface and a known session ID. If the original native ID cannot be detected, let the user choose/link it from a provider-supported session list or enter an ID; if the tool cannot list sessions, explain that limitation and keep the terminal available.

## 7. Agent session semantics

- Each document has exactly one nullable `default_session_id` in application storage. When the document was generated through a provider session and the app can verify that session's native ID, automatically bind it. Otherwise the user may link an existing session; do not guess based on most recent global activity.
- A new discussion uses the document default session. If none exists, create one using the configured default provider on the first question, then set it as the document default. A user may explicitly choose `new session` or select another supported existing session when starting any discussion.
- The header's `new session` action creates a new session and makes it the document default for new discussions. Existing discussions and their historical messages are unchanged.
- Each discussion stores `last_session_id`. Replies default to that discussion's last used session; the user can explicitly switch it for that turn. Each message stores its actual `session_id`. Display when a discussion's session differs from the document default.
- Some providers allocate the native session ID only when the first turn starts. In that case create a local session record with `native_id=null` and status `uninitialized`; queue subsequent turns on that local record, then bind the real native ID transactionally when the provider reports it. If allocation fails, show failure and retain the pending question; never manufacture a native ID.
- Session names/IDs are provider-specific. Never send a Codex session ID to OpenCode or vice versa. Listing or resuming a session that is unavailable returns a named error; never silently start a fresh one and claim continuity.
- Serialize jobs started by this app for the same native agent session. Jobs for separate sessions may run concurrently. If a provider reports busy, queue and visibly show `waiting for session`; offer cancellation and the option to explicitly use a new session. The app cannot reliably know about every external CLI process, so show `availability unknown` for externally created sessions until the provider confirms readiness. Do not claim that a local mutex protects processes outside the app.
- Prefer a provider's supported server/session API when it safely supports the running session. Otherwise resume by session ID using the CLI, with the selected workspace directory, same OS user and existing auth. If the provider rejects concurrent use by an active terminal TUI, preserve the question and guide the user to finish/exit that TUI or explicitly choose a different session. Never kill the user's terminal automatically.
- Agent context includes the user's question, verbatim selected quote, the current document path, content hash, heading/context and access to the workspace under existing provider permissions. When historical source material is available, expose paths/revisions as references. An agent must not claim to know the original reasoning if those records are unavailable.
- Store application-visible messages independently of provider state. A long agent conversation can be compacted by that provider; do not claim the model necessarily retains all prior text verbatim.

## 8. Agent adapter contract and verified starting points

Define one backend adapter interface with operations `capabilities()`, `list_sessions(workspace)`, `create_session(workspace)`, `resume_turn(session, prompt, workspace)`, and `cancel(job)`. An adapter returns typed streaming events `started`, `delta`, `tool_activity`, `permission_request`, `completed`, `failed`, and `cancelled`. Persist the native session ID as soon as the provider supplies it. `capabilities()` states whether the installed version can list, resume, stream structured output, cancel and use an existing local service. Disable only the unavailable operation, with an actionable message; keep the rest of the app usable.

Use documented CLI interfaces, check executable/version on startup and report capabilities. The following are documentation-backed integration starting points, not claims that the user's currently installed versions have been tested:

- [OpenCode CLI](https://opencode.ai/docs/cli/) documents non-interactive `run`, `--session`, `--format json`, and `session list`. The [OpenCode server API](https://opencode.ai/docs/server/) documents session listing/status, message submission, cancellation, permission responses and an SSE event stream. Its documentation says the TUI starts a server, normally on a randomly assigned port; starting a separate `opencode serve` starts a **new server**, not an automatic attachment to the TUI's existing server. To reuse the same OpenCode backend, configure a known loopback host/port once and connect both the terminal client and the app to that same instance, or explicitly associate an accessible existing instance. Never guess ports or claim two server processes share live state. Map `prompt_async`'s HTTP 204 to “accepted”, not “completed”; wait for provider events/status and persist the final result. Treat external-client concurrency behavior as unverified until tested with the installed release.
- [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive/) documents `codex exec --json` JSONL events including a session/thread-start event and `codex exec resume <SESSION_ID>`. It says saved CLI authentication is reused by default, and that `codex exec` defaults to a read-only sandbox. An event stream can provide discrete completed messages rather than token-by-token deltas: display whatever progress/events the installed version actually emits and do not invent token streaming.
- For app-driven Codex turns, the user's explicit request to run this Wiki tool includes workspace editing. At initial setup, present and save one persistent provider setting `codex_sandbox`, default `workspace-write`, scoped to the configured workspace. When supported by the installed CLI, pass its documented workspace-write option for app-driven turns; do not re-prompt for every turn. If the user explicitly configures a different agent profile, honor it. Managed policies and OS permissions remain authoritative. Do not silently enable `danger-full-access`, bypass approvals or change the user's interactive terminal configuration. If the chosen setting is unavailable or denied, show the exact provider error and a direct route to the user's terminal/settings rather than looping or claiming the file changed.
- In OpenCode native-server mode, if the provider requests a permission decision, surface the provider's real request in the discussion and forward the user's response only through the documented permission endpoint using the job protocol in §13. This is the provider's existing policy, not an additional application approval. In a CLI mode that cannot relay such a request, expose the cause and offer the existing terminal as a fallback.

Do not parse human-oriented colored terminal output as a stable protocol. Do not pass selected text or credentials through a shell string. Agent startup failures and missing login must be visible with the exact actionable cause, without logging authentication tokens.

## 9. Markdown rendering and document identity

- Read UTF-8 `.md` / `.markdown`; return an explicit error for invalid UTF-8. Default maximum document size is 20 MiB, configurable. Preserve the exact original bytes until the user or agent edits the file.
- Render GitHub-style Markdown as close as the chosen renderer supports, with heading anchors, fenced code, tables, lists, blockquotes and local images. Sanitize the generated HTML with DOMPurify; do not execute scripts or event attributes from repository content. Respect relative links, URL-encode paths and reject unsafe protocols.
- A document has a stable application UUID for each `(workspace_id, normalized relative path)`. Store the current Git branch/commit as metadata, not as the sole version identifier. A branch switch changes content/version; it does not silently transfer a thread to a different file.
- A version is SHA-256 of exact file bytes and is recorded even if a file is untracked or modified. Store snapshots or diffs needed to display the version referenced by an annotation and the local app's own edits. Git remains the authority for Git history; the app must not fabricate a commit.
- When a file is renamed/deleted outside the app, retain the original document and discussions as `missing`; let the user explicitly relink to another file. Never infer identity from same filename alone.

## 10. Deterministic annotation anchors

Create a deterministic rendered-text model from the sanitized document DOM: concatenate text nodes in document order, insert one newline between adjacent block elements, and normalize CRLF/CR to LF. Text offsets are JavaScript UTF-16 code-unit offsets in this model. Backend validation must convert Python Unicode indices correctly and reject offsets that split a surrogate pair. Build a map from model ranges to DOM text nodes so the browser can reconstruct a DOM Range. Save:

`document_id`, `version_sha256`, `quote` (exact selected rendered text), `prefix` (up to 40 preceding model code units), `suffix` (up to 40 following code units), `start`, `end`, and nearest heading text/path. Store all text as UTF-8 in the database. Do not replace the quoted selection with the whole paragraph.

Resolve anchors in this order:

1. If version hash matches and the model slice at `[start,end)` equals `quote`, the anchor is `attached`.
2. Otherwise find all **exact** occurrences of `quote` in the new model. If exactly one exists, attach to it and mark `moved`.
3. If multiple occurrences exist, keep candidates whose preceding/following strings exactly match all nonempty stored prefix/suffix. If exactly one candidate remains, attach and mark `moved`.
4. If there are no occurrences, mark `orphaned`. If multiple candidates remain, or no unique context match exists, mark `ambiguous`. Display the old quote and allow the user to select a new passage manually. Never attach to an arbitrary match.

Reanchoring may update the current anchor but must preserve the original quote and version in history. Store each explicit manual reanchor. If the document changes between selection and thread creation, reject with `DOCUMENT_VERSION_CONFLICT`, preserve the composed question in the UI, and offer reload/reselect.

## 11. Terminal design and reconnection

- Create one tmux window/session per UI terminal under a dedicated app socket/namespace. Start the shell as the same server user with their normal login environment and configured initial directory. Terminal input is a byte stream, not a command form.
- Open a PTY that attaches to the tmux session, relay it to the browser using a same-origin WebSocket, and resize it on xterm resize. Browser sends keyboard, paste and control characters; output supports ANSI/256/true-color sequences as supported by the server, tmux and xterm.js. Preserve the user's chosen theme and font settings.
- Configure RGB capability only in the dedicated app-owned tmux server when xterm.js and its advertised outer terminal actually support it; do not change the user's global tmux configuration. Confirm the inner `$TERM` exists on the remote host and the shell uses a compatible UTF-8 locale; fall back visibly if not.
- Browser closure/disconnect tears down only the WebSocket/attachment. The tmux session, shell and currently running CLI agent continue. Reopening reattaches to the same live session and tmux redraws the current screen. Browser-side xterm scrollback is a separate buffer and does not automatically survive a page reload. Retain at least 10,000 lines in tmux's configurable pane history and expose that history after reconnect, for example through tmux copy mode plus a clearly labelled history action; if a bounded history snapshot is replayed into xterm, avoid silently claiming exact, lossless history during concurrent output. Full-screen alternate-screen state, scrollback and application conversation history depend on tmux, the terminal emulator and the particular program. Never claim that restoring the current screen restores every prior message.
- On Web server restart, discover app-owned live tmux sessions and reattach them to saved UI tab metadata. When tmux is stopped, a terminal exits, or the host reboots, mark it `exited`/`unavailable` and allow a user-initiated new terminal. Never present a new shell as the old live process.
- Multiple browser clients of the same authenticated user may attach. Input is shared; the most recently focused terminal client controls the effective rows/columns. No artificial per-command permissions. An explicit `Close terminal` ends that terminal only after an ordinary close confirmation because it terminates running processes; disconnecting a browser does not invoke close.

WebSocket messages use JSON. Browser → server: `{"type":"input","data":"<base64 raw bytes>"}`, `{"type":"resize","cols":120,"rows":40}`, `{"type":"ping"}`. Server → browser: `{"type":"output","data":"<base64 raw bytes>"}`, `{"type":"state","state":"connected|detached|exited|error","message":"..."}`, `{"type":"pong"}`. Convert xterm.js `onData` strings to UTF-8 bytes; handle `onBinary` separately by copying each character's low 8 bits directly to a byte, because some mouse reports are not UTF-8 text. Base64 encodes the resulting bytes without alteration. The backend forwards bytes to/from the PTY without text decoding; the browser passes output bytes to `terminal.write(Uint8Array)` and waits for its callback when ordering a redraw. xterm.js interprets these output bytes as UTF-8, so non-UTF-8 terminal locales may not render exactly; default to a UTF-8 locale when available and report the limitation. Reject invalid base64 and dimensions outside 2–1000 columns or 1–500 rows. See the official [xterm.js Terminal API](https://xtermjs.org/docs/api/terminal/classes/terminal/).

## 12. Storage schema

Use UTC ISO-8601 timestamps, UUIDs and database transactions. Persist at least:

| Table | Required fields |
| --- | --- |
| `workspaces` | `id`, canonical `root_path`, `created_at` |
| `documents` | `id`, `workspace_id`, relative `path`, `default_session_id` nullable, `last_sha256`, `missing`, timestamps; unique workspace/path |
| `document_versions` | `id`, `document_id`, `sha256`, snapshot/diff reference, Git branch/commit nullable, `created_at` |
| `agent_sessions` | `id`, `provider`, `native_id` nullable, `workspace_id`, `display_name`, `status`, timestamps; unique provider/native ID/workspace when native ID is present |
| `threads` | `id`, `document_id`, original anchor fields, current anchor fields/state, `last_session_id` nullable, timestamps |
| `messages` | `id`, `thread_id`, `session_id`, `role`, `content`, `status`, `job_id` nullable, `created_at` |
| `jobs` | `id`, `session_id`, `thread_id`, `status`, `error_code`, `created_at`, `finished_at` nullable |
| `job_events` | `job_id`, monotonically increasing `seq`, `type`, JSON payload, timestamp; unique `(job_id,seq)` |
| `reading_state` | `document_id`, `version_sha256`, heading, offset, scroll fraction, updated timestamp |
| `review_marks` | `id`, `document_id`, anchor fields, reviewed version, state, timestamp |
| `terminals` | `id`, tmux identifier, label, workspace/cwd initial value, tab order, `state`, timestamps |
| `login_sessions` | token hash, expiry, CSRF token hash, timestamps; never store plaintext access tokens |

Store database, terminal metadata and app config in an application state directory outside the Git checkout by default. Do not store shell screen output, agent transcripts, API tokens or entire document contents in normal request logs. If provider history is imported, treat it as user data and retain source/consent metadata.

## 13. HTTP and WebSocket contract

All endpoints use `/api/v1`, UTF-8 JSON and error shape `{"error":{"code":"...","message":"...","details":{}}}`. IDs are UUID strings. Return 400 for malformed input, 401 unauthenticated, 403 unauthorized/origin failure, 404 missing resources, 409 version/session conflict, 413 oversized documents, and 500 for unexpected server failures. Never include secrets or stack traces in client errors.

| Method and path | Request → response |
| --- | --- |
| `POST /auth/login` | `{token}` → `{csrf_token,expires_at}` plus HttpOnly session cookie. |
| `POST /auth/logout` | Invalidate current login session and clear cookie. |
| `GET /bootstrap` | `{workspace,default_provider,providers,terminals,settings}`. |
| `GET /files?path=` | `{items:[{path,name,type,git_status}]}` for a relative directory. |
| `GET /documents/open?path=` | `{document_id,path,text,sha256,git,reading_state,default_session_id}`. Create a document record on first open. |
| `GET /documents/{id}/threads` | `{threads:[...messages,anchor,anchor_state,last_session_id...]}` for current content. |
| `POST /documents/{id}/threads` | `{version_sha256,quote,prefix,suffix,start,end,heading,question,session_id?}` → `{thread_id,job_id,session_id}`. Validate quote against the supplied version and persist atomically before enqueueing. |
| `POST /threads/{id}/messages` | `{text,session_id?}` → `{message_id,job_id,session_id}`; use `last_session_id` when omitted. |
| `GET /jobs/{id}/events?after=N` | Server-sent events in increasing `seq`; replay stored events with sequence `>N`, then stream new events. A reconnect cannot duplicate persisted messages. |
| `POST /jobs/{id}/cancel` | Request cancellation of the job; for an app-owned child, terminate that child after five seconds if needed. Invoke a provider-wide abort only if it is known to target the app's own active turn. Never kill a separate user's terminal process. |
| `POST /jobs/{id}/permission-decisions` | `{request_id,decision,remember?}` responds to an actual provider-generated permission request. Validate the decision against the provider-advertised options and forward it to that provider; return 409 if the request is stale. Never manufacture application-level permission prompts. |
| `GET /agent-sessions?provider=&workspace=` | `{sessions:[...],capabilities}`; include provider-supported existing sessions. |
| `POST /agent-sessions` | `{provider,workspace_id,display_name?}` → new app/provider session. |
| `PUT /documents/{id}/default-session` | `{session_id}` changes only the default for future threads. |
| `PUT /threads/{id}/anchor` | `{version_sha256,quote,prefix,suffix,start,end,heading}` explicitly reanchors after version validation. |
| `PUT /documents/{id}/reading-state` | `{version_sha256,heading,offset,scroll_fraction}`; return saved state. |
| `POST /documents/{id}/review-marks` | `{version_sha256,anchor}` → explicit review mark. |
| `GET /documents/{id}/diff?from=&to=` | Return textual diff for recorded app snapshot IDs or valid Git refs; no implicit push. |
| `POST /documents/{id}/previews` | `{instruction,base_sha256,session_id}` → preview job. See §14. |
| `POST /previews/{id}/apply` | `{base_sha256}` → updated document/version, or 409 on mismatch. |
| `GET /terminals` / `POST /terminals` | List or create terminal; create body `{cwd?,label?}` returns `{id,label,state}`. |
| `DELETE /terminals/{id}` | Explicitly close terminal/processes; UI presents ordinary close confirmation. |
| `GET /terminals/{id}/ws` | Authenticated WebSocket; protocol in §11. |
| `GET /health` | Process health only; no private paths, tokens or terminal content. |

`DOCUMENT_VERSION_CONFLICT`, `SESSION_BUSY`, `SESSION_NOT_FOUND`, `PROVIDER_UNAVAILABLE`, `PROVIDER_AUTH_REQUIRED`, `PREVIEW_UNAVAILABLE`, `TERMINAL_GONE`, `IDEMPOTENCY_KEY_REUSED` and `INVALID_ANCHOR` are stable error codes. A job status is one of `queued`, `running`, `waiting_provider_permission`, `completed`, `failed`, `cancelled`, `interrupted`. On a real permission event, persist the provider request ID and options, emit `permission_request`, and set `waiting_provider_permission`; after the user responds and the provider accepts, return to `running`. If the provider cannot relay approvals programmatically, preserve the question and show the available terminal fallback instead of waiting indefinitely. Retrying a request with a client-generated `Idempotency-Key` must not create duplicate threads/jobs or edits; store request keys for 24 hours. Bind each key to the authenticated user, HTTP method, path and a SHA-256 of the request body; return the stored response for an exact retry and return 409 `IDEMPOTENCY_KEY_REUSED` if the same key is used for a different request.

## 14. Edits, preview, Git and concurrency

- Direct explicit edits use the agent's existing configured permissions. The app detects the resulting local file change, snapshots its pre/post contents when possible, shows the actual diff, and recalculates anchors. This path does not insert a new app confirmation dialog after the user's instruction.
- In optional preview mode, run the proposal in an isolated temporary copy or provider-supported read-only/proposal mode and return a proposed patch without touching the user's worktree. Do not claim a prompt alone enforces isolation. If this capability cannot be guaranteed for the installed provider, report `PREVIEW_UNAVAILABLE` and offer the normal terminal/direct-edit flow.
- The Apply operation compares the exact current file SHA-256 with the preview base, rejects mismatch, applies atomically, preserves file permissions and records a new version. Do not overwrite newer user/terminal changes. Prevent path traversal and symlink replacement from escaping the configured document root.
- The file tree and document banner distinguish untracked, modified, committed and remote sync state where Git can determine it. Do not automatically `git reset`, discard local edits, rewrite history, commit or push. The user may perform their normal Git operations in the unrestricted terminal.
- When an agent or external terminal edits a file during a reader question, keep the original quote/version with the job and show the reader that the file changed; do not silently replace an in-progress selection.

## 15. Authentication and minimal deployment

Use a single-user token-based login by default, not a multi-user account system. Bind to `127.0.0.1` by default. On first setup, generate a random 32-byte access token, save it only in a mode-0600 user configuration file, and print the local setup instructions once; alternatively the user supplies it through an environment variable. The login session lasts 30 days and is renewed while the user is active. Reuse it across normal operations. Do not print, include in URLs, or log the token.

Use an HttpOnly, SameSite=Strict cookie; include Secure when configured behind HTTPS. Mutating HTTP endpoints require an `X-CSRF-Token` value tied to the session and validate browser `Origin`. Derive the per-login CSRF token using HMAC-SHA-256 from a server secret stored in the mode-0600 app configuration and the raw session cookie; return it from authenticated bootstrap so a restored browser can recover it and multiple tabs share it without invalidating each other. WebSocket upgrades require the authenticated cookie, the CSRF token carried in a WebSocket subprotocol, and allowed Origin. Validate forwarded headers only from explicitly configured trusted proxies. Same-origin hosting must work without CORS configuration.

The README must provide two concrete ways to access the service remotely: an existing private VPN/tunnel, and HTTPS through an authenticated reverse proxy. These are deployment options, not mandatory new accounts. Never default to an unauthenticated shell on a public interface. The setup script runs as the current user and must not install system packages automatically; if Git, tmux, Python or Node is missing, name the missing dependency and give the corresponding user-actionable installation guidance. A user-level service-manager example is optional; `scripts/start.sh` must run without it.

Default config: loopback bind `127.0.0.1`, port `8765`, state directory `~/.local/share/wiki-agent-workspace`, workspace root supplied during setup, default provider `opencode` if detected, otherwise `codex` if detected, otherwise unset with a visible explanation. The user can switch provider without reinstalling.

## 16. Failure behavior and observability

- A single missing dependency, expired provider login, missing session or Git failure must produce a precise, actionable error while leaving unrelated features working. Missing tmux disables only terminal creation/reattach and shows the installation instruction.
- Server restart does not corrupt SQLite data; transactions either commit completely or are rolled back. Interrupted jobs are marked `interrupted`, not `completed`, and may be explicitly retried. Never rerun a potentially destructive agent turn automatically after a restart.
- A lost browser connection leaves the server-side agent job running when the provider process remains alive. Reconnecting replays persisted SSE events from the last sequence number. If the service itself terminates the child, show the true interrupted status.
- State and audit records are local. Show provider/tool errors without exposing environment variables or tokens. Offer export/backup of the SQLite database and associated app-managed document snapshots.

## 17. Mandatory acceptance tests

Implement automated tests where feasible; manual provider checks require the user's own server and are recorded separately.

**A. Service and setup**

1. Start the app as an unprivileged Linux user with an existing Git worktree and an untracked Markdown file; login once and open that file. Restart only the Web server; discussions and reading state remain.
2. Reuse an existing user-level fake agent executable and `HOME` without installing a model provider key into the app. A missing agent must not stop the terminal or reader.
3. Verify unauthenticated HTTP and WebSocket access cannot execute shell commands; one valid login permits normal repeated actions without additional app approval dialogs.

**B. Reader, comments, sessions**

4. Select a few words and a cross-line range in formatted Markdown. The floating input shows the exact quote, creates the correct anchored discussion and restores it after reload.
5. On one document, create discussions at two locations: both use its default native session. Explicitly select a new session; old messages retain their historical session and later replies default to that thread's last session. Verify session IDs never cross providers.
6. Link an existing provider session when available. Simulate missing session, provider failure, busy session and reconnect: no silent new session, no lost question, and a usable path to retry or explicitly select another session.
7. Insert content before an annotation, duplicate its quote, modify the quote and delete it; verify exact and context-unique matches attach, and ambiguity/deletion show the correct non-attached state. Preserve the original version and a manual reanchor.
8. Save reading position; reload the same version; then change a reviewed section and verify `needs review`. No scroll event alone changes review status.

**C. Terminal**

9. In the real tmux-backed terminal run an interactive shell, write a marker, start a process that keeps running, close the browser connection, reconnect and verify the same tmux session/process and marker state. Restart the Web server and repeat. The simulated browser interface cannot count as this test.
10. Verify terminal resize, Ctrl-C, ANSI/256/true-color output when supported, Unicode output split across byte chunks, non-UTF-8 `onBinary` mouse-report input, and at least one full-screen terminal program. Verify retained tmux history is accessible after reconnect and clearly distinguish it from the browser's xterm buffer. Create a second terminal and verify the two are independent.
11. Terminate a tmux session or simulate host reboot; the UI shows `exited`/`unavailable`, not a falsely resumed shell. Explicitly creating a new terminal works.

**D. Updates and safety**

12. Ask a fake agent to make a user-authorized change; no extra app permission prompt; see the actual diff and updated document. In preview mode, confirm there is no change before Apply. Modify the same file externally between preview and Apply; verify 409 and preservation of the external change.
13. Verify path traversal, symlink escape, script tags, unsafe URLs, WebSocket origin checks, repeated message idempotency, and no tokens in logs. These controls must not impose additional prompts during normal use.
14. Run the frontend browser tests and backend suite from commands stated in README. Include a recorded manual checklist for real installed OpenCode/Codex versions and terminal colors.

## 18. Explicit implementation handoff

Implement the entire v1 above; do not stop after generating another mockup or planning document. Follow the existing user CLI permissions and provider authentication instead of introducing new mandatory approvals. Make errors visible, preserve the user's data and workflow, and include reproducible setup and test commands. Explain any feature that could not be validated on the implementation machine, especially real provider session reuse or the user's actual remote environment. The provided HTML is a visual interaction reference only.

## 19. Verification notes and remaining real-environment tests

The following conclusions were checked against primary documentation on 2026-11-05:

1. OpenCode documents session IDs, structured CLI output, an HTTP session API, and an existing TUI server; starting a second server is not the same as connecting to the first. Sources: [CLI](https://opencode.ai/docs/cli/) and [server](https://opencode.ai/docs/server/). Shared live-session concurrency is not established by these pages and must be tested against the installed version.
2. Codex documents JSONL events and session resumption, saved CLI authentication by default, and a default read-only sandbox for `codex exec`. Source: [non-interactive mode](https://developers.openai.com/codex/noninteractive/). Effective sandbox settings and saved authentication on the user's host have not been inspected.
3. tmux documents detach/reattach of running programs and configurable pane history. RGB may require correctly advertised terminal capabilities; see [Getting Started](https://github.com/tmux/tmux/wiki/Getting-Started) and [tmux FAQ](https://github.com/tmux/tmux/wiki/FAQ). Neither detach/reattach nor a Web server restart implies survival of an OS reboot.
4. xterm.js documents separate text and binary input callbacks and raw-byte writes that are interpreted as UTF-8. Source: [Terminal API](https://xtermjs.org/docs/api/terminal/classes/terminal/). Font, RGB, IME, alternate-screen and historical-buffer fidelity must be tested with the user's actual browser and target programs.

These checks validate documented capabilities only. The application, the user's actual remote host, installed CLI versions, authentication, concurrent-session behavior and networking have **not** been tested or deployed.
