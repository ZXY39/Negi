"""Control-tool parameter errors must carry the contract the validator enforced.

`submit_final_result` and the other injected control tools have no loadable resource
descriptor, so `load_tool_context` is a dead end for them. The guidance therefore has to
name the required keys and their types itself - the incident this guards against
(node:abaf5c7d6e11, 2026-09-22) was a model omitting three required keys and being told
only to "recheck the parameters".
"""

from __future__ import annotations

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
