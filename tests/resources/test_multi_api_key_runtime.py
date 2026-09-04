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


def _target(*, provider, retry_count: int, api_key_count: int, api_key_indexes: list[int] | None = None, retry_on: list[str] | None = None) -> ProviderTarget:
    return ProviderTarget(
        provider_ref="primary",
        provider_id="custom",
        model_id="custom-model",
        provider=provider,
        retry_on=["network", "429", "502"] if retry_on is None else list(retry_on),
        retry_count=retry_count,
        api_key_count=api_key_count,
        api_key_indexes=list(range(api_key_count)) if api_key_indexes is None else api_key_indexes,
    )


@pytest.mark.asyncio
async def test_fallback_provider_rotates_keys_on_non_retryable_error(monkeypatch) -> None:
    # 新契约：轮换由"非可重试且非请求形状错误"触发。这里 retry_on 只含 network，故 502
    # 不可重试 → 轮换整轮 key（key0 恒失败、key1 第 2 次成功），轮换在切模型/整链重试之前。
    # （可重试错误不再轮换、改走整链重试，见 test_should_rotate_predicate_*。）
    calls: list[int] = []
    providers = {
        0: _AlwaysRetryableProvider(0, calls),
        1: _RetryThenSuccessProvider(1, calls, succeed_on_call=2),
    }

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, model_key
        key_index = int(api_key_index or 0)
        return _target(provider=providers[key_index], retry_count=1, api_key_count=2, retry_on=["network"])

    monkeypatch.setattr("g3ku.providers.provider_factory.build_provider_from_model_key", _builder)

    provider = fallback_module.FallbackProvider(
        config=SimpleNamespace(),
        model_chain=["primary"],
        default_model_ref="primary",
    )
    response = await provider.chat(messages=[{"role": "user", "content": "demo"}], model="primary")

    assert response.content == "ok"
    assert calls == [0, 1, 0, 1]


def test_should_rotate_predicate_retryable_request_shape_and_internal() -> None:
    """轮换谓词新契约：retryOn 命中(可重试)/请求形状(400)/内部错误 都不换；其余换。"""
    rotate = fallback_module.should_rotate_api_key_error
    retry_on = ["network", "429", "502"]
    # 可重试（命中 retryOn）→ 不换 key，走整链重试
    assert rotate("RateLimitError: Error code: 429 - too many requests", retry_on=retry_on) is False
    assert rotate("HTTP 502: upstream request failed", retry_on=retry_on) is False
    # 请求形状错误（400/bad request）→ 不换（换 key 修不了畸形 payload）
    assert rotate("HTTP 400: bad request", retry_on=retry_on) is False
    assert rotate(RuntimeError("BadRequestError: 400 invalid_request_error"), retry_on=retry_on) is False
    # 内部运行时错误 → 不换
    assert rotate("sqlite database is locked", retry_on=retry_on) is False
    # 非可重试、非请求形状、非内部（如 401 坏 key、503）→ 换 key
    assert rotate("HTTP 401: unauthorized", retry_on=retry_on) is True
    assert rotate("HTTP 503: service unavailable", retry_on=retry_on) is True
    # retryOn 显式置空 → 无可重试关键字 → 非请求形状错误一律换 key
    assert rotate("HTTP 502: upstream request failed", retry_on=[]) is True
    assert rotate("HTTP 400: bad request", retry_on=[]) is False  # 请求形状豁免与 retryOn 无关


