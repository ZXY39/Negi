#!/usr/bin/env sh
set -eu

export G3KU_RESOURCE_SEED_ROOT="${G3KU_RESOURCE_SEED_ROOT:-/opt/g3ku-seed}"
export G3KU_INTERNAL_CALLBACK_URL="${G3KU_INTERNAL_CALLBACK_URL:-http://web:${G3KU_WEB_PORT:-18790}/api/internal/task-terminal}"

exec python -m g3ku worker
