from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
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
    """Keep a silent probe child from opening its own console.

    The desktop shortcut runs this file under ``pythonw``, which has no console to
    inherit, so every console child (the interpreter probes, ``uv sync``) would pop
    its own black window on the way to the browser.

    This applies to probes only. The command handoff (``negi status``, ``agent``,
    ``--version``) must NOT carry it: measured here, a child started with
    ``CREATE_NO_WINDOW`` loses the terminal handles it would otherwise inherit and
    prints nothing at all, which turns every typed command into a silent run.
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
        sync_argv = [uv, "sync", "--frozen"]
        # 开发机的 venv 里装着 dev extras（AGENTS.md 要求改码前后跑 ruff/pytest），
        # 而不带 --extra dev 的 sync 会把它们当非锁定包裁掉——下一次 `negi` 起完，
        # 同一棵树里就 `No module named pytest` 了。装了才跟着保留。
        if _python_has_module(VENV_PYTHON, "pytest"):
            sync_argv += ["--extra", "dev"]
        _run(sync_argv, cwd=PROJECT_ROOT)
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

    取**最后一次**出现：`start-negi.cmd` 为了不做 batch 参数拆解，先把默认端口写在前面，
    操作员给的 `-Port` 追在后面（typer 也是后者赢）。取第一次会让回收与就绪探测盯着默认端口，
    而服务实际绑在自定义端口上——看着像"起不来"。
    """
    resolved: int | None = None
    for index, part in enumerate(args):
        value: str | None = None
        if part in ("--port", "-p") and index + 1 < len(args):
            value = args[index + 1]
        elif str(part).startswith("--port="):
            value = str(part).split("=", 1)[1]
        if value is None:
            continue
        try:
            resolved = int(str(value).strip())
        except ValueError:
            return None
    return resolved


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


# 与 `start-negi.sh` 的匹配式同一条 token 形状，也与 AGENTS.md 冻结项「`python -m g3ku web|worker`
# 是 reaper 认的 cmdline」对齐：`_spawn_web_runtime` 故意按这个形状起，任何别的进程形状都会留下
# 占着端口与启动锁的孤儿。第三种形状是"直接跑 negi_bootstrap.py web"（旧快捷方式）。
RUNTIME_CMDLINE_PATTERN = r"-m\s+g3ku\s+web\b|-m\s+g3ku\s+worker\b|negi_bootstrap\.py\"?\s+web\b"
_GRACEFUL_EXIT_TIMEOUT_SECONDS = 40.0
_GRACEFUL_EXIT_POLL_SECONDS = 0.5


def _runtime_cmdline_is_managed(command_line: object, *, root: object = None) -> bool:
    """是不是"这个 checkout 起的 Negi 运行时"。

    两道判据缺一不可：cmdline 形状认进程在干什么，根路径把它限定在本安装里——一台机器上并存两个
    checkout 时，只按形状就会把别人家的实例收掉。Windows 路径大小写不敏感，按小写比。
    """
    text = str(command_line or "")
    if root is not None and str(root).lower() not in text.lower():
        return False
    return bool(re.search(RUNTIME_CMDLINE_PATTERN, text))


