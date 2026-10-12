from __future__ import annotations

import asyncio
import time

import pytest

from main.runtime.model_key_concurrency import ModelKeyConcurrencyController
from main.runtime.model_load_balancer import ModelLoadBalancer
from main.runtime.model_route import (
    MODEL_ROUTE_KIND_LOAD_BALANCE,
    MODEL_ROUTE_KIND_MODEL,
    ModelRoutePlan,
    ResolvedLoadBalanceGroup,
    ResolvedModelRoute,
    RouteCandidateFilters,
    RouteMemberView,
)
from main.runtime.node_turn_controller import (
    HEAD_PIN_LIMIT_SECONDS,
    SKIP_AGING_LIMIT,
    NodeTurnController,
    NodeTurnUnservableError,
)


def _group(group_key: str, *keys: str, max_rounds: int = 1) -> ResolvedLoadBalanceGroup:
    return ResolvedLoadBalanceGroup(
        group_key=group_key,
        enabled=True,
        max_retry_rounds=max_rounds,
        members=[RouteMemberView(model_key=key, context_window_tokens=200000, image_multimodal_enabled=True) for key in keys],
    )


def _group_route(index: int, group: ResolvedLoadBalanceGroup) -> ResolvedModelRoute:
    return ResolvedModelRoute(
        index=index,
        kind=MODEL_ROUTE_KIND_LOAD_BALANCE,
        group_key=group.group_key,
        group=group,
        candidates=tuple(group.candidate_model_keys),
    )


def _model_route(index: int, model_key: str) -> ResolvedModelRoute:
    return ResolvedModelRoute(index=index, kind=MODEL_ROUTE_KIND_MODEL, model_key=model_key, candidates=(model_key,))


def _controller(*, limit: int | None = 1) -> ModelKeyConcurrencyController:
    return ModelKeyConcurrencyController(
        resolve_model_limits=lambda model_ref: {"key_indexes": [0], "per_key_limits": {0: limit}},
    )


def _harness(*groups: ResolvedLoadBalanceGroup, limit: int | None = 1) -> tuple[NodeTurnController, ModelKeyConcurrencyController, ModelLoadBalancer]:
    controller = _controller(limit=limit)
    balancer = ModelLoadBalancer(permit_source=controller)
    balancer.configure(groups={group.group_key: group for group in groups}, config_revision=1)
    turn_controller = NodeTurnController(
        model_concurrency_controller=controller,
        balancer=balancer,
        gate_supplier=lambda: True,
        poll_interval_seconds=0.05,
    )
    controller.configure(on_availability_changed=turn_controller.poke)
    return turn_controller, controller, balancer


@pytest.mark.asyncio
async def test_admission_selects_group_member_and_carries_route_lease() -> None:
    group = _group("g1", "m_a", "m_b")
    turn_controller, controller, balancer = _harness(group)
    plan = ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
    try:
        first = await turn_controller.acquire_turn(task_id="task:1", node_id="node:1", route_plan=plan)
        second = await turn_controller.acquire_turn(task_id="task:1", node_id="node:2", route_plan=plan)

        assert first.group_key == "g1"
        assert first.route_index == 0
        assert first.selected_model_ref == first.model_ref == "m_a"
        assert second.selected_model_ref == "m_b"
        assert first.route_lease is not None and first.route_lease.permit is first.initial_model_permit
        assert balancer.bound_model_for_node("node:1") == "m_a"
        # 每个在飞节点在底层各占一颗 permit，没有重复预占。
        assert sum(controller.model_state("m_a")["running"].values()) == 1
        assert sum(controller.model_state("m_b")["running"].values()) == 1
    finally:
        await turn_controller.close()


