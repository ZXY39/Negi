from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from g3ku.utils.api_keys import SingleAPIKeyMaxConcurrency, normalize_single_api_key_max_concurrency

from .enums import AuthMode, Capability, FieldInputType, ProbeStatus, ProtocolAdapter


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderTemplateSummary(StrictModel):
    provider_id: str
    display_name: str
    protocol_adapter: ProtocolAdapter
    capability: Capability = Capability.CHAT
    auth_mode: AuthMode = AuthMode.API_KEY
    category: str
    supports_custom_base_url: bool
    supports_api_key: bool
    default_model: str


class TemplateFieldOption(StrictModel):
    value: str
    label: str


class TemplateFieldSpec(StrictModel):
    key: str
    label: str
    input_type: FieldInputType
    required: bool
    secret: bool = False
    advanced: bool = False
    default: Any | None = None
    placeholder: str | None = None
    help: str | None = None
    options: list[TemplateFieldOption] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)


class ProviderTemplate(StrictModel):
    provider_id: str
    display_name: str
    protocol_adapter: ProtocolAdapter
    capability: Capability = Capability.CHAT
    auth_mode: AuthMode = AuthMode.API_KEY
    category: str
    default_base_url: str
    default_model: str
    suggested_models: list[str] = Field(default_factory=list)
    default_headers: dict[str, str] = Field(default_factory=dict)
    fields: list[TemplateFieldSpec] = Field(default_factory=list)
    extra_options_schema: dict[str, Any] = Field(default_factory=dict)
    template_version: str


class ProviderConfigDraft(StrictModel):
    provider_id: str
    capability: Capability = Capability.CHAT
    auth_mode: AuthMode = AuthMode.API_KEY
    display_name: str | None = None
    api_key: str
    base_url: str
    default_model: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_chat_context_window_tokens(self) -> "ProviderConfigDraft":
        if self.capability != Capability.CHAT:
            return self
        raw_value = dict(self.parameters or {}).get("context_window_tokens")
        if raw_value in (None, ""):
            raise ValueError("parameters.context_window_tokens is required for chat capability")
        try:
            resolved = int(raw_value)
        except Exception as exc:
            raise ValueError("parameters.context_window_tokens must be an integer") from exc
        if resolved <= 25_000:
            raise ValueError("parameters.context_window_tokens must be > 25000")
        self.parameters["context_window_tokens"] = resolved
        return self


class NormalizedProviderConfig(StrictModel):
    config_id: str
    provider_id: str
    display_name: str
    protocol_adapter: ProtocolAdapter
    capability: Capability = Capability.CHAT
    auth_mode: AuthMode = AuthMode.API_KEY
    base_url: str
    default_model: str
    auth: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    extra_options: dict[str, Any] = Field(default_factory=dict)
    template_version: str
    created_at: datetime
    updated_at: datetime


class FieldError(StrictModel):
    field: str
    code: str
    message: str


class ValidationResult(StrictModel):
    valid: bool
    errors: list[FieldError] = Field(default_factory=list)
    normalized_preview: NormalizedProviderConfig | None = None


class ProbeResult(StrictModel):
    status: ProbeStatus
    success: bool
    provider_id: str
    protocol_adapter: ProtocolAdapter
    capability: Capability = Capability.CHAT
    resolved_base_url: str
    checked_model: str
    latency_ms: int | None = None
    http_status: int | None = None
    message: str
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class APIKeyMaxConcurrencyProbeItem(StrictModel):
    key_index: int
    api_key_mask: str
    suggested_limit: int = Field(default=0, ge=0)
    connection_probe: ProbeResult
    attempted_levels: list[int] = Field(default_factory=list)
    last_successful_limit: int = Field(default=0, ge=0)
    first_failed_limit: int | None = Field(default=None, ge=1)


class MaxConcurrencyProbeResult(StrictModel):
    success: bool
    provider_id: str
    protocol_adapter: ProtocolAdapter
    capability: Capability = Capability.CHAT
    resolved_base_url: str
    checked_model: str
    message: str
    suggested_limits: list[int] = Field(default_factory=list)
    per_key_results: list[APIKeyMaxConcurrencyProbeItem] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class StoredConfigSummary(StrictModel):
    config_id: str
    provider_id: str
    display_name: str
    capability: Capability = Capability.CHAT
    default_model: str
    last_probe_status: str | None = None
    created_at: datetime
    updated_at: datetime


class GenericRuntimeConfig(StrictModel):
    provider_id: str
    protocol_adapter: ProtocolAdapter
    capability: Capability = Capability.CHAT
    auth_mode: AuthMode = AuthMode.API_KEY
    connection: dict[str, Any] = Field(default_factory=dict)
    auth: dict[str, Any] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    extra_options: dict[str, Any] = Field(default_factory=dict)


class RuntimeTarget(StrictModel):
    model_key: str
    config_id: str
    capability: Capability
    provider_id: str
    protocol_adapter: ProtocolAdapter
    auth_mode: AuthMode
    secret_payload: dict[str, Any] = Field(default_factory=dict)
    base_url: str
    resolved_model: str
    headers: dict[str, str] = Field(default_factory=dict)
    model_parameters: dict[str, Any] = Field(default_factory=dict)
    max_tokens_limit: int | None = None
    default_temperature: float | None = None
    default_reasoning_effort: str | None = None
    retry_on: list[str] = Field(default_factory=list)
    retry_count: int = Field(default=0, ge=0)
    single_api_key_max_concurrency: SingleAPIKeyMaxConcurrency = None
    extra_options: dict[str, Any] = Field(default_factory=dict)


class ModelBindingDraft(StrictModel):
    key: str
    config_id: str
    enabled: bool = True
    description: str = ""
    retry_on: list[str] = Field(default_factory=lambda: ["network", "429", "5xx"])
    retry_count: int = Field(default=0, ge=0)
    single_api_key_max_concurrency: SingleAPIKeyMaxConcurrency = None

    @field_validator("single_api_key_max_concurrency", mode="before")
    @classmethod
    def _normalize_single_api_key_max_concurrency(cls, value: Any) -> SingleAPIKeyMaxConcurrency:
        return normalize_single_api_key_max_concurrency(value)


class MemoryModelBinding(StrictModel):
    embedding_config_id: str | None = None
    embedding_provider_model: str = ""
    rerank_config_id: str | None = None
    rerank_provider_model: str = ""
