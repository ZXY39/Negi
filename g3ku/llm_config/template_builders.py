from __future__ import annotations

from typing import Iterable

from g3ku.utils.api_keys import MULTI_API_KEY_HELP_TEXT, MULTI_API_KEY_PLACEHOLDER

from .enums import AuthMode, Capability, FieldInputType, ProtocolAdapter
from .models import ProviderTemplate, TemplateFieldOption, TemplateFieldSpec

TEMPLATE_VERSION = "2026.03.15.1"


def option(value: str, label: str | None = None) -> TemplateFieldOption:
    return TemplateFieldOption(value=value, label=label or value)


def field(
    *,
    key: str,
    label: str,
    input_type: FieldInputType,
    required: bool,
    secret: bool = False,
    advanced: bool = False,
    default: object | None = None,
    placeholder: str | None = None,
    help_text: str | None = None,
    options: Iterable[TemplateFieldOption] | None = None,
    constraints: dict[str, object] | None = None,
) -> TemplateFieldSpec:
    return TemplateFieldSpec(
        key=key,
        label=label,
        input_type=input_type,
        required=required,
        secret=secret,
        advanced=advanced,
        default=default,
        placeholder=placeholder,
        help=help_text,
        options=list(options or []),
        constraints=dict(constraints or {}),
    )


def _base_fields(default_base_url: str, default_model: str, api_key_default: str = "") -> list[TemplateFieldSpec]:
    return [
        field(
            key="api_key",
            label="API Key",
            input_type=FieldInputType.SECRET,
            required=True,
            secret=True,
            default=api_key_default,
            placeholder=MULTI_API_KEY_PLACEHOLDER,
            help_text=MULTI_API_KEY_HELP_TEXT,
        ),
        field(
            key="base_url",
            label="Base URL",
            input_type=FieldInputType.URL,
            required=True,
            default=default_base_url,
        ),
        field(
            key="default_model",
            label="Default Model",
            input_type=FieldInputType.SELECT,
            required=True,
            default=default_model,
        ),
        field(
            key="timeout_s",
            label="Timeout (seconds)",
            input_type=FieldInputType.NUMBER,
            required=True,
            default=8,
            constraints={"min": 1, "max": 20},
        ),
        field(
            key="temperature",
            label="Temperature",
            input_type=FieldInputType.NUMBER,
            required=True,
            default=0.2,
            constraints={"min": 0, "max": 2},
        ),
        field(
            key="max_tokens",
            label="Max Tokens",
            input_type=FieldInputType.NUMBER,
            required=True,
            default=4096,
            constraints={"min": 1},
        ),
        field(
            key="extra_headers",
            label="Extra Headers",
            input_type=FieldInputType.KV_LIST,
            required=False,
            advanced=True,
            default={},
        ),
        field(
            key="extra_options",
            label="Extra Options",
            input_type=FieldInputType.JSON,
            required=False,
            advanced=True,
            default={},
        ),
    ]


def _api_mode_options(default_api_mode: ProtocolAdapter) -> list[TemplateFieldOption]:
    if default_api_mode in {
        ProtocolAdapter.OPENAI_COMPLETIONS,
        ProtocolAdapter.OPENAI_RESPONSES,
    }:
        return [
            option(ProtocolAdapter.OPENAI_COMPLETIONS.value, "OpenAI Completions"),
            option(ProtocolAdapter.OPENAI_RESPONSES.value, "OpenAI Responses"),
        ]
    return [option(default_api_mode.value, default_api_mode.value)]


def build_openai_compatible_template(
    *,
    provider_id: str,
    display_name: str,
    default_base_url: str,
    default_model: str,
    suggested_models: list[str],
    category: str = "openai-compatible",
    default_headers: dict[str, str] | None = None,
    site_metadata: bool = False,
    default_api_mode: ProtocolAdapter = ProtocolAdapter.OPENAI_COMPLETIONS,
    api_key_default: str = "",
    capability: Capability = Capability.CHAT,
    auth_mode: AuthMode = AuthMode.API_KEY,
) -> ProviderTemplate:
    fields = _base_fields(default_base_url, default_model, api_key_default)
    for entry in fields:
        if entry.key == "default_model":
            entry.options = [option(model) for model in suggested_models]
    fields.extend(
        [
            field(
                key="organization",
                label="Organization",
                input_type=FieldInputType.TEXT,
                required=False,
                advanced=True,
                placeholder="org_...",
                help_text="OpenAI-compatible organization header when supported.",
            ),
            field(
                key="project",
                label="Project",
                input_type=FieldInputType.TEXT,
                required=False,
                advanced=True,
                placeholder="proj_...",
                help_text="OpenAI-compatible project header when supported.",
            ),
            field(
                key="reasoning_effort",
                label="Reasoning Effort",
                input_type=FieldInputType.SELECT,
                required=False,
                advanced=True,
                default="medium",
                options=[option("low"), option("medium"), option("high")],
            ),
            field(
                key="auth_header",
                label="Use Authorization Header",
                input_type=FieldInputType.BOOLEAN,
                required=False,
                advanced=True,
                default=True,
            ),
            field(
                key="api_mode",
                label="API Mode",
                input_type=FieldInputType.SELECT,
                required=False,
                advanced=True,
                default=default_api_mode.value,
                options=_api_mode_options(default_api_mode),
            ),
        ]
    )
    if site_metadata:
        fields.extend(
            [
                field(
                    key="site_url",
                    label="Site URL",
                    input_type=FieldInputType.URL,
                    required=False,
                    advanced=True,
                    placeholder="https://your-app.example",
                ),
                field(
                    key="site_name",
                    label="Site Name",
                    input_type=FieldInputType.TEXT,
                    required=False,
                    advanced=True,
                    placeholder="Your App",
                ),
            ]
        )
    return ProviderTemplate(
        provider_id=provider_id,
        display_name=display_name,
        protocol_adapter=default_api_mode,
        capability=capability,
        auth_mode=auth_mode,
        category=category,
        default_base_url=default_base_url,
        default_model=default_model,
        suggested_models=suggested_models,
        default_headers=default_headers or {},
        fields=fields,
        extra_options_schema={"type": "object", "additionalProperties": True},
        template_version=TEMPLATE_VERSION,
    )


