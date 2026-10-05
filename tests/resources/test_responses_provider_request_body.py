"""Responses 协议实际发出的请求体字段。

`text.verbosity` 与 `instructions` 都是会把请求打回口的字段：把 /responses 代理到
Chat Completions 后端的供应商，前者报 "text.verbosity is not supported by the selected
Chat Completions backend"，后者报 "inference request is invalid"
（code=invalid_parameter_error）。系统提示词因此只走 `input` 里的 `[SYSTEM]` 块。
连接探测用的是最小体，所以这类字段级拒绝只在真实回合暴露。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.providers.responses_provider import ResponsesProvider


def _fake_client(captured: dict) -> type:
    class _Stream:
        async def __aenter__(self):
            return SimpleNamespace(status_code=200)

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def stream(self, method, url, headers=None, json=None):
            captured["method"] = method
            captured["url"] = url
            captured["body"] = dict(json or {})
            return _Stream()

    return _FakeAsyncClient


async def _send(monkeypatch: pytest.MonkeyPatch, **chat_kwargs) -> dict:
    captured: dict = {}
    messages = chat_kwargs.pop("messages", [{"role": "user", "content": "ping"}])

    async def _fake_consume_sse(response):
        # 桩替换掉了真实流消费者：把终止事件补记进诊断，否则这条假流会被 provider
        # 当成"没说完"（断流现在按提供侧故障处理），测试要的就不是它声称的那条路径了。
        note_terminal = getattr(response, "note_terminal_event", None)
        if callable(note_terminal):
            note_terminal()
        return "ok", [], "stop", {}, []

    monkeypatch.setattr("g3ku.providers.responses_provider.httpx.AsyncClient", _fake_client(captured))
    monkeypatch.setattr("g3ku.providers.responses_provider._consume_sse", _fake_consume_sse)

    provider = ResponsesProvider(api_key="test-key", api_base="https://example.com/v1")
    await provider.chat(messages=messages, **chat_kwargs)
    return captured


@pytest.mark.asyncio
async def test_responses_body_omits_openai_only_text_field(monkeypatch) -> None:
    captured = await _send(monkeypatch, model="demo", reasoning_effort="xhigh")

    body = captured["body"]
    assert "text" not in body
    assert captured["url"].endswith("/responses")
    assert body["model"] == "demo"
    assert body["reasoning"] == {"effort": "xhigh"}


@pytest.mark.asyncio
async def test_responses_body_keeps_reasoning_out_when_effort_is_none(monkeypatch) -> None:
    captured = await _send(monkeypatch, model="demo", reasoning_effort=None)

    body = captured["body"]
    assert "text" not in body
    assert "reasoning" not in body
    assert body["store"] is False


@pytest.mark.asyncio
async def test_responses_body_carries_system_prompt_in_input_not_instructions(monkeypatch) -> None:
    captured = await _send(
        monkeypatch,
        model="demo",
        messages=[
            {"role": "system", "content": "你是 G3KU。"},
            {"role": "user", "content": "ping"},
        ],
    )

    body = captured["body"]
    assert "instructions" not in body
    first_item = body["input"][0]
    assert first_item["role"] == "user"
    assert "你是 G3KU。" in first_item["content"][0]["text"]
    assert first_item["content"][0]["text"].startswith("[SYSTEM]")


@pytest.mark.asyncio
async def test_responses_body_keeps_mid_history_system_at_its_own_index(monkeypatch) -> None:
    """活状态块（工具契约/阶段门/压缩块都是 system 角色）留在原位发。

    抽到 input[0] 会把每次改动推到请求最前面，前缀缓存边界就落在那个改动点上，
    改动之后的整段历史全部按新输入重计费。
    """
    captured = await _send(
        monkeypatch,
        model="demo",
        messages=[
            {"role": "system", "content": "基础提示"},
            {"role": "user", "content": "ping"},
            {"role": "assistant", "content": "pong"},
            {"role": "system", "content": "## Runtime Stage Gate\nactive_stage_id=stage-7"},
            {"role": "user", "content": "again"},
        ],
    )

    items = captured["body"]["input"]
    leading_text = items[0]["content"][0]["text"]
    assert "基础提示" in leading_text
    assert "Runtime Stage Gate" not in leading_text

    gate_index = next(i for i, item in enumerate(items) if item.get("role") == "system" and i > 0)
    assert items[gate_index]["type"] == "message"
    assert "active_stage_id=stage-7" in items[gate_index]["content"][0]["text"]
    # 原位：排在它所属的 assistant 之后、下一条用户消息之前
    assistant_index = next(i for i, item in enumerate(items) if item.get("role") == "assistant")
    last_user_index = max(i for i, item in enumerate(items) if item.get("role") == "user")
    assert assistant_index < gate_index < last_user_index


@pytest.mark.asyncio
async def test_responses_body_merges_only_the_leading_run_of_system(monkeypatch) -> None:
    captured = await _send(
        monkeypatch,
        model="demo",
        messages=[
            {"role": "system", "content": "甲"},
            {"role": "system", "content": "乙"},
            {"role": "user", "content": "ping"},
            {"role": "system", "content": "丙"},
        ],
    )

    items = captured["body"]["input"]
    leading_text = items[0]["content"][0]["text"]
    assert "甲" in leading_text and "乙" in leading_text
    assert "丙" not in leading_text
    tail = [item for item in items[1:] if item.get("role") == "system"]
    assert len(tail) == 1
    assert "丙" in tail[0]["content"][0]["text"]


@pytest.mark.asyncio
async def test_responses_leading_item_survives_a_change_to_the_tail_state_block(monkeypatch) -> None:
    """阶段一推进，只有尾部那项变，前导项必须逐字节不变——这是缓存能命中的前提。"""
    common = [
        {"role": "system", "content": "基础提示"},
        {"role": "user", "content": "ping"},
    ]
    first = await _send(
        monkeypatch,
        model="demo",
        messages=[*common, {"role": "system", "content": "active_stage_id=stage-1"}],
    )
    second = await _send(
        monkeypatch,
        model="demo",
        messages=[*common, {"role": "system", "content": "active_stage_id=stage-2"}],
    )

    leading_first = first["body"]["input"][0]["content"][0]["text"]
    leading_second = second["body"]["input"][0]["content"][0]["text"]
    assert leading_first == leading_second
    assert first["body"]["input"][-1] != second["body"]["input"][-1]
