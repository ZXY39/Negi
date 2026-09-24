"""流式响应未正常终止时的 frontdoor 重试契约。

上游在思考中途关闭 SSE 时不会给出 finish_reason，消费层此前一律按 "stop" 收尾，
运行时因此把断流当成正常完成，最终把内部兜底文案作为助手回复投递到外部渠道。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from g3ku.providers.base import LLMResponse
from g3ku.providers.base_chat_model_adapter import G3kuChatModelAdapter
from g3ku.providers.fallback import ModelProviderExhaustedError
from g3ku.providers.streaming_timeouts import StreamingDiagnostics, consume_openai_like_chat_stream
from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor.state_models import CeoRuntimeContext


def _reasoning_only_chunks(count: int = 3) -> list[dict]:
    return [{"choices": [{"delta": {"reasoning_content": f"t{i}"}}]} for i in range(count)]


async def _aiter(chunks: list[dict]):
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_consumer_flags_stream_that_never_delivered_finish_reason() -> None:
    diagnostics = StreamingDiagnostics.start("openai_chat")

    content, tool_calls, finish_reason, usage, reasoning = await consume_openai_like_chat_stream(
        _aiter(_reasoning_only_chunks() + [{"usage": {"prompt_tokens": 7}}]),
        diagnostics=diagnostics,
        first_chunk_timeout_seconds=5,
        idle_chunk_timeout_seconds=5,
    )

    assert content is None
    assert tool_calls == []
    assert finish_reason == "stop"
    assert reasoning == "t0t1t2"
    assert diagnostics.finish_reason_seen is False


@pytest.mark.asyncio
async def test_consumer_records_finish_reason_when_stream_terminates() -> None:
    diagnostics = StreamingDiagnostics.start("openai_chat")
    chunks = _reasoning_only_chunks(1) + [
        {"choices": [{"delta": {"content": "日报正文"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"usage": {"prompt_tokens": 7, "completion_tokens": 3}},
    ]

    content, _tool_calls, finish_reason, usage, _reasoning = await consume_openai_like_chat_stream(
        _aiter(chunks),
        diagnostics=diagnostics,
        first_chunk_timeout_seconds=5,
        idle_chunk_timeout_seconds=5,
    )

    assert content == "日报正文"
    assert finish_reason == "stop"
    assert usage.get("input_tokens") == 7
    assert diagnostics.finish_reason_seen is True
    assert "finish_reason_seen=1" in diagnostics.render_summary(outcome="completed")


@pytest.mark.asyncio
async def test_diagnostics_histogram_tells_keepalive_only_stream_apart_from_thinking_stream() -> None:
    """长流卡在哪一侧由 kind 直方图判：chunk_count 只证明分片一直在到。

    上游可以挂着 SSE 滴十几分钟不携带任何增量而照常带 finish_reason 收尾，
    这种流既不会被 idle-chunk 超时截断，也不会留下可用的判读线索。
    """
    keepalive = StreamingDiagnostics.start("openai_chat")
    await consume_openai_like_chat_stream(
        _aiter(
            [{"usage": {"prompt_tokens": 7}}] * 3
            + [{"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        ),
        diagnostics=keepalive,
        first_chunk_timeout_seconds=5,
        idle_chunk_timeout_seconds=5,
    )
    keepalive_summary = keepalive.render_summary(outcome="completed")
    assert "first_text_delta_received_ms= " in keepalive_summary
    assert "chunk_kinds=chunk:1,non_choice_chunk:3" in keepalive_summary

    thinking = StreamingDiagnostics.start("openai_chat")
    await consume_openai_like_chat_stream(
        _aiter(_reasoning_only_chunks(2) + [{"choices": [{"delta": {"content": "正文"}}]}]),
        diagnostics=thinking,
        first_chunk_timeout_seconds=5,
        idle_chunk_timeout_seconds=5,
    )
    thinking_summary = thinking.render_summary(outcome="completed")
    assert "chunk_kinds=reasoning_delta:2,text_delta:1" in thinking_summary
    # 直方图必须与 chunk_count 同账，否则有 kind 漏记。
    kinds_total = sum(
        int(item.split(":")[1]) for item in thinking_summary.split("chunk_kinds=")[1].split()[0].split(",")
    )
    assert kinds_total == thinking.chunk_count


@pytest.mark.asyncio
async def test_adapter_carries_stream_incomplete_onto_ai_message() -> None:
    async def _chat(**_kwargs):
        return LLMResponse(content=None, reasoning_content="想了一半", stream_incomplete=True)

    adapter = G3kuChatModelAdapter(chat_backend=SimpleNamespace(chat=_chat), model_refs=["glm-5.2"])

    result = await adapter._agenerate([HumanMessage(content="推送日报")])

    message = result.generations[0].message
    assert message.additional_kwargs["stream_incomplete"] is True
    assert message.response_metadata["finish_reason"] == "stop"


def _view(**overrides) -> SimpleNamespace:
    payload = {
        "content": None,
        "tool_calls": [],
        "error_text": "",
        "reasoning_content": "思考内容",
        "thinking_blocks": None,
        "stream_incomplete": True,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def test_unterminated_empty_response_predicate_is_narrow() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    assert runner._is_unterminated_empty_response(_view()) is True
    # 有可用输出时一律不按截断处理：省略 finish_reason 的非规范 provider 不会被误伤。
    assert runner._is_unterminated_empty_response(_view(content="正文")) is False
    assert runner._is_unterminated_empty_response(_view(tool_calls=[{"name": "content_open"}])) is False
    assert runner._is_unterminated_empty_response(_view(error_text="boom")) is False
    # 正常收尾的 reasoning-only 响应保持既有行为，不进入重放。
    assert runner._is_unterminated_empty_response(_view(stream_incomplete=False)) is False


def _make_runner_state(monkeypatch: pytest.MonkeyPatch, runner: CreateAgentCeoFrontDoorRunner) -> dict:
    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **_: [])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "glm-5.2-3",
            "provider_model": "openai:glm-5.2",
            "context_window_tokens": 390000,
        },
        raising=False,
    )
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **_: 1000, raising=False)
    monkeypatch.setattr(runner, "_refresh_runtime_config_for_retry_invalidation", lambda: False)
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", lambda **_: {})

    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(ceo_runtime_ops.asyncio, "sleep", _sleep)

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _frontdoor_stage_state={"active_stage_id": "", "transition_required": False, "stages": []},
        _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
        _compression_state={},
        _semantic_context_state={},
        _frontdoor_hydrated_tool_names=[],
        _emit_state_snapshot=lambda: None,
    )
    runtime = SimpleNamespace(
        context=CeoRuntimeContext(loop=None, session=session, session_key="web:shared", on_progress=None)
    )
    state = {
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "推送日报"},
        ],
        "model_refs": ["glm-5.2-3"],
        "parallel_enabled": False,
        "prompt_cache_key": "cache-key",
        "iteration": 1,
        "max_iterations": 5,
        "session_key": "web:shared",
        "tool_names": [],
        "provider_tool_names": [],
        "candidate_tool_names": [],
        "candidate_tool_items": [],
        "hydrated_tool_names": [],
        "visible_skill_ids": [],
        "candidate_skill_ids": [],
        "rbac_visible_tool_names": [],
        "rbac_visible_skill_ids": [],
        "turn_overlay_text": "",
        "repair_overlay_text": None,
        "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
        "frontdoor_history_shrink_reason": "",
        "frontdoor_token_preflight_diagnostics": {},
    }
    return {"runner": runner, "state": state, "runtime": runtime}


@pytest.mark.asyncio
async def test_graph_call_model_replays_until_terminated_response(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    ctx = _make_runner_state(monkeypatch, runner)

    calls = [
        AIMessage(content="", additional_kwargs={"reasoning_content": "半截思考", "stream_incomplete": True}),
        AIMessage(content="今天的日报如下：", additional_kwargs={"reasoning_content": "想完了"}),
    ]
    seen: list[int] = []

    async def _call_model_with_tools(**_kwargs):
        seen.append(len(seen))
        return calls[len(seen) - 1]

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)

    result = await runner._graph_call_model(ctx["state"], runtime=ctx["runtime"])

    assert len(seen) == 2
    assert result["empty_response_retry_count"] == 1
    assert result["response_payload"]["content"] == "今天的日报如下："
    assert result["response_payload"]["stream_incomplete"] is False


@pytest.mark.asyncio
async def test_graph_call_model_raises_when_truncated_replies_exhaust_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    ctx = _make_runner_state(monkeypatch, runner)

    attempts: list[int] = []

    async def _call_model_with_tools(**_kwargs):
        attempts.append(1)
        return AIMessage(content="", additional_kwargs={"reasoning_content": "半截思考", "stream_incomplete": True})

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)

    with pytest.raises(ModelProviderExhaustedError) as raised:
        await runner._graph_call_model(ctx["state"], runtime=ctx["runtime"])

    assert len(attempts) == ceo_runtime_ops._PROVIDER_RETRY_LIMIT
    detail = str(raised.value)
    assert "响应流未正常终止" in detail
    assert "3 次" in detail
    # 内部兜底文案不再作为助手回复出现在这条路径上。
    assert "pretending a successful reply" not in detail


@pytest.mark.asyncio
async def test_graph_call_model_still_returns_cleanly_stopped_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常收尾但只有思考的响应不在本次契约内：不重试，交给既有 finalize 分支。"""
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    ctx = _make_runner_state(monkeypatch, runner)

    attempts: list[int] = []

    async def _call_model_with_tools(**_kwargs):
        attempts.append(1)
        return AIMessage(content="", additional_kwargs={"reasoning_content": "想清楚了但没说话"})

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)

    result = await runner._graph_call_model(ctx["state"], runtime=ctx["runtime"])

    assert len(attempts) == 1
    assert result["empty_response_retry_count"] == 0
    assert result["response_payload"]["stream_incomplete"] is False
