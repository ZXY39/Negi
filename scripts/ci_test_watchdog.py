"""CI 用的挂死看门狗：把「一片跑到预算整点被砍、日志里连用例名都没有」变成「几分钟内点名」。

为什么要它：分片只解决"总时长超预算"，解决不了"某一条用例不返回"。Windows runner
上 stdout 不是 TTY，pytest 的点号按块缓冲，被取消的那片日志只能看到最后一次刷新的
位置（实测 shard3 停在 64% 之后 16 分钟无输出，凶手用例名一个都没落进日志）。
本插件不改变任何用例的判定，只做两件事：超阈值时点名，超上限时留下线程栈后退出。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback

_REPORT_SECONDS = float(os.environ.get("NEGI_CI_HANG_REPORT_SECONDS", "90"))
_KILL_SECONDS = float(os.environ.get("NEGI_CI_HANG_KILL_SECONDS", "300"))
_TICK_SECONDS = 2.0

_state_lock = threading.Lock()
_current: dict[str, float] = {}

# pytest 在执行用例时接管 sys.stdout 并把 fd 1 指向临时文件；被 os._exit 砍掉的进程
# 永远不会把那份缓冲倒进作业日志（本机实测：看门狗打了字，日志里一个字都没有）。
# 所以在导入时复制一份"真实的 fd 1"，之后的点名全部直接写这个 fd。
try:
    _LOG_FD = os.dup(1)
except OSError:
    _LOG_FD = 2


def _announce(text: str) -> None:
    try:
        os.write(_LOG_FD, f"[hang-watchdog] {text}\n".encode("utf-8", "replace"))
    except OSError:
        pass


def _watch() -> None:
    while True:
        time.sleep(_TICK_SECONDS)
        with _state_lock:
            nodeid = str(_current.get("nodeid") or "")
            started = float(_current.get("started") or 0.0)
            reported = bool(_current.get("reported"))
        if not nodeid or not started:
            # 两条用例之间的空档：继续等，不能退出线程，否则整片失去保护。
            continue
        elapsed = time.monotonic() - started
        if elapsed < _KILL_SECONDS:
            if elapsed >= _REPORT_SECONDS and not reported:
                _announce(f"{nodeid} still running after {elapsed:.0f}s")
                with _state_lock:
                    _current["reported"] = True
            continue
        _announce(f"KILLING run: {nodeid} has not returned for {elapsed:.0f}s")
        frames = sys._current_frames()
        myself = threading.current_thread()
        for thread in threading.enumerate():
            frame = frames.get(thread.ident)
            if frame is None or thread is myself:
                continue
            stack = "".join(traceback.format_stack(frame))
            _announce(f"thread {thread.name}: {stack}")
        os._exit(124)


def pytest_sessionstart(session):
    threading.Thread(target=_watch, name="hang-watchdog", daemon=True).start()


def pytest_runtest_logstart(nodeid, location):
    with _state_lock:
        _current["nodeid"] = nodeid
        _current["started"] = time.monotonic()


def pytest_runtest_logfinish(nodeid, location):
    with _state_lock:
        _current.clear()
