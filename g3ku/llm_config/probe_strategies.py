from __future__ import annotations

import json
import time
from typing import Any

import httpx

from g3ku.json_schema_utils import (
    normalize_openai_tool_definitions,
    normalize_responses_tool_definitions,
)
from g3ku.utils.api_keys import parse_api_keys, should_switch_api_key_for_http_status

from .enums import AuthMode, ProbeStatus, ProtocolAdapter
from .models import ModelCatalogResult, NormalizedProviderConfig, ProbeResult

_PROBE_TIMEOUT_SECONDS = 30


def _join_url(base_url: str, suffix: str) -> str:
    return f"{base_url.rstrip('/')}/{suffix.lstrip('/')}"


def _response_content_type(response: httpx.Response) -> str:
    raw_value = str(response.headers.get("content-type", "") or "").strip()
    if not raw_value:
        return ""
    return raw_value.split(";", 1)[0].strip()


def _response_body_preview(response: httpx.Response, *, limit: int = 160) -> str:
    body = str(response.text or "")
    normalized = " ".join(body.split())
    if not normalized:
        return ""
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit].rstrip()}..."


def _non_json_failure(
    config: NormalizedProviderConfig,
    *,
    response: httpx.Response,
    latency_ms: int,
    label: str,
) -> ProbeResult:
    content_type = _response_content_type(response)
    details: list[str] = []
    if response.status_code:
        details.append(f"HTTP {response.status_code}")
    if content_type:
        details.append(content_type)
    detail_suffix = f" ({', '.join(details)})" if details else ""
    diagnostics: dict[str, Any] = {}
    if content_type:
        diagnostics["content_type"] = content_type
    preview = _response_body_preview(response)
    if preview:
        diagnostics["body_preview"] = preview
    return _failure_result(
        config,
        status=ProbeStatus.INVALID_RESPONSE,
        http_status=response.status_code,
        latency_ms=latency_ms,
        message=(
            f"{label}{detail_suffix}. "
            "This usually means the Base URL points to a web page, auth portal, or full endpoint path instead of the provider API root."
        ),
        diagnostics=diagnostics,
    )


def _success_result(
    config: NormalizedProviderConfig,
    *,
    latency_ms: int,
    http_status: int,
    message: str,
    diagnostics: dict[str, Any] | None = None,
) -> ProbeResult:
    return ProbeResult(
        status=ProbeStatus.SUCCESS,
        success=True,
        provider_id=config.provider_id,
        protocol_adapter=config.protocol_adapter,
        capability=config.capability,
        resolved_base_url=config.base_url,
        checked_model=config.default_model,
        latency_ms=latency_ms,
        http_status=http_status,
        message=message,
        diagnostics=diagnostics or {},
    )


