#!/usr/bin/env bash
# User-level setup for Wiki Agent Workspace. No sudo, no system packages installed.
# Usage: scripts/setup.sh [--workspace DIR] [--port N] [--bind ADDR] [--default-provider opencode|codex]
#                         [--codex-sandbox read-only|workspace-write|danger-full-access] [--show-token]
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

missing=0
need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "MISSING: $1 — $2" >&2
    missing=1
  fi
}
need git "install Git (Debian/Ubuntu: sudo apt install git; Fedora: sudo dnf install git)"
need tmux "install tmux 3.2+ (Debian/Ubuntu: sudo apt install tmux; Fedora: sudo dnf install tmux). Terminals are disabled without it; the reader still works."
need node "install Node.js 20+ (recommended 22) from https://nodejs.org or via nvm, needed only to build the frontend"
need npm "comes with Node.js"

PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then PY="$cand"; break; fi
  fi
done
if [ -z "$PY" ]; then
  echo "MISSING: Python 3.11+ — install python3.11 or newer (Debian/Ubuntu: sudo apt install python3 python3-venv; or use pyenv/uv)" >&2
  missing=1
fi
if [ "$missing" -ne 0 ] && [ -z "$PY" ]; then
  echo "Fix the missing dependencies above and re-run scripts/setup.sh" >&2
  exit 1
fi
if command -v tmux >/dev/null 2>&1; then
  tv=$(tmux -V | sed 's/[^0-9.]*\([0-9.]*\).*/\1/')
  case "$tv" in 1.*|2.*|3.0*|3.1*) echo "WARNING: tmux $tv found; 3.2 or later is recommended." >&2;; esac
fi
if command -v node >/dev/null 2>&1; then
  nv=$(node -e 'process.stdout.write(String(process.versions.node.split(".")[0]))')
  if [ "$nv" -lt 20 ]; then echo "WARNING: Node $nv is older than 20; the frontend build may fail. Node 22+ recommended." >&2; fi
fi

echo "==> Python virtual environment (.venv) using $PY"
if [ ! -x .venv/bin/python ]; then "$PY" -m venv .venv; fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.lock
.venv/bin/python -m pip install --quiet -e . --no-deps

if command -v npm >/dev/null 2>&1; then
  echo "==> Frontend build (frontend/dist)"
  (cd frontend && npm ci --no-audit --no-fund --silent 2>/dev/null || npm install --no-audit --no-fund --silent) && (cd frontend && npm run build --silent)
else
  echo "Skipping frontend build (npm missing). The API runs, but the browser UI will be unavailable until frontend/dist exists." >&2
fi

echo "==> Configuration"
.venv/bin/python -m backend.app.cli setup "$@"
echo
echo "Start the server with: scripts/start.sh"
