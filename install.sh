#!/usr/bin/env bash
# G3KU one-line installer (macOS / Linux).
#
#   curl -LsSf https://github.com/ZXY39/Negi/releases/latest/download/install.sh | bash
#
# Provisions uv (and therefore Python) on a machine that has neither, fetches the
# pinned checkout, syncs the locked environment and hands off to negi_bootstrap.py.
# Runtime code is not modified by this script.
#
# Environment knobs (passed straight through to uv):
#   UV_DEFAULT_INDEX          e.g. https://pypi.tuna.tsinghua.edu.cn/simple
#   UV_PYTHON_INSTALL_MIRROR  e.g. a reachable python-build-standalone mirror
set -euo pipefail

REPO_OWNER='ZXY39'
REPO_NAME='Negi'
REF="${G3KU_REF:-v2.4.0}"
DIR="${G3KU_DIR:-$HOME/Negi}"
NO_START=0
UPGRADE=0
NO_PATH=0
NO_SHORTCUT=0
REPO_GIT="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"
REPO_ZIP="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/${REF}.zip"
UV_INSTALLER='https://astral.sh/uv/install.sh'
# Kept across upgrades: the environment and everything the operator created.
PROTECTED_ENTRIES='.venv .g3ku .git'
STAGE_DIR=''
CHECKOUT_DIR=''

cleanup() {
  if [ -n "$STAGE_DIR" ]; then
    rm -rf "$STAGE_DIR"
  fi
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
Usage: install.sh [--dir PATH] [--ref TAG] [--no-start] [--upgrade] [--no-path] [--no-shortcut]

  --dir PATH       install location (default ~/Negi)
  --ref TAG        git ref to install (default the pinned release tag)
  --no-start       prepare the environment but do not launch the web UI
  --upgrade        update the code in an existing install, keep .venv and .g3ku
  --no-path        write the command stub but leave PATH alone
  --no-shortcut    skip the desktop entry
EOF
}

log() { printf '[install] %s\n' "$*"; }

fail() {
  printf '[install] %s\n' "$*" >&2
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)
      DIR="${2:?--dir needs a path}"
      shift 2
      ;;
    --ref)
      REF="${2:?--ref needs a tag}"
      REPO_ZIP="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/${REF}.zip"
      shift 2
      ;;
    --no-start)
      NO_START=1
      shift
      ;;
    --upgrade)
      UPGRADE=1
      shift
      ;;
    --no-path)
      NO_PATH=1
      shift
      ;;
    --no-shortcut)
      NO_SHORTCUT=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "unknown argument: $1"
      ;;
  esac
done

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

ensure_uv() {
  if UV="$(find_uv)"; then
    log "using uv: $UV"
    return
  fi
  log 'uv not found, installing it from astral.sh'
  curl -LsSf "$UV_INSTALLER" | sh || fail 'uv installer failed'
  UV="$(find_uv || true)"
  [ -n "${UV}" ] || fail 'uv installer finished but uv is still not reachable; add ~/.local/bin to PATH and rerun'
  log "using uv: $UV"
}

stage_code() {
  # $1 = archive 时强制走源码包：无 git 的历史安装升级要保持原取码形态。
  # 代码一律先落到临时目录再逐顶层合并 —— `git clone` 对着一个非空目录（保留数据
  # 后重装的 .g3ku 就算非空）直接 fatal，装不上也谈不上认领。
  local mode="${1:-auto}"
  STAGE_DIR="$(mktemp -d)"
  if [ "$mode" != "archive" ] && command -v git >/dev/null 2>&1; then
    log "git clone --depth 1 --branch $REF (staged)"
    git clone --depth 1 --branch "$REF" "$REPO_GIT" "$STAGE_DIR/checkout" || fail 'git clone failed'
    CHECKOUT_DIR="$STAGE_DIR/checkout"
    return
  fi
  log "downloading $REPO_ZIP"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf "$REPO_ZIP" -o "$STAGE_DIR/source.zip" || fail 'source archive download failed'
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$STAGE_DIR/source.zip" "$REPO_ZIP" || fail 'source archive download failed'
  else
    fail 'neither curl nor wget is available to fetch the source archive'
  fi
  command -v unzip >/dev/null 2>&1 || fail 'unzip is required when git is unavailable'
  mkdir -p "$STAGE_DIR/expanded"
  unzip -q "$STAGE_DIR/source.zip" -d "$STAGE_DIR/expanded" || fail 'source archive extraction failed'
  CHECKOUT_DIR="$(find "$STAGE_DIR/expanded" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  [ -n "${CHECKOUT_DIR:-}" ] || fail "archive for $REF contained no top-level directory"
}

