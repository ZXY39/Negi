from __future__ import annotations

from g3ku.config.schema import ManagedModelConfig
from g3ku.llm_config.models import ProviderConfigDraft
from g3ku.llm_config.normalization import normalize_draft
from g3ku.llm_config.template_registry import TemplateRegistry


def test_managed_model_config_accepts_multiple_api_keys() -> None:
    config = ManagedModelConfig(
        key="primary",
        provider_model="openai:gpt-4.1",
        api_key="key-1,key-2",
    )

    assert config.api_key == "key-1,key-2"


def test_managed_model_config_rejects_separator_only_api_keys() -> None:
    try:
        ManagedModelConfig(
            key="primary",
            provider_model="openai:gpt-4.1",
            api_key=" , \n ",
        )
    except ValueError as exc:
        assert "models.catalog[].api_key is required before migration" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("separator-only API keys should fail validation")


def test_normalize_draft_accepts_multiple_api_keys() -> None:
    registry = TemplateRegistry()
    draft = ProviderConfigDraft(
        provider_id="custom",
        api_key="key-1\nkey-2",
        base_url="https://example.com/v1",
        default_model="custom-model",
        parameters={
            "timeout_s": 8,
            "temperature": 0.2,
            "max_tokens": 256,
            "api_mode": "custom-direct",
        },
    )

    normalized, errors = normalize_draft(draft, registry)

    assert errors == []
    assert normalized is not None
    assert normalized.auth["api_key"] == "key-1\nkey-2"


def test_api_key_field_help_mentions_multiple_keys() -> None:
    template = TemplateRegistry().get_template("custom")
    api_key_field = next(field for field in template.fields if field.key == "api_key")

    assert "comma-separated" in str(api_key_field.help or "")
    assert "retry_count" in str(api_key_field.help or "")
