from __future__ import annotations

from types import SimpleNamespace

import pytest

import main.runtime.chat_backend as chat_backend_module
from g3ku.providers.base import LLMResponse
from g3ku.providers.provider_factory import ProviderTarget
from main.runtime.model_key_concurrency import ModelKeyConcurrencyController
from main.runtime.model_load_balancer import ModelLoadBalancer
from main.runtime.model_route import (
    MODEL_ROUTE_KIND_LOAD_BALANCE,
    MODEL_ROUTE_KIND_MODEL,
    ModelRoutePlan,
    ResolvedLoadBalanceGroup,
    ResolvedModelRoute,
)
from main.runtime.node_turn_controller import NodeTurnController, NodeTurnLease


class _StatusError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _RateLimitedProvider:
    """永远 429：用于观察组内换成员与链前进。"""

    def __init__(self, model_key: str, calls: list[str]) -> None:
        self.model_key = model_key
        self.calls = calls

    async def chat(self, **kwargs):
        _ = kwargs
        self.calls.append(self.model_key)
        raise _StatusError("RateLimitError: Error code: 429 - {'error': {'message': 'rpm limit'}}", 429)


class _OkProvider:
    def __init__(self, model_key: str, calls: list[str], on_call=None) -> None:
        self.model_key = model_key
        self.calls = calls
        self.on_call = on_call

    async def chat(self, **kwargs):
        _ = kwargs
        self.calls.append(self.model_key)
        if self.on_call is not None:
            self.on_call()
        return LLMResponse(content="ok", finish_reason="stop")


def _target(model_key: str, provider, *, retry_count: int = 0, api_key_count: int = 1, api_key_indexes: list[int] | None = None) -> ProviderTarget:
    return ProviderTarget(
        provider_ref=str(model_key),
        provider_id="custom",
        model_id=f"{model_key}-model",
        provider=provider,
        retry_on=["network", "429", "502"],
        retry_count=retry_count,
        api_key_count=api_key_count,
        api_key_indexes=list(range(api_key_count)) if api_key_indexes is None else list(api_key_indexes),
    )


def _group(*keys: str, max_rounds: int = 1) -> ResolvedLoadBalanceGroup:
    from main.runtime.model_route import RouteMemberView

    return ResolvedLoadBalanceGroup(
        group_key="g1",
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


def _wiring(group: ResolvedLoadBalanceGroup, *, plan: ModelRoutePlan, limits: dict[str, dict[str, object]] | None = None):
    """按准入层的真实形状造一个已绑定的 node-turn lease（不启动 pump）。"""
    controller = ModelKeyConcurrencyController(resolve_model_limits=lambda model_ref: dict((limits or {}).get(model_ref) or {"key_indexes": [0], "per_key_limits": {0: None}}))
    balancer = ModelLoadBalancer(permit_source=controller)
    balancer.configure(groups={group.group_key: group}, config_revision=plan.config_revision)
    route_lease, reason = balancer.select(node_id="node:1", route_index=0, group_key=group.group_key)
    assert route_lease is not None, reason
    lease = NodeTurnLease(
        lease_id=1,
        task_id="task:1",
        node_id="node:1",
        model_ref=str(route_lease.model_key),
        key_index=int(route_lease.key_index),
        acquired_at="",
        initial_model_permit=route_lease.permit,
        route_index=0,
        group_key=str(route_lease.group_key),
        route_plan=plan,
        route_lease=route_lease,
    )
    turn_controller = NodeTurnController(model_concurrency_controller=controller, balancer=balancer)
    return turn_controller, controller, balancer, lease


def _backend() -> chat_backend_module.ConfigChatBackend:
    return chat_backend_module.ConfigChatBackend(config=SimpleNamespace(get_model_runtime_profile=lambda ref: None))


def _patch(monkeypatch, providers: dict[str, ProviderTarget]) -> None:
    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return providers[str(model_key)]

    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)


