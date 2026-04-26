from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

import g3ku.providers.responses_provider as responses_provider_module
import main.runtime.chat_backend as chat_backend_module
from g3ku.llm_config.enums import AuthMode, Capability, ProbeStatus, ProtocolAdapter
from g3ku.llm_config.models import NormalizedProviderConfig
import g3ku.llm_config.probe_strategies as probe_strategies_module
from g3ku.llm_config.probe_strategies import _build_openai_headers, probe_config, probe_config_for_concurrency
from g3ku.providers.custom_provider import CustomProvider
from g3ku.providers.provider_factory import ProviderTarget
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
async def test_responses_provider_sanitizes_tool_schema_combinators_before_transport(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeStream:
        async def __aenter__(self):
            return SimpleNamespace(status_code=200)

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            _ = args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

        def stream(self, *args, **kwargs):
            captured["json"] = dict(kwargs.get("json") or {})
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
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "schema_demo",
                    "description": "demo",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "target": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "description": "nullable id",
                            },
                            "mode": {
                                "oneOf": [{"type": "string"}, {"type": "integer"}],
                            },
                            "payload": {
                                "allOf": [
                                    {
                                        "type": "object",
                                        "properties": {"id": {"type": "string"}},
                                    },
                                    {"required": ["id"]},
                                ]
                            },
                        },
                        "anyOf": [{"required": ["target"]}, {"required": ["mode"]}],
                    },
                },
            }
        ],
        request_timeout_seconds=7.5,
    )

    assert response.content == "ok"
    body = dict(captured.get("json") or {})
    parameters = dict(((body.get("tools") or [])[0] or {}).get("parameters") or {})
    properties = dict(parameters.get("properties") or {})
    payload_schema = dict(properties.get("payload") or {})

    assert "anyOf" not in parameters
    assert "oneOf" not in parameters
    assert "allOf" not in parameters
    assert "anyOf" not in properties.get("target", {})
    assert "oneOf" not in properties.get("mode", {})
    assert "allOf" not in payload_schema
    assert properties.get("target", {}).get("type") == "string"
    assert properties.get("mode", {}).get("type") == "string"
    assert payload_schema.get("type") == "object"
    assert "id" in dict(payload_schema.get("properties") or {})


def _capture_responses_provider_logs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sink: list[str] = []

    def _record(template, *args, **kwargs):
        _ = kwargs
        try:
            rendered = str(template).format(*args)
        except Exception:
            rendered = str(template)
        sink.append(rendered)

    monkeypatch.setattr(
        responses_provider_module,
        "logger",
        SimpleNamespace(debug=_record, warning=_record, error=_record),
    )
    return sink


@pytest.mark.asyncio
async def test_responses_provider_logs_sse_diagnostics_for_success(monkeypatch) -> None:
    logs = _capture_responses_provider_logs(monkeypatch)

    class _FakeResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self._lines = iter(
                [
                    "event: response.created",
                    'data: {"type":"response.created"}',
                    "",
                    "event: response.output_text.delta",
                    'data: {"type":"response.output_text.delta","delta":"OK"}',
                    "",
                    "event: response.completed",
                    'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}',
                    "",
                ]
            )

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    class _FakeStream:
        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            _ = args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

        def stream(self, *args, **kwargs):
            _ = args, kwargs
            return _FakeStream()

    async def _fake_consume_sse(response):
        async for _line in response.aiter_lines():
            pass
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
    joined = "\n".join(logs)
    assert "responses stream diagnostics" in joined
    assert "status_code=200" in joined
    assert "first_chunk_received_ms=" in joined
    assert "first_event_received_ms=" in joined
    assert "first_text_delta_received_ms=" in joined
    assert "stream_completed_ms=" in joined
    assert "last_event=response.completed" in joined


