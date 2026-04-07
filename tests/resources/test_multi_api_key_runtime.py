from __future__ import annotations

import asyncio
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


class _AlwaysRetryableChainProvider:
    def __init__(self, model_key: str, calls: list[str]) -> None:
        self.model_key = model_key
        self.calls = calls

    async def chat(self, **kwargs):
        _ = kwargs
        self.calls.append(self.model_key)
        raise RuntimeError("HTTP 502: upstream request failed")


class _RetryableChainThenSuccessProvider:
    def __init__(self, model_key: str, calls: list[str], *, succeed_on_call: int) -> None:
        self.model_key = model_key
        self.calls = calls
        self.succeed_on_call = succeed_on_call
        self.call_count = 0

    async def chat(self, **kwargs):
        _ = kwargs
        self.calls.append(self.model_key)
        self.call_count += 1
        if self.call_count >= self.succeed_on_call:
            return LLMResponse(content="ok", finish_reason="stop")
        raise RuntimeError("HTTP 502: upstream request failed")


class _HangingChainProvider:
    def __init__(self, model_key: str, calls: list[str], timeouts: list[float | None]) -> None:
        self.model_key = model_key
        self.calls = calls
        self.timeouts = timeouts

    async def chat(self, **kwargs):
        self.calls.append(self.model_key)
        self.timeouts.append(kwargs.get("request_timeout_seconds"))
        await asyncio.Event().wait()


class _TimeoutAwareSuccessProvider:
    def __init__(self, model_key: str, calls: list[str], timeouts: list[float | None]) -> None:
        self.model_key = model_key
        self.calls = calls
        self.timeouts = timeouts

    async def chat(self, **kwargs):
        self.calls.append(self.model_key)
        self.timeouts.append(kwargs.get("request_timeout_seconds"))
        return LLMResponse(content="ok", finish_reason="stop")


def _target(*, provider, retry_count: int, api_key_count: int, api_key_indexes: list[int] | None = None) -> ProviderTarget:
    return ProviderTarget(
        provider_ref="primary",
        provider_id="custom",
        model_id="custom-model",
        provider=provider,
        retry_on=["network", "429", "5xx"],
        retry_count=retry_count,
        api_key_count=api_key_count,
        api_key_indexes=list(range(api_key_count)) if api_key_indexes is None else api_key_indexes,
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


@pytest.mark.asyncio
async def test_config_chat_backend_skips_disabled_api_keys_in_rotation(monkeypatch) -> None:
    calls: list[int] = []

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, model_key
        key_index = int(api_key_index or 0)
        provider = _AuthThenSuccessProvider(key_index, calls, succeed=key_index == 1)
        return _target(provider=provider, retry_count=0, api_key_count=3, api_key_indexes=[0, 1])

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
async def test_config_chat_backend_rejects_when_all_api_keys_disabled(monkeypatch) -> None:
    class _UnexpectedProvider:
        async def chat(self, **kwargs):
            raise AssertionError(f"provider should not be called when all api keys are disabled: {kwargs!r}")

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, model_key, api_key_index
        return _target(provider=_UnexpectedProvider(), retry_count=0, api_key_count=3, api_key_indexes=[])

    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())

    with pytest.raises(RuntimeError, match="All configured API keys are disabled"):
        await backend.chat(
            messages=[{"role": "user", "content": "demo"}],
            tools=None,
            model_refs=["primary"],
        )


@pytest.mark.asyncio
async def test_config_chat_backend_retries_full_model_chain_on_retryable_exhaustion(monkeypatch) -> None:
    calls: list[str] = []
    providers = {
        "primary": _AlwaysRetryableChainProvider("primary", calls),
        "secondary": _RetryableChainThenSuccessProvider("secondary", calls, succeed_on_call=2),
    }

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=providers[str(model_key)],
            retry_on=["network", "429", "5xx"],
            retry_count=0,
            api_key_count=1,
        )

    monkeypatch.setattr(chat_backend_module, "RETRYABLE_MODEL_CHAIN_MAX_ROUNDS", 2)
    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    response = await backend.chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["primary", "secondary"],
    )

    assert response.content == "ok"
    assert calls == ["primary", "secondary", "primary", "secondary"]


