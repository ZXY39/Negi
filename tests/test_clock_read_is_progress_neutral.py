"""时钟读数不算进度证据：两处按"相同签名重复"判停的守卫都要让开它。

`time` 参数逐字相同是它的本性（无参），所以"连续三次相同调用"这条判据对它恒真——不豁免
的话，一个每轮校时的节点（报时节点正是这一类）会被看门狗判成 `repeated_identical_tool_call`
并落进 `暂停·异常`，而它只是想知道现在几点。`schedule_wake` 不豁免：逐字相同的重复设闹钟
确实是空转，那正是守卫该抓的形状。

两条守卫必须一起豁免，只豁免一处时另一处照样打停，症状只是换了个 reason。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from main.runtime.node_watchdog import watchdog_pause_reason
from main.runtime.tool_names import (
    PROGRESS_NEUTRAL_TOOL_NAMES,
    SCHEDULE_WAKE_TOOL_NAME,
    TIME_TOOL_NAME,
)

NOW = datetime(2026, 10, 26, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))


class _Call:
    """`list_task_node_tool_results` 返回行的最小形状（看门狗只读名字/入参/状态/时刻）。"""

    def __init__(self, tool_name: str, arguments: str = '', *, status: str = 'success', finished: str = ''):
        self.tool_name = tool_name
        self.arguments_text = arguments
        self.status = status
        self.finished_at = finished or NOW.isoformat()


def _frame(phase: str = 'in_model') -> dict:
    return {'phase': phase, 'tool_calls': [], 'await_marker': ''}


def _stamp(offset_seconds: int) -> str:
    return (NOW + timedelta(seconds=offset_seconds)).isoformat()


class TestWatchdogExemption:
    def test_three_identical_clock_reads_do_not_pause(self):
        reason = watchdog_pause_reason(
            frame=_frame(),
            tool_results=[_Call(TIME_TOOL_NAME) for _ in range(3)],
            now=NOW,
            stall_seconds=600.0,
            last_event_at=_stamp(0),
        )
        assert reason == ''

    def test_three_identical_real_calls_still_pause(self):
        """豁免只针对时钟：同一颗工具带同一份入参连成三次，仍然是"没有新信息"。"""
        reason = watchdog_pause_reason(
            frame=_frame(),
            tool_results=[_Call('content_open', json.dumps({'path': 'C:/a.md'})) for _ in range(3)],
            now=NOW,
            stall_seconds=600.0,
            last_event_at=_stamp(0),
        )
        assert reason == 'repeated_identical_tool_call'

    def test_clock_reads_do_not_reset_the_stall_clock(self):
        """摘掉时钟读数是刻意的：`no_new_events` 取的是滤完之后的最后一项时刻，所以
        "这一小时只在校时、没有真事件"照样按失速窗口判停——校时不算进展。"""
        results = [_Call('exec', json.dumps({'command': 'ls'}), finished=_stamp(0))]
        results += [_Call(TIME_TOOL_NAME, finished=_stamp(step * 300)) for step in range(1, 5)]
        reason = watchdog_pause_reason(
            frame=_frame(),
            tool_results=results,
            now=NOW + timedelta(seconds=4 * 300),
            stall_seconds=600.0,
            last_event_at='',
        )
        assert reason == 'no_new_events'

    def test_error_streak_of_clock_reads_is_not_a_fault_either(self):
        reason = watchdog_pause_reason(
            frame=_frame(),
            tool_results=[_Call(TIME_TOOL_NAME, status='error') for _ in range(3)],
            now=NOW,
            stall_seconds=600.0,
            last_event_at=_stamp(0),
        )
        assert reason == ''


class TestCrossTurnBreakerExemption:
    """跨轮熔断的机制本体 + "两处守卫不能只豁免一处"的漂移闸门。

    熔断器本身按签名判三次连发，这段用真实对象钉住它的判据（豁免前它确实会打停时钟读数）。
    豁免发生在两个调用点（`react_loop` 的跨轮熔断、`node_watchdog` 的重复判据），所以另加
    一条源码级对账：两边都必须引用同一个常量，只改一处的症状是 reason 换个名字照旧打停。
    """

    def _signature(self, name: str, arguments: dict) -> str:
        return f'{name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}'

    def test_three_identical_signatures_trip_the_breaker(self):
        from main.runtime.react_loop import RepeatedActionCircuitBreaker

        breaker = RepeatedActionCircuitBreaker()
        signature = self._signature('exec', {'command': 'ls'})
        assert breaker.register(signature) is None
        assert breaker.register(signature) is None
        assert breaker.register(signature) == signature

    def test_clock_reads_are_skipped_before_the_breaker_sees_them(self):
        """豁免档在 `react_loop` 里排在 `breaker.register` 之前：签名根本没进窗口。"""
        source = (Path(__file__).resolve().parents[1] / 'main' / 'runtime' / 'react_loop.py').read_text(
            encoding='utf-8'
        )
        skip = 'if tool_name in PROGRESS_NEUTRAL_TOOL_NAMES:'
        assert skip in source
        assert source.index(skip) < source.index('breaker.register(signature)')

    def test_both_guards_read_the_same_constant(self):
        root = Path(__file__).resolve().parents[1]
        for relative in ('main/runtime/react_loop.py', 'main/runtime/node_watchdog.py'):
            text = (root / relative).read_text(encoding='utf-8')
            assert 'PROGRESS_NEUTRAL_TOOL_NAMES' in text, f'{relative} lost the exemption'

    def test_wake_tool_is_not_exempt(self):
        assert SCHEDULE_WAKE_TOOL_NAME not in PROGRESS_NEUTRAL_TOOL_NAMES
