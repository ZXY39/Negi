from __future__ import annotations

from .exceptions import TemplateNotFoundError
from .models import ProviderTemplate, ProviderTemplateSummary
from .provider_snapshots import PROVIDER_TEMPLATES


class TemplateRegistry:
    def __init__(self, templates: list[ProviderTemplate] | None = None):
        self._templates = {template.provider_id: template for template in templates or PROVIDER_TEMPLATES}

    def list_templates(self) -> list[ProviderTemplateSummary]:
        summaries = [
            ProviderTemplateSummary(
                provider_id=template.provider_id,
                display_name=template.display_name,
                protocol_adapter=template.protocol_adapter,
                capability=template.capability,
                auth_mode=template.auth_mode,
                category=template.category,
                supports_custom_base_url=True,
                supports_api_key=template.auth_mode.value == "api_key",
                default_model=template.default_model,
            )
            for template in self._templates.values()
        ]
        return sorted(summaries, key=lambda item: (item.category, item.display_name.lower()))

    def get_template(self, provider_id: str) -> ProviderTemplate:
        normalized = provider_id.strip().lower()
        try:
            return self._templates[normalized]
        except KeyError as exc:
            raise TemplateNotFoundError(f"Unknown provider template: {provider_id}") from exc
