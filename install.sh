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
#
# When those are unset, a failed direct download is retried once through the
# mirrors below. --mirror starts on the mirrors, --no-mirror disables the retry.
set -euo pipefail

REPO_OWNER='ZXY39'
REPO_NAME='Negi'
REF="${G3KU_REF:-v2.9.0}"
REF_SET=0
DIR="${G3KU_DIR:-$HOME/Negi}"
NO_START=0
UPGRADE=0
NO_PATH=0
NO_SHORTCUT=0
REPO_GIT="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"
REPO_ZIP="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/${REF}.zip"
UV_INSTALLER='https://astral.sh/uv/install.sh'
PYPI_MIRROR='https://pypi.tuna.tsinghua.edu.cn/simple'
PYTHON_MIRROR='https://mirror.sjtu.edu.cn/github-release/astral-sh/python-build-standalone/releases/download'
MIRROR=0
NO_MIRROR=0
KIT=''
KIT_STAGE=''
KIT_ROOT=''
KIT_SRC=''
KIT_WHEELS=''
KIT_PYTHON=''
KIT_UV=''
KIT_REQUIREMENTS=''
KIT_VERSION=''
# Kept across upgrades: the environment and everything the operator created.
PROTECTED_ENTRIES='.venv .g3ku .git'
STAGE_DIR=''
CHECKOUT_DIR=''
STASH_LABEL=''

cleanup() {
  if [ -n "$STAGE_DIR" ]; then
    rm -rf "$STAGE_DIR"
  fi
  # A kit that failed halfway leaves an unpacked tree (with a CPython inside) in /tmp;
  # the trap is the only thing that runs on every exit path, including fail().
  if [ -n "${KIT_STAGE:-}" ]; then
    rm -rf "$KIT_STAGE" 2>/dev/null || true
  fi
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
Usage: install.sh [--dir PATH] [--ref TAG] [--no-start] [--upgrade] [--no-path] [--no-shortcut] [--mirror|--no-mirror]

  --dir PATH       install location (default ~/Negi)
  --ref TAG        git ref to install (default the pinned release tag)
  --no-start       prepare the environment but do not launch the web UI
  --upgrade        update the code in an existing install, keep .venv and .g3ku
  --no-path        write the command stub but leave PATH alone
  --no-shortcut    skip the desktop entry
  --mirror         start Python and package downloads on the built-in mirrors
  --no-mirror      only use the direct sources; no automatic mirror retry
  --kit PATH       install fully offline from a release kit (zip or unpacked directory)
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
      REF_SET=1
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
    --mirror)
      MIRROR=1
      shift
      ;;
    --no-mirror)
      NO_MIRROR=1
      shift
      ;;
    --kit)
      KIT="${2:?--kit needs a kit path (zip or unpacked directory)}"
      shift 2
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

if [ "$MIRROR" -eq 1 ] && [ "$NO_MIRROR" -eq 1 ]; then
  fail '--mirror and --no-mirror cannot be combined'
fi
if [ "$MIRROR" -eq 1 ]; then
  export UV_DEFAULT_INDEX="$PYPI_MIRROR"
  export UV_PYTHON_INSTALL_MIRROR="$PYTHON_MIRROR"
  log "mirror mode: index=$PYPI_MIRROR python=$PYTHON_MIRROR"
fi

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

