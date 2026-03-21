from __future__ import annotations

import json

from g3ku.runtime.ceo_async_task_guard import (
    GUARD_OVERLAY_MARKER,
    maybe_build_ceo_overlay,
    maybe_build_execution_overlay,
    overlay_threshold_for_iteration,
)


def _parse_overlay_payload(message: str) -> dict[str, object]:
    text = str(message or '')
    marker = f'{GUARD_OVERLAY_MARKER}\n'
    assert marker in text
    payload_text = text.split(marker, 1)[1].strip()
    return json.loads(payload_text)


def test_overlay_threshold_for_iteration_matches_expected_schedule() -> None:
    assert overlay_threshold_for_iteration(1) is None
    assert overlay_threshold_for_iteration(20) is None
    assert overlay_threshold_for_iteration(21) == 20
    assert overlay_threshold_for_iteration(51) == 50
    assert overlay_threshold_for_iteration(71) == 70
    assert overlay_threshold_for_iteration(91) == 90


def test_ceo_overlay_uses_strong_text_and_structured_payload() -> None:
    overlay = maybe_build_ceo_overlay(iteration=21)

    assert overlay is not None
    assert '【注意：当前你已调用20轮工具，你必须立即评估是否 create_async_task 还是继续自主完成，若不选择分流，则忽略本次提醒】' in overlay
    payload = _parse_overlay_payload(overlay)
    assert payload['status'] == 'advisory'
    assert payload['advisory_kind'] == 'ceo_async_task_overlay'
    assert payload['threshold'] == 20
    assert payload['recommended_tool'] == 'create_async_task'
    assert payload['allowed_next_actions'] == ['create_async_task', 'continue_self_execute']
    assert payload['must_evaluate'] is True
    assert payload['ignore_allowed'] is True


def test_execution_overlay_only_exists_when_node_can_spawn_children() -> None:
    assert maybe_build_execution_overlay(iteration=21, can_spawn_children=False) is None

    overlay = maybe_build_execution_overlay(iteration=21, can_spawn_children=True)

    assert overlay is not None
    assert '【注意：当前你已调用20轮工具，你必须立即评估是否 spawn_child_nodes 还是继续自主完成，若不选择分流，则忽略本次提醒】' in overlay
    payload = _parse_overlay_payload(overlay)
    assert payload['advisory_kind'] == 'execution_spawn_child_overlay'
    assert payload['threshold'] == 20
    assert payload['recommended_tool'] == 'spawn_child_nodes'
    assert payload['allowed_next_actions'] == ['spawn_child_nodes', 'continue_self_execute']


def test_ceo_overlay_repeats_at_70th_completed_round() -> None:
    overlay = maybe_build_ceo_overlay(iteration=71)

    assert overlay is not None
    assert '当前你已调用70轮工具' in overlay
    assert _parse_overlay_payload(overlay)['threshold'] == 70
