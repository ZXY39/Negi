from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


@dataclass(slots=True)
class ToolSlotLease:
    lease_id: int
    task_id: str
    node_id: str
    tool_name: str
    tool_call_id: str
    acquired_at: str
    queued_at: str = ''


@dataclass(slots=True)
class _QueuedToolRequest:
    sequence: int
    future: asyncio.Future[ToolSlotLease]
    task_id: str
    node_id: str
    tool_name: str
    tool_call_id: str
    queued_at: str


class AdaptiveToolBudgetController:
    def __init__(
        self,
        *,
        normal_limit: int = 4,
        safe_limit: int = 1,
        step_up: int = 1,
    ) -> None:
        self._lock = threading.RLock()
        self._normal_limit = max(1, int(normal_limit or 1))
        self._safe_limit = max(1, min(int(safe_limit or 1), self._normal_limit))
        self._step_up = max(1, int(step_up or 1))
        self._target_running_tools_limit = self._normal_limit
        self._running_tools_count = 0
        self._waiting_queue: deque[_QueuedToolRequest] = deque()
        self._next_sequence = 0
        self._next_lease_id = 0
        self._pressure_state = 'normal'
        self._last_transition_at = ''
        self._throttled_since = ''

    def configure(self, *, normal_limit: int, safe_limit: int, step_up: int) -> None:
        ready: list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]] = []
        with self._lock:
            self._normal_limit = max(1, int(normal_limit or 1))
            self._safe_limit = max(1, min(int(safe_limit or 1), self._normal_limit))
            self._step_up = max(1, int(step_up or 1))
            if self._pressure_state == 'throttled':
                self._target_running_tools_limit = min(self._target_running_tools_limit, self._safe_limit)
            else:
                self._target_running_tools_limit = min(self._target_running_tools_limit, self._normal_limit)
                if self._target_running_tools_limit <= 0:
                    self._target_running_tools_limit = self._normal_limit
            ready = self._drain_waiters_locked()
        self._resolve_waiters(ready)

    async def acquire_tool_slot(
        self,
        *,
        task_id: str,
        node_id: str,
        tool_name: str,
        tool_call_id: str,
    ) -> ToolSlotLease:
        future: asyncio.Future[ToolSlotLease] | None = None
        with self._lock:
            if not self._waiting_queue and self._running_tools_count < self._target_running_tools_limit:
                self._running_tools_count += 1
                return self._build_lease(
                    task_id=task_id,
                    node_id=node_id,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    queued_at='',
                )
            loop = asyncio.get_running_loop()
            self._next_sequence += 1
            future = loop.create_future()
            self._waiting_queue.append(
                _QueuedToolRequest(
                    sequence=self._next_sequence,
                    future=future,
                    task_id=str(task_id or '').strip(),
                    node_id=str(node_id or '').strip(),
                    tool_name=str(tool_name or '').strip() or 'tool',
                    tool_call_id=str(tool_call_id or '').strip(),
                    queued_at=_now_iso(),
                )
            )
        try:
            return await future
        except Exception:
            with self._lock:
                self._waiting_queue = deque(item for item in self._waiting_queue if item.future is not future)
            raise

    def release_tool_slot(self, lease: ToolSlotLease | None) -> None:
        if lease is None:
            return
        ready: list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]] = []
        with self._lock:
            if self._running_tools_count > 0:
                self._running_tools_count -= 1
            ready = self._drain_waiters_locked()
        self._resolve_waiters(ready)

    def throttle(self, *, at: str | None = None) -> None:
        timestamp = str(at or _now_iso()).strip() or _now_iso()
        with self._lock:
            self._pressure_state = 'throttled'
            self._target_running_tools_limit = self._safe_limit
            self._last_transition_at = timestamp
            if not self._throttled_since:
                self._throttled_since = timestamp

    def begin_recovery(self, *, at: str | None = None) -> None:
        timestamp = str(at or _now_iso()).strip() or _now_iso()
        with self._lock:
            self._pressure_state = 'recovering'
            if self._target_running_tools_limit <= 0:
                self._target_running_tools_limit = self._safe_limit
            self._last_transition_at = timestamp

    def step_recovery(self, *, at: str | None = None) -> bool:
        ready: list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]] = []
        changed = False
        timestamp = str(at or _now_iso()).strip() or _now_iso()
        with self._lock:
            next_limit = min(self._normal_limit, self._target_running_tools_limit + self._step_up)
            if next_limit != self._target_running_tools_limit:
                self._target_running_tools_limit = next_limit
                changed = True
            if self._target_running_tools_limit >= self._normal_limit:
                self._pressure_state = 'normal'
                self._throttled_since = ''
            else:
                self._pressure_state = 'recovering'
            self._last_transition_at = timestamp
            ready = self._drain_waiters_locked()
        self._resolve_waiters(ready)
        return changed

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                'tool_pressure_state': self._pressure_state,
                'tool_pressure_target_limit': int(self._target_running_tools_limit),
                'tool_pressure_running_count': int(self._running_tools_count),
                'tool_pressure_waiting_count': int(len(self._waiting_queue)),
                'tool_pressure_last_transition_at': self._last_transition_at,
                'tool_pressure_throttled_since': self._throttled_since,
            }

    def _build_lease(
        self,
        *,
        task_id: str,
        node_id: str,
        tool_name: str,
        tool_call_id: str,
        queued_at: str,
    ) -> ToolSlotLease:
        self._next_lease_id += 1
        return ToolSlotLease(
            lease_id=self._next_lease_id,
            task_id=str(task_id or '').strip(),
            node_id=str(node_id or '').strip(),
            tool_name=str(tool_name or '').strip() or 'tool',
            tool_call_id=str(tool_call_id or '').strip(),
            acquired_at=_now_iso(),
            queued_at=str(queued_at or '').strip(),
        )

    def _drain_waiters_locked(self) -> list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]]:
        ready: list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]] = []
        while self._waiting_queue and self._running_tools_count < self._target_running_tools_limit:
            request = self._waiting_queue.popleft()
            if request.future.cancelled():
                continue
            self._running_tools_count += 1
            ready.append(
                (
                    request.future,
                    self._build_lease(
                        task_id=request.task_id,
                        node_id=request.node_id,
                        tool_name=request.tool_name,
                        tool_call_id=request.tool_call_id,
                        queued_at=request.queued_at,
                    ),
                )
            )
        return ready

    @staticmethod
    def _resolve_waiters(ready: list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]]) -> None:
        for future, lease in ready:
            if not future.done():
                future.set_result(lease)
