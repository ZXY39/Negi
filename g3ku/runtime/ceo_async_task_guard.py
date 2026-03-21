from __future__ import annotations

import json
from typing import Any


CREATE_ASYNC_TASK_TOOL_NAME = 'create_async_task'
SPAWN_CHILD_NODES_TOOL_NAME = 'spawn_child_nodes'

GUARD_OVERLAY_MARKER = '[[G3KU_GUARD_OVERLAY_V1]]'


def overlay_threshold_for_iteration(iteration: int) -> int | None:
    completed_rounds = max(0, int(iteration or 0) - 1)
    return _trigger_threshold_for_completed_rounds(completed_rounds)


def maybe_build_ceo_overlay(*, iteration: int) -> str | None:
    threshold = overlay_threshold_for_iteration(iteration)
    if threshold is None:
        return None
    return _build_overlay(
        threshold=threshold,
        advisory_kind='ceo_async_task_overlay',
        recommended_tool=CREATE_ASYNC_TASK_TOOL_NAME,
    )


def maybe_build_execution_overlay(*, iteration: int, can_spawn_children: bool) -> str | None:
    if not bool(can_spawn_children):
        return None
    threshold = overlay_threshold_for_iteration(iteration)
    if threshold is None:
        return None
    return _build_overlay(
        threshold=threshold,
        advisory_kind='execution_spawn_child_overlay',
        recommended_tool=SPAWN_CHILD_NODES_TOOL_NAME,
    )


def _build_overlay(*, threshold: int, advisory_kind: str, recommended_tool: str) -> str:
    tool_name = str(recommended_tool or '').strip()
    text = (
        f'【注意：当前你已调用{int(threshold or 0)}轮工具，你必须立即评估是否 {tool_name} '
        '还是继续自主完成，若不选择分流，则忽略本次提醒】'
    )
    payload = {
        'status': 'advisory',
        'advisory_kind': str(advisory_kind or '').strip(),
        'threshold': max(0, int(threshold or 0)),
        'recommended_tool': tool_name,
        'allowed_next_actions': [tool_name, 'continue_self_execute'],
        'must_evaluate': True,
        'ignore_allowed': True,
        'text': text,
    }
    return '\n'.join(
        [
            text,
            GUARD_OVERLAY_MARKER,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ]
    )


def _trigger_threshold_for_completed_rounds(completed_rounds: int) -> int | None:
    rounds = max(0, int(completed_rounds or 0))
    if rounds == 20:
        return 20
    if rounds == 50:
        return 50
    if rounds > 50 and (rounds - 50) % 20 == 0:
        return rounds
    return None


def context_value(context: Any, key: str, default: Any = '') -> Any:
    if isinstance(context, dict):
        return context.get(key, default)
    return getattr(context, key, default)
