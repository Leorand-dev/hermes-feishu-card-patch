#!/usr/bin/env bash
# Thin wrapper: prefer the checkout's own venv python (it has ruamel.yaml for the
# config activation step), then hand over to install.py.
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
if [ -z "$PY" ]; then
  echo "error: no python3 interpreter found" >&2
  exit 1
fi

exec "$PY" "$REPO_DIR/install.py" --hermes-dir "$HERMES_DIR" "$@"
