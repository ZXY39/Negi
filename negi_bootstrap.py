from __future__ import annotations

import hashlib
import json
import os
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
RUNTIME_CONSOLE_LOG_MAX_BYTES = 50 * 1024 * 1024
RUNTIME_CONSOLE_LOG_RETENTION_SECONDS = 7 * 24 * 3600
MIN_PYTHON = (3, 11)
DEFAULT_WEB_PORT = 18790
READY_WAIT_SECONDS = 180
RUNTIME_IMPORT_PROBES = (
    "fastapi",
    "aiosqlite",
    "pydantic",
)


def _no_window_flags() -> dict[str, int]:
    """Never let a child allocate its own console.

    The desktop shortcut runs this file under ``pythonw``, which has no console to
    inherit, so every console child (the interpreter probes, ``uv sync``) would pop
    its own black window on the way to the browser. Under a real terminal the child
    still inherits it, so typed commands keep printing where the operator is looking.
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=str(cwd), check=True, **_no_window_flags())


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
            **_no_window_flags(),
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
        **_no_window_flags(),
    )
    return completed.returncode == 0


def _ensure_host_python_supported() -> None:
    if sys.version_info < MIN_PYTHON:
        required = ".".join(str(part) for part in MIN_PYTHON)
        current = ".".join(str(part) for part in sys.version_info[:3])
        raise SystemExit(
            f"[negi] Python {required}+ is required to bootstrap this project. Current interpreter: {current} ({sys.executable})"
        )


def _ensure_venv() -> None:
    _ensure_host_python_supported()
    if _is_runnable_python(VENV_PYTHON):
        return
    if VENV_DIR.exists():
        print(f"[negi] Recreating stale virtualenv at {VENV_DIR}")
        _reset_venv()
    _run([sys.executable, "-m", "venv", str(VENV_DIR)], cwd=PROJECT_ROOT)


def _python_has_module(python: Path, module: str) -> bool:
    completed = subprocess.run(
        [str(python), "-c", f"import {module!s}"],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        **_no_window_flags(),
    )
    return completed.returncode == 0


def _uv_executable() -> str | None:
    """定位 uv，与安装脚本同判据：PATH 里没有就回落两个已知安装位。

    astral 安装器把 uv 写进 `~/.local/bin` 并追加用户 PATH，而 PATH 变更对已经开着的终端
    不可见；首启正落在这个窗口里，只认 `which` 会让新装设备停在 "No usable package installer"。
    """
    resolved = shutil.which("uv")
    if resolved:
        return str(resolved)
    exe = "uv.exe" if os.name == "nt" else "uv"
    for relative in (Path(".local") / "bin" / exe, Path(".cargo") / "bin" / exe):
        candidate = Path.home() / relative
        if candidate.exists():
            return str(candidate)
    return None


def _ensure_project_installed() -> None:
    if _marker_matches_current() and _venv_has_runtime_deps():
        return
    # uv 托管的 checkout 走锁文件同步：uv 创建的 venv 不带 pip，且任何
    # uv sync 都会把非锁定包（包括 pip）从 venv 裁剪掉，pip 路径在此类
    # 环境必然失败——必须优先探测 uv。
    uv = _uv_executable()
    if uv is not None and (PROJECT_ROOT / "uv.lock").exists():
        _run([uv, "sync", "--frozen"], cwd=PROJECT_ROOT)
    elif _python_has_module(VENV_PYTHON, "pip"):
        _run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], cwd=PROJECT_ROOT)
        _run([str(VENV_PYTHON), "-m", "pip", "install", "-e", "."], cwd=PROJECT_ROOT)
    else:
        raise SystemExit(
            f"[negi] No usable package installer: uv is not on PATH and {VENV_DIR} has no pip. "
            "Install uv (https://docs.astral.sh/uv/) or recreate the virtualenv with pip, then retry."
        )
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


def _rotate_runtime_console_log() -> None:
    """console.log 超上限时改名成一枚时间戳代，并清掉 7 天以上的旧代。

    必须赶在 ``_open_runtime_console_log_stream()`` 之前跑：正文是 web 子进程继承的
    stdout 句柄写进去的，句柄一旦建立就换不掉（Windows 上被占用的文件 rename 直接失败），
    所以改名轮转只有在下一次启动、句柄还不存在时才做得动。长跑期间的封顶另有
    runtime_service 的 ``console_log_cap`` 维护位负责。best-effort：任何失败都不挡启动。
    """
    try:
        if (
            RUNTIME_CONSOLE_LOG_FILE.is_file()
            and RUNTIME_CONSOLE_LOG_FILE.stat().st_size > RUNTIME_CONSOLE_LOG_MAX_BYTES
        ):
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            RUNTIME_CONSOLE_LOG_FILE.rename(
                RUNTIME_CONSOLE_LOG_FILE.with_name(f"{RUNTIME_CONSOLE_LOG_FILE.name}.{stamp}")
            )
    except OSError:
        return
    try:
        cutoff = time.time() - RUNTIME_CONSOLE_LOG_RETENTION_SECONDS
        for generation in RUNTIME_LOG_DIR.glob(f"{RUNTIME_CONSOLE_LOG_FILE.name}.*"):
            if generation.is_file() and generation.stat().st_mtime < cutoff:
                generation.unlink(missing_ok=True)
    except OSError:
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


def _port_override(args: list[str]) -> int | None:
    """``--port 9000`` / ``-p 9000`` / ``--port=9000`` out of a forwarded argv.

    The readiness probe has to watch the port the child will actually bind: the
    config value alone makes every non-default port look like a startup failure
    while the server is running fine.
    """
    for index, part in enumerate(args):
        value: str | None = None
        if part in ("--port", "-p") and index + 1 < len(args):
            value = args[index + 1]
        elif str(part).startswith("--port="):
            value = str(part).split("=", 1)[1]
        if value is None:
            continue
        try:
            return int(str(value).strip())
        except ValueError:
            return None
    return None


def _web_bind_port(port_override: int | None = None) -> int:
    if port_override is not None:
        return port_override
    web_cfg = _load_bootstrap_config().get("web")
    try:
        return int((web_cfg or {}).get("port") or DEFAULT_WEB_PORT)
    except (TypeError, ValueError):
        return DEFAULT_WEB_PORT


def _probe_web_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/bootstrap/status", timeout=2) as resp:
            return int(getattr(resp, "status", 200) or 200) < 400
    except Exception:
        return False


def _web_url(port: int) -> str:
    # The bind host in config is routinely 0.0.0.0, which is not a clickable
    # address; the loopback form is what the operator is meant to open.
    return f"http://127.0.0.1:{port}/"


def _open_browser(url: str) -> bool:
    try:
        import webbrowser
    except Exception as exc:
        print(f"[negi] no browser to open: {exc}; the URL is {url}", flush=True)
        return False
    try:
        return bool(webbrowser.open(url))
    except Exception as exc:
        print(f"[negi] could not open the browser: {exc}; the URL is {url}", flush=True)
        return False


def _print_web_ready_banner(port: int) -> None:
    url = _web_url(port)
    if os.environ.get("WT_SESSION"):
        url = f"\x1b]8;;{url}\x07{url}\x1b]8;;\x07"
    print(f"[negi] web UI started successfully: {url} (click to open)", flush=True)


def _print_web_start_failure(returncode: int) -> None:
    print(
        f"[negi] web UI failed to start (exit code {returncode}); "
        f"see {RUNTIME_CONSOLE_LOG_FILE}",
        flush=True,
    )


def _take_hidden_flag(args: list[str]) -> tuple[list[str], bool]:
    if "--hidden" not in args:
        return args, False
    return [part for part in args if part != "--hidden"], True


def _spawn_web_runtime(args: list[str], *, hidden: bool, console_log_stream: object | None) -> object:
    """Start ``python -m g3ku web`` in the shape the reaper recognizes.

    The cmdline stays ``-m g3ku web`` on purpose: ``g3ku/web/launcher.py`` and
    ``start-negi.*`` find stale servers by exactly that token shape, so any
    other process form would leave an orphan holding the port and start lock.
    """
    popen_kwargs: dict[str, object] = {"cwd": str(PROJECT_ROOT)}
    env: dict[str, str] | None = None
    if console_log_stream is not None:
        popen_kwargs["stdout"] = console_log_stream
        popen_kwargs["stderr"] = subprocess.STDOUT
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
    elif hidden:
        popen_kwargs["stdout"] = subprocess.DEVNULL
        popen_kwargs["stderr"] = subprocess.DEVNULL
    if hidden:
        popen_kwargs["stdin"] = subprocess.DEVNULL
        popen_kwargs["env"] = env if env is not None else dict(os.environ)
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
    return subprocess.Popen([str(VENV_PYTHON), "-m", "g3ku", *args], **popen_kwargs)


def _wait_for_web_ready(child: object, port: int) -> bool:
    deadline = time.monotonic() + READY_WAIT_SECONDS
    warned_slow = False
    while child.poll() is None:  # type: ignore[union-attr]
        if _probe_web_ready(port):
            return True
        if not warned_slow and time.monotonic() >= deadline:
            warned_slow = True
            print(
                f"[negi] web UI is not responding on port {port} yet; "
                f"check {RUNTIME_CONSOLE_LOG_FILE}",
                flush=True,
            )
        time.sleep(1)
    return False


def _launch_web(web_args: list[str], *, port: int, open_browser: bool, hidden: bool) -> int:
    console_log_stream = _open_runtime_console_log_stream()
    if console_log_stream is not None:
        print(f"[negi] web runtime stdout/stderr appending to {RUNTIME_CONSOLE_LOG_FILE}", flush=True)
    elif hidden:
        print(f"[negi] {RUNTIME_CONSOLE_LOG_FILE} is not writable; a hidden server keeps no log")
    child = _spawn_web_runtime(web_args, hidden=hidden, console_log_stream=console_log_stream)
    try:
        if not _wait_for_web_ready(child, port):
            returncode = int(getattr(child, "returncode", None) or 1)
            _print_web_start_failure(returncode)
            return returncode
        _print_web_ready_banner(port)
        if open_browser:
            _open_browser(_web_url(port))
        if hidden:
            # Nobody owns this console: the server keeps running after this
            # process exits, and stopping it is the web UI's exit button.
            print("[negi] Negi runs in the background now; closing this window does not stop it", flush=True)
            return 0
        return int(child.wait() or 0)  # type: ignore[union-attr]
    except KeyboardInterrupt:
        raise
    finally:
        if console_log_stream is not None:
            try:
                console_log_stream.close()  # type: ignore[union-attr]
            except Exception:
                pass


def _run_start(extra_args: list[str]) -> int:
    # `--hidden` is still consumed so a shortcut written by an earlier install
    # keeps working, but start always detaches: a server the operator launched by
    # typing `negi` must outlive the window it was started from. Watching the
    # process itself is what `negi web` is for.
    web_args, _ = _take_hidden_flag(list(extra_args))
    port = _web_bind_port(_port_override(web_args))
    if _probe_web_ready(port):
        url = _web_url(port)
        print(f"[negi] Negi is already running on port {port}: {url}", flush=True)
        _open_browser(url)
        return 0
    print(f"[negi] Negi is not running yet; starting the web UI on port {port}", flush=True)
    return _launch_web(["web", *web_args], port=port, open_browser=True, hidden=True)


def main(argv: list[str] | None = None) -> int:
    os.chdir(PROJECT_ROOT)
    _ensure_venv()
    _ensure_project_installed()
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        args = ["start"]
        print("[negi] no command given, starting the web UI (use `negi.cmd <command>` for others)")
    _rotate_runtime_console_log()
    command = str(args[0] or "").strip().lower()
    try:
        if command == "start":
            return _run_start(args[1:])
        if command == "web":
            web_args = args[1:]
            return _launch_web(
                ["web", *web_args],
                port=_web_bind_port(_port_override(web_args)),
                open_browser=False,
                hidden=False,
            )
        completed = subprocess.run(
            [str(VENV_PYTHON), "-m", "g3ku", *args],
            cwd=str(PROJECT_ROOT),
            **_no_window_flags(),
        )
        return int(completed.returncode or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