device_kit_platform() {
  # The kit asset is named for the machine it installs on; installing the wrong one
  # produces a tree that cannot start, so this is a refusal and not a warning.
  local os arch
  os="$(uname -s)"
  arch="$(uname -m)"
  case "$os/$arch" in
    MINGW*/*|MSYS*/*|CYGWIN*/*|Windows_NT/*)
      case "$arch" in
        aarch64|arm64) printf 'win-arm64\n' ;;
        *) printf 'win-x64\n' ;;
      esac
      ;;
    Darwin/arm64) printf 'macos-arm64\n' ;;
    Darwin/x86_64) printf 'macos-x64\n' ;;
    Linux/x86_64|Linux/amd64) printf 'linux-x64\n' ;;
    Linux/aarch64|Linux/arm64) printf 'linux-arm64\n' ;;
    *) printf 'unknown\n' ;;
  esac
}

sha256_of() {
  # Three names for one job across the platforms a kit can land on.
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$1" | awk '{print $NF}'
  else
    return 1
  fi
}

kit_field() {
  # kit.json is written by the release build; the four scalars the installer needs are
  # read with sed rather than pulling in a JSON parser a minimal device may not have.
  # The sed program stays single-quoted with the key spliced in: escaped quotes inside
  # a double-quoted command substitution do not survive parsing (bash -n rejects it).
  local file="$1" key="$2" value
  value=$(sed -n 's/.*"'${key}'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$file" | head -n 1)
  [ -n "$value" ] || { echo "[install] kit.json has no '$key'" >&2; return 1; }
  printf '%s' "$value"
}

kit_wheel_pairs() {
  # kit.json records every wheel it ships with its digest, one property per line. Pairing
  # a filename with the sha256 that follows it is enough, so a device needs no JSON parser.
  # An object that ends before its digest is malformed, not something to skip quietly:
  # skipping would install an unchecked file.
  awk '
    /"filename"[[:space:]]*:[[:space:]]*"/ {
      if (pending != "") {
        print "kit.json records " pending " without a digest" > "/dev/stderr"
        exit 4
      }
      name = $0
      sub(/.*"filename"[[:space:]]*:[[:space:]]*"/, "", name)
      sub(/".*/, "", name)
      pending = name
      next
    }
    /"sha256"[[:space:]]*:[[:space:]]*"/ {
      if (pending == "") next
      digest = $0
      sub(/.*"sha256"[[:space:]]*:[[:space:]]*"/, "", digest)
      sub(/".*/, "", digest)
      print digest " " pending
      pending = ""
      next
    }
    END {
      if (pending != "") {
        print "kit.json records " pending " without a digest" > "/dev/stderr"
        exit 4
      }
    }'
}

resolve_kit() {
  # Unpack if needed, then check what is inside against the device and against itself
  # before a single file is copied: a corrupt or mismatched kit must fail early, and
  # nothing on this lane reaches the network - that is the whole point of the kit.
  local manifest platform device declared pair digest wheel_file actual pairs
  if [ -d "$KIT" ] && [ -f "$KIT/kit.json" ]; then
    KIT_ROOT="$KIT"
  elif [ -e "$KIT" ]; then
    KIT_STAGE="$(mktemp -d)"
    # Unpack inside the staging directory, never next to the file the operator handed
    # over: a read-only media path is a normal place to keep a kit.
    mkdir -p "$KIT_STAGE/unpacked"
    if command -v unzip >/dev/null 2>&1; then
      unzip -q "$KIT" -d "$KIT_STAGE/unpacked" || fail "kit archive could not be unpacked: $KIT"
    else
      # bsdtar reads a zip too, and the kit is written by bsdtar on every platform.
      tar -xf "$KIT" -C "$KIT_STAGE/unpacked" || fail "kit archive could not be unpacked: $KIT"
    fi
    # The archive holds kit.json at the top; a plain unzip can add one wrapper directory.
    if [ -f "$KIT_STAGE/unpacked/kit.json" ]; then
      KIT_ROOT="$KIT_STAGE/unpacked"
    else
      manifest="$(find "$KIT_STAGE/unpacked" -mindepth 2 -maxdepth 2 -name kit.json | head -n 1)"
      [ -n "${manifest:-}" ] || fail "no kit.json inside $KIT"
      KIT_ROOT="${manifest%/*}"
    fi
  else
    fail "kit not found: $KIT"
  fi
  manifest="$KIT_ROOT/kit.json"
  platform="$(kit_field "$manifest" platform)" || fail 'unreadable kit'
  device="$(device_kit_platform)"
  [ "$platform" = "$device" ] \
    || fail "this kit is for $platform; this device is $device"
  KIT_VERSION="$(kit_field "$manifest" version)"
  KIT_SRC="$KIT_ROOT/source"
  KIT_WHEELS="$KIT_ROOT/wheels"
  [ -d "$KIT_SRC" ] || fail 'the kit carries no source tree'
  [ -f "$KIT_SRC/pyproject.toml" ] || fail 'the kit source has no pyproject.toml'
  [ -d "$KIT_WHEELS" ] || fail 'the kit carries no wheels directory'
  declared="$(sed -n 's/.*__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$KIT_SRC/g3ku/__init__.py" | head -n 1)"
  [ "$declared" = "$KIT_VERSION" ] \
    || fail "kit.json says $KIT_VERSION but its source declares ${declared:-nothing}"
  if [ "$REF_SET" = "1" ] && [ "$REF" != "v$KIT_VERSION" ]; then
    fail "--ref $REF conflicts with the kit's version v$KIT_VERSION"
  fi
  local count=0
  # The pairs go through a variable, not a process substitution: `fail` inside <(...) only
  # ends the subshell, so a kit with a bad manifest would sail on to the copy step.
  pairs="$(kit_wheel_pairs < "$manifest")" || fail 'kit.json holds a wheel without a digest'
  while IFS= read -r pair; do
    [ -n "$pair" ] || continue
    digest="${pair%% *}"
    wheel_file="${pair#* }"
    [ -f "$KIT_WHEELS/$wheel_file" ] || fail "kit is missing $wheel_file"
    actual="$(sha256_of "$KIT_WHEELS/$wheel_file")" || fail 'no sha256 tool available to verify the kit'
    [ "$actual" = "$digest" ] \
      || fail "kit wheel $wheel_file does not match its recorded sha256"
    count=$((count + 1))
  done <<EOF