@pytest.mark.asyncio
async def test_same_node_rebinding_is_sticky_across_admissions() -> None:
    group = _group("g1", "m_a", "m_b", "m_c")
    turn_controller, _controller, balancer = _harness(group, limit=None)
    plan = ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
    try:
        lease = await turn_controller.acquire_turn(task_id="task:1", node_id="node:1", route_plan=plan)
        assert lease.selected_model_ref == "m_a"
        turn_controller.release_route_lease(lease)
        turn_controller.release_turn(lease)

        for _ in range(4):
            next_lease = await turn_controller.acquire_turn(task_id="task:1", node_id="node:1", route_plan=plan)
            assert next_lease.selected_model_ref == "m_a"
            turn_controller.release_route_lease(next_lease)
            turn_controller.release_turn(next_lease)
    finally:
        await turn_controller.close()


@pytest.mark.asyncio
async def test_busy_group_advances_to_next_route_entry() -> None:
    group = _group("g1", "m_a")
    turn_controller, controller, _balancer = _harness(group, limit=1)
    plan = ModelRoutePlan(routes=[_group_route(0, group), _model_route(1, "m_emergency")], config_revision=1)
    try:
        first = await turn_controller.acquire_turn(task_id="task:1", node_id="node:1", route_plan=plan)
        assert first.selected_model_ref == "m_a"

        # m_a 已满，组给不出候选：按链前进到 direct entry，而不是排在组里死等。
        second = await turn_controller.acquire_turn(task_id="task:1", node_id="node:2", route_plan=plan)
        assert second.selected_model_ref == "m_emergency"
        assert second.group_key == ""
        assert second.route_index == 1
        assert sum(controller.model_state("m_emergency")["running"].values()) == 1
    finally:
        await turn_controller.close()


@pytest.mark.asyncio
async def test_release_turn_returns_unconsumed_route_permit() -> None:
    group = _group("g1", "m_a", "m_b")
    turn_controller, controller, balancer = _harness(group, limit=None)
    plan = ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
    try:
        lease = await turn_controller.acquire_turn(task_id="task:1", node_id="node:1", route_plan=plan)
        # 模拟 chat 没有消费这颗 permit（preflight 失败 / 取消）：回合释放必须归还全部占用。
        turn_controller.release_turn(lease)

        assert lease.route_lease is None
        assert lease.initial_model_permit is None
        reserved = {row["model_key"]: row["reserved"] for row in balancer.snapshot()["groups"]["g1"]["members"]}
        assert reserved == {"m_a": 0, "m_b": 0}
        assert sum(controller.model_state("m_a")["running"].values()) == 0
        assert turn_controller.snapshot()["node_queue_running_count"] == 0
    finally:
        await turn_controller.close()


@pytest.mark.asyncio
async def test_cancelled_request_leaks_no_permit() -> None:
    group = _group("g1", "m_a")
    turn_controller, controller, balancer = _harness(group, limit=1)
    plan = ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
    try:
        occupied = await turn_controller.acquire_turn(task_id="task:1", node_id="node:1", route_plan=plan)

        waiter = asyncio.create_task(turn_controller.acquire_turn(task_id="task:1", node_id="node:2", route_plan=plan))
        await asyncio.sleep(0.12)
        assert waiter.done() is False
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await asyncio.sleep(0.12)

        # 被取消的请求不能把第二颗 permit 或第二笔 reserved 留在 m_a 上：此刻 m_a 上
        # 只有 blocker 自己那一笔。
        assert sum(controller.model_state("m_a")["running"].values()) == 1
        members = {row["model_key"]: row["reserved"] for row in balancer.snapshot()["groups"]["g1"]["members"]}
        assert members["m_a"] == 1
        assert turn_controller.snapshot()["node_queue_waiting_count"] == 0
        turn_controller.release_turn(occupied)
        assert sum(controller.model_state("m_a")["running"].values()) == 0
        members_after = {row["model_key"]: row["reserved"] for row in balancer.snapshot()["groups"]["g1"]["members"]}
        assert members_after["m_a"] == 0
    finally:
        await turn_controller.close()


