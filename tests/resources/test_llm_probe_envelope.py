"""连接探测必须发真实请求形状。

探测原本只发最小体（模型 + 一条 ping），所以 `text.verbosity`、`instructions` 这类
被上游整单拒绝的字段只在真实回合才暴露；模型目录可用时甚至根本不碰推理端点。
这里锁住两件事：探测体与 provider 发送体的字段集合一致，以及目录通过但推理被拒
必须判失败。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from g3ku.llm_config.enums import AuthMode, Capability, ProbeStatus, ProtocolAdapter
from g3ku.llm_config.models import NormalizedProviderConfig
from g3ku.llm_config.probe_strategies import _build_openai_fallback_payload, probe_config


def _config(
    *,
    provider_id: str,
    protocol_adapter: ProtocolAdapter,
    parameters: dict | None = None,
) -> NormalizedProviderConfig:
    now = datetime.now(UTC)
    return NormalizedProviderConfig(
        config_id="cfg-probe",
        provider_id=provider_id,
        display_name="probe",
        protocol_adapter=protocol_adapter,
        capability=Capability.CHAT,
        auth_mode=AuthMode.API_KEY,
        base_url="https://provider.test/v1",
        default_model="model-x",
        auth={"type": "api_key", "api_key": "test-key"},
        parameters=parameters or {},
        headers={},
        extra_options={},
        template_version="test",
        created_at=now,
        updated_at=now,
    )


RESPONSES_SENDER_FIELDS = {
    "model",
    "store",
    "stream",
    "input",
    "include",
    "prompt_cache_key",
    "max_output_tokens",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
}

CHAT_SENDER_FIELDS = {
    "model",
    "messages",
    "max_tokens",
    "stream",
    "stream_options",
    "tools",
    "tool_choice",
}


def test_responses_probe_payload_matches_sender_fields() -> None:
    config = _config(
        provider_id="responses",
        protocol_adapter=ProtocolAdapter.OPENAI_RESPONSES,
        parameters={"reasoning_effort": "xhigh"},
    )

    endpoint, payload = _build_openai_fallback_payload(config)

    assert endpoint == "/responses"
    assert RESPONSES_SENDER_FIELDS <= set(payload)
    assert payload["reasoning"] == {"effort": "xhigh"}
    assert payload["stream"] is True
    assert payload["store"] is False
    # 这两个字段会把代理到 Chat Completions 后端的整单请求打回。
    assert "instructions" not in payload
    assert "text" not in payload
    assert payload["input"][0]["content"][0]["type"] == "input_text"
    assert payload["tools"][0]["type"] == "function"
    assert "function" not in payload["tools"][0]


def test_chat_probe_payload_matches_sender_fields() -> None:
    config = _config(
        provider_id="openai",
        protocol_adapter=ProtocolAdapter.OPENAI_COMPLETIONS,
        parameters={"reasoning_effort": "high"},
    )

    endpoint, payload = _build_openai_fallback_payload(config)

    assert endpoint == "/chat/completions"
    assert CHAT_SENDER_FIELDS <= set(payload)
    assert payload["reasoning_effort"] == "high"
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["tools"][0]["function"]["name"] == "g3ku_connection_probe"


def test_chat_probe_payload_omits_reasoning_when_effort_is_none() -> None:
    config = _config(
        provider_id="openai",
        protocol_adapter=ProtocolAdapter.OPENAI_COMPLETIONS,
        parameters={"reasoning_effort": "none"},
    )

    _endpoint, payload = _build_openai_fallback_payload(config)

    assert "reasoning_effort" not in payload


def test_probe_fails_when_catalog_passes_but_envelope_is_rejected() -> None:
    config = _config(
        provider_id="responses",
        protocol_adapter=ProtocolAdapter.OPENAI_RESPONSES,
        parameters={"reasoning_effort": "medium"},
    )
    seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "model-x"}]})
        return httpx.Response(
            400,
            json={"error": {"message": "inference request is invalid", "code": "invalid_parameter_error"}},
        )

    result = probe_config(config, transport=httpx.MockTransport(_handler))

    assert seen == ["/v1/models", "/v1/responses"]
    assert result.success is False
    assert result.status == ProbeStatus.INVALID_RESPONSE
    assert "inference request is invalid" in result.message
    assert "invalid_parameter_error" in result.message
    assert result.diagnostics["catalog_ok"] is True
    assert result.diagnostics["model_count"] == 1


def test_probe_succeeds_when_catalog_and_envelope_both_pass() -> None:
    config = _config(
        provider_id="responses",
        protocol_adapter=ProtocolAdapter.OPENAI_RESPONSES,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "model-x"}, {"id": "model-y"}]})
        return httpx.Response(200, json={"id": "resp_1", "object": "response"})

    result = probe_config(config, transport=httpx.MockTransport(_handler))

    assert result.success is True
    assert result.message == "Model catalog request succeeded."
    assert result.diagnostics["model_count"] == 2
    assert result.diagnostics["envelope_checked"] is True
