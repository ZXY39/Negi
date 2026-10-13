"""frontdoor token 压缩回归：append-only 形态、空响应重试、分块压缩。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor.state_models import CeoRuntimeContext


def _build_runner(monkeypatch: pytest.MonkeyPatch, *, context_window_tokens: int = 100_000):
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_model": "openai:gpt-5.2",
            "context_window_tokens": context_window_tokens,
        },
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "_build_frontdoor_provider_request_body_preview",
        lambda **_: {},
        raising=False,
    )
    monkeypatch.setattr(runner, "_persist_frontdoor_internal_request_artifact", lambda **_: {}, raising=False)

    async def _emit(**_kwargs):
        return None

    monkeypatch.setattr(runner, "_emit_frontdoor_runtime_snapshot", _emit, raising=False)
    monkeypatch.setattr(runner, "_refresh_runtime_config_for_retry_invalidation", lambda: False, raising=False)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs_for_session", lambda *_a, **_k: [], raising=False)
    monkeypatch.setattr(runner, "_model_response_usage", lambda _message: {}, raising=False)
    return runner


def _build_runtime():
    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _compression_state={},
    )
    return SimpleNamespace(
        context=CeoRuntimeContext(loop=None, session=session, session_key="web:shared", on_progress=None)
    )


def _request_messages():
    return [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "older-1"},
        {"role": "assistant", "content": "older-2"},
        {"role": "user", "content": "older-3"},
        {"role": "assistant", "content": "older-4"},
        {"role": "user", "content": "recent-1"},
        {"role": "assistant", "content": "recent-2"},
        {"role": "user", "content": "recent-3"},
        {"role": "assistant", "content": "recent-4"},
    ]


@pytest.mark.asyncio
async def test_frontdoor_compression_request_is_append_only_with_trailing_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _build_runner(monkeypatch)
    captured: list[list[dict[str, object]]] = []

    async def _call_model_with_tools(**kwargs):
        captured.append(list(kwargs.get("messages") or []))
        return {"content": "压缩后的摘要"}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(
        runner,
        "_estimate_frontdoor_send_total_tokens",
        lambda **kwargs: 10_000,
        raising=False,
    )

    result = await runner._run_frontdoor_llm_token_compression(
        state={"session_key": "web:shared", "prompt_cache_key": ""},
        runtime=_build_runtime(),
        request_messages=_request_messages(),
        model_refs=["ceo_primary"],
        tool_schemas=[],
    )

    assert len(captured) == 1
    messages = captured[0]
    contents = [str(item.get("content") or "") for item in messages]
    # append-only：不再有整段历史 JSON 巨包
    assert not any("frontdoor_token_compression" in content for content in contents)
    assert not any('"older_history_messages"' in content for content in contents)
    # 前缀 = 原系统提示 + 较早历史（原位、原样），末尾是指令
    assert contents[0] == "SYSTEM"
    assert contents[1:5] == ["older-1", "older-2", "older-3", "older-4"]
    assert str(messages[-1].get("role") or "") == "user"
    assert "【上下文压缩指令】" in contents[-1]
    assert result.history_shrink_reason == "token_compression"
    assert result.diagnostics["mode"] == "llm"
    # 重写后请求体：系统前缀 + 压缩块 + 尾部 + 契约
    rewritten = result.request_messages
    assert str(rewritten[0].get("content") or "") == "SYSTEM"
    assert "[TOKEN_COMPACT_V2]" in str(rewritten[1].get("content") or "")
    assert [str(item.get("content") or "") for item in rewritten[2:6]] == [
        "recent-1",
        "recent-2",
        "recent-3",
        "recent-4",
    ]


@pytest.mark.asyncio
async def test_frontdoor_compression_retries_empty_response_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _build_runner(monkeypatch)
    calls: list[int] = []

    async def _call_model_with_tools(**kwargs):
        calls.append(len(calls))
        if len(calls) == 1:
            return {"content": ""}
        return {"content": "第二次尝试得到的摘要"}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **kwargs: 10_000, raising=False)

    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(ceo_runtime_ops.asyncio, "sleep", _fast_sleep)

    result = await runner._run_frontdoor_llm_token_compression(
        state={"session_key": "web:shared", "prompt_cache_key": ""},
        runtime=_build_runtime(),
        request_messages=_request_messages(),
        model_refs=["ceo_primary"],
        tool_schemas=[],
    )

    assert len(calls) == 2
    assert "第二次尝试得到的摘要" in str(result.request_messages[1].get("content") or "")
    assert result.history_shrink_reason == "token_compression"


@pytest.mark.asyncio
async def test_frontdoor_compression_empty_retry_respects_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _build_runner(monkeypatch)
    cancel_state = {"cancelled": False}

    async def _call_model_with_tools(**kwargs):
        cancel_state["cancelled"] = True
        return {"content": ""}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **kwargs: 10_000, raising=False)

    runtime = _build_runtime()
    runtime.context.session._active_cancel_token = SimpleNamespace(
        is_cancelled=lambda: cancel_state["cancelled"]
    )

    with pytest.raises(asyncio.CancelledError):
        await runner._run_frontdoor_llm_token_compression(
            state={"session_key": "web:shared", "prompt_cache_key": ""},
            runtime=runtime,
            request_messages=_request_messages(),
            model_refs=["ceo_primary"],
            tool_schemas=[],
        )


@pytest.mark.asyncio
async def test_frontdoor_compression_chunks_when_single_shot_exceeds_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _build_runner(monkeypatch, context_window_tokens=2_000)
    captured: list[list[dict[str, object]]] = []

    async def _call_model_with_tools(**kwargs):
        messages = list(kwargs.get("messages") or [])
        captured.append(messages)
        rendered = json.dumps(messages, ensure_ascii=False, default=str)
        if "frontdoor_token_compression_chunk" in rendered:
            index = len([item for item in captured if "chunk" in json.dumps(item, ensure_ascii=False, default=str)])
            return {"content": f"块摘要{index}"}
        return {"content": "归并后的总摘要"}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)

    def _instance_estimate(**kwargs):
        rendered = json.dumps(list(kwargs.get("request_messages") or []), ensure_ascii=False, default=str)
        if "【上下文压缩指令】" in rendered:
            return 2_100  # 单发压缩请求超窗 → 分块
        return 100  # 压缩后重写估算收敛

    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", _instance_estimate, raising=False)

    def _module_estimate(provider_request_body=None, request_messages=None, tool_schemas=None):
        messages = list(request_messages or [])
        rendered = json.dumps(messages, ensure_ascii=False, default=str)
        if "frontdoor_token_compression_merge" in rendered:
            return 100
        if "frontdoor_token_compression_chunk" in rendered:
            return 100
        if len(messages) == 1 and str((messages[0] or {}).get("role") or "") == "assistant":
            text = str((messages[0] or {}).get("content") or "")
            if text.startswith("[分块摘要"):
                # 归并判据只看"拼接后的摘要本身超容量"：按内容形态识别这一读，
                # 否则某个只含一条 assistant 的原子组会被一起顶大。
                return 100_000
            return 300
        if "【上下文压缩指令】" in rendered:
            # 与"每条历史 300"同一口径：信封（前缀 + 指令）900 + 4 条历史 = 2100，
            # 落在 2000 窗口之外 ⇒ 必须分块。单发读数与组成它的历史读数必须由同一
            # 估算器给出，否则规划器算出的信封是假的（容量直接非正 ⇒ 整次压缩不跑）。
            return 900 + 300 * max(0, len(messages) - 2)
        return len(messages) * 300  # 按装载量给读数（原子组各 1 条 ⇒ 300 tok）

    monkeypatch.setattr(ceo_runtime_ops, "_estimate_frontdoor_provider_request_tokens", _module_estimate)

    result = await runner._run_frontdoor_llm_token_compression(
        state={"session_key": "web:shared", "prompt_cache_key": ""},
        runtime=_build_runtime(),
        request_messages=_request_messages(),
        model_refs=["ceo_primary"],
        tool_schemas=[],
    )

    chunk_calls = [
        messages
        for messages in captured
        if any("frontdoor_token_compression_chunk" in str(item.get("content") or "") for item in messages)
    ]
    merge_calls = [
        messages
        for messages in captured
        if any("frontdoor_token_compression_merge" in str(item.get("content") or "") for item in messages)
    ]
    assert len(chunk_calls) >= 2
    assert len(merge_calls) == 1
    assert result.diagnostics["mode"] == "llm_chunked"
    assert result.diagnostics["chunk_count"] == len(chunk_calls)
    assert result.diagnostics["merge_pass_applied"] is True
    assert "归并后的总摘要" in str(result.request_messages[1].get("content") or "")


def _history_body(groups: int) -> list[dict[str, object]]:
    """system + `groups + 4` 条单消息历史（尾部固定留 4 条，与生产同一保留数）。"""
    messages: list[dict[str, object]] = [{"role": "system", "content": "SYSTEM"}]
    for index in range(groups + 4):
        role = "user" if index % 2 == 0 else "assistant"
        messages.append({"role": role, "content": f"h{index}"})
    return messages


@pytest.mark.asyncio
async def test_frontdoor_overflow_defers_the_tail_and_keeps_it_in_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """30 块装不下两轮（12+12）时，压不完的原始历史必须还在这份请求体里。

    旧处置是抛 `frontdoor_context_window_exceeded` 打死这一跳。现在第一轮 12 块、第二轮
    12 块，剩下 6 组原样留在摘要块之后：静默删掉它们才是真的丢内容。
    """
    runner = _build_runner(monkeypatch, context_window_tokens=4_000)
    captured: list[list[dict[str, object]]] = []

    async def _call_model_with_tools(**kwargs):
        messages = list(kwargs.get("messages") or [])
        captured.append(messages)
        rendered = json.dumps(messages, ensure_ascii=False, default=str)
        if "frontdoor_token_compression_merge" in rendered:
            return {"content": "归并后的总摘要"}
        index = len([item for item in captured if "frontdoor_token_compression_chunk" in json.dumps(item, ensure_ascii=False, default=str)])
        return {"content": f"块摘要{index}"}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **kwargs: 1_000, raising=False)

    def _module_estimate(provider_request_body=None, request_messages=None, tool_schemas=None):
        messages = list(request_messages or [])
        rendered = json.dumps(messages, ensure_ascii=False, default=str)
        if "【上下文压缩指令】" in rendered:
            loaded = sum(1 for item in messages if str(item.get("content") or "").startswith("h"))
            return 900 + 2100 * loaded
        if "frontdoor_token_compression_merge" in rendered:
            return 100
        if len(messages) == 1 and str((messages[0] or {}).get("content") or "").startswith("[分块摘要"):
            return 100_000  # 逼出一次归并，顺带证明归并不会吞掉"仍有原文在场"那行
        return 2100

    monkeypatch.setattr(ceo_runtime_ops, "_estimate_frontdoor_provider_request_tokens", _module_estimate)

    result = await runner._run_frontdoor_llm_token_compression(
        state={"session_key": "web:shared", "prompt_cache_key": ""},
        runtime=_build_runtime(),
        request_messages=_history_body(30),
        model_refs=["ceo_primary"],
        tool_schemas=[],
    )

    diagnostics = result.diagnostics
    assert diagnostics.get("applied") is True, diagnostics
    assert diagnostics.get("mode") == "llm_chunked"
    assert int(diagnostics.get("chunk_count") or 0) == 30
    assert int(diagnostics.get("compression_pass_rounds") or 0) == 2
    assert len(captured) == 24 + 1, f'两轮各 12 块再加一次归并：{len(captured)}'
    assert int(diagnostics.get("deferred_message_count") or 0) == 6
    contents = [str(item.get("content") or "") for item in result.request_messages]
    assert "h0" not in contents, '压中的组必须真的不再在场'
    assert "h24" in contents and "h29" in contents, f'压不完的原样留在请求里：{contents[-12:]}'
    assert "归并后的总摘要" in contents[1]
    assert "下一跳按那时的窗口继续压缩" in contents[1]


@pytest.mark.asyncio
async def test_frontdoor_failed_chunk_ships_the_committed_prefix_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第 3 块没拿到摘要：前 2 块照常交付，第 3 块及其后的原文留在请求里。

    旧行为有两处不对：在摘要里插一行"本块原文未被摘要替代"却把原文删掉（等于谎报），
    以及任何一块失败都整次作废、下一跳把已压好的块重烧一遍。
    """
    runner = _build_runner(monkeypatch, context_window_tokens=4_000)
    captured: list[list[dict[str, object]]] = []

    async def _call_model_with_tools(**kwargs):
        messages = list(kwargs.get("messages") or [])
        rendered = json.dumps(messages, ensure_ascii=False, default=str)
        if "frontdoor_token_compression_chunk" in rendered:
            captured.append(messages)
            # 按块号判失败，不按调用次数：helper 自己会重试，次数会漂。
            chunk_index = 0
            for item in messages:
                text = str(item.get("content") or "")
                if '"chunk_index"' in text:
                    chunk_index = int(json.loads(text).get("chunk_index") or 0)
            if chunk_index == 3:
                return {"content": "", "error_text": "HTTP 429: token plan entitlement exhausted"}
            return {"content": f"块摘要{chunk_index}"}
        captured.append(messages)
        return {"content": "归并后的总摘要"}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **kwargs: 1_000, raising=False)

    def _module_estimate(provider_request_body=None, request_messages=None, tool_schemas=None):
        messages = list(request_messages or [])
        rendered = json.dumps(messages, ensure_ascii=False, default=str)
        if "【上下文压缩指令】" in rendered:
            loaded = sum(1 for item in messages if str(item.get("content") or "").startswith("h"))
            return 900 + 2100 * loaded
        if "frontdoor_token_compression_merge" in rendered:
            return 100
        if len(messages) == 1 and str((messages[0] or {}).get("content") or "").startswith("[分块摘要"):
            return 300
        return 2100

    monkeypatch.setattr(ceo_runtime_ops, "_estimate_frontdoor_provider_request_tokens", _module_estimate)

    result = await runner._run_frontdoor_llm_token_compression(
        state={"session_key": "web:shared", "prompt_cache_key": ""},
        runtime=_build_runtime(),
        request_messages=_history_body(8),
        model_refs=["ceo_primary"],
        tool_schemas=[],
    )

    diagnostics = result.diagnostics
    assert diagnostics.get("applied") is True, diagnostics
    assert int(diagnostics.get("chunk_count") or 0) == 8
    assert int(diagnostics.get("chunk_failed_count") or 0) == 1
    assert int(diagnostics.get("deferred_message_count") or 0) == 6
    contents = [str(item.get("content") or "") for item in result.request_messages]
    summary_block = contents[1]
    assert "[分块摘要 1/8]" in summary_block and "[分块摘要 2/8]" in summary_block
    assert "3/8" not in summary_block, f"停下的那块不许留缺口行：{summary_block}"
    assert "未取得摘要，本块原文未被摘要替代" not in summary_block
    assert "h2" in contents and "h7" in contents, f"未压中的原文必须在场：{contents}"


