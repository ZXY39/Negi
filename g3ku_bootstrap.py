from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"
if os.name == "nt":
    VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
else:
    VENV_PYTHON = VENV_DIR / "bin" / "python"
BOOTSTRAP_MARKER = VENV_DIR / ".g3ku_bootstrap_complete"
RUNTIME_LOG_DIR = PROJECT_ROOT / ".g3ku" / "logs"
RUNTIME_CONSOLE_LOG_FILE = RUNTIME_LOG_DIR / "console.log"
MIN_PYTHON = (3, 11)
MIN_CHINA_BRIDGE_NODE = (20, 0, 0)
WINDOWS_NODE_LTS_PACKAGE_ID = "OpenJS.NodeJS.LTS"
RUNTIME_IMPORT_PROBES = (
    "langgraph.checkpoint.sqlite.aio",
    "aiosqlite",
)


def _run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=str(cwd), check=True)


def _is_runnable_python(python_path: Path) -> bool:
    if not python_path.exists():
        return False
    try:
        completed = subprocess.run(
            [str(python_path), "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _reset_venv() -> None:
    if VENV_DIR.exists():
        shutil.rmtree(VENV_DIR)


def _pyproject_fingerprint() -> str:
    pyproject = PROJECT_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return "missing"
    digest = hashlib.sha256(pyproject.read_bytes()).hexdigest()
    return f"pyproject:{digest}"


def _marker_matches_current() -> bool:
    if not BOOTSTRAP_MARKER.exists():
        return False
    try:
        stored = BOOTSTRAP_MARKER.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return stored == _pyproject_fingerprint()


def _venv_has_runtime_deps() -> bool:
    if not _is_runnable_python(VENV_PYTHON):
        return False
    code = (
        "import importlib\n"
        f"mods = {RUNTIME_IMPORT_PROBES!r}\n"
        "for name in mods:\n"
        "    importlib.import_module(name)\n"
    )
    completed = subprocess.run(
        [str(VENV_PYTHON), "-c", code],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def _ensure_host_python_supported() -> None:
    if sys.version_info < MIN_PYTHON:
        required = ".".join(str(part) for part in MIN_PYTHON)
        current = ".".join(str(part) for part in sys.version_info[:3])
        raise SystemExit(
            f"[g3ku] Python {required}+ is required to bootstrap this project. Current interpreter: {current} ({sys.executable})"
        )


def _ensure_venv() -> None:
    _ensure_host_python_supported()
    if _is_runnable_python(VENV_PYTHON):
        return
    if VENV_DIR.exists():
        print(f"[g3ku] Recreating stale virtualenv at {VENV_DIR}")
        _reset_venv()
    _run([sys.executable, "-m", "venv", str(VENV_DIR)], cwd=PROJECT_ROOT)


def _ensure_project_installed() -> None:
    if _marker_matches_current() and _venv_has_runtime_deps():
        return
    _run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], cwd=PROJECT_ROOT)
    _run([str(VENV_PYTHON), "-m", "pip", "install", "-e", "."], cwd=PROJECT_ROOT)
    BOOTSTRAP_MARKER.write_text(_pyproject_fingerprint() + "\n", encoding="utf-8")


def _config_path() -> Path:
    return PROJECT_ROOT / ".g3ku" / "config.json"


def _load_bootstrap_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _china_bridge_enabled_for_bootstrap(config: dict) -> bool:
    bridge = config.get("chinaBridge")
    if not isinstance(bridge, dict):
        return False
    return bool(bridge.get("enabled")) and bool(bridge.get("autoStart", True))


def _china_bridge_node_bin(config: dict) -> str:
    bridge = config.get("chinaBridge") if isinstance(config, dict) else None
    if isinstance(bridge, dict):
        return str(bridge.get("nodeBin") or "node").strip() or "node"
    return "node"


def _china_bridge_package_candidates(config: dict) -> list[str]:
    bridge = config.get("chinaBridge") if isinstance(config, dict) else None
    npm_client = str(bridge.get("npmClient") or "pnpm").strip() if isinstance(bridge, dict) else "pnpm"
    preferred = npm_client or "pnpm"
    candidates: list[str] = [preferred]
    for fallback in ("pnpm", "npm"):
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates


def _china_bridge_host_root() -> Path:
    return PROJECT_ROOT / "subsystems" / "china_channels_host"


def _china_bridge_dist_entry() -> Path:
    return _china_bridge_host_root() / "dist" / "index.js"


def _china_bridge_node_modules_dir() -> Path:
    return _china_bridge_host_root() / "node_modules"


def _china_bridge_install_stamp_path(config: dict) -> Path:
    bridge = config.get("chinaBridge")
    if isinstance(bridge, dict):
        state_dir = str(bridge.get("stateDir") or ".g3ku/china-bridge")
    else:
        state_dir = ".g3ku/china-bridge"
    return PROJECT_ROOT / state_dir / "deps.installed.json"


def _china_bridge_needs_package_manager(config: dict) -> bool:
    if not _china_bridge_node_modules_dir().exists():
        return True
    stamp_path = _china_bridge_install_stamp_path(config)
    if not stamp_path.exists():
        return True
    return not _china_bridge_dist_entry().exists()


def _parse_semver(raw: str) -> tuple[int, ...] | None:
    parts = [int(item) for item in re.findall(r"\d+", str(raw or ""))]
    if not parts:
        return None
    return tuple(parts[:3])


def _version_at_least(current: tuple[int, ...] | None, required: tuple[int, ...]) -> bool:
    if current is None:
        return False
    padded_current = current + (0,) * max(0, len(required) - len(current))
    return padded_current[: len(required)] >= required


def _windows_candidate_paths(name: str) -> list[Path]:
    candidates: list[Path] = []
    names = [name]
    lower_name = name.lower()
    if lower_name.endswith(".exe") or lower_name.endswith(".cmd"):
        pass
    elif name == "node":
        names.append(f"{name}.exe")
    else:
        names.extend([f"{name}.cmd", f"{name}.exe"])

    base_dirs: list[Path] = []
    for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(env_key)
        if value:
            base_dirs.append(Path(value) / "nodejs")
    local_app_data = os.environ.get("LocalAppData")
    if local_app_data:
        base_dirs.append(Path(local_app_data) / "Programs" / "nodejs")
        base_dirs.append(Path(local_app_data) / "Microsoft" / "WindowsApps")

    for base in base_dirs:
        for item in names:
            candidates.append(base / item)
    return candidates


def _resolve_node_executable(config: dict) -> Path | None:
    node_bin = _china_bridge_node_bin(config)
    resolved = shutil.which(node_bin)
    if resolved:
        return Path(resolved)
    candidate_path = Path(node_bin)
    if candidate_path.is_file():
        return candidate_path
    if os.name == "nt":
        for candidate in _windows_candidate_paths(node_bin):
            if candidate.is_file():
                return candidate
    return None


def _resolve_package_manager_executable(config: dict, *, node_path: Path | None = None) -> Path | None:
    for candidate in _china_bridge_package_candidates(config):
        resolved = shutil.which(candidate)
        if resolved:
            return Path(resolved)
    if node_path is not None and node_path.parent.exists():
        for candidate in _china_bridge_package_candidates(config):
            names = [candidate]
            lower_candidate = candidate.lower()
            if not lower_candidate.endswith(".cmd") and not lower_candidate.endswith(".exe"):
                names.extend([f"{candidate}.cmd", f"{candidate}.exe"])
            for item in names:
                adjacent = node_path.parent / item
                if adjacent.is_file():
                    return adjacent
    return None


def _ensure_executable_dir_on_path(executable: Path | None) -> None:
    if executable is None:
        return
    directory = str(Path(executable).resolve().parent)
    current_path = os.environ.get("PATH", "")
    entries = current_path.split(os.pathsep) if current_path else []
    normalized = {entry.lower() for entry in entries if entry}
    if directory.lower() in normalized:
        return
    os.environ["PATH"] = directory if not current_path else directory + os.pathsep + current_path


def _node_version(node_path: Path | None) -> tuple[int, ...] | None:
    if node_path is None:
        return None
    try:
        completed = subprocess.run(
            [str(node_path), "--version"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return _parse_semver(completed.stdout.strip() or completed.stderr.strip())


def _node_satisfies_min_version(node_path: Path | None) -> bool:
    return _version_at_least(_node_version(node_path), MIN_CHINA_BRIDGE_NODE)


def _install_windows_node_lts() -> bool:
    if os.name != "nt":
        return False
    winget = shutil.which("winget")
    if not winget:
        return False
    print("[g3ku] China bridge bootstrap: installing Node.js LTS via winget ...")
    completed = subprocess.run(
        [
            winget,
            "install",
            "--exact",
            "--id",
            WINDOWS_NODE_LTS_PACKAGE_ID,
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--silent",
        ],
        cwd=str(PROJECT_ROOT),
        check=False,
    )
    return completed.returncode == 0


def _ensure_china_bridge_toolchain() -> None:
    config = _load_bootstrap_config()
    if not _china_bridge_enabled_for_bootstrap(config):
        return

    node_path = _resolve_node_executable(config)
    if node_path is not None:
        _ensure_executable_dir_on_path(node_path)

    if not _node_satisfies_min_version(node_path):
        if _install_windows_node_lts():
            node_path = _resolve_node_executable(config)
            if node_path is not None:
                _ensure_executable_dir_on_path(node_path)

    package_manager_path = _resolve_package_manager_executable(config, node_path=node_path)
    if package_manager_path is not None:
        _ensure_executable_dir_on_path(package_manager_path)


def _china_bridge_preflight_messages() -> list[str]:
    config = _load_bootstrap_config()
    if not _china_bridge_enabled_for_bootstrap(config):
        return []

    node_bin = _china_bridge_node_bin(config)
    node_path = _resolve_node_executable(config)
    package_candidates = _china_bridge_package_candidates(config)
    package_manager_path = _resolve_package_manager_executable(config, node_path=node_path)

    messages: list[str] = []
    if not _node_satisfies_min_version(node_path):
        messages.append(
            "[g3ku] China bridge preflight: Node.js is not available in PATH "
            f"(configured nodeBin={node_bin!r}). Install Node.js 20+ before using chinaBridge auto-start."
        )
    if _china_bridge_needs_package_manager(config) and package_manager_path is None:
        messages.append(
            "[g3ku] China bridge preflight: no package manager was found for the China bridge host "
            f"(looked for: {', '.join(package_candidates)}). Install npm or pnpm before first startup build."
        )
    return messages


def _configure_runtime_log_loguru_sink() -> None:
    """Add a rolling loguru file sink to .g3ku/logs/console.log when available.

    Guarded: unavailability or misconfiguration must never break bootstrap. The
    real web runtime capture is handled by stream redirection in main(); this
    sink keeps the owning process loguru records persistent as well.
    """
    try:
        from loguru import logger as _loguru_logger
    except Exception:
        return
    try:
        RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
        _loguru_logger.add(
            str(RUNTIME_CONSOLE_LOG_FILE),
            rotation="50 MB",
            retention="7 days",
            encoding="utf-8",
            backtrace=False,
            diagnose=False,
        )
    except Exception:
        return


def _open_runtime_console_log_stream() -> object | None:
    """Append-mode stream for a server subprocess (stdout/stderr).

    Returns None when the runtime log directory is not writable so callers can
    keep the old console-only behavior.
    """
    try:
        RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
        return RUNTIME_CONSOLE_LOG_FILE.open("a", encoding="utf-8")
    except OSError:
        return None


def _web_bind_port() -> int:
    web_cfg = _load_bootstrap_config().get("web")
    try:
        return int((web_cfg or {}).get("port") or 18790)
    except (TypeError, ValueError):
        return 18790


def _probe_web_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/bootstrap/status", timeout=2) as resp:
            return int(getattr(resp, "status", 200) or 200) < 400
    except Exception:
        return False


def _print_web_ready_banner(port: int) -> None:
    url = f"http://127.0.0.1:{port}/"
    if os.environ.get("WT_SESSION"):
        url = f"\x1b]8;;{url}\x07{url}\x1b]8;;\x07"
    print(f"[g3ku] web UI started successfully: {url} (click to open)", flush=True)


def _print_web_start_failure(returncode: int) -> None:
    print(
        f"[g3ku] web UI failed to start (exit code {returncode}); "
        f"see {RUNTIME_CONSOLE_LOG_FILE}",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    os.chdir(PROJECT_ROOT)
    _ensure_venv()
    _ensure_project_installed()
    _ensure_china_bridge_toolchain()
    for message in _china_bridge_preflight_messages():
        print(message)
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        args = ["web"]
        print("[g3ku] no command given, starting web UI (use `g3ku.cmd <command>` for others)")
    _configure_runtime_log_loguru_sink()
    console_log_stream: object | None = None
    process_env: dict[str, str] | None = None
    is_web_command = bool(args) and str(args[0] or "").strip().lower() == "web"
    if is_web_command:
        console_log_stream = _open_runtime_console_log_stream()
        if console_log_stream is not None:
            print(f"[g3ku] web runtime stdout/stderr appending to {RUNTIME_CONSOLE_LOG_FILE}")
            process_env = dict(os.environ)
            process_env["PYTHONUNBUFFERED"] = "1"
    try:
        if is_web_command:
            child = subprocess.Popen(
                [str(VENV_PYTHON), "-m", "g3ku", *args],
                cwd=str(PROJECT_ROOT),
                stdout=console_log_stream,
                stderr=subprocess.STDOUT if console_log_stream is not None else None,
                env=process_env,
            )
            port = _web_bind_port()
            deadline = time.monotonic() + 180
            warned_slow = False
            ready = False
            while child.poll() is None:
                if _probe_web_ready(port):
                    ready = True
                    break
                if not warned_slow and time.monotonic() >= deadline:
                    warned_slow = True
                    print(
                        f"[g3ku] web UI is not responding on port {port} yet; "
                        f"check {RUNTIME_CONSOLE_LOG_FILE}",
                        flush=True,
                    )
                time.sleep(1)
            if not ready:
                _print_web_start_failure(int(child.returncode or 1))
                return int(child.returncode or 1)
            _print_web_ready_banner(port)
            return int(child.wait() or 0)
        completed = subprocess.run(
            [str(VENV_PYTHON), "-m", "g3ku", *args],
            cwd=str(PROJECT_ROOT),
            stdout=console_log_stream,
            stderr=subprocess.STDOUT if console_log_stream is not None else None,
            env=process_env,
        )
        return int(completed.returncode or 0)
    except KeyboardInterrupt:
        return 130
    finally:
        if console_log_stream is not None:
            try:
                console_log_stream.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
