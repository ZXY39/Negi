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
        # 形状错误判定只认结构化 status：模拟 SDK BadRequestError（status_code=400）。
        raise _StatusError("HTTP 400: bad request", 400)


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


class _StatusError(RuntimeError):
    """携带结构化 HTTP 状态的异常，模拟 OpenAI SDK 异常（.status_code 可得）。"""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


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
    # 新契约：非可重试错误（未命中 retry_on）每个 key 各试一次（单趟轮换）后切下一
    # 模型，不再按 retry_count 重复整轮——retry_count 专属可重试错误的退避轮预算。
    calls: list[int] = []
    providers = {
        0: _AlwaysRetryableProvider(0, calls),
        1: _RetryThenSuccessProvider(1, calls, succeed_on_call=1),
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
    assert calls == [0, 1]


def test_should_rotate_predicate_retryable_request_shape_and_internal() -> None:
    """轮换谓词契约：retryOn 命中(可重试)/请求形状(结构化 400/422)/内部错误 都不换；其余换。"""
    rotate = fallback_module.should_rotate_api_key_error
    retry_on = ["network", "429", "502"]
    # 可重试（命中 retryOn）→ 不换 key，走整链重试
    assert rotate("RateLimitError: Error code: 429 - too many requests", retry_on=retry_on) is False
    assert rotate("HTTP 502: upstream request failed", retry_on=retry_on) is False
    # 请求形状错误只认结构化 status：status_code 400/422 → 不换（换 key 修不了畸形 payload）
    assert rotate(_StatusError("provider rejected payload", 400), retry_on=retry_on) is False
    assert rotate(_StatusError("unprocessable payload", 422), retry_on=retry_on) is False
    # 结构化 status 可得且非 400/422 → 不再落文本匹配：网关标成
    # invalid_request_error 的 429 限流不会被误判成形状错误（仍因命中
    # retryOn 而不轮换，但判定理由是可重试而非形状错误）。
    assert rotate(
        _StatusError("RateLimitError: {'type': 'invalid_request_error'} code=429001 status=429", 429),
        retry_on=retry_on,
    ) is False
    # 无结构化 status 的文本错误一律不按形状错误处理（文本兜底已移除）
    assert rotate("HTTP 400: bad request", retry_on=retry_on) is True
    assert rotate(RuntimeError("BadRequestError: 400 invalid_request_error"), retry_on=retry_on) is True
    # 内部运行时错误 → 不换
    assert rotate("sqlite database is locked", retry_on=retry_on) is False
    # 非可重试、非请求形状、非内部（如 401 坏 key、503）→ 换 key
    assert rotate("HTTP 401: unauthorized", retry_on=retry_on) is True
    assert rotate("HTTP 503: service unavailable", retry_on=retry_on) is True
    # retryOn 显式置空 → 无可重试关键字 → 非形状错误一律换 key
    assert rotate("HTTP 502: upstream request failed", retry_on=[]) is True
    # 结构化形状豁免与 retryOn 无关
    assert rotate(_StatusError("provider rejected payload", 400), retry_on=[]) is False


def test_is_request_shape_error_structured_status_only() -> None:
    """形状错误判定只信任结构化 HTTP 状态，文本关键字兜底已移除。"""
    shape = fallback_module.is_request_shape_error
    # 结构化 status 可得：只有 400/422 是形状错误
    assert shape(_StatusError("bad payload", 400)) is True
    assert shape(_StatusError("unprocessable", 422)) is True
    assert shape(_StatusError("{'type': 'invalid_request_error'} tpm exhausted", 429)) is False
    assert shape(_StatusError("unauthorized", 401)) is False
    assert shape(_StatusError("upstream error", 503)) is False
    # 无结构化 status → 一律非形状错误（即使文本带 bad request / invalid_request_error）
    assert shape("HTTP 400: bad request") is False
    assert shape(RuntimeError("BadRequestError: 400 invalid_request_error")) is False
    # 响应路径用 LLMResponse.error_status
    assert shape(LLMResponse(content="err", finish_reason="error", error_status=400)) is True
    assert shape(LLMResponse(content="err", finish_reason="error", error_status=422)) is True
    assert shape(LLMResponse(content="err", finish_reason="error", error_status=429)) is False


def test_response_requires_rotation_uses_structured_error_status() -> None:
    """响应路径用结构化 error_status 判请求形状错误（error_text 字符串拿不到 status）。"""
    bad_request = LLMResponse(content="err", error_text="provider rejected", finish_reason="error", error_status=400)
    assert fallback_module.response_requires_api_key_rotation(bad_request, retry_on=["network", "429"]) is False
    server_error = LLMResponse(content="err", error_text="upstream 503", finish_reason="error", error_status=503)
    assert fallback_module.response_requires_api_key_rotation(server_error, retry_on=["network", "429"]) is True
    retryable = LLMResponse(content="err", error_text="429 too many requests", finish_reason="error", error_status=429)
    assert fallback_module.response_requires_api_key_rotation(retryable, retry_on=["network", "429"]) is False
    # 网关把 429 限流标成 invalid_request_error 也不得误判为形状错误：
    # 结构化 error_status=429 权威，不因文本关键字改变分类。
    poisoned = LLMResponse(
        content="err",
        error_text="{'type': 'invalid_request_error'} 429 rate limit",
        finish_reason="error",
        error_status=429,
    )
    assert fallback_module.response_requires_api_key_rotation(poisoned, retry_on=["network", "429"]) is False


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
async def test_config_chat_backend_emits_retry_status_on_key_rotation(monkeypatch) -> None:
    calls: list[int] = []
    events: list[dict[str, object]] = []

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, model_key
        key_index = int(api_key_index or 0)
        provider = _AuthThenSuccessProvider(key_index, calls, succeed=key_index == 1)
        return _target(provider=provider, retry_count=0, api_key_count=2)

    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)

    async def record_status(status: dict[str, object]) -> None:
        events.append(status)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    response = await backend.chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["primary"],
        on_model_retry_status=record_status,
    )

    assert response.content == "ok"
    assert calls == [0, 1]
    retrying_events = [event for event in events if event.get("state") == "retrying"]
    # key0 的 401 非可重试 → 同模型轮换 key1。咽喉点对这次同模型重发发 retrying，
    # retry_count=1 = 轮换前已打出的 1 发真实请求；此前轮换完全不计、也不发。
    assert len(retrying_events) == 1
    assert retrying_events[0]["retry_count"] == 1
    assert "HTTP 401" in str(retrying_events[0]["error_message"])
    assert events[-1] == {"state": "cleared"}


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
async def test_config_chat_backend_retries_retryable_error_without_consuming_fallback(monkeypatch) -> None:
    # 新契约（回归 task:e580ebc3dc55）：retry_on 命中的错误在独占退避窗口内直接转
    # 整链退避重试，不在轮内跨模型消费下游模型——旧实现零等待降级到弱回退模型，
    # 把决定性请求交给了劣质响应。链首恢复后即由链首交付，全程不触碰 secondary。
    calls: list[str] = []
    providers = {
        "primary": _RetryableChainThenSuccessProvider("primary", calls, succeed_on_call=3),
        "secondary": _AlwaysRetryableChainProvider("secondary", calls),
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
    # 前两轮 502 命中 retry_on → 整链退避重试（从链首重启）；第 3 轮链首恢复。
    assert calls == ["primary", "primary", "primary"]
    assert calls.count("secondary") == 0


@pytest.mark.asyncio
async def test_config_chat_backend_publishes_model_retry_status_and_clears_it(monkeypatch) -> None:
    calls: list[str] = []
    events: list[dict[str, object]] = []
    providers = {
        "primary": _RetryableChainThenSuccessProvider("primary", calls, succeed_on_call=2),
        "secondary": _AlwaysRetryableChainProvider("secondary", calls),
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
    assert calls == ["primary", "primary"]
    retrying_events = [event for event in events if event.get("state") == "retrying"]
    # 链首可重试错误转整链退避重试时发一次 retrying；retry_count 是 provider 实际
    # 请求次数（退避前已打出 1 发）。
    assert len(retrying_events) == 1
    assert retrying_events[0]["retry_count"] == 1
    assert "HTTP 502: upstream request failed" in str(retrying_events[0]["error_message"])
    assert events[-1] == {"state": "cleared"}


@pytest.mark.asyncio
async def test_fallback_provider_retries_retryable_error_without_consuming_fallback(monkeypatch) -> None:
    # 前门 FallbackProvider 同一契约：链首可重试错误直接整链退避重试，不跨模型消费下游。
    calls: list[str] = []
    providers = {
        "primary": _RetryableChainThenSuccessProvider("primary", calls, succeed_on_call=2),
        "secondary": _AlwaysRetryableChainProvider("secondary", calls),
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
    assert calls == ["primary", "primary"]
    assert calls.count("secondary") == 0


@pytest.mark.asyncio
async def test_fallback_provider_chain_retry_stops_at_round_budget(monkeypatch) -> None:
    # 可重试退避以逐模型轮数预算为唯一权威上限：retry_count=3 → 单 key 模型恰好
    # 3 次尝试后冒泡 exhausted，而非无限重试（时间上限已移除，次数即预算）。
    calls: list[str] = []

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=_AlwaysRetryableChainProvider(str(model_key), calls),
            retry_on=["network", "429", "502"],
            retry_count=3,
            api_key_count=1,
        )

    monkeypatch.setattr("g3ku.providers.provider_factory.build_provider_from_model_key", _builder)
    monkeypatch.setattr(fallback_module, "model_retry_backoff_seconds", lambda attempt: 0.0)

    provider = fallback_module.FallbackProvider(
        config=SimpleNamespace(),
        model_chain=["primary"],
        default_model_ref="primary",
    )
    with pytest.raises(fallback_module.ModelProviderExhaustedError) as exc_info:
        await provider.chat(messages=[{"role": "user", "content": "demo"}], model="primary")
    assert exc_info.value.retryable is True
    assert calls.count("primary") == 3  # 有界：恰好轮数预算次


@pytest.mark.asyncio
async def test_config_chat_backend_honors_per_model_retry_round_budgets(monkeypatch) -> None:
    # 每个模型有独立的退避重试轮预算（绑定 retry_count）：primary 预算 2 轮耗尽后
    # 前进到 secondary；secondary 预算 15 轮，第 12 轮成功——远超历史 10 轮上限，
    # 证明轮预算完全由逐模型配置驱动。
    calls: list[str] = []
    providers = {
        "primary": _AlwaysRetryableChainProvider("primary", calls),
        "secondary": _RetryableChainThenSuccessProvider("secondary", calls, succeed_on_call=12),
    }
    budgets = {"primary": 2, "secondary": 15}

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=providers[str(model_key)],
            retry_on=["network", "429", "502"],
            retry_count=budgets[str(model_key)],
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
    assert calls.count("primary") == 2
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
            # primary 轮预算 1：耗尽后在模型前进边界触发链刷新。
            retry_count=1,
            api_key_count=1,
        )

    resolver_calls: list[int] = []

    def _resolver():
        resolver_calls.append(1)
        # 首次解析仍是单模型链；primary 预算耗尽后的模型前进边界上，
        # 新加入的 fallback 模型变得可见。
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
    assert calls == ["primary", "secondary"]


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
            # timeout 命中 network 预设关键字时走整链退避重试（见
            # test_config_chat_backend_retries_retryable_timeout_without_fallback）；
            # 这里用不含超时语义的关键字，验证不可重试超时仍跨模型降级。
            retry_on=["502"],
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
            # 同上：不含超时语义的关键字，验证不可重试超时的跨模型降级。
            retry_on=["502"],
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


class _HangOnceThenSuccessProvider:
    """首次调用挂起（制造超时），之后正常返回。"""

    def __init__(self, model_key: str, calls: list[str]) -> None:
        self.model_key = model_key
        self.calls = calls
        self.call_count = 0

    async def chat(self, **kwargs):
        self.calls.append(self.model_key)
        self.call_count += 1
        if self.call_count == 1:
            await asyncio.Event().wait()
        return LLMResponse(content="ok", finish_reason="stop")


@pytest.mark.asyncio
async def test_config_chat_backend_retries_retryable_timeout_without_fallback(monkeypatch) -> None:
    # timeout 命中 network 预设关键字：链首超时转整链退避重试，不跨模型消费下游。
    calls: list[str] = []
    providers = {
        "primary": _HangOnceThenSuccessProvider("primary", calls),
        "secondary": _TimeoutAwareSuccessProvider("secondary", calls, []),
    }

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=providers[str(model_key)],
            retry_on=["network", "429"],
            retry_count=0,
            api_key_count=1,
        )

    monkeypatch.setattr(chat_backend_module, "model_retry_backoff_seconds", lambda attempt: 0.0)
    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    backend._model_attempt_timeout_seconds = 0.01

    response = await backend.chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["primary", "secondary"],
    )

    assert response.content == "ok"
    assert calls == ["primary", "primary"]
    assert calls.count("secondary") == 0


