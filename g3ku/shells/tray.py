"""Windows 状态栏托盘图标（``negi tray``）。

独立进程，由 web 运行时启动体派生：pystray 承载图标与右键菜单，退出走现成的
``POST /api/bootstrap/exit``（二次确认用 ctypes 原生 MessageBoxW）。一个看门狗线程
探活，服务停了托盘自己退。fail-closed：pystray/Pillow 缺失或非 Windows 时无托盘、
不影响服务。

为什么不用 PowerShell NotifyIcon：受管机器 ConstrainedLanguage 拒 .NET/COM；
pystray 的 Windows 后端走 ctypes 调 user32/shell32，Python ctypes 不受该策略限制。
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
import webbrowser
from pathlib import Path

from loguru import logger

try:
    import pystray
    from PIL import Image

    _TRAY_AVAILABLE = True
except Exception:  # pragma: no cover - 取决于可选依赖是否在场
    pystray = None
    Image = None
    _TRAY_AVAILABLE = False

ICON_PATH = Path(__file__).resolve().parents[1] / "web" / "frontend" / "favicon.ico"
STATUS_POLL_SECONDS = 3.0
# Win32 MessageBox 常量（OS 合同名，模块级大写）。
MB_OKCANCEL = 0x01
MB_ICONWARNING = 0x30
IDOK = 1
# 忽略环境代理：本机代理变量可能畸形（架构 docs「环境代理变量与 HTTP 客户端构造」），
# 托盘只探本机回环，不该走任何代理。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _web_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/"


def _get_json(url: str, timeout: float = 5.0) -> dict | None:
    try:
        with _OPENER.open(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except Exception:
        return None


def _service_alive(port: int) -> bool:
    payload = _get_json(_web_url(port) + "api/bootstrap/status", timeout=3.0)
    return isinstance(payload, dict) and payload.get("ok") is True


def _check_running_work(port: int) -> dict:
    payload = _get_json(_web_url(port) + "api/bootstrap/exit-check")
    if isinstance(payload, dict) and isinstance(payload.get("item"), dict):
        return payload["item"]
    return {"has_running_work": False, "running_sessions": [], "running_tasks": []}


def _request_exit(port: int) -> bool:
    body = json.dumps({"pause_running_work": True}).encode("utf-8")
    request = urllib.request.Request(
        _web_url(port) + "api/bootstrap/exit",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=60.0) as response:
            return 200 <= int(getattr(response, "status", 200)) < 300
    except Exception as exc:
        logger.warning("tray exit request failed: {}", exc)
        return False


def _exit_confirm_text(snapshot: dict) -> str:
    if snapshot.get("has_running_work"):
        sessions = len(snapshot.get("running_sessions") or [])
        tasks = len(snapshot.get("running_tasks") or [])
        return f"将暂停 {sessions} 个对话和 {tasks} 个任务，然后停止 Negi 服务。确定退出吗？"
    return "确定退出 Negi 吗？服务将停止。"


def _confirm_exit(snapshot: dict) -> bool:
    import ctypes

    result = ctypes.windll.user32.MessageBoxW(
        0, _exit_confirm_text(snapshot), "退出 Negi", MB_OKCANCEL | MB_ICONWARNING
    )
    return result == IDOK


def _open_ui(port: int) -> None:
    webbrowser.open(_web_url(port))


def _exit_action(port: int) -> None:
    snapshot = _check_running_work(port)
    if not _confirm_exit(snapshot):
        return
    _request_exit(port)


def _watch_service(port: int, icon, stop_event: threading.Event, miss_limit: int = 3) -> None:
    misses = 0
    while not stop_event.is_set():
        if _service_alive(port):
            misses = 0
        else:
            misses += 1
            if misses >= miss_limit:
                try:
                    icon.stop()
                except Exception:
                    pass
                return
        stop_event.wait(STATUS_POLL_SECONDS)


def run_tray(port: int, host: str | None = None) -> int:
    if os.name != "nt":
        print("[negi] tray icon is Windows-only; nothing to do", flush=True)
        return 0
    if not _TRAY_AVAILABLE:
        print("[negi] tray unavailable (pystray/Pillow not installed); service runs without a tray icon", flush=True)
        return 0
    try:
        image = Image.open(ICON_PATH).convert("RGBA")
    except Exception as exc:
        logger.warning("tray icon image load failed: {}", exc)
        return 0

    stop_event = threading.Event()
    menu = pystray.Menu(
        pystray.MenuItem("打开 Negi", lambda icon, item: _open_ui(port), default=True),
        pystray.MenuItem("退出 Negi", lambda icon, item: _exit_action(port)),
    )
    icon = pystray.Icon("negi", image, "Negi", menu)
    watcher = threading.Thread(
        target=_watch_service, args=(port, icon, stop_event), daemon=True
    )
    watcher.start()
    logger.info("tray icon started for port {}", port)
    try:
        icon.run()
    finally:
        stop_event.set()
    return 0
