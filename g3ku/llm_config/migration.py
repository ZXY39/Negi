from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import yaml

from .enums import AuthMode, Capability
from .models import NormalizedProviderConfig

if TYPE_CHECKING:
    from .facade import LLMConfigFacade


EMBEDDING_KEY = "memory_embedding_default"
RERANK_KEY = "memory_rerank_default"
_MEMORY_KEYS = {EMBEDDING_KEY, RERANK_KEY}


def _infer_auth_mode(provider_id: str) -> AuthMode:
    if provider_id in {"openai_codex", "github_copilot"}:
        return AuthMode.OAUTH_CACHE
    return AuthMode.API_KEY


def _infer_capability(provider_id: str, model_id: str) -> Capability:
    if provider_id == "dashscope" and model_id == "qwen3-vl-embedding":
        return Capability.EMBEDDING
    if provider_id == "dashscope" and model_id == "qwen3-vl-rerank":
        return Capability.RERANK
    return Capability.CHAT


def _normalized_provider_id(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _memory_provider_template_id(provider_id: str, capability: Capability, model_id: str) -> str:
    if capability == Capability.EMBEDDING and provider_id == "dashscope" and model_id == "qwen3-vl-embedding":
        return "dashscope_embedding"
    if capability == Capability.RERANK and provider_id == "dashscope" and model_id == "qwen3-vl-rerank":
        return "dashscope_rerank"
    return provider_id


def _record_provider_id(provider_id: str, capability: Capability) -> str:
    if capability == Capability.EMBEDDING and provider_id == "dashscope":
        return "dashscope_embedding"
    if capability == Capability.RERANK and provider_id == "dashscope":
        return "dashscope_rerank"
    return provider_id


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
    capability: Capability | None = None,
    config_id: str | None = None,
) -> str:
    normalized_provider_id = _normalized_provider_id(provider_id)
    capability = capability or _infer_capability(normalized_provider_id, model_id)
    template = facade.template_service.get_template(
        _memory_provider_template_id(normalized_provider_id, capability, model_id)
    )
    auth_mode = _infer_auth_mode(normalized_provider_id)
    now = datetime.now(UTC)
    existing = facade._get_optional_record(str(config_id or "").strip()) if config_id else None
    record = NormalizedProviderConfig(
        config_id=str(config_id or uuid4().hex).strip(),
        provider_id=_record_provider_id(normalized_provider_id, capability),
        display_name=template.display_name,
        protocol_adapter=template.protocol_adapter,
        capability=capability,
        auth_mode=auth_mode,
        base_url=str(api_base or template.default_base_url).rstrip("/"),
        default_model=model_id,
        auth={"type": auth_mode.value, "api_key": str(api_key or "").strip()},
        parameters={
            "timeout_s": 8,
            "temperature": 0.1 if temperature is None else temperature,
            "max_tokens": 4096 if max_tokens is None else max_tokens,
            **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
        },
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


def _catalog_provider_model(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("providerModel", item.get("provider_model", "")) or "").strip()


def _provider_payload(legacy_providers: dict[str, Any], provider_id: str) -> tuple[str, str | None, dict[str, str] | None]:
    payload = legacy_providers.get(_normalized_provider_id(provider_id), {})
    if not isinstance(payload, dict):
        payload = {}
    api_key = str(payload.get("apiKey", payload.get("api_key", "")) or "").strip()
    api_base = str(payload.get("apiBase", payload.get("api_base", "")) or "").strip() or None
    extra_headers = payload.get("extraHeaders", payload.get("extra_headers"))
    if not isinstance(extra_headers, dict):
        extra_headers = None
    return api_key, api_base, extra_headers


def _resolve_memory_binding_config_id(
    facade: LLMConfigFacade,
    legacy_providers: dict[str, Any],
    *,
    capability: Capability,
    provider_model: str = "",
    source_config_id: str | None = None,
    legacy_fixed_config_id: str | None = None,
) -> str | None:
    if source_config_id:
        source = facade._get_optional_record(source_config_id)
        if source is not None and source.capability == capability:
            return source_config_id

    normalized_provider_model = str(provider_model or "").strip()
    if normalized_provider_model and ":" in normalized_provider_model:
        provider_id, model_id = normalized_provider_model.split(":", 1)
        api_key, api_base, extra_headers = _provider_payload(legacy_providers, provider_id)
        return _build_record(
            facade,
            provider_id=provider_id,
            model_id=model_id.strip(),
            api_key=api_key,
            api_base=api_base,
            extra_headers=extra_headers,
            capability=capability,
        )

    if legacy_fixed_config_id and facade._get_optional_record(legacy_fixed_config_id) is not None:
        record = facade._get_optional_record(legacy_fixed_config_id)
        if record is not None and record.capability == capability:
            return legacy_fixed_config_id

    return None


def _first_catalog_item(catalog: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for item in catalog:
        if isinstance(item, dict) and str(item.get("key") or "").strip() == key:
            return item
    return None


def _strip_legacy_memory_fields(section: dict[str, Any], field_names: tuple[str, ...]) -> bool:
    changed = False
    for field_name in field_names:
        if field_name in section:
            section.pop(field_name, None)
            changed = True
    return changed


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
    if not catalog:
        return raw_data, False

    workspace = (workspace or Path.cwd()).resolve()
    from .facade import LLMConfigFacade

    facade = LLMConfigFacade(workspace)
    next_data = deepcopy(raw_data)
    next_catalog: list[dict[str, Any]] = []
    changed = False
    reserved_catalog: dict[str, dict[str, Any]] = {}
    orphaned_memory_record_ids: set[str] = set()

    for item in catalog:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if key in _MEMORY_KEYS:
            reserved_catalog[key] = deepcopy(item)
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
        )
        next_catalog.append(
            {
                "key": key,
                "llmConfigId": config_id,
                "enabled": bool(item.get("enabled", True)),
                "description": str(item.get("description") or "").strip(),
                "retryOn": list(
                    item.get("retryOn", item.get("retry_on", ["network", "429", "5xx"])) or ["network", "429", "5xx"]
                ),
            }
        )
        changed = True

    next_data.setdefault("models", {})["catalog"] = next_catalog

    providers = next_data.get("providers") if isinstance(next_data.get("providers"), dict) else {}
    referenced_provider_ids = {
        _normalized_provider_id(str(item.get("providerModel", item.get("provider_model", "")) or "").split(":", 1)[0])
        for item in catalog
        if isinstance(item, dict) and ":" in str(item.get("providerModel", item.get("provider_model", "")) or "")
    }
    referenced_provider_ids.discard("")
    legacy_providers = deepcopy(providers)

    memory_manifest = workspace / "tools" / "memory_runtime" / "resource.yaml"
    embedding_provider_model = _catalog_provider_model(reserved_catalog.get(EMBEDDING_KEY))
    rerank_provider_model = _catalog_provider_model(reserved_catalog.get(RERANK_KEY))
    embedding_source_config_id = _catalog_config_id(reserved_catalog.get(EMBEDDING_KEY))
    rerank_source_config_id = _catalog_config_id(reserved_catalog.get(RERANK_KEY))

    if memory_manifest.exists():
        try:
            data = yaml.safe_load(memory_manifest.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        settings = data.get("settings") if isinstance(data, dict) else None
        manifest_changed = False
        if isinstance(settings, dict):
            embedding = settings.get("embedding") if isinstance(settings.get("embedding"), dict) else None
            retrieval = settings.get("retrieval") if isinstance(settings.get("retrieval"), dict) else None
            if isinstance(embedding, dict):
                manifest_provider_model = str(embedding.get("provider_model") or embedding.get("providerModel") or "").strip()
                manifest_model_key = str(embedding.get("model_key") or "").strip()
                embedding_provider_model = manifest_provider_model or embedding_provider_model
                if manifest_model_key and manifest_model_key not in _MEMORY_KEYS:
                    embedding_source_config_id = _catalog_config_id(_first_catalog_item(catalog, manifest_model_key))
                manifest_changed = _strip_legacy_memory_fields(
                    embedding,
                    ("provider_model", "providerModel", "model_key"),
                ) or manifest_changed
            if isinstance(retrieval, dict):
                manifest_provider_model = str(
                    retrieval.get("rerank_provider_model") or retrieval.get("rerankProviderModel") or ""
                ).strip()
                manifest_model_key = str(retrieval.get("rerank_model_key") or "").strip()
                rerank_provider_model = manifest_provider_model or rerank_provider_model
                if manifest_model_key and manifest_model_key not in _MEMORY_KEYS:
                    rerank_source_config_id = _catalog_config_id(_first_catalog_item(catalog, manifest_model_key))
                manifest_changed = _strip_legacy_memory_fields(
                    retrieval,
                    ("rerank_provider_model", "rerankProviderModel", "rerank_model_key"),
                ) or manifest_changed
        if manifest_changed:
            memory_manifest.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            changed = True

    embedding_binding_config_id = _resolve_memory_binding_config_id(
        facade,
        legacy_providers,
        capability=Capability.EMBEDDING,
        provider_model=embedding_provider_model,
        source_config_id=embedding_source_config_id,
        legacy_fixed_config_id=EMBEDDING_KEY,
    )
    rerank_binding_config_id = _resolve_memory_binding_config_id(
        facade,
        legacy_providers,
        capability=Capability.RERANK,
        provider_model=rerank_provider_model,
        source_config_id=rerank_source_config_id,
        legacy_fixed_config_id=RERANK_KEY,
    )
    current_binding = facade.get_memory_binding()
    desired_embedding_binding = embedding_binding_config_id
    desired_rerank_binding = rerank_binding_config_id
    if desired_embedding_binding is None and current_binding.embedding_config_id:
        desired_embedding_binding = current_binding.embedding_config_id
    if desired_rerank_binding is None and current_binding.rerank_config_id:
        desired_rerank_binding = current_binding.rerank_config_id
    if (
        desired_embedding_binding != current_binding.embedding_config_id
        or desired_rerank_binding != current_binding.rerank_config_id
    ):
        facade.set_memory_binding(
            embedding_config_id=desired_embedding_binding,
            rerank_config_id=desired_rerank_binding,
        )
        changed = True

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
    retained_config_ids.update(
        {
            config_id
                for config_id in (
                    desired_embedding_binding,
                    desired_rerank_binding,
                    EMBEDDING_KEY,
                    RERANK_KEY,
                )
                if config_id
            }
        )
    _delete_orphaned_memory_records(
        facade,
        orphaned_config_ids=orphaned_memory_record_ids,
        retained_config_ids=retained_config_ids,
    )

    return next_data, changed
