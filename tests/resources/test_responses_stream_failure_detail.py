"""Responses 流失败时必须带上游错误体。

`response.failed` / `error` 事件过去只抛固定一句 "Codex response failed"，节点错误栏
与心跳通知因此没有任何可判读信息（实盘 task:38e0b6120ddb 连抛 3 次同文案后被熔断）。
现在消息带 message/type/code/reason，超长的完整错误体外置到 worker 日志。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.providers import responses_protocol_helpers as helpers
from g3ku.providers.responses_protocol_helpers import CODEX_FAILURE_DETAIL_LIMIT, CodexStreamError


async def _consume(monkeypatch: pytest.MonkeyPatch, events: list[dict]) -> tuple:
    async def _fake_iter(_response):
        for event in events:
            yield event

    monkeypatch.setattr(helpers, "_iter_sse", _fake_iter)
    return await helpers._consume_sse(None)


async def _raise_from(monkeypatch: pytest.MonkeyPatch, events: list[dict]) -> CodexStreamError:
    with pytest.raises(CodexStreamError) as raised:
        await _consume(monkeypatch, events)
    return raised.value


@pytest.mark.asyncio
async def test_response_failed_carries_upstream_error_fields(monkeypatch) -> None:
    event = {
        "type": "response.failed",
        "response": {
            "id": "resp_1",
            "status": "failed",
            "model": "sensenova-6.8-flash-lite",
            "error": {"message": "inference engine crashed", "type": "server_error", "code": "engine_error"},
            "output": [{"type": "reasoning"}] * 40,
        },
    }

    raised = await _raise_from(monkeypatch, [event])

    message = str(raised)
    assert message.startswith("Codex response failed:")
    assert "inference engine crashed" in message
    assert "server_error" in message
    assert "engine_error" in message
    assert "reasoning" not in message


@pytest.mark.asyncio
async def test_error_event_message_is_propagated(monkeypatch) -> None:
    raised = await _raise_from(monkeypatch, [{"type": "error", "error": {"message": "context length exceeded"}}])

    assert "context length exceeded" in str(raised)


@pytest.mark.asyncio
async def test_incomplete_details_reason_is_propagated(monkeypatch) -> None:
    event = {
        "type": "response.failed",
        "response": {"id": "resp_2", "status": "failed", "incomplete_details": {"reason": "max_output_tokens"}},
    }

    raised = await _raise_from(monkeypatch, [event])

    assert "max_output_tokens" in str(raised)


@pytest.mark.asyncio
async def test_oversized_detail_is_bounded_and_full_body_kept(monkeypatch) -> None:
    long_message = "x" * (CODEX_FAILURE_DETAIL_LIMIT + 500)
    raised = await _raise_from(monkeypatch, [{"type": "error", "error": {"message": long_message}}])

    message = str(raised)
    assert "截断" in message
    assert len(message) < len(long_message)
    assert len(raised.error_body) >= len(long_message)
    assert json.loads(raised.error_body)["message"] == long_message


@pytest.mark.asyncio
async def test_partial_content_is_kept_on_failure(monkeypatch) -> None:
    events = [
        {"type": "response.output_text.delta", "delta": "已经写出的部分"},
        {"type": "response.failed", "response": {"status": "failed", "error": {"message": "boom"}}},
    ]

    raised = await _raise_from(monkeypatch, events)

    assert raised.partial_content == "已经写出的部分"
    assert "boom" in str(raised)


@pytest.mark.asyncio
async def test_provider_keeps_node_text_bounded_and_logs_full_body(monkeypatch) -> None:
    import g3ku.providers.responses_provider as provider_module

    long_message = "y" * (CODEX_FAILURE_DETAIL_LIMIT + 400)
    logged: list[str] = []

    class _Logger:
        def debug(self, message, *args):
            logged.append("debug")

        def info(self, message, *args):
            logged.append("info")

        def warning(self, message, *args):
            logged.append(str(message).format(*args) if "%s" not in str(message) else str(message))

        def error(self, message, *args):
            logged.append("error:" + str(message))

    class _Stream:
        async def __aenter__(self):
            return SimpleNamespace(status_code=200, headers={})

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def stream(self, method, url, headers=None, json=None):
            return _Stream()

    async def _raise(response, **kwargs):
        raise CodexStreamError(
            "Codex response failed: 截断后的短文案",
            partial_content="",
            error_body=json.dumps({"message": long_message}, ensure_ascii=False),
        )

    monkeypatch.setattr(provider_module, "logger", _Logger())
    monkeypatch.setattr(provider_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(provider_module, "_consume_sse", _raise)

    provider = provider_module.ResponsesProvider(api_key="k", api_base="https://example.com/v1")
    with pytest.raises(RuntimeError) as raised:
        await provider.chat(messages=[{"role": "user", "content": "ping"}], model="m")

    assert len(str(raised.value)) < 200
    assert any(long_message[:200] in line for line in logged)
