from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from g3ku.llm_config.enums import AuthMode, Capability, ProbeStatus, ProtocolAdapter
from g3ku.llm_config.models import NormalizedProviderConfig
from g3ku.llm_config.probe_strategies import _build_openai_headers, probe_config
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


@pytest.mark.asyncio
async def test_responses_provider_refuses_empty_bearer_header(monkeypatch) -> None:
    class _UnexpectedAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("HTTP client should not be created when api_key is empty")

    monkeypatch.setattr("g3ku.providers.responses_provider.httpx.AsyncClient", _UnexpectedAsyncClient)
    provider = ResponsesProvider(api_key="   ", api_base="https://example.com/v1/responses")

    with pytest.raises(ValueError, match="empty Authorization header"):
        await provider.chat(messages=[{"role": "user", "content": "ping"}])


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

