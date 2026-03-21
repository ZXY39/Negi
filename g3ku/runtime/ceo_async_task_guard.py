from __future__ import annotations

from typing import Any


CREATE_ASYNC_TASK_TOOL_NAME = 'create_async_task'
STATE_KEY = 'ceo_async_task_guard_state'
REMINDER_TEMPLATE = (
    '由于执行轮数超过{threshold}轮，工具执行暂被拦截，立即评估是否需要将剩余工作改成异步任务，'
    '如果继续自行执行，请重新调用工具。'
)


def build_guard_state(
    *,
    completed_rounds: int = 0,
    consumed_thresholds: list[int] | None = None,
) -> dict[str, Any]:
    normalized_thresholds = sorted(
        {max(0, int(item or 0)) for item in list(consumed_thresholds or []) if int(item or 0) > 0}
    )
    return {
        'completed_rounds': max(0, int(completed_rounds or 0)),
        'consumed_thresholds': normalized_thresholds,
    }


def should_track_ceo_self_execution(context: Any) -> bool:
    return str(_context_value(context, 'actor_role', '') or '').strip().lower() == 'ceo'


def ensure_guard_state(context: Any) -> dict[str, Any] | None:
    if context is None or not should_track_ceo_self_execution(context):
        return None
    existing = _context_value(context, STATE_KEY, None)
    if isinstance(existing, dict):
        existing['completed_rounds'] = max(0, int(existing.get('completed_rounds', 0) or 0))
        thresholds = existing.get('consumed_thresholds')
        if not isinstance(thresholds, list):
            thresholds = []
        existing['consumed_thresholds'] = sorted(
            {max(0, int(item or 0)) for item in list(thresholds or []) if int(item or 0) > 0}
        )
        return existing
    state = build_guard_state()
    if _context_assign(context, STATE_KEY, state):
        return state
    return None


def maybe_build_intercept_message(context: Any, *, tool_name: str) -> str | None:
    normalized_tool_name = str(tool_name or '').strip()
    if not normalized_tool_name or normalized_tool_name == CREATE_ASYNC_TASK_TOOL_NAME:
        return None
    state = ensure_guard_state(context)
    if state is None:
        return None
    threshold = _trigger_threshold_for_completed_rounds(int(state.get('completed_rounds', 0) or 0))
    if threshold is None:
        return None
    consumed = {
        max(0, int(item or 0))
        for item in list(state.get('consumed_thresholds') or [])
        if int(item or 0) > 0
    }
    if threshold in consumed:
        return None
    consumed.add(threshold)
    state['consumed_thresholds'] = sorted(consumed)
    return REMINDER_TEMPLATE.format(threshold=threshold)


def record_completed_tool_round(context: Any, *, tool_name: str) -> None:
    normalized_tool_name = str(tool_name or '').strip()
    if not normalized_tool_name or normalized_tool_name == CREATE_ASYNC_TASK_TOOL_NAME:
        return None
    state = ensure_guard_state(context)
    if state is None:
        return None
    state['completed_rounds'] = max(0, int(state.get('completed_rounds', 0) or 0)) + 1
    return None


def _trigger_threshold_for_completed_rounds(completed_rounds: int) -> int | None:
    rounds = max(0, int(completed_rounds or 0))
    if rounds == 20:
        return 20
    if rounds == 50:
        return 50
    if rounds > 50 and (rounds - 50) % 20 == 0:
        return rounds
    return None


def _context_value(context: Any, key: str, default: Any = '') -> Any:
    if isinstance(context, dict):
        return context.get(key, default)
    return getattr(context, key, default)


def _context_assign(context: Any, key: str, value: Any) -> bool:
    if isinstance(context, dict):
        context[key] = value
        return True
    try:
        setattr(context, key, value)
    except Exception:
        return False
    return True
