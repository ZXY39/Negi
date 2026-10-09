"""The model-visible projection may not hide a boundary the validator still enforces.

Node control tools hand-write `model_parameters` as a trimmed view of the authoritative
`parameters`, but `Tool.validate_params()` keeps checking the authoritative one. On
2026-09-27 node:fed1c7e2f9c5 submitted `start_line: 0` / `end_line: 0` on `kind=url`
evidence - legal under the schema it was shown, rejected by `minimum: 1` it was never
shown - and burned an invalid-final-submission strike on it.

Mirror the constraints, not the prose: the provider normalizer strips every `description`
inside `parameters`, so a field description written for the model is dead text, while
`minimum` / `minLength` / `minItems` / `enum` / `required` survive to the wire.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import pytest

from g3ku.json_schema_utils import normalize_openai_tool_definition
from main.runtime.internal_tools import (
    SilentTool,
    SpawnChildNodesTool,
    SubmitFinalResultTool,
    SubmitMessageDistributionTool,
    SubmitNextStageTool,
    SubmitNoticeInspectionDecisionTool,
)


async def _callback(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {}


def _constraint_map(node: Any, path: str = '') -> dict[str, Any]:
    """Every schema keyword that can reject a value, keyed by field path."""

    collected: dict[str, Any] = {}
    if not isinstance(node, dict):
        return collected
    for key, value in node.items():
        if key in {'description', 'properties', 'items'}:
            continue
        collected[f'{path or "<root>"}#{key}'] = value
    properties = node.get('properties')
    if isinstance(properties, dict):
        for name, sub in properties.items():
            collected.update(_constraint_map(sub, f'{path}.{name}' if path else name))
    items = node.get('items')
    if isinstance(items, dict):
        collected.update(_constraint_map(items, f'{path}[]'))
    return collected


def _description_keys(node: Any, path: str = '') -> list[str]:
    found: list[str] = []
    if not isinstance(node, dict):
        return found
    if 'description' in node:
        found.append(f'{path or "<root>"}#description')
    properties = node.get('properties')
    if isinstance(properties, dict):
        for name, sub in properties.items():
            found.extend(_description_keys(sub, f'{path}.{name}' if path else name))
    items = node.get('items')
    if isinstance(items, dict):
        found.extend(_description_keys(items, f'{path}[]'))
    return found


CONTROL_TOOLS: list[tuple[str, Callable[[], Any]]] = [
    ('submit_next_stage', lambda: SubmitNextStageTool(_callback)),
    ('silent', lambda: SilentTool()),
    ('spawn_child_nodes', lambda: SpawnChildNodesTool(_callback)),
    ('submit_final_result(execution)', lambda: SubmitFinalResultTool(_callback, node_kind='execution')),
    ('submit_final_result(acceptance)', lambda: SubmitFinalResultTool(_callback, node_kind='acceptance')),
    ('submit_message_distribution', lambda: SubmitMessageDistributionTool(_callback)),
    ('submit_notice_inspection_decision', lambda: SubmitNoticeInspectionDecisionTool(_callback)),
]


@pytest.mark.parametrize(('label', 'build'), CONTROL_TOOLS, ids=[name for name, _ in CONTROL_TOOLS])
def test_model_visible_schema_keeps_every_validated_constraint(label: str, build: Callable[[], Any]) -> None:
    tool = build()
    authoritative = _constraint_map(tool.parameters)
    model_visible = _constraint_map(tool.model_parameters)

    dropped = sorted(key for key in authoritative if key not in model_visible)
    weakened = sorted(
        f'{key}: {value!r} -> {model_visible[key]!r}'
        for key, value in authoritative.items()
        if key in model_visible and model_visible[key] != value
    )
    assert not dropped, f'{label} hides constraints the validator still enforces: {dropped}'
    assert not weakened, f'{label} restates constraints differently: {weakened}'


@pytest.mark.parametrize(('label', 'build'), CONTROL_TOOLS, ids=[name for name, _ in CONTROL_TOOLS])
def test_wire_schema_carries_constraints_and_no_field_prose(label: str, build: Callable[[], Any]) -> None:
    """Pins the premise behind mirroring: constraints reach the model, field prose does not."""

    tool = build()
    function = normalize_openai_tool_definition(tool.to_model_schema())['function']
    parameters = function['parameters']

    assert str(function.get('description') or '').strip(), f'{label} lost its tool-level description'
    assert _constraint_map(tool.model_parameters) == _constraint_map(parameters), (
        f'{label} constraints were rewritten on the way to the provider'
    )
    assert not _description_keys(parameters), (
        f'{label} carries field descriptions that the provider strips anyway: '
        f'{json.dumps(_description_keys(parameters))}'
    )


def test_stage_tool_model_description_states_that_eviction_is_permanent() -> None:
    """裁撤语义只能写在工具级 `model_description` 上：字段级 description 出不了 provider。

    本文件上面两条用例钉住了这件事——`parameters` 里那段逐参数文案会被 normalizer 整段剥掉，
    所以把它改得再详细也不会到模型眼前。能到模型的只有 `model_description` 与三份提示词，
    少了"此后所有回合都不再出现 + 用 content_open 按 archive_ref 读回"这两句，模型就会按参数名
    读成"只躲过下一阶段"，进而把它其实还要逐字引用的原文裁掉。
    """
    tool = SubmitNextStageTool(_callback)
    text = str(tool.model_description or '')
    assert 'every later turn' in text
    assert 'content_open' in text
    assert 'archive_ref' in text
    assert len(text) < len(str(tool.description or '')), '模型面必须短于权威面（既有约束）'
