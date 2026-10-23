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
  echo "[negi] lint: no virtualenv python found under $PROJECT_ROOT/.venv" >&2
  echo "[negi] lint: expected .venv/Scripts/python.exe (Windows) or .venv/bin/python (Linux)" >&2
  exit 1
fi

# First-party scope: `.` would also scan the untracked scratch files that
# accumulate at the repo root, and vendored trees are excluded in pyproject.
SCOPE="g3ku main tools scripts bridges tests negi_bootstrap.py"

run_check() {
  (cd "$PROJECT_ROOT" && "$PYTHON_BIN" -m ruff check $SCOPE)
}

status=0

echo "[negi] lint: ruff check (first-party)"
if ! run_check; then
  echo "[negi] lint: FAILED - ruff check reported violations" >&2
  status=1
fi

if [ "$status" -ne 0 ]; then
  echo "[negi] lint: FAILED - one or more lint checks failed" >&2
  exit 1
fi

echo "[negi] lint: all checks passed"