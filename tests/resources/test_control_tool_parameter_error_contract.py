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
from g3ku.agent.tools.main_runtime import _candidate_gate_error, _loadable_tool_gate_error
from g3ku.runtime.tool_error_guidance import (
    PARAMETER_ERROR_GUIDANCE_TEMPLATE,
    PARAMETER_RECHECK_GUIDANCE_TEMPLATE,
    append_parameter_error_guidance,
    availability_hint,
    no_load_needed_hint,
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


def test_acceptance_flag_does_not_suppress_the_content_open_pointer(tmp_path) -> None:
    """content ref 白名单没有任何执行侧实现读得到，不该再据此降级指针。

    旧实现担心"承诺一条会被白名单拒的读取路径"。实盘：该拒绝全库 1 次、来自 legacy
    `content`，而 legacy `content` 调用数为 0；split `content_*` 根本不查白名单。
    保留降级分支只会让验收节点稳定拿到较弱的那句。
    """
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

    assert path in guidance
    assert 'load_tool_context(tool_id=' not in guidance


def test_missing_toolskill_file_falls_back_to_the_load_pointer(tmp_path) -> None:
    tool, _path = _resource_tool(tmp_path, with_toolskill=False)

    guidance = parameter_error_guidance(
        'filesystem_write',
        tool=tool,
        runtime_context={'hydrated_executor_names': ['filesystem_write']},
    )

    assert guidance == PARAMETER_ERROR_GUIDANCE_TEMPLATE.format(tool_name='filesystem_write')


# --- 名称被拒：必须说清"什么能用" --------------------------------------------


def test_availability_hint_separates_not_hydrated_from_unknown_name() -> None:
    hint = availability_hint(
        requested='web_search',
        callable_names=['exec', 'content_open'],
        candidate_names=['web_search', 'image_search'],
    )

    # 候选未水化 → 给出加载动作；同时列出可调用与全部候选（含它自己，便于改选别的）。
    assert '「web_search」本轮只是候选、尚未水化' in hint
    assert '当前可直接调用的工具：exec、content_open' in hint
    assert '本轮可加载的候选工具：web_search、image_search' in hint


def test_availability_hint_for_unknown_name_lists_no_candidate_group() -> None:
    hint = availability_hint(requested='nope', callable_names=['exec'], candidate_names=[])

    assert '只是候选' not in hint
    assert '当前可直接调用的工具：exec' in hint


def test_availability_hint_caps_long_lists_but_reports_the_true_count() -> None:
    names = [f'tool_{index}' for index in range(45)]

    hint = availability_hint(requested='missing', callable_names=names)

    assert 'tool_19' in hint
    assert 'tool_20' not in hint
    assert '等 45 个' in hint


def test_no_load_needed_hint_answers_for_resident_builtin_tools() -> None:
    hint = no_load_needed_hint(requested='exec', actor_role='ceo')

    assert '「exec」是常驻内置工具，无需加载说明即可直接调用' in hint
    assert no_load_needed_hint(requested='some_resource_tool', actor_role='ceo') == ''
    assert no_load_needed_hint(requested='exec', actor_role='') == ''


def test_loadable_tool_gate_error_names_builtin_and_lists_candidates() -> None:
    runtime = {
        'tool_contract_enforced': True,
        'actor_role': 'ceo',
        'candidate_tool_names': ['agent_browser', 'web_search'],
        'rbac_visible_tool_names': ['content_search'],
    }

    text = _loadable_tool_gate_error(runtime=runtime, requested_id='exec')

    assert '是常驻内置工具' in text
    assert '本轮候选工具：agent_browser、web_search' in text
    assert 'RBAC 可见 surfaced tools：content_search' in text


def test_candidate_gate_error_enumerates_the_candidates_it_demands() -> None:
    runtime = {
        'tool_contract_enforced': True,
        'candidate_skill_ids': ['skill-creator', 'pdf-tools'],
    }

    text = _candidate_gate_error(
        runtime=runtime,
        field_name='candidate_skill_ids',
        requested_id='nope',
        label='技能',
    )

    assert '本轮候选技能：skill-creator、pdf-tools' in text


# --- 空提交：区分"没写参数"与"参数没解析成功" ---------------------------------


def test_empty_submission_is_called_out_next_to_the_inlined_contract() -> None:
    tool = _submit_tool()

    guidance = parameter_error_guidance(tool.name, tool=tool, arguments={})

    assert '本次调用未携带任何参数' in guidance
    assert '参数 JSON 没有被成功解析' in guidance
    assert CONTRACT_PREFIX in guidance


def test_empty_submission_note_stays_off_the_pointer_branches() -> None:
    """指针分支下面没有内联结构，不能出现"按下方结构"这种指错地方的话。"""
    tool = _submit_tool()
    tool._descriptor = object()  # type: ignore[attr-defined]

    guidance = parameter_error_guidance('submit_final_result', tool=tool, arguments={})

    assert '本次调用未携带任何参数' not in guidance
