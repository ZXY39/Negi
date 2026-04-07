from __future__ import annotations

from types import SimpleNamespace

from g3ku.config.schema import ManagedModelConfig
from g3ku.llm_config.facade import LLMConfigFacade
from g3ku.llm_config.models import ModelBindingDraft, ProviderConfigDraft
from g3ku.llm_config.normalization import normalize_draft
from g3ku.llm_config.template_registry import TemplateRegistry
import pytest


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


def test_managed_model_config_accepts_per_key_concurrency_limit_arrays() -> None:
    config = ManagedModelConfig(
        key="primary",
        provider_model="openai:gpt-4.1",
        api_key="key-1,key-2",
        single_api_key_max_concurrency=[3, 5],
    )

    assert config.single_api_key_max_concurrency == [3, 5]


def test_model_binding_draft_accepts_per_key_concurrency_limit_arrays() -> None:
    draft = ModelBindingDraft(
        key="primary",
        config_id="cfg-1",
        single_api_key_max_concurrency=[3, 5, 7],
    )

    assert draft.single_api_key_max_concurrency == [3, 5, 7]


def test_binding_api_key_limits_reject_mismatched_array_length() -> None:
    with pytest.raises(ValueError, match="expects 3 entries"):
        LLMConfigFacade._validate_binding_api_key_limits(
            api_key="key-1,key-2,key-3",
            value=[3, 5],
        )


def test_binding_api_key_limits_reject_all_zero_arrays() -> None:
    with pytest.raises(ValueError, match="leave at least one API key enabled"):
        LLMConfigFacade._validate_binding_api_key_limits(
            api_key="key-1,key-2",
            value=[0, 0],
        )


def test_create_binding_rejects_duplicate_model_key_before_creating_config_record(monkeypatch, tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    facade = LLMConfigFacade(workspace)
    config = SimpleNamespace(
        models=SimpleNamespace(catalog=[]),
        get_managed_model=lambda key: SimpleNamespace(key=key) if key == "primary" else None,
    )

    def _unexpected_create_config_record(payload):
        raise AssertionError(f"create_config_record should not be called for duplicate keys: {payload!r}")

    monkeypatch.setattr(facade, "create_config_record", _unexpected_create_config_record)

    with pytest.raises(ValueError, match="Model key already exists: primary"):
        facade.create_binding(
            config,
            draft_payload={"provider_id": "custom"},
            binding_payload={
                "key": "primary",
                "config_id": "",
                "enabled": True,
            },
        )