$pairs
EOF
  [ "$count" -gt 0 ] || fail 'the kit lists no wheels'
  KIT_PYTHON="$(kit_field "$manifest" python_executable)" || fail 'unreadable kit'
  KIT_UV="$(kit_field "$manifest" uv_binary)" || fail 'unreadable kit'
  KIT_REQUIREMENTS="$(kit_field "$manifest" requirements)" || fail 'unreadable kit'
  KIT_PYTHON="$KIT_ROOT/$KIT_PYTHON"
  KIT_UV="$KIT_ROOT/$KIT_UV"
  KIT_REQUIREMENTS="$KIT_ROOT/$KIT_REQUIREMENTS"
  [ -f "$KIT_PYTHON" ] || fail "kit interpreter missing: $KIT_PYTHON"
  [ -f "$KIT_UV" ] || fail "kit uv missing: $KIT_UV"
  [ -s "$KIT_REQUIREMENTS" ] || fail "kit requirement list missing: $KIT_REQUIREMENTS"
  log "kit v$KIT_VERSION verified: $count wheels, platform $platform"
}

clear_kit_stage() {
  [ -n "${KIT_STAGE:-}" ] || return 0
  # A staging directory holds a copied CPython tree, and on some platforms rm -rf is
  # not enough to let go of it; failing to clear it is noise, not a failed install.
  rm -rf "$KIT_STAGE" 2>/dev/null || true
  KIT_STAGE=''
}

