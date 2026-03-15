from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from .enums import AuthMode, FieldInputType, ProtocolAdapter
from .models import FieldError, NormalizedProviderConfig, ProviderConfigDraft, ProviderTemplate
from .template_registry import TemplateRegistry

PROVIDER_ID_ALIASES = {
    "z.ai": "zai",
    "z-ai": "zai",
}

DERIVED_HEADER_FIELDS = {
    "anthropic_version": "anthropic-version",
    "organization": "OpenAI-Organization",
    "project": "OpenAI-Project",
    "site_url": "HTTP-Referer",
    "site_name": "X-Title",
}

NON_PARAMETER_FIELDS = {"api_key", "base_url", "default_model", "extra_headers", "extra_options"}


def normalize_provider_id(provider_id: str) -> str:
    normalized = provider_id.strip().lower()
    return PROVIDER_ID_ALIASES.get(normalized, normalized)


def _parse_number(value: Any, *, integer: bool) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if integer else float(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value) if integer else float(value)
        except ValueError:
            return None
    return None


def _parse_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return None


def _validate_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


def _coerce_field_value(field_type: FieldInputType, raw_value: Any, default: Any) -> Any:
    if raw_value is None:
        return default
    if field_type in {FieldInputType.TEXT, FieldInputType.SECRET, FieldInputType.URL, FieldInputType.SELECT}:
        return str(raw_value).strip()
    if field_type == FieldInputType.NUMBER:
        parsed = _parse_number(raw_value, integer=isinstance(default, int) and not isinstance(default, bool))
        return parsed if parsed is not None else raw_value
    if field_type == FieldInputType.BOOLEAN:
        parsed_bool = _parse_boolean(raw_value)
        return parsed_bool if parsed_bool is not None else raw_value
    return raw_value


def _validate_field_constraints(field_key: str, value: Any, constraints: dict[str, Any]) -> list[FieldError]:
    errors: list[FieldError] = []
    if isinstance(value, (int, float)):
        min_value = constraints.get("min")
        max_value = constraints.get("max")
        if min_value is not None and value < min_value:
            errors.append(
                FieldError(field=field_key, code="below_min", message=f"Must be >= {min_value}.")
            )
        if max_value is not None and value > max_value:
            errors.append(
                FieldError(field=field_key, code="above_max", message=f"Must be <= {max_value}.")
            )
    return errors


def _build_headers(template: ProviderTemplate, parameters: dict[str, Any], extra_headers: dict[str, str]) -> dict[str, str]:
    headers = dict(template.default_headers)
    for parameter_key, header_name in DERIVED_HEADER_FIELDS.items():
        value = parameters.get(parameter_key)
        if isinstance(value, str) and value.strip():
            headers[header_name] = value.strip()
    for key, value in extra_headers.items():
        if key.strip() and value.strip():
            headers[key.strip()] = value.strip()
    return headers


def _resolve_protocol_adapter(template: ProviderTemplate, parameters: dict[str, Any]) -> ProtocolAdapter:
    raw_mode = parameters.get("api_mode")
    if isinstance(raw_mode, str):
        try:
            return ProtocolAdapter(raw_mode)
        except ValueError:
            return template.protocol_adapter
    return template.protocol_adapter


def normalize_draft(
    draft: ProviderConfigDraft,
    registry: TemplateRegistry,
    *,
    config_id: str = "preview",
    now: datetime | None = None,
) -> tuple[NormalizedProviderConfig | None, list[FieldError]]:
    errors: list[FieldError] = []
    normalized_provider_id = normalize_provider_id(draft.provider_id)
    try:
        template = registry.get_template(normalized_provider_id)
    except Exception:
        return None, [
            FieldError(field="provider_id", code="unknown_provider", message="Unknown provider template.")
        ]

    api_key = draft.api_key.strip()
    if draft.auth_mode == AuthMode.API_KEY and not api_key:
        errors.append(FieldError(field="api_key", code="required", message="API key is required."))

    base_url = draft.base_url.strip().rstrip("/")
    if not base_url:
        base_url = template.default_base_url.rstrip("/")
    if not _validate_url(base_url):
        errors.append(FieldError(field="base_url", code="invalid_url", message="Base URL is invalid."))

    default_model = draft.default_model.strip() or template.default_model
    if not default_model:
        errors.append(
            FieldError(field="default_model", code="required", message="Default model is required.")
        )

    parameters: dict[str, Any] = {}
    for field in template.fields:
        if field.key in NON_PARAMETER_FIELDS:
            continue
        raw_value = draft.parameters.get(field.key, field.default)
        value = _coerce_field_value(field.input_type, raw_value, field.default)
        if field.required and (value is None or value == ""):
            errors.append(
                FieldError(field=field.key, code="required", message=f"{field.label} is required.")
            )
            continue
        if field.input_type == FieldInputType.SELECT:
            allowed_values = {option.value for option in field.options}
            if value not in {None, ""} and allowed_values and value not in allowed_values:
                errors.append(
                    FieldError(field=field.key, code="invalid_choice", message="Invalid option selected.")
                )
        if field.input_type == FieldInputType.NUMBER and not isinstance(value, (int, float)):
            errors.append(
                FieldError(field=field.key, code="invalid_number", message="Expected a numeric value.")
            )
        if field.input_type == FieldInputType.BOOLEAN and not isinstance(value, bool):
            errors.append(
                FieldError(field=field.key, code="invalid_boolean", message="Expected a boolean value.")
            )
        parameters[field.key] = value
        errors.extend(_validate_field_constraints(field.key, value, field.constraints))

    extra_options = dict(draft.extra_options)
    if default_model not in template.suggested_models:
        extra_options["custom_model"] = True

    headers = _build_headers(template, parameters, draft.extra_headers)
    now = now or datetime.now(UTC)
    normalized = NormalizedProviderConfig(
        config_id=config_id,
        provider_id=template.provider_id,
        display_name=(draft.display_name or template.display_name).strip() or template.display_name,
        protocol_adapter=_resolve_protocol_adapter(template, parameters),
        capability=draft.capability,
        auth_mode=draft.auth_mode,
        base_url=base_url,
        default_model=default_model,
        auth={"type": draft.auth_mode.value, "api_key": api_key},
        parameters=parameters,
        headers=headers,
        extra_options=extra_options,
        template_version=template.template_version,
        created_at=now,
        updated_at=now,
    )
    return (normalized if not errors else None), errors
