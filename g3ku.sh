#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BOOTSTRAP="$SCRIPT_DIR/g3ku_bootstrap.py"

if [ ! -f "$BOOTSTRAP" ]; then
  echo "[g3ku] Missing bootstrap script: $BOOTSTRAP" >&2
  exit 1
fi

if command -v py >/dev/null 2>&1; then
  exec py -3.14 "$BOOTSTRAP" "$@"
fi

exec python "$BOOTSTRAP" "$@"
