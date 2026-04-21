#!/usr/bin/env sh
set -eu

BIND_HOST="127.0.0.1"
PORT="18790"
OPEN_BROWSER=0
PROMPT_LOG=0
RELOAD=0
KEEP_WORKER=0

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BOOTSTRAP_SCRIPT="$SCRIPT_DIR/g3ku.sh"

usage() {
  cat <<'EOF'
Usage: ./start-g3ku.sh [--host HOST] [--port PORT] [--open-browser] [--prompt-log] [--reload] [--keep-worker]
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --host)
      [ "$#" -ge 2 ] || { echo "[g3ku] Missing value for --host" >&2; exit 1; }
      BIND_HOST="$2"
      shift 2
      ;;
    --port|-p)
      [ "$#" -ge 2 ] || { echo "[g3ku] Missing value for --port" >&2; exit 1; }
      PORT="$2"
      shift 2
      ;;
    --open-browser)
      OPEN_BROWSER=1
      shift
      ;;
    --prompt-log)
      PROMPT_LOG=1
      shift
      ;;
    --reload)
      RELOAD=1
      shift
      ;;
    --keep-worker)
      KEEP_WORKER=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[g3ku] Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [ ! -f "$BOOTSTRAP_SCRIPT" ]; then
  echo "[g3ku] Missing launcher script: $BOOTSTRAP_SCRIPT" >&2
  exit 1
fi

get_managed_pids() {
  ps -ax -o pid= -o command= | awk -v root="$SCRIPT_DIR" '
    index($0, root) &&
    (
      $0 ~ /g3ku_bootstrap\.py([[:space:]]|")*web/ ||
      $0 ~ /-m[[:space:]]+g3ku[[:space:]]+web/ ||
      $0 ~ /-m[[:space:]]+g3ku[[:space:]]+worker/
    ) { print $1 }
  '
}

stop_managed_processes() {
  pids="$(get_managed_pids | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
  [ -n "$pids" ] || return 0
  echo "[g3ku] Restarting existing g3ku web/worker processes..."
  for pid in $pids; do
    kill "$pid" 2>/dev/null || true
  done
  sleep 2
  for pid in $pids; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
}

port_listener_summary() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u | paste -sd ", " -
    return 0
  fi
  if command -v ss >/dev/null 2>&1; then
    if ss -ltn "( sport = :$PORT )" 2>/dev/null | tail -n +2 | grep -q .; then
      echo "port-in-use"
    fi
    return 0
  fi
  if command -v netstat >/dev/null 2>&1; then
    if netstat -an 2>/dev/null | grep -E "[\.\:]$PORT[[:space:]].*LISTEN" >/dev/null; then
      echo "port-in-use"
    fi
    return 0
  fi
  return 0
}

assert_start_preconditions() {
  listener_summary="$(port_listener_summary)"
  if [ -n "$listener_summary" ]; then
    echo "[g3ku] Port $PORT is already in use by: $listener_summary. Stop the existing process before starting g3ku." >&2
    exit 1
  fi

  remaining_managed="$(get_managed_pids | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
  if [ -n "$remaining_managed" ]; then
    echo "[g3ku] Existing g3ku web/worker processes are still running after restart attempt: $remaining_managed" >&2
    exit 1
  fi
}

stop_managed_processes
assert_start_preconditions

if [ "$PROMPT_LOG" -eq 1 ]; then
  export G3KU_PROMPT_TRACE=1
  echo "[g3ku] Prompt logging enabled via G3KU_PROMPT_TRACE=1."
else
  unset G3KU_PROMPT_TRACE 2>/dev/null || true
fi

if [ "$KEEP_WORKER" -eq 1 ]; then
  export G3KU_WEB_KEEP_WORKER=1
  echo "[g3ku] KeepWorker enabled; web-managed worker will be left running when the web server exits."
else
  unset G3KU_WEB_KEEP_WORKER 2>/dev/null || true
fi

if [ "$OPEN_BROWSER" -eq 1 ]; then
  (
    sleep 3
    TARGET_URL="http://$BIND_HOST:$PORT"
    if command -v open >/dev/null 2>&1; then
      open "$TARGET_URL" >/dev/null 2>&1 || true
    elif command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$TARGET_URL" >/dev/null 2>&1 || true
    fi
  ) &
fi

echo "[g3ku] Project root: $SCRIPT_DIR"
if [ "$RELOAD" -eq 1 ]; then
  echo "[g3ku] Reload mode enabled; the web runtime will not auto-start a managed worker."
else
  echo "[g3ku] Task worker will start after project unlock."
fi
echo "[g3ku] Starting web server on http://$BIND_HOST:$PORT ..."

set -- web --host "$BIND_HOST" --port "$PORT"
if [ "$RELOAD" -eq 1 ]; then
  set -- "$@" --reload
fi

exec sh "$BOOTSTRAP_SCRIPT" "$@"
