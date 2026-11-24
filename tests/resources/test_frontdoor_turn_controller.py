from __future__ import annotations

import asyncio
from typing import Any

from g3ku.config.schema import Config
from main.runtime.frontdoor_turn_controller import FrontdoorTurnController
from main.runtime.model_key_concurrency import ModelKeyConcurrencyController
from main.runtime.model_load_balancer import ModelLoadBalancer
from main.runtime.model_route import (
    LEASE_OUTCOME_SUCCESS,
    build_model_route_plan,
)
from tests.resources.test_model_load_balancer import _Clock, _group


def _config(ceo_roles: Any) -> Config:
    return Config.model_validate(
        {
            "agents": {"defaults": {"workspace": "."}},
            "models": {
                "catalog": [
                    {"key": "m_a", "llmConfigId": "rec_a", "contextWindowTokens": 200000},
                    {"key": "m_b", "llmConfigId": "rec_b", "contextWindowTokens": 200000},
                    {"key": "m_x", "llmConfigId": "rec_x", "contextWindowTokens": 200000},
                ],
                "loadBalanceGroups": {"g1": {"enabled": True, "maxRetryRounds": 2, "modelKeys": ["m_a", "m_b"]}},
                "roles": {"ceo": ceo_roles, "execution": ["m_a"], "inspection": ["m_b"], "memory": []},
            },
        }
    )


def _controller(clock: _Clock) -> tuple[FrontdoorTurnController, ModelLoadBalancer, ModelKeyConcurrencyController]:
    concurrency = ModelKeyConcurrencyController(
        resolve_model_limits=lambda ref: {"key_indexes": [0], "per_key_limits": {0: None}}
    )
    balancer = ModelLoadBalancer(
        permit_source=concurrency,
        resolve_quota_buckets=lambda key: [f"key:{key}"],
        clock=clock,
    )
    balancer.configure(groups={"g1": _group("g1", "m_a", "m_b")}, config_revision=1)
    ftc = FrontdoorTurnController(model_concurrency_controller=concurrency, balancer=balancer)
    return ftc, balancer, concurrency


def test_acquire_is_sticky_per_session_and_spreads_across_sessions() -> None:
    clock = _Clock()
    ftc, balancer, _concurrency = _controller(clock)
    plan = build_model_route_plan(_config([{"type": "load_balance", "groupKey": "g1"}]), "ceo")

    async def run() -> None:
        first = await ftc.acquire_turn(task_id="frontdoor", node_id="web:s1", route_plan=plan)
        again = await ftc.acquire_turn(task_id="frontdoor", node_id="web:s1", route_plan=plan)
        assert first is not None and again is not None
        # 同一会话粘滞：跨回合沿用同一成员（保前缀缓存）。
        assert first.selected_model_ref == again.selected_model_ref
        ftc.release_turn(first, outcome=LEASE_OUTCOME_SUCCESS)
        ftc.release_turn(again, outcome=LEASE_OUTCOME_SUCCESS)

        bound = set()
        for index in range(12):
            lease = await ftc.acquire_turn(task_id="frontdoor", node_id=f"web:s{index}", route_plan=plan)
            assert lease is not None
            bound.add(lease.selected_model_ref)
            ftc.release_turn(lease, outcome=LEASE_OUTCOME_SUCCESS)
        # 不同会话按负载摊到两个成员。
        assert bound == {"m_a", "m_b"}

    asyncio.run(run())


def test_rebind_rotates_member_after_failure() -> None:
    clock = _Clock()
    ftc, balancer, _concurrency = _controller(clock)
    plan = build_model_route_plan(_config([{"type": "load_balance", "groupKey": "g1"}]), "ceo")

    async def run() -> None:
        lease = await ftc.acquire_turn(task_id="frontdoor", node_id="web:s1", route_plan=plan)
        assert lease is not None
        first_member = lease.selected_model_ref
        ftc.record_route_request_start(lease)
        ftc.record_route_outcome(lease, status_code=429, error_text="429 rpm limit")
        selected = ftc.rebind_turn(
            lease,
            route_index=0,
            excluded_model_keys=frozenset({first_member}),
            rebind_reason="fallback_after_failure",
        )
        assert selected is not None
        assert selected.model_key != first_member
        ftc.release_turn(lease, outcome=LEASE_OUTCOME_SUCCESS)

    asyncio.run(run())


def test_acquire_returns_none_when_group_disabled() -> None:
    clock = _Clock()
    ftc, balancer, _concurrency = _controller(clock)
    disabled = _group("g1", "m_a", "m_b")
    disabled.enabled = False
    balancer.configure(groups={"g1": disabled}, config_revision=2)
    plan = build_model_route_plan(_config([{"type": "load_balance", "groupKey": "g1"}]), "ceo")

    async def run() -> None:
        lease = await ftc.acquire_turn(task_id="frontdoor", node_id="web:s1", route_plan=plan)
        assert lease is None

    asyncio.run(run())


def test_direct_chain_acquire_without_group() -> None:
    clock = _Clock()
    ftc, balancer, _concurrency = _controller(clock)
    plan = build_model_route_plan(_config(["m_x"]), "ceo")

    async def run() -> None:
        lease = await ftc.acquire_turn(task_id="frontdoor", node_id="web:s1", route_plan=plan)
        assert lease is not None
        assert lease.selected_model_ref == "m_x"
        assert lease.route_lease is None
        ftc.release_turn(lease, outcome=LEASE_OUTCOME_SUCCESS)

    asyncio.run(run())
