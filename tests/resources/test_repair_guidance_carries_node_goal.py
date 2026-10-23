from __future__ import annotations

import json
from types import SimpleNamespace

from main.runtime.react_loop import (
    ReActToolLoop,
    _node_contract_echo_repair_message,
    _node_goal_anchor_text,
)

GOAL = '把周报写进 reports/week.md 并核对数字'
ANCHOR = '这一节点的交付目标：' + GOAL + '。'


def _repeat_call() -> SimpleNamespace:
    return SimpleNamespace(name='task_progress', arguments={'任务id': 'task:discipline'})


def _signature(call: SimpleNamespace) -> str:
    return call.name + ':' + json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)


def _violations(*, node_goal: str = '') -> list[dict[str, object]]:
    call = _repeat_call()
    kwargs = {
        'response_tool_calls': [call],
        'task_id': 'task:discipline',
        'node_kind': 'execution',
        'message_history': [],
        'prior_violation_counts': {_signature(call): 1},
    }
    if node_goal:
        kwargs['node_goal'] = node_goal
    return ReActToolLoop._read_only_repeat_violations(**kwargs)


def test_contract_echo_repair_message_names_the_node_goal() -> None:
    text = _node_contract_echo_repair_message(SimpleNamespace(goal=GOAL, prompt=''))

    assert 'Runtime Tool Contract' in text
    assert ANCHOR in text


def test_contract_echo_repair_message_without_goal_adds_no_anchor() -> None:
    text = _node_contract_echo_repair_message(SimpleNamespace(goal='', prompt=''))

    assert 'Runtime Tool Contract' in text
    assert ANCHOR not in text


def test_goal_anchor_falls_back_to_the_node_prompt() -> None:
    assert _node_goal_anchor_text(SimpleNamespace(goal='', prompt=GOAL)) == ANCHOR


def test_repeat_repair_text_ends_with_the_node_goal() -> None:
    violations = _violations(node_goal=_node_goal_anchor_text(SimpleNamespace(goal=GOAL)))

    assert violations
    for violation in violations:
        assert violation['repair_text'].endswith(ANCHOR)


def test_repeat_repair_text_is_unchanged_without_a_goal() -> None:
    violations = _violations()

    assert violations
    for violation in violations:
        assert ANCHOR not in violation['repair_text']
