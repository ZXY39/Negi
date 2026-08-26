from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from g3ku.config.schema import Config
from g3ku.llm_config.enums import ProtocolAdapter
from g3ku.llm_config.runtime_resolver import resolve_chat_target
from g3ku.providers.base import LLMProvider
from g3ku.providers.openai_chat_provider import OpenAIChatProvider
from g3ku.providers.responses_provider import ResponsesProvider
from g3ku.utils.api_keys import (
    APIKeyConfigurationError,
    SingleAPIKeyMaxConcurrency,
    parse_api_keys,
    resolve_api_key_concurrency_layout,
)
from g3ku.utils.retry_keywords import DEFAULT_RETRY_ON_KEYWORDS

_CHAT_PROTOCOL_ADAPTERS = {
    ProtocolAdapter.OPENAI_COMPLETIONS.value,
}


@dataclass(slots=True)
class ProviderTarget:
    provider_ref: str
    provider_id: str
    model_id: str
    provider: LLMProvider
    model_parameters: dict[str, Any] = field(default_factory=dict)
    max_tokens_limit: int | None = None
    default_temperature: float | None = None
    default_reasoning_effort: str | None = None
    retry_on: list[str] = field(default_factory=lambda: list(DEFAULT_RETRY_ON_KEYWORDS))
    retry_count: int = 0
    api_key_count: int = 0
    api_key_indexes: list[int] | None = None
    single_api_key_max_concurrency: SingleAPIKeyMaxConcurrency = None


def _require_non_empty_api_key(
    *,
    provider_id: str,
    model_key: str,
    config_id: str,
    api_key: str,
) -> str:
    token = str(api_key or '').strip()
    if token:
        return token
    raise ValueError(
        "Missing API key for managed model binding.\n"
        f"Model key: {model_key}\n"
        f"Provider: {provider_id}\n"
        f"LLM config id: {config_id or '<unknown>'}\n"
        "Fix: set a non-empty API key for this model binding, or switch the role to a provider that does not require Authorization."
    )


def _protocol_adapter_value(target) -> str:
    raw = getattr(target, 'protocol_adapter', None)
    if hasattr(raw, 'value'):
        raw = raw.value
    return str(raw or '').strip().lower()


def build_provider_from_model_key(
    config: Config,
    model_key: str,
    *,
    api_key_index: int | None = None,
) -> ProviderTarget:
    provider_ref = str(model_key or '').strip()
    target = resolve_chat_target(config, provider_ref)
    provider_id = target.provider_id
    model_id = target.resolved_model
    raw_api_key = str(target.secret_payload.get('api_key', '') or '')
    api_keys = parse_api_keys(raw_api_key)
    try:
        key_layout = resolve_api_key_concurrency_layout(
            raw_api_key,
            getattr(target, 'single_api_key_max_concurrency', None),
            include_empty_slot=not api_keys,
            reject_all_zero=bool(api_keys),
        )
    except ValueError as exc:
        raise APIKeyConfigurationError(
            f"Invalid API key concurrency config for model {provider_ref}: {exc}"
        ) from exc
    if api_keys and not key_layout.key_indexes:
        raise APIKeyConfigurationError(f"All configured API keys are disabled for model {provider_ref}")

    selected_index = key_layout.key_indexes[0] if key_layout.key_indexes else 0
    if api_key_index is not None:
        requested_index = max(0, int(api_key_index))
        if key_layout.key_indexes and requested_index not in key_layout.key_indexes:
            raise APIKeyConfigurationError(
                f"API key index {requested_index} is disabled for model {provider_ref}"
            )
        selected_index = requested_index
    if api_keys:
        selected_index = max(0, min(selected_index, len(api_keys) - 1))
    api_key = api_keys[selected_index] if api_keys else raw_api_key.strip()
    api_base = target.base_url
    managed = config.get_model_runtime_profile(provider_ref)
    if managed is not None and not managed.enabled:
        raise ValueError(f'Managed model {provider_ref} is disabled')
    model_parameters = dict(getattr(target, 'model_parameters', {}) or {})
    max_tokens_limit = target.max_tokens_limit
    default_temperature = target.default_temperature
    default_reasoning_effort = target.default_reasoning_effort
    retry_on = list(managed.retry_on or []) if managed is not None else list(DEFAULT_RETRY_ON_KEYWORDS)
    retry_count = int(getattr(managed, 'retry_count', 0) or 0) if managed is not None else 0
    api_key_count = len(api_keys)
    api_key_indexes = list(key_layout.key_indexes or ([0] if not api_keys else []))
    extra_headers = dict(getattr(target, 'headers', {}) or {})

    adapter = _protocol_adapter_value(target)
    if adapter == ProtocolAdapter.OPENAI_RESPONSES.value:
        provider = ResponsesProvider(
            api_key=_require_non_empty_api_key(
                provider_id=provider_id,
                model_key=provider_ref,
                config_id=str(getattr(target, 'config_id', '') or ''),
                api_key=api_key,
            ),
            api_base=api_base or '',
            default_model=model_id,
            extra_headers=extra_headers,
        )
    elif adapter in _CHAT_PROTOCOL_ADAPTERS:
        provider = OpenAIChatProvider(
            api_key=api_key or 'no-key',
            api_base=api_base or 'http://localhost:8000/v1',
            default_model=model_id,
            extra_headers=extra_headers,
        )
    else:
        raise ValueError(
            "Unsupported protocol adapter for managed model binding.\n"
            f"Model key: {provider_ref}\n"
            f"Protocol adapter: {adapter or '<empty>'}\n"
            "Supported protocol adapters: openai-completions (Chat Completions), "
            "openai-responses (Responses).\n"
            "Fix: rebind this model key to the `openai` or `responses` provider "
            "with a matching protocol adapter."
        )
    return ProviderTarget(
        provider_ref=provider_ref,
        provider_id=provider_id,
        model_id=model_id,
        provider=provider,
        model_parameters=model_parameters,
        max_tokens_limit=max_tokens_limit,
        default_temperature=default_temperature,
        default_reasoning_effort=default_reasoning_effort,
        retry_on=retry_on,
        retry_count=retry_count,
        api_key_count=api_key_count,
        api_key_indexes=api_key_indexes,
        single_api_key_max_concurrency=getattr(target, 'single_api_key_max_concurrency', None),
    )
