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
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = PROJECT_ROOT / ".g3ku" / "logs" / "update-apply.log"
# 重拉起的 web 进程的输出落点：与 g3ku_bootstrap 给 web 自身日志选的文件一致，
# 运维找服务日志只去这里，升级锚点只去 LOG_FILE，两边不互相淹没。
WEB_LOG_FILE = PROJECT_ROOT / ".g3ku" / "logs" / "console.log"
EXIT_WAIT_SECONDS = 90.0
EXIT_POLL_SECONDS = 1.0
# 升级子进程的看门狗：每 30 秒没结束就在日志里落一行"仍在跑"，累计超过
# UPGRADE_TIMEOUT_SECONDS 则杀掉，避免慢网络下无限等。
UPGRADE_HEARTBEAT_SECONDS = 30.0
UPGRADE_TIMEOUT_SECONDS = 1200.0
# 让发起 apply 的那次 HTTP 响应先回到浏览器，再动手关停服务。
STARTUP_GRACE_SECONDS = 3.0
# apply 单飞闸门：`started` 记录在这么多秒内视为上一次还在跑，拒掉第二个执行体。
# 上限取"最慢一次升级"的量级（下载 + uv sync 实测到 2 分钟级），留足余量。
APPLY_IN_FLIGHT_SECONDS = 600.0


def _log(message: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def _record(outcome: str, ref: str, detail: str = "") -> None:
    """把这一跳的结局写给 web 侧读，界面才有"没升级成"可说。

    `apply` 端点在踢起执行体那一刻只能回 `restarting: true`；之后执行体无论停在
    哪一步，前端都看不见（实盘两次都是"正在重启"挂在那里，真相在日志里 2 秒就
    结束了）。终态码：started / ok / upgrade_failed / exit_refused / port_busy /
    port_unknown / not_this_service。
    """
    from datetime import datetime

    from g3ku.update_check import write_apply_result

    write_apply_result({
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ref": ref,
        "outcome": outcome,
        "detail": detail,
    })


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


def _probe_self(port: int) -> bool:
    """确认这个端口上确实是本服务的 bootstrap 面，再谈关停。

    端口取错的两种后果都很糟：关掉同机另一个实例，或在别人的端口上空等到超时后
    放弃（用户那边就是"点了没反应"）。所以探不到就中止，不做任何猜测。
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/bootstrap/status", timeout=5.0) as response:
            payload = json.loads(response.read().decode() or "{}")
    except Exception:
        return False
    return isinstance(payload, dict) and payload.get("ok") is True and isinstance(payload.get("item"), dict)


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


def _no_window_flags() -> dict[str, int]:
    """Windows 下别给子进程分配控制台。

    安装脚本是控制台程序：从无人值守的执行体启动它会弹一个什么都不写的黑窗，
    用户只能盯着它，误以为程序死了。
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _run_upgrade(ref: str) -> bool:
    command = _upgrade_command(ref)
    if command is None:
        _log("upgrade skipped: no installer script in project root")
        return False
    _log(f"upgrade start: {' '.join(command)}")
    # 输出直接续写进同一个日志文件：捕获到子进程结束才落盘的话，慢网络下的整段
    # 进度在日志与屏幕上都不存在，用户报上来的就只有"窗口空着不动"。
    try:
        sink = LOG_FILE.open("a", encoding="utf-8", errors="replace")
    except OSError:
        sink = subprocess.DEVNULL
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=sink,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **_no_window_flags(),
        )
        while True:
            try:
                returncode = proc.wait(timeout=UPGRADE_HEARTBEAT_SECONDS)
                break
            except subprocess.TimeoutExpired:
                elapsed = int(time.monotonic() - started)
                if elapsed >= UPGRADE_TIMEOUT_SECONDS:
                    proc.kill()
                    proc.wait(timeout=30)
                    _log(f"upgrade timed out after {elapsed}s; killed")
                    return False
                _log(f"upgrade still running at {elapsed}s (无新输出多半卡在下载/网络)")
    except OSError as exc:
        _log(f"upgrade errored: {exc}")
        return False
    finally:
        if sink is not subprocess.DEVNULL:
            try:
                sink.close()
            except OSError:
                pass
    _log(f"upgrade exit={returncode}")
    return returncode == 0


