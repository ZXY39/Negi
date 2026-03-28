from __future__ import annotations

from dataclasses import dataclass, field

from g3ku.config.schema import Config
from g3ku.llm_config.enums import ProtocolAdapter
from g3ku.llm_config.runtime_resolver import resolve_chat_target
from g3ku.providers.base import LLMProvider
from g3ku.providers.custom_provider import CustomProvider
from g3ku.providers.litellm_provider import LiteLLMProvider
from g3ku.providers.openai_codex_provider import OpenAICodexProvider
from g3ku.providers.responses_provider import ResponsesProvider
from g3ku.utils.api_keys import parse_api_keys


@dataclass(slots=True)
class ProviderTarget:
    provider_ref: str
    provider_id: str
    model_id: str
    provider: LLMProvider
    max_tokens_limit: int | None = None
    default_temperature: float | None = None
    default_reasoning_effort: str | None = None
    retry_on: list[str] = field(default_factory=lambda: ['network', '429', '5xx'])
    retry_count: int = 0
    api_key_count: int = 0


def _resolve_litellm_model(provider_id: str, model_id: str) -> str:
    from g3ku.providers.registry import find_by_name

    spec = find_by_name(provider_id)
    if spec is None:
        return model_id
    resolved = model_id.strip()
    if '/' in resolved:
        explicit_prefix, remainder = resolved.split('/', 1)
        if explicit_prefix.lower().replace('-', '_') == provider_id and remainder:
            resolved = remainder
    if spec.strip_model_prefix and '/' in resolved:
        resolved = resolved.split('/')[-1]
    if spec.litellm_prefix and not any(resolved.startswith(prefix) for prefix in spec.skip_prefixes):
        if not resolved.startswith(f'{spec.litellm_prefix}/'):
            resolved = f'{spec.litellm_prefix}/{resolved}'
    return resolved


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


def _uses_openai_responses_protocol(target) -> bool:
    return _protocol_adapter_value(target) == ProtocolAdapter.OPENAI_RESPONSES.value


def _uses_openai_direct_chat_protocol(target) -> bool:
    return _protocol_adapter_value(target) in {
        ProtocolAdapter.OPENAI_COMPLETIONS.value,
        ProtocolAdapter.CUSTOM_DIRECT.value,
    }


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
    selected_index = 0
    if api_keys and api_key_index is not None:
        selected_index = max(0, min(int(api_key_index), len(api_keys) - 1))
    api_key = api_keys[selected_index] if api_keys else raw_api_key.strip()
    api_base = target.base_url
    managed = config.get_model_runtime_profile(provider_ref)
    if managed is not None and not managed.enabled:
        raise ValueError(f'Managed model {provider_ref} is disabled')
    max_tokens_limit = target.max_tokens_limit
    default_temperature = target.default_temperature
    default_reasoning_effort = target.default_reasoning_effort
    retry_on = list(managed.retry_on or []) if managed is not None else ['network', '429', '5xx']
    retry_count = int(getattr(managed, 'retry_count', 0) or 0) if managed is not None else 0
    api_key_count = len(api_keys)
    extra_headers = dict(getattr(target, 'headers', {}) or {})

    if provider_id == 'responses' or _uses_openai_responses_protocol(target):
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
        return ProviderTarget(provider_ref=provider_ref, provider_id=provider_id, model_id=model_id, provider=provider, max_tokens_limit=max_tokens_limit, default_temperature=default_temperature, default_reasoning_effort=default_reasoning_effort, retry_on=retry_on, retry_count=retry_count, api_key_count=api_key_count)

    if provider_id == 'openai_codex':
        provider = OpenAICodexProvider(default_model=f'openai_codex/{model_id}')
        return ProviderTarget(provider_ref=provider_ref, provider_id=provider_id, model_id=model_id, provider=provider, max_tokens_limit=max_tokens_limit, default_temperature=default_temperature, default_reasoning_effort=default_reasoning_effort, retry_on=retry_on, retry_count=retry_count, api_key_count=api_key_count)

    if provider_id == 'custom' or _uses_openai_direct_chat_protocol(target):
        provider = CustomProvider(
            api_key=api_key or 'no-key',
            api_base=api_base or 'http://localhost:8000/v1',
            default_model=model_id,
            extra_headers=extra_headers,
        )
        return ProviderTarget(
            provider_ref=provider_ref,
            provider_id=provider_id,
            model_id=model_id,
            provider=provider,
            max_tokens_limit=max_tokens_limit,
            default_temperature=default_temperature,
            default_reasoning_effort=default_reasoning_effort,
            retry_on=retry_on,
            retry_count=retry_count,
            api_key_count=api_key_count,
        )

    resolved_model = _resolve_litellm_model(provider_id, model_id)
    provider = LiteLLMProvider(
        api_key=api_key or None,
        api_base=api_base,
        default_model=resolved_model,
        extra_headers=extra_headers,
        provider_name=provider_id,
    )
    return ProviderTarget(provider_ref=provider_ref, provider_id=provider_id, model_id=resolved_model, provider=provider, max_tokens_limit=max_tokens_limit, default_temperature=default_temperature, default_reasoning_effort=default_reasoning_effort, retry_on=retry_on, retry_count=retry_count, api_key_count=api_key_count)
