from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from .exceptions import ProbeFailedError, ValidationFailedError
from .export import to_generic_runtime_config
from .form_schema import build_form_spec
from .models import (
    APIKeyMaxConcurrencyProbeItem,
    GenericRuntimeConfig,
    MaxConcurrencyProbeResult,
    NormalizedProviderConfig,
    ProbeResult,
    ProviderConfigDraft,
    ProviderTemplate,
    ProviderTemplateSummary,
    StoredConfigSummary,
    ValidationResult,
)
from .normalization import normalize_draft
from .probe_strategies import probe_config
from .repositories import EncryptedConfigRepository
from .template_registry import TemplateRegistry
from g3ku.utils.api_keys import parse_api_keys

_MAX_CONCURRENCY_PROBE_LEVELS = (1, 2, 4, 8, 16, 32)
_MAX_CONCURRENCY_PARALLEL_KEYS = 5


def _redact_config(config: NormalizedProviderConfig) -> NormalizedProviderConfig:
    redacted_auth = dict(config.auth)
    if "api_key" in redacted_auth:
        redacted_auth["api_key"] = "***"
    return config.model_copy(update={"auth": redacted_auth})


class TemplateService:
    def __init__(self, registry: TemplateRegistry | None = None):
        self.registry = registry or TemplateRegistry()

    def list_templates(self) -> list[ProviderTemplateSummary]:
        return self.registry.list_templates()

    def get_template(self, provider_id: str) -> ProviderTemplate:
        return self.registry.get_template(provider_id)

    def get_form_spec(self, provider_id: str) -> dict[str, object]:
        return build_form_spec(self.get_template(provider_id))