copy_checkout_into_root() {
  local keep_protected="${1:-0}" entry name
  # Top-level merge: environment and operator data are never touched on an
  # upgrade. Files the new release deleted stay behind until a reinstall.
  for entry in "$CHECKOUT_DIR"/.* "$CHECKOUT_DIR"/*; do
    name="$(basename "$entry")"
    case "$name" in
      .|..) continue ;;
    esac
    if [ "$keep_protected" = "1" ]; then
      case " $PROTECTED_ENTRIES " in
        *" $name "*) continue ;;
      esac
    fi
    if [ "$name" = ".git" ] && [ -d "$DIR/.git" ]; then
      continue
    fi
    [ -e "$entry" ] || continue
    cp -Rf "$entry" "$DIR"/
  done
}

update_git_checkout() {
  # 只看跟踪文件的**内容**差异：未跟踪项（用户装的 skill、桥接产物、本地脚本）在任何
  # 正常设备上都不可能为空，算进脏就把升级永久拒死。判据用 diff 而不是 status ——
  # autocrlf 设备上 git 检出 CRLF、运行时写回 LF，status 把这种字节相同的行尾差报成
  # 已修改（实测 blob 哈希一致），diff 归一后判无变化；--cached 补住只暂存的改动。
  TRACKED_DIRTY=0
  git -C "$DIR" diff --quiet --ignore-cr-at-eol || TRACKED_DIRTY=1
  git -C "$DIR" diff --cached --quiet --ignore-cr-at-eol || TRACKED_DIRTY=1
  if [ "$TRACKED_DIRTY" -ne 0 ]; then
    fail "$DIR has local edits to tracked files; commit or discard them before upgrading"
  fi
  log "git fetch --depth 1 origin $REF"
  git -C "$DIR" fetch --depth 1 origin "$REF" || fail 'git fetch failed'
  git -C "$DIR" checkout --detach FETCH_HEAD || fail 'git checkout failed'
  log "code updated to $REF"
}

update_code() {
  if [ -f "$DIR/pyproject.toml" ]; then
    if [ "$UPGRADE" -eq 0 ]; then
      log "checkout already present at $DIR, code untouched (pass --upgrade to update it)"
      return
    fi
    if [ -d "$DIR/.git" ] && command -v git >/dev/null 2>&1; then
      update_git_checkout
      return
    fi
    if [ -d "$DIR/.git" ]; then
      fail "$DIR is a git checkout but git is unavailable; install git so the upgrade stays consistent"
    fi
    log "upgrading code from the $REF source archive (keeping .venv and .g3ku)"
    stage_code archive
    copy_checkout_into_root 1
    return
  fi
  mkdir -p "$DIR"
  if [ -f "$DIR/.g3ku/config.json" ]; then
    # 卸载时选择"保留数据"的设备：代码没了、口令与任务还在原地，这次装回来
    # 直接认领，不重新初始化项目。
    log "adopting the data root already present at $DIR (config, sessions and keys kept)"
  fi
  stage_code
  copy_checkout_into_root 0
}

install_environment() {
  if [ -f "$DIR/.python-version" ]; then
    pin="$(head -n 1 "$DIR/.python-version" | tr -d '[:space:]')"
    if [ -n "$pin" ]; then
      log "uv python install $pin"
      "$UV" python install "$pin" || fail "uv python install $pin failed"
    fi
  fi
  log 'uv sync --frozen'
  (cd "$DIR" && "$UV" sync --frozen) || fail 'uv sync failed'
}

BIN_DIR="$HOME/.local/bin"
STATE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/negi"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"

path_has_bin_dir() {
  case ":$PATH:" in
    *":$BIN_DIR:"*) return 0 ;;
    *) return 1 ;;
  esac
}

add_path_entry() {
  # uv's own installer usually puts ~/.local/bin in the shell rc already; only
  # write when it is genuinely missing, and never rewrite an rc we did not touch.
  local rc
  case "${SHELL:-}" in
    */zsh) rc="$HOME/.zshrc" ;;
    */bash) rc="$HOME/.bashrc" ;;
    *) rc="$HOME/.profile" ;;
  esac
  if [ -f "$rc" ] && grep -qs '\.local/bin' "$rc"; then
    log "$BIN_DIR already referenced in $rc; leaving it alone"
    PATH_ENTRY_ADDED=false
    return 0
  fi
  if printf '\n# Added by Negi install.sh\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >>"$rc"; then
    PATH="$BIN_DIR:$PATH"
    PATH_ENTRY_ADDED=true
    log "added $BIN_DIR to PATH in $rc (open a new shell, or: source $rc)"
  else
    PATH_ENTRY_ADDED=false
    log "could not write $rc; add $BIN_DIR to PATH yourself"
  fi
}

