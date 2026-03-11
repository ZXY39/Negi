#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BOOTSTRAP="$SCRIPT_DIR/g3ku_bootstrap.py"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [ ! -f "$VENV_PYTHON" ]; then
  VENV_PYTHON="$SCRIPT_DIR/.venv/Scripts/python.exe"
fi

if [ ! -f "$BOOTSTRAP" ]; then
  echo "[g3ku] Missing bootstrap script: $BOOTSTRAP" >&2
  exit 1
fi

if [ -f "$VENV_PYTHON" ] && "$VENV_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  exec "$VENV_PYTHON" "$BOOTSTRAP" "$@"
fi

if command -v py >/dev/null 2>&1; then
  exec py -3 "$BOOTSTRAP" "$@"
fi

if command -v python >/dev/null 2>&1; then
  exec python "$BOOTSTRAP" "$@"
fi

echo "[g3ku] Python not found. Install Python or create a local .venv first." >&2
exit 1