class ConfigService:
    def __init__(
        self,
        repository: EncryptedConfigRepository,
        *,
        template_service: TemplateService | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.repository = repository
        self.template_service = template_service or TemplateService()
        self.transport = transport

    def validate_draft(self, draft: ProviderConfigDraft) -> ValidationResult:
        normalized, errors = normalize_draft(draft, self.template_service.registry)
        return ValidationResult(valid=not errors, errors=errors, normalized_preview=normalized)

    def probe_draft(self, draft: ProviderConfigDraft) -> ProbeResult:
        validation = self.validate_draft(draft)
        if not validation.valid or validation.normalized_preview is None:
            return ProbeResult(
                status="invalid_response",
                success=False,
                provider_id=draft.provider_id,
                protocol_adapter=self.template_service.get_template(draft.provider_id).protocol_adapter,
                resolved_base_url=draft.base_url,
                checked_model=draft.default_model,
                message="Draft validation failed.",
                diagnostics={"errors": [error.model_dump(mode="json") for error in validation.errors]},
            )
        return probe_config(validation.normalized_preview, transport=self.transport)

    async def probe_max_concurrency_draft(self, draft: ProviderConfigDraft) -> MaxConcurrencyProbeResult:
        validation = self.validate_draft(draft)
        if not validation.valid or validation.normalized_preview is None:
            raise ValidationFailedError("Draft validation failed.")

        normalized = validation.normalized_preview
        api_keys = parse_api_keys(str(normalized.auth.get("api_key", "") or ""))
        if not api_keys:
            raise ValidationFailedError("At least one API key is required to probe max concurrency.")

        semaphore = asyncio.Semaphore(_MAX_CONCURRENCY_PARALLEL_KEYS)

        async def _probe_key(key_index: int, api_key: str) -> APIKeyMaxConcurrencyProbeItem:
            async with semaphore:
                return await self._probe_single_api_key_max_concurrency(
                    normalized=normalized,
                    key_index=key_index,
                    api_key=api_key,
                )

        per_key_results = list(
            await asyncio.gather(
                *[_probe_key(key_index, api_key) for key_index, api_key in enumerate(api_keys)]
            )
        )
        suggested_limits = [int(item.suggested_limit) for item in per_key_results]
        successful_count = sum(1 for item in per_key_results if item.suggested_limit > 0)
        if successful_count == len(per_key_results):
            message = f"Detected max concurrency for all {len(per_key_results)} API keys."
        elif successful_count > 0:
            message = f"Detected max concurrency for {successful_count}/{len(per_key_results)} API keys."
        else:
            message = "Connection test failed for all API keys."
        return MaxConcurrencyProbeResult(
            success=successful_count > 0,
            provider_id=normalized.provider_id,
            protocol_adapter=normalized.protocol_adapter,
            capability=normalized.capability,
            resolved_base_url=normalized.base_url,
            checked_model=normalized.default_model,
            message=message,
            suggested_limits=suggested_limits,
            per_key_results=per_key_results,
            diagnostics={"api_key_count": len(api_keys), "parallel_key_limit": _MAX_CONCURRENCY_PARALLEL_KEYS},
        )

    async def _probe_single_api_key_max_concurrency(
        self,
        *,
        normalized: NormalizedProviderConfig,
        key_index: int,
        api_key: str,
    ) -> APIKeyMaxConcurrencyProbeItem:
        single_key_config = normalized.model_copy(
            update={"auth": {**dict(normalized.auth), "api_key": str(api_key or "").strip()}}
        )
        connection_probe = await self._probe_single_config_async(single_key_config)
        if not connection_probe.success:
            return APIKeyMaxConcurrencyProbeItem(
                key_index=key_index,
                api_key_mask=self._mask_api_key(api_key),
                suggested_limit=0,
                connection_probe=connection_probe,
                attempted_levels=[],
                last_successful_limit=0,
                first_failed_limit=1,
            )

        attempted_levels: list[int] = []
        last_successful_limit = 0
        first_failed_limit: int | None = None

        for level in _MAX_CONCURRENCY_PROBE_LEVELS:
            attempted_levels.append(level)
            if await self._probe_concurrency_level(single_key_config, level):
                last_successful_limit = level
                continue
            first_failed_limit = level
            break

        if first_failed_limit is not None:
            low = last_successful_limit
            high = first_failed_limit - 1
            while low < high:
                mid = (low + high + 1) // 2
                attempted_levels.append(mid)
                if await self._probe_concurrency_level(single_key_config, mid):
                    low = mid
                else:
                    high = mid - 1
            last_successful_limit = low

        return APIKeyMaxConcurrencyProbeItem(
            key_index=key_index,
            api_key_mask=self._mask_api_key(api_key),
            suggested_limit=last_successful_limit,
            connection_probe=connection_probe,
            attempted_levels=attempted_levels,
            last_successful_limit=last_successful_limit,
            first_failed_limit=first_failed_limit,
        )

    async def _probe_concurrency_level(self, normalized: NormalizedProviderConfig, level: int) -> bool:
        results = await asyncio.gather(
            *[self._probe_single_config_async(normalized) for _ in range(max(1, int(level or 1)))]
        )
        return all(bool(item.success) for item in results)

    async def _probe_single_config_async(self, normalized: NormalizedProviderConfig) -> ProbeResult:
        return await asyncio.to_thread(probe_config, normalized, transport=self.transport)

    @staticmethod
    def _mask_api_key(api_key: str) -> str:
        raw = str(api_key or "").strip()
        if len(raw) <= 8:
            return raw
        return f"{raw[:4]}...{raw[-4:]}"

    def save_draft(
        self, draft: ProviderConfigDraft, *, require_successful_probe: bool = True
    ) -> StoredConfigSummary:
        validation = self.validate_draft(draft)
        if not validation.valid or validation.normalized_preview is None:
            raise ValidationFailedError("Draft validation failed.")
        probe = self.probe_draft(draft) if require_successful_probe else None
        if require_successful_probe and (probe is None or not probe.success):
            raise ProbeFailedError(probe.message if probe else "Probe failed.")
        now = datetime.now(UTC)
        normalized = validation.normalized_preview.model_copy(
            update={"config_id": uuid4().hex, "created_at": now, "updated_at": now}
        )
        return self.repository.save(
            normalized,
            last_probe_status=probe.status if probe is not None else None,
        )

    def list_configs(self) -> list[StoredConfigSummary]:
        return self.repository.list_summaries()

    def get_config(self, config_id: str, *, include_secrets: bool = False) -> NormalizedProviderConfig:
        config = self.repository.get(config_id)
        return config if include_secrets else _redact_config(config)

    def delete_config(self, config_id: str) -> None:
        self.repository.delete(config_id)

    def export_config(self, config_id: str, *, include_secrets: bool = True) -> GenericRuntimeConfig:
        config = self.repository.get(config_id)
        return to_generic_runtime_config(config, include_secrets=include_secrets)