@pytest.mark.asyncio
async def test_first_attempt_consumes_admitted_group_permit(monkeypatch) -> None:
    group = _group("m_a", "m_b")
    plan = ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
    turn_controller, controller, balancer, lease = _wiring(group, plan=plan)
    calls: list[str] = []
    _patch(monkeypatch, {"m_a": _target("m_a", _OkProvider("m_a", calls)), "m_b": _target("m_b", _OkProvider("m_b", calls))})

    acquired: list[tuple[str, int]] = []
    real_acquire = controller.acquire_specific

    async def _spy(*, model_ref: str, key_index: int):
        acquired.append((str(model_ref), int(key_index)))
        return await real_acquire(model_ref=model_ref, key_index=key_index)

    monkeypatch.setattr(controller, "acquire_specific", _spy)

    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=list(plan.candidate_model_keys),
        model_routes=plan,
        node_turn_lease=lease,
        node_turn_controller=turn_controller,
        model_concurrency_controller=controller,
    )

    assert response.content == "ok"
    assert calls == ["m_a"]
    # 准入 permit 被第一次 attempt 消费，没有第二次 acquire。
    assert acquired == []
    assert lease.initial_model_permit is None
    # RPM 观测只在真正发请求那一刻记一次。
    members = {row["model_key"]: row for row in balancer.snapshot()["groups"]["g1"]["members"]}
    assert members["m_a"]["rolling_rpm_60s"] == 1
    assert members["m_a"]["reserved"] == 0


@pytest.mark.asyncio
async def test_group_budget_ignores_member_catalog_retry_count(monkeypatch) -> None:
    group = _group("m_a", "m_b", max_rounds=1)
    plan = ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
    turn_controller, controller, balancer, lease = _wiring(group, plan=plan)
    calls: list[str] = []
    a_running_during_b: list[int] = []

    def _observe_previous_member() -> None:
        a_running_during_b.append(sum(int(v or 0) for v in controller.model_state("m_a")["running"].values()))

    # m_a 的 catalog retry_count=9999999：对 group route 必须无效，只跑 1 个 pass 就让位。
    _patch(
        monkeypatch,
        {
            "m_a": _target("m_a", _RateLimitedProvider("m_a", calls), retry_count=9999999),
            "m_b": _target("m_b", _OkProvider("m_b", calls, on_call=_observe_previous_member)),
        },
    )
    monkeypatch.setattr(chat_backend_module, "model_retry_backoff_seconds", lambda attempt: 0.0)

    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=list(plan.candidate_model_keys),
        model_routes=plan,
        node_turn_lease=lease,
        node_turn_controller=turn_controller,
        model_concurrency_controller=controller,
    )

    assert response.content == "ok"
    assert calls == ["m_a", "m_b"]
    assert lease.selected_model_ref == "m_b"
    # 换成员时旧成员的 permit 已归还：m_b 在飞期间 m_a 不再占用。
    assert a_running_during_b == [0]
    # 结束后不得有任何残留占用或双减。
    assert sum(controller.model_state("m_a")["running"].values()) == 0
    assert sum(controller.model_state("m_b")["running"].values()) == 0
    reserved = {row["model_key"]: row["reserved"] for row in balancer.snapshot()["groups"]["g1"]["members"]}
    assert reserved == {"m_a": 0, "m_b": 0}


@pytest.mark.asyncio
async def test_member_rotation_is_paced_by_backoff(monkeypatch) -> None:
    """组预算收缩后必须保留节拍：换成员前调用退避，否则 429 风暴下会背靠背打满整组。"""
    group = _group("m_a", "m_b", max_rounds=1)
    plan = ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
    turn_controller, controller, _balancer, lease = _wiring(group, plan=plan)
    calls: list[str] = []
    delays: list[int] = []

    def _record(attempt_number: int) -> float:
        delays.append(int(attempt_number))
        return 0.0

    monkeypatch.setattr(chat_backend_module, "model_retry_backoff_seconds", _record)
    _patch(
        monkeypatch,
        {
            "m_a": _target("m_a", _RateLimitedProvider("m_a", calls)),
            "m_b": _target("m_b", _OkProvider("m_b", calls)),
        },
    )
    await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=list(plan.candidate_model_keys),
        model_routes=plan,
        node_turn_lease=lease,
        node_turn_controller=turn_controller,
        model_concurrency_controller=controller,
    )

    assert delays == [1]
    assert calls == ["m_a", "m_b"]


