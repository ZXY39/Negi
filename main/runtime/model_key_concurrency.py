from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class ModelKeyPermitLease:
    lease_id: int
    model_ref: str
    key_index: int
    acquired_at: str
    queued_at: str = ""


@dataclass(slots=True)
class _QueuedModelKeyPermitRequest:
    future: asyncio.Future[ModelKeyPermitLease]
    model_ref: str
    key_index: int
    queued_at: str


class ModelKeyConcurrencyController:
    def __init__(
        self,
        *,
        resolve_model_limits: Callable[[str], dict[str, Any] | None] | None = None,
        on_availability_changed: Callable[[], None] | None = None,
    ) -> None:
        self._resolve_model_limits = resolve_model_limits if callable(resolve_model_limits) else None
        self._on_availability_changed = on_availability_changed if callable(on_availability_changed) else None
        self._lock = threading.RLock()
        self._running_counts: dict[tuple[str, int], int] = {}
        self._waiters: dict[tuple[str, int], deque[_QueuedModelKeyPermitRequest]] = {}
        self._next_lease_id = 0

    def configure(
        self,
        *,
        resolve_model_limits: Callable[[str], dict[str, Any] | None] | None = None,
        on_availability_changed: Callable[[], None] | None = None,
    ) -> None:
        with self._lock:
            if callable(resolve_model_limits):
                self._resolve_model_limits = resolve_model_limits
            if callable(on_availability_changed):
                self._on_availability_changed = on_availability_changed
            ready = self._drain_all_waiters_locked()
        self._resolve_waiters(ready)
        self._notify_availability_changed()

    def model_state(self, model_ref: str) -> dict[str, Any]:
        with self._lock:
            limits = self._normalized_limits(model_ref)
            key_count = int(limits["key_count"])
            per_key_limit = limits["per_key_limit"]
            running = {index: int(self._running_counts.get((model_ref, index), 0)) for index in range(key_count)}
            waiting = {
                index: int(len(self._waiters.get((model_ref, index), ()) or ()))
                for index in range(key_count)
            }
            return {
                "model_ref": str(model_ref or "").strip(),
                "key_count": key_count,
                "per_key_limit": per_key_limit,
                "running": running,
                "waiting": waiting,
            }

    def try_acquire_first_available(self, *, model_ref: str) -> ModelKeyPermitLease | None:
        normalized_model_ref = str(model_ref or "").strip()
        if not normalized_model_ref:
            return None
        with self._lock:
            limits = self._normalized_limits(normalized_model_ref)
            for key_index in range(int(limits["key_count"])):
                if not self._slot_has_capacity_locked(normalized_model_ref, key_index, per_key_limit=limits["per_key_limit"]):
                    continue
                self._running_counts[(normalized_model_ref, key_index)] = int(
                    self._running_counts.get((normalized_model_ref, key_index), 0)
                ) + 1
                return self._build_lease(model_ref=normalized_model_ref, key_index=key_index, queued_at="")
        return None

    async def acquire_specific(self, *, model_ref: str, key_index: int) -> ModelKeyPermitLease:
        normalized_model_ref = str(model_ref or "").strip()
        normalized_key_index = max(0, int(key_index or 0))
        if not normalized_model_ref:
            raise ValueError("model_ref must not be empty")
        future: asyncio.Future[ModelKeyPermitLease]
        with self._lock:
            limits = self._normalized_limits(normalized_model_ref)
            bounded_key_index = min(normalized_key_index, int(limits["key_count"]) - 1)
            if self._slot_has_capacity_locked(normalized_model_ref, bounded_key_index, per_key_limit=limits["per_key_limit"]):
                self._running_counts[(normalized_model_ref, bounded_key_index)] = int(
                    self._running_counts.get((normalized_model_ref, bounded_key_index), 0)
                ) + 1
                return self._build_lease(
                    model_ref=normalized_model_ref,
                    key_index=bounded_key_index,
                    queued_at="",
                )
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._waiters.setdefault((normalized_model_ref, bounded_key_index), deque()).append(
                _QueuedModelKeyPermitRequest(
                    future=future,
                    model_ref=normalized_model_ref,
                    key_index=bounded_key_index,
                    queued_at=_now_iso(),
                )
            )
        self._notify_availability_changed()
        try:
            return await future
        except Exception:
            with self._lock:
                key = (normalized_model_ref, bounded_key_index)
                waiters = deque(
                    item for item in self._waiters.get(key, deque()) if item.future is not future
                )
                if waiters:
                    self._waiters[key] = waiters
                else:
                    self._waiters.pop(key, None)
            self._notify_availability_changed()
            raise

    def release(self, lease: ModelKeyPermitLease | None) -> None:
        if lease is None:
            return
        ready: list[tuple[asyncio.Future[ModelKeyPermitLease], ModelKeyPermitLease]] = []
        with self._lock:
            key = (str(lease.model_ref or "").strip(), max(0, int(lease.key_index or 0)))
            current = max(0, int(self._running_counts.get(key, 0)))
            if current <= 1:
                self._running_counts.pop(key, None)
            else:
                self._running_counts[key] = current - 1
            ready = self._drain_waiters_locked(*key)
        self._resolve_waiters(ready)
        self._notify_availability_changed()

    def refresh(self) -> None:
        with self._lock:
            ready = self._drain_all_waiters_locked()
        self._resolve_waiters(ready)
        self._notify_availability_changed()

    def _normalized_limits(self, model_ref: str) -> dict[str, Any]:
        payload = {}
        if self._resolve_model_limits is not None:
            try:
                payload = dict(self._resolve_model_limits(model_ref) or {})
            except Exception:
                payload = {}
        raw_key_count = payload.get("key_count")
        raw_limit = payload.get("per_key_limit")
        try:
            key_count = max(1, int(raw_key_count or 0))
        except Exception:
            key_count = 1
        if raw_limit in (None, ""):
            per_key_limit = None
        else:
            try:
                per_key_limit = max(1, int(raw_limit))
            except Exception:
                per_key_limit = None
        return {
            "key_count": key_count,
            "per_key_limit": per_key_limit,
        }

    def _slot_has_capacity_locked(self, model_ref: str, key_index: int, *, per_key_limit: int | None) -> bool:
        if per_key_limit is None:
            return True
        return int(self._running_counts.get((model_ref, key_index), 0)) < int(per_key_limit)

    def _build_lease(self, *, model_ref: str, key_index: int, queued_at: str) -> ModelKeyPermitLease:
        self._next_lease_id += 1
        return ModelKeyPermitLease(
            lease_id=self._next_lease_id,
            model_ref=str(model_ref or "").strip(),
            key_index=max(0, int(key_index or 0)),
            acquired_at=_now_iso(),
            queued_at=str(queued_at or "").strip(),
        )

    def _drain_waiters_locked(
        self,
        model_ref: str,
        key_index: int,
    ) -> list[tuple[asyncio.Future[ModelKeyPermitLease], ModelKeyPermitLease]]:
        ready: list[tuple[asyncio.Future[ModelKeyPermitLease], ModelKeyPermitLease]] = []
        key = (str(model_ref or "").strip(), max(0, int(key_index or 0)))
        waiters = self._waiters.get(key)
        if not waiters:
            return ready
        limits = self._normalized_limits(key[0])
        bounded_key_index = min(key[1], int(limits["key_count"]) - 1)
        while waiters and self._slot_has_capacity_locked(key[0], bounded_key_index, per_key_limit=limits["per_key_limit"]):
            request = waiters.popleft()
            if request.future.cancelled():
                continue
            self._running_counts[(key[0], bounded_key_index)] = int(
                self._running_counts.get((key[0], bounded_key_index), 0)
            ) + 1
            ready.append(
                (
                    request.future,
                    self._build_lease(
                        model_ref=key[0],
                        key_index=bounded_key_index,
                        queued_at=request.queued_at,
                    ),
                )
            )
        if waiters:
            self._waiters[key] = waiters
        else:
            self._waiters.pop(key, None)
        return ready

    def _drain_all_waiters_locked(self) -> list[tuple[asyncio.Future[ModelKeyPermitLease], ModelKeyPermitLease]]:
        ready: list[tuple[asyncio.Future[ModelKeyPermitLease], ModelKeyPermitLease]] = []
        for model_ref, key_index in list(self._waiters.keys()):
            ready.extend(self._drain_waiters_locked(model_ref, key_index))
        return ready

    @staticmethod
    def _resolve_waiters(ready: list[tuple[asyncio.Future[ModelKeyPermitLease], ModelKeyPermitLease]]) -> None:
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

    def _notify_availability_changed(self) -> None:
        callback = self._on_availability_changed
        if callback is None:
            return
        try:
            callback()
        except Exception:
            return


def _set_future_result_if_pending(future: asyncio.Future[ModelKeyPermitLease], lease: ModelKeyPermitLease) -> None:
    if not future.done():
        future.set_result(lease)
