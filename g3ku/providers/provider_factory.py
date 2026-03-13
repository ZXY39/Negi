from __future__ import annotations

from dataclasses import dataclass, field

from g3ku.config.schema import Config
from g3ku.providers.base import LLMProvider
from g3ku.providers.custom_provider import CustomProvider
from g3ku.providers.litellm_provider import LiteLLMProvider
from g3ku.providers.openai_codex_provider import OpenAICodexProvider
from g3ku.providers.registry import find_by_name
from g3ku.providers.responses_provider import ResponsesProvider


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


def _resolve_litellm_model(provider_id: str, model_id: str) -> str:
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


def _assert_provider_ready(config: Config, provider_id: str, model_key: str) -> None:
    provider_cfg = config.get_provider(model_key)
    api_key = (provider_cfg.api_key if provider_cfg else '') or ''
    api_base = config.get_api_base(model_key)
    spec = find_by_name(provider_id)
    if provider_id in {'custom', 'openai_codex'}:
        return
    if spec and spec.is_oauth:
        return
    if spec and spec.is_local:
        if not api_base:
            raise ValueError(f'Local provider {provider_id} requires api_base')
        return
    if not api_key:
        raise ValueError(f'Provider {provider_id} is not configured with an API key')


def build_provider_from_model_key(config: Config, model_key: str) -> ProviderTarget:
    provider_ref = str(model_key or '').strip()
    provider_id, model_id = config.get_model_target(provider_ref)
    _assert_provider_ready(config, provider_id, provider_ref)
    provider_cfg = config.get_provider(provider_ref)
    api_key = (provider_cfg.api_key if provider_cfg else '') or ''
    api_base = config.get_api_base(provider_ref)
    managed = config.get_model_runtime_profile(provider_ref)
    if managed is not None and not managed.enabled:
        raise ValueError(f'Managed model {provider_ref} is disabled')
    max_tokens_limit = int(managed.max_tokens) if managed is not None else None
    default_temperature = float(managed.temperature) if managed is not None else None
    default_reasoning_effort = str(managed.reasoning_effort) if managed is not None and managed.reasoning_effort is not None else None
    retry_on = list(managed.retry_on or []) if managed is not None else ['network', '429', '5xx']

    if provider_id == 'custom':
        provider = CustomProvider(api_key=api_key or 'no-key', api_base=api_base or 'http://localhost:8000/v1', default_model=model_id)
        return ProviderTarget(provider_ref=provider_ref, provider_id=provider_id, model_id=model_id, provider=provider, max_tokens_limit=max_tokens_limit, default_temperature=default_temperature, default_reasoning_effort=default_reasoning_effort, retry_on=retry_on)

    if provider_id == 'responses':
        provider = ResponsesProvider(api_key=api_key, api_base=api_base or '', default_model=model_id)
        return ProviderTarget(provider_ref=provider_ref, provider_id=provider_id, model_id=model_id, provider=provider, max_tokens_limit=max_tokens_limit, default_temperature=default_temperature, default_reasoning_effort=default_reasoning_effort, retry_on=retry_on)

    if provider_id == 'openai_codex':
        provider = OpenAICodexProvider(default_model=f'openai_codex/{model_id}')
        return ProviderTarget(provider_ref=provider_ref, provider_id=provider_id, model_id=model_id, provider=provider, max_tokens_limit=max_tokens_limit, default_temperature=default_temperature, default_reasoning_effort=default_reasoning_effort, retry_on=retry_on)

    resolved_model = _resolve_litellm_model(provider_id, model_id)
    provider = LiteLLMProvider(
        api_key=api_key or None,
        api_base=api_base,
        default_model=resolved_model,
        extra_headers=(provider_cfg.extra_headers if provider_cfg else None),
        provider_name=provider_id,
    )
    return ProviderTarget(provider_ref=provider_ref, provider_id=provider_id, model_id=resolved_model, provider=provider, max_tokens_limit=max_tokens_limit, default_temperature=default_temperature, default_reasoning_effort=default_reasoning_effort, retry_on=retry_on)