def _failure_result(
    config: NormalizedProviderConfig,
    *,
    status: ProbeStatus,
    message: str,
    http_status: int | None = None,
    latency_ms: int | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> ProbeResult:
    return ProbeResult(
        status=status,
        success=False,
        provider_id=config.provider_id,
        protocol_adapter=config.protocol_adapter,
        capability=config.capability,
        resolved_base_url=config.base_url,
        checked_model=config.default_model,
        latency_ms=latency_ms,
        http_status=http_status,
        message=message,
        diagnostics=diagnostics or {},
    )


def _with_bearer_auth(headers: dict[str, str], api_key: Any) -> dict[str, str]:
    next_headers = dict(headers)
    token = str(api_key or "").strip()
    if token:
        next_headers["Authorization"] = f"Bearer {token}"
    return next_headers


def _build_openai_headers(config: NormalizedProviderConfig) -> dict[str, str]:
    headers = dict(config.headers)
    api_key = str(config.auth.get("api_key", "") or "").strip()
    use_auth_header = bool(config.parameters.get("auth_header", True))
    if use_auth_header:
        headers = _with_bearer_auth(headers, api_key)
    elif api_key:
        headers["x-api-key"] = api_key
    return headers



def _probe_reasoning_effort(config: NormalizedProviderConfig) -> str:
    effort = str(config.parameters.get("reasoning_effort") or "").strip().lower()
    return "" if effort in {"", "none"} else effort


PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "g3ku_connection_probe",
        "description": "Connection probe placeholder. Never call it.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _build_openai_fallback_payload(config: NormalizedProviderConfig) -> tuple[str, dict[str, Any]]:
    """A ping shaped like the real request.

    The field set mirrors `ResponsesProvider.chat` / `OpenAIChatProvider.chat` for a
    tools-present call, and the tool goes through the same normalizer each sender uses,
    so a backend that rejects one of those fields fails 测试连接 instead of failing the
    first real turn.
    """
    if config.protocol_adapter == ProtocolAdapter.OPENAI_RESPONSES:
        payload: dict[str, Any] = {
            "model": config.default_model,
            "store": False,
            "stream": True,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": "g3ku-connection-probe",
            "max_output_tokens": 1,
            "tools": normalize_responses_tool_definitions([PROBE_TOOL]),
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        effort = _probe_reasoning_effort(config)
        if effort:
            payload["reasoning"] = {"effort": effort}
        return "/responses", payload
    payload = {
        "model": config.default_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": True,
        "stream_options": {"include_usage": True},
        "tools": normalize_openai_tool_definitions([PROBE_TOOL]),
        "tool_choice": "auto",
    }
    effort = _probe_reasoning_effort(config)
    if effort:
        payload["reasoning_effort"] = effort
    return "/chat/completions", payload


def _extract_upstream_error_detail(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return "no detail returned"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:200]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        parts = [str(error.get("message") or "").strip(), str(error.get("code") or "").strip()]
        detail = " | ".join(part for part in parts if part)
        if detail:
            return detail[:200]
    if isinstance(error, str) and error.strip():
        return error.strip()[:200]
    return text[:200]


def _probe_openai_inference_envelope(
    client: httpx.Client,
    config: NormalizedProviderConfig,
    *,
    label: str = "Inference",
    extra_diagnostics: dict[str, Any] | None = None,
) -> ProbeResult:
    headers = _build_openai_headers(config)
    endpoint, payload = _build_openai_fallback_payload(config)
    start = time.perf_counter()
    with client.stream("POST", _join_url(config.base_url, endpoint), headers=headers, json=payload) as response:
        http_status = response.status_code
        if http_status in {401, 403}:
            return _failure_result(
                config,
                status=ProbeStatus.AUTH_ERROR,
                http_status=http_status,
                latency_ms=int((time.perf_counter() - start) * 1000),
                message="Authentication failed during inference request.",
            )
        if 200 <= http_status < 300:
            return _success_result(
                config,
                latency_ms=int((time.perf_counter() - start) * 1000),
                http_status=http_status,
                message=f"{label} request succeeded.",
                diagnostics={
                    **(extra_diagnostics or {}),
                    "request_fields": sorted(payload.keys()),
                    "response_content_type": _response_content_type(response),
                },
            )
        detail = _extract_upstream_error_detail(response.read().decode("utf-8", "ignore"))
    return _failure_result(
        config,
        status=ProbeStatus.INVALID_RESPONSE,
        http_status=http_status,
        latency_ms=int((time.perf_counter() - start) * 1000),
        message=f"{label} request failed: {detail}",
        diagnostics={**(extra_diagnostics or {}), "upstream_detail": detail},
    )


def _config_with_api_key(config: NormalizedProviderConfig, api_key: str) -> NormalizedProviderConfig:
    auth = dict(config.auth)
    auth["api_key"] = str(api_key or "").strip()
    return config.model_copy(update={"auth": auth})


def _with_probe_attempt_diagnostics(
    result: ProbeResult,
    *,
    api_key_count: int,
    api_key_attempts: int,
) -> ProbeResult:
    diagnostics = dict(result.diagnostics)
    diagnostics["api_key_count"] = max(0, int(api_key_count or 0))
    diagnostics["api_key_attempts"] = max(0, int(api_key_attempts or 0))
    return result.model_copy(update={"diagnostics": diagnostics})


def _should_switch_api_key_for_probe_result(result: ProbeResult) -> bool:
    if result.status in {ProbeStatus.AUTH_ERROR, ProbeStatus.CONNECTION_ERROR, ProbeStatus.TIMEOUT}:
        return True
    return should_switch_api_key_for_http_status(result.http_status)


def _probe_openai_compatible(client: httpx.Client, config: NormalizedProviderConfig) -> ProbeResult:
    headers = _build_openai_headers(config)
    start = time.perf_counter()
    response = client.get(_join_url(config.base_url, "/models"), headers=headers)
    latency_ms = int((time.perf_counter() - start) * 1000)
    if response.status_code in {401, 403}:
        return _failure_result(
            config,
            status=ProbeStatus.AUTH_ERROR,
            http_status=response.status_code,
            latency_ms=latency_ms,
            message="Authentication failed while requesting model catalog.",
        )
    if 200 <= response.status_code < 300:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            if config.protocol_adapter != ProtocolAdapter.OPENAI_RESPONSES:
                return _non_json_failure(
                    config,
                    response=response,
                    latency_ms=latency_ms,
                    label="Model catalog returned a non-JSON response",
                )
            payload = None
        if payload is not None:
            model_count = None
            if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                model_count = len(payload["data"])
            elif isinstance(payload, list):
                model_count = len(payload)
            # A readable model catalog only proves the credentials reach the provider;
            # the model itself is only exercised by a real inference envelope.
            envelope = _probe_openai_inference_envelope(client, config, label="Envelope")
            if not envelope.success:
                return envelope.model_copy(
                    update={
                        "diagnostics": {**envelope.diagnostics, "model_count": model_count, "catalog_ok": True}
                    }
                )
            return _success_result(
                config,
                latency_ms=latency_ms,
                http_status=response.status_code,
                message="Model catalog request succeeded.",
                diagnostics={"model_count": model_count, "envelope_checked": True},
            )
    return _probe_openai_inference_envelope(
        client,
        config,
        label="Fallback",
        extra_diagnostics={"fallback_used": True},
    )



def _probe_single_config(
    config: NormalizedProviderConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ProbeResult:
    timeout_value = _PROBE_TIMEOUT_SECONDS
    try:
        with httpx.Client(timeout=timeout_value, transport=transport, follow_redirects=True) as client:
            return _probe_openai_compatible(client, config)
    except httpx.TimeoutException:
        return _failure_result(config, status=ProbeStatus.TIMEOUT, message="Probe timed out.")
    except (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError):
        return _failure_result(
            config,
            status=ProbeStatus.CONNECTION_ERROR,
            message="Could not connect to the provider endpoint.",
        )


def _probe_single_config_for_concurrency(
    config: NormalizedProviderConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ProbeResult:
    timeout_value = _PROBE_TIMEOUT_SECONDS
    try:
        with httpx.Client(timeout=timeout_value, transport=transport, follow_redirects=True) as client:
            return _probe_openai_inference_envelope(client, config)
    except httpx.TimeoutException:
        return _failure_result(config, status=ProbeStatus.TIMEOUT, message="Probe timed out.")
    except (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError):
        return _failure_result(
            config,
            status=ProbeStatus.CONNECTION_ERROR,
            message="Could not connect to the provider endpoint.",
        )


def probe_config(
    config: NormalizedProviderConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ProbeResult:
    if config.auth_mode != AuthMode.API_KEY:
        return _probe_single_config(config, transport=transport)

    api_keys = parse_api_keys(str(config.auth.get("api_key", "") or ""))
    if not api_keys:
        return _with_probe_attempt_diagnostics(
            _probe_single_config(config, transport=transport),
            api_key_count=0,
            api_key_attempts=0,
        )

    last_result: ProbeResult | None = None
    for attempt_index, api_key in enumerate(api_keys, start=1):
        result = _probe_single_config(_config_with_api_key(config, api_key), transport=transport)
        result = _with_probe_attempt_diagnostics(
            result,
            api_key_count=len(api_keys),
            api_key_attempts=attempt_index,
        )
        if result.success:
            return result
        last_result = result
        if not _should_switch_api_key_for_probe_result(result):
            return result

    if last_result is not None:
        return last_result
    return _failure_result(config, status=ProbeStatus.INVALID_RESPONSE, message="Probe failed.")


def probe_config_for_concurrency(
    config: NormalizedProviderConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ProbeResult:
    if config.auth_mode != AuthMode.API_KEY:
        return _probe_single_config_for_concurrency(config, transport=transport)

    api_keys = parse_api_keys(str(config.auth.get("api_key", "") or ""))
    if not api_keys:
        return _with_probe_attempt_diagnostics(
            _probe_single_config_for_concurrency(config, transport=transport),
            api_key_count=0,
            api_key_attempts=0,
        )

    last_result: ProbeResult | None = None
    for attempt_index, api_key in enumerate(api_keys, start=1):
        result = _probe_single_config_for_concurrency(_config_with_api_key(config, api_key), transport=transport)
        result = _with_probe_attempt_diagnostics(
            result,
            api_key_count=len(api_keys),
            api_key_attempts=attempt_index,
        )
        if result.success:
            return result
        last_result = result
        if not _should_switch_api_key_for_probe_result(result):
            return result

    if last_result is not None:
        return last_result
    return _failure_result(config, status=ProbeStatus.INVALID_RESPONSE, message="Probe failed.")


def _model_catalog_result(
    config: NormalizedProviderConfig,
    *,
    success: bool,
    message: str,
    models: list[str] | None = None,
    http_status: int | None = None,
    latency_ms: int | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> ModelCatalogResult:
    return ModelCatalogResult(
        success=success,
        provider_id=config.provider_id,
        resolved_base_url=config.base_url,
        models=list(models or []),
        message=message,
        latency_ms=latency_ms,
        http_status=http_status,
        diagnostics=diagnostics or {},
    )


def _extract_model_ids(payload: Any) -> list[str]:
    items: Any = None
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload.get("models"), list):
            items = payload["models"]
    elif isinstance(payload, list):
        items = payload
    if items is None:
        return []
    model_ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            value = item.strip()
        elif isinstance(item, dict):
            value = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
        else:
            value = ""
        if value and value not in model_ids:
            model_ids.append(value)
    return sorted(model_ids, key=str.lower)


def _list_models_single_request(client: httpx.Client, config: NormalizedProviderConfig) -> ModelCatalogResult:
    headers = _build_openai_headers(config)
    start = time.perf_counter()
    response = client.get(_join_url(config.base_url, "/models"), headers=headers)
    latency_ms = int((time.perf_counter() - start) * 1000)
    if response.status_code in {401, 403}:
        return _model_catalog_result(
            config,
            success=False,
            http_status=response.status_code,
            latency_ms=latency_ms,
            message="Authentication failed while requesting model catalog.",
        )
    if not 200 <= response.status_code < 300:
        return _model_catalog_result(
            config,
            success=False,
            http_status=response.status_code,
            latency_ms=latency_ms,
            message=f"Model catalog request failed with HTTP {response.status_code}.",
            diagnostics={"body_preview": _response_body_preview(response)},
        )
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return _model_catalog_result(
            config,
            success=False,
            http_status=response.status_code,
            latency_ms=latency_ms,
            message=(
                "Model catalog returned a non-JSON response. "
                "This usually means the Base URL points to a web page, auth portal, or full endpoint path instead of the provider API root."
            ),
            diagnostics={"body_preview": _response_body_preview(response)},
        )
    model_ids = _extract_model_ids(payload)
    if not model_ids:
        return _model_catalog_result(
            config,
            success=False,
            http_status=response.status_code,
            latency_ms=latency_ms,
            message="Model catalog response has no recognizable model entries.",
            diagnostics={"body_preview": _response_body_preview(response)},
        )
    return _model_catalog_result(
        config,
        success=True,
        http_status=response.status_code,
        latency_ms=latency_ms,
        models=model_ids,
        message=f"Model catalog returned {len(model_ids)} models.",
    )


def _list_models_single(
    config: NormalizedProviderConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ModelCatalogResult:
    try:
        with httpx.Client(timeout=_PROBE_TIMEOUT_SECONDS, transport=transport, follow_redirects=True) as client:
            return _list_models_single_request(client, config)
    except httpx.TimeoutException:
        return _model_catalog_result(config, success=False, message="Model catalog request timed out.")
    except (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError):
        return _model_catalog_result(
            config,
            success=False,
            message="Could not connect to the provider endpoint.",
        )


def _should_switch_api_key_for_catalog_result(result: ModelCatalogResult) -> bool:
    if result.http_status is not None:
        return should_switch_api_key_for_http_status(result.http_status)
    message = str(result.message or "").lower()
    return "authentication" in message or "connect" in message or "timed out" in message


def list_config_models(
    config: NormalizedProviderConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ModelCatalogResult:
    if config.auth_mode != AuthMode.API_KEY:
        return _list_models_single(config, transport=transport)

    api_keys = parse_api_keys(str(config.auth.get("api_key", "") or ""))
    if not api_keys:
        return _list_models_single(config, transport=transport)

    last_result: ModelCatalogResult | None = None
    for api_key in api_keys:
        result = _list_models_single(_config_with_api_key(config, api_key), transport=transport)
        if result.success:
            return result
        last_result = result
        if not _should_switch_api_key_for_catalog_result(result):
            return result

    if last_result is not None:
        return last_result
    return _model_catalog_result(config, success=False, message="Model catalog request failed.")