@pytest.mark.asyncio
async def test_responses_provider_logs_sse_diagnostics_for_stream_failure(monkeypatch) -> None:
    logs = _capture_responses_provider_logs(monkeypatch)

    class _FakeResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self._lines = iter(
                [
                    "event: response.created",
                    'data: {"type":"response.created"}',
                    "",
                    "event: response.output_item.added",
                    'data: {"type":"response.output_item.added","item":{"type":"message"}}',
                    "",
                ]
            )

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    class _FakeStream:
        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            _ = args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

        def stream(self, *args, **kwargs):
            _ = args, kwargs
            return _FakeStream()

    async def _fake_consume_sse(response):
        async for _line in response.aiter_lines():
            pass
        raise RuntimeError("stream stalled")

    monkeypatch.setattr("g3ku.providers.responses_provider.httpx.AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr("g3ku.providers.responses_provider._consume_sse", _fake_consume_sse)

    provider = ResponsesProvider(api_key="test-key", api_base="https://example.com/v1")

    with pytest.raises(RuntimeError, match="stream stalled"):
        await provider.chat(
            messages=[{"role": "user", "content": "ping"}],
            model="demo",
            request_timeout_seconds=7.5,
        )

    joined = "\n".join(logs)
    assert "responses stream diagnostics" in joined
    assert "stream_failed_ms=" in joined
    assert "last_event=response.output_item.added" in joined
    assert "first_data_received_ms=" in joined


@pytest.mark.asyncio
async def test_responses_provider_allows_total_stream_time_above_timeout_when_chunks_keep_arriving(monkeypatch) -> None:
    class _FakeResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self._lines = [
                "event: response.created",
                'data: {"type":"response.created"}',
                "",
                "event: response.output_text.delta",
                'data: {"type":"response.output_text.delta","delta":"O"}',
                "",
                "event: response.output_text.delta",
                'data: {"type":"response.output_text.delta","delta":"K"}',
                "",
                "event: response.completed",
                'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}',
                "",
            ]

        async def aiter_lines(self):
            for line in self._lines:
                await asyncio.sleep(0.03)
                yield line

    class _FakeStream:
        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            _ = args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

        def stream(self, *args, **kwargs):
            _ = args, kwargs
            return _FakeStream()

    monkeypatch.setattr("g3ku.providers.responses_provider.httpx.AsyncClient", _FakeAsyncClient)

    provider = ResponsesProvider(api_key="test-key", api_base="https://example.com/v1")
    response = await provider.chat(
        messages=[{"role": "user", "content": "ping"}],
        model="demo",
        request_timeout_seconds=0.05,
    )

    assert response.content == "OK"


@pytest.mark.asyncio
async def test_responses_provider_times_out_when_first_chunk_exceeds_timeout(monkeypatch) -> None:
    class _FakeResponse:
        def __init__(self) -> None:
            self.status_code = 200

        async def aiter_lines(self):
            await asyncio.sleep(0.03)
            yield "event: response.created"

    class _FakeStream:
        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            _ = args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

        def stream(self, *args, **kwargs):
            _ = args, kwargs
            return _FakeStream()

    monkeypatch.setattr("g3ku.providers.responses_provider.httpx.AsyncClient", _FakeAsyncClient)

    provider = ResponsesProvider(api_key="test-key", api_base="https://example.com/v1")
    with pytest.raises(RuntimeError, match="timeout"):
        await provider.chat(
            messages=[{"role": "user", "content": "ping"}],
            model="demo",
            request_timeout_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_responses_provider_times_out_when_stream_goes_idle_after_first_chunk(monkeypatch) -> None:
    class _FakeResponse:
        def __init__(self) -> None:
            self.status_code = 200

        async def aiter_lines(self):
            yield "event: response.created"
            await asyncio.sleep(0.03)
            yield 'data: {"type":"response.created"}'

    class _FakeStream:
        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            _ = args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

        def stream(self, *args, **kwargs):
            _ = args, kwargs
            return _FakeStream()

    monkeypatch.setattr("g3ku.providers.responses_provider.httpx.AsyncClient", _FakeAsyncClient)

    provider = ResponsesProvider(api_key="test-key", api_base="https://example.com/v1")
    with pytest.raises(RuntimeError, match="idle timeout"):
        await provider.chat(
            messages=[{"role": "user", "content": "ping"}],
            model="demo",
            request_timeout_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_responses_provider_preserves_flat_function_tool_schemas_in_transport_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_consume_sse(_diagnostics):
        return "ok", [], "stop", {}

    class _FakeResponse:
        def __init__(self) -> None:
            self.status_code = 200

        async def aread(self):
            return b""

    class _FakeStream:
        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            _ = args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

        def stream(self, method, url, *, headers=None, json=None):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            captured["body"] = dict(json or {})
            return _FakeStream()

    monkeypatch.setattr("g3ku.providers.responses_provider.httpx.AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(responses_provider_module, "_consume_sse", _fake_consume_sse)

    flat_tool_schema = {
        "type": "function",
        "name": "exec",
        "description": "Run a command",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }

    provider = ResponsesProvider(api_key="test-key", api_base="https://example.com/v1")
    response = await provider.chat(
        messages=[{"role": "user", "content": "ping"}],
        tools=[flat_tool_schema],
        model="demo",
    )

    assert response.provider_request_body["tools"] == [flat_tool_schema]
    assert dict(captured["body"])["tools"] == [flat_tool_schema]


@pytest.mark.asyncio
async def test_chat_backend_skips_outer_total_timeout_for_internally_managed_stream_provider(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    class _StreamingProvider:
        manages_request_timeout_internally = True

        async def chat(self, **kwargs):
            _ = kwargs
            return LLMResponse(content="ok", finish_reason="stop")

    async def _fake_wait_for_model_attempt(awaitable, **kwargs):
        captured.append(dict(kwargs))
        return await awaitable

    target = ProviderTarget(
        provider_ref="primary",
        provider_id="responses",
        model_id="gpt-primary",
        provider=_StreamingProvider(),
        retry_on=["network", "429", "5xx"],
        retry_count=0,
        api_key_count=1,
        api_key_indexes=[0],
    )

    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", lambda config, ref, api_key_index=None: target)
    monkeypatch.setattr(chat_backend_module, "wait_for_model_attempt", _fake_wait_for_model_attempt)

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    response = await backend.chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["primary"],
    )

    assert response.content == "ok"
    assert len(captured) == 1
    assert captured[0]["timeout_seconds"] is None


@pytest.mark.asyncio
async def test_chat_backend_forwards_text_delta_callback_only_to_streaming_providers(monkeypatch) -> None:
    captured: list[dict[str, object]] = []
    seen_deltas: list[str] = []

    class _StreamingProvider:
        manages_request_timeout_internally = True
        supports_streaming = True

        async def chat(self, **kwargs):
            captured.append(dict(kwargs))
            callback = kwargs.get("on_text_delta")
            if callable(callback):
                callback("O")
                callback("K")
            return LLMResponse(content="OK", finish_reason="stop")

    target = ProviderTarget(
        provider_ref="primary",
        provider_id="responses",
        model_id="gpt-primary",
        provider=_StreamingProvider(),
        retry_on=["network", "429", "5xx"],
        retry_count=0,
        api_key_count=1,
        api_key_indexes=[0],
    )

    monkeypatch.setattr(
        chat_backend_module,
        "build_provider_from_model_key",
        lambda config, ref, api_key_index=None: target,
    )

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    response = await backend.chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["primary"],
        on_text_delta=seen_deltas.append,
    )

    assert response.content == "OK"
    assert captured[0]["on_text_delta"] is seen_deltas.append
    assert seen_deltas == ["O", "K"]


@pytest.mark.asyncio
async def test_chat_backend_stops_model_fallback_after_visible_stream_text(monkeypatch) -> None:
    attempts: list[str] = []
    streamed_chunks: list[str] = []

    class _StreamingFailureProvider:
        manages_request_timeout_internally = True
        supports_streaming = True

        async def chat(self, **kwargs):
            attempts.append("primary")
            callback = kwargs.get("on_text_delta")
            if callable(callback):
                callback("partial")
            raise RuntimeError("HTTP 502: Upstream request failed")

    class _SecondaryProvider:
        manages_request_timeout_internally = True
        supports_streaming = True

        async def chat(self, **kwargs):
            _ = kwargs
            attempts.append("secondary")
            return LLMResponse(content="secondary ok", finish_reason="stop")

    targets = {
        "primary": ProviderTarget(
            provider_ref="primary",
            provider_id="responses",
            model_id="gpt-primary",
            provider=_StreamingFailureProvider(),
            retry_on=["network", "429", "5xx"],
            retry_count=0,
            api_key_count=1,
            api_key_indexes=[0],
        ),
        "secondary": ProviderTarget(
            provider_ref="secondary",
            provider_id="responses",
            model_id="gpt-secondary",
            provider=_SecondaryProvider(),
            retry_on=["network", "429", "5xx"],
            retry_count=0,
            api_key_count=1,
            api_key_indexes=[0],
        ),
    }

    monkeypatch.setattr(
        chat_backend_module,
        "build_provider_from_model_key",
        lambda config, ref, api_key_index=None: targets[ref],
    )

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())

    with pytest.raises(RuntimeError, match="HTTP 502: Upstream request failed"):
        await backend.chat(
            messages=[{"role": "user", "content": "demo"}],
            tools=None,
            model_refs=["primary", "secondary"],
            on_text_delta=streamed_chunks.append,
        )

    assert streamed_chunks == ["partial"]
    assert attempts == ["primary"]


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
async def test_custom_provider_uses_streaming_path_when_stream_is_supported(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _FakeStream:
        def __init__(self) -> None:
            self._chunks = [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="O", reasoning_content=None, tool_calls=[]),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                ),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="K", reasoning_content=None, tool_calls=[]),
                            finish_reason="stop",
                        )
                    ],
                    usage={"prompt_tokens": 1, "completion_tokens": 2},
                ),
            ]

        def __aiter__(self):
            self._index = 0
            return self

        async def __anext__(self):
            if self._index >= len(self._chunks):
                raise StopAsyncIteration
            chunk = self._chunks[self._index]
            self._index += 1
            return chunk

    class _FakeCompletions:
        async def create(self, **kwargs):
            calls.append(dict(kwargs))
            if kwargs.get("stream"):
                return _FakeStream()
            raise AssertionError("non-stream fallback should not be used")

    class _FakeAsyncOpenAI:
        def __init__(self, *args, **kwargs) -> None:
            _ = args, kwargs
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr("g3ku.providers.custom_provider.AsyncOpenAI", _FakeAsyncOpenAI)

    provider = CustomProvider(api_key="test-key", api_base="https://example.com/v1", default_model="demo")
    response = await provider.chat(
        messages=[{"role": "user", "content": "ping"}],
        model="demo",
        request_timeout_seconds=0.5,
    )

    assert response.content == "OK"
    assert response.finish_reason == "stop"
    assert response.usage == {"input_tokens": 1, "output_tokens": 2}
    assert len(calls) == 1
    assert calls[0]["stream"] is True