def _wait_port_quiet(port: int, *, timeout: float = 10.0, poll: float = 0.5) -> bool:
    """等这台端口的 HTTP 不再应答。

    回收轮询看的是进程，端口刚释放时 listen 套接字可能还在 TIME_WAIT 之外短暂应答；不等一下
    就把"旧实例还活着"报给操作员是假警报。判据用我们本来就有的 `/api/bootstrap/status`，
    不再为端口另开一次进程查询。
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if not _probe_web_ready(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.05, float(poll)))


def _run_capture(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=20,
            **_no_window_flags(),
        )
    except Exception as exc:  # noqa: BLE001 - a missing probe tool must not block a start
        return subprocess.CompletedProcess(argv, 1, "", f"{type(exc).__name__}:{exc}")


def _managed_runtime_processes() -> list[int]:
    """本 checkout 起着的 web/worker 进程 PID（与启动器旧实现同一判据，但不含自己）。

    入口用绝对路径调这个文件时，**启动器自己的命令行**同时命中「checkout 根」与
    `negi_bootstrap.py web` 两条判据。把自己算进回收名单，下面的 `taskkill /T` 就会
    在回收途中杀掉正在回收的进程本身：入口以 exit 1 静默消失、一个字都不打印，看起来
    像"脚本起不来"（实测：同一个文件用相对路径调用则不复现——相对 argv 不含 checkout 根）。

    要排除的是**一对** PID：venv 的 `python.exe` 是重定向器，真正执行本文件的解释器是
    它的子进程（实测启动 PID ≠ 运行 PID）。`os.getpid()` 只是那个子进程，cmdline 命中
    判据的却是父进程，所以父 PID 必须一起排除，否则强杀那一步仍然打到自己。
    """
    self_pids = {os.getpid(), os.getppid()}
    if os.name == "nt":
        # PowerShell 单引号里反斜杠是字面量，`.NET` 正则要的 `\s` 原样传即可（多倍成一个
        # 反斜杠就变成"匹配一个反斜杠再加 s"，扫不到任何进程——看着像"没有旧实例"）。
        # 根路径用 [regex]::Escape 现算：它含点号与反斜杠，直接塞进正则会被当元字符。
        escaped_root = str(PROJECT_ROOT).replace("'", "''")
        script = (
            "$root = [regex]::Escape('" + escaped_root + "')\n"
            "Get-CimInstance Win32_Process | Where-Object "
            "{ $_.Name -like 'python*' -and $_.CommandLine -and $_.CommandLine -match $root -and "
            "$_.CommandLine -match '" + RUNTIME_CMDLINE_PATTERN + "' } "
            "| ForEach-Object { $_.ProcessId }"
        )
        completed = _run_capture(["powershell.exe", "-NoProfile", "-Command", script])
        pids = [int(token) for token in completed.stdout.split() if token.strip().isdigit()]
        return [pid for pid in pids if pid not in self_pids]
    completed = _run_capture(["ps", "-eo", "pid=,args="])
    pids: list[int] = []
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        if _runtime_cmdline_is_managed(parts[1], root=PROJECT_ROOT):
            pids.append(int(parts[0]))
    return [pid for pid in pids if pid not in self_pids]


def _request_runtime_exit(port: int, *, timeout: float = 20.0) -> bool:
    """`POST /api/bootstrap/exit`：让运行时自己停车并退出（与启动器旧实现同一入口）。"""
    body = json.dumps({"pause_running_work": True}).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/bootstrap/exit",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200) < 400
    except Exception:
        return False


def _terminate_runtime_process(pid: int) -> bool:
    if os.name == "nt":
        # `/T` 是必需的：venv 的 `python.exe` 是重定向器，真解释器是它的**子进程**
        # （实测启动 PID != 运行 PID）。只 `/F` 杀父会留下孤儿服务占着端口，然后
        # "端口还被占"变成永久起不来。
        completed = _run_capture(["taskkill", "/PID", str(int(pid)), "/T", "/F"])
        return completed.returncode == 0
    try:
        os.kill(int(pid), signal.SIGKILL)
    except OSError:
        return False
    return True


def _recorded_runtime_port() -> int:
    """上一个实例自己记下的端口：web 启动时 launcher 会把含端口的回调地址写进
    `.g3ku/internal-callback.json`（给 worker 回传终态用）。读不到就是 0。"""
    try:
        payload = json.loads((PROJECT_ROOT / ".g3ku" / "internal-callback.json").read_text(encoding="utf-8"))
        host_port = urllib.parse.urlparse(str((payload or {}).get("url") or "")).port
    except Exception:
        return 0
    return int(host_port or 0)


def _runtime_exit_ports(port: int) -> list[int]:
    """优雅退出要打到的候选端口：这次要绑的端口，再加旧实例记下的端口、配置端口与默认端口。

    回收按 checkout 收、不按端口收；操作者这次换了 `-Port` 时旧实例仍听在旧端口上，
    只打目标端口等于没发请求，回收就会退化成强杀——那是异常中断（下次启动走恢复清洗、
    任务卡提示"异常停止"），正是优雅退出这一步要避免的形状。
    """
    candidates = [int(port)]
    try:
        configured = int((_load_bootstrap_config().get("web") or {}).get("port") or 0)
    except (TypeError, ValueError):
        configured = 0
    for candidate in (_recorded_runtime_port(), configured, DEFAULT_WEB_PORT):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        completed = _run_capture(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"])
        return str(int(pid)) in str(completed.stdout or "")
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def reap_existing_runtime_processes(
    port: int,
    *,
    graceful_timeout: float = _GRACEFUL_EXIT_TIMEOUT_SECONDS,
    poll: float = _GRACEFUL_EXIT_POLL_SECONDS,
    print_lines: list[str] | None = None,
) -> dict[str, object]:
    """启动前回收本 checkout 的旧实例：先优雅退出并轮询，残留才动手（`start-negi.sh` 同语义）。

    回收不能活在 `start-negi.ps1` 里：入口被机器策略拦下时（未签名的 .ps1 被拒）谁都起不来，
    而 `negi web` 这条道也不会经过它。放在解释器侧，`.cmd` / `.ps1` / `negi web` 共用一次回收。
    """
    lines = print_lines if print_lines is not None else []
    found = _managed_runtime_processes()
    if not found:
        return {"found": [], "graceful": False, "forced": [], "remaining": []}
    lines.append("[negi] Restarting existing Negi web/worker processes...")
    graceful = False
    for exit_port in _runtime_exit_ports(port):
        if _request_runtime_exit(exit_port):
            graceful = True
            break
    if graceful:
        lines.append("[negi] Graceful exit requested; waiting for the runtime to pause all work and stop...")
        deadline = time.monotonic() + max(0.0, float(graceful_timeout))
        while time.monotonic() < deadline:
            if not _managed_runtime_processes():
                return {"found": found, "graceful": True, "forced": [], "remaining": []}
            time.sleep(max(0.05, float(poll)))
    remaining = _managed_runtime_processes()
    if remaining:
        lines.append("[negi] Force-stopping remaining Negi processes...")
    forced: list[int] = []
    failed: list[int] = []
    for pid in remaining:
        if pid not in found:
            # 快照之后新出现的 PID 不动手；只收拾"我让它退出而它没退"的那批。
            continue
        if _terminate_runtime_process(pid) or not _process_exists(pid):
            # `taskkill /T` 收父进程时已经把子进程带走了；排在后面的子 PID 再杀一次会
            # 报"找不到进程"，但那不是失败——只有**杀完仍活着**才算失败。
            forced.append(pid)
        else:
            failed.append(pid)
    if failed:
        lines.append(f"[negi] Failed to stop PID(s): {', '.join(str(pid) for pid in failed)}")
    time.sleep(1.0)
    return {"found": found, "graceful": graceful, "forced": forced, "remaining": _managed_runtime_processes()}



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
            # CREATE_NO_WINDOW, not DETACHED_PROCESS: the venv's python.exe is a
            # redirector that spawns the real interpreter as its child (measured:
            # launched pid != running pid). A detached parent has no console at all,
            # so Windows gives that child a fresh visible one - and that process is
            # the server, so closing the "extra" window kills Negi. An invisible
            # console is inherited by the child instead. The new process group keeps
            # Ctrl+C in this terminal from reaching the server.
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
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


def _open_desktop_window(port: int) -> bool:
    """Hand the window to the desktop shell instead of a browser tab.

    The shell is a GUI process, so it goes out through ``pythonw`` detached: a
    console interpreter would pop a black window behind the very window we are
    trying to be. Closing the shell leaves the server running either way.
    """
    pythonw = VENV_PYTHON.with_name("pythonw.exe" if os.name == "nt" else "python")
    if not pythonw.exists():
        print(f"[negi] {pythonw} is missing; opening the browser instead", flush=True)
        return _open_browser(_web_url(port))
    popen_kwargs: dict[str, object] = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    try:
        subprocess.Popen([str(pythonw), "-m", "g3ku", "window", "--port", str(port)], **popen_kwargs)
    except Exception as exc:
        print(f"[negi] could not open the desktop window: {exc}; using the browser", flush=True)
        return _open_browser(_web_url(port))
    print(f"[negi] desktop window starting against {_web_url(port)}", flush=True)
    return True


def _launch_web(web_args: list[str], *, port: int, open_browser: bool, hidden: bool, open_window: bool = False) -> int:
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
        if open_window:
            _open_desktop_window(port)
        elif open_browser:
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


def _take_window_flag(args: list[str]) -> tuple[list[str], bool]:
    if "--window" not in args:
        return args, False
    return [part for part in args if part != "--window"], True


def _take_bool_flag(args: list[str], name: str) -> tuple[list[str], bool]:
    """`--flag/--no-flag` 两种写法都收，返回剩余参数与是否置真。"""
    positive = name
    negative = f"--no-{name.lstrip('-')}"
    found = False
    kept = []
    for part in args:
        if part == positive:
            found = True
            continue
        if part == negative:
            found = False
            continue
        kept.append(part)
    return kept, found


# `start-negi.ps1` / `start-negi.cmd` 的开关是 PowerShell 形状（`-PromptLog`，大小写不敏感）。
# 翻译表放在解释器侧只写一次：入口脚本因此可以是薄壳，`.cmd` 也不必用 batch 循环去拆参数——
# Windows 上 batch 的字符串处理正是最容易静默错一个开关的地方。
WEB_ENTRY_SWITCH_ALIASES: dict[str, str] = {
    "-bindhost": "--host",
    "-host": "--host",
    "-port": "--port",
    "-openbrowser": "--open-browser",
    "-promptlog": "--prompt-log",
    "-keepworker": "--keep-worker",
    "-reload": "--reload",
}
WEB_ENTRY_HELP_SWITCHES = frozenset({"-h", "-help", "--help"})

WEB_ENTRY_USAGE = """Usage: start-negi [-BindHost HOST] [-Port PORT] [-OpenBrowser] [-PromptLog] [-Reload] [-KeepWorker]

  -BindHost      Web bind host. Default: 127.0.0.1
  -Port          Web bind port. Default: 18790
  -OpenBrowser   Open the browser once the runtime answers
  -PromptLog     Enable G3KU_PROMPT_TRACE=1
  -Reload        Web auto-reload (managed worker auto-start is disabled)
  -KeepWorker    Leave the managed worker running when the web server exits
