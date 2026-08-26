from __future__ import annotations

from .enums import ProtocolAdapter
from .models import ProviderTemplate
from .template_builders import build_openai_compatible_template


PROVIDER_TEMPLATES: list[ProviderTemplate] = [
    build_openai_compatible_template(
        provider_id="openai",
        display_name="OpenAI Chat",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4.1",
        suggested_models=["gpt-4.1", "gpt-4o", "gpt-4o-mini", "gpt-5.4"],
        default_api_mode=ProtocolAdapter.OPENAI_COMPLETIONS,
    ),
    build_openai_compatible_template(
        provider_id="responses",
        display_name="OpenAI Responses",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-5.4",
        suggested_models=["gpt-4.1", "gpt-5.4"],
        default_api_mode=ProtocolAdapter.OPENAI_RESPONSES,
        category="direct",
    ),
]
