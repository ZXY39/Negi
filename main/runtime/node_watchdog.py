"""Node watchdog: three structural pause criteria (P6).

The watchdog never kills a turn. It only decides that a node has stopped making
*structural* progress, then asks the node runner to mark it as an abnormal pause
(``pause_reason='error'``) after the current synchronous tool call has ended.
The criteria are behavioural, not content-based:

* ``repeated_identical_tool_call`` — the last three terminal calls are the same
  tool with byte-identical arguments, so no new information can have entered the
  context (a success-only loop).
* ``tool_error_streak`` — same, but all three failed; the model neither changed
  the arguments nor switched tools.
* ``no_new_events`` — nothing happened for longer than the stall window. Parked
  phases (``waiting_explicit`` / ``waiting_acceptance``) and paused nodes are
  explicitly excluded: they are idle by design, not stalled.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

PARKED_PHASES = frozenset({'waiting_explicit', 'waiting_children', 'waiting_acceptance'})
REPEAT_THRESHOLD = 3
ERROR_STREAK_THRESHOLD = 3
_TERMINAL_TOOL_STATUSES = {'success', 'error', 'timeout', 'cancelled'}
WATCHDOG_HISTORY_KEY = 'watchdog_history'


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _signature(item: Any) -> tuple[str, str]:
    name = str(getattr(item, 'tool_name', '') or (item.get('tool_name') if isinstance(item, dict) else '') or '').strip()
    arguments = str(
        getattr(item, 'arguments_text', '') or (item.get('arguments_text') if isinstance(item, dict) else '') or ''
    ).strip()
    return name, arguments


def _status(item: Any) -> str:
    value = getattr(item, 'status', None)
    if value is None and isinstance(item, dict):
        value = item.get('status')
    return str(value or '').strip().lower()


def _finished_at(item: Any) -> str:
    value = getattr(item, 'finished_at', None)
    if value is None and isinstance(item, dict):
        value = item.get('finished_at')
    return str(value or '').strip()


def _frame_has_running_tool(frame: dict[str, Any] | None) -> bool:
    for call in list((frame or {}).get('tool_calls') or []):
        if isinstance(call, dict) and str(call.get('status') or '').strip().lower() == 'running':
            return True
    return False


def watchdog_pause_reason(
    *,
    frame: dict[str, Any] | None,
    tool_results: list[Any],
    now: datetime | None = None,
    stall_seconds: float = 600.0,
    last_event_at: str = '',
) -> str:
    """Return the pause reason, or ``''`` when the node should keep running."""
    phase = str((frame or {}).get('phase') or '').strip()
    if phase in PARKED_PHASES:
        return ''
    # 协作式：工具还在跑就不判停，等它结束或超时后下一拍再看。
    if _frame_has_running_tool(frame):
        return ''

    terminal = [item for item in list(tool_results or []) if _status(item) in _TERMINAL_TOOL_STATUSES]
    recent = terminal[-max(REPEAT_THRESHOLD, ERROR_STREAK_THRESHOLD):]
    if len(recent) >= REPEAT_THRESHOLD:
        signatures = {_signature(item) for item in recent}
        if len(signatures) == 1 and next(iter(signatures))[0] and next(iter(signatures))[1]:
            statuses = {_status(item) for item in recent}
            if statuses == {'error'}:
                return 'tool_error_streak'
            if len(statuses) == 1:
                return 'repeated_identical_tool_call'

    current = now if now is not None else datetime.now(timezone.utc)
    last_event: datetime | None = _parse_iso(last_event_at)
    for item in terminal[-1:]:
        stamp = _parse_iso(_finished_at(item))
        if stamp is not None and (last_event is None or stamp > last_event):
            last_event = stamp
    if last_event is None:
        return ''
    if (current - last_event).total_seconds() >= max(1.0, float(stall_seconds or 600.0)):
        return 'no_new_events'
    return ''


class NodeWatchdog:
    """Per-task periodic scan that applies :func:`watchdog_pause_reason`."""

    def __init__(
        self,
        *,
        service: Any,
        interval_seconds: float = 30.0,
        stall_seconds: float = 600.0,
    ) -> None:
        self._service = service
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.interval_seconds = max(1.0, float(interval_seconds or 30.0))
        self.stall_seconds = max(1.0, float(stall_seconds or 600.0))

    def start_task(self, task_id: str) -> None:
        key = str(task_id or '').strip()
        if not key:
            return
        self.cancel_task(key)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        scheduled = loop.create_task(self._loop(key), name=f'node-watchdog:{key}')
        self._tasks[key] = scheduled
        scheduled.add_done_callback(lambda done, stored_key=key: self._cleanup(stored_key, done))

    def cancel_task(self, task_id: str) -> None:
        task = self._tasks.pop(str(task_id or '').strip(), None)
        if task is not None:
            task.cancel()

    def bootstrap_running_tasks(self) -> None:
        list_tasks = getattr(self._service.store, 'list_tasks', None)
        if not callable(list_tasks):
            return
        for task in list(list_tasks() or []):
            if str(getattr(task, 'status', '') or '').strip().lower() != 'in_progress':
                continue
            if bool(getattr(task, 'is_paused', False)) or bool(getattr(task, 'pause_requested', False)):
                continue
            self.start_task(str(getattr(task, 'task_id', '') or ''))

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _cleanup(self, task_id: str, done: asyncio.Task[None]) -> None:
        if self._tasks.get(task_id) is done:
            self._tasks.pop(task_id, None)

    async def _loop(self, task_id: str) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                self.scan_task(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 看门狗自身故障不得打死任务
                logger.debug(f'node watchdog scan skipped: {task_id}: {exc}')

    def scan_task(self, task_id: str) -> list[str]:
        """Scan one task; return the node ids paused by this scan."""
        task = self._service.store.get_task(task_id)
        if task is None or str(getattr(task, 'status', '') or '').strip().lower() != 'in_progress':
            return []
        if bool(getattr(task, 'is_paused', False)) or bool(getattr(task, 'pause_requested', False)):
            return []
        paused: list[str] = []
        for node in list(self._service.store.list_nodes(task_id) or []):
            if str(getattr(node, 'node_kind', '') or '').strip().lower() != 'execution':
                continue
            if str(getattr(node, 'status', '') or '').strip().lower() != 'in_progress':
                continue
            if bool(getattr(node, 'is_paused', False)) or bool(getattr(node, 'pause_requested', False)):
                continue
            frame = self._service.log_service.read_runtime_frame(task_id, node.node_id) or {}
            results = list(self._service.store.list_task_node_tool_results(task_id, node.node_id) or [])
            reason = watchdog_pause_reason(
                frame=frame if isinstance(frame, dict) else {},
                tool_results=results,
                stall_seconds=self.stall_seconds,
                last_event_at=str(getattr(node, 'updated_at', '') or ''),
            )
            if not reason:
                continue
            self._pause_node(task_id=task_id, node=node, reason=reason)
            paused.append(str(node.node_id or ''))
        return paused

    def _pause_node(self, *, task_id: str, node: Any, reason: str) -> None:
        node_id = str(getattr(node, 'node_id', '') or '').strip()
        remark = {
            'repeated_identical_tool_call': '看门狗判停：同名工具携带完全相同的参数连续成功执行，没有新信息进入上下文。',
            'tool_error_streak': '看门狗判停：同名工具以相同参数连续失败，既未改参数也未换工具。',
            'no_new_events': '看门狗判停：超过失速窗口没有任何新事件。',
        }.get(reason, f'看门狗判停：{reason}')
        runner = self._service.node_runner
        entry = {
            'at': str(self._service._stall_now_iso()) if hasattr(self._service, '_stall_now_iso') else '',
            'reason': reason,
            'remark': remark,
        }

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            history = list(metadata.get(WATCHDOG_HISTORY_KEY) or [])
            history.append(dict(entry))
            metadata[WATCHDOG_HISTORY_KEY] = history[-10:]
            return metadata

        self._service.log_service.update_node_metadata(node_id, _mutate)
        runner._mark_node_paused(task_id, node_id, reason='error', remark=remark)
        runner.notify_parent_abnormal_pause(task_id=task_id, node_id=node_id, reason=remark)
        self._service.log_service.refresh_task_view(task_id, mark_unread=True)
