#!/usr/bin/env bash
# Start the Wiki Agent Workspace server as the current user (no service manager required).
# Config: $WAW_CONFIG or ~/.config/wiki-agent-workspace/config.toml (created by scripts/setup.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  echo "Run scripts/setup.sh first." >&2
  exit 1
fi
exec .venv/bin/python -m backend.app.cli serve "$@"
