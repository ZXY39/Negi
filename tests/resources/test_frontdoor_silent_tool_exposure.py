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
from pathlib import Path
from types import SimpleNamespace

from g3ku.agent.tools.registry import ToolRegistry
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.state_models import initial_persistent_state
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


def _contract(callable_names: list[str]) -> str:
    from g3ku.runtime.frontdoor.tool_contract import _render_frontdoor_contract_summary

    return _render_frontdoor_contract_summary(
        {'callable_tool_names': callable_names, 'hydrated_tool_names': [], 'candidate_skill_ids': [], 'candidate_tools': []}
    )


def test_visible_turns_are_told_the_silent_tool_is_the_only_exit() -> None:
    """B：心跳车道那份措辞覆盖不到普通用户回合 —— 23:25 那轮模型正是按上下文里残留的
    旧契约去输出文本哨兵，所以可见回合必须自己说一句"静默没有文本写法"。"""
    rendered = _contract(['exec', SILENT_TOOL_NAME])
    help_line = next((line for line in rendered.splitlines() if line.startswith('silent_help:')), '')
    assert f'`{SILENT_TOOL_NAME}(reason=' in help_line
    assert 'There is no text form of silence' in help_line
    # 措辞挂在每轮重渲染的契约里，不新起一层：实测该契约 6,614 字符，这条 +200。
    assert len(help_line) < 300


def test_silent_help_follows_the_callable_list() -> None:
    """工具没注册时不能继续叫模型去调它 —— 与 P1 的"未注册则整条消失"同一条边界。"""
    assert 'silent_help:' not in _contract(['exec'])


def test_execution_bundle_resolves_the_silent_tool_object() -> None:
    """01:17 实盘：模型确实调了 `silent`，却收到 `Error: tool not available: silent`。

    P1 只把名字注进三个名单，而执行侧查的是 `_frontdoor_execution_bundle` 组装的
    **工具对象字典** —— 那里历史上只塞过 `submit_next_stage`。名字可见 ≠ 对象可解析，
    这条就是钉住第四处注入点（与 submit_next_stage 同一处、同样不依赖注册表）。
    """
    ops = _ops(registered=False)
    ops._build_tool_runtime_context = lambda **kwargs: {}
    state = dict(initial_persistent_state(user_input={'content': 'x', 'metadata': {}}))
    bundle = ops._frontdoor_execution_bundle(state=state, runtime=SimpleNamespace(context=SimpleNamespace()))
    assert SILENT_TOOL_NAME in bundle.visible_tools
    assert bundle.visible_tools[SILENT_TOOL_NAME].name == SILENT_TOOL_NAME


def test_no_model_facing_surface_still_names_the_deleted_sentinel() -> None:
    """旧哨兵的字面串一旦重新进入任何逐字送达模型的面，模型就会继续用它 —— 而识别已删。

    23:25 与 00:34 两次实盘都是这么来的：一次是压缩块里的旧措辞，一次是
    `ceo_frontdoor.md` 这条每轮注入的系统提示从没被改写成工具。这里只钉"逐字渲染"的
    两处（提示词文件与契约渲染器）；代码里的其余命中都在注释/文档串里，记录的是
    "为什么删"，不进上下文。
    """
    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in sorted((root / 'g3ku' / 'runtime' / 'prompts').glob('*.md')):
        if 'G3KU_SILENT' in path.read_text(encoding='utf-8'):
            offenders.append(str(path.relative_to(root)))
    contract = root / 'g3ku' / 'runtime' / 'frontdoor' / 'tool_contract.py'
    if 'G3KU_SILENT' in contract.read_text(encoding='utf-8'):
        offenders.append(str(contract.relative_to(root)))
    assert offenders == []
