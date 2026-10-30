#!/usr/bin/env bash
# Thin wrapper for uninstall.py (prefers the checkout's venv python).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_DIR="${HERMES_DIR:-$HOME/.hermes/hermes-agent}"

PY=""
for cand in "$HERMES_DIR/.venv/bin/python" "$HERMES_DIR/.venv/bin/python3"; do
  if [ -x "$cand" ]; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ]; then
  PY="$(command -v python3 || command -v python || true)"
fi

exec "$PY" "$REPO_DIR/uninstall.py" --hermes-dir "$HERMES_DIR" "$@"
