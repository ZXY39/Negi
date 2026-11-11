"""跨平台进程树终止助手。

杀单个 PID 只会结束直接子进程；Windows 上 asyncio/Popen 的 ``kill()`` 底层是
TerminateProcess，孙进程树会存活。这里提供统一的"整树终止"入口：

- Windows：``taskkill /T /F /PID``（系统自带，零新依赖）。
- POSIX：仅当目标是自己的进程组组长（即以 ``start_new_session=True`` 启动，
  pgid == pid）时用 ``killpg`` 杀整组，否则回退为直接 ``kill``——绝不
  对非组长进程调用 killpg，避免误杀宿主进程组。

``kill_process_tree_pid_checked`` 返回逐项裁决结果：终止动作是否发起、
目标进程（以及终止前捕获的后代）是否确认消失、失败原因与诊断文本。
"命令执行了"（taskkill 返回过）与"进程已消失"（身份校验过）分别报告，
回收方必须以后者为准。``kill_process_tree_pid`` 保留旧 bool 合同，
内部走同一条已校验路径。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field


@dataclass
class ProcessTreeKillResult:
    """一次进程树终止的逐项裁决。"""

    initiated: bool = False
    terminated: bool = False
    reason: str = ""
    detail: str = ""
    descendant_total: int = 0
    descendant_leftovers: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.terminated


def _psutil():
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    return psutil


def _pid_gone(psutil_mod, pid: int, *, create_time: float | None = None) -> bool:
    """确认 PID 已消失；能给 create_time 时同时校验身份，防 PID 复用。"""
    if psutil_mod is not None:
        try:
            proc = psutil_mod.Process(pid)
        except psutil_mod.NoSuchProcess:
            return True
        except Exception:
            proc = None
        if proc is None:
            return False
        if create_time is not None and create_time > 0:
            try:
                current_create_time = float(proc.create_time())
            except psutil_mod.NoSuchProcess:
                return True
            except Exception:
                return False
            if abs(current_create_time - create_time) < 1.0:
                return False
            return True
        try:
            proc.status()
        except psutil_mod.NoSuchProcess:
            return True
        except Exception:
            return False
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return False
    return False


def _capture_descendants(psutil_mod, pid: int) -> list[tuple[int, float]]:
    """终止前捕获后代（pid, create_time），用于终止后的存活核对。"""
    if psutil_mod is None:
        return []
    captured: list[tuple[int, float]] = []
    try:
        parent = psutil_mod.Process(pid)
        for child in parent.children(recursive=True):
            try:
                captured.append((int(child.pid), float(child.create_time())))
            except Exception:
                continue
    except Exception:
        return captured
    return captured


def _verify_gone(
    psutil_mod,
    pid: int,
    *,
    create_time: float | None,
    descendants: list[tuple[int, float]],
) -> tuple[bool, str, list[int]]:
    """核对目标与已捕获后代是否都消失；返回 (ok, reason, leftover_pids)。"""
    if not _pid_gone(psutil_mod, pid, create_time=create_time):
        return False, "process_still_alive", []
    leftovers: list[int] = []
    for child_pid, child_create_time in descendants:
        if child_pid == pid:
            continue
        if not _pid_gone(psutil_mod, child_pid, create_time=child_create_time):
            leftovers.append(child_pid)
    if leftovers:
        return False, "descendants_still_alive", leftovers
    return True, "terminated", []


def _wait_until_gone(
    psutil_mod,
    pid: int,
    *,
    create_time: float | None,
    descendants: list[tuple[int, float]],
    timeout_seconds: float = 2.0,
) -> tuple[bool, str, list[int]]:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        ok, reason, leftovers = _verify_gone(
            psutil_mod, pid, create_time=create_time, descendants=descendants
        )
        if ok:
            return True, reason, []
        if time.monotonic() >= deadline:
            return False, reason, leftovers
        time.sleep(0.1)


def kill_process_tree_pid_checked(pid: int | None) -> ProcessTreeKillResult:
    """按 PID 终止整树并核对结果；返回逐项裁决，不抛异常。"""
    result = ProcessTreeKillResult()
    try:
        normalized_pid = int(pid)
    except (TypeError, ValueError):
        result.reason = "invalid_pid"
        result.detail = f"pid={pid!r} is not a valid pid"
        return result
    if normalized_pid <= 0:
        result.reason = "invalid_pid"
        result.detail = f"pid={normalized_pid} is not a positive pid"
        return result

    psutil_mod = _psutil()
    target_create_time: float | None = None
    if psutil_mod is not None:
        try:
            target_create_time = float(psutil_mod.Process(normalized_pid).create_time())
        except Exception:
            target_create_time = None

    if sys.platform == "win32":
        descendants = _capture_descendants(psutil_mod, normalized_pid)
        result.descendant_total = len(descendants)
        try:
            completed = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(normalized_pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except Exception as exc:
            result.reason = "taskkill_failed"
            result.detail = f"taskkill raised: {exc}"
            return result
        result.initiated = True
        if completed.returncode == 128:
            # taskkill: "not found" —— 目标树里没有可终止的进程。
            result.terminated = True
            result.reason = "already_gone"
            result.detail = (completed.stderr or b"").decode(errors="replace").strip()
            return result
        if completed.returncode != 0:
            result.reason = "taskkill_rejected"
            result.detail = (
                f"taskkill exit={completed.returncode} "
                f"stderr={(completed.stderr or b'').decode(errors='replace').strip()}"
            )
            return result
        ok, reason, leftovers = _wait_until_gone(
            psutil_mod,
            normalized_pid,
            create_time=target_create_time,
            descendants=descendants,
        )
        result.terminated = ok
        result.reason = reason
        result.descendant_leftovers = leftovers
        if not ok:
            result.detail = f"taskkill exit=0 but verification failed: {reason}"
        return result

    descendants = _capture_descendants(psutil_mod, normalized_pid)
    result.descendant_total = len(descendants)
    try:
        pgid = os.getpgid(normalized_pid)
    except (ProcessLookupError, PermissionError, OSError):
        # 进程可能已经消失；交给统一核对裁决。
        ok, reason, leftovers = _wait_until_gone(
            psutil_mod,
            normalized_pid,
            create_time=target_create_time,
            descendants=descendants,
            timeout_seconds=0.5,
        )
        result.initiated = True
        result.terminated = ok
        result.reason = reason if ok else "process_unreachable"
        result.descendant_leftovers = leftovers
        if ok:
            result.detail = "process already gone before signal"
        else:
            result.detail = "os.getpgid failed and process still alive"
        return result
    result.initiated = True
    if pgid == normalized_pid:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except Exception as exc:
            result.reason = "killpg_failed"
            result.detail = f"killpg raised: {exc}"
            return result
    else:
        try:
            os.kill(normalized_pid, signal.SIGKILL)
        except Exception as exc:
            result.reason = "kill_failed"
            result.detail = f"kill raised: {exc}"
            return result
    ok, reason, leftovers = _wait_until_gone(
        psutil_mod,
        normalized_pid,
        create_time=target_create_time,
        descendants=descendants,
    )
    result.terminated = ok
    result.reason = reason
    result.descendant_leftovers = leftovers
    if not ok:
        result.detail = f"signal sent but verification failed: {reason}"
    return result


def kill_process_tree_pid(pid: int | None) -> bool:
    """按 PID 尽力终止整个进程树；返回终止动作已发起且核对未否决。"""
    result = kill_process_tree_pid_checked(pid)
    if not result.initiated:
        return False
    if result.terminated:
        return True
    # 旧合同语义是"发起过终止"；核对失败时不再把命令执行过冒充成功。
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
