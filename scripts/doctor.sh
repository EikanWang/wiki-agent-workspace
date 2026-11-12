#!/usr/bin/env bash
# Check the installation feature by feature (core, server, agents, GitHub, optional features) and print how to fix
# what is missing. Changes nothing. Options: --quick (skip slow checks) --json --url URL. Exit code 1 on problems.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  echo "✗ .venv is missing: run scripts/install.sh (or scripts/setup.sh) first." >&2
  exit 1
fi
exec .venv/bin/python -m backend.app.cli doctor "$@"
