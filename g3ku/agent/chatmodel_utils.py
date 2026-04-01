"""Utilities for normalizing model providers to BaseChatModel."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel


def ensure_chat_model(
    provider: Any,
    *,
    default_model: str,
    default_temperature: float | None,
    default_max_tokens: int | None,
    default_reasoning_effort: str | None,
) -> BaseChatModel:
    """Coerce legacy g3ku providers into BaseChatModel when needed."""
    if isinstance(provider, BaseChatModel):
        return provider

    if hasattr(provider, "chat"):
        try:
            from g3ku.integrations.langchain_runtime import ProviderChatModelAdapter
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency fallback
            raise RuntimeError(
                "LangChain runtime dependencies are missing. Install langchain to run the agent loop."
            ) from exc
        return ProviderChatModelAdapter(
            provider=provider,
            default_model=default_model,
            default_temperature=default_temperature,
            default_max_tokens=default_max_tokens,
            default_reasoning_effort=default_reasoning_effort,
        )

    raise TypeError(
        "Unsupported provider object for runtime. Expected BaseChatModel or legacy provider with .chat()."
    )

