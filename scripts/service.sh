#!/usr/bin/env bash
# Run Wiki Agent Workspace in the background as your own user, and keep it running.
#
#   scripts/service.sh install    # a systemd *user* service (starts now and at boot); without systemd: start in the background
#   scripts/service.sh start | stop | restart | status | logs [-f] | uninstall
#
# No root needed. With systemd the service starts at boot only if "lingering" is on for your user
# (`loginctl enable-linger $USER`; install tries it). Without systemd (containers, some HPC hosts) the server runs
# detached with a pid file; `start` again after a reboot (or add `scripts/service.sh start` to your crontab @reboot).
#
# Environment: WAW_CONFIG (another config file), WAW_SERVICE_NAME (default wiki-agent-workspace; one per instance),
# WAW_NO_LINGER=1 (install: do not turn lingering on; undo with `loginctl disable-linger $USER`), WAW_NO_SYSTEMD=1.
set -euo pipefail
USER="${USER:-$(id -un)}" # unset under su, cron, some containers / SSH setups
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
NAME="${WAW_SERVICE_NAME:-wiki-agent-workspace}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$NAME.service"

die() { echo "error: $*" >&2; exit 1; }
[ -x .venv/bin/python ] || die "run scripts/install.sh (or scripts/setup.sh) first"

py() { .venv/bin/python -c "$1"; }
STATE_DIR="$(py 'from backend.app.config import load_settings; print(load_settings().state_dir)')"
URL="$(py 'from backend.app.config import load_settings as l; s=l(); h="127.0.0.1" if s.bind in ("0.0.0.0","::","") else s.bind; print(f"http://{h}:{s.port}")')"
LOG="$STATE_DIR/server.log"
PID="$STATE_DIR/$NAME.pid"
mkdir -p "$STATE_DIR"

have_systemd() {
  [ -z "${WAW_NO_SYSTEMD:-}" ] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}
uses_systemd() { have_systemd && [ -f "$UNIT" ]; }

healthy() { curl -fsS --noproxy '*' --max-time 3 "$URL/api/v1/health" >/dev/null 2>&1; } # our own server: never via a proxy
wait_healthy() {
  for _ in $(seq 1 60); do healthy && return 0; sleep 1; done
  return 1
}

# One systemd Environment= line, quoted: `"` and `\` escaped, `%` doubled (systemd specifiers).
env_line() {
  local v="$2"
  v="${v//\\/\\\\}"; v="${v//\"/\\\"}"; v="${v//%/%%}"
  printf 'Environment="%s=%s"\n' "$1" "$v"
}
# What the service must keep from your shell: PATH (the agent CLIs, gh…), HOME, and, when set, the proxy and the CA
# certificates — a systemd user service starts with none of them, so behind a corporate proxy GitHub, gh and the
# agents could not connect.
KEEP_ENV="http_proxy https_proxy ftp_proxy all_proxy no_proxy HTTP_PROXY HTTPS_PROXY FTP_PROXY ALL_PROXY NO_PROXY
  SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE NODE_EXTRA_CA_CERTS GIT_SSL_CAINFO
  LANG LC_ALL CODEX_HOME CLAUDE_CONFIG_DIR XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME WAW_CONFIG"
unit_env() {
  env_line PATH "$PATH"
  env_line HOME "$HOME"
  local k
  for k in $KEEP_ENV; do
    if [ -n "${!k:-}" ]; then env_line "$k" "${!k}"; fi
  done
}

write_unit() {
  mkdir -p "$UNIT_DIR"
  # written with mode 0600: the proxy settings may hold a password
  ( umask 077; cat > "$UNIT" <<EOF
[Unit]
Description=Wiki Agent Workspace ($NAME)
After=network-online.target

[Service]
WorkingDirectory=$ROOT
ExecStart=$ROOT/scripts/start.sh
Restart=on-failure
RestartSec=3
$(unit_env)
# tmux sessions of the app's terminals must survive a restart of the server
KillMode=process

[Install]
WantedBy=default.target
EOF
  )
}

bg_running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }

