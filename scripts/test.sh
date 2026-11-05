#!/usr/bin/env bash
# Run the backend test suite (real git + tmux + fake agent executables); add --e2e for the Playwright browser tests.
set -euo pipefail
cd "$(dirname "$0")/.."
E2E=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--e2e" ]; then E2E=1; else ARGS+=("$a"); fi
done
.venv/bin/python -m pytest tests/backend -q --timeout 120 "${ARGS[@]}"
if [ "$E2E" -eq 1 ]; then
  (cd frontend && npx playwright test)
fi