@pytest.mark.asyncio
async def test_fallback_provider_retries_full_model_chain_on_retryable_exhaustion(monkeypatch) -> None:
    calls: list[str] = []
    providers = {
        "primary": _AlwaysRetryableChainProvider("primary", calls),
        "secondary": _RetryableChainThenSuccessProvider("secondary", calls, succeed_on_call=2),
    }

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=providers[str(model_key)],
            retry_on=["network", "429", "5xx"],
            retry_count=0,
            api_key_count=1,
        )

    monkeypatch.setattr(fallback_module, "RETRYABLE_MODEL_CHAIN_MAX_ROUNDS", 2)
    monkeypatch.setattr("g3ku.providers.provider_factory.build_provider_from_model_key", _builder)

    provider = fallback_module.FallbackProvider(
        config=SimpleNamespace(),
        model_chain=["primary", "secondary"],
        default_model_ref="primary",
    )
    response = await provider.chat(messages=[{"role": "user", "content": "demo"}], model="primary")

    assert response.content == "ok"
    assert calls == ["primary", "secondary", "primary", "secondary"]


@pytest.mark.asyncio
async def test_config_chat_backend_fails_after_retryable_chain_round_limit(monkeypatch) -> None:
    calls: list[str] = []
    providers = {
        "primary": _AlwaysRetryableChainProvider("primary", calls),
        "secondary": _AlwaysRetryableChainProvider("secondary", calls),
    }

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=providers[str(model_key)],
            retry_on=["network", "429", "5xx"],
            retry_count=0,
            api_key_count=1,
        )

    monkeypatch.setattr(chat_backend_module, "RETRYABLE_MODEL_CHAIN_MAX_ROUNDS", 2)
    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())

    with pytest.raises(
        RuntimeError,
        match="Model provider call failed after exhausting the configured fallback chain.",
    ):
        await backend.chat(
            messages=[{"role": "user", "content": "demo"}],
            tools=None,
            model_refs=["primary", "secondary"],
        )

    assert calls == ["primary", "secondary", "primary", "secondary"]


@pytest.mark.asyncio
async def test_config_chat_backend_falls_back_after_attempt_timeout(monkeypatch) -> None:
    calls: list[str] = []
    primary_timeouts: list[float | None] = []
    secondary_timeouts: list[float | None] = []
    providers = {
        "primary": _HangingChainProvider("primary", calls, primary_timeouts),
        "secondary": _TimeoutAwareSuccessProvider("secondary", calls, secondary_timeouts),
    }

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=providers[str(model_key)],
            retry_on=["network", "429", "5xx"],
            retry_count=0,
            api_key_count=1,
        )

    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    backend._model_attempt_timeout_seconds = 0.01

    response = await backend.chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["primary", "secondary"],
    )

    assert response.content == "ok"
    assert calls == ["primary", "secondary"]
    assert primary_timeouts == [0.01]
    assert secondary_timeouts == [0.01]


@pytest.mark.asyncio
async def test_fallback_provider_falls_back_after_attempt_timeout(monkeypatch) -> None:
    calls: list[str] = []
    primary_timeouts: list[float | None] = []
    secondary_timeouts: list[float | None] = []
    providers = {
        "primary": _HangingChainProvider("primary", calls, primary_timeouts),
        "secondary": _TimeoutAwareSuccessProvider("secondary", calls, secondary_timeouts),
    }

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=providers[str(model_key)],
            retry_on=["network", "429", "5xx"],
            retry_count=0,
            api_key_count=1,
        )

    monkeypatch.setattr("g3ku.providers.provider_factory.build_provider_from_model_key", _builder)

    provider = fallback_module.FallbackProvider(
        config=SimpleNamespace(),
        model_chain=["primary", "secondary"],
        default_model_ref="primary",
    )

    response = await provider.chat(
        messages=[{"role": "user", "content": "demo"}],
        model="primary",
        request_timeout_seconds=0.01,
    )

    assert response.content == "ok"
    assert calls == ["primary", "secondary"]
    assert primary_timeouts == [0.01]
    assert secondary_timeouts == [0.01]