@pytest.mark.asyncio
async def test_rebind_turn_advances_within_group_on_same_lease() -> None:
    group = _group("g1", "m_a", "m_b", max_rounds=1)
    turn_controller, controller, balancer = _harness(group, limit=None)
    plan = ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
    try:
        lease = await turn_controller.acquire_turn(task_id="task:1", node_id="node:1", route_plan=plan)
        assert lease.selected_model_ref == "m_a"
        running_before = sum(controller.model_state("m_a")["running"].values())

        rebound = turn_controller.rebind_turn(
            lease,
            filters=RouteCandidateFilters(excluded_model_keys=frozenset()),
            excluded_model_keys=frozenset({"m_a"}),
            rebind_reason="fallback_after_failure",
        )

        assert rebound is not None
        assert lease.selected_model_ref == "m_b"
        assert lease.route_lease is rebound
        assert lease.key_index == rebound.key_index
        # 旧成员的 permit 已归还，新成员的挂上；回合权数量不变。
        assert sum(controller.model_state("m_a")["running"].values()) == running_before - 1
        assert sum(controller.model_state("m_b")["running"].values()) == 1
        assert turn_controller.snapshot()["node_queue_running_count"] == 1
        assert balancer.bound_model_for_node("node:1") == "m_b"
        turn_controller.release_turn(lease)
    finally:
        await turn_controller.close()


@pytest.mark.asyncio
async def test_aged_request_becomes_a_barrier_against_head_of_line_starvation() -> None:
    controller = ModelKeyConcurrencyController(
        resolve_model_limits=lambda model_ref: {
            "key_indexes": [0],
            "per_key_limits": {0: 1 if model_ref == "model:blocked" else 8},
        }
    )
    turn_controller = NodeTurnController(
        model_concurrency_controller=controller,
        gate_supplier=lambda: True,
        poll_interval_seconds=0.05,
    )
    controller.configure(on_availability_changed=turn_controller.poke)
    try:
        # 先把 model:blocked 占满，让队头请求始终不可授予。
        blocker = await turn_controller.acquire_turn(task_id="task:b", node_id="node:b", model_ref="model:blocked")
        aged = asyncio.create_task(turn_controller.acquire_turn(task_id="task:aged", node_id="node:aged", model_ref="model:blocked"))
        await asyncio.sleep(0.12)

        # 越过它被授予的可用请求会累加它的 skipped；到上界为止恰好还能过 SKIP_AGING_LIMIT 次。
        later_grants = []
        for index in range(SKIP_AGING_LIMIT):
            lease = await asyncio.wait_for(
                turn_controller.acquire_turn(task_id="task:free", node_id=f"node:free:{index}", model_ref="model:free"),
                timeout=1.0,
            )
            later_grants.append(lease)

        assert aged.done() is False
        window = turn_controller._grant_window()
        # 屏障：之后只能等它自己可用，不再越过它授予后来者。
        assert [request.node_id for request in window] == ["node:aged"]
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                turn_controller.acquire_turn(task_id="task:free", node_id="node:free:late", model_ref="model:free"),
                timeout=0.3,
            )

        controller.release(blocker.initial_model_permit)
        blocker.initial_model_permit = None
        turn_controller.release_turn(blocker)

        granted_aged = await asyncio.wait_for(aged, timeout=1.0)
        assert granted_aged.node_id == "node:aged"
        for lease in later_grants:
            turn_controller.release_turn(lease)
    finally:
        await turn_controller.close()


