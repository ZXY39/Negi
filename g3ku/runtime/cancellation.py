from __future__ import annotations

import subprocess
import threading
import time
from typing import Any


class ToolCancellationRequested(RuntimeError):
    """Raised when a tool should stop because the session was paused or cancelled."""


class ToolCancellationToken:
    """Cooperative cancellation token shared between a turn and its tools."""

    def __init__(self, *, session_key: str, reason: str = "") -> None:
        self.session_key = str(session_key or "").strip()
        self.reason = str(reason or "").strip()
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._processes: set[subprocess.Popen[Any]] = set()
        self._children: set["ToolCancellationToken"] = set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def derive_child(self, *, reason: str = "") -> "ToolCancellationToken":
        child = ToolCancellationToken(
            session_key=self.session_key,
            reason=str(reason or "").strip(),
        )
        with self._lock:
            self._children.add(child)
            already_cancelled = self._event.is_set()
            inherited_reason = str(self.reason or "").strip()
        if already_cancelled:
            child.cancel(reason=inherited_reason or str(reason or "").strip() or "user_cancelled")
        return child

    def detach_child(self, child: "ToolCancellationToken | None") -> None:
        if child is None:
            return
        with self._lock:
            self._children.discard(child)

    def cancel(self, *, reason: str = "user_cancelled") -> None:
        with self._lock:
            if self._event.is_set():
                if reason and not self.reason:
                    self.reason = str(reason).strip()
                return
            self.reason = str(reason or self.reason or "user_cancelled").strip()
            self._event.set()
            children = list(self._children)
        for child in children:
            try:
                child.cancel(reason=self.reason)
            except Exception:
                continue
        self.terminate_registered_processes()

    def raise_if_cancelled(self, *, default_message: str = "用户已请求暂停，正在安全停止...") -> None:
        if not self.is_cancelled():
            return
        raise ToolCancellationRequested(str(self.reason or default_message))

    def register_process(self, process: subprocess.Popen[Any] | None) -> None:
        if process is None:
            return
        with self._lock:
            self._processes.add(process)

    def unregister_process(self, process: subprocess.Popen[Any] | None) -> None:
        if process is None:
            return
        with self._lock:
            self._processes.discard(process)

    def terminate_registered_processes(self, *, grace_seconds: float = 2.0) -> None:
        snapshot: list[subprocess.Popen[Any]]
        with self._lock:
            snapshot = list(self._processes)
        for process in snapshot:
            try:
                if process.poll() is not None:
                    self.unregister_process(process)
                    continue
                process.terminate()
            except Exception:
                self.unregister_process(process)
                continue
        if grace_seconds > 0:
            deadline = time.monotonic() + float(grace_seconds)
            while time.monotonic() < deadline:
                remaining = [process for process in snapshot if process.poll() is None]
                if not remaining:
                    break
                time.sleep(0.05)
        for process in snapshot:
            try:
                if process.poll() is None:
                    process.kill()
            except Exception:
                pass
            finally:
                self.unregister_process(process)