"""


def _normalize_web_entry_switches(web_args: list[str]) -> tuple[list[str], bool]:
    """把入口脚本的开关统一成 CLI 形状；返回净化后的参数与"只要帮助"。"""
    kept: list[str] = []
    wants_help = False
    for part in web_args:
        lowered = str(part).lower()
        if lowered in WEB_ENTRY_HELP_SWITCHES:
            wants_help = True
            continue
        alias = WEB_ENTRY_SWITCH_ALIASES.get(lowered)
        kept.append(alias if alias else str(part))
    return kept, wants_help


def _apply_web_entry_flags(web_args: list[str]) -> tuple[list[str], bool, bool]:
    """归一入口开关并落到子进程环境。

    返回 `(CLI 参数, 是否开浏览器, 是否只要帮助)`。`G3KU_PROMPT_TRACE` 与
    `G3KU_WEB_KEEP_WORKER` 的读取点各自只有一处（provider/engine、worker_control），在这里设
    一次就够，不需要每个入口脚本各写一份；没给开关时把两个变量清掉，免得上一支 shell 里残留的
    值被无声继承下来。

    `--host/--port` 不在这里摘：它们要原样交给 `python -m g3ku web`（typer 是唯一判据源），
    只有端口需要在这里读一次，好让回收与就绪探测看着同一个端口。
    """
    web_args, wants_help = _normalize_web_entry_switches(list(web_args))
    web_args, want_browser = _take_bool_flag(web_args, "--open-browser")
    web_args, want_prompt_log = _take_bool_flag(web_args, "--prompt-log")
    web_args, want_keep_worker = _take_bool_flag(web_args, "--keep-worker")
    for name in ("G3KU_PROMPT_TRACE", "G3KU_WEB_KEEP_WORKER"):
        os.environ.pop(name, None)
    if want_prompt_log:
        os.environ["G3KU_PROMPT_TRACE"] = "1"
    if want_keep_worker:
        os.environ["G3KU_WEB_KEEP_WORKER"] = "1"
    return web_args, want_browser, wants_help


def _print_reap_lines(lines: list[str]) -> None:
    for line in lines:
        print(line, flush=True)


def _run_start(extra_args: list[str]) -> int:
    # `--hidden` is still consumed so a shortcut written by an earlier install
    # keeps working, but start always detaches: a server the operator launched by
    # typing `negi` must outlive the window it was started from. Watching the
    # process itself is what `negi web` is for. `--window` swaps the browser tab
    # for the desktop shell; the server lane is identical either way.
    web_args, _ = _take_hidden_flag(list(extra_args))
    web_args, want_window = _take_window_flag(web_args)
    port = _web_bind_port(_port_override(web_args))
    if _probe_web_ready(port):
        url = _web_url(port)
        print(f"[negi] Negi is already running on port {port}: {url}", flush=True)
        if want_window:
            _open_desktop_window(port)
        else:
            _open_browser(url)
        return 0
    print(f"[negi] Negi is not running yet; starting the web UI on port {port}", flush=True)
    return _launch_web(
        ["web", *web_args],
        port=port,
        open_browser=not want_window,
        hidden=True,
        open_window=want_window,
    )


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
            # 旧实例的回收在解释器侧做（原只在 `start-negi.ps1` 里）：入口脚本可能因机器策略根本
            # 跑不起来（未签名的 .ps1 被拒），`negi.cmd` / `negi web` 这条道仍要能起，且三条道
            # 必须同一语义。
            web_args, want_browser, wants_help = _apply_web_entry_flags(args[1:])
            if wants_help:
                print(WEB_ENTRY_USAGE, flush=True)
                return 0
            port = _web_bind_port(_port_override(web_args))
            reap_lines: list[str] = []
            report = reap_existing_runtime_processes(port, print_lines=reap_lines)
            _print_reap_lines(reap_lines)
            leftovers = [int(pid) for pid in list(report.get("remaining") or [])]
            if leftovers:
                print(
                    "[negi] existing Negi process(es) refused to stop: "
                    f"{', '.join(str(pid) for pid in leftovers)}; not starting a second instance",
                    flush=True,
                )
                return 1
            if not _wait_port_quiet(port):
                print(
                    f"[negi] port {port} still answers after the reaper finished; another server holds it",
                    flush=True,
                )
                return 1
            return _launch_web(
                ["web", *web_args],
                port=port,
                open_browser=want_browser,
                hidden=False,
            )
        completed = subprocess.run(
            [str(VENV_PYTHON), "-m", "g3ku", *args],
            cwd=str(PROJECT_ROOT),
        )
        return int(completed.returncode or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
