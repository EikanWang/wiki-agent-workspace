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
| App-owned tmux socket name / config | `<state_dir>/tmux-socket`, `<state_dir>/tmux.conf` | socket lives in `/tmp/tmux-<uid>/<name>` |
| Config incl. secrets | `~/.config/wiki-agent-workspace/config.toml` (or `WAW_CONFIG`) | mode 0600 |
| Code, Python venv, built UI | the repository (`.venv/`, `frontend/dist/`) | rebuildable with `scripts/setup.sh` |
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

Rules the app applies when `workspace.root` differs from what the database knows:

- Old root **no longer exists** → the single workspace record is relocated to the new root (logged). Document
  identities and discussions follow the moved checkout.
- Old root **still exists** (you pointed the same state at a *different* wiki) → a second workspace is created;
  the old wiki's data stays untouched and reappears when its root is configured again.

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
| Terminal shows garbled non-ASCII | Server locale is not UTF-8 and no UTF-8 locale exists on the host (`locale -a`). Install one (e.g. `C.UTF-8`); the UI banner reports which locale is used. |
| "inotify watch limit … exhausted" banner | Raise `fs.inotify.max_user_watches` (see DEPLOYMENT.md §1). Manual refresh (`↻`) keeps working meanwhile. |
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
| `423 PATH_LOCKED` | The file or a parent folder is locked (🔒). Unlock it in the tree or header, or use your terminal (locks never restrict the shell). |
| `lock_violation` event in a discussion | An app-driven agent turn modified a locked file anyway (OpenCode CLI mode cannot be sandboxed by the app). Review the diff; revert with Git if unwanted. Codex turns on locked files run read-only. |
| "The selected text could not be located…" (`INVALID_ANCHOR`) | The offsets sent by the browser did not match the current file and the quote plus its surrounding context could not be located uniquely (e.g. the passage is repeated verbatim and the file changed while the question box was open). Refresh the document and reselect. |
| `PATH_ESCAPES_WORKSPACE` | Only raised for *writes* through a symlink that points outside the workspace (reads through such links are allowed and flagged in the tree). Edit the real target in your terminal. |
| `UNSUPPORTED_FORMAT` when opening | Only Markdown, text/source, `.docx` and `.pdf` open in the reader. Other files are listed with their icon for orientation. |
| Lost the access token | `scripts/setup.sh --show-token`. To rotate: edit `auth.access_token` (any random string), restart, log in again. |