def test_response_requires_rotation_uses_structured_error_status() -> None:
    """响应路径用结构化 error_status 判请求形状错误（error_text 字符串拿不到 status）。"""
    bad_request = LLMResponse(content="err", error_text="provider rejected", finish_reason="error", error_status=400)
    assert fallback_module.response_requires_api_key_rotation(bad_request, retry_on=["network", "429"]) is False
    server_error = LLMResponse(content="err", error_text="upstream 503", finish_reason="error", error_status=503)
    assert fallback_module.response_requires_api_key_rotation(server_error, retry_on=["network", "429"]) is True
    retryable = LLMResponse(content="err", error_text="429 too many requests", finish_reason="error", error_status=429)
    assert fallback_module.response_requires_api_key_rotation(retryable, retry_on=["network", "429"]) is False


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
        match=r"HTTP 400: bad request",
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
            retry_on=["network", "429", "502"],
            retry_count=0,
            api_key_count=1,
        )

    monkeypatch.setattr(chat_backend_module, "model_retry_backoff_seconds", lambda attempt: 0.0)
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
async def test_config_chat_backend_publishes_model_retry_status_and_clears_it(monkeypatch) -> None:
    calls: list[str] = []
    events: list[dict[str, object]] = []
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
            retry_on=["network", "429", "502"],
            retry_count=0,
            api_key_count=1,
        )

    monkeypatch.setattr(chat_backend_module, "model_retry_backoff_seconds", lambda attempt: 0.0)
    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)

    async def record_status(status: dict[str, object]) -> None:
        events.append(status)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    response = await backend.chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["primary", "secondary"],
        on_model_retry_status=record_status,
    )

    assert response.content == "ok"
    assert events[0]["state"] == "retrying"
    assert events[0]["retry_count"] == 1
    assert "HTTP 502: upstream request failed" in str(events[0]["error_message"])
    assert events[-1] == {"state": "cleared"}


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
            retry_on=["network", "429", "502"],
            retry_count=0,
            api_key_count=1,
        )

    monkeypatch.setattr(fallback_module, "model_retry_backoff_seconds", lambda attempt: 0.0)
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
async def test_fallback_provider_chain_retry_stops_at_backoff_budget_cap(monkeypatch) -> None:
    # 整链可重试退避有累计上限：超过即停止重试并冒泡 exhausted，而非无限重试。
    # 把上限与单次退避压到极小，使 cap 在几轮内触发（避免真实 20 分钟 sleep）。
    calls: list[str] = []

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=_AlwaysRetryableChainProvider(str(model_key), calls),
            retry_on=["network", "429", "502"],
            retry_count=0,
            api_key_count=1,
        )

    monkeypatch.setattr("g3ku.providers.provider_factory.build_provider_from_model_key", _builder)
    monkeypatch.setattr(fallback_module, "model_retry_backoff_seconds", lambda attempt: 0.02)
    monkeypatch.setattr(fallback_module, "MAX_RETRYABLE_CHAIN_BACKOFF_SECONDS", 0.05)

    provider = fallback_module.FallbackProvider(
        config=SimpleNamespace(),
        model_chain=["primary"],
        default_model_ref="primary",
    )
    # 退避累计 0.02 → 0.04，第三轮 0.04+0.02 > 0.05 触发 cap → break → 冒泡 exhausted。
    # 没有 cap 时这里会无限重试（测试挂起）。
    with pytest.raises(fallback_module.ModelProviderExhaustedError):
        await provider.chat(messages=[{"role": "user", "content": "demo"}], model="primary")
    assert calls.count("primary") <= 4  # 有界，而非无限


@pytest.mark.asyncio
async def test_config_chat_backend_retries_retryable_exhaustion_without_round_limit(monkeypatch) -> None:
    calls: list[str] = []
    providers = {
        "primary": _AlwaysRetryableChainProvider("primary", calls),
        # Succeeds on its 12th call: round 12 is beyond the historical
        # 10-round cap, proving retryable retries are now unbounded.
        "secondary": _RetryableChainThenSuccessProvider("secondary", calls, succeed_on_call=12),
    }

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=providers[str(model_key)],
            retry_on=["network", "429", "502"],
            retry_count=0,
            api_key_count=1,
        )

    monkeypatch.setattr(chat_backend_module, "model_retry_backoff_seconds", lambda attempt: 0.0)
    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    response = await backend.chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["primary", "secondary"],
    )

    assert response.content == "ok"
    assert calls.count("primary") == 12
    assert calls.count("secondary") == 12
    assert calls[-1] == "secondary"


