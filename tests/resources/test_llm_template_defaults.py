from __future__ import annotations

import pytest

from g3ku.llm_config.models import ProviderConfigDraft
from g3ku.llm_config.normalization import normalize_draft
from g3ku.llm_config.template_registry import TemplateRegistry

_CORE_FIELDS = {"api_key", "base_url", "default_model", "extra_headers", "extra_options"}


def _draft_from_template(provider_id: str) -> ProviderConfigDraft:
    registry = TemplateRegistry()
    template = registry.get_template(provider_id)
    parameters = {
        field.key: field.default
        for field in template.fields
        if field.key not in _CORE_FIELDS and field.default not in {None, ""}
    }
    return ProviderConfigDraft(
        provider_id=template.provider_id,
        capability=template.capability,
        auth_mode=template.auth_mode,
        display_name=template.display_name,
        api_key="test-key" if template.auth_mode.value == "api_key" else "oauth-cache",
        base_url=template.default_base_url,
        default_model=template.default_model,
        parameters=parameters,
        extra_headers={},
        extra_options={},
    )


@pytest.mark.parametrize(
    ("provider_id", "expected_api_mode"),
    [
        ("custom", "custom-direct"),
        ("openai_codex", "oauth-proxy"),
        ("github_copilot", "oauth-proxy"),
        ("dashscope_embedding", "dashscope-embedding"),
        ("dashscope_rerank", "dashscope-rerank"),
    ],
)
def test_template_default_draft_accepts_nonstandard_api_mode(provider_id: str, expected_api_mode: str):
    registry = TemplateRegistry()
    draft = _draft_from_template(provider_id)

    normalized, errors = normalize_draft(draft, registry)

    assert errors == []
    assert normalized is not None
    assert normalized.parameters["api_mode"] == expected_api_mode
    assert normalized.protocol_adapter.value == expected_api_mode


def test_openai_template_keeps_standard_api_mode_choices():
    template = TemplateRegistry().get_template("openai")
    api_mode_field = next(field for field in template.fields if field.key == "api_mode")

    assert [option.value for option in api_mode_field.options] == [
        "openai-completions",
        "openai-responses",
    ]