install_kit_uv() {
  # uv is the only thing that can read uv.lock, so the kit carries its own copy. It lands
  # in the same ~/.local/bin uv's installer uses, so a later `negi` finds it offline too.
  local target
  target="$BIN_DIR/$(basename "$KIT_UV")"
  mkdir -p "$BIN_DIR" || return 1
  cp -f "$KIT_UV" "$target" || return 1
  chmod +x "$target" || return 1
  "$target" --version >/dev/null 2>&1 || { rm -f "$target" 2>/dev/null || true; return 1; }
  # Reachable for the rest of this run as well, not only after the operator's next login:
  # BIN_DIR is usually not on PATH yet on a machine that has never had uv.
  PATH="$BIN_DIR:$PATH"
  export PATH
  log "uv from the kit: $target"
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
    # 模式必须走数组：`/*` 未加引号时会被 bash 当 glob 按当前目录展开（在 / 下就是
    # /bin /etc /usr…），那样 sparse 规则全是绝对路径，检出来的树只剩 .git。
    local names='' name
    local -a patterns=('/*')
    if git -C "$CHECKOUT_DIR" cat-file -e 'HEAD:packaging/distribution-excludes.txt' 2>/dev/null; then
      names="$(git -C "$CHECKOUT_DIR" show 'HEAD:packaging/distribution-excludes.txt' | exclude_names_from_stream)" \
        || fail 'packaging/distribution-excludes.txt is malformed'
    fi
    for name in $names; do patterns+=("!/$name/"); done
    if [ -n "$names" ]; then
      log "sparse checkout: excluding $(printf '%s' "$names" | tr '\n' ' ')"
      git -C "$CHECKOUT_DIR" sparse-checkout set --no-cone "${patterns[@]}" || fail 'sparse-checkout failed'
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

update_code_from_kit() {
  # 离线 kit 车道：代码来自 kit 里的快照，取码这一步不碰网络。
  if [ -f "$DIR/pyproject.toml" ]; then
    if [ "$UPGRADE" -eq 0 ]; then
      log "checkout already present at $DIR, code untouched (pass --upgrade to update it)"
      return
    fi
    if [ -d "$DIR/.git" ]; then
      # 留着 .git 却换上 kit 的代码，index 立刻和树不一致——而升级的门禁正是"跟踪文件
      # 没动过"，于是这台设备永久升不动。和 Windows 侧同一句话，宁可拒也不放行。
      fail "$DIR is a git checkout; a kit upgrade would leave it inconsistent with its own .git. Remove $DIR/.git first if the kit should own this tree."
    fi
    log "upgrading code from the kit v$KIT_VERSION (keeping .venv and .g3ku)"
    CHECKOUT_DIR="$KIT_SRC"
    copy_checkout_into_root 1
    return
  fi
  mkdir -p "$DIR"
  if [ -f "$DIR/.g3ku/config.json" ]; then
    log "adopting the data root already present at $DIR (config, sessions and keys kept)"
  fi
  log "installing code from the offline kit v$KIT_VERSION"
  CHECKOUT_DIR="$KIT_SRC"
  copy_checkout_into_root 0
}

update_code() {
  if [ -n "$KIT_ROOT" ]; then
    update_code_from_kit
    return
  fi
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

install_python() {
  local pin="$1"
  log "uv python install $pin"
  if "$UV" python install "$pin"; then
    return 0
  fi
  # A mirror already chosen by the operator (or by --mirror above) is not
  # second-guessed; the retry is only for "direct failed, nothing chosen yet".
  if [ "$NO_MIRROR" -eq 1 ] || [ -n "${UV_PYTHON_INSTALL_MIRROR:-}" ]; then
    fail "uv python install $pin failed; set UV_PYTHON_INSTALL_MIRROR to a reachable mirror and rerun"
  fi
  log "python download failed; retrying via $PYTHON_MIRROR"
  export UV_PYTHON_INSTALL_MIRROR="$PYTHON_MIRROR"
  "$UV" python install "$pin" || fail "uv python install $pin failed direct and via $PYTHON_MIRROR"
}

sync_environment() {
  log 'uv sync --frozen'
  if (cd "$DIR" && "$UV" sync --frozen); then
    return 0
  fi
  if [ "$NO_MIRROR" -eq 1 ] || [ -n "${UV_DEFAULT_INDEX:-}" ]; then
    fail 'uv sync failed; set UV_DEFAULT_INDEX to a reachable index and rerun'
  fi
  log "dependency download failed; retrying via $PYPI_MIRROR"
  export UV_DEFAULT_INDEX="$PYPI_MIRROR"
  (cd "$DIR" && "$UV" sync --frozen) || fail "uv sync failed direct and via $PYPI_MIRROR"
}

install_offline_environment() {
  # The venv records its base interpreter, so the kit runtime has to live somewhere
  # permanent before the venv is built - not in the staging directory this script deletes
  # when it finishes.
  local runtime_home="$DIR/.runtime"
  local runtime_target="$runtime_home/negi-python"
  local python src_home venv_python
  src_home="$(dirname "$KIT_PYTHON")"
  # Windows ships <dir>/python.exe, the standalone builds <dir>/bin/python3.
  [ "$(basename "$src_home")" = "bin" ] && src_home="$(dirname "$src_home")"
  [ -d "$src_home" ] || fail "kit runtime directory is missing: $src_home"
  rm -rf "$runtime_target" 2>/dev/null || true
  mkdir -p "$runtime_home" || fail "could not create $runtime_home"
  cp -Rf "$src_home" "$runtime_target" || fail "could not copy the kit runtime into $runtime_target"
  if [ -x "$runtime_target/bin/python3" ]; then
    python="$runtime_target/bin/python3"
  elif [ -f "$runtime_target/python.exe" ]; then
    python="$runtime_target/python.exe"
  else
    fail "the copied kit runtime has no interpreter under $runtime_target"
  fi
  UV="$(find_uv || true)"
  [ -n "${UV:-}" ] || fail 'neither the kit nor the device provides uv, so the environment cannot be built offline'
  if [ ! -d "$DIR/.venv" ]; then
    log 'creating the virtual environment from the kit runtime'
    (cd "$DIR" && "$UV" venv --relocatable --python "$python" .venv) \
      || fail 'uv venv failed while creating the environment from the kit runtime'
  fi
  venv_python="$DIR/.venv/bin/python"
  [ -x "$venv_python" ] || venv_python="$DIR/.venv/Scripts/python.exe"
  [ -x "$venv_python" ] || fail 'the offline environment has no interpreter to install into'
  # `uv pip install` and not `uv sync`: sync goes looking for the URLs recorded in
  # uv.lock, and with --offline it reads them from uv's cache - so a machine that has
  # never downloaded anything fails there even when every wheel sits in the kit. pip
  # install treats --find-links as the source of the packages themselves, which is what
  # an offline device actually has.
  local install_args=("$UV" pip install --python "$venv_python" --offline --no-index
    --find-links "$KIT_WHEELS")
  log 'uv pip install --offline --no-index (kit wheels, no index)'
  (cd "$DIR" && "${install_args[@]}" -r "$KIT_REQUIREMENTS") \
    || fail 'offline install failed: the kit does not carry every wheel the lock pins'
  # The project itself last: building its editable wheel needs the [build-system] wheels,
  # which is why the kit carries hatchling and friends too.
  (cd "$DIR" && "${install_args[@]}" -e "$DIR") \
    || fail 'offline install failed: the kit cannot build the project itself'
  # Without this the first `negi` run calls _ensure_project_installed(), which runs
  # `uv sync --frozen` and reaches for an index this device does not have.
  (cd "$DIR" && "$venv_python" negi_bootstrap.py mark-installed) \
    || fail 'recording the installed environment failed; the next negi run would reach for the network'
}

install_environment() {
  if [ -n "$KIT_ROOT" ]; then
    install_offline_environment
    return
  fi
  if [ -f "$DIR/.python-version" ]; then
    pin="$(head -n 1 "$DIR/.python-version" | tr -d '[:space:]')"
    if [ -n "$pin" ]; then
      install_python "$pin"
    fi
  fi
  sync_environment
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
if [ -n "$KIT" ]; then
  resolve_kit
  # uv comes from the kit when the device has none: astral.sh's installer is a network
  # call, and a device that needs a kit usually cannot make it.
  if ! find_uv >/dev/null 2>&1; then
    install_kit_uv || fail 'the kit carries no runnable uv; install uv and rerun'
  fi
fi
ensure_uv
update_code
install_environment
clear_kit_stage

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
