from __future__ import annotations

import asyncio

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
from main.runtime.node_turn_controller import SKIP_AGING_LIMIT, NodeTurnController


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
