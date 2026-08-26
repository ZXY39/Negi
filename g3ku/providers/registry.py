"""
Provider Registry — single source of truth for LLM provider metadata.

The project supports exactly two OpenAI protocol entries:

  - `openai`:    OpenAI Chat Completions protocol (/v1/chat/completions)
  - `responses`: OpenAI Responses protocol (/v1/responses)

Any OpenAI-compatible endpoint binds through either entry with a custom base_url.
Adding a provider means adding a ProviderSpec below plus a template in
g3ku/llm_config/provider_snapshots.py.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    """One LLM provider entry's metadata.

    name:                    config field name / provider id, e.g. "openai"
    keywords:                model-name keywords for matching (lowercase)
    display_name:            shown in `g3ku status`
    supports_prompt_caching: provider has a native prompt-cache mechanism
    """

    name: str
    keywords: tuple[str, ...]
    display_name: str = ""
    supports_prompt_caching: bool = False

    @property
    def label(self) -> str:
        return self.display_name or self.name.title()


# ---------------------------------------------------------------------------
# PROVIDERS — the registry. Order = match priority.
# ---------------------------------------------------------------------------

PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="openai",
        keywords=("openai", "gpt"),
        display_name="OpenAI Chat",
    ),
    ProviderSpec(
        name="responses",
        keywords=("responses",),
        display_name="OpenAI Responses",
        supports_prompt_caching=True,
    ),
)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def find_by_model(model: str) -> ProviderSpec | None:
    """Match a provider by model-name keyword (case-insensitive)."""
    model_lower = model.lower()
    model_normalized = model_lower.replace("-", "_")
    model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
    normalized_prefix = model_prefix.replace("-", "_")

    # Prefer explicit provider prefix.
    for spec in PROVIDERS:
        if model_prefix and normalized_prefix == spec.name:
            return spec

    for spec in PROVIDERS:
        if any(kw in model_lower or kw.replace("-", "_") in model_normalized for kw in spec.keywords):
            return spec
    return None


def find_by_name(name: str) -> ProviderSpec | None:
    """Find a provider spec by config field name, e.g. "openai"."""
    for spec in PROVIDERS:
        if spec.name == name:
            return spec
    return None
