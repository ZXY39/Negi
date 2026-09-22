"""Control-tool parameter errors must carry the contract the validator enforced.

`submit_final_result` and the other injected control tools have no loadable resource
descriptor, so `load_tool_context` is a dead end for them. The guidance therefore has to
name the required keys and their types itself - the incident this guards against
(node:abaf5c7d6e11, 2026-09-22) was a model omitting three required keys and being told
only to "recheck the parameters".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.runtime.tool_error_guidance import (
    PARAMETER_ERROR_GUIDANCE_TEMPLATE,
    PARAMETER_RECHECK_GUIDANCE_TEMPLATE,
    append_parameter_error_guidance,
    parameter_error_guidance,
)
from main.runtime.internal_tools import SubmitFinalResultTool

CONTRACT_PREFIX = '该工具没有可加载的扩展说明，参数契约如下（必填项及其类型与取值结构）：'


class _SchemaTool(Tool):
    """Minimal no-descriptor control tool with an authoritatively declared schema."""

    def __init__(self, *, name: str, schema: dict[str, Any]) -> None:
        self._name = name
        self._schema = schema

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return self._schema

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return 'ok'


def _submit_tool(*, node_kind: str = 'execution') -> SubmitFinalResultTool:
    async def _submit(payload: dict[str, Any]) -> dict[str, Any]:
        return dict(payload)

    return SubmitFinalResultTool(_submit, node_kind=node_kind)


def test_contract_guidance_names_every_required_key_with_type_and_enum() -> None:
    tool = _submit_tool()
    guidance = parameter_error_guidance(tool.name, tool=tool)

    assert guidance.startswith(CONTRACT_PREFIX)
    required = tool.parameters['required']
    assert isinstance(required, list)
    # The render must not silently drop a single required key.
    for name in required:
        assert f'{name}=' in guidance
    assert 'status=string(success|failed)' in guidance
    assert 'delivery_status=string(final|blocked)' in guidance
    assert 'summary=string(非空)' in guidance
    assert 'remaining_work=array<string>' in guidance


def test_contract_guidance_shows_evidence_is_an_object_array_not_a_string() -> None:
    guidance = parameter_error_guidance('submit_final_result', tool=_submit_tool())

    assert 'evidence=array<object{' in guidance
    assert '必填:kind=string(file|artifact|url)' in guidance
    assert 'path' in guidance and 'ref' in guidance and 'note' in guidance


def test_contract_guidance_keeps_the_load_tool_context_lane_for_resource_tools() -> None:
    tool = _submit_tool()
    tool._descriptor = object()  # type: ignore[attr-defined]

    guidance = parameter_error_guidance(tool.name, tool=tool)

    assert guidance == PARAMETER_ERROR_GUIDANCE_TEMPLATE.format(tool_name=tool.name)
    assert CONTRACT_PREFIX not in guidance


def test_guidance_falls_back_when_nothing_is_required() -> None:
    tool = _SchemaTool(
        name='no_required',
        schema={'type': 'object', 'properties': {'value': {'type': 'string'}}},
    )

    assert parameter_error_guidance('no_required', tool=tool) == PARAMETER_RECHECK_GUIDANCE_TEMPLATE


def test_guidance_falls_back_on_unrenderable_schema_without_raising() -> None:
    broken = _SchemaTool(name='broken', schema={'type': 'object', 'properties': 'not-a-dict'})  # type: ignore[dict-item]
    assert parameter_error_guidance('broken', tool=broken) == PARAMETER_RECHECK_GUIDANCE_TEMPLATE
    assert parameter_error_guidance('no_params', tool=object()) == PARAMETER_RECHECK_GUIDANCE_TEMPLATE
    assert parameter_error_guidance('missing_tool', tool=None) == PARAMETER_ERROR_GUIDANCE_TEMPLATE.format(
        tool_name='missing_tool'
    )


def test_guidance_stays_bounded_and_degrades_to_the_generic_recheck() -> None:
    properties = {f'key_{index}': {'type': 'string'} for index in range(200)}
    tool = _SchemaTool(
        name='huge',
        schema={'type': 'object', 'properties': properties, 'required': sorted(properties)},
    )

    guidance = parameter_error_guidance('huge', tool=tool)

    assert len(guidance) < 900
    assert guidance == PARAMETER_RECHECK_GUIDANCE_TEMPLATE


def test_append_guidance_is_idempotent_and_keeps_the_original_error_first() -> None:
    tool = _submit_tool()
    once = append_parameter_error_guidance('Error: missing required status', tool_name=tool.name, tool=tool)
    twice = append_parameter_error_guidance(once, tool_name=tool.name, tool=tool)

    assert once.startswith('Error: missing required status')
    assert CONTRACT_PREFIX in once
    assert once == twice


# --- ②：未识别参数名的因果提示（校验器静默放过多余键）-------------------------


def test_unrecognized_key_is_named_with_a_near_miss_suggestion() -> None:
    tool = _SchemaTool(
        name='demo',
        schema={
            'type': 'object',
            'properties': {'value': {'type': 'string'}, 'mode': {'type': 'string'}},
            'required': ['value'],
        },
    )

    guidance = parameter_error_guidance('demo', tool=tool, arguments={'valu': 'x'})

    assert '本次提交中该工具不接受这些参数名：valu→value?' in guidance


def test_unrecognized_key_without_a_close_match_is_still_named() -> None:
    tool = _SchemaTool(
        name='demo',
        schema={'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value']},
    )

    guidance = parameter_error_guidance('demo', tool=tool, arguments={'zzz': 1})

    assert '本次提交中该工具不接受这些参数名：zzz' in guidance
    assert '→' not in guidance


def test_no_unrecognized_hint_when_every_submitted_key_is_defined() -> None:
    tool = _SchemaTool(
        name='demo',
        schema={'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value']},
    )

    guidance = parameter_error_guidance('demo', tool=tool, arguments={'value': 'x'})

    assert '不接受这些参数名' not in guidance


def test_control_tool_error_carries_both_cause_and_contract() -> None:
    """事故形态：缺 envelope 字段 + evidence 写成串 → 原因在前、契约在后。"""
    tool = _submit_tool()
    text = append_parameter_error_guidance(
        'Error: missing required status',
        tool_name=tool.name,
        tool=tool,
        arguments={'answer': 'a', 'evidence': '[]', 'anwer': 'typo'},
    )

    assert 'anwer→answer?' in text
    assert CONTRACT_PREFIX in text
    assert text.index('anwer→answer?') < text.index(CONTRACT_PREFIX)


# --- ①：说明文档指针三分支 --------------------------------------------------


def _resource_tool(tmp_path, *, with_toolskill: bool):
    tool = _SchemaTool(
        name='filesystem_write',
        schema={'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']},
    )
    path = None
    if with_toolskill:
        path = tmp_path / 'toolskill.md'
        path.write_text('# contract', encoding='utf-8')
    tool._descriptor = SimpleNamespace(toolskill_main_path=path, toolskills_main_path=path)  # type: ignore[attr-defined]
    return tool, str(path or '')


def test_hydrated_resource_tool_is_pointed_at_content_open_not_load_tool_context(tmp_path) -> None:
    tool, path = _resource_tool(tmp_path, with_toolskill=True)

    guidance = parameter_error_guidance(
        'filesystem_write',
        tool=tool,
        runtime_context={'hydrated_executor_names': ['filesystem_write']},
    )

    assert 'content_open' in guidance
    assert path in guidance
    assert 'load_tool_context(tool_id=' not in guidance


def test_not_yet_hydrated_resource_tool_keeps_the_load_pointer(tmp_path) -> None:
    tool, _path = _resource_tool(tmp_path, with_toolskill=True)

    guidance = parameter_error_guidance(
        'filesystem_write',
        tool=tool,
        runtime_context={'hydrated_executor_names': []},
    )

    assert guidance == PARAMETER_ERROR_GUIDANCE_TEMPLATE.format(tool_name='filesystem_write')


def test_acceptance_content_ref_allowlist_suppresses_the_content_open_pointer(tmp_path) -> None:
    """验收节点白名单下不承诺外开路径，否则指针自己会被闸门拒。"""
    tool, path = _resource_tool(tmp_path, with_toolskill=True)

    guidance = parameter_error_guidance(
        'filesystem_write',
        tool=tool,
        runtime_context={
            'hydrated_executor_names': ['filesystem_write'],
            'enforce_content_ref_allowlist': True,
            'allowed_content_refs': [],
        },
    )

    assert path not in guidance
    assert guidance == PARAMETER_ERROR_GUIDANCE_TEMPLATE.format(tool_name='filesystem_write')


def test_missing_toolskill_file_falls_back_to_the_load_pointer(tmp_path) -> None:
    tool, _path = _resource_tool(tmp_path, with_toolskill=False)

    guidance = parameter_error_guidance(
        'filesystem_write',
        tool=tool,
        runtime_context={'hydrated_executor_names': ['filesystem_write']},
    )

    assert guidance == PARAMETER_ERROR_GUIDANCE_TEMPLATE.format(tool_name='filesystem_write')
