#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BOOTSTRAP="$SCRIPT_DIR/g3ku_bootstrap.py"
VENV_PYTHON="$SCRIPT_DIR/.venv/Scripts/python.exe"

if [ ! -f "$BOOTSTRAP" ]; then
  echo "[g3ku] Missing bootstrap script: $BOOTSTRAP" >&2
  exit 1
fi

if [ -f "$VENV_PYTHON" ]; then
  exec "$VENV_PYTHON" "$BOOTSTRAP" "$@"
fi

if command -v python >/dev/null 2>&1; then
  exec python "$BOOTSTRAP" "$@"
fi

if command -v py >/dev/null 2>&1; then
  exec py -3 "$BOOTSTRAP" "$@"
fi

echo "[g3ku] Python not found. Install Python or create a local .venv first." >&2
exit 1
