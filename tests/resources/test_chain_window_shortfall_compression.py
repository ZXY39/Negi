"""链前进到更窄的一档时，按那一档的窗口压缩请求体，而不是把候选拒掉。

实盘形状（2026-10-26）：预检按链首组（390,000）判定不用压，请求体 252,316；链首组
429 后前进到尾组（240,000），准入按体量把整组筛空，发送层连一个候选都拿不到，
最后把上一跳的 429 原样抛成节点停车。这里固定三条行为：

1. direct 链：塞不下的候选先回调压缩器，压完仍用这颗发；
2. 负载均衡组：整组被窗口筛空时，按组内最宽可用成员窗口压一次再重绑；
3. 没有压缩器或压缩失败：保留原来的"留到最后兜底"语义，不新增失败面。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import main.runtime.chat_backend as chat_backend_module
from g3ku.providers.base import LLMResponse
from g3ku.providers.provider_factory import ProviderTarget
from main.runtime.model_route import (
    MODEL_ROUTE_KIND_LOAD_BALANCE,
    ModelRouteLease,
    ModelRoutePlan,
    ResolvedLoadBalanceGroup,
    ResolvedModelRoute,
    RouteCandidateFilters,
    RouteMemberView,
)
from main.runtime.node_turn_controller import NodeTurnLease
from main.runtime.react_loop import _NODE_TOKEN_COMPACT_MARKER, ReActToolLoop


class _OkProvider:
    def __init__(self, model_key: str, calls: list[tuple[str, list[dict]]], *, failing: bool = False) -> None:
        self.model_key = model_key
        self.calls = calls
        self.failing = bool(failing)

    async def chat(self, **kwargs):
        messages = [dict(item) for item in list(kwargs.get("messages") or [])]
        self.calls.append((self.model_key, messages))
        if self.failing:
            return LLMResponse(
                content="",
                finish_reason="error",
                error_text="HTTP 403: forbidden",
                error_status=403,
                error_kind="AuthError",
            )
        return LLMResponse(content="ok", finish_reason="stop")


CALLS: list[tuple[str, list[dict]]] = []


def _target(model_key: str, *, window: int, failing: bool = False) -> ProviderTarget:
    return ProviderTarget(
        provider_ref=model_key,
        provider_id="custom",
        model_id=f"{model_key}-model",
        provider=_OkProvider(model_key, CALLS, failing=failing),
        model_parameters={"context_window_tokens": int(window)},
        retry_on=["network", "429", "502"],
        retry_count=0,
        api_key_count=1,
        api_key_indexes=[0],
    )


def _patch(monkeypatch, windows: dict[str, int], *, failing: tuple[str, ...] = ()) -> None:
    CALLS.clear()
    targets = {
        key: _target(key, window=window, failing=key in failing)
        for key, window in windows.items()
    }

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return targets[str(model_key)]

    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)


def _backend() -> chat_backend_module.ConfigChatBackend:
    return chat_backend_module.ConfigChatBackend(
        config=SimpleNamespace(get_model_runtime_profile=lambda ref: None)
    )


@pytest.mark.asyncio
async def test_direct_model_over_window_is_compressed_then_sent(monkeypatch) -> None:
    _patch(monkeypatch, {"m_small": 100})
    seen: list[tuple[int, str]] = []
    notifications: list[dict] = []

    async def _compress(messages, target_window_tokens, model_ref, candidate_refs=()):
        _ = candidate_refs
        seen.append((int(target_window_tokens), str(model_ref)))
        assert messages == [{"role": "user", "content": "original"}]
        return {
            "messages": [{"role": "user", "content": "compressed"}],
            "estimated_tokens": 90,
            "compression_mode": "llm_chunked",
            "chunk_count": 2,
        }

    response = await _backend().chat(
        messages=[{"role": "user", "content": "original"}],
        tools=None,
        model_refs=["m_small"],
        required_context_window_tokens=200,
        window_shortfall_compressor=_compress,
        on_window_shortfall_compressed=notifications.append,
    )

    assert response.content == "ok"
    assert seen == [(100, "m_small")]
    assert [item[0] for item in CALLS] == ["m_small"]
    assert CALLS[0][1] == [{"role": "user", "content": "compressed"}]
    assert notifications == [
        {
            "messages": [{"role": "user", "content": "compressed"}],
            "estimated_tokens": 90,
            "required_before": 200,
            "target_window_tokens": 100,
            "model_ref": "m_small",
            "compression_mode": "llm_chunked",
            "chunk_count": 2,
            "chunk_failed_count": 0,
        }
    ]


@pytest.mark.asyncio
async def test_direct_model_compression_failure_keeps_legacy_fallback(monkeypatch) -> None:
    _patch(monkeypatch, {"m_small": 100})

    async def _compress(messages, target_window_tokens, model_ref, candidate_refs=()):
        _ = messages, target_window_tokens, model_ref, candidate_refs
        return None

    response = await _backend().chat(
        messages=[{"role": "user", "content": "original"}],
        tools=None,
        model_refs=["m_small"],
        required_context_window_tokens=200,
        window_shortfall_compressor=_compress,
    )

    assert response.content == "ok"
    assert [item[0] for item in CALLS] == ["m_small"]
    assert CALLS[0][1] == [{"role": "user", "content": "original"}]


class _FakeController:
    """只实现 chat_backend 用到的重绑接口；窗口筛选口径与 balancer 一致。"""

    def __init__(self, windows: dict[str, int], *, capacity_blocked: bool = False) -> None:
        self.windows = dict(windows)
        self.capacity_blocked = bool(capacity_blocked)
        self.requested_required_windows: list[int] = []
        self._lease_id = 0

    def rebind_turn(
        self,
        lease,
        *,
        route_index: int,
        filters=None,
        excluded_model_keys=frozenset(),
        rebind_reason: str = "",
    ):
        _ = rebind_reason
        effective = filters or lease.route_filters or RouteCandidateFilters()
        required = int(getattr(effective, "required_context_window_tokens", 0) or 0)
        self.requested_required_windows.append(required)
        candidates = [
            key
            for key, window in self.windows.items()
            if key not in excluded_model_keys and (required <= 0 or int(window) >= required)
        ]
        if self.capacity_blocked:
            candidates = []
        if not candidates:
            lease.initial_model_permit = None
            return None
        selected = candidates[0]
        self._lease_id += 1
        lease.route_index = int(route_index)
        lease.group_key = "tail"
        lease.model_ref = selected
        lease.route_filters = effective
        lease.route_lease = ModelRouteLease(
            lease_id=self._lease_id,
            node_id=lease.node_id,
            task_id=lease.task_id,
            route_index=int(route_index),
            group_key="tail",
            model_key=selected,
            key_index=0,
            quota_bucket_key=f"key:{selected}",
            selection_reason="test",
            config_revision=1,
        )
        return lease.route_lease

    def record_route_request_start(self, lease) -> None:
        _ = lease

    def record_route_outcome(self, lease, *, status_code=None, error_text="") -> None:
        _ = lease, status_code, error_text

    def release_route_lease(self, lease, *, outcome="", error="") -> None:
        _ = lease, outcome, error


def _tail_plan(
    *members: str,
    window: int = 100,
    windows: dict[str, int] | None = None,
) -> ModelRoutePlan:
    window_overrides = dict(windows or {})
    group = ResolvedLoadBalanceGroup(
        group_key="tail",
        enabled=True,
        max_retry_rounds=1,
        members=[
            RouteMemberView(
                model_key=key,
                enabled=True,
                context_window_tokens=int(window_overrides.get(key, window)),
            )
            for key in members
        ],
    )
    route = ResolvedModelRoute(
        index=0,
        kind=MODEL_ROUTE_KIND_LOAD_BALANCE,
        group_key="tail",
        group=group,
        candidates=tuple(members),
    )
    return ModelRoutePlan(routes=[route])


def _tail_lease(plan: ModelRoutePlan, *, required: int) -> NodeTurnLease:
    return NodeTurnLease(
        lease_id=1,
        task_id="task-1",
        node_id="node-1",
        model_ref="s1",
        key_index=0,
        acquired_at="2026-10-26T00:00:00+08:00",
        route_index=0,
        group_key="tail",
        route_plan=plan,
        route_lease=None,
        route_filters=RouteCandidateFilters(required_context_window_tokens=required),
    )


@pytest.mark.asyncio
async def test_group_screened_empty_by_window_is_compressed_then_rebound(monkeypatch) -> None:
    _patch(monkeypatch, {"s1": 100, "s2": 100})
    plan = _tail_plan("s1", "s2")
    controller = _FakeController({"s1": 100, "s2": 100})
    seen: list[tuple[int, tuple[str, ...]]] = []

    async def _compress(messages, target_window_tokens, model_ref, candidate_refs=()):
        _ = messages, model_ref
        seen.append((int(target_window_tokens), tuple(candidate_refs)))
        return {
            "messages": [{"role": "user", "content": "compressed-for-tail"}],
            "estimated_tokens": 90,
            "compression_mode": "llm_chunked",
            "chunk_count": 2,
        }

    response = await _backend().chat(
        messages=[{"role": "user", "content": "original"}],
        tools=None,
        model_refs=["s1"],
        model_routes=plan,
        node_turn_controller=controller,
        node_turn_lease=_tail_lease(plan, required=200),
        required_context_window_tokens=200,
        window_shortfall_compressor=_compress,
    )

    assert response.content == "ok"
    assert seen == [(100, ("s1", "s2"))]
    assert controller.requested_required_windows == [200, 90]
    assert [item[0] for item in CALLS] == ["s1"]
    assert CALLS[0][1] == [{"role": "user", "content": "compressed-for-tail"}]


@pytest.mark.asyncio
async def test_group_with_capacity_reason_is_not_compressed(monkeypatch) -> None:
    """最宽窗口仍装得下时不压：组给不出成员是容量/成员耗尽，不是窗口。"""
    _patch(monkeypatch, {"s1": 300, "s2": 300})
    plan = _tail_plan("s1", "s2", window=300)
    controller = _FakeController({"s1": 300, "s2": 300}, capacity_blocked=True)
    calls: list[int] = []

    async def _compress(messages, target_window_tokens, model_ref, candidate_refs=()):
        _ = messages, model_ref, candidate_refs
        calls.append(int(target_window_tokens))
        return None

    with pytest.raises(Exception):
        await _backend().chat(
            messages=[{"role": "user", "content": "original"}],
            tools=None,
            model_refs=["s1"],
            model_routes=plan,
            node_turn_controller=controller,
            node_turn_lease=_tail_lease(plan, required=200),
            required_context_window_tokens=200,
            window_shortfall_compressor=_compress,
        )

    assert calls == []


@pytest.mark.asyncio
async def test_group_rotates_to_narrower_member_after_wide_member_fails(monkeypatch) -> None:
    """异构组：宽成员 403 后，窄成员不该因为上一发太大而整组判死。"""
    windows = {"s_wide": 300, "s_narrow": 100}
    _patch(monkeypatch, windows, failing=("s_wide",))
    plan = _tail_plan("s_wide", "s_narrow", windows=windows)
    controller = _FakeController(windows)
    compressed_targets: list[int] = []

    async def _compress(messages, target_window_tokens, model_ref, candidate_refs=()):
        _ = messages, model_ref, candidate_refs
        compressed_targets.append(int(target_window_tokens))
        return {
            "messages": [{"role": "user", "content": "compressed-for-narrow"}],
            "estimated_tokens": 90,
            "compression_mode": "llm_chunked",
            "chunk_count": 1,
        }

    response = await _backend().chat(
        messages=[{"role": "user", "content": "original"}],
        tools=None,
        model_refs=["s_wide"],
        model_routes=plan,
        node_turn_controller=controller,
        node_turn_lease=_tail_lease(plan, required=200),
        required_context_window_tokens=200,
        window_shortfall_compressor=_compress,
    )

    assert response.content == "ok"
    assert compressed_targets == [100]
    assert [item[0] for item in CALLS] == ["s_wide", "s_narrow"]
    assert CALLS[-1][1] == [{"role": "user", "content": "compressed-for-narrow"}]


class _Resp:
    def __init__(self, content: str = "", error_text: str = "") -> None:
        self.content = content
        self.error_text = error_text
        self.usage: dict = {}
        self.attempts: list = []
        self.provider_request_meta: dict = {}
        self.provider_request_body: dict = {}


def _node_history(pairs: int) -> list[dict]:
    messages: list[dict] = [
        {"role": "system", "content": "你是执行节点。" * 20},
        {"role": "user", "content": "任务目标：读完大文件后汇总。" + "目标" * 100},
    ]
    for index in range(pairs):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {
                            "name": "exec",
                            "arguments": json.dumps({"command": "echo " + "实测数值与路径记录。" * 200}),
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "name": "exec",
                "content": "结果" + "。" * 400,
            }
        )
    return messages


@pytest.mark.asyncio
async def test_node_shortfall_compressor_rewrites_body_to_target_window() -> None:
    """节点侧回调必须真的走分片压缩，并把重写后的请求体与体量交回发送层。"""
    calls: list[list[dict]] = []

    async def fake_chat(*args, messages=None, **kwargs):
        _ = args, kwargs
        calls.append(list(messages or []))
        return _Resp(content="【摘要】保留结论与路径。")

    log_service = SimpleNamespace(
        _store=SimpleNamespace(get_task=lambda task_id: None),
    )
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service)
    loop._chat_with_optional_extensions = fake_chat
    loop._resolved_model_response_timeout_seconds = lambda **_: 30.0
    loop._is_empty_model_response = lambda r: not str(getattr(r, "content", "") or "").strip()
    loop._node_token_compression_task_goal = lambda **_: "探针任务目标"
    loop._node_compression_must_preserve_records = lambda **_: []

    compressor = loop._node_window_shortfall_compressor(
        task_id="task:probe",
        node_id="node:probe",
        tool_schemas=[],
    )
    result = await compressor(
        _node_history(pairs=12),
        12_000,
        "seva-member",
        ("seva-member",),
    )

    assert result is not None
    assert result["target_window_tokens"] == 12_000
    assert result["estimated_tokens"] > 0
    assert result["compression_mode"] == "llm_chunked"
    assert result["chunk_count"] >= 2
    assert calls
    rewritten = result["messages"]
    assert any(
        str(message.get("content") or "").startswith(_NODE_TOKEN_COMPACT_MARKER)
        for message in rewritten
    )
    assert any("【摘要】" in str(message.get("content") or "") for message in rewritten)
