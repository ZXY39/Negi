from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from main.runtime.model_key_concurrency import ModelKeyConcurrencyController, ModelKeyPermitLease


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class NodeTurnLease:
    lease_id: int
    task_id: str
    node_id: str
    model_ref: str
    key_index: int
    acquired_at: str
    initial_model_permit: ModelKeyPermitLease | None = None
    queued_at: str = ""


@dataclass(slots=True)
class _QueuedNodeTurnRequest:
    future: asyncio.Future[NodeTurnLease]
    task_id: str
    node_id: str
    model_ref: str
    queued_at: str
    queued_mono: float


class NodeTurnController:
    def __init__(
        self,
        *,
        model_concurrency_controller: ModelKeyConcurrencyController,
        gate_supplier: Callable[[], bool] | None = None,
        freeze_supplier: Callable[[str], bool] | None = None,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        self._model_concurrency_controller = model_concurrency_controller
        self._gate_supplier = gate_supplier if callable(gate_supplier) else (lambda: True)
        self._freeze_supplier = freeze_supplier if callable(freeze_supplier) else (lambda _task_id: False)
        self._poll_interval_seconds = max(0.05, float(poll_interval_seconds or 0.1))
        self._lock = threading.RLock()
        self._queue: deque[_QueuedNodeTurnRequest] = deque()
        self._frozen_queues: dict[str, deque[_QueuedNodeTurnRequest]] = {}
        self._running_leases: dict[int, NodeTurnLease] = {}
        self._next_lease_id = 0
        self._wake_event: asyncio.Event | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    def configure(
        self,
        *,
        gate_supplier: Callable[[], bool] | None = None,
        freeze_supplier: Callable[[str], bool] | None = None,
    ) -> None:
        with self._lock:
            if callable(gate_supplier):
                self._gate_supplier = gate_supplier
            if callable(freeze_supplier):
                self._freeze_supplier = freeze_supplier
        self.poke()

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            oldest_wait_ms = 0.0
            queued_items = list(self._queue)
            for queue in self._frozen_queues.values():
                queued_items.extend(list(queue or []))
            if queued_items:
                oldest_queued_mono = min(float(item.queued_mono or 0.0) for item in queued_items)
                oldest_wait_ms = max(0.0, (time.perf_counter() - oldest_queued_mono) * 1000.0)
            return {
                "node_queue_running_count": int(len(self._running_leases)),
                "node_queue_waiting_count": int(len(self._queue) + sum(len(queue) for queue in self._frozen_queues.values())),
                "node_queue_frozen_count": int(sum(len(queue) for queue in self._frozen_queues.values())),
                "node_queue_oldest_wait_ms": round(oldest_wait_ms, 3),
            }

    async def acquire_turn(self, *, task_id: str, node_id: str, model_ref: str) -> NodeTurnLease:
        normalized_task_id = str(task_id or "").strip()
        normalized_node_id = str(node_id or "").strip()
        normalized_model_ref = str(model_ref or "").strip()
        if not normalized_task_id or not normalized_node_id or not normalized_model_ref:
            raise ValueError("task_id, node_id, and model_ref are required")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[NodeTurnLease] = loop.create_future()
        with self._lock:
            self._ensure_pump_locked(loop)
            request = _QueuedNodeTurnRequest(
                future=future,
                task_id=normalized_task_id,
                node_id=normalized_node_id,
                model_ref=normalized_model_ref,
                queued_at=_now_iso(),
                queued_mono=time.perf_counter(),
            )
            if self._is_task_frozen_locked(normalized_task_id):
                self._frozen_queue_for_task_locked(normalized_task_id).append(request)
            else:
                self._queue.append(request)
        self.poke()
        try:
            return await future
        except Exception:
            with self._lock:
                self._queue = deque(item for item in self._queue if item.future is not future)
                for task_id, queue in list(self._frozen_queues.items()):
                    next_queue = deque(item for item in queue if item.future is not future)
                    if next_queue:
                        self._frozen_queues[task_id] = next_queue
                    else:
                        self._frozen_queues.pop(task_id, None)
            self.poke()
            raise

    def release_turn(self, lease: NodeTurnLease | None) -> None:
        if lease is None:
            return
        with self._lock:
            self._running_leases.pop(int(lease.lease_id or 0), None)
        self.poke()

    def poke(self) -> None:
        wake_event = self._wake_event
        loop = self._loop
        if wake_event is None or loop is None:
            return
        try:
            loop.call_soon_threadsafe(wake_event.set)
        except Exception:
            return

    async def close(self) -> None:
        pump_task = self._pump_task
        self._pump_task = None
        with self._lock:
            self._closed = True
            while self._queue:
                request = self._queue.popleft()
                if not request.future.done():
                    request.future.cancel()
            for task_id in list(self._frozen_queues.keys()):
                queue = self._frozen_queues.pop(task_id, deque())
                while queue:
                    request = queue.popleft()
                    if not request.future.done():
                        request.future.cancel()
        self.poke()
        if pump_task is not None and not pump_task.done():
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
        self._wake_event = None
        self._loop = None

    def _ensure_pump_locked(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._closed:
            raise RuntimeError("node turn controller is closed")
        self._loop = loop
        if self._wake_event is None:
            self._wake_event = asyncio.Event()
        if self._pump_task is not None and not self._pump_task.done():
            return
        self._pump_task = loop.create_task(self._pump(), name="node-turn-controller")

    async def _pump(self) -> None:
        try:
            while True:
                granted = False
                while True:
                    request = self._peek_request()
                    if request is None:
                        if not self._has_pending_requests():
                            return
                        break
                    if not bool(self._gate_supplier()):
                        break
                    permit = self._model_concurrency_controller.try_acquire_first_available(model_ref=request.model_ref)
                    if permit is None:
                        break
                    granted = True
                    with self._lock:
                        current = self._queue.popleft() if self._queue else None
                        if current is None or current.future.cancelled():
                            self._model_concurrency_controller.release(permit)
                            continue
                        self._next_lease_id += 1
                        lease = NodeTurnLease(
                            lease_id=self._next_lease_id,
                            task_id=current.task_id,
                            node_id=current.node_id,
                            model_ref=current.model_ref,
                            key_index=int(permit.key_index),
                            acquired_at=_now_iso(),
                            initial_model_permit=permit,
                            queued_at=current.queued_at,
                        )
                        self._running_leases[lease.lease_id] = lease
                    _set_future_result_if_pending(current.future, lease)
                wake_event = self._wake_event
                if wake_event is None:
                    return
                if granted:
                    await asyncio.sleep(0)
                    continue
                wake_event.clear()
                try:
                    await asyncio.wait_for(wake_event.wait(), timeout=self._poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._pump_task = None

    def _peek_request(self) -> _QueuedNodeTurnRequest | None:
        with self._lock:
            self._reconcile_frozen_queues_locked()
            while self._queue and self._queue[0].future.cancelled():
                self._queue.popleft()
            return self._queue[0] if self._queue else None

    def _has_pending_requests(self) -> bool:
        with self._lock:
            if self._queue:
                return True
            return any(queue for queue in self._frozen_queues.values())

    def _reconcile_frozen_queues_locked(self) -> None:
        if self._queue:
            next_queue: deque[_QueuedNodeTurnRequest] = deque()
            while self._queue:
                request = self._queue.popleft()
                if request.future.cancelled():
                    continue
                if self._is_task_frozen_locked(request.task_id):
                    self._frozen_queue_for_task_locked(request.task_id).append(request)
                else:
                    next_queue.append(request)
            self._queue = next_queue
        thawed_task_ids = [
            task_id
            for task_id in list(self._frozen_queues.keys())
            if not self._is_task_frozen_locked(task_id)
        ]
        for task_id in thawed_task_ids:
            queue = self._frozen_queues.pop(task_id, deque())
            while queue:
                request = queue.popleft()
                if request.future.cancelled():
                    continue
                self._queue.append(request)

    def _frozen_queue_for_task_locked(self, task_id: str) -> deque[_QueuedNodeTurnRequest]:
        key = str(task_id or "").strip()
        queue = self._frozen_queues.get(key)
        if queue is None:
            queue = deque()
            self._frozen_queues[key] = queue
        return queue

    def _is_task_frozen_locked(self, task_id: str) -> bool:
        try:
            return bool(self._freeze_supplier(str(task_id or "").strip()))
        except Exception:
            return False


def _set_future_result_if_pending(future: asyncio.Future[NodeTurnLease], lease: NodeTurnLease) -> None:
    if not future.done():
        future.set_result(lease)
