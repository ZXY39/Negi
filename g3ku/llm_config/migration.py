from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from .enums import AuthMode, Capability
from .models import NormalizedProviderConfig
from .facade import LLMConfigFacade


EMBEDDING_KEY = "memory_embedding_default"
RERANK_KEY = "memory_rerank_default"


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


def _build_record(facade: LLMConfigFacade, *, provider_id: str, model_id: str, api_key: str, api_base: str | None, extra_headers: dict[str, str] | None, max_tokens: int | None = None, temperature: float | None = None, reasoning_effort: str | None = None, capability: Capability | None = None) -> str:
    template = facade.template_service.get_template(
        {"dashscope": "dashscope_embedding" if model_id == "qwen3-vl-embedding" else "dashscope_rerank" if model_id == "qwen3-vl-rerank" else provider_id}.get(provider_id, provider_id)
    )
    now = datetime.now(UTC)
    capability = capability or _infer_capability(provider_id, model_id)
    record = NormalizedProviderConfig(
        config_id=uuid4().hex,
        provider_id=provider_id if capability == Capability.CHAT else ("dashscope_embedding" if capability == Capability.EMBEDDING else "dashscope_rerank"),
        display_name=template.display_name,
        protocol_adapter=template.protocol_adapter,
        capability=capability,
        auth_mode=_infer_auth_mode(provider_id),
        base_url=str(api_base or template.default_base_url).rstrip("/"),
        default_model=model_id,
        auth={"type": _infer_auth_mode(provider_id).value, "api_key": api_key},
        parameters={
            "timeout_s": 8,
            "temperature": 0.1 if temperature is None else temperature,
            "max_tokens": 4096 if max_tokens is None else max_tokens,
            **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
        },
        headers=dict(extra_headers or {}),
        extra_options={},
        template_version=template.template_version,
        created_at=now,
        updated_at=now,
    )
    facade.repository.save(record, last_probe_status=None)
    return record.config_id


def migrate_raw_config_if_needed(raw_data: dict[str, Any], *, workspace: Path | None = None) -> tuple[dict[str, Any], bool]:
    models = raw_data.get("models") if isinstance(raw_data.get("models"), dict) else None
    catalog = list((models or {}).get("catalog") or []) if isinstance(models, dict) else []
    if not catalog:
        return raw_data, False
    if all(isinstance(item, dict) and str(item.get("llmConfigId") or item.get("llm_config_id") or "").strip() for item in catalog):
        return raw_data, False

    workspace = (workspace or Path.cwd()).resolve()
    facade = LLMConfigFacade(workspace)
    next_data = deepcopy(raw_data)
    next_catalog: list[dict[str, Any]] = []
    for item in catalog:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        provider_model = str(item.get("providerModel", item.get("provider_model", "")) or "").strip()
        if not key or not provider_model or ":" not in provider_model:
            continue
        provider_id, model_id = provider_model.split(":", 1)
        config_id = _build_record(
            facade,
            provider_id=provider_id.strip().lower().replace("-", "_"),
            model_id=model_id.strip(),
            api_key=str(item.get("apiKey", item.get("api_key", "")) or "").strip(),
            api_base=str(item.get("apiBase", item.get("api_base", "")) or "").strip() or None,
            extra_headers=item.get("extraHeaders", item.get("extra_headers")) if isinstance(item.get("extraHeaders", item.get("extra_headers")), dict) else None,
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
                "retryOn": list(item.get("retryOn", item.get("retry_on", ["network", "429", "5xx"])) or ["network", "429", "5xx"]),
            }
        )

    next_data.setdefault("models", {})["catalog"] = next_catalog

    providers = next_data.get("providers") if isinstance(next_data.get("providers"), dict) else {}
    referenced_provider_ids = {
        str(item.get("providerModel", item.get("provider_model", "")) or "").split(":", 1)[0].strip().lower().replace("-", "_")
        for item in catalog
        if isinstance(item, dict) and ":" in str(item.get("providerModel", item.get("provider_model", "")) or "")
    }
    referenced_provider_ids.discard("")
    for provider_id in referenced_provider_ids:
        providers.setdefault(provider_id, {"apiKey": "", "apiBase": None, "extraHeaders": None})
    legacy_providers = deepcopy(providers)
    for provider_name, payload in list((providers or {}).items()):
        if isinstance(payload, dict):
            payload["apiKey"] = ""
            payload["apiBase"] = None
            payload["extraHeaders"] = None

    memory_manifest = workspace / "tools" / "memory_runtime" / "resource.yaml"
    if memory_manifest.exists():
        try:
            data = yaml.safe_load(memory_manifest.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        settings = data.get("settings") if isinstance(data, dict) else None
        if isinstance(settings, dict):
            embedding = settings.get("embedding") if isinstance(settings.get("embedding"), dict) else None
            retrieval = settings.get("retrieval") if isinstance(settings.get("retrieval"), dict) else None
            if isinstance(embedding, dict):
                provider_model = str(embedding.get("provider_model") or embedding.get("providerModel") or "").strip()
                if provider_model and ":" in provider_model:
                    provider_id, model_id = provider_model.split(":", 1)
                    embedding_config_id = _build_record(
                        facade,
                        provider_id=provider_id.strip().lower().replace("-", "_"),
                        model_id=model_id.strip(),
                        api_key=str((legacy_providers or {}).get("dashscope", {}).get("apiKey", "") or "").strip(),
                        api_base=str((legacy_providers or {}).get("dashscope", {}).get("apiBase", "") or "").strip() or None,
                        extra_headers=None,
                        capability=Capability.EMBEDDING,
                    )
                    next_catalog.append(
                        {
                            "key": EMBEDDING_KEY,
                            "llmConfigId": embedding_config_id,
                            "enabled": True,
                            "description": "Auto-migrated memory embedding model",
                            "retryOn": ["network", "429", "5xx"],
                        }
                    )
                    embedding["model_key"] = EMBEDDING_KEY
                    embedding.pop("provider_model", None)
                    embedding.pop("providerModel", None)
            if isinstance(retrieval, dict):
                provider_model = str(retrieval.get("rerank_provider_model") or retrieval.get("rerankProviderModel") or "").strip()
                if provider_model and ":" in provider_model:
                    provider_id, model_id = provider_model.split(":", 1)
                    rerank_config_id = _build_record(
                        facade,
                        provider_id=provider_id.strip().lower().replace("-", "_"),
                        model_id=model_id.strip(),
                        api_key=str((legacy_providers or {}).get("dashscope", {}).get("apiKey", "") or "").strip(),
                        api_base=str((legacy_providers or {}).get("dashscope", {}).get("apiBase", "") or "").strip() or None,
                        extra_headers=None,
                        capability=Capability.RERANK,
                    )
                    next_catalog.append(
                        {
                            "key": RERANK_KEY,
                            "llmConfigId": rerank_config_id,
                            "enabled": True,
                            "description": "Auto-migrated memory rerank model",
                            "retryOn": ["network", "429", "5xx"],
                        }
                    )
                    retrieval["rerank_model_key"] = RERANK_KEY
                    retrieval.pop("rerank_provider_model", None)
                    retrieval.pop("rerankProviderModel", None)
            memory_manifest.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # Deduplicate appended memory items by key.
    deduped: dict[str, dict[str, Any]] = {}
    for item in next_data["models"]["catalog"]:
        if isinstance(item, dict) and str(item.get("key") or "").strip():
            deduped[str(item["key"]).strip()] = item
    next_data["models"]["catalog"] = list(deduped.values())
    return next_data, True
