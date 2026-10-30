from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from g3ku.config.schema import normalize_reasoning_effort, require_non_negative_retry_count
from g3ku.llm_config.export import MASKED_SECRET_VALUE, to_generic_runtime_config
from g3ku.llm_config.models import (
    GenericRuntimeConfig,
    ModelBindingDraft,
    NormalizedProviderConfig,
    ProviderConfigDraft,
    RuntimeTarget,
)
from g3ku.llm_config.repositories import EncryptedConfigRepository
from g3ku.llm_config.secret_store import EncryptedFileSecretStore
from g3ku.llm_config.service import ConfigService, TemplateService
from g3ku.security.bootstrap import get_bootstrap_security_service
from g3ku.utils.api_keys import (
    SingleAPIKeyMaxConcurrency,
    normalize_single_api_key_max_concurrency,
    parse_api_keys,
    resolve_api_key_concurrency_layout,
)
from g3ku.utils.retry_keywords import DEFAULT_RETRY_ON_KEYWORDS, split_retry_keywords


def _runtime_model_parameters(parameters: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(parameters or {})
    result: dict[str, Any] = {}
    raw_max_tokens = payload.get("max_tokens")
    if raw_max_tokens not in (None, ""):
        try:
            result["max_tokens"] = max(1, int(raw_max_tokens))
        except Exception:
            pass
    raw_temperature = payload.get("temperature")
    if raw_temperature not in (None, ""):
        try:
            result["temperature"] = float(raw_temperature)
        except Exception:
            pass
    raw_reasoning = str(payload.get("reasoning_effort") or "").strip()
    if raw_reasoning:
        result["reasoning_effort"] = normalize_reasoning_effort(raw_reasoning)
    raw_context_window_tokens = payload.get("context_window_tokens")
    if raw_context_window_tokens not in (None, ""):
        try:
            resolved = int(raw_context_window_tokens)
        except Exception:
            resolved = 0
        if resolved > 0:
            result["context_window_tokens"] = resolved
    raw_request_timeout_seconds = payload.get("request_timeout_seconds")
    if raw_request_timeout_seconds not in (None, ""):
        try:
            resolved_timeout = float(raw_request_timeout_seconds)
        except Exception:
            resolved_timeout = 0.0
        if resolved_timeout > 0:
            result["request_timeout_seconds"] = resolved_timeout
    return result


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


_FACADES: dict[str, "LLMConfigFacade"] = {}
_FACADES_LOCK = threading.Lock()


def get_llm_config_facade(workspace: Path | None = None) -> "LLMConfigFacade":
    """按 workspace 复用同一个 facade（与 get_bootstrap_security_service 同形）。

    构造 facade 会连带构造配置仓储，而仓储 __init__ 无条件做 records 目录
    `mkdir(parents=True, exist_ok=True)` 与 index 存在性检查。模型路由解析每拍都要
    为每条绑定新建一个，实测零流量空转窗口里这条路径占主线程样本 43.4%、`mkdir`
    叶子 23.2%。锁内构造：仓储不持外部资源，且首次之后的调用只查字典。字典键用
    normcase 的原样路径而不是 `Path.resolve()`——后者每次要走 realpath，实测 0.13 ms，
    和缓存本身的收益同量级。
    """
    root = str(os.path.normcase(str(workspace if workspace is not None else Path.cwd())))
    with _FACADES_LOCK:
        facade = _FACADES.get(root)
        if facade is None:
            facade = LLMConfigFacade(Path(workspace) if workspace is not None else Path.cwd())
            _FACADES[root] = facade
        return facade


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

    async def probe_max_concurrency_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        draft = ProviderConfigDraft.model_validate(payload)
        return (await self.config_service.probe_max_concurrency_draft(draft)).model_dump(mode="json")

    async def list_draft_models(self, payload: dict[str, Any]) -> dict[str, Any]:
        draft = ProviderConfigDraft.model_validate(payload)
        return (await self.config_service.list_draft_models(draft)).model_dump(mode="json")

    def list_config_records(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.repository.list_summaries()]

    def get_config_record(self, config_id: str, *, include_secrets: bool = False) -> dict[str, Any]:
        record = self._hydrate_record_secrets(self.repository.get(config_id))
        if not include_secrets:
            auth = dict(record.auth)
            api_key = str(auth.get("api_key", "") or "")
            auth["api_key"] = MASKED_SECRET_VALUE if api_key else ""
            record = record.model_copy(update={"auth": auth, "headers": {}})
        return record.model_dump(mode="json")

    def create_config_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        draft = ProviderConfigDraft.model_validate(payload)
        validation = self.config_service.validate_draft(draft)
        if not validation.valid or validation.normalized_preview is None:
            detail = "; ".join(
                f"{err.field}: {err.message or err.code or 'invalid'}" for err in (validation.errors or [])
            ).strip()
            raise ValueError(f"Draft validation failed: {detail}" if detail else "Draft validation failed")
        now = datetime.now(UTC)
        normalized = validation.normalized_preview.model_copy(
            update={"config_id": uuid4().hex, "created_at": now, "updated_at": now}
        )
        # 保存只做字段校验与落盘，不发真实推理：`last_probe_status=None` 表示这份配置
        # 还没被测过，落一次盘不会把旧的探测结果继续挂在新内容上。
        self._persist_record(normalized)
        return self.get_config_record(normalized.config_id, include_secrets=True)

    def update_config_record(self, config_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self._hydrate_record_secrets(self.repository.get(config_id))
        merged = self._merge_draft(current, payload, replace_parameters=True)
        validation = self.config_service.validate_draft(merged)
        if not validation.valid or validation.normalized_preview is None:
            detail = "; ".join(
                f"{err.field}: {err.message or err.code or 'invalid'}" for err in (validation.errors or [])
            ).strip()
            raise ValueError(f"Draft validation failed: {detail}" if detail else "Draft validation failed")
        updated = validation.normalized_preview.model_copy(
            update={
                "config_id": current.config_id,
                "created_at": current.created_at,
                "updated_at": datetime.now(UTC),
            }
        )
        self._persist_record(updated)
        return self.get_config_record(updated.config_id, include_secrets=True)

    def delete_config_record(self, config_id: str) -> None:
        self.repository.delete(config_id)
        self.security.delete_overlay_prefix_all_realms(f"llm_config.{config_id}.")

    def record_exists(self, config_id: str) -> bool:
        """这一位绑定还有没有凭据记录——只看文件在不在，不解密、不外抛。

        路由解析每拍都要问一次「这个模型用不用得了」，走 `repository.get()` 会连带解密
        整份记录并把 `ConfigNotFoundError` 抛给启动路径。
        """
        return self.repository.has_record(config_id)

    def list_bindings(self, config: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for binding in list(getattr(config.models, "catalog", []) or []):
            row = self._binding_row(binding)
            if row is not None:
                items.append(row)
        return items

    def _binding_row(self, binding: Any) -> dict[str, Any] | None:
        """operator 视角的绑定行：记录丢了也照出一份，并带 `record_missing`。

        模型列表是修复入口——一条坏绑定不能让列表 500，否则既看不到也删不掉它。
        """
        config_id = str(getattr(binding, "llm_config_id", "") or "").strip()
        if not config_id:
            return None
        record = self._get_optional_record(config_id)
        if record is None:
            return self._missing_record_payload(binding)
        return self._binding_payload(binding, self._hydrate_record_secrets(record))

    def get_binding(self, config: Any, model_key: str) -> dict[str, Any]:
        binding = config.get_managed_model(model_key)
        if binding is None or not str(getattr(binding, "llm_config_id", "") or "").strip():
            raise ValueError(f"Unknown model key: {model_key}")
        record = self._hydrate_record_secrets(self.repository.get(binding.llm_config_id))
        return self._binding_payload(binding, record)

    def describe_binding(self, config: Any, model_key: str) -> dict[str, Any]:
        """详情/删除/开关这类 operator 读法：记录没了也返回降级行。"""
        binding = config.get_managed_model(model_key)
        if binding is None:
            raise ValueError(f"Unknown model key: {model_key}")
        row = self._binding_row(binding)
        if row is None:
            raise ValueError(f"Unknown model key: {model_key}")
        return row

    def get_binding_capability(self, config: Any, model_key: str) -> str:
        payload = self.get_binding(config, model_key)
        return str(payload.get("capability") or "").strip().lower()

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
        record = self._hydrate_record_secrets(self.repository.get(config_id))

        binding.key = self._ensure_unique_binding_key(
            config,
            preferred=str(binding.key or "").strip() or str(record.default_model or "").strip(),
        )
        existing = config.get_managed_model(binding.key)
        if existing is not None:
            raise ValueError(f"Model key already exists: {binding.key}")

        binding.name = self._validate_binding_name_unique(config, binding.name)

        binding.single_api_key_max_concurrency = self._validate_binding_api_key_limits(
            api_key=str(record.auth.get("api_key", "") or ""),
            value=binding.single_api_key_max_concurrency,
        )

        config.models.catalog.append(
            ManagedModelConfig(
                key=binding.key,
                llm_config_id=config_id,
                enabled=binding.enabled,
                description=binding.description,
                name=binding.name,
                retry_on=binding.retry_on,
                retry_count=binding.retry_count,
                single_api_key_max_concurrency=binding.single_api_key_max_concurrency,
                context_window_tokens=self._binding_context_window_tokens(record),
                request_timeout_seconds=self._binding_request_timeout_seconds(record),
                image_multimodal_enabled=bool(getattr(binding, "image_multimodal_enabled", False)),
            )
        )
        return self.get_binding(config, binding.key)

    @staticmethod
    def _ensure_unique_binding_key(config: Any, preferred: str) -> str:
        base = str(preferred or "").strip()
        if not base:
            base = uuid4().hex
        key = base
        counter = 2
        while config.get_managed_model(key) is not None:
            key = f"{base}-{counter}"
            counter += 1
        return key

    def update_binding(self, config: Any, *, model_key: str, draft_payload: dict[str, Any]) -> dict[str, Any]:
        binding = config.get_managed_model(model_key)
        if binding is None or not str(getattr(binding, "llm_config_id", "") or "").strip():
            raise ValueError(f"Unknown model key: {model_key}")
        config_fields = {"provider_id", "default_model", "base_url", "api_key", "parameters", "extra_headers", "extra_options"}
        # `binding` 是 config.models.catalog 里的活对象。先算后写：任何一步判否（重名、
        # 并发上限、draft 校验）都必须发生在这份 pending 落到 binding 之前，否则管理端
        # 收到 400 却不重载 config，进程内已经生效而磁盘还是旧值——"没保存成功却已改"。
        pending: dict[str, Any] = {}
        next_config_id = str(draft_payload.get("config_id") or draft_payload.get("llm_config_id") or "").strip()
        effective_config_id = next_config_id or str(binding.llm_config_id or "").strip()
        if next_config_id and next_config_id != binding.llm_config_id:
            self.repository.get(next_config_id)
            pending["llm_config_id"] = next_config_id
        if "enabled" in draft_payload:
            pending["enabled"] = bool(draft_payload.get("enabled"))
        if "description" in draft_payload:
            pending["description"] = str(draft_payload.get("description") or "").strip()
        if "name" in draft_payload:
            pending["name"] = self._validate_binding_name_unique(
                config,
                draft_payload.get("name"),
                exclude_key=model_key,
            )
        if "retry_on" in draft_payload:
            pending["retry_on"] = split_retry_keywords(draft_payload.get("retry_on")) or list(DEFAULT_RETRY_ON_KEYWORDS)
        if "retry_count" in draft_payload:
            # 这里是 setattr 到已有绑定，绕开字段上的 ge=0，所以负数必须在这一步拒掉。
            pending["retry_count"] = require_non_negative_retry_count(
                draft_payload.get("retry_count"),
                field="models.catalog[].retryCount",
            )
        if "single_api_key_max_concurrency" in draft_payload or "singleApiKeyMaxConcurrency" in draft_payload:
            raw_limit = draft_payload.get("single_api_key_max_concurrency")
            if raw_limit is None and "singleApiKeyMaxConcurrency" in draft_payload:
                raw_limit = draft_payload.get("singleApiKeyMaxConcurrency")
            next_limit = normalize_single_api_key_max_concurrency(raw_limit)
        else:
            next_limit = getattr(binding, "single_api_key_max_concurrency", None)
        if "image_multimodal_enabled" in draft_payload or "imageMultimodalEnabled" in draft_payload:
            raw_enabled = draft_payload.get("image_multimodal_enabled")
            if raw_enabled is None and "imageMultimodalEnabled" in draft_payload:
                raw_enabled = draft_payload.get("imageMultimodalEnabled")
            pending["image_multimodal_enabled"] = bool(raw_enabled)
        current = self._hydrate_record_secrets(self.repository.get(effective_config_id))
        merged = self._merge_draft(current, draft_payload, replace_parameters=False)
        pending_limit = self._validate_binding_api_key_limits(
            api_key=str(merged.api_key or ""),
            value=next_limit,
        )
        # 记录没改动时，这两个读数仍按现记录重取一次（绑定面改 enabled/name 也要看到
        # 与磁盘一致的窗口与超时）。
        pending["context_window_tokens"] = self._binding_context_window_tokens(current)
        pending["request_timeout_seconds"] = self._binding_request_timeout_seconds(current)

        touches_record = any(key in draft_payload for key in config_fields)
        if touches_record:
            validation = self.config_service.validate_draft(merged)
            if not validation.valid or validation.normalized_preview is None:
                detail = "; ".join(
                    f"{err.field}: {err.message or err.code or 'invalid'}" for err in (validation.errors or [])
                ).strip()
                raise ValueError(f"Draft validation failed: {detail}" if detail else "Draft validation failed")
            updated = validation.normalized_preview.model_copy(
                update={
                    "config_id": current.config_id,
                    "created_at": current.created_at,
                    "updated_at": datetime.now(UTC),
                }
            )

        pending["single_api_key_max_concurrency"] = pending_limit
        for field, value in pending.items():
            setattr(binding, field, value)
        if not touches_record:
            return self.get_binding(config, model_key)

        self._persist_record(updated)
        binding.context_window_tokens = self._binding_context_window_tokens(updated)
        binding.request_timeout_seconds = self._binding_request_timeout_seconds(updated)
        return self.get_binding(config, model_key)

    def set_binding_enabled(self, config: Any, model_key: str, enabled: bool) -> dict[str, Any]:
        binding = config.get_managed_model(model_key)
        if binding is None:
            raise ValueError(f"Unknown model key: {model_key}")
        binding.enabled = bool(enabled)
        return self.describe_binding(config, model_key)

    def delete_binding(self, config: Any, model_key: str) -> None:
        binding = config.get_managed_model(model_key)
        if binding is None:
            raise ValueError(f"Unknown model key: {model_key}")
        config_id = str(getattr(binding, "llm_config_id", "") or "").strip()
        config.models.catalog = [item for item in config.models.catalog if item.key != model_key]
        # 凭据记录是多对一共享的：兄弟绑定还指着同一个 config_id 时删文件，等于把它们
        # 一起判成 record_missing（解析那一步直接抛）。
        still_referenced = any(
            str(getattr(item, "llm_config_id", "") or "").strip() == config_id
            for item in config.models.catalog
        )
        if not config_id or still_referenced:
            return
        if self.repository.has_record(config_id):
            self.repository.delete(config_id)
        # 与 delete_config_record 同一件事：覆盖层里那把可解密的密钥也要一起摘掉，
        # 否则每删一个模型就留下一条没人认领的凭据。
        self.security.delete_overlay_prefix_all_realms(f"llm_config.{config_id}.")

    def get_routes(self, config: Any) -> dict[str, list[str]]:
        # 候选展开视图：链里出现负载均衡组时，成员也算这条车道用到的模型。
        # 旧的 fallback 顺序语义已由 `routes` 字段（route entries）承载。
        return {
            "ceo": config.get_role_model_keys("ceo"),
            "execution": config.get_role_model_keys("execution"),
            "inspection": config.get_role_model_keys("inspection"),
            "memory": config.get_role_model_keys("memory"),
        }


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
            single_api_key_max_concurrency=getattr(binding, "single_api_key_max_concurrency", None),
        )

    @staticmethod
    def _validate_binding_name_unique(config: Any, raw_name: Any, *, exclude_key: str = "") -> str:
        clean = str(raw_name or "").strip()
        if not clean:
            return ""
        lowered = clean.lower()
        for binding in list(getattr(config.models, "catalog", []) or []):
            if str(getattr(binding, "key", "") or "") == exclude_key:
                continue
            other = str(getattr(binding, "name", "") or "").strip().lower()
            if other == lowered:
                raise ValueError(f"配置名称已存在：{clean}")
        return clean

    def _binding_payload(self, binding: Any, record: NormalizedProviderConfig) -> dict[str, Any]:
        api_key = str(record.auth.get("api_key", "") or "")
        return {
            "key": binding.key,
            "llm_config_id": record.config_id,
            "provider_model": self._provider_model_from_record(record),
            "default_model": record.default_model,
            "api_key": MASKED_SECRET_VALUE if api_key else "",
            "api_base": record.base_url,
            "extra_headers": record.headers,
            "enabled": binding.enabled,
            "max_tokens": record.parameters.get("max_tokens"),
            "temperature": record.parameters.get("temperature"),
            "reasoning_effort": record.parameters.get("reasoning_effort"),
            "retry_on": list(binding.retry_on or []),
            "retry_count": int(getattr(binding, "retry_count", 0) or 0),
            "single_api_key_max_concurrency": getattr(binding, "single_api_key_max_concurrency", None),
            "description": binding.description,
            "name": str(getattr(binding, "name", "") or "").strip(),
            "context_window_tokens": record.parameters.get("context_window_tokens"),
            "request_timeout_seconds": record.parameters.get("request_timeout_seconds"),
            "image_multimodal_enabled": bool(getattr(binding, "image_multimodal_enabled", False)),
            "quota_pool_key": str(getattr(binding, "quota_pool_key", "") or "").strip() or None,
            "capability": record.capability.value,
            "auth_mode": record.auth_mode.value,
            "config_id": record.config_id,
            "record_missing": False,
        }

    def _missing_record_payload(self, binding: Any) -> dict[str, Any]:
        """`llm_config_id` 指向的记录已经不在了：目录行自带的那一半照出，凭据那一半留空。

        键集合与 `_binding_payload` 对齐，operator 才能照常打开这行并把它删掉；
        `record_missing` 是让前端说清「这一行需要重新绑定」的唯一判据。
        """
        config_id = str(getattr(binding, "llm_config_id", "") or "").strip()
        return {
            "key": binding.key,
            "llm_config_id": config_id,
            "provider_model": str(getattr(binding, "provider_model", "") or "").strip(),
            "default_model": "",
            "api_key": "",
            "api_base": getattr(binding, "api_base", None),
            "extra_headers": getattr(binding, "extra_headers", None) or {},
            "enabled": bool(getattr(binding, "enabled", True)),
            "max_tokens": getattr(binding, "max_tokens", None),
            "temperature": getattr(binding, "temperature", None),
            "reasoning_effort": getattr(binding, "reasoning_effort", None),
            "retry_on": list(binding.retry_on or []),
            "retry_count": int(getattr(binding, "retry_count", 0) or 0),
            "single_api_key_max_concurrency": getattr(binding, "single_api_key_max_concurrency", None),
            "description": str(getattr(binding, "description", "") or ""),
            "name": str(getattr(binding, "name", "") or "").strip(),
            "context_window_tokens": getattr(binding, "context_window_tokens", None),
            "request_timeout_seconds": getattr(binding, "request_timeout_seconds", None),
            "image_multimodal_enabled": bool(getattr(binding, "image_multimodal_enabled", False)),
            "quota_pool_key": str(getattr(binding, "quota_pool_key", "") or "").strip() or None,
            "capability": "",
            "auth_mode": "",
            "config_id": config_id,
            "record_missing": True,
        }


    def _get_optional_record(self, config_id: str) -> NormalizedProviderConfig | None:
        try:
            return self.repository.get(config_id)
        except Exception:
            return None

    def _provider_model_from_record(self, record: NormalizedProviderConfig | None) -> str:
        if record is None:
            return ""
        return f"{record.provider_id}:{record.default_model}"

    @staticmethod
    def _binding_context_window_tokens(record: NormalizedProviderConfig | None) -> int | None:
        if record is None:
            return None
        try:
            resolved = int(dict(record.parameters or {}).get("context_window_tokens") or 0)
        except Exception:
            return None
        return resolved if resolved > 0 else None

    @staticmethod
    def _binding_request_timeout_seconds(record: NormalizedProviderConfig | None) -> float | None:
        if record is None:
            return None
        try:
            resolved = float(dict(record.parameters or {}).get("request_timeout_seconds") or 0)
        except Exception:
            return None
        return resolved if resolved > 0 else None

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

    def _store_record_secrets(self, record: NormalizedProviderConfig) -> bool:
        """把钥匙与请求头交给加密覆盖层；返回"覆盖层到底收没收"。

        项目锁定时覆盖层写不进去，此时**不能**再清洗盘上那份——清洗了就是两头都没有，
        模型直接变成"API key is required"。调用方一律走 `_persist_record`，判据只有一处。
        """
        if not self.security.is_unlocked():
            return False
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
        return True

    def _persist_record(
        self, record: NormalizedProviderConfig, *, last_probe_status: Any = None
    ) -> Any:
        """一条凭据记录唯一的落盘口：密钥先进覆盖层，收下了才把不带钥匙的那份写盘。"""
        stored = self._store_record_secrets(record)
        return self.repository.save(
            self._sanitize_record_for_storage(record) if stored else record,
            last_probe_status=last_probe_status,
        )

    @staticmethod
    def _validate_binding_api_key_limits(
        *,
        api_key: str,
        value: SingleAPIKeyMaxConcurrency,
    ) -> SingleAPIKeyMaxConcurrency:
        if value is None:
            return None
        has_keys = bool(parse_api_keys(api_key))
        layout = resolve_api_key_concurrency_layout(
            api_key,
            value,
            include_empty_slot=not has_keys,
            reject_all_zero=has_keys,
        )
        return layout.configured_value

    def _runtime_target(
        self,
        *,
        model_key: str,
        record: NormalizedProviderConfig,
        retry_on: list[str],
        retry_count: int,
        single_api_key_max_concurrency: SingleAPIKeyMaxConcurrency,
    ) -> RuntimeTarget:
        model_parameters = _runtime_model_parameters(record.parameters)
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
            model_parameters=model_parameters,
            max_tokens_limit=int(model_parameters.get("max_tokens")) if model_parameters.get("max_tokens") is not None else None,
            default_temperature=float(model_parameters.get("temperature")) if model_parameters.get("temperature") is not None else None,
            default_reasoning_effort=str(model_parameters.get("reasoning_effort")) if model_parameters.get("reasoning_effort") is not None else None,
            retry_on=list(retry_on),
            retry_count=int(retry_count or 0),
            single_api_key_max_concurrency=single_api_key_max_concurrency,
            extra_options=dict(record.extra_options),
        )

    @staticmethod
    def _sanitize_parameter_mapping(payload: dict[str, Any] | None) -> dict[str, Any]:
        return {
            str(key or '').strip(): value
            for key, value in dict(payload or {}).items()
            if str(key or '').strip() and value not in (None, '')
        }

    @classmethod
    def _merge_parameter_patch(
        cls,
        current: dict[str, Any],
        patch: dict[str, Any] | None,
        *,
        replace: bool,
    ) -> dict[str, Any]:
        if replace:
            if patch is None:
                return dict(current or {})
            return cls._sanitize_parameter_mapping(patch)
        next_payload = dict(current or {})
        for key, value in dict(patch or {}).items():
            clean_key = str(key or '').strip()
            if not clean_key:
                continue
            if value in (None, ''):
                next_payload.pop(clean_key, None)
                continue
            next_payload[clean_key] = value
        return next_payload

    def _merge_draft(
        self,
        current: NormalizedProviderConfig,
        patch: dict[str, Any],
        *,
        replace_parameters: bool,
    ) -> ProviderConfigDraft:
        api_key = patch.get("api_key")
        if api_key == MASKED_SECRET_VALUE:
            api_key = current.auth.get("api_key", "")
        elif api_key is None:
            api_key = current.auth.get("api_key", "")
        parameter_patch = patch.get("parameters") if isinstance(patch.get("parameters"), dict) else None
        extra_headers = (
            dict(patch.get("extra_headers") or {})
            if "extra_headers" in patch and isinstance(patch.get("extra_headers"), dict)
            else dict(current.headers)
        )
        extra_options = (
            dict(patch.get("extra_options") or {})
            if "extra_options" in patch and isinstance(patch.get("extra_options"), dict)
            else dict(current.extra_options)
        )
        return ProviderConfigDraft(
            provider_id=str(patch.get("provider_id") or current.provider_id),
            capability=patch.get("capability", current.capability),
            auth_mode=patch.get("auth_mode", current.auth_mode),
            display_name=patch.get("display_name", current.display_name),
            api_key=str(api_key or ""),
            base_url=str(patch.get("base_url") or current.base_url),
            default_model=str(patch.get("default_model") or current.default_model),
            parameters=self._merge_parameter_patch(
                dict(current.parameters),
                parameter_patch,
                replace=replace_parameters,
            ),
            extra_headers=extra_headers,
            extra_options=extra_options,
        )