@pytest.mark.asyncio
async def test_request_wider_than_every_candidate_is_rejected_before_it_can_pin_the_queue() -> None:
    """体量比链上任何候选都大的请求：入队前就结清，队列长度一根毛都不涨。

    实盘形状（2026-10-12 task:d07d706989b4）：392,742 的单 vs 390,000 的成员窗口，
    入队后被越过 4 次成队头独占，11:55:29 之后整机 0 次授予、零错误行、110 分钟。
    """
    group = _group("g1", "m_a", "m_b")
    turn_controller, _controller, _balancer = _harness(group, limit=None)
    plan = ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
    try:
        with pytest.raises(NodeTurnUnservableError) as raised:
            await turn_controller.acquire_turn(
                task_id="task:1",
                node_id="node:wide",
                route_plan=plan,
                filters=RouteCandidateFilters(required_context_window_tokens=250000),
            )
        assert raised.value.reason == "no_candidate"
        assert raised.value.required_window_tokens == 250000
        assert raised.value.largest_candidate_window_tokens == 200000
        assert turn_controller.snapshot()["node_queue_waiting_count"] == 0
    finally:
        await turn_controller.close()


@pytest.mark.asyncio
async def test_route_pointing_at_a_missing_group_is_rejected_as_unservable() -> None:
    group = _group("g1", "m_a")
    turn_controller, _controller, _balancer = _harness(group, limit=None)
    ghost = _group("gone", "m_x")
    plan = ModelRoutePlan(routes=[_group_route(0, ghost)], config_revision=1)
    try:
        with pytest.raises(NodeTurnUnservableError) as raised:
            await turn_controller.acquire_turn(task_id="task:1", node_id="node:1", route_plan=plan)
        assert raised.value.reason == "unknown_group"
        assert turn_controller.snapshot()["node_queue_waiting_count"] == 0
    finally:
        await turn_controller.close()


@pytest.mark.asyncio
async def test_placeable_but_busy_request_still_queues_and_reports_capacity_reason() -> None:
    group = _group("g1", "m_a")
    turn_controller, _controller, _balancer = _harness(group, limit=1)
    plan = ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
    try:
        holder = await asyncio.wait_for(
            turn_controller.acquire_turn(task_id="task:1", node_id="node:a", route_plan=plan), timeout=1.0
        )
        waiter = asyncio.create_task(
            turn_controller.acquire_turn(task_id="task:1", node_id="node:b", route_plan=plan)
        )
        for _ in range(40):
            await asyncio.sleep(0.05)
            if turn_controller.snapshot()["node_queue_waiting_count"] == 1:
                break
        snap = turn_controller.snapshot()
        assert snap["node_queue_waiting_count"] == 1
        # "暂时没位置"与"永远等不到"必须分得开：这条只该排队，不该被结清。
        assert snap["node_queue_head_busy_reason"] == "no_capacity"

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await asyncio.sleep(0.1)
        # `CancelledError` 不是 `Exception`：漏掉它就会在队列里留一条无人应答的幽灵项。
        assert turn_controller.snapshot()["node_queue_waiting_count"] == 0
        turn_controller.release_turn(holder)
    finally:
        await turn_controller.close()


@pytest.mark.asyncio
async def test_pinned_head_that_turned_unservable_is_settled_after_the_time_limit() -> None:
    group = _group("g1", "m_a")
    turn_controller, _controller, _balancer = _harness(group, limit=1)
    plan = ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
    try:
        holder = await asyncio.wait_for(
            turn_controller.acquire_turn(task_id="task:1", node_id="node:a", route_plan=plan), timeout=1.0
        )
        waiter = asyncio.create_task(
            turn_controller.acquire_turn(task_id="task:1", node_id="node:b", route_plan=plan)
        )
        for _ in range(40):
            await asyncio.sleep(0.05)
            if turn_controller.snapshot()["node_queue_waiting_count"] == 1:
                break
        with turn_controller._lock:
            request = turn_controller._queue[0]
            request.skipped = SKIP_AGING_LIMIT
            request.pinned_mono = time.perf_counter() - (HEAD_PIN_LIMIT_SECONDS + 100.0)
            # 授予后被改成"没有任何候选塞得下"（成员窗口缩了/被移出组）：独占必须有出口。
            request.filters = RouteCandidateFilters(required_context_window_tokens=999_999)

        with pytest.raises(NodeTurnUnservableError):
            await asyncio.wait_for(waiter, timeout=3.0)
        assert turn_controller.snapshot()["node_queue_waiting_count"] == 0
        turn_controller.release_turn(holder)
    finally:
        await turn_controller.close()


