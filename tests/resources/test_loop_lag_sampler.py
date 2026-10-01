from __future__ import annotations

import time

from main.runtime.tool_pressure_monitor import _EventLoopLagSampler


class _DeferredLoop:
    """`call_soon_threadsafe` 收下单子但永远不执行——模拟事件循环被占住。"""

    def __init__(self) -> None:
        self.deferred: list[tuple[object, tuple]] = []

    def call_soon_threadsafe(self, handler, *args):
        self.deferred.append((handler, args))

    def drain(self) -> None:
        pending, self.deferred = list(self.deferred), []
        for handler, args in pending:
            handler(*args)


def test_an_unresolved_ping_reports_its_own_age_not_zero() -> None:
    """滞后跨过几十毫秒还没被循环跑掉，读数就必须是那个数——不许读成 0。

    旧调用形状（先 ping 再用 ping 之前的时刻做差）会让这里恒为 0，
    于是 warn 线 250 ms 那一整段真实滞后在闸的输入里消失。
    """
    loop = _DeferredLoop()
    sampler = _EventLoopLagSampler(loop)

    sampler.ping()
    time.sleep(0.06)

    assert sampler.sample() >= 50.0


def test_the_resolved_lag_of_the_previous_ping_survives_into_the_next_sample() -> None:
    loop = _DeferredLoop()
    sampler = _EventLoopLagSampler(loop)

    sampler.ping()
    time.sleep(0.06)
    loop.drain()

    assert sampler.sample() >= 50.0


def test_a_responsive_loop_stays_well_under_the_warn_line() -> None:
    """反向钉：修完不许让健康的循环读出 warn 级滞后，否则闸会因为仪器而收。"""
    loop = _DeferredLoop()
    sampler = _EventLoopLagSampler(loop)

    for _ in range(5):
        sampler.ping()
        loop.drain()

    assert sampler.sample() < 250.0


def test_a_ping_is_only_in_flight_once() -> None:
    loop = _DeferredLoop()
    sampler = _EventLoopLagSampler(loop)

    sampler.ping()
    sampler.ping()

    assert len(loop.deferred) == 1
