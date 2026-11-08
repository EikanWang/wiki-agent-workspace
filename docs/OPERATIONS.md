# Operations: logs, backup, restore, moving, troubleshooting

## Logs

`scripts/start.sh` logs to stdout (uvicorn access log + app log). Under systemd: `journalctl --user -u
wiki-agent-workspace -f`. Logs never include the access token, cookies, document contents or terminal output.

Startup lines worth knowing:

```
terminal reconcile: {'live': 1, 'exited': 0, 'discovered': 0, 'tmux_available': True, 'error': None}
provider opencode: available=True version=1.18.31
provider codex: available=True version=codex-cli 0.155.1
filesystem watcher ready for /path/to/wiki          # may take up to ~1 min on large trees
job <id> waiting for session <id> / acquired / released
workspace root … no longer exists; relocating …     # only after you moved the wiki checkout
```

## What is where

| Data | Location | Notes |
| --- | --- | --- |
| Discussions, sessions, jobs, reading state, review marks, terminal tabs, logins | `<state_dir>/workspace.sqlite3` (+ `-wal`, `-shm` while running) | schema: [SCHEMA.md](SCHEMA.md) |
| Exact bytes of every document version referenced by discussions/previews | `<state_dir>/snapshots/<document_id>/<sha256>.md`, `<state_dir>/snapshots/previews/<preview_id>.md` | |
| Layout-view renders (PDFs of Office files) and LibreOffice profiles | `<state_dir>/renders/`, `<state_dir>/lo-profiles/` | cache, capped by `[office] cache_mb`; safe to delete at any time; not in backups. Contains rendered copies of your documents: same confidentiality as the wiki |
| App-owned tmux socket name / config | `<state_dir>/tmux-socket`, `<state_dir>/tmux.conf` | socket lives in `/tmp/tmux-<uid>/<name>` |
| Config incl. secrets | `~/.config/wiki-agent-workspace/config.toml` (or `WAW_CONFIG`) | mode 0600 |
| Code, Python venv, built UI | the repository (`.venv/`, `frontend/dist/`) | rebuildable with `scripts/setup.sh` |
| Folders added in the UI | `workspaces` table in the database | no config entry needed; removing a folder keeps its data |
| Your wiki | `workspace.root` | the app only writes there when you explicitly ask an agent to edit, or apply a preview |
| Provider transcripts | OpenCode's store / `~/.codex/sessions` | independent copies; the app keeps its own Q&A text |

## Backup

**While running** — consistent snapshot via the API (also the **Backup** button in the UI):

```bash
# log in once and keep the cookie jar
curl -s -c jar -H 'origin: http://127.0.0.1:8765' -H 'content-type: application/json' \
     -d "{\"token\":\"$(grep access_token ~/.config/wiki-agent-workspace/config.toml | cut -d'"' -f2)\"}" \
     http://127.0.0.1:8765/api/v1/auth/login >/dev/null
curl -s -b jar http://127.0.0.1:8765/api/v1/export -o waw-backup-$(date +%F).tar.gz
```
The archive contains `workspace.sqlite3` (a consistent `sqlite3 .backup` copy) and `snapshots/`.

**While stopped** — just copy the folder: `tar czf waw-backup.tar.gz -C ~/.local/share wiki-agent-workspace`.
A clean stop checkpoints the write-ahead log, so `workspace.sqlite3` alone is complete; include the `-wal` file
anyway if you copy a running instance by hand.

Also back up the config file (token + server secret) if you want logins to survive a restore.

## Restore

```bash
systemctl --user stop wiki-agent-workspace            # or Ctrl-C
mkdir -p ~/.local/share/wiki-agent-workspace
tar xzf waw-backup-2026-11-05.tar.gz -C ~/.local/share/wiki-agent-workspace   # yields workspace.sqlite3 + snapshots/
scripts/start.sh
```
Migrations run automatically if the backup came from an older version.

## Moving or cloning an installation

The state directory is self-contained and all internal references are relative to it.

1. Stop the server.
2. Copy the state directory (`cp -a` / `rsync -a`) to its new place, and the config file if changing machines.
3. In the config set `server.state_dir` (and `workspace.root` if the wiki moved).
4. Start. Documents, discussions, reading positions, versions and *Recent* are back.

Rules the app applies when `workspace.root` (the primary folder) differs from what the database knows:

- Old root **no longer exists** and it was the only folder → its workspace record is relocated to the new root
  (logged). Document identities and discussions follow the moved checkout.
- Otherwise (old root still exists, or several folders are known) → a new workspace record is created; the other
  folder's data stays untouched and reappears when that folder is configured or added again.

Additional folders added from the UI are stored by absolute path. If such a folder moved, it is listed as *missing*
in the file tree: remove it and add the new location (its old discussions stay attached to the old record), or move
the folder back.

Terminals: on the **same host**, the tmux socket name travels with the folder (`tmux-socket`), so live sessions
reattach after the move. On another host, running processes obviously don't move; tabs show `exited` and can be
dismissed.

