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
REF="${G3KU_REF:-v2.7.0}"
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
STASH_LABEL=''

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

exclude_names_from_stream() {
  # packaging/distribution-excludes.txt（标准输入）-> 一行一个顶层名。
  # 清单只存在于负载里这一份，两个安装脚本都不再抄一遍，抄了就会各说各话。
  # 注释与空行去掉；名字里带分隔符的直接报错，不猜他想要哪个子集。
  sed -e 's/\r$//' | awk '
    /^[[:space:]]*#/ { next }
    {
      gsub(/[[:space:]]/, "")
      if (length($0) == 0) next
      if ($0 ~ /[^A-Za-z0-9._-]/ || index($0, "..")) {
        print "not a single top-level name: " $0 > "/dev/stderr"
        exit 3
      }
      print
    }'
}

is_excluded_top_level() {
  local n="$1" e
  for e in ${EXCLUDED_TOP_LEVELS:-}; do
    [ "$e" = "$n" ] && return 0
  done
  return 1
}

stage_code() {
  # $1 = archive 时强制走源码包：无 git 的历史安装升级要保持原取码形态。
  # 代码一律先落到临时目录再逐顶层合并 —— `git clone` 对着一个非空目录（保留数据
  # 后重装的 .g3ku 就算非空）直接 fatal，装不上也谈不上认领。
  local mode="${1:-auto}"
  STAGE_DIR="$(mktemp -d)"
  if [ "$mode" != "archive" ] && command -v git >/dev/null 2>&1; then
    log "git clone --depth 1 --branch $REF (staged)"
    git clone --depth 1 --branch "$REF" --no-checkout "$REPO_GIT" "$STAGE_DIR/checkout" || fail 'git clone failed'
    CHECKOUT_DIR="$STAGE_DIR/checkout"
    # 排除靠 sparse 规则，不是删文件：checkout 之后再删，每个被删的文件都会被判成
    # "跟踪文件已修改"，而 -Upgrade 第一道门就是"跟踪文件没动过"，删一次就把这台
    # 设备永久锁在升级门外。规则写在 .git 里，跟着拷贝走，以后的 checkout 也照规则出树。
    local names='' patterns='/*' name
    if git -C "$CHECKOUT_DIR" cat-file -e 'HEAD:packaging/distribution-excludes.txt' 2>/dev/null; then
      names="$(git -C "$CHECKOUT_DIR" show 'HEAD:packaging/distribution-excludes.txt' | exclude_names_from_stream)" \
        || fail 'packaging/distribution-excludes.txt is malformed'
    fi
    for name in $names; do patterns="$patterns !/$name/"; done
    if [ -n "$names" ]; then
      log "sparse checkout: excluding $(printf '%s' "$names" | tr '\n' ' ')"
      git -C "$CHECKOUT_DIR" sparse-checkout set --no-cone $patterns || fail 'sparse-checkout failed'
    fi
    git -C "$CHECKOUT_DIR" checkout || fail 'checkout failed'
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
  # 源码包车道没有 index 可讲道理，只能在合并这一步跳过同样的清单；git 车道到这里
  # 时那些顶层本来就不在树上，跳过一次是无害的重复。
  EXCLUDED_TOP_LEVELS=''
  if [ -f "$CHECKOUT_DIR/packaging/distribution-excludes.txt" ]; then
    EXCLUDED_TOP_LEVELS="$(exclude_names_from_stream < "$CHECKOUT_DIR/packaging/distribution-excludes.txt")" \
      || fail 'packaging/distribution-excludes.txt is malformed'
  fi
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
    if is_excluded_top_level "$name"; then
      continue
    fi
    [ -e "$entry" ] || continue
    cp -Rf "$entry" "$DIR"/
  done
}

save_tracked_changes() {
  # 本地改动既不能被新版本覆盖，也不能变成"永久升不动"的理由：换代码前把整棵跟踪
  # 工作树存进 stash，换完再原样放回。`git stash` 要写提交对象、因此需要身份，而刚装
  # 好的设备往往没配过 user.name/user.email —— 升级不该卡在 git 配置上，这里内联给一个。
  STASH_LABEL="negi-upgrade-$(date -u +%Y%m%dT%H%M%SZ)"
  log "local edits to tracked files found; stashing them as $STASH_LABEL (restored after the upgrade)"
  git -C "$DIR" -c user.name=negi-upgrade -c user.email=negi-upgrade@localhost \
    stash push --quiet --message "$STASH_LABEL" \
    || fail "$DIR has local edits to tracked files and they could not be stashed; commit or discard them before upgrading"
}

restore_tracked_changes() {
  [ -n "$STASH_LABEL" ] || return 0
  if git -C "$DIR" stash pop --quiet 'stash@{0}'; then
    log 'local edits restored'
  else
    # 冲突说明新版本动过同一批行：丢弃这次合并结果（改动本身仍在 stash 里），代码树
    # 必须保持干净可运行 —— 带冲突标记的文件交给服务就是起不来。
    git -C "$DIR" reset --hard HEAD >/dev/null 2>&1 || true
    log "local edits conflict with $REF; kept in git stash as $STASH_LABEL (resolve with: git -C $DIR stash pop)"
  fi
  STASH_LABEL=''
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
    save_tracked_changes
  fi
  log "git fetch --depth 1 origin $REF"
  if ! git -C "$DIR" fetch --depth 1 origin "$REF"; then
    # 取码失败时树还停在旧提交，先把改动放回去：升级失败不能顺带把本地改动藏进 stash。
    restore_tracked_changes
    fail 'git fetch failed'
  fi
  if ! git -C "$DIR" checkout --detach FETCH_HEAD; then
    restore_tracked_changes
    fail 'git checkout failed'
  fi
  log "code updated to $REF"
  restore_tracked_changes
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
Exec=$BIN_DIR/negi start
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
