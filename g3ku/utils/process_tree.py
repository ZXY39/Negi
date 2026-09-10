"""跨平台进程树终止助手。

杀单个 PID 只会结束直接子进程；Windows 上 asyncio/Popen 的 ``kill()`` 底层是
TerminateProcess，孙进程树会存活。这里提供统一的"整树终止"入口：

- Windows：``taskkill /T /F /PID``（系统自带，零新依赖）。
- POSIX：仅当目标是自己的进程组组长（即以 ``start_new_session=True`` 启动，
  pgid == pid）时用 ``killpg`` 杀整组，否则回退为直接 ``kill``——绝不
  对非组长进程调用 killpg，避免误杀宿主进程组。

所有路径都是 best-effort：返回是否尝试了终止动作，不抛异常。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def kill_process_tree_pid(pid: int | None) -> bool:
    """按 PID 尽力终止整个进程树；成功发起终止动作返回 True。"""
    try:
        normalized_pid = int(pid)
    except (TypeError, ValueError):
        return False
    if normalized_pid <= 0:
        return False

    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(normalized_pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return True
        except Exception:
            return False

    try:
        pgid = os.getpgid(normalized_pid)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    if pgid == normalized_pid:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return True
        except Exception:
            pass
    try:
        os.kill(normalized_pid, signal.SIGKILL)
        return True
    except Exception:
        return False


def kill_process_tree(process: object | None) -> bool:
    """对 Popen / asyncio.subprocess.Process 对象做整树终止（best-effort）。"""
    if process is None:
        return False
    pid = getattr(process, "pid", None)
    tree_killed = kill_process_tree_pid(pid)
    try:
        if getattr(process, "returncode", None) is None:
            process.kill()
    except Exception:
        pass
    return tree_killed