@pytest.mark.asyncio
async def test_custom_provider_falls_back_to_non_streaming_when_streaming_unsupported(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _FakeCompletions:
        async def create(self, **kwargs):
            calls.append(dict(kwargs))
            if kwargs.get("stream"):
                raise RuntimeError("stream unsupported by upstream")
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
    assert len(calls) == 2
    assert calls[0]["stream"] is True
    assert "stream" not in calls[1]
    assert calls[1]["timeout"] == 12.5


@pytest.mark.asyncio
async def test_custom_provider_fallback_defaults_to_120_seconds_without_explicit_timeout(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _FakeCompletions:
        async def create(self, **kwargs):
            calls.append(dict(kwargs))
            if kwargs.get("stream"):
                raise RuntimeError("stream unsupported by upstream")
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
    )

    assert response.content == "ok"
    assert len(calls) == 2
    assert calls[1]["timeout"] == 120.0


@pytest.mark.asyncio
async def test_custom_provider_normalizes_flat_function_tool_schemas_before_transport(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _FakeCompletions:
        async def create(self, **kwargs):
            calls.append(dict(kwargs))
            if kwargs.get("stream"):
                raise RuntimeError("stream unsupported by upstream")
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

    flat_tool_schema = {
        "type": "function",
        "name": "exec",
        "description": "Run a command",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }

    provider = CustomProvider(api_key="test-key", api_base="https://example.com/v1", default_model="demo")
    response = await provider.chat(
        messages=[{"role": "user", "content": "ping"}],
        tools=[flat_tool_schema],
        model="demo",
    )

    assert response.content == "ok"
    assert len(calls) == 2
    assert calls[1]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "exec",
                "description": "Run a command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_litellm_provider_falls_back_to_non_streaming_when_streaming_unsupported(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def _fake_acompletion(**kwargs):
        calls.append(dict(kwargs))
        if kwargs.get("stream"):
            raise RuntimeError("stream unsupported by provider")
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
    assert len(calls) == 2
    assert calls[0]["stream"] is True
    assert "stream" not in calls[1]
    assert calls[1]["timeout"] == 9.5


@pytest.mark.asyncio
async def test_litellm_provider_normalizes_flat_function_tool_schemas_before_transport(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def _fake_acompletion(**kwargs):
        calls.append(dict(kwargs))
        if kwargs.get("stream"):
            raise RuntimeError("stream unsupported by provider")
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

    flat_tool_schema = {
        "type": "function",
        "name": "exec",
        "description": "Run a command",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }

    provider = LiteLLMProvider(
        api_key="test-key",
        api_base="https://example.com/v1",
        default_model="openai/gpt-4.1",
        provider_name="openai",
    )
    response = await provider.chat(
        messages=[{"role": "user", "content": "ping"}],
        tools=[flat_tool_schema],
        model="openai/gpt-4.1",
        request_timeout_seconds=9.5,
    )

    assert response.content == "ok"
    assert len(calls) == 2
    assert calls[1]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "exec",
                "description": "Run a command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_litellm_provider_uses_streaming_path_when_stream_is_supported(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _FakeStream:
        def __init__(self) -> None:
            self._chunks = [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="O", reasoning_content=None, tool_calls=[]),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                ),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="K", reasoning_content=None, tool_calls=[]),
                            finish_reason="stop",
                        )
                    ],
                    usage={"prompt_tokens": 1, "completion_tokens": 2},
                ),
            ]

        def __aiter__(self):
            self._index = 0
            return self

        async def __anext__(self):
            if self._index >= len(self._chunks):
                raise StopAsyncIteration
            chunk = self._chunks[self._index]
            self._index += 1
            return chunk

    async def _fake_acompletion(**kwargs):
        calls.append(dict(kwargs))
        if kwargs.get("stream"):
            return _FakeStream()
        raise AssertionError("non-stream fallback should not be used")

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
        request_timeout_seconds=0.5,
    )

    assert response.content == "OK"
    assert response.finish_reason == "stop"
    assert response.usage == {"input_tokens": 1, "output_tokens": 2}
    assert len(calls) == 1
    assert calls[0]["stream"] is True


