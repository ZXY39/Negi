from __future__ import annotations

import json
import time
from typing import Any

import httpx

from g3ku.utils.api_keys import parse_api_keys, should_switch_api_key_for_http_status

from .enums import AuthMode, ProbeStatus, ProtocolAdapter
from .models import NormalizedProviderConfig, ProbeResult

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


def _build_anthropic_url(base_url: str) -> str:
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1") or trimmed.endswith("/anthropic"):
        return f"{trimmed}/messages"
    return f"{trimmed}/v1/messages"


def _build_openai_fallback_payload(config: NormalizedProviderConfig) -> tuple[str, dict[str, Any]]:
    endpoint = "/responses" if config.protocol_adapter == ProtocolAdapter.OPENAI_RESPONSES else "/chat/completions"
    if endpoint == "/responses":
        return endpoint, {"model": config.default_model, "input": "ping", "max_output_tokens": 1}
    return endpoint, {
        "model": config.default_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }


def _probe_openai_minimal_inference(client: httpx.Client, config: NormalizedProviderConfig) -> ProbeResult:
    headers = _build_openai_headers(config)
    endpoint, payload = _build_openai_fallback_payload(config)
    start = time.perf_counter()
    response = client.post(_join_url(config.base_url, endpoint), headers=headers, json=payload)
    latency_ms = int((time.perf_counter() - start) * 1000)
    if response.status_code in {401, 403}:
        return _failure_result(
            config,
            status=ProbeStatus.AUTH_ERROR,
            http_status=response.status_code,
            latency_ms=latency_ms,
            message="Authentication failed during minimal inference request.",
        )
    try:
        response_payload = response.json()
    except json.JSONDecodeError:
        return _non_json_failure(
            config,
            response=response,
            latency_ms=latency_ms,
            label="Minimal inference endpoint returned a non-JSON response",
        )
    if 200 <= response.status_code < 300:
        return _success_result(
            config,
            latency_ms=latency_ms,
            http_status=response.status_code,
            message="Minimal inference request succeeded.",
            diagnostics={"response_keys": sorted(response_payload.keys()) if isinstance(response_payload, dict) else []},
        )
    return _failure_result(
        config,
        status=ProbeStatus.INVALID_RESPONSE,
        http_status=response.status_code,
        latency_ms=latency_ms,
        message="Minimal inference request failed.",
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
            return _success_result(
                config,
                latency_ms=latency_ms,
                http_status=response.status_code,
                message="Model catalog request succeeded.",
                diagnostics={"model_count": model_count},
            )
    endpoint, payload = _build_openai_fallback_payload(config)
    fallback = client.post(_join_url(config.base_url, endpoint), headers=headers, json=payload)
    if fallback.status_code in {401, 403}:
        return _failure_result(
            config,
            status=ProbeStatus.AUTH_ERROR,
            http_status=fallback.status_code,
            latency_ms=latency_ms,
            message="Authentication failed during fallback request.",
        )
    try:
        fallback_payload = fallback.json()
    except json.JSONDecodeError:
        return _non_json_failure(
            config,
            response=fallback,
            latency_ms=latency_ms,
            label="Fallback endpoint returned a non-JSON response",
        )
    if 200 <= fallback.status_code < 300:
        return _success_result(
            config,
            latency_ms=latency_ms,
            http_status=fallback.status_code,
            message="Fallback request succeeded.",
            diagnostics={"fallback_used": True, "response_keys": sorted(fallback_payload.keys())},
        )
    return _failure_result(
        config,
        status=ProbeStatus.INVALID_RESPONSE,
        http_status=fallback.status_code,
        latency_ms=latency_ms,
        message="Fallback request failed.",
        diagnostics={"fallback_used": True},
    )


def _probe_anthropic_compatible(client: httpx.Client, config: NormalizedProviderConfig) -> ProbeResult:
    headers = dict(config.headers)
    headers["x-api-key"] = str(config.auth.get("api_key", ""))
    headers.setdefault("anthropic-version", str(config.parameters.get("anthropic_version", "2023-06-01")))
    payload = {
        "model": config.default_model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    start = time.perf_counter()
    response = client.post(_build_anthropic_url(config.base_url), headers=headers, json=payload)
    latency_ms = int((time.perf_counter() - start) * 1000)
    if response.status_code in {401, 403}:
        return _failure_result(
            config,
            status=ProbeStatus.AUTH_ERROR,
            http_status=response.status_code,
            latency_ms=latency_ms,
            message="Authentication failed during anthropic-compatible probe.",
        )
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return _non_json_failure(
            config,
            response=response,
            latency_ms=latency_ms,
            label="Anthropic-compatible endpoint returned a non-JSON response",
        )
    if 200 <= response.status_code < 300:
        return _success_result(
            config,
            latency_ms=latency_ms,
            http_status=response.status_code,
            message="Anthropic-compatible probe succeeded.",
            diagnostics={"response_keys": sorted(payload.keys()) if isinstance(payload, dict) else []},
        )
    return _failure_result(
        config,
        status=ProbeStatus.INVALID_RESPONSE,
        http_status=response.status_code,
        latency_ms=latency_ms,
        message="Anthropic-compatible probe failed.",
    )


def _probe_gemini(client: httpx.Client, config: NormalizedProviderConfig) -> ProbeResult:
    api_version = str(config.parameters.get("api_version", "v1beta")).strip() or "v1beta"
    prefix = config.base_url.rstrip("/")
    if not prefix.endswith(f"/{api_version}"):
        prefix = f"{prefix}/{api_version}"
    endpoint = f"{prefix}/models/{config.default_model}:generateContent"
    params = {"key": str(config.auth.get("api_key", ""))}
    payload = {
        "contents": [{"parts": [{"text": "ping"}]}],
        "generationConfig": {"maxOutputTokens": 1},
    }
    start = time.perf_counter()
    response = client.post(endpoint, params=params, json=payload, headers=config.headers)
    latency_ms = int((time.perf_counter() - start) * 1000)
    if response.status_code in {401, 403}:
        return _failure_result(
            config,
            status=ProbeStatus.AUTH_ERROR,
            http_status=response.status_code,
            latency_ms=latency_ms,
            message="Authentication failed during Gemini probe.",
        )
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return _non_json_failure(
            config,
            response=response,
            latency_ms=latency_ms,
            label="Gemini endpoint returned a non-JSON response",
        )
    if 200 <= response.status_code < 300:
        return _success_result(
            config,
            latency_ms=latency_ms,
            http_status=response.status_code,
            message="Gemini probe succeeded.",
            diagnostics={"response_keys": sorted(payload.keys()) if isinstance(payload, dict) else []},
        )
    return _failure_result(
        config,
        status=ProbeStatus.INVALID_RESPONSE,
        http_status=response.status_code,
        latency_ms=latency_ms,
        message="Gemini probe failed.",
    )


def _probe_dashscope_embedding(client: httpx.Client, config: NormalizedProviderConfig) -> ProbeResult:
    endpoint = _join_url(
        config.base_url,
        "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding",
    )
    headers = _with_bearer_auth(config.headers, config.auth.get("api_key", ""))
    payload = {
        "model": config.default_model,
        "input": {"contents": [{"text": "ping"}]},
        "parameters": {"output_type": "dense"},
    }
    start = time.perf_counter()
    response = client.post(endpoint, headers=headers, json=payload)
    latency_ms = int((time.perf_counter() - start) * 1000)
    if response.status_code in {401, 403}:
        return _failure_result(config, status=ProbeStatus.AUTH_ERROR, http_status=response.status_code, latency_ms=latency_ms, message="DashScope embedding auth failed.")
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return _non_json_failure(
            config,
            response=response,
            latency_ms=latency_ms,
            label="DashScope embedding returned a non-JSON response",
        )
    if 200 <= response.status_code < 300:
        return _success_result(config, latency_ms=latency_ms, http_status=response.status_code, message="DashScope embedding probe succeeded.", diagnostics={"response_keys": sorted(payload.keys()) if isinstance(payload, dict) else []})
    return _failure_result(config, status=ProbeStatus.INVALID_RESPONSE, http_status=response.status_code, latency_ms=latency_ms, message="DashScope embedding probe failed.")


def _probe_dashscope_rerank(client: httpx.Client, config: NormalizedProviderConfig) -> ProbeResult:
    endpoint = _join_url(config.base_url, "/api/v1/services/rerank/text-rerank/text-rerank")
    headers = _with_bearer_auth(config.headers, config.auth.get("api_key", ""))
    payload = {
        "model": config.default_model,
        "input": {"query": "ping", "documents": [{"text": "ping"}]},
        "parameters": {"return_documents": False, "top_n": 1},
    }
    start = time.perf_counter()
    response = client.post(endpoint, headers=headers, json=payload)
    latency_ms = int((time.perf_counter() - start) * 1000)
    if response.status_code in {401, 403}:
        return _failure_result(config, status=ProbeStatus.AUTH_ERROR, http_status=response.status_code, latency_ms=latency_ms, message="DashScope rerank auth failed.")
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return _non_json_failure(
            config,
            response=response,
            latency_ms=latency_ms,
            label="DashScope rerank returned a non-JSON response",
        )
    if 200 <= response.status_code < 300:
        return _success_result(config, latency_ms=latency_ms, http_status=response.status_code, message="DashScope rerank probe succeeded.", diagnostics={"response_keys": sorted(payload.keys()) if isinstance(payload, dict) else []})
    return _failure_result(config, status=ProbeStatus.INVALID_RESPONSE, http_status=response.status_code, latency_ms=latency_ms, message="DashScope rerank probe failed.")


def _probe_ollama(client: httpx.Client, config: NormalizedProviderConfig) -> ProbeResult:
    start = time.perf_counter()
    response = client.get(_join_url(config.base_url, "/api/tags"), headers=config.headers)
    latency_ms = int((time.perf_counter() - start) * 1000)
    if response.status_code in {401, 403}:
        return _failure_result(
            config,
            status=ProbeStatus.AUTH_ERROR,
            http_status=response.status_code,
            latency_ms=latency_ms,
            message="Authentication failed during Ollama probe.",
        )
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return _non_json_failure(
            config,
            response=response,
            latency_ms=latency_ms,
            label="Ollama returned a non-JSON response",
        )
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return _failure_result(
            config,
            status=ProbeStatus.INVALID_RESPONSE,
            http_status=response.status_code,
            latency_ms=latency_ms,
            message="Ollama response did not include a models list.",
        )
    model_names = [entry.get("name") for entry in models if isinstance(entry, dict) and entry.get("name")]
    if config.default_model not in model_names:
        return _failure_result(
            config,
            status=ProbeStatus.INVALID_RESPONSE,
            http_status=response.status_code,
            latency_ms=latency_ms,
            message="Configured Ollama model is not available on the server.",
            diagnostics={"available_models": model_names},
        )
    return _success_result(
        config,
        latency_ms=latency_ms,
        http_status=response.status_code,
        message="Ollama probe succeeded.",
        diagnostics={"available_models": model_names},
    )


def _probe_single_config(
    config: NormalizedProviderConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ProbeResult:
    timeout_value = _PROBE_TIMEOUT_SECONDS
    try:
        with httpx.Client(timeout=timeout_value, transport=transport, follow_redirects=True) as client:
            if config.protocol_adapter in {
                ProtocolAdapter.OPENAI_COMPLETIONS,
                ProtocolAdapter.OPENAI_RESPONSES,
                ProtocolAdapter.CUSTOM_DIRECT,
                ProtocolAdapter.OAUTH_PROXY,
            }:
                return _probe_openai_compatible(client, config)
            if config.protocol_adapter == ProtocolAdapter.ANTHROPIC_MESSAGES:
                return _probe_anthropic_compatible(client, config)
            if config.protocol_adapter == ProtocolAdapter.GOOGLE_GENERATIVE_AI:
                return _probe_gemini(client, config)
            if config.protocol_adapter == ProtocolAdapter.DASHSCOPE_EMBEDDING:
                return _probe_dashscope_embedding(client, config)
            if config.protocol_adapter == ProtocolAdapter.DASHSCOPE_RERANK:
                return _probe_dashscope_rerank(client, config)
            return _probe_ollama(client, config)
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
            if config.protocol_adapter in {
                ProtocolAdapter.OPENAI_COMPLETIONS,
                ProtocolAdapter.OPENAI_RESPONSES,
                ProtocolAdapter.CUSTOM_DIRECT,
                ProtocolAdapter.OAUTH_PROXY,
            }:
                return _probe_openai_minimal_inference(client, config)
            return _probe_single_config(config, transport=transport)
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