## Routine maintenance

- Database size grows with discussions and job events (a few KB per turn) and with snapshots (one copy of a
  document per distinct version it was seen in). Nothing is auto-deleted. Prune with SQL if ever needed (delete
  `job_events` of old completed jobs; snapshots not referenced by `document_versions.snapshot_ref` or
  `previews.proposed_snapshot_ref` can be removed).
- `idempotency_keys` are purged after 24 h automatically; `login_sessions` on logout or expiry.
- Inspect data directly: `sqlite3 ~/.local/share/wiki-agent-workspace/workspace.sqlite3` (see queries in
  [SCHEMA.md](SCHEMA.md)). Prefer the API or a stopped server for writes.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Browser can't reach `http://host:8765` from another machine | Server binds to `127.0.0.1` by default. Use an SSH tunnel, or set `server.bind = "0.0.0.0"` and restart. Check `ss -ltn \| grep 8765` and the host firewall. |
| Browser shows a proxy "Access Denied" page | The browser sent the private address through a corporate proxy. Add the host/IP to the proxy bypass (`no_proxy`) list, or use the tunnel. |
| `403 ORIGIN_NOT_ALLOWED` / `CSRF_INVALID` | Browser `Origin` doesn't match the `Host` the server sees (reverse proxy rewriting Host, or different hostname/port). Add the public origin to `allowed_origins`, set `trusted_proxies` so `X-Forwarded-Host` is honoured, or open the UI via the same URL the API uses. |
| Login works, then immediately logged out behind HTTPS | `cookie_secure` mismatch or proxy not forwarding cookies. Set `cookie_secure = true` for HTTPS, `false` for plain HTTP. |
| "tmux is not installed" banner; no terminals | Install tmux 3.2+ and restart. Reader and discussions work regardless. |
| Terminal tab shows `(exited)` | The shell ended (`exit`, crash, host reboot). Dismiss it with `×` / **Clear exited** and create a new one. Never auto-replaced, by design. |
| Ctrl+T / Ctrl+W / Ctrl+N open or close browser tabs instead of reaching the terminal | Browsers reserve these in normal tabs; pages cannot override them. Use **⛶ Full screen** (keyboard capture in Chrome/Edge; hold Esc to leave), **Keys ▾**, or sticky **Ctrl** then the key. |
| Terminal shows garbled non-ASCII | Server locale is not UTF-8 and no UTF-8 locale exists on the host (`locale -a`). Install one (e.g. `C.UTF-8`); the UI banner reports which locale is used. |
| "inotify watch limit … exhausted" banner (more likely with several folders: each folder has its own watcher) | Raise `fs.inotify.max_user_watches` (see DEPLOYMENT.md §1). Manual refresh (`↻`) keeps working meanwhile. |
| "filesystem watcher process exited; use manual refresh" (builds before 2026-11-06) | A large change batch arriving in pieces was mistaken for an exit under uvloop. Fixed: the watcher reads partial batches and restarts itself if its process really dies (log: `filesystem watcher … lost …; restarting`). On an old build, restart the server. |
| "filesystem watcher stopped after 5 restarts" | The watcher process keeps dying. See `server.log` for the reason (`lost: … exit code`); fix it and restart the server. `↻` works meanwhile. |
| Tree shows "Could not refresh" / "cannot reach the server", or ↻ seemed to spin forever (builds before 2026-11-07) | The browser could not reach the server (network, proxy) or, on older builds, the tree flooded it: every file change reloaded every folder ever expanded, including deleted ones (`server.log` full of `GET /api/v1/files … 404`). Fixed: only visible, changed folders reload. Reload the page once; old remembered folders are forgotten automatically. |
| `FILE_NOT_FOUND: <path> not found` right after loading the page (builds before 2026-11-08) | The app reopened a document remembered by an old build without its folder, inside another folder. Fixed: only the folder's own last document or tabs are reopened, and ones that no longer exist are dropped quietly. Reload once. |
| "The file watcher is still indexing" banner for a long time | Very large trees (tens of thousands of directories, e.g. `node_modules`, corpora) take a while to register; use `↻` meanwhile. Excluded automatically: `.git`, `node_modules`, `.codex`, `.opencode`, `__pycache__`. |
| Discussion fails with `PROVIDER_AUTH_REQUIRED` | The provider's own login expired or is missing. Log in **in the Terminal tab as the same user** (`opencode auth login` / `codex login`), then **Retry**. The exact provider message is shown. |
| `SESSION_NOT_FOUND` | The provider no longer has that session (deleted/compacted/another machine). Link another session or start a new one from the header; the question is kept. |
| `SESSION_BUSY` / "waiting for session" | Another turn (possibly your interactive TUI) is using that session. Finish it, or answer with a new session. |
| `PROVIDER_UNAVAILABLE` | Executable not found or `--version` fails; the bootstrap error text names the path checked. Fix `agents.<provider>.bin` or PATH for the service user. |
| Codex turn fails with "sandbox read-only" | `agents.codex.sandbox` is `read-only`; set `workspace-write` if you want app-driven edits. |
| Preview: `PREVIEW_UNAVAILABLE` | Workspace too large to copy (512 MiB cap) or provider mode without isolation. Use the direct-edit flow. |
| Apply preview → `409 DOCUMENT_VERSION_CONFLICT` | The file changed after the preview was generated; the newer change was preserved. Refresh and generate a new preview. |
| Selection popover doesn't appear | It appears ~350 ms after releasing the mouse on a non-empty selection inside the document; on touch devices use the native selection then **Ask** / **Ask about selection**. |
| Startup fails with `FOREIGN KEY constraint failed` / `_alembic_tmp_*` table present | An older build ran a SQLite table-rebuild migration with foreign keys on and was interrupted. Current builds run migrations with the pragma off and drop the leftover temp table automatically; just restart. Data is intact (the failed step was rolled back). |
| `Address already in use` at start | Another instance (or something else) uses the port: `ss -ltnp \| grep 8765`; change `server.port`. |
| Jobs show `interrupted` after a restart | Expected: the server stopped while they were running. They are never re-run automatically; click **Retry**. |
| Terminal copy shows "The browser blocked the clipboard" | On plain `http://` (not localhost) a clipboard write needs a recent click/key press. Click **Copy now** in the note or the toolbar **Copy** button; serve over HTTPS (or an SSH tunnel to localhost) to avoid it. |
| Editor: "The file was changed on disk after you started editing" | Someone (agent, terminal, Git) changed the file since you opened the editor. Choose *Overwrite with mine* or *Discard mine, reload*; *Copy my text* keeps your version on the clipboard. |
| `NOT_EDITABLE` | Only Markdown and text/source files can be edited in the browser; Word, PowerPoint and PDF are read-only. |
| `PUSH_REJECTED` | The remote branch has commits you don't have. The app never force-pushes: `git pull --rebase` (or merge) in the terminal, then push again. |
| `PUSH_AUTH` | `git push` was refused by the remote's authentication. Fix credentials/SSH keys in the terminal (`gh auth login`, `ssh -T git@github.com`, credential helper). |
| `GH_UNAVAILABLE` / `NOT_GITHUB` | Pull requests need the GitHub CLI logged in (`gh auth login`) and an `origin` on github.com. |
| `GIT_IDENTITY_MISSING` | Set `git config --global user.name` / `user.email` for the server user; commits are made with that identity. |
| `423 PATH_LOCKED` | The file or a parent folder is locked (🔒). Unlock it in the tree or header, or use your terminal (locks never restrict the shell). |
| `lock_violation` event in a discussion | An app-driven agent turn modified a locked file anyway (OpenCode CLI mode cannot be sandboxed by the app). Review the diff; revert with Git if unwanted. Codex turns on locked files run read-only. |
| "The selected text could not be located…" (`INVALID_ANCHOR`) | The offsets sent by the browser did not match the current file and the quote plus its surrounding context could not be located uniquely (e.g. the passage is repeated verbatim and the file changed while the question box was open). Refresh the document and reselect. |
| `PATH_ESCAPES_WORKSPACE` | Only raised for *writes* through a symlink that points outside the workspace (reads through such links are allowed and flagged in the tree). Edit the real target in your terminal. |
| `UNSUPPORTED_FORMAT` when opening | The reader opens Markdown, text/source, PDF, Word, PowerPoint, Visio and OpenDocument files. Other files are listed with their icon for orientation. |
| `RENDER_FAILED` | The Text view could not parse the Word/PowerPoint/Visio file (corrupt, password-protected, or not Office Open XML). Try the **Layout** view. |
| Layout view: "LibreOffice is not installed" | Install it (DEPLOYMENT.md §1) or set `[office] soffice`, then restart. The Text view works meanwhile. |
| Layout view: "could not be shown" / "did not finish within … s" | LibreOffice failed on this file (damaged, password-protected) or took longer than `[office] timeout`. `server.log` has the reason (`waw.office`). **Try again** re-runs it; raise `timeout` for huge decks. |
| Layout view stays on "Waiting for another document" | `[office] max_parallel` conversions are already running; it starts when one finishes. |
| Layout view: "the file is … MiB" | Above `[office] max_bytes`. Raise it if the server has the memory. |
| `WORKSPACE_NOT_OPEN` / folder shown as *missing* | The folder was removed from the window or no longer exists on disk. Add it again with **＋** in the file tree (data comes back), or remove the stale entry. |
| `ALREADY_OPEN` when adding a folder | That folder (or the same resolved path) is already one of the window's folders. |
| `WRONG_WORKSPACE` | A request tried to read a document through another folder; reload the page (the browser had a stale folder selection). |
| `CONFIGURED_WORKSPACE` | Folders listed in `config.toml` (`[workspace]`, `[[workspaces]]`) can only be removed there. |
| Lost the access token | `scripts/setup.sh --show-token`. To rotate: edit `auth.access_token` (any random string), restart, log in again. |