def _spawn_detached(*args: str, sink_path: Path | None = None) -> None:
    """Start a child that outlives this process (and the server it came from).

    The log handle is closed right after spawn; the child keeps its own dup of
    the OS handle, and an unwritable log dir must not block the relaunch.

    ``sink_path`` defaults to this runner's own log because the runner prints
    nothing but progress. The relaunched web server must NOT use it: its access
    log buries the runner's anchor lines (real case: the whole apply timeline
    had to be filtered out of tens of thousands of INFO lines to be readable).
    """
    target = sink_path or LOG_FILE
    kwargs: dict[str, object] = {"cwd": str(PROJECT_ROOT), "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        sink = target.open("a", encoding="utf-8", errors="replace")
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
    _spawn_detached(*args, sink_path=WEB_LOG_FILE)


def apply_in_flight(window_seconds: float = APPLY_IN_FLIGHT_SECONDS) -> dict[str, Any] | None:
    """上一次 apply 还挂着吗：结果文件是 ``started`` 且落在闸门内。

    闸门不是锦上添花：实盘 13:36 两次点击起了两个执行体，第二个 10 秒后升级完
    重拉起服务，第一个跑了自己的 ``uv sync`` 到 13:39 又重拉起一次并顶掉前者，
    中间服务空窗约 2 分钟；两个进程还各自持有 ``update-apply.log`` 的缓冲写句柄，
    行会互相覆盖（那次 13:36:07 的执行体连自己的 ``apply start`` 都没留下）。
    """
    from datetime import datetime, timedelta

    from g3ku.update_check import read_apply_result

    record = read_apply_result()
    if not isinstance(record, dict) or str(record.get("outcome") or "") != "started":
        return None
    try:
        last = datetime.fromisoformat(str(record.get("at") or "").strip())
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.astimezone()
    if datetime.now().astimezone() - last <= timedelta(seconds=window_seconds):
        return record
    return None


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
    _record("started", ref, f"port={port} pause_running_work={pause_running_work}")


def run(ref: str, *, port: int | None = None, pause_running_work: bool = True) -> int:
    _log(f"apply start ref={ref} pause_running_work={pause_running_work}")
    resolved = _resolve_port(port)
    if resolved is None:
        _log("apply aborted: web port unknown (no --port and no readable .g3ku/config.json)")
        _record("port_unknown", ref, "no --port and no readable config port")
        return 4
    if not _probe_self(resolved):
        _log(f"apply aborted: no Negi bootstrap endpoint answering on port {resolved}")
        _record("not_this_service", ref, f"port {resolved}")
        return 5
    time.sleep(STARTUP_GRACE_SECONDS)
    label = _request_exit(resolved, pause_running_work)
    _log(f"exit request: {label}")
    if label.startswith("exit_refused"):
        # 服务仍在跑（未确认暂停）：不动代码，用户侧保持原状。
        _record("exit_refused", ref, label)
        return 2
    if not _wait_for_release(resolved):
        _log(f"abort: port {resolved} still busy after {EXIT_WAIT_SECONDS}s; leaving the running service alone")
        _record("port_busy", ref, f"{label}; port {resolved} still busy")
        return 3
    upgraded = _run_upgrade(ref)
    if not upgraded:
        _log("upgrade failed; relaunching the previous version")
    _relaunch_web(resolved)
    if upgraded:
        _record("ok", ref, f"relaunched on port {resolved}")
    else:
        _record("upgrade_failed", ref, f"{label}; install -Upgrade 非零退出，旧版本已重新拉起")
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
