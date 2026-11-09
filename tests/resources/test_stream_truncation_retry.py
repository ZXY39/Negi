"""断流重试契约：不留半截正文，丢弃后同模型重试，预算耗尽再降级。

发送层是这条车道的入口：provider 的 `StreamIncomplete` 响应、可见正文的丢弃、
同模型轮次预算与跨模型前进都在这里结算。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import main.runtime.chat_backend as chat_backend_module
from g3ku.providers.base import LLMResponse
from g3ku.providers.provider_factory import ProviderTarget

TRUNCATION_TEXT = "stream closed before finish_reason after 57 chunks"


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple] = []

    def add(self, *item) -> None:
        self.items.append(tuple(item))


class _ScriptedProvider:
    supports_streaming = True
    supports_reasoning_delta_streaming = False
    supports_upstream_wait_notice = False

    def __init__(
        self,
        model_key: str,
        events: _Events,
        *,
        truncations: int,
        stream_text: bool = True,
        partial: str = "半截正文",
        final: str = "完整正文",
    ) -> None:
        self.model_key = model_key
        self.events = events
        self.truncations = int(truncations)
        self.stream_text = bool(stream_text)
        self.partial = partial
        self.final = final

    async def chat(self, **kwargs):
        self.events.add("call", self.model_key)
        if self.truncations > 0:
            self.truncations -= 1
            on_text_delta = kwargs.get("on_text_delta")
            if self.stream_text and callable(on_text_delta):
                result = on_text_delta(self.partial)
                if inspect.isawaitable(result):
                    await result
            return LLMResponse(
                content=self.partial if self.stream_text else "",
                finish_reason="error",
                error_text=TRUNCATION_TEXT,
                error_kind="StreamIncomplete",
                visible_text_streamed=self.stream_text,
                stream_incomplete=True,
            )
        return LLMResponse(content=self.final, finish_reason="stop", visible_text_streamed=True)


def _target(model_key: str, provider, *, retry_count: int = 0) -> ProviderTarget:
    return ProviderTarget(
        provider_ref=str(model_key),
        provider_id="custom",
        model_id=f"{model_key}-model",
        provider=provider,
        retry_on=["network", "429", "502"],
        retry_count=int(retry_count),
        api_key_count=1,
        api_key_indexes=[0],
    )


def _backend() -> chat_backend_module.ConfigChatBackend:
    return chat_backend_module.ConfigChatBackend(
        config=SimpleNamespace(get_model_runtime_profile=lambda ref: None)
    )


def _patch(monkeypatch, providers: dict[str, ProviderTarget]) -> None:
    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return providers[str(model_key)]

    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)
    monkeypatch.setattr(chat_backend_module, "model_retry_backoff_seconds", lambda attempt: 0.0)


@pytest.mark.asyncio
async def test_truncated_visible_reply_is_discarded_and_retried_on_same_model(monkeypatch) -> None:
    events = _Events()
    providers = {
        "m_a": _target("m_a", _ScriptedProvider("m_a", events, truncations=1)),
    }
    _patch(monkeypatch, providers)

    async def _reset() -> None:
        events.add("reset")

    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["m_a"],
        on_text_delta=lambda text: events.add("delta", text),
        on_visible_text_reset=_reset,
    )

    assert response.content == "完整正文"
    assert response.finish_reason == "stop"
    assert [(item[0], item[1] if len(item) > 1 else "") for item in events.items if item[0] in {"call", "reset"}] == [
        ("call", "m_a"),
        ("reset", ""),
        ("call", "m_a"),
    ]


@pytest.mark.asyncio
async def test_truncation_budget_exhausts_then_downgrades_to_next_model(monkeypatch) -> None:
    events = _Events()
    providers = {
        "m_a": _target("m_a", _ScriptedProvider("m_a", events, truncations=99)),
        "m_b": _target("m_b", _ScriptedProvider("m_b", events, truncations=0)),
    }
    _patch(monkeypatch, providers)

    async def _reset() -> None:
        events.add("reset")

    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["m_a", "m_b"],
        on_text_delta=lambda text: events.add("delta", text),
        on_visible_text_reset=_reset,
    )

    assert response.content == "完整正文"
    calls = [item[1] for item in events.items if item[0] == "call"]
    resets = [item for item in events.items if item[0] == "reset"]
    assert calls == ["m_a", "m_a", "m_b"]
    assert len(resets) == 2


@pytest.mark.asyncio
async def test_truncation_budget_follows_configured_retry_count(monkeypatch) -> None:
    events = _Events()
    providers = {
        "m_a": _target("m_a", _ScriptedProvider("m_a", events, truncations=99), retry_count=3),
        "m_b": _target("m_b", _ScriptedProvider("m_b", events, truncations=0)),
    }
    _patch(monkeypatch, providers)

    async def _reset() -> None:
        events.add("reset")

    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["m_a", "m_b"],
        on_text_delta=lambda text: events.add("delta", text),
        on_visible_text_reset=_reset,
    )

    assert response.content == "完整正文"
    calls = [item[1] for item in events.items if item[0] == "call"]
    resets = [item for item in events.items if item[0] == "reset"]
    assert calls == ["m_a", "m_a", "m_a", "m_b"]
    assert len(resets) == 3


@pytest.mark.asyncio
async def test_truncation_without_visible_text_retries_without_reset(monkeypatch) -> None:
    events = _Events()
    providers = {
        "m_a": _target("m_a", _ScriptedProvider("m_a", events, truncations=1, stream_text=False)),
    }
    _patch(monkeypatch, providers)

    async def _reset() -> None:
        events.add("reset")

    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["m_a"],
        on_text_delta=lambda text: events.add("delta", text),
        on_visible_text_reset=_reset,
    )

    assert response.content == "完整正文"
    calls = [item[1] for item in events.items if item[0] == "call"]
    resets = [item for item in events.items if item[0] == "reset"]
    assert calls == ["m_a", "m_a"]
    assert resets == []
