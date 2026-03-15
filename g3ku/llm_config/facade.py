from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from g3ku.llm_config.models import (
    GenericRuntimeConfig,
    MemoryModelBinding,
    ModelBindingDraft,
    NormalizedProviderConfig,
    ProviderConfigDraft,
    RuntimeTarget,
)
from g3ku.llm_config.repositories import EncryptedConfigRepository
from g3ku.llm_config.secret_store import EncryptedFileSecretStore
from g3ku.llm_config.service import ConfigService, TemplateService


MASKED_SECRET_VALUE = "********"


def _store_root(workspace: Path | None = None) -> Path:
    root = (workspace or Path.cwd()).resolve()
    return root / ".g3ku" / "llm-config"


def _resolve_master_key(storage_root: Path) -> str:
    import os

    env_key = os.getenv("G3KU_LLM_MASTER_KEY", "").strip()
    if env_key:
        return env_key
    key_path = storage_root / "master.key"
    if key_path.exists():
        return key_path.read_text(encoding="utf-8").strip()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = EncryptedFileSecretStore.generate_key()
    key_path.write_text(key, encoding="utf-8")
    return key


class LLMConfigFacade:
    def __init__(self, workspace: Path | None = None):
        self.workspace = (workspace or Path.cwd()).resolve()
        storage_root = _store_root(self.workspace)
        secret_store = EncryptedFileSecretStore(_resolve_master_key(storage_root))
        self.repository = EncryptedConfigRepository(storage_root, secret_store)
        self.template_service = TemplateService()
        self.config_service = ConfigService(self.repository, template_service=self.template_service)

    def list_templates(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.template_service.list_templates()]

    def get_template(self, provider_id: str) -> dict[str, Any]:
        return self.template_service.get_form_spec(provider_id)

    def validate_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        draft = ProviderConfigDraft.model_validate(payload)
        return self.config_service.validate_draft(draft).model_dump(mode="json")

    def probe_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        draft = ProviderConfigDraft.model_validate(payload)
        return self.config_service.probe_draft(draft).model_dump(mode="json")

    def list_config_records(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.repository.list_summaries()]

    def get_config_record(self, config_id: str, *, include_secrets: bool = False) -> dict[str, Any]:
        return self.config_service.get_config(config_id, include_secrets=include_secrets).model_dump(mode="json")

    def create_config_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        draft = ProviderConfigDraft.model_validate(payload)
        return self.config_service.save_draft(draft).model_dump(mode="json")

    def update_config_record(self, config_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.repository.get(config_id)
        merged = self._merge_draft(current, payload)
        validation = self.config_service.validate_draft(merged)
        if not validation.valid or validation.normalized_preview is None:
            raise ValueError("Draft validation failed")
        probe = self.config_service.probe_draft(merged)
        if not probe.success:
            raise ValueError(probe.message)
        updated = validation.normalized_preview.model_copy(
            update={
                "config_id": current.config_id,
                "created_at": current.created_at,
                "updated_at": datetime.now(UTC),
            }
        )
        self.repository.save(updated, last_probe_status=probe.status.value)
        return updated.model_dump(mode="json")

    def delete_config_record(self, config_id: str) -> None:
        self.repository.delete(config_id)

    def list_bindings(self, config: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for binding in list(getattr(config.models, "catalog", []) or []):
            config_id = str(getattr(binding, "llm_config_id", "") or "").strip()
            if not config_id:
                continue
            record = self.repository.get(config_id)
            items.append(self._binding_payload(binding, record))
        return items

    def get_binding(self, config: Any, model_key: str) -> dict[str, Any]:
        binding = config.get_managed_model(model_key)
        if binding is None or not str(getattr(binding, "llm_config_id", "") or "").strip():
            raise ValueError(f"Unknown model key: {model_key}")
        record = self.repository.get(binding.llm_config_id)
        return self._binding_payload(binding, record)

    def create_binding(self, config: Any, *, draft_payload: dict[str, Any], binding_payload: dict[str, Any]) -> dict[str, Any]:
        binding = ModelBindingDraft.model_validate(binding_payload)
        from g3ku.config.schema import ManagedModelConfig

        config_id = str(binding_payload.get("config_id") or "").strip()
        if config_id:
            self.repository.get(config_id)
        else:
            draft = ProviderConfigDraft.model_validate(draft_payload)
            summary = self.config_service.save_draft(draft)
            config_id = summary.config_id

        config.models.catalog.append(
            ManagedModelConfig(
                key=binding.key,
                llm_config_id=config_id,
                enabled=binding.enabled,
                description=binding.description,
                retry_on=binding.retry_on,
            )
        )
        return self.get_binding(config, binding.key)

    def update_binding(self, config: Any, *, model_key: str, draft_payload: dict[str, Any]) -> dict[str, Any]:
        binding = config.get_managed_model(model_key)
        if binding is None or not str(getattr(binding, "llm_config_id", "") or "").strip():
            raise ValueError(f"Unknown model key: {model_key}")
        next_config_id = str(draft_payload.get("config_id") or draft_payload.get("llm_config_id") or "").strip()
        if next_config_id and next_config_id != binding.llm_config_id:
            self.repository.get(next_config_id)
            binding.llm_config_id = next_config_id
        if "enabled" in draft_payload:
            binding.enabled = bool(draft_payload.get("enabled"))
        if "description" in draft_payload:
            binding.description = str(draft_payload.get("description") or "").strip()
        if "retry_on" in draft_payload and isinstance(draft_payload.get("retry_on"), list):
            binding.retry_on = [str(item).strip() for item in draft_payload.get("retry_on") if str(item).strip()]

        if next_config_id and not any(
            key in draft_payload for key in ("provider_id", "default_model", "base_url", "api_key", "parameters", "extra_headers", "extra_options")
        ):
            return self.get_binding(config, model_key)

        current = self.repository.get(binding.llm_config_id)
        merged = self._merge_draft(current, draft_payload)
        validation = self.config_service.validate_draft(merged)
        if not validation.valid or validation.normalized_preview is None:
            raise ValueError("Draft validation failed")
        probe = self.config_service.probe_draft(merged)
        if not probe.success:
            raise ValueError(probe.message)
        updated = validation.normalized_preview.model_copy(
            update={
                "config_id": current.config_id,
                "created_at": current.created_at,
                "updated_at": datetime.now(UTC),
            }
        )
        self.repository.save(updated, last_probe_status=probe.status.value)
        return self.get_binding(config, model_key)

    def set_binding_enabled(self, config: Any, model_key: str, enabled: bool) -> dict[str, Any]:
        binding = config.get_managed_model(model_key)
        if binding is None:
            raise ValueError(f"Unknown model key: {model_key}")
        binding.enabled = bool(enabled)
        return self.get_binding(config, model_key)

    def delete_binding(self, config: Any, model_key: str) -> None:
        binding = config.get_managed_model(model_key)
        if binding is None:
            raise ValueError(f"Unknown model key: {model_key}")
        config.models.catalog = [item for item in config.models.catalog if item.key != model_key]
        self.repository.delete(binding.llm_config_id)

    def get_routes(self, config: Any) -> dict[str, list[str]]:
        return {
            "ceo": list(config.models.roles.ceo),
            "execution": list(config.models.roles.execution),
            "inspection": list(config.models.roles.inspection),
        }

    def set_route(self, config: Any, scope: str, model_keys: list[str]) -> dict[str, list[str]]:
        normalized = str(scope or "").strip().lower().replace("-", "_")
        setattr(config.models.roles, normalized, [str(item).strip() for item in model_keys if str(item).strip()])
        return self.get_routes(config)

    def get_memory_binding(self, settings: Any) -> MemoryModelBinding:
        return MemoryModelBinding(
            embedding_model_key=getattr(settings.embedding, "model_key", None),
            rerank_model_key=getattr(settings.retrieval, "rerank_model_key", None),
        )

    def set_memory_binding(
        self,
        settings: Any,
        *,
        embedding_model_key: str | None = None,
        rerank_model_key: str | None = None,
    ) -> MemoryModelBinding:
        settings.embedding.model_key = str(embedding_model_key or "").strip() or None
        settings.retrieval.rerank_model_key = str(rerank_model_key or "").strip() or None
        return self.get_memory_binding(settings)

    def export_runtime_config(self, config_id: str) -> GenericRuntimeConfig:
        return self.config_service.export_config(config_id, include_secrets=True)

    def resolve_target(self, config: Any, model_key: str) -> RuntimeTarget:
        binding = config.get_managed_model(model_key)
        if binding is None or not str(getattr(binding, "llm_config_id", "") or "").strip():
            raise ValueError(f"Unknown model key: {model_key}")
        record = self.repository.get(binding.llm_config_id)
        return RuntimeTarget(
            model_key=model_key,
            config_id=record.config_id,
            capability=record.capability,
            provider_id=record.provider_id,
            protocol_adapter=record.protocol_adapter,
            auth_mode=record.auth_mode,
            secret_payload=dict(record.auth),
            base_url=record.base_url,
            resolved_model=record.default_model,
            headers=dict(record.headers),
            max_tokens_limit=int(record.parameters.get("max_tokens")) if record.parameters.get("max_tokens") else None,
            default_temperature=float(record.parameters.get("temperature")) if record.parameters.get("temperature") is not None else None,
            default_reasoning_effort=(
                str(record.parameters.get("reasoning_effort"))
                if record.parameters.get("reasoning_effort") is not None and str(record.parameters.get("reasoning_effort")).strip()
                else None
            ),
            retry_on=list(getattr(binding, "retry_on", []) or []),
            extra_options=dict(record.extra_options),
        )

    def _binding_payload(self, binding: Any, record: NormalizedProviderConfig) -> dict[str, Any]:
        api_key = str(record.auth.get("api_key", "") or "")
        provider_id = record.provider_id
        if provider_id in {"dashscope_embedding", "dashscope_rerank"}:
            provider_id = "dashscope"
        return {
            "key": binding.key,
            "llm_config_id": record.config_id,
            "provider_model": f"{provider_id}:{record.default_model}",
            "api_key": MASKED_SECRET_VALUE if api_key else "",
            "api_base": record.base_url,
            "extra_headers": record.headers,
            "enabled": binding.enabled,
            "max_tokens": record.parameters.get("max_tokens"),
            "temperature": record.parameters.get("temperature"),
            "reasoning_effort": record.parameters.get("reasoning_effort"),
            "retry_on": list(binding.retry_on or []),
            "description": binding.description,
            "capability": record.capability.value,
            "auth_mode": record.auth_mode.value,
            "config_id": record.config_id,
        }

    def _merge_draft(self, current: NormalizedProviderConfig, patch: dict[str, Any]) -> ProviderConfigDraft:
        api_key = patch.get("api_key")
        if api_key == MASKED_SECRET_VALUE:
            api_key = current.auth.get("api_key", "")
        elif api_key is None:
            api_key = current.auth.get("api_key", "")
        return ProviderConfigDraft(
            provider_id=str(patch.get("provider_id") or current.provider_id),
            capability=patch.get("capability", current.capability),
            auth_mode=patch.get("auth_mode", current.auth_mode),
            display_name=patch.get("display_name", current.display_name),
            api_key=str(api_key or ""),
            base_url=str(patch.get("base_url") or current.base_url),
            default_model=str(patch.get("default_model") or current.default_model),
            parameters=dict(current.parameters | dict(patch.get("parameters") or {})),
            extra_headers=dict(patch.get("extra_headers") or current.headers),
            extra_options=dict(current.extra_options | dict(patch.get("extra_options") or {})),
        )
