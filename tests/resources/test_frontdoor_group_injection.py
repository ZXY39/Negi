from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from g3ku.config.schema import Config
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from main.runtime.frontdoor_turn_controller import FrontdoorTurnController
from main.runtime.model_key_concurrency import ModelKeyConcurrencyController
from main.runtime.model_load_balancer import ModelLoadBalancer
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


class _FakeBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(content="ok", tool_calls=[], finish_reason="stop", error_text="", usage={})


def _ops(cfg: Config, *, pinned: str = "", chain: list[str] | None = None) -> tuple[CeoFrontDoorRuntimeOps, _FakeBackend, FrontdoorTurnController]:
    clock = _Clock()
    concurrency = ModelKeyConcurrencyController(
        resolve_model_limits=lambda ref: {"key_indexes": [0], "per_key_limits": {0: None}}
    )
    balancer = ModelLoadBalancer(permit_source=concurrency, resolve_quota_buckets=lambda key: [f"key:{key}"], clock=clock)
    balancer.configure(groups={"g1": _group("g1", "m_a", "m_b")}, config_revision=1)
    ftc = FrontdoorTurnController(model_concurrency_controller=concurrency, balancer=balancer)
    service = SimpleNamespace(
        frontdoor_turn_controller=ftc,
        model_load_balancer=balancer,
        model_key_concurrency_controller=concurrency,
    )
    ops = object.__new__(CeoFrontDoorRuntimeOps)
    ops._loop = SimpleNamespace(app_config=cfg, main_task_service=service, sessions=None)
    ops._session_pinned_model_ref = lambda key: pinned
    ops._session_model_chain_refs = lambda key: list(chain or [])
    ops._frontdoor_runtime_config_revision = lambda: 1
    backend = _FakeBackend()
    ops._resolve_chat_backend = lambda: backend
    ops._model_response_payload_dict = lambda response: {"content": response.content}
    return ops, backend, ftc


def _call(ops: CeoFrontDoorRuntimeOps, cfg: Config, session_key: str | None) -> None:
    asyncio.run(
        ops._call_model_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tool_schemas=[],
            model_refs=cfg.get_role_model_keys("ceo"),
            parallel_tool_calls=None,
            prompt_cache_key="",
            session_key=session_key,
        )
    )


def test_group_chain_injects_route_plan_and_lease() -> None:
    cfg = _config([{"type": "load_balance", "groupKey": "g1"}])
    ops, backend, ftc = _ops(cfg)
    _call(ops, cfg, "web:s1")
    assert len(backend.calls) == 1
    kwargs = backend.calls[0]
    assert kwargs.get("model_routes") is not None
    assert kwargs.get("node_turn_lease") is not None
    assert kwargs.get("node_turn_controller") is ftc
    assert kwargs["model_refs"] == ["m_a", "m_b"]
    # lease 在 finally 释放，不留占用。
    assert ftc._running_leases == {}


def test_session_override_stays_flat() -> None:
    cfg = _config([{"type": "load_balance", "groupKey": "g1"}])
    ops, backend, ftc = _ops(cfg, pinned="m_x")
    _call(ops, cfg, "web:s1")
    kwargs = backend.calls[0]
    assert "model_routes" not in kwargs
    assert "node_turn_lease" not in kwargs


def test_no_group_chain_stays_flat() -> None:
    cfg = _config(["m_x"])
    ops, backend, ftc = _ops(cfg)
    _call(ops, cfg, "web:s1")
    kwargs = backend.calls[0]
    assert "model_routes" not in kwargs
    assert kwargs["model_refs"] == ["m_x"]


def test_no_session_key_stays_flat() -> None:
    cfg = _config([{"type": "load_balance", "groupKey": "g1"}])
    ops, backend, ftc = _ops(cfg)
    _call(ops, cfg, None)
    kwargs = backend.calls[0]
    assert "model_routes" not in kwargs