@pytest.mark.asyncio
async def test_litellm_provider_fallback_defaults_to_120_seconds_without_explicit_timeout(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def _fake_acompletion(**kwargs):
        calls.append(dict(kwargs))
        if kwargs.get("stream"):
            raise RuntimeError("stream unsupported by provider")
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
    )

    assert response.content == "ok"
    assert len(calls) == 2
    assert calls[1]["timeout"] == 120.0


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


@pytest.mark.asyncio
async def test_openai_codex_provider_times_out_when_first_chunk_exceeds_timeout(monkeypatch) -> None:
    class _FakeResponse:
        def __init__(self) -> None:
            self.status_code = 200

        async def aiter_lines(self):
            await asyncio.sleep(0.03)
            yield "event: response.created"

    class _FakeStream:
        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            _ = args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

        def stream(self, *args, **kwargs):
            _ = args, kwargs
            return _FakeStream()

    monkeypatch.setattr(
        "g3ku.providers.openai_codex_provider.get_codex_token",
        lambda: SimpleNamespace(account_id="acct", access="token"),
    )
    monkeypatch.setattr("g3ku.providers.openai_codex_provider.httpx.AsyncClient", _FakeAsyncClient)

    provider = OpenAICodexProvider(default_model="openai_codex/gpt-5.1-codex")
    response = await provider.chat(
        messages=[{"role": "user", "content": "ping"}],
        model="openai_codex/gpt-5.1-codex",
        request_timeout_seconds=0.01,
    )

    assert response.finish_reason == "error"
    assert "timeout" in str(response.content or "").lower()


