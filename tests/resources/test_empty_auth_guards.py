from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from g3ku.llm_config.enums import AuthMode, Capability, ProbeStatus, ProtocolAdapter
from g3ku.llm_config.models import NormalizedProviderConfig
from g3ku.llm_config.probe_strategies import _build_openai_headers, probe_config, probe_config_for_concurrency
from g3ku.providers.custom_provider import CustomProvider
from g3ku.providers.fallback import FallbackProvider
from g3ku.providers.base import LLMResponse
from g3ku.providers.litellm_provider import LiteLLMProvider
from g3ku.providers.openai_codex_provider import OpenAICodexProvider
from g3ku.providers.provider_factory import build_provider_from_model_key
from g3ku.providers.responses_provider import ResponsesProvider


def _config(
    *,
    provider_id: str,
    protocol_adapter: ProtocolAdapter,
    base_url: str,
    default_model: str,
    api_key: str = "",
    parameters: dict | None = None,
    headers: dict[str, str] | None = None,
) -> NormalizedProviderConfig:
    now = datetime.now(UTC)
    return NormalizedProviderConfig(
        config_id="cfg-test",
        provider_id=provider_id,
        display_name="test",
        protocol_adapter=protocol_adapter,
        capability=Capability.CHAT,
        auth_mode=AuthMode.API_KEY,
        base_url=base_url,
        default_model=default_model,
        auth={"type": "api_key", "api_key": api_key},
        parameters=parameters or {},
        headers=headers or {},
        extra_options={},
        template_version="test",
        created_at=now,
        updated_at=now,
    )


def test_build_provider_from_model_key_rejects_empty_responses_api_key(monkeypatch) -> None:
    monkeypatch.setattr(
        "g3ku.providers.provider_factory.resolve_chat_target",
        lambda config, ref: SimpleNamespace(
            provider_id="responses",
            resolved_model="gpt-5.4",
            secret_payload={"api_key": ""},
            base_url="https://example.com/v1/responses",
            max_tokens_limit=None,
            default_temperature=None,
            default_reasoning_effort=None,
            config_id="cfg-123",
        ),
    )
    config = SimpleNamespace(get_model_runtime_profile=lambda ref: None)

    with pytest.raises(ValueError) as exc_info:
        build_provider_from_model_key(config, "primary")

    message = str(exc_info.value)
    assert "Missing API key for managed model binding" in message
    assert "Model key: primary" in message
    assert "LLM config id: cfg-123" in message


def test_build_provider_from_model_key_selects_requested_api_key_index(monkeypatch) -> None:
    monkeypatch.setattr(
        "g3ku.providers.provider_factory.resolve_chat_target",
        lambda config, ref: SimpleNamespace(
            provider_id="custom",
            resolved_model="gpt-4.1",
            secret_payload={"api_key": "key-1,key-2"},
            base_url="https://example.com/v1",
            max_tokens_limit=None,
            default_temperature=None,
            default_reasoning_effort=None,
            config_id="cfg-123",
            headers={},
        ),
    )
    config = SimpleNamespace(get_model_runtime_profile=lambda ref: None)

    first = build_provider_from_model_key(config, "primary")
    second = build_provider_from_model_key(config, "primary", api_key_index=1)

    assert first.api_key_count == 2
    assert first.provider.api_key == "key-1"
    assert second.provider.api_key == "key-2"


def test_build_provider_from_model_key_routes_openai_responses_protocol_to_direct_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        "g3ku.providers.provider_factory.resolve_chat_target",
        lambda config, ref: SimpleNamespace(
            provider_id="openai",
            protocol_adapter=ProtocolAdapter.OPENAI_RESPONSES,
            resolved_model="gpt-5.4",
            secret_payload={"api_key": "test-key"},
            base_url="https://example.com/v1",
            max_tokens_limit=None,
            default_temperature=None,
            default_reasoning_effort=None,
            config_id="cfg-123",
            headers={"x-trace": "enabled"},
        ),
    )
    config = SimpleNamespace(get_model_runtime_profile=lambda ref: None)

    target = build_provider_from_model_key(config, "primary")

    assert isinstance(target.provider, ResponsesProvider)
    assert target.provider.api_base == "https://example.com/v1"
    assert target.provider.extra_headers == {"x-trace": "enabled"}