bg_start() {
  if bg_running; then echo "already running (pid $(cat "$PID")) — $URL"; return 0; fi
  if healthy; then echo "a server already answers at $URL (started some other way); not starting another"; return 0; fi
  echo "starting in the background (log: $LOG)"
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$ROOT/scripts/start.sh" >>"$LOG" 2>&1 < /dev/null &
  else
    nohup "$ROOT/scripts/start.sh" >>"$LOG" 2>&1 < /dev/null &
  fi
  echo $! > "$PID"
}

bg_stop() {
  if ! bg_running; then echo "not running"; rm -f "$PID"; return 0; fi
  local p; p="$(cat "$PID")"
  # start.sh execs the Python server: the pid is the server itself
  kill -TERM "$p" 2>/dev/null || true
  for _ in $(seq 1 30); do kill -0 "$p" 2>/dev/null || break; sleep 1; done
  kill -0 "$p" 2>/dev/null && { echo "still running after 30 s; sending KILL"; kill -KILL "$p" 2>/dev/null || true; }
  rm -f "$PID"
  echo "stopped"
}

cmd="${1:-status}"
shift || true
case "$cmd" in
  install)
    if have_systemd; then
      before="$(cat "$UNIT" 2>/dev/null || true)"
      write_unit
      systemctl --user daemon-reload
      if [ -n "$before" ] && [ "$before" != "$(cat "$UNIT")" ] && systemctl --user is-active --quiet "$NAME.service"; then
        # re-run after installing an agent or changing the proxy: the running server gets the new PATH / environment
        systemctl --user enable "$NAME.service" >/dev/null 2>&1
        systemctl --user restart "$NAME.service"
        echo "restarted with the updated environment (terminals keep running)"
      else
        systemctl --user enable --now "$NAME.service"
      fi
      if [ -z "${WAW_NO_LINGER:-}" ] && command -v loginctl >/dev/null 2>&1 && [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
        loginctl enable-linger "$USER" 2>/dev/null && echo "lingering enabled: the service starts at boot and keeps running after you log out" \
          || echo "note: to keep it running after you log out and start it at boot, run: sudo loginctl enable-linger $USER"
      fi
      echo "installed systemd user service $NAME ($UNIT)"
    else
      echo "systemd user services are not available here: running in the background instead"
      bg_start
    fi
    if wait_healthy; then echo "✓ the server answers at $URL"; else echo "✗ the server does not answer at $URL yet — see: scripts/service.sh logs" >&2; exit 1; fi
    ;;
  uninstall)
    if have_systemd && [ -f "$UNIT" ]; then
      systemctl --user disable --now "$NAME.service" || true
      rm -f "$UNIT"
      systemctl --user daemon-reload
      echo "removed the systemd user service $NAME (your data and terminals' tmux sessions are kept)"
    else
      bg_stop
    fi
    ;;
  start)
    if uses_systemd; then systemctl --user start "$NAME.service"; else bg_start; fi
    if wait_healthy; then echo "✓ $URL"; else echo "✗ not answering at $URL — see: scripts/service.sh logs" >&2; exit 1; fi
    ;;
  stop)
    if uses_systemd; then systemctl --user stop "$NAME.service" && echo "stopped"; else bg_stop; fi
    ;;
  restart)
    if uses_systemd; then systemctl --user restart "$NAME.service"; else bg_stop; bg_start; fi
    if wait_healthy; then echo "✓ $URL"; else echo "✗ not answering at $URL — see: scripts/service.sh logs" >&2; exit 1; fi
    ;;
  status)
    if uses_systemd; then
      systemctl --user --no-pager status "$NAME.service" | head -5 || true
    elif bg_running; then
      echo "running in the background (pid $(cat "$PID")), log: $LOG"
    else
      echo "not running as a service (scripts/service.sh install, or scripts/start.sh in a terminal)"
    fi
    if healthy; then echo "✓ answering at $URL"; else echo "✗ not answering at $URL"; exit 3; fi
    ;;
  logs)
    if uses_systemd; then journalctl --user -u "$NAME.service" --no-pager "${@:--n200}"; else
      [ -f "$LOG" ] || die "no log yet ($LOG)"
      if [ "${1:-}" = "-f" ]; then tail -n 100 -f "$LOG"; else tail -n 200 "$LOG"; fi
    fi
    ;;
  *)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
