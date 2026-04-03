from __future__ import annotations

import asyncio
import threading
import time
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
    queued_mono: float


class AdaptiveToolBudgetController:
    def __init__(
        self,
        *,
        normal_limit: int = 6,
        throttled_limit: int | None = None,
        critical_limit: int | None = None,
        safe_limit: int | None = None,
        step_up: int = 1,
    ) -> None:
        self._lock = threading.RLock()
        self._target_running_tools_limit = 1
        self._running_tools_count = 0
        self._waiting_queue: deque[_QueuedToolRequest] = deque()
        self._next_sequence = 0
        self._next_lease_id = 0
        self._pressure_state = 'normal'
        self._last_transition_at = ''
        self._throttled_since = ''
        self._critical_since = ''

    def configure(
        self,
        *,
        normal_limit: int,
        throttled_limit: int | None = None,
        critical_limit: int | None = None,
        safe_limit: int | None = None,
        step_up: int,
    ) -> None:
        ready: list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]] = []
        with self._lock:
            if self._running_tools_count <= 0 and not self._waiting_queue:
                self._reset_idle_locked()
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
        return await self.acquire_work_slot(
            task_id=task_id,
            node_id=node_id,
            work_kind=tool_name,
            work_id=tool_call_id,
        )

    async def acquire_work_slot(
        self,
        *,
        task_id: str,
        node_id: str,
        work_kind: str,
        work_id: str,
    ) -> ToolSlotLease:
        future: asyncio.Future[ToolSlotLease] | None = None
        with self._lock:
            if not self._waiting_queue and self._running_tools_count < self._target_running_tools_limit:
                self._running_tools_count += 1
                return self._build_lease(
                    task_id=task_id,
                    node_id=node_id,
                    tool_name=work_kind,
                    tool_call_id=work_id,
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
                    tool_name=str(work_kind or '').strip() or 'work',
                    tool_call_id=str(work_id or '').strip(),
                    queued_at=_now_iso(),
                    queued_mono=time.perf_counter(),
                )
            )
        try:
            return await future
        except Exception:
            with self._lock:
                self._waiting_queue = deque(item for item in self._waiting_queue if item.future is not future)
            raise

    def release_tool_slot(self, lease: ToolSlotLease | None) -> None:
        self.release_work_slot(lease)

    def release_work_slot(self, lease: ToolSlotLease | None) -> None:
        if lease is None:
            return
        ready: list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]] = []
        with self._lock:
            if self._running_tools_count > 0:
                self._running_tools_count -= 1
            if self._running_tools_count <= 0 and not self._waiting_queue:
                self._reset_idle_locked()
            ready = self._drain_waiters_locked()
        self._resolve_waiters(ready)

    def throttle(self, *, at: str | None = None) -> None:
        with self._lock:
            target_limit = int(self._running_tools_count)
        self.set_budget_state('throttled', at=at, target_limit=target_limit)

    def critical(self, *, at: str | None = None) -> None:
        self.set_budget_state('critical', at=at, target_limit=1)

    def set_budget_state(self, state: str, *, at: str | None = None, target_limit: int | None = None) -> None:
        timestamp = str(at or _now_iso()).strip() or _now_iso()
        with self._lock:
            normalized_state = str(state or 'normal').strip().lower() or 'normal'
            if normalized_state == 'recovering':
                normalized_state = 'easing'
            if normalized_state not in {'normal', 'easing', 'throttled', 'critical'}:
                normalized_state = 'normal'
            self._pressure_state = normalized_state
            if target_limit is None:
                if normalized_state == 'critical':
                    next_limit = 1
                elif normalized_state == 'throttled':
                    next_limit = int(self._running_tools_count)
                else:
                    next_limit = max(int(self._target_running_tools_limit), 1)
            else:
                next_limit = max(0, int(target_limit))
            self._target_running_tools_limit = next_limit
            self._last_transition_at = timestamp
            if normalized_state in {'throttled', 'critical'} and not self._throttled_since:
                self._throttled_since = timestamp
            if normalized_state == 'critical' and not self._critical_since:
                self._critical_since = timestamp
            if normalized_state == 'normal':
                self._throttled_since = ''
                self._critical_since = ''
            elif normalized_state == 'throttled':
                self._critical_since = ''
            if self._running_tools_count <= 0 and not self._waiting_queue and normalized_state == 'normal':
                self._reset_idle_locked()
            ready = self._drain_waiters_locked()
        self._resolve_waiters(ready)

    def begin_easing(self, *, at: str | None = None) -> None:
        timestamp = str(at or _now_iso()).strip() or _now_iso()
        with self._lock:
            self._pressure_state = 'easing'
            self._last_transition_at = timestamp

    def begin_recovery(self, *, at: str | None = None) -> None:
        self.begin_easing(at=at)

    def step_easing(self, *, at: str | None = None) -> bool:
        ready: list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]] = []
        changed = False
        timestamp = str(at or _now_iso()).strip() or _now_iso()
        with self._lock:
            next_limit = max(1, int(self._running_tools_count) + 1)
            if next_limit != self._target_running_tools_limit:
                self._target_running_tools_limit = next_limit
                changed = True
            self._pressure_state = 'easing'
            self._last_transition_at = timestamp
            ready = self._drain_waiters_locked()
        self._resolve_waiters(ready)
        return changed

    def step_recovery(self, *, at: str | None = None) -> bool:
        return self.step_easing(at=at)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            oldest_wait_ms = 0.0
            if self._waiting_queue:
                oldest_wait_ms = max(0.0, (time.perf_counter() - float(self._waiting_queue[0].queued_mono or 0.0)) * 1000.0)
            return {
                'tool_pressure_state': self._pressure_state,
                'tool_pressure_target_limit': int(self._target_running_tools_limit),
                'tool_pressure_running_count': int(self._running_tools_count),
                'tool_pressure_waiting_count': int(len(self._waiting_queue)),
                'tool_queue_running_count': int(self._running_tools_count),
                'tool_queue_waiting_count': int(len(self._waiting_queue)),
                'tool_pressure_last_transition_at': self._last_transition_at,
                'tool_pressure_throttled_since': self._throttled_since,
                'tool_pressure_critical_since': self._critical_since,
                'worker_execution_state': self._pressure_state,
                'worker_execution_target_limit': int(self._target_running_tools_limit),
                'worker_execution_running_count': int(self._running_tools_count),
                'worker_execution_waiting_count': int(len(self._waiting_queue)),
                'worker_execution_oldest_wait_ms': round(oldest_wait_ms, 3),
            }

    def _reset_idle_locked(self) -> None:
        self._pressure_state = 'normal'
        self._target_running_tools_limit = 1
        self._throttled_since = ''
        self._critical_since = ''

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
            if future.done():
                continue
            try:
                loop = future.get_loop()
            except Exception:
                loop = None
            if loop is not None:
                loop.call_soon_threadsafe(_set_future_result_if_pending, future, lease)
            else:
                _set_future_result_if_pending(future, lease)


def _set_future_result_if_pending(future: asyncio.Future[ToolSlotLease], lease: ToolSlotLease) -> None:
    if not future.done():
        future.set_result(lease)
