"""「重启并更新」的脱离执行体。

由 web 进程以脱离子进程方式启动后独立运行：请求优雅退出 → 等端口与启动锁释放
→ 用仓库里的安装脚本升级代码 → 重新拉起 Web。任何一步失败都必须把旧版本重新
拉起，绝不把设备留在"没有服务"的状态。

顺序不能反过来：运行中的解释器会在源码被替换的窗口里 import 到半截文件（项目里
出过 `submit_next_stage` 18 连败 NameError、阶段死锁的事故），所以升级只在服务
确认已经退出之后才开始。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = PROJECT_ROOT / ".g3ku" / "logs" / "update-apply.log"
EXIT_WAIT_SECONDS = 90.0
EXIT_POLL_SECONDS = 1.0
# 让发起 apply 的那次 HTTP 响应先回到浏览器，再动手关停服务。
STARTUP_GRACE_SECONDS = 3.0


def _log(message: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def _read_config_port() -> int | None:
    """Read the bind port straight from the project config.

    Deliberately not using ``load_config``: the runner may run while the project
    is locked, and the port is the only field it needs. No fallback value — a
    wrong guess here would shut down a *different* G3KU instance on the same
    machine, so an unknown port aborts the run instead.
    """
    try:
        payload = json.loads((PROJECT_ROOT / ".g3ku" / "config.json").read_text(encoding="utf-8"))
        port = int((payload.get("web") or {}).get("port") or 0)
    except Exception:
        return None
    return port if 0 < port < 65536 else None


def _resolve_port(explicit: int | None) -> int | None:
    if explicit is not None:
        return explicit if 0 < explicit < 65536 else None
    return _read_config_port()


def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _request_exit(port: int, pause_running_work: bool) -> str:
    """Ask the live server to shut down. Returns a status label for the log."""
    body = json.dumps({"pause_running_work": bool(pause_running_work)}).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/bootstrap/exit",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60.0) as response:
            return f"exit_accepted_{getattr(response, 'status', 200)}"
    except urllib.error.HTTPError as exc:
        # 409 = 有在跑的活且没被确认暂停；这里绝不继续升级。
        return f"exit_refused_{exc.code}"
    except (urllib.error.URLError, OSError):
        return "exit_unreachable"


def _wait_for_release(port: int) -> bool:
    deadline = time.monotonic() + EXIT_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not _port_busy(port):
            # 端口空了再稳一拍，让锁句柄与 worker 收割真正落定。
            time.sleep(2.0)
            return True
        time.sleep(EXIT_POLL_SECONDS)
    return False


def _upgrade_command(ref: str) -> list[str] | None:
    """Upgrade *this* project root.

    `-Dir` must be passed explicitly: the installer's own default is
    `%USERPROFILE%/G3KU-Agent`, so omitting it would download and upgrade a
    different directory while this one restarts unchanged.
    """
    if os.name == "nt":
        script = PROJECT_ROOT / "install.ps1"
        if not script.exists():
            return None
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Dir",
            str(PROJECT_ROOT),
            "-Upgrade",
            "-NoStart",
            "-Ref",
            ref,
        ]
    script = PROJECT_ROOT / "install.sh"
    if not script.exists():
        return None
    return ["bash", str(script), "--dir", str(PROJECT_ROOT), "--upgrade", "--no-start", "--ref", ref]


def _log_tail(text: str, limit: int = 2000) -> str:
    cleaned = str(text or "").strip().replace("\r", "")
    return cleaned[-limit:]


def _run_upgrade(ref: str) -> bool:
    command = _upgrade_command(ref)
    if command is None:
        _log("upgrade skipped: no installer script in project root")
        return False
    _log(f"upgrade start: {' '.join(command)}")
    try:
        completed = subprocess.run(command, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=1800.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log(f"upgrade errored: {exc}")
        return False
    ok = completed.returncode == 0
    _log(f"upgrade exit={completed.returncode}")
    for label, stream in (("stdout", completed.stdout), ("stderr", completed.stderr)):
        if str(stream or "").strip():
            _log(f"upgrade {label}: {_log_tail(stream)}")
    return ok


def _spawn_detached(*args: str) -> None:
    """Start a child that outlives this process (and the server it came from).

    The log handle is closed right after spawn; the child keeps its own dup of
    the OS handle, and an unwritable log dir must not block the relaunch.
    """
    kwargs: dict[str, object] = {"cwd": str(PROJECT_ROOT), "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        sink = LOG_FILE.open("a", encoding="utf-8")
    except OSError:
        sink = subprocess.DEVNULL
    kwargs["stdout"] = sink
    kwargs["stderr"] = subprocess.STDOUT
    try:
        subprocess.Popen([sys.executable, *args], **kwargs)
    finally:
        if sink is not subprocess.DEVNULL:
            try:
                sink.close()
            except OSError:
                pass


def _relaunch_web(port: int | None = None) -> None:
    """Bring the service back on the port it was serving, so a device that moved
    off the default port does not come back somewhere the browser is not looking.
    """
    args = ["-m", "g3ku", "web"]
    if port:
        args += ["--port", str(int(port))]
    _log(f"relaunch web {' '.join(args)}")
    _spawn_detached(*args)


def spawn_runner(ref: str, *, port: int | None = None, pause_running_work: bool = True) -> None:
    """Launch this module as a detached process (called from the web process).

    The caller's live port is handed over explicitly: the runner must never
    guess it, because a wrong guess stops an unrelated instance.
    """
    args = ["-m", __name__, "--ref", ref]
    if port:
        args += ["--port", str(int(port))]
    if not pause_running_work:
        args.append("--no-pause")
    _spawn_detached(*args)


def run(ref: str, *, port: int | None = None, pause_running_work: bool = True) -> int:
    _log(f"apply start ref={ref} pause_running_work={pause_running_work}")
    resolved = _resolve_port(port)
    if resolved is None:
        _log("apply aborted: web port unknown (no --port and no readable .g3ku/config.json)")
        return 4
    time.sleep(STARTUP_GRACE_SECONDS)
    label = _request_exit(resolved, pause_running_work)
    _log(f"exit request: {label}")
    if label.startswith("exit_refused"):
        # 服务仍在跑（未确认暂停）：不动代码，用户侧保持原状。
        return 2
    if not _wait_for_release(resolved):
        _log(f"abort: port {resolved} still busy after {EXIT_WAIT_SECONDS}s; leaving the running service alone")
        return 3
    upgraded = _run_upgrade(ref)
    if not upgraded:
        _log("upgrade failed; relaunching the previous version")
    _relaunch_web(resolved)
    return 0 if upgraded else 1


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    ref = ""
    port: int | None = None
    pause = True
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--ref" and index + 1 < len(args):
            ref = args[index + 1].strip()
            index += 2
            continue
        if token == "--port" and index + 1 < len(args):
            try:
                port = int(args[index + 1])
            except ValueError:
                port = None
            index += 2
            continue
        if token == "--no-pause":
            pause = False
            index += 1
            continue
        index += 1
    if not ref:
        _log("apply aborted: missing --ref")
        return 64
    return run(ref, port=port, pause_running_work=pause)


if __name__ == "__main__":
    raise SystemExit(main())