def test_build_provider_from_model_key_routes_openai_completions_protocol_to_custom_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        "g3ku.providers.provider_factory.resolve_chat_target",
        lambda config, ref: SimpleNamespace(
            provider_id="openrouter",
            protocol_adapter=ProtocolAdapter.OPENAI_COMPLETIONS,
            resolved_model="openai/gpt-4.1",
            secret_payload={"api_key": "test-key"},
            base_url="https://example.com/v1",
            max_tokens_limit=None,
            default_temperature=None,
            default_reasoning_effort=None,
            config_id="cfg-123",
            headers={"HTTP-Referer": "https://app.example"},
        ),
    )
    config = SimpleNamespace(get_model_runtime_profile=lambda ref: None)

    target = build_provider_from_model_key(config, "primary")

    assert isinstance(target.provider, CustomProvider)
    assert target.provider.api_base == "https://example.com/v1"
    assert target.provider.extra_headers == {"HTTP-Referer": "https://app.example"}


@pytest.mark.asyncio
async def test_fallback_provider_forwards_prompt_cache_key(monkeypatch) -> None:
    captured: list[str | None] = []

    class _RecorderProvider:
        async def chat(self, **kwargs):
            captured.append(kwargs.get("prompt_cache_key"))
            return LLMResponse(content="ok", finish_reason="stop")

    monkeypatch.setattr(
        "g3ku.providers.provider_factory.build_provider_from_model_key",
        lambda config, model_key, api_key_index=None: SimpleNamespace(
            provider=_RecorderProvider(),
            model_id="gpt-5.4",
            max_tokens_limit=None,
            default_temperature=None,
            default_reasoning_effort=None,
            retry_on=[],
            retry_count=0,
            api_key_count=0,
        ),
    )

    provider = FallbackProvider(
        config=SimpleNamespace(),
        model_chain=["primary"],
        default_model_ref="primary",
    )

    await provider.chat(
        messages=[{"role": "user", "content": "hello"}],
        prompt_cache_key="stable-key",
    )

    assert captured == ["stable-key"]


@pytest.mark.asyncio
async def test_responses_provider_refuses_empty_bearer_header(monkeypatch) -> None:
    class _UnexpectedAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("HTTP client should not be created when api_key is empty")

    monkeypatch.setattr("g3ku.providers.responses_provider.httpx.AsyncClient", _UnexpectedAsyncClient)
    provider = ResponsesProvider(api_key="   ", api_base="https://example.com/v1/responses")

    with pytest.raises(ValueError, match="empty Authorization header"):
        await provider.chat(messages=[{"role": "user", "content": "ping"}])


@pytest.mark.asyncio
async def test_custom_provider_forwards_request_timeout_to_openai_sdk(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=[]),
                        finish_reason="stop",
                    )
                ],
                usage={},
            )

    class _FakeAsyncOpenAI:
        def __init__(self, *args, **kwargs) -> None:
            _ = args, kwargs
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr("g3ku.providers.custom_provider.AsyncOpenAI", _FakeAsyncOpenAI)

    provider = CustomProvider(api_key="test-key", api_base="https://example.com/v1", default_model="demo")
    response = await provider.chat(
        messages=[{"role": "user", "content": "ping"}],
        model="demo",
        request_timeout_seconds=12.5,
    )

    assert response.content == "ok"
    assert captured["timeout"] == 12.5


