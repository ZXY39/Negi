from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stall_bucket_minutes(
    last_visible_output_at: str,
    *,
    now: datetime | None = None,
    minute_seconds: float = 60.0,
) -> int:
    started_at = _parse_iso(last_visible_output_at)
    current = now if now is not None else _now_utc()
    if started_at is None:
        return 0
    elapsed_seconds = max(0.0, (current - started_at).total_seconds())
    unit_seconds = max(0.001, float(minute_seconds or 60.0))
    elapsed_minutes = int(elapsed_seconds // unit_seconds)
    if elapsed_minutes < 20:
        return 0
    if elapsed_minutes < 30:
        return 20
    return int(elapsed_minutes // 10) * 10


def stalled_minutes_since(
    last_visible_output_at: str,
    *,
    now: datetime | None = None,
    minute_seconds: float = 60.0,
) -> int:
    started_at = _parse_iso(last_visible_output_at)
    current = now if now is not None else _now_utc()
    if started_at is None:
        return 0
    unit_seconds = max(0.001, float(minute_seconds or 60.0))
    return max(0, int((current - started_at).total_seconds() // unit_seconds))


def _next_bucket_minutes(last_bucket_minutes: int) -> int:
    bucket = max(0, int(last_bucket_minutes or 0))
    if bucket <= 0:
        return 20
    return bucket + 10


class TaskStallNotifier:
    def __init__(self, *, service: Any, minute_seconds: float = 60.0) -> None:
        self._service = service
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.minute_seconds = max(0.001, float(minute_seconds or 60.0))

    @staticmethod
    def _is_web_task(service: Any, task: Any) -> bool:
        origin_getter = getattr(service, "_task_origin_session_id", None)
        if callable(origin_getter):
            session_id = str(origin_getter(task) or "").strip()
        else:
            session_id = str(getattr(task, "session_id", "") or "").strip()
        return session_id.startswith("web:")

    def start_task(self, task_id: str) -> None:
        task = self._service.get_task(task_id)
        if task is None or not self._is_web_task(self._service, task):
            self.cancel_task(task_id)
            return
        runtime_meta = self._service.log_service.read_task_runtime_meta(task.task_id) or {}
        last_visible_output_at = str(runtime_meta.get("last_visible_output_at") or task.created_at or "").strip()
        if not last_visible_output_at:
            last_visible_output_at = str(getattr(task, "created_at", "") or "").strip()
        self._service.log_service.update_task_runtime_meta(
            task.task_id,
            last_visible_output_at=last_visible_output_at,
            last_stall_notice_bucket_minutes=max(
                0,
                int(runtime_meta.get("last_stall_notice_bucket_minutes") or 0),
            ),
        )
        self._schedule(task.task_id)

    def reset_visible_output(self, task_id: str, *, occurred_at: str | None = None) -> None:
        task = self._service.get_task(task_id)
        if task is None or not self._is_web_task(self._service, task):
            self.cancel_task(task_id)
            return
        reset_at = str(occurred_at or "").strip() or self._service._stall_now_iso()
        self._service.log_service.update_task_runtime_meta(
            task.task_id,
            last_visible_output_at=reset_at,
            last_stall_notice_bucket_minutes=0,
        )
        self._schedule(task.task_id)

    def pause_task(self, task_id: str) -> None:
        self.cancel_task(task_id)

    def cancel_requested(self, task_id: str) -> None:
        self.cancel_task(task_id)

    def terminal_task(self, task: Any) -> None:
        task_id = str(getattr(task, "task_id", "") or "").strip()
        self.cancel_task(task_id)

    def bootstrap_running_tasks(self) -> None:
        list_tasks = getattr(self._service.store, "list_tasks", None)
        if not callable(list_tasks):
            return
        for task in list(list_tasks() or []):
            task_id = str(getattr(task, "task_id", "") or "").strip()
            if not task_id:
                continue
            if str(getattr(task, "status", "") or "").strip().lower() != "in_progress":
                continue
            if bool(getattr(task, "is_paused", False)):
                continue
            self.start_task(task_id)

    def cancel_task(self, task_id: str) -> None:
        key = str(task_id or "").strip()
        task = self._tasks.pop(key, None)
        if task is not None:
            task.cancel()

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule(self, task_id: str) -> None:
        key = str(task_id or "").strip()
        if not key:
            return
        self.cancel_task(key)
        task = self._service.get_task(key)
        if task is None or not self._is_web_task(self._service, task):
            return
        runtime_state = self._service.log_service.read_runtime_state(key) or {}
        is_actionable = getattr(self._service, "is_task_stall_actionable", None)
        if callable(is_actionable):
            try:
                if not bool(is_actionable(key, runtime_state=runtime_state)):
                    return
            except Exception:
                return
        elif (
            str(getattr(task, "status", "") or "").strip().lower() != "in_progress"
            or bool(getattr(task, "is_paused", False))
            or bool(getattr(task, "pause_requested", False))
            or bool(getattr(task, "cancel_requested", False))
        ):
            return
        last_visible_output_at = str(runtime_state.get("last_visible_output_at") or task.created_at or "").strip()
        if not last_visible_output_at:
            return
        last_bucket_minutes = max(0, int(runtime_state.get("last_stall_notice_bucket_minutes") or 0))
        next_bucket_minutes = _next_bucket_minutes(last_bucket_minutes)
        base_time = _parse_iso(last_visible_output_at)
        if base_time is None:
            return
        due_at = base_time + timedelta(seconds=(next_bucket_minutes * self.minute_seconds))
        delay_seconds = max(0.0, (due_at - _now_utc()).total_seconds())
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        scheduled = loop.create_task(
            self._sleep_then_emit(key, delay_seconds),
            name=f"task-stall:{key}",
        )
        self._tasks[key] = scheduled
        scheduled.add_done_callback(lambda done_task, stored_key=key: self._cleanup(stored_key, done_task))

    def _cleanup(self, task_id: str, done_task: asyncio.Task[None]) -> None:
        current = self._tasks.get(task_id)
        if current is done_task:
            self._tasks.pop(task_id, None)

    async def _sleep_then_emit(self, task_id: str, delay_seconds: float) -> None:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await self._emit_if_still_due(task_id)

    async def _emit_if_still_due(self, task_id: str) -> None:
        task = self._service.get_task(task_id)
        if task is None or not self._is_web_task(self._service, task):
            return
        runtime_state = self._service.log_service.read_runtime_state(task_id) or {}
        is_actionable = getattr(self._service, "is_task_stall_actionable", None)
        if callable(is_actionable):
            try:
                if not bool(is_actionable(task_id, runtime_state=runtime_state)):
                    return
            except Exception:
                return
        elif (
            str(getattr(task, "status", "") or "").strip().lower() != "in_progress"
            or bool(getattr(task, "is_paused", False))
            or bool(getattr(task, "pause_requested", False))
            or bool(getattr(task, "cancel_requested", False))
        ):
            return
        last_visible_output_at = str(runtime_state.get("last_visible_output_at") or task.created_at or "").strip()
        last_bucket_minutes = max(0, int(runtime_state.get("last_stall_notice_bucket_minutes") or 0))
        current_bucket_minutes = stall_bucket_minutes(
            last_visible_output_at,
            minute_seconds=self.minute_seconds,
        )
        if current_bucket_minutes <= last_bucket_minutes:
            self._schedule(task_id)
            return
        payload = self._service.build_task_stall_payload(
            task_id,
            bucket_minutes=current_bucket_minutes,
            last_visible_output_at=last_visible_output_at,
        )
        if not payload:
            self._schedule(task_id)
            return
        self._service.log_service.update_task_runtime_meta(
            task_id,
            last_stall_notice_bucket_minutes=current_bucket_minutes,
        )
        self._service.emit_task_stall(payload)
        self._schedule(task_id)
