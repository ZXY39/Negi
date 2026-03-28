from __future__ import annotations

from types import SimpleNamespace

import pytest

import g3ku.providers.fallback as fallback_module
import main.runtime.chat_backend as chat_backend_module
from g3ku.providers.base import LLMResponse
from g3ku.providers.provider_factory import ProviderTarget


class _AlwaysRetryableProvider:
    def __init__(self, key_index: int, calls: list[int]) -> None:
        self.key_index = key_index
        self.calls = calls

    async def chat(self, **kwargs):
        _ = kwargs
        self.calls.append(self.key_index)
        raise RuntimeError("HTTP 502: upstream request failed")


class _RetryThenSuccessProvider:
    def __init__(self, key_index: int, calls: list[int], succeed_on_call: int) -> None:
        self.key_index = key_index
        self.calls = calls
        self.succeed_on_call = succeed_on_call
        self.calls_for_key = 0

    async def chat(self, **kwargs):
        _ = kwargs
        self.calls.append(self.key_index)
        self.calls_for_key += 1
        if self.calls_for_key >= self.succeed_on_call:
            return LLMResponse(content="ok", finish_reason="stop")
        raise RuntimeError("HTTP 502: upstream request failed")


class _AuthThenSuccessProvider:
    def __init__(self, key_index: int, calls: list[int], *, succeed: bool) -> None:
        self.key_index = key_index
        self.calls = calls
        self.succeed = succeed

    async def chat(self, **kwargs):
        _ = kwargs
        self.calls.append(self.key_index)
        if self.succeed:
            return LLMResponse(content="ok", finish_reason="stop")
        raise RuntimeError("HTTP 401: unauthorized")


class _BadRequestProvider:
    def __init__(self, key_index: int, calls: list[int]) -> None:
        self.key_index = key_index
        self.calls = calls

    async def chat(self, **kwargs):
        _ = kwargs
        self.calls.append(self.key_index)
        raise RuntimeError("HTTP 400: bad request")


def _target(*, provider, retry_count: int, api_key_count: int) -> ProviderTarget:
    return ProviderTarget(
        provider_ref="primary",
        provider_id="custom",
        model_id="custom-model",
        provider=provider,
        retry_on=["network", "429", "5xx"],
        retry_count=retry_count,
        api_key_count=api_key_count,
    )


@pytest.mark.asyncio
async def test_fallback_provider_rotates_full_key_round_before_consuming_retry(monkeypatch) -> None:
    calls: list[int] = []
    providers = {
        0: _AlwaysRetryableProvider(0, calls),
        1: _RetryThenSuccessProvider(1, calls, succeed_on_call=2),
    }

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, model_key
        key_index = int(api_key_index or 0)
        return _target(provider=providers[key_index], retry_count=1, api_key_count=2)

    monkeypatch.setattr("g3ku.providers.provider_factory.build_provider_from_model_key", _builder)

    provider = fallback_module.FallbackProvider(
        config=SimpleNamespace(),
        model_chain=["primary"],
        default_model_ref="primary",
    )
    response = await provider.chat(messages=[{"role": "user", "content": "demo"}], model="primary")

    assert response.content == "ok"
    assert calls == [0, 1, 0, 1]


@pytest.mark.asyncio
async def test_config_chat_backend_rotates_on_auth_error(monkeypatch) -> None:
    calls: list[int] = []

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, model_key
        key_index = int(api_key_index or 0)
        provider = _AuthThenSuccessProvider(key_index, calls, succeed=key_index == 1)
        return _target(provider=provider, retry_count=0, api_key_count=2)

    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    response = await backend.chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["primary"],
    )

    assert response.content == "ok"
    assert calls == [0, 1]


@pytest.mark.asyncio
async def test_config_chat_backend_does_not_rotate_on_bad_request(monkeypatch) -> None:
    calls: list[int] = []

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, model_key
        key_index = int(api_key_index or 0)
        if key_index == 0:
            provider = _BadRequestProvider(key_index, calls)
        else:
            provider = _AuthThenSuccessProvider(key_index, calls, succeed=True)
        return _target(provider=provider, retry_count=0, api_key_count=2)

    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())

    with pytest.raises(
        RuntimeError,
        match="Model provider call failed after exhausting the configured fallback chain.",
    ):
        await backend.chat(
            messages=[{"role": "user", "content": "demo"}],
            tools=None,
            model_refs=["primary"],
        )

    assert calls == [0]