@pytest.mark.asyncio
async def test_config_chat_backend_retries_gateway_429_with_invalid_request_error_marker(monkeypatch) -> None:
    # 回归 task:e580ebc3dc55（异常路径）：网关把 429 限流标成
    # 'type': 'invalid_request_error'，不得误判为请求形状错误而改变分类；
    # 链首命中 retry_on → 整链退避重试而非零等待降级到弱回退模型。
    calls: list[str] = []

    class _RateLimitOnceThenSuccess:
        def __init__(self, model_key: str) -> None:
            self.model_key = model_key
            self.call_count = 0

        async def chat(self, **kwargs):
            _ = kwargs
            calls.append(self.model_key)
            self.call_count += 1
            if self.call_count == 1:
                raise _StatusError(
                    "RateLimitError: Error code: 429 - {'error': {'message': 'rpm exhausted', "
                    "'type': 'invalid_request_error', 'code': '429001'}} code=429001 status=429",
                    429,
                )
            return LLMResponse(content="ok", finish_reason="stop")

    providers = {
        "primary": _RateLimitOnceThenSuccess("primary"),
        "secondary": _AlwaysRetryableChainProvider("secondary", calls),
    }

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=providers[str(model_key)],
            retry_on=["network", "429"],
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
    assert calls == ["primary", "primary"]
    assert calls.count("secondary") == 0


