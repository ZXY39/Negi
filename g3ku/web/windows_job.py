from __future__ import annotations

import atexit
import ctypes
import os
from ctypes import wintypes
from typing import Any


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001

_JOB_HANDLE = None
_JOB_ATEXIT_REGISTERED = False


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _get_kernel32():
    try:
        return ctypes.windll.kernel32
    except Exception:
        return None


def _coerce_pid(process: Any) -> int:
    if isinstance(process, int):
        return max(0, int(process))
    return max(0, int(getattr(process, "pid", 0) or 0))


def _register_atexit() -> None:
    global _JOB_ATEXIT_REGISTERED
    if _JOB_ATEXIT_REGISTERED:
        return
    atexit.register(close_kill_on_close_job)
    _JOB_ATEXIT_REGISTERED = True


def _ensure_job_handle(kernel32) -> Any | None:
    global _JOB_HANDLE
    if _JOB_HANDLE:
        return _JOB_HANDLE

    job_handle = kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        return None

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        job_handle,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        try:
            kernel32.CloseHandle(job_handle)
        except Exception:
            pass
        return None

    _JOB_HANDLE = job_handle
    _register_atexit()
    return _JOB_HANDLE


def close_kill_on_close_job() -> None:
    global _JOB_HANDLE
    if os.name != "nt":
        _JOB_HANDLE = None
        return
    if not _JOB_HANDLE:
        return
    kernel32 = _get_kernel32()
    if kernel32 is None:
        _JOB_HANDLE = None
        return
    try:
        kernel32.CloseHandle(_JOB_HANDLE)
    except Exception:
        pass
    _JOB_HANDLE = None


def assign_process_to_kill_on_close_job(process: Any) -> bool:
    if os.name != "nt":
        return False

    pid = _coerce_pid(process)
    if pid <= 0:
        return False

    kernel32 = _get_kernel32()
    if kernel32 is None:
        return False

    job_handle = _ensure_job_handle(kernel32)
    if not job_handle:
        return False

    access = PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
    process_handle = kernel32.OpenProcess(access, False, pid)
    if not process_handle:
        return False

    try:
        return bool(kernel32.AssignProcessToJobObject(job_handle, process_handle))
    finally:
        try:
            kernel32.CloseHandle(process_handle)
        except Exception:
            pass


__all__ = [
    "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
    "assign_process_to_kill_on_close_job",
    "close_kill_on_close_job",
]
