"""`[RUNTIME_NOTICE]` 待修复一节的行形状，与 `tools[]` 单源化的默认档。

两个渲染器（前门 `_render_repair_notice` / 节点 `_node_contract_notice_sections`）共用
`format_repair_notice_row`：这一节是模型在一次会话里唯一稳定看得见"哪些名字待修复"的地方，
两条道各渲各的形状，同一个故障在 CEO 会话里能说清原因、在执行节点里就只剩一个名字。
skill 那一档过去正是只剩名字——`description_too_long` 这类原因由装载路径记进 payload 的
`reason`，渲染器不照抄就等于把它丢在字段里。

单源开关那两条钉的是默认值本身：改描述不该顶掉缓存命中，靠的就是参数表只从钉住整表取字节。
"""

from __future__ import annotations

from typing import Any

import pytest

from g3ku.runtime.tool_error_guidance import format_repair_notice_row
from g3ku.utils.helpers import TOOLS_BUNDLE_SINGLE_SOURCE_ENV, tools_bundle_single_source_enabled

_TOOL_ROW = {'tool_id': 'stuck_tool', 'reason': 'description_too_long: limit 160'}
_SKILL_ROW = {'skill_id': 'tmux', 'reason': 'description_too_long: skill description is 211 characters'}
_EXPECTED_ROW = '`stuck_tool` (description_too_long: limit 160); skill `tmux` (description_too_long: skill description is 211 characters)'


def _frontdoor_notice(*, tools: list[Any], skills: list[Any]) -> str:
    from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps

    return CeoFrontDoorRuntimeOps._render_repair_notice(tools, skills)


def _node_notice(*, tools: list[Any], skills: list[Any]) -> str:
    from main.runtime.react_loop import ReActToolLoop

    loop = object.__new__(ReActToolLoop)
    sections = loop._node_contract_notice_sections(
        payload={'repair_required_tools': tools, 'repair_required_skills': skills},
        tool_schema_selection={'provider_tool_names': ['submit_final_result'], 'declared_denied_tool_names': []},
        runtime_context={'declared_denied_tool_names': []},
        exec_runtime_policy=None,
        task_temp_dir='',
    )
    return str(sections.get('repair_required') or '')


@pytest.mark.parametrize('lane', ('frontdoor', 'node'))
def test_repair_notice_row_carries_the_recorded_reason_for_skills(lane: str) -> None:
    """工具行与 skill 行同形，都带装载记录的原因。"""
    render = _frontdoor_notice if lane == 'frontdoor' else _node_notice
    text = render(tools=[dict(_TOOL_ROW)], skills=[dict(_SKILL_ROW)])
    assert text == _EXPECTED_ROW, lane


@pytest.mark.parametrize('lane', ('frontdoor', 'node'))
def test_both_lanes_render_the_same_row_for_the_same_input(lane: str) -> None:
    """同一份 payload 在两条道上产出同一行——共用一个行渲染器是这条不变量的实现。"""
    row = {'tool_id': 'a_tool', 'reason': 'tool_handler_unavailable'}
    skill = {'skill_id': 'a_skill', 'reason': 'manifest missing'}
    assert _frontdoor_notice(tools=[row], skills=[skill]) == _node_notice(tools=[row], skills=[skill])


def test_repair_notice_row_omits_parentheses_when_no_reason_is_recorded() -> None:
    assert format_repair_notice_row('skill `x`', '') == 'skill `x`'
    assert format_repair_notice_row('', 'reason') == ''
    # 旧形状（光名字）仍要渲得出来：payload 里混进字符串不该整节报错
    assert _frontdoor_notice(tools=[], skills=['bare_skill']) == 'skill `bare_skill`'


def test_pinned_tool_bundle_single_source_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认开：改 `resource.yaml` 的描述不在冻结跳改字节，因此不顶掉缓存命中。

    正样本——把判据写成"只有显式 1 才开"，这条会在 unset 时拿到 False 当场红。
    """
    monkeypatch.delenv(TOOLS_BUNDLE_SINGLE_SOURCE_ENV, raising=False)
    assert tools_bundle_single_source_enabled() is True


@pytest.mark.parametrize('value', ['0', 'false', 'no', 'off', 'FALSE', ' Off '])
def test_single_source_stays_reachable_as_a_retirement_lane(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(TOOLS_BUNDLE_SINGLE_SOURCE_ENV, value)
    assert tools_bundle_single_source_enabled() is False


@pytest.mark.parametrize('value', ['1', 'true', 'yes', 'on'])
def test_single_source_explicit_on(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(TOOLS_BUNDLE_SINGLE_SOURCE_ENV, value)
    assert tools_bundle_single_source_enabled() is True
