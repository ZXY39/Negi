from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx

from .exceptions import ProbeFailedError, ValidationFailedError
from .export import to_generic_runtime_config
from .form_schema import build_form_spec
from .models import (
    GenericRuntimeConfig,
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

