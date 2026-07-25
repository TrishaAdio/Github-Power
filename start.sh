#!/usr/bin/env bash
# Creates a venv on first run, then starts the server on port 5000.
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
      PY="$candidate"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "Python 3.10+ is required." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "  setting up virtualenv ($PY)…"
  "$PY" -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

exec ./.venv/bin/python -m gh_mcp "$@"
