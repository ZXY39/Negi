from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .enums import AuthMode, Capability
from .models import NormalizedProviderConfig
from g3ku.utils.retry_keywords import DEFAULT_RETRY_ON_KEYWORDS

if TYPE_CHECKING:
    from .facade import LLMConfigFacade


_MEMORY_KEYS = {"memory_embedding_default", "memory_rerank_default"}
_SUPPORTED_PROVIDER_IDS = {"openai", "responses"}
_LEGACY_MAX_TOKENS_DEFAULT = 4096
_LEGACY_TEMPERATURE_DEFAULTS = {0.1, 0.2}
_LEGACY_TIMEOUT_DEFAULT = 8


def _normalized_provider_id(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _optional_chat_parameters(
    *,
    max_tokens: Any = None,
    temperature: Any = None,
    reasoning_effort: Any = None,
    context_window_tokens: Any = None,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    if max_tokens not in (None, ""):
        parameters["max_tokens"] = int(max_tokens)
    if temperature not in (None, ""):
        parameters["temperature"] = float(temperature)
    if reasoning_effort not in (None, ""):
        parameters["reasoning_effort"] = str(reasoning_effort)
    if context_window_tokens not in (None, ""):
        parameters["context_window_tokens"] = int(context_window_tokens)
    return parameters


def _cleanup_legacy_default_parameters(parameters: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    payload = dict(parameters or {})
    changed = False
    raw_max_tokens = payload.get("max_tokens")
    if raw_max_tokens not in (None, ""):
        try:
            if int(raw_max_tokens) == _LEGACY_MAX_TOKENS_DEFAULT:
                payload.pop("max_tokens", None)
                changed = True
        except Exception:
            pass
    raw_temperature = payload.get("temperature")
    if raw_temperature not in (None, ""):
        try:
            if float(raw_temperature) in _LEGACY_TEMPERATURE_DEFAULTS:
                payload.pop("temperature", None)
                changed = True
        except Exception:
            pass
    raw_timeout = payload.get("timeout_s")
    if raw_timeout not in (None, ""):
        try:
            if int(raw_timeout) == _LEGACY_TIMEOUT_DEFAULT:
                payload.pop("timeout_s", None)
                changed = True
        except Exception:
            pass
    return payload, changed


def _cleanup_repository_legacy_default_parameters(facade: LLMConfigFacade) -> bool:
    changed = False
    for summary in list(facade.repository.list_summaries()):
        try:
            record = facade._hydrate_record_secrets(facade.repository.get(summary.config_id))
        except Exception:
            continue
        cleaned_parameters, record_changed = _cleanup_legacy_default_parameters(record.parameters)
        if not record_changed:
            continue
        updated_record = record.model_copy(
            update={
                "parameters": cleaned_parameters,
                "updated_at": datetime.now(UTC),
            }
        )
        facade.repository.save(updated_record, last_probe_status=summary.last_probe_status)
        facade._store_record_secrets(updated_record)
        changed = True
    return changed


def _delete_unsupported_repository_records(
    facade: LLMConfigFacade,
    *,
    protected_config_ids: set[str],
) -> bool:
    """Drop records for removed providers/capabilities left by older installs.

    Records still referenced by the model catalog are rejected loudly instead of
    being orphaned.
    """
    changed = False
    for summary in list(facade.repository.list_summaries()):
        provider_id = _normalized_provider_id(summary.provider_id)
        if provider_id in _SUPPORTED_PROVIDER_IDS and summary.capability == Capability.CHAT:
            continue
        if summary.config_id in protected_config_ids:
            raise ValueError(
                "Model binding references a removed provider.\n"
                f"Original record: config_id={summary.config_id!r}, "
                f"provider={summary.provider_id!r}, model={summary.default_model!r}\n"
                "Supported providers: openai (OpenAI Chat Completions), responses (OpenAI Responses).\n"
                "Example fix: rebind this model key to an 'openai' or 'responses' record "
                "pointing at the same endpoint."
            )
        try:
            facade.repository.delete(summary.config_id)
            changed = True
        except Exception:
            continue
    return changed


def _build_record(
    facade: LLMConfigFacade,
    *,
    provider_id: str,
    model_id: str,
    api_key: str,
    api_base: str | None,
    extra_headers: dict[str, str] | None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    context_window_tokens: int | None = None,
    config_id: str | None = None,
) -> str:
    normalized_provider_id = _normalized_provider_id(provider_id)
    if normalized_provider_id not in _SUPPORTED_PROVIDER_IDS:
        raise ValueError(
            "Unsupported provider in legacy model catalog binding.\n"
            f"Original provider_model: '{provider_id}:{model_id}'\n"
            "Supported providers: openai (OpenAI Chat Completions), responses (OpenAI Responses).\n"
            f"Example fix: rebind this catalog entry to 'openai:{model_id}' "
            "and keep the same api_base in the llm-config record."
        )
    template = facade.template_service.get_template(normalized_provider_id)
    now = datetime.now(UTC)
    existing = facade._get_optional_record(str(config_id or "").strip()) if config_id else None
    record = NormalizedProviderConfig(
        config_id=str(config_id or uuid4().hex).strip(),
        provider_id=normalized_provider_id,
        display_name=template.display_name,
        protocol_adapter=template.protocol_adapter,
        capability=Capability.CHAT,
        auth_mode=AuthMode.API_KEY,
        base_url=str(api_base or template.default_base_url).rstrip("/"),
        default_model=model_id,
        auth={"type": AuthMode.API_KEY.value, "api_key": str(api_key or "").strip()},
        parameters=_optional_chat_parameters(
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            context_window_tokens=context_window_tokens,
        ),
        headers=dict(extra_headers or {}),
        extra_options={},
        template_version=template.template_version,
        created_at=existing.created_at if existing is not None else now,
        updated_at=now,
    )
    facade.repository.save(record, last_probe_status=None)
    return record.config_id


def _has_llm_config_id(item: dict[str, Any]) -> bool:
    return bool(str(item.get("llmConfigId") or item.get("llm_config_id") or "").strip())


def _catalog_config_id(item: dict[str, Any] | None) -> str | None:
    if not isinstance(item, dict):
        return None
    config_id = str(item.get("llmConfigId") or item.get("llm_config_id") or "").strip()
    return config_id or None


def _delete_orphaned_memory_records(
    facade: LLMConfigFacade,
    *,
    orphaned_config_ids: set[str],
    retained_config_ids: set[str],
) -> None:
    for config_id in orphaned_config_ids:
        if not config_id or config_id in retained_config_ids or config_id in _MEMORY_KEYS:
            continue
        try:
            facade.repository.delete(config_id)
        except Exception:
            continue


def migrate_raw_config_if_needed(raw_data: dict[str, Any], *, workspace: Path | None = None) -> tuple[dict[str, Any], bool]:
    models = raw_data.get("models") if isinstance(raw_data.get("models"), dict) else None
    catalog = list((models or {}).get("catalog") or []) if isinstance(models, dict) else []

    workspace = (workspace or Path.cwd()).resolve()
    from .facade import LLMConfigFacade

    facade = LLMConfigFacade(workspace)
    repository_changed = _cleanup_repository_legacy_default_parameters(facade)
    protected_config_ids = {
        config_id
        for config_id in (_catalog_config_id(item) for item in catalog if isinstance(item, dict))
        if config_id
    }
    repository_changed = _delete_unsupported_repository_records(
        facade,
        protected_config_ids=protected_config_ids,
    ) or repository_changed
    if not catalog:
        return raw_data, repository_changed
    next_data = deepcopy(raw_data)
    next_catalog: list[dict[str, Any]] = []
    changed = False
    orphaned_memory_record_ids: set[str] = set()

    for item in catalog:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if key in _MEMORY_KEYS:
            config_id = _catalog_config_id(item)
            if config_id:
                orphaned_memory_record_ids.add(config_id)
            changed = True
            continue
        if _has_llm_config_id(item):
            next_catalog.append(deepcopy(item))
            continue
        provider_model = str(item.get("providerModel", item.get("provider_model", "")) or "").strip()
        if not key or not provider_model or ":" not in provider_model:
            continue
        provider_id, model_id = provider_model.split(":", 1)
        config_id = _build_record(
            facade,
            provider_id=provider_id,
            model_id=model_id.strip(),
            api_key=str(item.get("apiKey", item.get("api_key", "")) or "").strip(),
            api_base=str(item.get("apiBase", item.get("api_base", "")) or "").strip() or None,
            extra_headers=item.get("extraHeaders", item.get("extra_headers"))
            if isinstance(item.get("extraHeaders", item.get("extra_headers")), dict)
            else None,
            max_tokens=item.get("maxTokens", item.get("max_tokens")),
            temperature=item.get("temperature"),
            reasoning_effort=str(item.get("reasoningEffort", item.get("reasoning_effort", "")) or "").strip() or None,
            context_window_tokens=item.get("contextWindowTokens", item.get("context_window_tokens")),
        )
        next_item = {
            "key": key,
            "llmConfigId": config_id,
            "enabled": bool(item.get("enabled", True)),
            "description": str(item.get("description") or "").strip(),
            "retryOn": list(
                item.get("retryOn", item.get("retry_on", list(DEFAULT_RETRY_ON_KEYWORDS)))
                or list(DEFAULT_RETRY_ON_KEYWORDS)
            ),
            "retryCount": int(item.get("retryCount", item.get("retry_count", 0)) or 0),
        }
        raw_context_window_tokens = item.get("contextWindowTokens", item.get("context_window_tokens"))
        if raw_context_window_tokens not in (None, ""):
            next_item["contextWindowTokens"] = int(raw_context_window_tokens)
        raw_single_api_key_max_concurrency = item.get(
            "singleApiKeyMaxConcurrency",
            item.get("single_api_key_max_concurrency"),
        )
        if raw_single_api_key_max_concurrency is not None:
            next_item["singleApiKeyMaxConcurrency"] = raw_single_api_key_max_concurrency
        next_catalog.append(next_item)
        changed = True

    next_data.setdefault("models", {})["catalog"] = next_catalog

    providers = next_data.get("providers") if isinstance(next_data.get("providers"), dict) else {}
    referenced_provider_ids = {
        _normalized_provider_id(str(item.get("providerModel", item.get("provider_model", "")) or "").split(":", 1)[0])
        for item in catalog
        if isinstance(item, dict) and ":" in str(item.get("providerModel", item.get("provider_model", "")) or "")
    }
    referenced_provider_ids.discard("")
    referenced_provider_ids &= _SUPPORTED_PROVIDER_IDS

    if changed:
        for provider_id in referenced_provider_ids:
            providers.setdefault(provider_id, {"apiKey": "", "apiBase": None, "extraHeaders": None})
        for provider_name, payload in list((providers or {}).items()):
            if isinstance(payload, dict):
                payload["apiKey"] = ""
                payload["apiBase"] = None
                payload["extraHeaders"] = None

    retained_config_ids = {
        str(item.get("llmConfigId") or item.get("llm_config_id") or "").strip()
        for item in next_catalog
        if isinstance(item, dict)
    }
    retained_config_ids.discard("")
    _delete_orphaned_memory_records(
        facade,
        orphaned_config_ids=orphaned_memory_record_ids,
        retained_config_ids=retained_config_ids,
    )

    return next_data, changed or repository_changed