@pytest.mark.asyncio
async def test_config_chat_backend_aborts_retry_loop_when_runtime_config_revision_changes(monkeypatch) -> None:
    calls: list[str] = []
    revisions = iter([5, 6])

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=_AlwaysRetryableChainProvider(str(model_key), calls),
            retry_on=["network", "429", "502"],
            retry_count=0,
            api_key_count=1,
        )

    monkeypatch.setattr(chat_backend_module, "model_retry_backoff_seconds", lambda attempt: 0.0)
    monkeypatch.setattr(chat_backend_module, "current_runtime_config_revision", lambda: next(revisions, 6))
    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())

    with pytest.raises(fallback_module.ModelProviderExhaustedError) as exc_info:
        await backend.chat(
            messages=[{"role": "user", "content": "demo"}],
            tools=None,
            model_refs=["primary"],
        )

    assert exc_info.value.retryable is True
    assert exc_info.value.config_revision_changed is True
    assert calls == ["primary"]


@pytest.mark.asyncio
async def test_fallback_provider_aborts_retry_loop_when_runtime_config_revision_changes(monkeypatch) -> None:
    calls: list[str] = []
    revisions = iter([5, 6])

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=_AlwaysRetryableChainProvider(str(model_key), calls),
            retry_on=["network", "429", "502"],
            retry_count=0,
            api_key_count=1,
        )

    monkeypatch.setattr(fallback_module, "model_retry_backoff_seconds", lambda attempt: 0.0)
    monkeypatch.setattr(fallback_module, "current_runtime_config_revision", lambda: next(revisions, 6))
    monkeypatch.setattr("g3ku.providers.provider_factory.build_provider_from_model_key", _builder)

    provider = fallback_module.FallbackProvider(
        config=SimpleNamespace(),
        model_chain=["primary"],
        default_model_ref="primary",
    )

    with pytest.raises(fallback_module.ModelProviderExhaustedError) as exc_info:
        await provider.chat(messages=[{"role": "user", "content": "demo"}], model="primary")

    assert exc_info.value.retryable is True
    assert exc_info.value.config_revision_changed is True
    assert calls == ["primary"]


@pytest.mark.asyncio
async def test_config_chat_backend_retry_backoff_is_cancellable(monkeypatch) -> None:
    calls: list[str] = []

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=_AlwaysRetryableChainProvider(str(model_key), calls),
            retry_on=["network", "429", "502"],
            retry_count=0,
            api_key_count=1,
        )

    # Long backoff: the cancel must land while the retry loop sleeps.
    monkeypatch.setattr(chat_backend_module, "model_retry_backoff_seconds", lambda attempt: 30.0)
    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    task = asyncio.create_task(
        backend.chat(
            messages=[{"role": "user", "content": "demo"}],
            tools=None,
            model_refs=["primary"],
        )
    )

    deadline = asyncio.get_running_loop().time() + 5.0
    while not calls and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert calls == ["primary"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["primary"]


@pytest.mark.asyncio
async def test_config_chat_backend_refreshes_model_chain_between_retry_rounds(monkeypatch) -> None:
    calls: list[str] = []
    providers = {
        "primary": _AlwaysRetryableChainProvider("primary", calls),
        "secondary": _RetryableChainThenSuccessProvider("secondary", calls, succeed_on_call=1),
    }

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=providers[str(model_key)],
            retry_on=["network", "429", "502"],
            retry_count=0,
            api_key_count=1,
        )

    resolver_calls: list[int] = []

    def _resolver():
        resolver_calls.append(1)
        # Round 1 keeps the original single-model chain; from round 2 onward
        # the freshly added fallback model becomes visible.
        return ["primary"] if len(resolver_calls) <= 1 else ["primary", "secondary"]

    monkeypatch.setattr(chat_backend_module, "model_retry_backoff_seconds", lambda attempt: 0.0)
    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    response = await backend.chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["primary"],
        model_refs_resolver=_resolver,
    )

    assert response.content == "ok"
    assert calls == ["primary", "primary", "secondary"]


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
            retry_on=["network", "429", "502"],
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
            retry_on=["network", "429", "502"],
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
