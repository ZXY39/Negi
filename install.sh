#!/usr/bin/env bash
# G3KU one-line installer (macOS / Linux).
#
#   curl -LsSf https://raw.githubusercontent.com/ZXY39/G3KU-Agent/v1.0.0/install.sh | bash
#
# Provisions uv (and therefore Python) on a machine that has neither, fetches the
# pinned checkout, syncs the locked environment and hands off to g3ku_bootstrap.py.
# Runtime code is not modified by this script.
#
# Environment knobs (passed straight through to uv):
#   UV_DEFAULT_INDEX          e.g. https://pypi.tuna.tsinghua.edu.cn/simple
#   UV_PYTHON_INSTALL_MIRROR  e.g. a reachable python-build-standalone mirror
set -euo pipefail

REPO_OWNER='ZXY39'
REPO_NAME='G3KU-Agent'
REF="${G3KU_REF:-v1.0.0}"
DIR="${G3KU_DIR:-$HOME/G3KU-Agent}"
NO_START=0
REPO_GIT="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"
REPO_ZIP="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/${REF}.zip"
UV_INSTALLER='https://astral.sh/uv/install.sh'

usage() {
  cat <<'EOF'
Usage: install.sh [--dir PATH] [--ref TAG] [--no-start]

  --dir PATH    install location (default ~/G3KU-Agent)
  --ref TAG     git ref to install (default the pinned release tag)
  --no-start    prepare the environment but do not launch the web UI
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

ensure_checkout() {
  if [ -f "$DIR/pyproject.toml" ]; then
    log "checkout already present at $DIR, skipping download"
    return
  fi
  mkdir -p "$DIR"
  if command -v git >/dev/null 2>&1; then
    log "git clone --branch $REF into $DIR"
    git clone --depth 1 --branch "$REF" "$REPO_GIT" "$DIR" || fail 'git clone failed'
    return
  fi
  log "git not available, downloading $REPO_ZIP"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf "$REPO_ZIP" -o "$tmp/source.zip" || fail 'source archive download failed'
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmp/source.zip" "$REPO_ZIP" || fail 'source archive download failed'
  else
    fail 'neither curl nor wget is available to fetch the source archive'
  fi
  command -v unzip >/dev/null 2>&1 || fail 'unzip is required when git is unavailable'
  unzip -q "$tmp/source.zip" -d "$tmp" || fail 'source archive extraction failed'
  inner="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  [ -n "$inner" ] || fail "archive for $REF contained no top-level directory"
  (shopt -s dotglob; mv "$inner"/* "$DIR"/) || fail 'could not move the extracted checkout into place'
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

log "target directory: $DIR"
ensure_uv
ensure_checkout
install_environment

if [ "$NO_START" -eq 1 ]; then
  log "environment ready at $DIR (--no-start: web not launched)"
  exit 0
fi

log 'launching G3KU web (first run asks for the project password in the browser)'
cd "$DIR"
exec .venv/bin/python g3ku_bootstrap.py web
