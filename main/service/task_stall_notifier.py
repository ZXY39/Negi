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


def _elapsed_minutes(
    started_at: datetime,
    *,
    now: datetime | None = None,
    minute_seconds: float = 60.0,
) -> int:
    current = now if now is not None else _now_utc()
    unit_seconds = max(0.001, float(minute_seconds or 60.0))
    elapsed_seconds = max(0.0, (current - started_at).total_seconds())
    return int(elapsed_seconds // unit_seconds)


def _bucket_from_elapsed_minutes(elapsed_minutes: int) -> int:
    if elapsed_minutes < 20:
        return 0
    if elapsed_minutes < 30:
        return 20
    return int(elapsed_minutes // 10) * 10


def stall_bucket_minutes(
    last_visible_output_at: str,
    *,
    now: datetime | None = None,
    minute_seconds: float = 60.0,
) -> int:
    started_at = _parse_iso(last_visible_output_at)
    if started_at is None:
        return 0
    return _bucket_from_elapsed_minutes(
        _elapsed_minutes(started_at, now=now, minute_seconds=minute_seconds)
    )


def stalled_minutes_since(
    last_visible_output_at: str,
    *,
    now: datetime | None = None,
    minute_seconds: float = 60.0,
) -> int:
    started_at = _parse_iso(last_visible_output_at)
    if started_at is None:
        return 0
    return max(0, _elapsed_minutes(started_at, now=now, minute_seconds=minute_seconds))


def _coerce_positive_seconds(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):  # NaN/inf 防御
        return None
    return parsed if parsed > 0 else None


def running_tool_deadline(runtime_state: Any) -> datetime | None:
    """返回当前仍在运行的节点工具调用里最晚的「本次调用截止时间」。

    截止时间 = ``started_at + timeout_seconds``，其中 ``timeout_seconds`` 是统一
    工具 Timeout 合同为这次调用解析出的保底运行时长（显式 ``timeout`` 参数优先，
    否则全局默认，见 tool-and-skill-system.md「统一工具 Timeout 合同」）。

    只统计 ``status`` 为 ``running`` 且记录了正 ``timeout_seconds`` 的调用；豁免
    工具（无外层时限）不记录 ``timeout_seconds``，因此不参与，保持原有失速行为。
    无此类调用时返回 ``None``。
    """
    frames: list[Any] = []
    if isinstance(runtime_state, dict):
        frames = list(runtime_state.get("frames") or [])
    latest: datetime | None = None
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        for call in list(frame.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            if str(call.get("status") or "").strip().lower() != "running":
                continue
            timeout_seconds = _coerce_positive_seconds(call.get("timeout_seconds"))
            if timeout_seconds is None:
                continue
            started_at = _parse_iso(call.get("started_at"))
            if started_at is None:
                continue
            deadline = started_at + timedelta(seconds=timeout_seconds)
            if latest is None or deadline > latest:
                latest = deadline
    return latest


def effective_silence_start(runtime_state: Any, last_visible_output_at: str) -> datetime | None:
    """失速判定的有效「静默起点」。

    若有正在运行且带已知截止时间的工具调用，则其截止时间晚于最近可见输出时，
    用截止时间作为静默起点：工具运行期间以及截止时间之后，运行时先强制终止工具、
    再由模型回合产出可见输出，静默是预期行为，不应计入失速；只有截止时间过后
    仍持续静默，才算真正的失速候选。
    """
    base = _parse_iso(last_visible_output_at)
    deadline = running_tool_deadline(runtime_state)
    if deadline is not None and (base is None or deadline > base):
        return deadline
    return base


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
        base_time = effective_silence_start(runtime_state, last_visible_output_at)
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
        silence_start = effective_silence_start(runtime_state, last_visible_output_at)
        current_bucket_minutes = (
            _bucket_from_elapsed_minutes(
                _elapsed_minutes(silence_start, minute_seconds=self.minute_seconds)
            )
            if silence_start is not None
            else 0
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