def _window_group(
    group_key: str,
    pairs: list[tuple[str, int]],
    *,
    group_enabled: bool = True,
    member_enabled: bool = True,
) -> ResolvedLoadBalanceGroup:
    return ResolvedLoadBalanceGroup(
        group_key=group_key,
        enabled=group_enabled,
        max_retry_rounds=1,
        members=[
            RouteMemberView(
                model_key=key,
                enabled=member_enabled,
                context_window_tokens=window,
                image_multimodal_enabled=True,
            )
            for key, window in pairs
        ],
    )


@pytest.mark.asyncio
async def test_peek_ruler_reports_the_group_minimum_and_takes_nothing() -> None:
    """尺子预测只读资格：给这个组最小成员的窗口，不写绑定、不占 permit。

    占 permit 的那一侧才是"这一跳打到谁"的权威；预测只提供一个数，越界了由授予兜住。
    """
    group = _window_group("g1", [("m_big", 690000), ("m_small", 390000)])
    turn_controller, _controller, balancer = _harness(group, limit=1)
    plan = ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
    try:
        peeked = turn_controller.peek_hop_windows(node_id="node:1", route_plan=plan)
        assert peeked == {"group_key": "g1", "model_key": "m_big", "min_window": 390000}
        assert balancer._bindings == {}, "预测不许写绑定：写了就等于替准入决定粘滞谁"
        # limit=1：预测若偷偷占了 permit，这一步授予就再也等不到。
        holder = await asyncio.wait_for(
            turn_controller.acquire_turn(task_id="task:1", node_id="node:1", route_plan=plan), timeout=1.0
        )
        turn_controller.release_turn(holder)
    finally:
        await turn_controller.close()


@pytest.mark.asyncio
async def test_peek_ruler_walks_to_the_route_that_can_actually_serve() -> None:
    """第一个组没人接单（成员全禁用）时按同一条走法往后一位：尺子跟着这一跳真会落到的组。"""
    dead = _window_group("g_dead", [("m_dead", 999999)], member_enabled=False)
    live = _window_group("g_live", [("m_live_big", 690000), ("m_live_small", 300000)])
    turn_controller, _controller, _balancer = _harness(dead, live, limit=1)
    plan = ModelRoutePlan(routes=[_group_route(0, dead), _group_route(1, live)], config_revision=1)
    try:
        peeked = turn_controller.peek_hop_windows(node_id="node:walk", route_plan=plan)
        assert peeked is not None
        assert peeked["group_key"] == "g_live"
        assert peeked["min_window"] == 300000
    finally:
        await turn_controller.close()


@pytest.mark.asyncio
async def test_peek_ruler_is_silent_for_direct_routes_and_missing_balancer() -> None:
    """direct 链位与没有 balancer 时返回 None：调用方退回链级口径，而不是拿到 0 去撞硬门。"""
    turn_controller, _controller, _balancer = _harness(_window_group("g1", [("m_a", 390000)]))
    direct_plan = ModelRoutePlan(routes=[_model_route(0, "m_direct")], config_revision=1)
    try:
        assert turn_controller.peek_hop_windows(node_id="node:direct", route_plan=direct_plan) is None
        assert turn_controller.peek_hop_windows(node_id="node:direct", route_plan=None) is None
    finally:
        await turn_controller.close()

    plain_controller = NodeTurnController(
        model_concurrency_controller=_controller,
        balancer=None,
        gate_supplier=lambda: True,
        poll_interval_seconds=0.05,
    )
    try:
        group = _window_group("g1", [("m_a", 390000)])
        assert plain_controller.peek_hop_windows(
            node_id="node:nobalancer", route_plan=ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
        ) is None
    finally:
        await plain_controller.close()