@pytest.mark.asyncio
async def test_openai_codex_provider_times_out_when_stream_goes_idle_after_first_chunk(monkeypatch) -> None:
    class _FakeResponse:
        def __init__(self) -> None:
            self.status_code = 200

        async def aiter_lines(self):
            yield "event: response.created"
            await asyncio.sleep(0.03)
            yield 'data: {"type":"response.created"}'

    class _FakeStream:
        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            _ = args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return None

        def stream(self, *args, **kwargs):
            _ = args, kwargs
            return _FakeStream()

    monkeypatch.setattr(
        "g3ku.providers.openai_codex_provider.get_codex_token",
        lambda: SimpleNamespace(account_id="acct", access="token"),
    )
    monkeypatch.setattr("g3ku.providers.openai_codex_provider.httpx.AsyncClient", _FakeAsyncClient)

    provider = OpenAICodexProvider(default_model="openai_codex/gpt-5.1-codex")
    response = await provider.chat(
        messages=[{"role": "user", "content": "ping"}],
        model="openai_codex/gpt-5.1-codex",
        request_timeout_seconds=0.01,
    )

    assert response.finish_reason == "error"
    assert "idle timeout" in str(response.content or "").lower()


@pytest.mark.asyncio
async def test_openai_codex_provider_preserves_flat_function_tool_schemas_in_transport_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_request_codex(url, headers, body, verify, timeout):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        captured["body"] = dict(body or {})
        captured["verify"] = verify
        captured["timeout"] = timeout
        return "ok", [], "stop", {}

    monkeypatch.setattr(
        "g3ku.providers.openai_codex_provider.get_codex_token",
        lambda: SimpleNamespace(account_id="acct", access="token"),
    )
    monkeypatch.setattr("g3ku.providers.openai_codex_provider._request_codex", _fake_request_codex)

    flat_tool_schema = {
        "type": "function",
        "name": "exec",
        "description": "Run a command",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }

    provider = OpenAICodexProvider(default_model="openai_codex/gpt-5.1-codex")
    response = await provider.chat(
        messages=[{"role": "user", "content": "ping"}],
        tools=[flat_tool_schema],
        model="openai_codex/gpt-5.1-codex",
    )

    assert response.provider_request_body["tools"] == [flat_tool_schema]
    assert dict(captured["body"])["tools"] == [flat_tool_schema]


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
        provider_id="custom",
        protocol_adapter=ProtocolAdapter.CUSTOM_DIRECT,
        base_url="https://example.com/v1",
        default_model="custom-model",
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


