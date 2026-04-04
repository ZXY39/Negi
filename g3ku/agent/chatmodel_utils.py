"""Utilities for normalizing model providers to BaseChatModel."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from g3ku.providers.base_chat_model_adapter import G3kuChatModelAdapter


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
        return G3kuChatModelAdapter(
            chat_backend=provider,
            default_model=default_model,
            default_temperature=default_temperature,
            default_max_tokens=default_max_tokens,
            default_reasoning_effort=default_reasoning_effort,
        )

    raise TypeError(
        "Unsupported provider object for runtime. Expected BaseChatModel or legacy provider with .chat()."
    )