@pytest.mark.asyncio
async def test_responses_provider_uses_request_timeout_for_http_client(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeStream:
        async def __aenter__(self):
            return SimpleNamespace(status_code=200)

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

        def stream(self, *args, **kwargs):
            _ = args, kwargs
            return _FakeStream()

    async def _fake_consume_sse(response):
        _ = response
        return "ok", [], "stop", {}

    monkeypatch.setattr("g3ku.providers.responses_provider.httpx.AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr("g3ku.providers.responses_provider._consume_sse", _fake_consume_sse)

    provider = ResponsesProvider(api_key="test-key", api_base="https://example.com/v1")
    response = await provider.chat(
        messages=[{"role": "user", "content": "ping"}],
        model="demo",
        request_timeout_seconds=7.5,
    )

    assert response.content == "ok"
    assert captured["timeout"] == 7.5


@pytest.mark.asyncio
async def test_litellm_provider_forwards_request_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_acompletion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage={},
        )

    monkeypatch.setattr("g3ku.providers.litellm_provider.acompletion", _fake_acompletion)

    provider = LiteLLMProvider(
        api_key="test-key",
        api_base="https://example.com/v1",
        default_model="openai/gpt-4.1",
        provider_name="openai",
    )
    response = await provider.chat(
        messages=[{"role": "user", "content": "ping"}],
        model="openai/gpt-4.1",
        request_timeout_seconds=9.5,
    )

    assert response.content == "ok"
    assert captured["timeout"] == 9.5


@pytest.mark.asyncio
async def test_openai_codex_provider_forwards_request_timeout(monkeypatch) -> None:
    captured: list[float | None] = []

    async def _fake_request_codex(url, headers, body, verify, timeout):
        _ = url, headers, body, verify
        captured.append(timeout)
        return "ok", [], "stop", {}

    monkeypatch.setattr(
        "g3ku.providers.openai_codex_provider.get_codex_token",
        lambda: SimpleNamespace(account_id="acct", access="token"),
    )
    monkeypatch.setattr("g3ku.providers.openai_codex_provider._request_codex", _fake_request_codex)

    provider = OpenAICodexProvider(default_model="openai_codex/gpt-5.1-codex")
    response = await provider.chat(
        messages=[{"role": "user", "content": "ping"}],
        model="openai_codex/gpt-5.1-codex",
        request_timeout_seconds=11.0,
    )

    assert response.content == "ok"
    assert captured == [11.0]


def test_build_openai_headers_omits_empty_authorization() -> None:
    config = _config(
        provider_id="responses",
        protocol_adapter=ProtocolAdapter.OPENAI_RESPONSES,
        base_url="https://example.com/v1/responses",
        default_model="gpt-5.4",
        api_key="",
        parameters={"auth_header": True},
    )

    headers = _build_openai_headers(config)

    assert "Authorization" not in headers
    assert "x-api-key" not in headers


@pytest.mark.parametrize(
    ("protocol_adapter", "default_model"),
    [
        (ProtocolAdapter.DASHSCOPE_EMBEDDING, "qwen3-vl-embedding"),
        (ProtocolAdapter.DASHSCOPE_RERANK, "qwen3-vl-rerank"),
    ],
)
def test_probe_config_omits_empty_bearer_for_dashscope(protocol_adapter, default_model) -> None:
    config = _config(
        provider_id="dashscope",
        protocol_adapter=protocol_adapter,
        base_url="https://example.com",
        default_model=default_model,
        api_key="",
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(401, json={"error": "missing auth"})

    result = probe_config(config, transport=httpx.MockTransport(_handler))

    assert result.status == ProbeStatus.AUTH_ERROR


def test_probe_config_reports_content_type_for_non_json_model_catalog() -> None:
    config = _config(
        provider_id="openai",
        protocol_adapter=ProtocolAdapter.OPENAI_RESPONSES,
        base_url="https://example.com/v1",
        default_model="gpt-5.4",
        api_key="test-key",
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><title>Sign in</title><body>login required</body></html>",
        )

    result = probe_config(config, transport=httpx.MockTransport(_handler))

    assert result.status == ProbeStatus.INVALID_RESPONSE
    assert "HTTP 200" in result.message
    assert "text/html" in result.message
    assert result.diagnostics["content_type"] == "text/html"
    assert "login required" in result.diagnostics["body_preview"]


def test_probe_config_falls_back_when_model_catalog_returns_500() -> None:
    config = _config(
        provider_id="custom",
        protocol_adapter=ProtocolAdapter.CUSTOM_DIRECT,
        base_url="https://example.com/v1",
        default_model="custom-model",
        api_key="test-key",
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(500, json={"error": "broken catalog"})
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    result = probe_config(config, transport=httpx.MockTransport(_handler))

    assert result.success is True
    assert result.message == "Fallback request succeeded."
    assert result.diagnostics["fallback_used"] is True
    assert result.diagnostics["api_key_count"] == 1
    assert result.diagnostics["api_key_attempts"] == 1


def test_probe_config_for_concurrency_uses_minimal_inference_request_for_openai_compatible() -> None:
    config = _config(
        provider_id="custom",
        protocol_adapter=ProtocolAdapter.CUSTOM_DIRECT,
        base_url="https://example.com/v1",
        default_model="custom-model",
        api_key="test-key",
    )
    seen_paths: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    result = probe_config_for_concurrency(config, transport=httpx.MockTransport(_handler))

    assert result.success is True
    assert seen_paths == ["/v1/chat/completions"]


def test_probe_config_rotates_api_keys_after_auth_failure() -> None:
    config = _config(
        provider_id="custom",
        protocol_adapter=ProtocolAdapter.CUSTOM_DIRECT,
        base_url="https://example.com/v1",
        default_model="custom-model",
        api_key="bad-key,good-key",
    )
    seen_tokens: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        token = request.headers.get("Authorization", "")
        seen_tokens.append(token)
        if token.endswith("bad-key"):
            return httpx.Response(401, json={"error": "bad key"})
        return httpx.Response(200, json={"data": [{"id": "custom-model"}]})

    result = probe_config(config, transport=httpx.MockTransport(_handler))

    assert result.success is True
    assert result.diagnostics["api_key_count"] == 2
    assert result.diagnostics["api_key_attempts"] == 2
    assert seen_tokens == ["Bearer bad-key", "Bearer good-key"]


def test_probe_config_does_not_rotate_api_keys_after_bad_request() -> None:
    config = _config(
        provider_id="custom",
        protocol_adapter=ProtocolAdapter.CUSTOM_DIRECT,
        base_url="https://example.com/v1",
        default_model="custom-model",
        api_key="bad-key,good-key",
    )
    seen_tokens: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers.get("Authorization", ""))
        if request.url.path == "/v1/models":
            return httpx.Response(400, json={"error": "unsupported"})
        return httpx.Response(400, json={"error": "bad payload"})

    result = probe_config(config, transport=httpx.MockTransport(_handler))

    assert result.success is False
    assert result.http_status == 400
    assert result.message == "Fallback request failed."
    assert result.diagnostics["api_key_count"] == 2
    assert result.diagnostics["api_key_attempts"] == 1
    assert seen_tokens == ["Bearer bad-key", "Bearer bad-key"]


@pytest.mark.parametrize(
    ("protocol_adapter", "default_model", "first_status"),
    [
        (ProtocolAdapter.DASHSCOPE_EMBEDDING, "qwen3-vl-embedding", 429),
        (ProtocolAdapter.DASHSCOPE_RERANK, "qwen3-vl-rerank", 401),
    ],
)
def test_probe_config_rotates_api_keys_for_dashscope_capabilities(protocol_adapter, default_model, first_status) -> None:
    config = _config(
        provider_id="dashscope",
        protocol_adapter=protocol_adapter,
        base_url="https://example.com",
        default_model=default_model,
        api_key="key-1,key-2",
    )
    seen_tokens: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers.get("Authorization", ""))
        if len(seen_tokens) == 1:
            return httpx.Response(first_status, json={"error": "retry next key"})
        if protocol_adapter == ProtocolAdapter.DASHSCOPE_EMBEDDING:
            return httpx.Response(200, json={"output": {"embeddings": [{"embedding": [0.1], "text_index": 0}]}})
        return httpx.Response(200, json={"output": {"results": [{"index": 0, "score": 0.9}]}})

    result = probe_config(config, transport=httpx.MockTransport(_handler))

    assert result.success is True
    assert result.diagnostics["api_key_count"] == 2
    assert result.diagnostics["api_key_attempts"] == 2
    assert seen_tokens == ["Bearer key-1", "Bearer key-2"]