def test_probe_config_openai_responses_falls_back_when_model_catalog_returns_html() -> None:
    config = _config(
        provider_id="responses",
        protocol_adapter=ProtocolAdapter.OPENAI_RESPONSES,
        base_url="https://example.com/v1",
        default_model="gpt-5.4",
        api_key="test-key",
    )

    seen_paths: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><title>Sign in</title><body>login required</body></html>",
            )
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, json={"id": "resp_123", "object": "response"})

    result = probe_config(config, transport=httpx.MockTransport(_handler))

    assert result.success is True
    assert result.message == "Fallback request succeeded."
    assert result.diagnostics["fallback_used"] is True
    assert result.diagnostics["api_key_count"] == 1
    assert result.diagnostics["api_key_attempts"] == 1
    assert seen_paths == ["/v1/models", "/v1/responses"]


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


def test_probe_config_uses_30_second_timeout(monkeypatch) -> None:
    config = _config(
        provider_id="custom",
        protocol_adapter=ProtocolAdapter.CUSTOM_DIRECT,
        base_url="https://example.com/v1",
        default_model="custom-model",
        api_key="test-key",
    )

    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, headers=None):
            return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(probe_strategies_module.httpx, "Client", _FakeClient)

    result = probe_config(config)

    assert result.success is True
    assert captured["timeout"] == 30


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