@pytest.mark.asyncio
async def test_429_outcome_is_attributed_to_the_selected_member(monkeypatch) -> None:
    group = _group("m_a", "m_b")
    plan = ModelRoutePlan(routes=[_group_route(0, group), _model_route(1, "m_emergency")], config_revision=1)
    turn_controller, controller, balancer, lease = _wiring(group, plan=plan)
    calls: list[str] = []
    monkeypatch.setattr(chat_backend_module, "model_retry_backoff_seconds", lambda attempt: 0.0)
    _patch(
        monkeypatch,
        {
            "m_a": _target("m_a", _RateLimitedProvider("m_a", calls)),
            "m_b": _target("m_b", _RateLimitedProvider("m_b", calls)),
            "m_emergency": _target("m_emergency", _OkProvider("m_emergency", calls)),
        },
    )

    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=list(plan.candidate_model_keys) + ["m_emergency"],
        model_routes=plan,
        node_turn_lease=lease,
        node_turn_controller=turn_controller,
        model_concurrency_controller=controller,
    )

    assert response.content == "ok"
    # 组内两个成员都失败后才进 direct entry。
    assert calls == ["m_a", "m_b", "m_emergency"]
    members = {row["model_key"]: row for row in balancer.snapshot()["groups"]["g1"]["members"]}
    assert members["m_a"]["penalty_429"] > 0
    # 失败记忆只剩上游限流这一档：快照里不再有冷却类字段。
    assert "cooldown_reason" not in members["m_a"]
    # 离开组去跑 direct 之前，组侧 permit 必须已经归还。
    assert sum(controller.model_state("m_a")["running"].values()) == 0
    assert sum(controller.model_state("m_b")["running"].values()) == 0


@pytest.mark.asyncio
async def test_group_slot_without_candidate_skips_to_next_route(monkeypatch) -> None:
    group = _group("m_a", "m_b")
    plan = ModelRoutePlan(routes=[_group_route(0, group), _model_route(1, "m_emergency")], config_revision=1)
    # 两个成员各限一颗 permit，且都被外部占满：组给不出候选。
    turn_controller, controller, _balancer, lease = _wiring(
        group,
        plan=plan,
        limits={
            "m_a": {"key_indexes": [0], "per_key_limits": {0: 1}},
            "m_b": {"key_indexes": [0], "per_key_limits": {0: 1}},
            "m_emergency": {"key_indexes": [0], "per_key_limits": {0: 1}},
        },
    )
    # lease 已经占掉 m_a 的一颗；再把 m_b 占满，组内就没有空位了。
    blocked_b = controller.try_acquire_first_available(model_ref="m_b")
    assert blocked_b is not None

    calls: list[str] = []
    _patch(monkeypatch, {"m_emergency": _target("m_emergency", _OkProvider("m_emergency", calls))})

    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=list(plan.candidate_model_keys) + ["m_emergency"],
        model_routes=plan,
        node_turn_lease=lease,
        node_turn_controller=turn_controller,
        model_concurrency_controller=controller,
    )

    assert response.content == "ok"
    assert calls == ["m_emergency"]
    controller.release(blocked_b)


