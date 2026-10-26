"""节点车道：`time` / `schedule_wake` 免 load 常驻，唤醒落在耐久表里而不是协程里。

这里钉的三件事各有一个实盘成因（task:4555863a6a49 的报时节点）：

1. 两颗工具对执行节点与验收节点都要在场，且不依赖注册表/RBAC——节点不会先去 load 一颗
   时钟，漏装对象就是"名字可见却调不动"（`silent` 那条四处注入的教训）。
2. 唤醒时刻必须落库：`exec Start-Sleep 5400` 被 09:02 的重启打断后，模型重新发了一次
   完整时长的睡眠，10:00 的到点被推成 10:33。
3. `wake_timer` 只能是运行时报的档位：模型能伪造它就等于能伪造"定时器响了"。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from g3ku.runtime.wake_clock import iso_from_ms, now_ms
from main.models import NodeRecord
from main.runtime.internal_tools import ScheduleWakeTool, TimeTool
from main.runtime.node_message_kind import (
    KIND_WAKE_TIMER,
    SYSTEM_KINDS,
    derive_message_kind,
    derive_priority,
)
from main.runtime.node_message_text import render_node_message_text, strip_node_message_prefix
from main.runtime.node_runner import NodeRunner
from main.runtime.tool_names import SCHEDULE_WAKE_TOOL_NAME, TIME_TOOL_NAME

TASK = 'task:wake'
ROOT = 'node:root'
PEER = 'node:peer'


def _runner(*, store, tool_supplier=None) -> NodeRunner:
    return NodeRunner(
        store=store,
        log_service=SimpleNamespace(update_frame=lambda *a, **k: None),
        react_loop=None,
        tool_provider=tool_supplier or (lambda node: {}),
        execution_model_refs=['execution_model'],
        acceptance_model_refs=['acceptance_model'],
    )


class _Store:
    """只带本组断言要用的方法的最小 store。"""

    def __init__(self, node: NodeRecord):
        self.node = node
        self.timer: dict | None = None
        self.upserts = 0

    def get_node(self, node_id: str):
        return self.node if node_id == self.node.node_id else None

    def upsert_task_node_timer(self, **payload):
        self.upserts += 1
        self.timer = dict(payload)
        return SimpleNamespace(wake_at_ms=int(payload['wake_at_ms']), note=str(payload['note']))


def _node(*, status: str = 'in_progress', kind: str = 'execution') -> NodeRecord:
    return NodeRecord(
        node_id=PEER,
        task_id=TASK,
        root_node_id=ROOT,
        node_kind=kind,
        status=status,
        goal='g',
        prompt='p',
        created_at='2026-10-26T08:00:00+08:00',
        updated_at='2026-10-26T08:00:00+08:00',
    )


class TestResidentRegistration:
    @pytest.mark.parametrize('kind', ['execution', 'acceptance'])
    def test_both_tools_are_present_without_any_registry_entry(self, kind):
        """`tool_provider` 交回空字典 ⇒ 两条名字仍在场：常驻不是"注册表里有"。"""
        runner = _runner(store=_Store(_node(kind=kind)))
        tools = runner._build_tools(
            task=SimpleNamespace(task_id=TASK, session_id='web:shared', metadata={}),
            node=_node(kind=kind),
        )
        assert TIME_TOOL_NAME in tools, f'{kind} node lost the clock'
        assert SCHEDULE_WAKE_TOOL_NAME in tools, f'{kind} node lost its wake-up'
        assert tools[TIME_TOOL_NAME].name == TIME_TOOL_NAME
        assert tools[SCHEDULE_WAKE_TOOL_NAME].name == SCHEDULE_WAKE_TOOL_NAME

    def test_tool_names_are_not_fixed_builtin_or_always_callable_slots(self):
        """名字不进 `NODE_FIXED_BUILTIN_TOOL_NAMES`：那颗名单同时是 RBAC/可见性口径的
        "常驻内置"，把无资源支撑的合成工具塞进去会改到别的判据（与 `silent` 同一处理）。"""
        from g3ku.runtime.tool_visibility import NODE_FIXED_BUILTIN_TOOL_NAMES

        assert {TIME_TOOL_NAME, SCHEDULE_WAKE_TOOL_NAME}.isdisjoint(set(NODE_FIXED_BUILTIN_TOOL_NAMES))


class TestArmWritesDurableDeadline:
    def test_tool_call_lands_one_row_for_the_node(self):
        store = _Store(_node())
        runner = _runner(store=store)
        task = SimpleNamespace(task_id=TASK, status='in_progress', is_paused=False, pause_requested=False)
        node = _node()
        raw = asyncio.run(
            runner._arm_node_wake(task=task, node=node, wake_at_ms=now_ms() + 60_000, note='播报下一幕')
        )
        assert raw['scope'] == 'node'
        assert store.timer is not None
        assert store.timer['node_id'] == PEER
        assert store.timer['note'] == '播报下一幕'
        # 回执给模型的是同一个时刻的两种写法，它不需要换算。
        assert raw['wake_at'] == iso_from_ms(store.timer['wake_at_ms'])

    def test_rearming_replaces_instead_of_stacking(self):
        store = _Store(_node())
        runner = _runner(store=store)
        task = SimpleNamespace(task_id=TASK, status='in_progress', is_paused=False, pause_requested=False)
        node = _node()
        first = now_ms() + 60_000
        second = now_ms() + 90_000
        asyncio.run(runner._arm_node_wake(task=task, node=node, wake_at_ms=first, note='a'))
        asyncio.run(runner._arm_node_wake(task=task, node=node, wake_at_ms=second, note='b'))
        assert store.upserts == 2 and store.timer['wake_at_ms'] == second

    def test_terminal_node_refuses_instead_of_accepting_a_deadline_it_cannot_keep(self):
        store = _Store(_node(status='success'))
        runner = _runner(store=store)
        raw = asyncio.run(
            runner._arm_node_wake(
                task=SimpleNamespace(task_id=TASK, status='in_progress', is_paused=False, pause_requested=False),
                node=_node(status='success'),
                wake_at_ms=now_ms() + 60_000,
                note='x',
            )
        )
        assert raw == {'error': 'node_terminal'}
        assert store.timer is None

    def test_terminal_task_refuses_too(self):
        store = _Store(_node())
        runner = _runner(store=store)
        raw = asyncio.run(
            runner._arm_node_wake(
                task=SimpleNamespace(task_id=TASK, status='success', is_paused=False, pause_requested=False),
                node=_node(),
                wake_at_ms=now_ms() + 60_000,
                note='x',
            )
        )
        assert raw == {'error': 'task_terminal'}
        assert store.timer is None


class TestWakeTimerKindCannotBeForged:
    def test_kind_is_runtime_only(self):
        """`send_message` 传不到 `system_kind`，所以模型发不出这一档。"""
        assert KIND_WAKE_TIMER in SYSTEM_KINDS
        assert derive_message_kind(origin='node', relation='peer') != KIND_WAKE_TIMER
        with pytest.raises(ValueError):
            derive_message_kind(origin='node', system_kind='wake_timer_not_a_kind')

    def test_priority_stays_normal(self):
        """停靠的节点被任意未读叫醒，因此不需要 high 那档无条件放行——high 会把暂停位也清掉。"""
        assert derive_priority(kind=KIND_WAKE_TIMER) == 'normal'

    def test_rendered_notice_still_strips_to_the_body(self):
        """标签必须留在 `来自X` 这一族里：notice tail 的去重与正文比对都靠剥这一行。"""
        body = 'Scheduled wake-up fired. Your note was: 播报下一幕'
        rendered = render_node_message_text(
            kind=KIND_WAKE_TIMER,
            source_node_id=PEER,
            message=body,
        )
        assert strip_node_message_prefix(rendered) == body

    def test_tool_receipt_is_json_and_carries_the_deadline(self):
        store = _Store(_node())
        runner = _runner(store=store)
        tool = ScheduleWakeTool(
            lambda **payload: runner._arm_node_wake(
                task=SimpleNamespace(task_id=TASK, status='in_progress', is_paused=False, pause_requested=False),
                node=_node(),
                **payload,
            )
        )
        parsed = json.loads(asyncio.run(tool.execute(note='x', after_seconds=45)))
        assert parsed['armed'] is True and parsed['scope'] == 'node'


def test_time_tool_needs_no_lane_wiring():
    """时钟不依赖任何注入：两条车道拿到的必须是同一份形状。"""
    assert set(json.loads(asyncio.run(TimeTool().execute()))) == {'epoch_ms', 'now', 'utc', 'utc_offset', 'weekday'}
