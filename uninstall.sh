#!/usr/bin/env bash
# Negi uninstaller (macOS / Linux).
#
#   ./uninstall.sh [--keep-data | --purge-data] [--yes]
#
# Deleting data is the only irreversible step here, so the default branch is
# "keep": the program goes, the operator's sessions, tasks and key envelopes
# stay where they are and the next install adopts them.
set -euo pipefail

BIN_DIR="$HOME/.local/bin"
STATE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/negi"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"
KEEP_DATA=1
DECIDED=0
ROOT=''

log() { printf '[uninstall] %s\n' "$*"; }
fail() {
  printf '[uninstall] %s\n' "$*" >&2
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --keep-data)
      KEEP_DATA=1
      DECIDED=1
      shift
      ;;
    --purge-data)
      KEEP_DATA=0
      DECIDED=1
      shift
      ;;
    --yes)
      DECIDED=1
      shift
      ;;
    -h | --help)
      echo 'Usage: uninstall.sh [--keep-data | --purge-data] [--yes]'
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

if [ -f "$STATE_DIR/install-root.env" ]; then
  # shellcheck disable=SC1090
  . "$STATE_DIR/install-root.env"
fi
ROOT="${G3KU_HOME:-$HOME/Negi}"
PATH_ENTRY_ADDED="${NEGI_PATH_ENTRY_ADDED:-false}"
[ -d "$ROOT" ] || fail "$ROOT is not an install root (nothing to uninstall there)"

DATA_DIR="$ROOT/.g3ku"
if [ -n "${G3KU_DATA_DIR:-}" ] && [ -d "$G3KU_DATA_DIR" ]; then
  DATA_DIR="$G3KU_DATA_DIR"
elif [ -f "$ROOT/.g3ku/data-root.json" ]; then
  pointed="$(tr -d ' \n\t{}"' <"$ROOT/.g3ku/data-root.json" | sed -n 's/.*data_dir:\([^,]*\).*/\1/p')"
  if [ -n "$pointed" ] && [ -d "$pointed" ]; then DATA_DIR="$pointed"; fi
fi

stop_runtime() {
  local port=18790
  if [ -f "$ROOT/.g3ku/config.json" ]; then
    port="$(tr -d ' \n' <"$ROOT/.g3ku/config.json" | sed -n 's/.*"port":\([0-9]*\).*/\1/p')"
    [ -n "$port" ] || port=18790
  fi
  if pgrep -f "g3ku (web|worker)|negi_bootstrap\.py (web|start)" >/dev/null 2>&1; then
    log 'requesting a graceful exit from the running Negi...'
    curl -fsS -m 20 -X POST -H 'Content-Type: application/json' \
      -d '{"pause_running_work":true}' "http://127.0.0.1:$port/api/bootstrap/exit" >/dev/null 2>&1 ||
      log 'the exit endpoint did not answer; waiting anyway'
    for _ in $(seq 1 80); do
      pgrep -f "g3ku (web|worker)|negi_bootstrap\.py (web|start)" >/dev/null 2>&1 || return 0
      sleep 0.5
    done
    fail 'Negi processes are still running; nothing was deleted'
  fi
}

assert_deletable() {
  # Only ever delete a directory we can prove belongs to Negi: the guard is a
  # marker file, not a name match, because a mistyped path here is unrecoverable.
  local path="$1" kind="$2" marker
  shift 2
  [ -n "$path" ] || fail "refusing to delete an empty path ($kind)"
  case "$path" in
    / | "$HOME") fail "refusing to delete $path ($kind)" ;;
  esac
  [ -d "$path" ] || fail "refusing to delete a missing path ($kind): $path"
  for marker in "$@"; do
    if [ -e "$path/$marker" ]; then return 0; fi
  done
  fail "refusing to delete $path: no Negi marker found ($kind)"
}

ask() {
  local size=''
  if command -v du >/dev/null 2>&1; then size="$(du -sh "$DATA_DIR" 2>/dev/null | cut -f1)"; fi
  echo
  echo "Negi 将从这台电脑卸载。数据目录：$DATA_DIR (${size:-未知})"
  echo "  1) 保留数据并卸载（默认）—— 下次安装直接认领这些数据"
  echo "  2) 全部删除 —— 连同上述目录一起删掉，无法恢复"
  printf '选择 [1/2]（回车=1）: '
  local answer=''
  if read -r answer 2>/dev/null; then
    case "$answer" in
      2 | purge) KEEP_DATA=0 ;;
      *) KEEP_DATA=1 ;;
    esac
  else
    log 'no terminal to ask on; keeping the data root'
  fi
}

log "program directory: $ROOT"
stop_runtime
if [ "$DECIDED" -eq 0 ]; then ask; fi

rm -f "$BIN_DIR/negi" && log "removed (command stub): $BIN_DIR/negi"
rm -f "$APPS_DIR/negi.desktop" && log "removed (desktop entry): $APPS_DIR/negi.desktop"
rm -f "$ICON_DIR/negi.png" && log "removed (icon): $ICON_DIR/negi.png"
if [ "$PATH_ENTRY_ADDED" = "true" ]; then
  for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
    if [ -f "$rc" ] && grep -qs '# Added by Negi install.sh' "$rc"; then
      sed -i.bak '/# Added by Negi install.sh/d; /^export PATH=".*\.local\/bin:\$PATH"$/d' "$rc"
      rm -f "$rc.bak"
      log "removed the PATH line this install added in $rc"
    fi
  done
fi
rm -rf "$STATE_DIR" && log "removed (state and pointer): $STATE_DIR"

if [ "$KEEP_DATA" -eq 0 ]; then
  # Two independent facts must hold before the data root goes: a Negi config or
  # key envelope, and at least one runtime directory. A mistyped G3KU_DATA_DIR
  # is the failure this guards against.
  assert_deletable "$DATA_DIR" 'data root' 'config.json' 'llm-config'
  data_has_runtime=0
  for dir in logs sessions tasks llm-config memory runtime; do
    if [ -e "$DATA_DIR/$dir" ]; then
      data_has_runtime=1
    fi
  done
  if [ "$data_has_runtime" -eq 0 ]; then
    fail "refusing to delete $DATA_DIR: no Negi runtime directory found (data root)"
  fi
  case "$DATA_DIR" in
    "$ROOT"*) log 'data root lives under the program directory; removed with it' ;;
    *)
      rm -rf "$DATA_DIR"
      log "removed (data root): $DATA_DIR"
      ;;
  esac
fi

assert_deletable "$ROOT" 'program directory' 'negi_bootstrap.py' '.g3ku/config.json'
if [ "$KEEP_DATA" -eq 0 ]; then
  rm -rf "$ROOT"
  log "removed (program directory): $ROOT"
else
  # "Keep data" must survive its own recursive delete: on a default install the
  # data root sits inside the program directory, so only the code goes.
  find "$ROOT" -mindepth 1 -maxdepth 1 ! -name '.g3ku' -exec rm -rf {} +
  log "removed the program files under $ROOT"
fi

if [ "$KEEP_DATA" -eq 1 ]; then
  log "kept the data root: $DATA_DIR (the next install into $ROOT adopts it)"
else
  log 'program and data removed'
fi
log 'uv and the Python it manages were left alone (shared toolchain)'
log 'done'
