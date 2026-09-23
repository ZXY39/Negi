"""Responses 协议实际发出的请求体字段。

`text.verbosity` 是 OpenAI 专有扩展：把 /responses 代理到 Chat Completions 后端的
供应商会因为这一个字段整单拒绝（HTTP 400 "text.verbosity is not supported by the
selected Chat Completions backend"），而连接探测走的是最小请求体，所以「测试连接」
通过、真实回合失败。
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

    async def _fake_consume_sse(response):
        return "ok", [], "stop", {}

    monkeypatch.setattr("g3ku.providers.responses_provider.httpx.AsyncClient", _fake_client(captured))
    monkeypatch.setattr("g3ku.providers.responses_provider._consume_sse", _fake_consume_sse)

    provider = ResponsesProvider(api_key="test-key", api_base="https://example.com/v1")
    await provider.chat(messages=[{"role": "user", "content": "ping"}], **chat_kwargs)
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
