"""P1：`silent` 作为前门常驻内置控制工具的曝光合同。

只验"能不能被调到 + 会不会漏到节点侧"，不验静默是否生效（那是 P2）。
这组断言存在的理由很具体：旧的文案静默出口在 160,675 条 assistant 消息里成功过
0 次、失败 11 次，全部写成「正文 + 空行 + `[G3KU_SILENT]`」。工具调用是与正文
彼此独立的通道，才能同时表达"这段留下"和"这条别发"。

其中第 4 条（节点侧不可见）是硬边界：静默只对面向用户的前门有意义，节点从不
直接对用户说话；把工具注册进 `loop.tools` 是全局的，所以必须正面钉住它不外溢。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from g3ku.agent.tools.registry import ToolRegistry
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.tool_visibility import NODE_FIXED_BUILTIN_TOOL_NAMES
from main.runtime.internal_tools import SilentTool
from main.runtime.stage_budget import (
    DEFAULT_NON_BUDGET_STAGE_TOOLS,
    DEFAULT_STAGE_GATE_BYPASS_TOOLS,
    SILENT_TOOL_NAME,
    STAGE_TOOL_NAME,
)


def _ops(*, registered: bool = True) -> CeoFrontDoorRuntimeOps:
    """只带本组断言所需依赖的实例（绕过完整 __init__）。"""
    registry = ToolRegistry()
    if registered:
        registry.register(SilentTool())
    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)
    ops._loop = SimpleNamespace(tools=registry, app_config=None)
    return ops


def _names(ops: CeoFrontDoorRuntimeOps, state: dict) -> list[str]:
    callable_names = ops._frontdoor_callable_tool_names_for_state(state)
    visible_names = ops._frontdoor_runtime_visible_tool_names_for_state(state)
    return [*callable_names, *visible_names]


def test_message_builder_injects_silent_without_visibility_precondition() -> None:
    """与 RESERVED_INTERNAL_TOOLS 的分水岭：那个要求名字已经在 visible 集合里，
    所以照它加根本进不了 schema。silent 不依赖曝光层。"""
    names = CeoMessageBuilder._callable_tool_names(
        visible_tool_names=['create_async_task', 'exec'],
        hydrated_tool_names=None,
    )
    assert SILENT_TOOL_NAME in names
    assert 'stop_tool_execution' not in names  # 未声明族 ⇒ RESERVED 不具备注入能力


def test_silent_survives_every_stage_and_lane_state() -> None:
    ops = _ops()
    states = [
        {'tool_names': ['exec'], 'provider_tool_names': ['exec']},
        {'tool_names': [], 'provider_tool_names': []},
        {'tool_names': ['exec'], 'heartbeat_internal': True},
        {'tool_names': ['exec'], 'cron_internal': True},
        {'tool_names': [STAGE_TOOL_NAME], 'frontdoor_stage_state': {'active_stage_id': 's1'}},
        {
            'tool_names': [STAGE_TOOL_NAME],
            'frontdoor_stage_state': {'active_stage_id': 's1', 'transition_required': True},
        },
    ]
    for index, state in enumerate(states):
        names = _names(ops, state)
        assert SILENT_TOOL_NAME in names, f'state #{index} lost silent: {names}'


def test_silent_reaches_provider_schema_once_registered() -> None:
    ops = _ops(registered=True)
    schemas = ops._selected_tool_schemas([SILENT_TOOL_NAME])
    exposed = {str((item.get('function') or {}).get('name') or '') for item in schemas}
    assert exposed == {SILENT_TOOL_NAME}


def test_unregistered_silent_is_dropped_rather_than_emitted_broken() -> None:
    """注册缺失时必须整条消失，不能留下有名字无可执行体的 schema。"""
    ops = _ops(registered=False)
    assert ops._selected_tool_schemas([SILENT_TOOL_NAME]) == []


def test_silent_never_reaches_node_prompts() -> None:
    assert SILENT_TOOL_NAME not in set(NODE_FIXED_BUILTIN_TOOL_NAMES)
    assert SILENT_TOOL_NAME not in set(CeoMessageBuilder.FIXED_BUILTIN_TOOL_NAMES)


def test_silent_is_gate_bypassed_and_budget_free() -> None:
    assert SILENT_TOOL_NAME in DEFAULT_STAGE_GATE_BYPASS_TOOLS
    assert SILENT_TOOL_NAME in DEFAULT_NON_BUDGET_STAGE_TOOLS


def test_reason_is_required_and_carries_the_audit_trail() -> None:
    tool = SilentTool()
    assert tool.validate_params({'reason': '   '})
    assert tool.validate_params({})
    assert tool.validate_params({'reason': '已在 17:49 汇报过'}) == []
    payload = json.loads(
        asyncio.run(
            tool.execute(reason='已在 17:49 汇报过', subject='task:543e0f15d798', superseded_by='task:9771d6c5469d')
        )
    )
    assert payload == {
        'silenced': True,
        'reason': '已在 17:49 汇报过',
        'subject': 'task:543e0f15d798',
        'superseded_by': 'task:9771d6c5469d',
    }


def test_silent_schema_stays_cheap_in_the_stable_prefix() -> None:
    """常驻 schema 的成本论证：设计期实测 32 个工具合计 12,406 字符，加这条
    约 +0.2%。这条断言防的是日后往描述里堆散文把它撑大。"""
    assert len(json.dumps(SilentTool().parameters, ensure_ascii=False)) < 800
