from __future__ import annotations

from typing import Iterable

from g3ku.utils.api_keys import MULTI_API_KEY_HELP_TEXT, MULTI_API_KEY_PLACEHOLDER

from .enums import AuthMode, Capability, FieldInputType, ProtocolAdapter
from .models import ProviderTemplate, TemplateFieldOption, TemplateFieldSpec

TEMPLATE_VERSION = "2026.04.01.1"


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


def _base_fields(
    default_base_url: str,
    default_model: str,
    api_key_default: str = "",
    *,
    capability: Capability = Capability.CHAT,
) -> list[TemplateFieldSpec]:
    fields = [
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
    if capability == Capability.CHAT:
        fields.extend(
            [
                field(
                    key="context_window_tokens",
                    label="Context Window Tokens",
                    input_type=FieldInputType.NUMBER,
                    required=True,
                    default=32000,
                    constraints={"min": 25001, "integer": True},
                    help_text="Required for chat providers and must be greater than 25000.",
                ),
                field(
                    key="temperature",
                    label="Temperature",
                    input_type=FieldInputType.NUMBER,
                    required=False,
                    default=None,
                    constraints={"min": 0, "max": 2, "integer": False},
                ),
                field(
                    key="max_tokens",
                    label="Max Tokens",
                    input_type=FieldInputType.NUMBER,
                    required=False,
                    default=None,
                    constraints={"min": 1, "integer": True},
                ),
            ]
        )
    return fields


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
    fields = _base_fields(
        default_base_url,
        default_model,
        api_key_default,
        capability=capability,
    )
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
                default=None,
                options=[
                    option("none"),
                    option("low"),
                    option("medium"),
                    option("high"),
                    option("xhigh"),
                    option("max"),
                ],
            ),
            field(
                key="reasoning_input_mode",
                label="Reasoning Input Mode",
                input_type=FieldInputType.SELECT,
                required=False,
                advanced=True,
                default="level",
                options=[option("level"), option("token")],
                help_text="level: 六档映射；token: 自填思考预算（OpenAI 系走 thinking_budget，切回 level 保留不生效）。",
            ),
            field(
                key="reasoning_budget_tokens",
                label="Reasoning Budget Tokens",
                input_type=FieldInputType.NUMBER,
                required=False,
                advanced=True,
                default=8192,
                constraints={"min": 0, "integer": True},
                help_text="token 模式的自定义思考预算，默认 8192（medium 档）。",
            ),
            field(
                key="auth_header",
                label="Use Authorization Header",
                input_type=FieldInputType.BOOLEAN,
                required=False,
                advanced=True,
                default=True,
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


def build_anthropic_template(
    *,
    provider_id: str = "anthropic",
    display_name: str = "Anthropic",
    default_base_url: str = "https://api.anthropic.com/v1",
    default_model: str = "claude-sonnet-4-5",
    suggested_models: list[str] | None = None,
    category: str = "anthropic",
    default_headers: dict[str, str] | None = None,
    capability: Capability = Capability.CHAT,
    auth_mode: AuthMode = AuthMode.API_KEY,
) -> ProviderTemplate:
    """Anthropic Messages protocol template.

    Differences from the OpenAI-compatible builder: no organization/project derived
    headers (OpenAI-only), ``auth_header`` defaults to False so the key rides
    ``x-api-key`` (Anthropic's scheme), and ``anthropic-version`` is a default header.
    The reasoning_effort select keeps Negi's six managed levels; the provider maps
    them to Anthropic thinking budgets.
    """
    suggested_models = list(suggested_models or ["claude-sonnet-4-5", "claude-opus-4-1"])
    fields = _base_fields(default_base_url, default_model, capability=capability)
    for entry in fields:
        if entry.key == "default_model":
            entry.options = [option(model) for model in suggested_models]
    fields.extend(
        [
            field(
                key="reasoning_effort",
                label="Reasoning Effort",
                input_type=FieldInputType.SELECT,
                required=False,
                advanced=True,
                default=None,
                options=[
                    option("none"),
                    option("low"),
                    option("medium"),
                    option("high"),
                    option("xhigh"),
                    option("max"),
                ],
            ),
            field(
                key="reasoning_input_mode",
                label="Reasoning Input Mode",
                input_type=FieldInputType.SELECT,
                required=False,
                advanced=True,
                default="level",
                options=[option("level"), option("token")],
                help_text="level: 六档映射预算；token: 自填 budget_tokens（切回 level 时保留不生效）。",
            ),
            field(
                key="reasoning_budget_tokens",
                label="Reasoning Budget Tokens",
                input_type=FieldInputType.NUMBER,
                required=False,
                advanced=True,
                default=8192,
                constraints={"min": 0, "integer": True},
                help_text="token 模式的自定义思考预算，默认 8192（medium 档）。须小于 max_tokens。",
            ),
            field(
                key="auth_header",
                label="Use Authorization Header",
                input_type=FieldInputType.BOOLEAN,
                required=False,
                advanced=True,
                default=False,
                help_text="Off (default) sends x-api-key, Anthropic's scheme. On sends Authorization: Bearer for gateways that require it.",
            ),
        ]
    )
    headers = {"anthropic-version": "2023-06-01"}
    headers.update(default_headers or {})
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
        default_headers=headers,
        fields=fields,
        extra_options_schema={"type": "object", "additionalProperties": True},
        template_version=TEMPLATE_VERSION,
    )

