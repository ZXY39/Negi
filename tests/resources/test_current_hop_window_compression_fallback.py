"""当前跳声明窗口压缩 + 失败 fallback（会话 / 节点统一）合同用例。

覆盖：
- 前门预检 0.8× 压缩 provider 失败 → 当前成员跳过、`initial_tried_model_refs` 带上链首；
- 预检 capacity/system 失败 → 仍然硬停；
- send-layer 闭包按当前成员声明窗口压缩、成功/失败/hunk 号/turn flag；
- helper 递归守卫：压缩 helper 调用不再带 shortfall compressor；
- 允许重复压缩：第二次输入包含第一次摘要；
- 节点预检 provider 失败不再 pause，返回 skip 信号 + 预估值/块号；
- 节点短压失败返回带 chunk 号的 failure dict；
- 节点终态心跳正文包含每成员压缩失败原因。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.providers.base import LLMResponse
from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor.state_models import CeoRuntimeContext
from g3ku.runtime.frontdoor.token_preflight_compaction import FrontdoorTokenPreflightResult
from main.runtime import react_loop as react_loop_module
from main.runtime.chat_backend import SendModelContextWindowInfo
from main.runtime.react_loop import ReActToolLoop


async def _announce_boundary_noop(**_kwargs) -> None:
    return None


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        announce_context_compression_boundary=_announce_boundary_noop,
        _compression_state={},
        _semantic_context_state={},
        _emit_state_snapshot=lambda: None,
    )


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        context=CeoRuntimeContext(
            loop=None,
            session=_session(),
            session_key="web:shared",
            on_progress=None,
        )
    )


def _graph_state(model_refs: list[str]) -> dict:
    return {
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "older-1"},
            {"role": "assistant", "content": "older-2"},
            {"role": "user", "content": "older-3"},
            {"role": "assistant", "content": "older-4"},
            {"role": "user", "content": "recent-1"},
            {"role": "assistant", "content": "recent-2"},
            {"role": "user", "content": "recent-3"},
            {"role": "assistant", "content": "recent-4"},
        ],
        "model_refs": list(model_refs),
        "parallel_enabled": False,
        "prompt_cache_key": "cache-key",
        "iteration": 0,
        "max_iterations": 3,
        "session_key": "web:shared",
        "tool_names": [],
        "provider_tool_names": [],
        "candidate_tool_names": [],
        "candidate_tool_items": [],
        "visible_skill_ids": [],
        "candidate_skill_ids": [],
        "rbac_visible_tool_names": [],
        "rbac_visible_skill_ids": [],
        "turn_overlay_text": "",
        "repair_overlay_text": None,
        "frontdoor_history_shrink_reason": "",
        "frontdoor_token_preflight_diagnostics": {},
    }


def _build_graph_runner(monkeypatch: pytest.MonkeyPatch) -> CreateAgentCeoFrontDoorRunner:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_frontdoor_tool_schemas_for_state", lambda **_: [], raising=False)
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "head",
            "provider_model": "openai:deepseek-flash",
            "context_window_tokens": 32_000,
        },
        raising=False,
    )
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **_: 33_001, raising=False)
    monkeypatch.setattr(runner, "_checkpoint_safe_model_response_payload", lambda _m: {"ok": True}, raising=False)
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", lambda **_: {}, raising=False)
    return runner


@pytest.mark.asyncio
async def test_preflight_provider_failure_skips_current_member_for_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _build_graph_runner(monkeypatch)
    captured: dict = {}

    async def _compression(**kwargs):
        return FrontdoorTokenPreflightResult(
            request_messages=list(kwargs.get("request_messages") or []),
            final_request_tokens=33_001,
            history_shrink_reason="",
            diagnostics={
                "applied": False,
                "failure_kind": "provider",
                "compression_pass_failed": "chunk 2/3: HTTP 429: token plan entitlement exhausted",
                "compression_provider_error": "chunk 2/3: HTTP 429: token plan entitlement exhausted",
                "chunk_index": 2,
                "chunk_count": 3,
            },
        )

    monkeypatch.setattr(runner, "_run_frontdoor_llm_token_compression", _compression)

    async def _call_model_with_tools(**kwargs):
        captured.update(kwargs)
        return {"content": "主请求回复", "finish_reason": "stop", "tool_calls": []}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)

    result = await runner._graph_call_model(_graph_state(["head", "lite"]), runtime=_runtime())

    assert captured.get("initial_tried_model_refs") == ["head"]
    assert captured.get("window_shortfall_compressor") is not None
    assert result["frontdoor_history_shrink_reason"] == ""


@pytest.mark.asyncio
async def test_preflight_capacity_failure_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _build_graph_runner(monkeypatch)
    called = False

    async def _compression(**kwargs):
        return FrontdoorTokenPreflightResult(
            request_messages=list(kwargs.get("request_messages") or []),
            final_request_tokens=33_001,
            history_shrink_reason="",
            diagnostics={
                "applied": False,
                "failure_kind": "capacity",
                "compression_pass_failed": "token compression skipped: per-chunk capacity is 0",
            },
        )

    monkeypatch.setattr(runner, "_run_frontdoor_llm_token_compression", _compression)

    async def _call_model_with_tools(**kwargs):
        nonlocal called
        called = True
        return {"content": "主请求回复", "finish_reason": "stop", "tool_calls": []}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)

    with pytest.raises(ceo_runtime_ops.FrontdoorCompressionRuntimeError):
        await runner._graph_call_model(_graph_state(["head", "lite"]), runtime=_runtime())
    assert called is False


@pytest.mark.asyncio
async def test_send_layer_compressor_targets_current_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    seen: dict = {}

    async def _compression(**kwargs):
        seen.update(kwargs)
        assert runner._frontdoor_compression_in_progress is True
        return FrontdoorTokenPreflightResult(
            request_messages=[{"role": "assistant", "content": "压缩后的摘要"}],
            final_request_tokens=200_000,
            history_shrink_reason="token_compression",
            diagnostics={
                "applied": True,
                "mode": "llm_chunked",
                "chunk_count": 2,
                "chunk_failed_count": 0,
            },
        )

    monkeypatch.setattr(runner, "_run_frontdoor_llm_token_compression", _compression)
    compressor = runner._build_frontdoor_window_shortfall_compressor(
        state={"session_key": "web:shared"},
        runtime=SimpleNamespace(),
        tool_schemas=[],
    )

    result = await compressor([{"role": "user", "content": "original"}], 240_000, "lite")

    assert seen.get("model_refs") == ["lite"]
    assert seen.get("compression_source") == "window_shortfall"
    assert result == {
        "messages": [{"role": "assistant", "content": "压缩后的摘要"}],
        "estimated_tokens": 200_000,
        "compression_mode": "llm_chunked",
        "chunk_count": 2,
        "chunk_failed_count": 0,
    }
    assert runner._frontdoor_compression_in_progress is False


@pytest.mark.asyncio
async def test_send_layer_compressor_provider_failure_carries_chunk_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    async def _compression(**kwargs):
        return FrontdoorTokenPreflightResult(
            request_messages=list(kwargs.get("request_messages") or []),
            final_request_tokens=266_765,
            history_shrink_reason="",
            diagnostics={
                "applied": False,
                "failure_kind": "compression",
                "compression_pass_failed": "chunk 2/3: HTTP 429",
                "chunk_failed_indexes": [2],
                "chunk_count": 3,
            },
        )

    monkeypatch.setattr(runner, "_run_frontdoor_llm_token_compression", _compression)
    compressor = runner._build_frontdoor_window_shortfall_compressor(
        state={"session_key": "web:shared"},
        runtime=SimpleNamespace(),
        tool_schemas=[],
    )

    result = await compressor([{"role": "user", "content": "original"}], 240_000, "lite")

    failure = (result or {}).get("failure") or {}
    assert failure.get("kind") == "compression"
    assert int(failure.get("chunk_index") or 0) == 2
    assert int(failure.get("chunk_planned") or 0) == 3
    assert "chunk 2/3" in str(failure.get("detail") or "")


@pytest.mark.asyncio
async def test_frontdoor_helper_call_disables_shortfall_compressor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    seen: dict = {}

    class _Backend:
        async def chat(self, **kwargs):
            seen.update(kwargs)
            return LLMResponse(content="ok", finish_reason="stop")

    monkeypatch.setattr(runner, "_resolve_chat_backend", lambda: _Backend())
    runner._frontdoor_compression_in_progress = True
    try:
        await runner._call_model_with_tools(
            messages=[{"role": "user", "content": "compression helper"}],
            tool_schemas=[],
            model_refs=["lite"],
            parallel_tool_calls=None,
            prompt_cache_key="",
            window_shortfall_compressor=object(),
            on_window_shortfall_compressed=object(),
            initial_tried_model_refs=["head"],
        )
    finally:
        runner._frontdoor_compression_in_progress = False

    assert seen.get("window_shortfall_compressor") is None
    assert seen.get("on_window_shortfall_compressed") is None
    assert seen.get("initial_tried_model_refs") is None


@pytest.mark.asyncio
async def test_double_compression_second_input_contains_first_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    inputs: list[list[dict]] = []

    async def _compression(**kwargs):
        messages = [dict(item) for item in list(kwargs.get("request_messages") or [])]
        inputs.append(messages)
        marker = "FIRST_SUMMARY" if len(inputs) == 1 else "SECOND_SUMMARY"
        return FrontdoorTokenPreflightResult(
            request_messages=[{"role": "assistant", "content": marker}],
            final_request_tokens=150_000 if len(inputs) == 1 else 120_000,
            history_shrink_reason="token_compression",
            diagnostics={"applied": True, "mode": "llm_chunked", "chunk_count": 2},
        )

    monkeypatch.setattr(runner, "_run_frontdoor_llm_token_compression", _compression)
    compressor = runner._build_frontdoor_window_shortfall_compressor(
        state={"session_key": "web:shared"},
        runtime=SimpleNamespace(),
        tool_schemas=[],
    )

    first = await compressor([{"role": "user", "content": "original"}], 300_000, "mid")
    second = await compressor(list((first or {}).get("messages") or []), 240_000, "lite")

    assert len(inputs) == 2
    assert any("FIRST_SUMMARY" in str(item.get("content") or "") for item in inputs[1])
    assert (second or {}).get("messages") == [{"role": "assistant", "content": "SECOND_SUMMARY"}]


def _preflight_estimate_payload() -> dict:
    return {
        "context_window_tokens": 240_000,
        "estimated_total_tokens": 266_765,
        "final_estimate_tokens": 266_765,
        "trigger_tokens": 192_000,
        "effective_trigger_tokens": 182_400,
        "ratio": 1.11,
        "would_exceed_context_window": True,
        "would_trigger_token_compression": True,
        "preview_estimate_tokens": 266_765,
        "usage_based_estimate_tokens": 0,
        "delta_estimate_tokens": 0,
        "effective_input_tokens": 0,
        "estimate_source": "preview_estimate",
        "comparable_to_previous_request": False,
    }


@pytest.mark.asyncio
async def test_node_preflight_provider_failure_returns_skip_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = ReActToolLoop(
        chat_backend=SimpleNamespace(),
        log_service=SimpleNamespace(_store=SimpleNamespace(get_task=lambda _task_id: None)),
    )
    monkeypatch.setattr(react_loop_module, "get_runtime_config", lambda **_kwargs: (object(), 0, False))
    monkeypatch.setattr(
        react_loop_module.runtime_chat_backend,
        "resolve_send_model_context_window_info",
        lambda **_kwargs: SendModelContextWindowInfo(
            model_key="member-a",
            provider_id="openai",
            provider_model="openai:lite",
            resolved_model="lite",
            context_window_tokens=240_000,
        ),
    )
    monkeypatch.setattr(loop, "_estimate_node_send_preflight_tokens", lambda **_kwargs: _preflight_estimate_payload())

    async def _compression(**_kwargs):
        return "", {
            "history_message_count": 40,
            "compression_mode": "llm_chunked",
            "chunk_count": 3,
            "chunk_failed_indexes": [2],
            "chunk_failed_count": 1,
            "chunk_error_text": "chunk 2/3: HTTP 429: token plan entitlement exhausted",
            "failure_kind": "provider",
        }

    monkeypatch.setattr(loop, "_request_node_token_compression_summary", _compression)

    original = [{"role": "user", "content": "original"}]
    messages, diagnostics, shrink_reason, failure_reason = (
        await loop._apply_node_send_token_preflight(
            task_id="task:test",
            node_id="node:test",
            model_refs=["member-a"],
            request_messages=original,
            tool_schemas=[],
            hop_ruler={"model_key": "member-a", "windows": [240_000]},
        )
    )

    assert failure_reason == ""
    assert shrink_reason == ""
    assert messages == original
    assert diagnostics.get("preflight_skip_model_keys") == ["member-a"]
    reasons = list(diagnostics.get("window_skip_reasons") or [])
    assert reasons and reasons[0].get("chunk_index") == 2
    assert reasons[0].get("chunk_planned") == 3
    assert "HTTP 429" in str(reasons[0].get("detail") or "")


@pytest.mark.asyncio
async def test_node_shortfall_compressor_returns_failure_with_chunk_index() -> None:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=SimpleNamespace())

    async def _compression(**_kwargs):
        return "", {
            "history_message_count": 40,
            "compression_mode": "llm_chunked",
            "chunk_count": 3,
            "chunk_failed_indexes": [2],
            "chunk_failed_count": 1,
            "chunk_error_text": "chunk 2/3: HTTP 429: token plan entitlement exhausted",
            "failure_kind": "provider",
        }

    loop._request_node_token_compression_summary = _compression
    compressor = loop._node_window_shortfall_compressor(
        task_id="task:test",
        node_id="node:test",
        tool_schemas=[],
    )

    result = await compressor(
        [{"role": "user", "content": "original"}],
        240_000,
        "member-a",
        ("member-a",),
    )

    failure = (result or {}).get("failure") or {}
    assert failure.get("kind") == "provider"
    assert int(failure.get("chunk_index") or 0) == 2
    assert int(failure.get("chunk_planned") or 0) == 3
    assert "HTTP 429" in str(failure.get("detail") or "")


def test_node_terminal_heartbeat_includes_compression_skip_reasons() -> None:
    result = ReActToolLoop._token_preflight_failure(
        reason="no model can hold this request",
        diagnostics={
            "estimated_total_tokens": 266_765,
            "context_window_tokens": 240_000,
            "window_skip_reasons": [
                {
                    "model_ref": "member-a",
                    "reason_kind": "provider",
                    "chunk_index": 2,
                    "chunk_planned": 3,
                    "detail": "HTTP 429: token plan entitlement exhausted",
                }
            ],
        },
    )

    assert "预估=266765" in result.blocking_reason
    assert "本轮发送窗口=240000" in result.blocking_reason
    assert "压缩失败成员=member-a:provider chunk 2/3" in result.blocking_reason
    assert "HTTP 429" in result.blocking_reason