install_entry_point() {
  mkdir -p "$BIN_DIR" "$STATE_DIR"
  cat >"$BIN_DIR/negi" <<EOF
#!/usr/bin/env sh
set -eu
if [ -z "\${G3KU_HOME:-}" ] && [ -f "$STATE_DIR/install-root.env" ]; then
  . "$STATE_DIR/install-root.env"
fi
if [ -z "\${G3KU_HOME:-}" ]; then
  echo "[negi] Negi install root is unknown. Set G3KU_HOME to the Negi folder, or run install.sh again." >&2
  exit 1
fi
NEGI_PYTHON="\$G3KU_HOME/.venv/bin/python"
if [ ! -x "\$NEGI_PYTHON" ]; then
  NEGI_PYTHON="\$G3KU_HOME/.venv/Scripts/python.exe"
fi
if [ ! -x "\$NEGI_PYTHON" ]; then
  echo "[negi] no interpreter under \$G3KU_HOME/.venv. Run install.sh again to rebuild the environment." >&2
  exit 1
fi
exec "\$NEGI_PYTHON" "\$G3KU_HOME/negi_bootstrap.py" "\$@"
EOF
  chmod +x "$BIN_DIR/negi"
  printf 'G3KU_HOME=%s\n' "$DIR" >"$STATE_DIR/install-root.env"
  printf 'NEGI_PATH_ENTRY_ADDED=%s\n' "$PATH_ENTRY_ADDED" >>"$STATE_DIR/install-root.env"
  if command -v negi >/dev/null 2>&1; then
    log "command ready: negi -> $(command -v negi)"
  else
    log "stub written to $BIN_DIR/negi but that directory is not on PATH in this session"
  fi
}

install_desktop_entry() {
  mkdir -p "$APPS_DIR" "$ICON_DIR"
  if [ -f "$DIR/assets/icon.png" ]; then
    cp -f "$DIR/assets/icon.png" "$ICON_DIR/negi.png"
  fi
  cat >"$APPS_DIR/negi.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Negi
Comment=Start Negi and open the browser
Exec=$BIN_DIR/negi start --hidden
Icon=negi
Terminal=false
Categories=Utility;Development;
StartupWMClass=negi
EOF
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1
  log "desktop entry: $APPS_DIR/negi.desktop"
}

log "target directory: $DIR"
ensure_uv
update_code
install_environment

PATH_ENTRY_ADDED=false
if path_has_bin_dir; then
  log "$BIN_DIR is already on PATH"
elif [ "$NO_PATH" -eq 0 ]; then
  add_path_entry
else
  log 'leaving PATH alone (--no-path)'
fi
install_entry_point
if [ "$NO_SHORTCUT" -eq 0 ]; then
  install_desktop_entry
else
  log "skipping the desktop entry (--no-shortcut)"
fi
log 'from any terminal now on: negi'

if [ "$NO_START" -eq 1 ]; then
  log "environment ready at $DIR (--no-start: web not launched)"
  exit 0
fi

log 'launching Negi web (first run asks for the project password in the browser)'
cd "$DIR"
exec .venv/bin/python negi_bootstrap.py web
