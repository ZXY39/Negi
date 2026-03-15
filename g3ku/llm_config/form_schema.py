from __future__ import annotations

from .models import ProviderConfigDraft, ProviderTemplate, TemplateFieldSpec

CORE_FIELD_PATHS = {
    "api_key": "api_key",
    "base_url": "base_url",
    "default_model": "default_model",
    "extra_headers": "extra_headers",
    "extra_options": "extra_options",
}


def resolve_field_path(field: TemplateFieldSpec) -> str:
    return CORE_FIELD_PATHS.get(field.key, f"parameters.{field.key}")


def build_form_spec(template: ProviderTemplate) -> dict[str, object]:
    basic_fields = []
    advanced_fields = []
    for field in template.fields:
        payload = {
            **field.model_dump(mode="json"),
            "path": resolve_field_path(field),
        }
        if field.advanced:
            advanced_fields.append(payload)
        else:
            basic_fields.append(payload)
    return {
        "provider": {
            "provider_id": template.provider_id,
            "display_name": template.display_name,
            "category": template.category,
            "protocol_adapter": template.protocol_adapter.value,
            "default_base_url": template.default_base_url,
            "default_model": template.default_model,
            "suggested_models": template.suggested_models,
            "template_version": template.template_version,
        },
        "fields": basic_fields + advanced_fields,
        "field_groups": {
            "basic": basic_fields,
            "advanced": advanced_fields,
        },
        "draft_schema": ProviderConfigDraft.model_json_schema(),
    }

