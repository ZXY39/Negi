#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN=""

# Locate the repository virtualenv python: Windows/git-bash keeps it under
# .venv/Scripts/python.exe, Linux/macOS under .venv/bin/python.
if [ -f "$PROJECT_ROOT/.venv/Scripts/python.exe" ]; then
  PYTHON_BIN="$PROJECT_ROOT/.venv/Scripts/python.exe"
elif [ -f "$PROJECT_ROOT/.venv/bin/python" ]; then
  PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
else
  echo "[g3ku] lint: no virtualenv python found under $PROJECT_ROOT/.venv" >&2
  echo "[g3ku] lint: expected .venv/Scripts/python.exe (Windows) or .venv/bin/python (Linux)" >&2
  exit 1
fi

run_check() {
  (cd "$PROJECT_ROOT" && "$PYTHON_BIN" -m ruff check .)
}

run_format_check() {
  (cd "$PROJECT_ROOT" && "$PYTHON_BIN" -m ruff format --check .)
}

status=0

echo "[g3ku] lint: ruff check ."
if ! run_check; then
  echo "[g3ku] lint: FAILED - ruff check reported violations" >&2
  status=1
fi

echo "[g3ku] lint: ruff format --check ."
if ! run_format_check; then
  echo "[g3ku] lint: FAILED - ruff format --check found unformatted files" >&2
  status=1
fi

if [ "$status" -ne 0 ]; then
  echo "[g3ku] lint: FAILED - one or more lint checks failed" >&2
  exit 1
fi

echo "[g3ku] lint: all checks passed"