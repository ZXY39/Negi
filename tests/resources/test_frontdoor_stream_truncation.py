"""流式响应未正常终止时的 frontdoor 重试契约。

上游在思考中途关闭 SSE 时不会给出 finish_reason，消费层此前一律按 "stop" 收尾，
运行时因此把断流当成正常完成，最终把内部兜底文案作为助手回复投递到外部渠道。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from g3ku.providers.base import LLMResponse
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
async def test_frontdoor_call_carries_stream_incomplete_onto_payload() -> None:
    async def _chat(**_kwargs):
        return LLMResponse(content=None, reasoning_content="想了一半", stream_incomplete=True)

    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    runner._resolve_chat_backend = lambda: SimpleNamespace(chat=_chat)

    payload = await runner._call_model_with_tools(
        messages=[{"role": "user", "content": "推送日报"}],
        tool_schemas=[],
        model_refs=["glm-5.2"],
        parallel_tool_calls=None,
        prompt_cache_key="",
    )

    assert payload["stream_incomplete"] is True
    assert payload["reasoning_content"] == "想了一半"
    assert payload["finish_reason"] == "stop"


def _view(**overrides) -> SimpleNamespace:
    payload = {
        "content": None,
        "tool_calls": [],
        "error_text": "",
        "error_kind": "",
        "reasoning_content": "思考内容",
        "thinking_blocks": None,
        "stream_incomplete": True,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def test_abort_ownership_moved_to_the_provider_so_the_frontdoor_has_no_second_predicate() -> None:
    """断流的归属收到 provider/模型链一层后，前门只留"正常收尾但全空"的重放道。

    钉住两件事：带终止故障标记的回包必然自带 error_text，因此不会被空响应重放道当成
    "模型返回空响应"再重发三次；而真正正常收尾却全空的响应仍归前门这条道。
    """
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    abort = _view(
        error_text="stream closed before finish_reason after 66 chunks",
        error_kind="StreamIncomplete",
    )
    assert runner._is_empty_model_response(abort) is False

    terminated_empty = _view(stream_incomplete=False, reasoning_content="", content=None)
    assert runner._is_empty_model_response(terminated_empty) is True

    # 正常收尾的 reasoning-only 响应仍按非空处理：它不是传输故障。
    assert runner._is_empty_model_response(_view(stream_incomplete=False)) is False


def _make_runner_state(monkeypatch: pytest.MonkeyPatch, runner: CreateAgentCeoFrontDoorRunner) -> dict:
    monkeypatch.setattr(runner, "_frontdoor_tool_schemas_for_state", lambda **_: [])
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
async def test_normalize_output_raises_chinese_error_on_stream_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """断流的归属已收到 provider/模型链一层：到这里时链内已换过槽位仍然失败。

    这里不再自己重发三次（那是"同一条请求打同一扇门"的版本），而是把内部英文取证串换成
    中文错误上抛，原文留在 raw_message 供 .g3ku/errors 与前端排障。
    """
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    ctx = _make_runner_state(monkeypatch, runner)
    state = dict(ctx["state"])
    state["response_payload"] = {
        "content": "核验结果充分，可以裁",
        "tool_calls": [],
        "finish_reason": "error",
        "error_text": "stream closed before finish_reason after 66 chunks",
        "error_kind": "StreamIncomplete",
        "stream_incomplete": True,
        "reasoning_content": "半截思考",
    }

    with pytest.raises(ModelProviderExhaustedError) as raised:
        await runner._graph_normalize_model_output(state, runtime=ctx["runtime"])

    # 渠道侧看到的是中文（ModelProviderExhaustedError 把 message 放进 str()），
    # 不是 provider 的内部英文串，也不是那半截回答。
    detail = str(raised.value)
    assert "响应流未正常终止" in detail
    assert "核验结果充分" not in detail
    assert "closed before finish_reason" in raised.value.raw_message


@pytest.mark.asyncio
async def test_graph_call_model_still_replays_terminated_empty_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常收尾却全空的响应仍归前门这条重放道：它不是传输故障，没带 error_text。"""
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    ctx = _make_runner_state(monkeypatch, runner)

    calls = [
        AIMessage(content="", additional_kwargs={}),
        AIMessage(content="今天的日报如下：", additional_kwargs={}),
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