@pytest.mark.asyncio
async def test_config_chat_backend_retries_retryable_error_response_at_chain_head(monkeypatch) -> None:
    # 回归 task:e580ebc3dc55（响应路径）：OpenAI 系 provider 把 SDK 异常转成
    # finish_reason=error + error_status=429 的响应。链首可重试错误响应转整链
    # 退避重试，secondary 全程不被消费。
    calls: list[str] = []

    class _RateLimitResponseOnceThenSuccess:
        def __init__(self, model_key: str) -> None:
            self.model_key = model_key
            self.call_count = 0

        async def chat(self, **kwargs):
            _ = kwargs
            calls.append(self.model_key)
            self.call_count += 1
            if self.call_count == 1:
                return LLMResponse(
                    content="RateLimitError: Error code: 429 - {'error': {'message': 'rpm exhausted', "
                    "'type': 'invalid_request_error', 'code': '429001'}} code=429001 status=429",
                    error_text="RateLimitError: Error code: 429 - {'error': {'message': 'rpm exhausted', "
                    "'type': 'invalid_request_error', 'code': '429001'}} code=429001 status=429",
                    finish_reason="error",
                    error_status=429,
                )
            return LLMResponse(content="ok", finish_reason="stop")

    providers = {
        "primary": _RateLimitResponseOnceThenSuccess("primary"),
        "secondary": _AlwaysRetryableChainProvider("secondary", calls),
    }

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=providers[str(model_key)],
            retry_on=["network", "429"],
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
    assert calls == ["primary", "primary"]
    assert calls.count("secondary") == 0