@pytest.mark.asyncio
async def test_frontdoor_append_only_request_under_window_stays_single_when_span_clamp_is_tight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实测跨度把**块容量**夹小时，不许把装得下的 append-only 单发切成多块。

    单发请求的前缀与刚被同一颗模型成功接受的那次发送字节一致，前缀缓存真实命中；块是
    重打包的新形状，才需要被实测跨度夹一刀。混用会把每个正常会话一进压缩就切成多块，
    正好毁掉 append-only 那条修复。
    """
    runner = _build_runner(monkeypatch, context_window_tokens=4_000)
    captured: list[list[dict[str, object]]] = []

    async def _call_model_with_tools(**kwargs):
        messages = list(kwargs.get("messages") or [])
        captured.append(messages)
        return {"content": "压缩后的摘要"}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **kwargs: 1_000, raising=False)
    monkeypatch.setattr(runner, "_frontdoor_observed_request_span_tokens", lambda _state: 1_500, raising=False)

    def _module_estimate(provider_request_body=None, request_messages=None, tool_schemas=None):
        messages = list(request_messages or [])
        rendered = json.dumps(messages, ensure_ascii=False, default=str)
        if "【上下文压缩指令】" in rendered:
            loaded = sum(1 for item in messages if str(item.get("content") or "").startswith("h"))
            return 900 + 900 * loaded
        return 900

    monkeypatch.setattr(ceo_runtime_ops, "_estimate_frontdoor_provider_request_tokens", _module_estimate)

    result = await runner._run_frontdoor_llm_token_compression(
        state={"session_key": "web:shared", "prompt_cache_key": ""},
        runtime=_build_runtime(),
        request_messages=_history_body(2),
        model_refs=["ceo_primary"],
        tool_schemas=[],
    )

    assert len(captured) == 1
    chunked = "frontdoor_token_compression_chunk" in json.dumps(captured[0], ensure_ascii=False, default=str)
    assert not chunked
    assert str((captured[0][-1] or {}).get("content") or "").startswith("【上下文压缩指令】")
    assert result.diagnostics.get("mode") == "llm"
    assert int(result.diagnostics.get("capacity_tokens") or 0) == int(1_500 * 0.9)


def test_frontdoor_planner_refuses_only_when_capacity_is_non_positive() -> None:
    """窗口连压缩请求的信封都装不下：规划器交回拒因，这一 pass 一次都不发。"""
    from g3ku.runtime.chunk_compression_plan import (
        COMPRESSION_MODE_NONE,
        REFUSAL_CAPACITY_NON_POSITIVE,
    )

    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    planned = runner._frontdoor_plan_token_compression_pass(
        grouped_history=[([{"role": "user", "content": "x"}], 1_000)],
        single_shot_request_tokens=9_000,
        context_window_tokens=4_000,
        observed_span_tokens=0,
    )

    assert planned.mode == COMPRESSION_MODE_NONE
    assert planned.refusal is not None
    assert planned.refusal.kind == REFUSAL_CAPACITY_NON_POSITIVE


def test_iter_compaction_atomic_groups_keeps_tool_call_runs_indivisible() -> None:
    from g3ku.runtime.tool_history import iter_compaction_atomic_groups

    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call:1"}]},
        {"role": "tool", "tool_call_id": "call:1", "content": "r1"},
        {"role": "tool", "tool_call_id": "call:1", "content": "r2"},
        {"role": "assistant", "content": "done"},
        {"role": "tool", "tool_call_id": "orphan", "content": "orphan-result"},
    ]
    groups = iter_compaction_atomic_groups(messages)
    assert [len(group) for group in groups] == [1, 3, 1, 1]
    assert groups[1][0]["role"] == "assistant"
    assert [item.get("role") for item in groups[1]] == ["assistant", "tool", "tool"]


@pytest.mark.asyncio
async def test_frontdoor_compression_aborts_and_keeps_body_when_helper_yields_no_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """helper 没拿到摘要就放弃这一 pass：原请求体照发，绝不拿空产物去替换历史。"""
    runner = _build_runner(monkeypatch)

    async def _empty(*_args, **_kwargs):
        return {"content": "", "error_text": "upstream returned empty"}

    monkeypatch.setattr(runner, "_call_model_with_tools", _empty)
    monkeypatch.setattr(
        runner,
        "_estimate_frontdoor_send_total_tokens",
        lambda **kwargs: 10_000,
        raising=False,
    )
    original = _request_messages()

    result = await runner._run_frontdoor_llm_token_compression(
        state={"session_key": "web:shared", "prompt_cache_key": ""},
        runtime=_build_runtime(),
        request_messages=[dict(item) for item in original],
        model_refs=["ceo_primary"],
        tool_schemas=[],
    )

    assert result.diagnostics.get("applied") is False, result.diagnostics
    assert [dict(item) for item in result.request_messages] == original
    assert result.history_shrink_reason == ""
    assert "no summary" in str(result.diagnostics.get("compression_pass_failed") or "")
