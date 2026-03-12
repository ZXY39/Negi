from __future__ import annotations

import platform
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from g3ku.resources.models import ResourceBusyState, ResourceKind


def _open_delete_guard(path: Path):
    if platform.system().lower() != "windows":
        return None
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            str(path),
            0x80000000,
            0x00000001 | 0x00000002,
            None,
            3,
            0x00000080,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in {None, invalid}:
            return None
        return handle
    except Exception:
        return None


def _close_delete_guard(handle: Any) -> None:
    if handle is None or platform.system().lower() != "windows":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        pass


@dataclass(slots=True)
class _LockRecord:
    refs: int = 0
    pending_delete: bool = False
    guard_handle: Any = None
    guard_path: Path | None = None


class ResourceAccessHandle:
    def __init__(self, manager: "ResourceLockManager", kind: ResourceKind, name: str):
        self._manager = manager
        self._kind = kind
        self._name = name
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._manager.release(self._kind, self._name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


class ResourceLockManager:
    def __init__(self, *, windows_fs_lock: bool = True):
        self._records: dict[tuple[str, str], _LockRecord] = {}
        self._paths: dict[tuple[str, str], Path] = {}
        self._lock = threading.RLock()
        self._windows_fs_lock = windows_fs_lock

    def register_path(self, kind: ResourceKind, name: str, path: Path | None) -> None:
        if path is None:
            return
        with self._lock:
            self._paths[(kind.value, name)] = path

    def unregister_path(self, kind: ResourceKind, name: str) -> None:
        with self._lock:
            self._paths.pop((kind.value, name), None)

    def acquire(self, kind: ResourceKind, name: str) -> ResourceAccessHandle:
        key = (kind.value, name)
        with self._lock:
            record = self._records.setdefault(key, _LockRecord())
            record.refs += 1
            if record.refs == 1 and self._windows_fs_lock:
                guard_path = self._paths.get(key)
                if guard_path and guard_path.exists():
                    record.guard_path = guard_path
                    record.guard_handle = _open_delete_guard(guard_path)
        return ResourceAccessHandle(self, kind, name)

    def release(self, kind: ResourceKind, name: str) -> None:
        key = (kind.value, name)
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            record.refs = max(0, record.refs - 1)
            if record.refs == 0:
                _close_delete_guard(record.guard_handle)
                record.guard_handle = None
                record.guard_path = None
                if not record.pending_delete:
                    self._records.pop(key, None)

    def mark_pending_delete(self, kind: ResourceKind, name: str) -> None:
        with self._lock:
            self._records.setdefault((kind.value, name), _LockRecord()).pending_delete = True

    def clear_pending_delete(self, kind: ResourceKind, name: str) -> None:
        key = (kind.value, name)
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            record.pending_delete = False
            if record.refs == 0:
                self._records.pop(key, None)

    def is_busy(self, kind: ResourceKind, name: str) -> bool:
        with self._lock:
            record = self._records.get((kind.value, name))
            return bool(record and record.refs > 0)

    def busy_state(self, kind: ResourceKind, name: str) -> ResourceBusyState:
        with self._lock:
            record = self._records.get((kind.value, name))
            if record is None:
                return ResourceBusyState(refs=0, pending_delete=False, busy=False)
            return ResourceBusyState(refs=record.refs, pending_delete=record.pending_delete, busy=record.refs > 0)
