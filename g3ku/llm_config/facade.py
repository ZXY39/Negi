from __future__ import annotations

import json
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
from g3ku.llm_config.export import to_generic_runtime_config
from g3ku.llm_config.repositories import EncryptedConfigRepository
from g3ku.llm_config.secret_store import EncryptedFileSecretStore
from g3ku.llm_config.service import ConfigService, TemplateService
from g3ku.security.bootstrap import get_bootstrap_security_service


MASKED_SECRET_VALUE = "********"
MEMORY_EMBEDDING_CONFIG_ID = "memory_embedding_default"
MEMORY_RERANK_CONFIG_ID = "memory_rerank_default"


def _store_root(workspace: Path | None = None) -> Path:
    root = (workspace or Path.cwd()).resolve()
    return root / ".g3ku" / "llm-config"


def _read_utf8_json_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _resolve_legacy_master_key(storage_root: Path) -> str | None:
    import os

    env_key = os.getenv("G3KU_LLM_MASTER_KEY", "").strip()
    if env_key:
        return env_key
    key_path = storage_root / "master.key"
    if key_path.exists():
        raw = _read_utf8_json_text(key_path).strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return raw
        if isinstance(payload, dict):
            return None
        return raw
    return None


class LLMConfigFacade:
    def __init__(self, workspace: Path | None = None):
        self.workspace = (workspace or Path.cwd()).resolve()
        storage_root = _store_root(self.workspace)
        self.security = get_bootstrap_security_service(self.workspace)
        legacy_key = _resolve_legacy_master_key(storage_root)
        secret_store = EncryptedFileSecretStore(legacy_key) if legacy_key else None
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
        record = self._hydrate_record_secrets(self.repository.get(config_id))
        if not include_secrets:
            auth = dict(record.auth)
            api_key = str(auth.get("api_key", "") or "")
            auth["api_key"] = "***" if api_key else ""
            record = record.model_copy(update={"auth": auth, "headers": {}})
        return record.model_dump(mode="json")

    def create_config_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        draft = ProviderConfigDraft.model_validate(payload)
        validation = self.config_service.validate_draft(draft)
        if not validation.valid or validation.normalized_preview is None:
            raise ValueError("Draft validation failed")
        probe = self.config_service.probe_draft(draft)
        if not probe.success:
            raise ValueError(probe.message)
        now = datetime.now(UTC)
        normalized = validation.normalized_preview.model_copy(
            update={"config_id": uuid4().hex, "created_at": now, "updated_at": now}
        )
        self.repository.save(self._sanitize_record_for_storage(normalized), last_probe_status=probe.status.value)
        self._store_record_secrets(normalized)
        return self.get_config_record(normalized.config_id, include_secrets=True)

    def update_config_record(self, config_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self._hydrate_record_secrets(self.repository.get(config_id))
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
        self.repository.save(self._sanitize_record_for_storage(updated), last_probe_status=probe.status.value)
        self._store_record_secrets(updated)
        return self.get_config_record(updated.config_id, include_secrets=True)

    def delete_config_record(self, config_id: str) -> None:
        self.repository.delete(config_id)
        self.security.delete_overlay_prefix_all_realms(f"llm_config.{config_id}.")

    def list_bindings(self, config: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for binding in list(getattr(config.models, "catalog", []) or []):
            config_id = str(getattr(binding, "llm_config_id", "") or "").strip()
            if not config_id:
                continue
            record = self._hydrate_record_secrets(self.repository.get(config_id))
            items.append(self._binding_payload(binding, record))
        return items

    def get_binding(self, config: Any, model_key: str) -> dict[str, Any]:
        binding = config.get_managed_model(model_key)
        if binding is None or not str(getattr(binding, "llm_config_id", "") or "").strip():
            raise ValueError(f"Unknown model key: {model_key}")
        record = self._hydrate_record_secrets(self.repository.get(binding.llm_config_id))
        return self._binding_payload(binding, record)

    def create_binding(self, config: Any, *, draft_payload: dict[str, Any], binding_payload: dict[str, Any]) -> dict[str, Any]:
        binding = ModelBindingDraft.model_validate(binding_payload)
        from g3ku.config.schema import ManagedModelConfig

        config_id = str(binding_payload.get("config_id") or "").strip()
        if config_id:
            self.repository.get(config_id)
        else:
            draft = ProviderConfigDraft.model_validate(draft_payload)
            item = self.create_config_record(draft.model_dump(mode="python"))
            config_id = str(item.get("config_id") or "").strip()

        config.models.catalog.append(
            ManagedModelConfig(
                key=binding.key,
                llm_config_id=config_id,
                enabled=binding.enabled,
                description=binding.description,
                retry_on=binding.retry_on,
                retry_count=binding.retry_count,
            )
        )
        return self.get_binding(config, binding.key)

    def update_binding(self, config: Any, *, model_key: str, draft_payload: dict[str, Any]) -> dict[str, Any]:
        binding = config.get_managed_model(model_key)
        if binding is None or not str(getattr(binding, "llm_config_id", "") or "").strip():
            raise ValueError(f"Unknown model key: {model_key}")
        config_fields = {"provider_id", "default_model", "base_url", "api_key", "parameters", "extra_headers", "extra_options"}
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
        if "retry_count" in draft_payload:
            binding.retry_count = int(draft_payload.get("retry_count") or 0)

        if not any(key in draft_payload for key in config_fields):
            return self.get_binding(config, model_key)

        current = self._hydrate_record_secrets(self.repository.get(binding.llm_config_id))
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
        self.repository.save(self._sanitize_record_for_storage(updated), last_probe_status=probe.status.value)
        self._store_record_secrets(updated)
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

    def get_memory_binding(self) -> MemoryModelBinding:
        binding_ids = self._memory_binding_ids()
        embedding_config_id = binding_ids.get("embedding")
        rerank_config_id = binding_ids.get("rerank")
        embedding = self._get_optional_record(embedding_config_id) if embedding_config_id else None
        rerank = self._get_optional_record(rerank_config_id) if rerank_config_id else None
        return MemoryModelBinding(
            embedding_config_id=embedding_config_id,
            embedding_provider_model=self._provider_model_from_record(embedding),
            rerank_config_id=rerank_config_id,
            rerank_provider_model=self._provider_model_from_record(rerank),
        )

    def set_memory_binding(
        self,
        *,
        embedding_config_id: str | None,
        rerank_config_id: str | None,
    ) -> MemoryModelBinding:
        payload = {
            "embedding_config_id": self._validate_memory_binding_config_id(
                embedding_config_id,
                capability="embedding",
            ),
            "rerank_config_id": self._validate_memory_binding_config_id(
                rerank_config_id,
                capability="rerank",
            ),
        }
        path = self._memory_binding_path()
        current_payload, loaded = self._read_memory_binding_payload()
        if loaded and current_payload == payload:
            return self.get_memory_binding()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return self.get_memory_binding()

    def resolve_memory_target(self, capability: str) -> RuntimeTarget:
        normalized = str(capability or "").strip().lower()
        if normalized == "embedding":
            expected_capability = "embedding"
            config_id = self._memory_binding_ids().get("embedding")
        elif normalized == "rerank":
            expected_capability = "rerank"
            config_id = self._memory_binding_ids().get("rerank")
        else:
            raise ValueError(f"Unsupported memory capability: {capability}")
        if not config_id:
            raise ValueError(f"Memory {normalized} config is not configured")
        record = self._hydrate_record_secrets(self.repository.get(config_id))
        if record.capability.value != expected_capability:
            raise ValueError(
                f"Memory config {config_id} is not configured for {expected_capability} capability"
            )
        return self._runtime_target(
            model_key=config_id,
            record=record,
            retry_on=[],
            retry_count=0,
        )

    def export_runtime_config(self, config_id: str) -> GenericRuntimeConfig:
        return to_generic_runtime_config(self._hydrate_record_secrets(self.repository.get(config_id)), include_secrets=True)

    def resolve_target(self, config: Any, model_key: str) -> RuntimeTarget:
        binding = config.get_managed_model(model_key)
        if binding is None or not str(getattr(binding, "llm_config_id", "") or "").strip():
            raise ValueError(f"Unknown model key: {model_key}")
        record = self._hydrate_record_secrets(self.repository.get(binding.llm_config_id))
        return self._runtime_target(
            model_key=model_key,
            record=record,
            retry_on=list(getattr(binding, "retry_on", []) or []),
            retry_count=int(getattr(binding, "retry_count", 0) or 0),
        )

    def _binding_payload(self, binding: Any, record: NormalizedProviderConfig) -> dict[str, Any]:
        api_key = str(record.auth.get("api_key", "") or "")
        return {
            "key": binding.key,
            "llm_config_id": record.config_id,
            "provider_model": self._provider_model_from_record(record),
            "api_key": MASKED_SECRET_VALUE if api_key else "",
            "api_base": record.base_url,
            "extra_headers": record.headers,
            "enabled": binding.enabled,
            "max_tokens": record.parameters.get("max_tokens"),
            "temperature": record.parameters.get("temperature"),
            "reasoning_effort": record.parameters.get("reasoning_effort"),
            "retry_on": list(binding.retry_on or []),
            "retry_count": int(getattr(binding, "retry_count", 0) or 0),
            "description": binding.description,
            "capability": record.capability.value,
            "auth_mode": record.auth_mode.value,
            "config_id": record.config_id,
        }

    def _get_optional_record(self, config_id: str) -> NormalizedProviderConfig | None:
        try:
            return self.repository.get(config_id)
        except Exception:
            return None

    def _provider_model_from_record(self, record: NormalizedProviderConfig | None) -> str:
        if record is None:
            return ""
        provider_id = record.provider_id
        if provider_id in {"dashscope_embedding", "dashscope_rerank"}:
            provider_id = "dashscope"
        return f"{provider_id}:{record.default_model}"

    @staticmethod
    def _llm_auth_overlay_key(config_id: str) -> str:
        return f"llm_config.{config_id}.auth"

    @staticmethod
    def _llm_headers_overlay_key(config_id: str) -> str:
        return f"llm_config.{config_id}.headers"

    def _sanitize_record_for_storage(self, record: NormalizedProviderConfig) -> NormalizedProviderConfig:
        auth = dict(record.auth)
        auth["api_key"] = ""
        return record.model_copy(update={"auth": auth, "headers": {}})

    def _hydrate_record_secrets(self, record: NormalizedProviderConfig) -> NormalizedProviderConfig:
        auth_value = self.security.get_overlay_value(self._llm_auth_overlay_key(record.config_id))
        headers_value = self.security.get_overlay_value(self._llm_headers_overlay_key(record.config_id))
        next_auth = dict(record.auth)
        if isinstance(auth_value, dict):
            next_auth = dict(auth_value)
        next_headers = dict(record.headers)
        if isinstance(headers_value, dict):
            next_headers = dict(headers_value)
        return record.model_copy(update={"auth": next_auth, "headers": next_headers})

    def _store_record_secrets(self, record: NormalizedProviderConfig) -> None:
        if not self.security.is_unlocked():
            return
        auth_payload = dict(record.auth)
        headers_payload = dict(record.headers)
        updates: dict[str, Any] = {}
        if str(auth_payload.get("api_key", "") or "").strip():
            updates[self._llm_auth_overlay_key(record.config_id)] = auth_payload
        else:
            updates[self._llm_auth_overlay_key(record.config_id)] = None
        if headers_payload:
            updates[self._llm_headers_overlay_key(record.config_id)] = headers_payload
        else:
            updates[self._llm_headers_overlay_key(record.config_id)] = None
        self.security.set_overlay_values(updates)

    def _memory_binding_path(self) -> Path:
        return _store_root(self.workspace) / "memory_binding.json"

    def _read_memory_binding_payload(self) -> tuple[dict[str, str | None], bool]:
        path = self._memory_binding_path()
        if not path.exists():
            return {}, False
        try:
            raw = json.loads(_read_utf8_json_text(path))
        except Exception:
            return {}, True
        if not isinstance(raw, dict):
            return {}, True
        return {
            "embedding_config_id": self._normalize_memory_binding_value(raw.get("embedding_config_id")),
            "rerank_config_id": self._normalize_memory_binding_value(raw.get("rerank_config_id")),
        }, True

    @staticmethod
    def _normalize_memory_binding_value(value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    def _memory_binding_ids(self) -> dict[str, str | None]:
        payload, loaded = self._read_memory_binding_payload()
        if not loaded:
            return {"embedding": None, "rerank": None}
        return {
            "embedding": payload.get("embedding_config_id"),
            "rerank": payload.get("rerank_config_id"),
        }

    def _validate_memory_binding_config_id(self, config_id: str | None, *, capability: str) -> str | None:
        normalized = str(config_id or "").strip()
        if not normalized:
            return None
        record = self.repository.get(normalized)
        if record.capability.value != capability:
            raise ValueError(f"Config {normalized} is not configured for {capability} capability")
        return normalized

    def _runtime_target(
        self,
        *,
        model_key: str,
        record: NormalizedProviderConfig,
        retry_on: list[str],
        retry_count: int,
    ) -> RuntimeTarget:
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
            retry_on=list(retry_on),
            retry_count=max(0, int(retry_count or 0)),
            extra_options=dict(record.extra_options),
        )

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
