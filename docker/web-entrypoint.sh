#!/usr/bin/env sh
set -eu

export G3KU_RESOURCE_SEED_ROOT="${G3KU_RESOURCE_SEED_ROOT:-/opt/g3ku-seed}"

exec python -m g3ku web \
  --host 0.0.0.0 \
  --port "${G3KU_WEB_PORT:-18790}" \
  --no-worker