@pytest.mark.asyncio
async def test_fallback_provider_retries_retryable_error_response_at_chain_head(monkeypatch) -> None:
    # 前门 FallbackProvider 响应路径同一契约。
    calls: list[str] = []

    class _RateLimitResponseOnceThenSuccess:
        def __init__(self, model_key: str) -> None:
            self.model_key = model_key
            self.call_count = 0

        async def chat(self, **kwargs):
            _ = kwargs
            calls.append(self.model_key)
            self.call_count += 1
            if self.call_count == 1:
                return LLMResponse(
                    content="429 too many requests",
                    error_text="429 too many requests",
                    finish_reason="error",
                    error_status=429,
                )
            return LLMResponse(content="ok", finish_reason="stop")

    providers = {
        "primary": _RateLimitResponseOnceThenSuccess("primary"),
        "secondary": _AlwaysRetryableChainProvider("secondary", calls),
    }

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=providers[str(model_key)],
            retry_on=["network", "429"],
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
    assert calls == ["primary", "primary"]
    assert calls.count("secondary") == 0


class _Persistent429Provider:
    """持续抛出网关型 429（含 invalid_request_error 标记）的 provider。"""

    def __init__(self, model_key: str, key_index: int, calls: list[tuple[str, int]]) -> None:
        self.model_key = model_key
        self.key_index = key_index
        self.calls = calls

    async def chat(self, **kwargs):
        _ = kwargs
        self.calls.append((self.model_key, self.key_index))
        raise RuntimeError(
            "RateLimitError: Error code: 429 - {'error': {'message': 'rpm exhausted', "
            "'type': 'invalid_request_error', 'code': '429001'}} code=429001 status=429"
        )


