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


def _view(message):
    content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
    return SimpleNamespace(
        content=content,
        tool_calls=[],
        error_text="",
        provider_request_meta={},
        provider_request_body={},
    )


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
    monkeypatch.setattr(runner, "_model_response_view", _view, raising=False)
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
    assert "[G3KU_TOKEN_COMPACT_V2]" in str(rewritten[1].get("content") or "")
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
    monkeypatch.setattr(runner, "_model_response_view", _view, raising=False)
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
    monkeypatch.setattr(runner, "_model_response_view", _view, raising=False)
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
    runner = _build_runner(monkeypatch, context_window_tokens=1_000)
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
    monkeypatch.setattr(runner, "_model_response_view", _view, raising=False)

    def _instance_estimate(**kwargs):
        rendered = json.dumps(list(kwargs.get("request_messages") or []), ensure_ascii=False, default=str)
        if "【上下文压缩指令】" in rendered:
            return 5_000  # 单发压缩请求超窗 → 分块
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
            return 25_000  # 合并摘要超预算 → 触发一次归并
        return 15_000  # 每个原子组自成一塊

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
