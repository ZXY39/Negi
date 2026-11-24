from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from g3ku.agent.memory_agent_runtime import (
    MemoryManager,
    set_memory_model_route_source,
)
from g3ku.config.schema import Config
from main.runtime.frontdoor_turn_controller import FrontdoorTurnController
from main.runtime.model_key_concurrency import ModelKeyConcurrencyController
from main.runtime.model_load_balancer import ModelLoadBalancer
from tests.resources.test_model_load_balancer import _Clock, _group


def _config(memory_roles: Any) -> Config:
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
                "roles": {"ceo": ["m_x"], "execution": ["m_a"], "inspection": ["m_b"], "memory": memory_roles},
            },
        }
    )


def _source() -> tuple[ModelLoadBalancer, FrontdoorTurnController]:
    clock = _Clock()
    concurrency = ModelKeyConcurrencyController(
        resolve_model_limits=lambda ref: {"key_indexes": [0], "per_key_limits": {0: None}}
    )
    balancer = ModelLoadBalancer(permit_source=concurrency, resolve_quota_buckets=lambda key: [f"key:{key}"], clock=clock)
    balancer.configure(groups={"g1": _group("g1", "m_a", "m_b")}, config_revision=1)
    ftc = FrontdoorTurnController(model_concurrency_controller=concurrency, balancer=balancer)
    return balancer, ftc


def _manager(scope_label: str = "local") -> MemoryManager:
    mgr = object.__new__(MemoryManager)
    mgr.scope_label = scope_label
    return mgr


def test_group_route_acquires_and_releases_per_batch() -> None:
    balancer, ftc = _source()
    set_memory_model_route_source(
        SimpleNamespace(balancer=balancer, turn_controller=ftc, concurrency_controller=ftc._model_concurrency_controller)
    )
    try:
        mgr = _manager("local")
        cfg = _config([{"type": "load_balance", "groupKey": "g1"}])

        async def run() -> None:
            chat_call, release = await mgr._build_memory_chat_call(cfg, [])
            assert callable(chat_call)
            # 批次内粘滞绑定存在。
            assert balancer.bound_model_for_node("memory:local") in {"m_a", "m_b"}
            release()
            # 每批动态：释放即解绑，下一批重新按当时负载选。
            assert balancer.bound_model_for_node("memory:local") == ""

        asyncio.run(run())
    finally:
        set_memory_model_route_source(None)


def test_no_source_falls_back_to_direct() -> None:
    set_memory_model_route_source(None)
    mgr = _manager("local")
    # 内联模型（无 llmConfigId）：build_chat_model 不查凭据记录即可构造。
    cfg = Config.model_validate(
        {
            "agents": {"defaults": {"workspace": "."}},
            "models": {
                "catalog": [
                    {"key": "m_x", "providerModel": "openai:x", "apiKey": "k", "contextWindowTokens": 200000},
                ],
                "roles": {"ceo": ["m_x"], "execution": ["m_x"], "inspection": ["m_x"], "memory": ["m_x"]},
            },
        }
    )

    async def run() -> None:
        chat_call, release = await mgr._build_memory_chat_call(cfg, [])
        assert callable(chat_call)
        release()  # no-op，不抛

    asyncio.run(run())


def test_group_scopes_have_distinct_binding_keys() -> None:
    balancer, ftc = _source()
    set_memory_model_route_source(
        SimpleNamespace(balancer=balancer, turn_controller=ftc, concurrency_controller=ftc._model_concurrency_controller)
    )
    try:
        cfg = _config([{"type": "load_balance", "groupKey": "g1"}])

        async def run() -> None:
            local = _manager("local")
            group = _manager("group-abc")
            _, release_local = await local._build_memory_chat_call(cfg, [])
            _, release_group = await group._build_memory_chat_call(cfg, [])
            assert balancer.bound_model_for_node("memory:local") in {"m_a", "m_b"}
            assert balancer.bound_model_for_node("memory:group-abc") in {"m_a", "m_b"}
            release_local()
            # 本地解绑不影响群绑定。
            assert balancer.bound_model_for_node("memory:local") == ""
            assert balancer.bound_model_for_node("memory:group-abc") in {"m_a", "m_b"}
            release_group()

        asyncio.run(run())
    finally:
        set_memory_model_route_source(None)


def test_attempted_refs_bridges_attempts_and_chain() -> None:
    # ConfigChatBackend 形状：attempts。
    response = SimpleNamespace(attempts=[SimpleNamespace(model_key="m_a"), SimpleNamespace(model_key="m_b")])
    assert MemoryManager._memory_attempted_refs(response) == ["m_a", "m_b"]
    # FallbackProvider 形状：attempted_model_chain 优先。
    response2 = SimpleNamespace(attempted_model_chain=["m_x"], attempts=[SimpleNamespace(model_key="m_a")])
    assert MemoryManager._memory_attempted_refs(response2) == ["m_x"]
    assert MemoryManager._memory_attempted_refs(SimpleNamespace()) == []