@pytest.mark.asyncio
async def test_fallback_provider_exhausts_full_key_round_budget_then_raises(monkeypatch) -> None:
    # 验收情景：链 = A(key1,key2) → B(key1,key3)，重试次数=10，所有 key 持续命中
    # 重试关键词且始终未恢复。一轮 = 完整轮过一个模型的所有 key；每模型 10 轮，
    # 共 4 个 key 槽位 × 10 轮 = 40 次请求后报错停止；每次重试保留动态退避间隔
    # （测试里 patch 为 0 加速）。同一 key 出现在多个模型配置中互不影响——预算按
    # (模型, key) 槽位独立计。
    calls: list[tuple[str, int]] = []

    def _builder(config, model_key, *, api_key_index=None):
        _ = config
        key_index = int(api_key_index or 0)
        return ProviderTarget(
            provider_ref=str(model_key),
            provider_id="custom",
            model_id=f"{model_key}-model",
            provider=_Persistent429Provider(str(model_key), key_index, calls),
            retry_on=["network", "429"],
            retry_count=10,
            api_key_count=2,
            api_key_indexes=[0, 1],
        )

    monkeypatch.setattr("g3ku.providers.provider_factory.build_provider_from_model_key", _builder)
    monkeypatch.setattr(fallback_module, "model_retry_backoff_seconds", lambda attempt: 0.0)

    provider = fallback_module.FallbackProvider(
        config=SimpleNamespace(),
        model_chain=["A", "B"],
        default_model_ref="A",
    )
    with pytest.raises(fallback_module.ModelProviderExhaustedError) as exc_info:
        await provider.chat(messages=[{"role": "user", "content": "demo"}], model=None)

    assert exc_info.value.retryable is True
    assert len(calls) == 40
    assert calls.count(("A", 0)) == 10
    assert calls.count(("A", 1)) == 10
    assert calls.count(("B", 0)) == 10
    assert calls.count(("B", 1)) == 10
    # 顺序：A 的预算全部耗尽后才轮到 B
    assert [model for model, _ in calls[:20]] == ["A"] * 20
    assert [model for model, _ in calls[20:]] == ["B"] * 20


@pytest.mark.asyncio
async def test_config_chat_backend_default_retry_budget_when_retry_count_zero(monkeypatch) -> None:
    # retry_count=0/未设置 → 使用默认轮预算 DEFAULT_RETRYABLE_MODEL_ROUNDS(10)：
    # 单 key 模型持续可重试失败时恰好 10 次尝试后冒泡 exhausted。
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

    monkeypatch.setattr(chat_backend_module, "model_retry_backoff_seconds", lambda attempt: 0.0)
    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    with pytest.raises(fallback_module.ModelProviderExhaustedError) as exc_info:
        await backend.chat(
            messages=[{"role": "user", "content": "demo"}],
            tools=None,
            model_refs=["primary"],
        )

    assert exc_info.value.retryable is True
    assert calls.count("primary") == fallback_module.DEFAULT_RETRYABLE_MODEL_ROUNDS == 10