@pytest.mark.asyncio
async def test_retry_status_reports_route_entries_and_attempts(monkeypatch) -> None:
    """重试 toast 的载荷必须给出真正的 fallback 链、实际试过的模型与当前组。

    用「同模型换 key」触发一次 retrying 事件：跨模型前进按既有策略只计数不发 toast。
    """
    group = _group("m_a", "m_b")
    plan = ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
    # 控制器要放行 m_a 的两把 key，否则换 key 会被判成「该 key 已禁用」。
    turn_controller, controller, _balancer, lease = _wiring(
        group,
        plan=plan,
        limits={
            "m_a": {"key_indexes": [0, 1], "per_key_limits": {0: None, 1: None}},
            "m_b": {"key_indexes": [0], "per_key_limits": {0: None}},
        },
    )
    calls: list[str] = []
    events: list[dict] = []

    class _RotateAfterAuthError:
        def __init__(self) -> None:
            self.attempts = 0

        async def chat(self, **kwargs):
            _ = kwargs
            calls.append("m_a")
            self.attempts += 1
            if self.attempts == 1:
                raise _StatusError("HTTP 401: invalid api key", 401)
            return LLMResponse(content="ok", finish_reason="stop")

    rotating = _RotateAfterAuthError()
    _patch(
        monkeypatch,
        {
            "m_a": _target("m_a", rotating, api_key_count=2),
            "m_b": _target("m_b", _OkProvider("m_b", calls)),
        },
    )

    async def _record(status: dict) -> None:
        events.append(dict(status))

    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=list(plan.candidate_model_keys),
        model_routes=plan,
        node_turn_lease=lease,
        node_turn_controller=turn_controller,
        model_concurrency_controller=controller,
        on_model_retry_status=_record,
    )

    assert response.content == "ok"
    assert calls == ["m_a", "m_a"]
    retrying = [event for event in events if event.get("state") == "retrying"]
    assert len(retrying) == 1
    payload = retrying[0]
    assert payload["route_entries"] == [
        {"type": "load_balance", "group_key": "g1", "model_keys": ["m_a", "m_b"], "max_retry_rounds": 1}
    ]
    assert payload["selected_group_key"] == "g1"
    assert payload["attempted_model_keys"] == ["m_a"]
    # 旧字段保持扁平候选列表，旧 UI 不因此坏掉。
    assert payload["model_refs"] == ["m_a", "m_b"]


@pytest.mark.asyncio
async def test_held_permit_key_is_rotated_to_front_of_key_pass(monkeypatch) -> None:
    """准入选中第二把 key 时，第一次 attempt 必须从那把 key 开始，否则会二次 acquire。"""
    group = _group("m_a")
    plan = ModelRoutePlan(routes=[_group_route(0, group)], config_revision=1)
    controller = ModelKeyConcurrencyController(
        resolve_model_limits=lambda model_ref: {"key_indexes": [0, 1, 2], "per_key_limits": {0: None, 1: None, 2: None}}
    )
    balancer = ModelLoadBalancer(permit_source=controller)
    balancer.configure(groups={"g1": group}, config_revision=1)
    route_lease, reason = balancer.select(node_id="node:1", route_index=0, group_key="g1")
    assert route_lease is not None, reason
    # 手工把绑定挪到第二把 key，模拟 least-loaded 命中非首 key。
    controller.release(route_lease.permit)
    held = controller.acquire_least_loaded(model_ref="m_a")
    assert held is not None
    held.key_index = 1
    route_lease.permit = held
    lease = NodeTurnLease(
        lease_id=1,
        task_id="task:1",
        node_id="node:1",
        model_ref="m_a",
        key_index=1,
        acquired_at="",
        initial_model_permit=held,
        route_index=0,
        group_key="g1",
        route_plan=plan,
        route_lease=route_lease,
    )
    turn_controller = NodeTurnController(model_concurrency_controller=controller, balancer=balancer)

    calls: list[str] = []
    _patch(monkeypatch, {"m_a": _target("m_a", _OkProvider("m_a", calls), api_key_count=3)})
    acquired: list[tuple[str, int]] = []
    real_acquire = controller.acquire_specific

    async def _spy(*, model_ref: str, key_index: int):
        acquired.append((str(model_ref), int(key_index)))
        return await real_acquire(model_ref=model_ref, key_index=key_index)

    monkeypatch.setattr(controller, "acquire_specific", _spy)

    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["m_a"],
        model_routes=plan,
        node_turn_lease=lease,
        node_turn_controller=turn_controller,
        model_concurrency_controller=controller,
    )

    assert response.content == "ok"
    assert calls == ["m_a"]
    assert acquired == []
