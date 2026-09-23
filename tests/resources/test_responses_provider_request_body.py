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
        return "ok", [], "stop", {}

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