def build_anthropic_compatible_template(
    *,
    provider_id: str,
    display_name: str,
    default_base_url: str,
    default_model: str,
    suggested_models: list[str],
    category: str = "anthropic-compatible",
    default_headers: dict[str, str] | None = None,
    api_key_default: str = "",
    capability: Capability = Capability.CHAT,
    auth_mode: AuthMode = AuthMode.API_KEY,
) -> ProviderTemplate:
    fields = _base_fields(default_base_url, default_model, api_key_default)
    for entry in fields:
        if entry.key == "default_model":
            entry.options = [option(model) for model in suggested_models]
    fields.extend(
        [
            field(
                key="anthropic_version",
                label="Anthropic Version",
                input_type=FieldInputType.TEXT,
                required=False,
                advanced=True,
                default="2023-06-01",
            ),
            field(
                key="top_p",
                label="Top P",
                input_type=FieldInputType.NUMBER,
                required=False,
                advanced=True,
                default=1.0,
                constraints={"min": 0, "max": 1},
            ),
            field(
                key="thinking_budget_tokens",
                label="Thinking Budget Tokens",
                input_type=FieldInputType.NUMBER,
                required=False,
                advanced=True,
                default=1024,
                constraints={"min": 0},
            ),
        ]
    )
    return ProviderTemplate(
        provider_id=provider_id,
        display_name=display_name,
        protocol_adapter=ProtocolAdapter.ANTHROPIC_MESSAGES,
        capability=capability,
        auth_mode=auth_mode,
        category=category,
        default_base_url=default_base_url,
        default_model=default_model,
        suggested_models=suggested_models,
        default_headers=default_headers or {"anthropic-version": "2023-06-01"},
        fields=fields,
        extra_options_schema={"type": "object", "additionalProperties": True},
        template_version=TEMPLATE_VERSION,
    )


def build_gemini_template(
    *,
    provider_id: str,
    display_name: str,
    default_base_url: str,
    default_model: str,
    suggested_models: list[str],
    capability: Capability = Capability.CHAT,
    auth_mode: AuthMode = AuthMode.API_KEY,
) -> ProviderTemplate:
    fields = _base_fields(default_base_url, default_model)
    for entry in fields:
        if entry.key == "default_model":
            entry.options = [option(model) for model in suggested_models]
    fields.extend(
        [
            field(
                key="api_version",
                label="API Version",
                input_type=FieldInputType.SELECT,
                required=False,
                advanced=True,
                default="v1beta",
                options=[option("v1beta"), option("v1")],
            ),
            field(
                key="safety_settings",
                label="Safety Settings",
                input_type=FieldInputType.JSON,
                required=False,
                advanced=True,
                default={},
            ),
        ]
    )
    return ProviderTemplate(
        provider_id=provider_id,
        display_name=display_name,
        protocol_adapter=ProtocolAdapter.GOOGLE_GENERATIVE_AI,
        capability=capability,
        auth_mode=auth_mode,
        category="google",
        default_base_url=default_base_url,
        default_model=default_model,
        suggested_models=suggested_models,
        default_headers={},
        fields=fields,
        extra_options_schema={"type": "object", "additionalProperties": True},
        template_version=TEMPLATE_VERSION,
    )


def build_ollama_template(
    *,
    provider_id: str,
    display_name: str,
    default_base_url: str,
    default_model: str,
    suggested_models: list[str],
    capability: Capability = Capability.CHAT,
    auth_mode: AuthMode = AuthMode.API_KEY,
) -> ProviderTemplate:
    fields = _base_fields(default_base_url, default_model, api_key_default="ollama-local")
    for entry in fields:
        if entry.key == "default_model":
            entry.options = [option(model) for model in suggested_models]
    fields.extend(
        [
            field(
                key="num_ctx",
                label="Context Window",
                input_type=FieldInputType.NUMBER,
                required=False,
                advanced=True,
                default=32768,
                constraints={"min": 1},
            ),
            field(
                key="keep_alive",
                label="Keep Alive",
                input_type=FieldInputType.TEXT,
                required=False,
                advanced=True,
                default="5m",
            ),
            field(
                key="stream",
                label="Stream",
                input_type=FieldInputType.BOOLEAN,
                required=False,
                advanced=True,
                default=True,
            ),
        ]
    )
    return ProviderTemplate(
        provider_id=provider_id,
        display_name=display_name,
        protocol_adapter=ProtocolAdapter.OLLAMA,
        capability=capability,
        auth_mode=auth_mode,
        category="local-native",
        default_base_url=default_base_url,
        default_model=default_model,
        suggested_models=suggested_models,
        default_headers={},
        fields=fields,
        extra_options_schema={"type": "object", "additionalProperties": True},
        template_version=TEMPLATE_VERSION,
    )
