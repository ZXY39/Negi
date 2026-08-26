"""Factory for building LangChain BaseChatModel instances from g3ku config."""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from g3ku.config.schema import Config
from g3ku.providers.fallback import FallbackProvider
from g3ku.providers.base_chat_model_adapter import G3kuChatModelAdapter


def build_chat_model(
    config: Config,
    *,
    role: str | None = None,
    model_key: str | None = None,
) -> BaseChatModel:
    """Build a BaseChatModel for a configured runtime role or explicit model key."""
    role_name = str(role or "").strip()
    direct_model_key = str(model_key or "").strip()
    if bool(role_name) == bool(direct_model_key):
        raise ValueError("build_chat_model requires exactly one of role or model_key")

    if direct_model_key:
        default_key = direct_model_key
        chain = [direct_model_key]
    else:
        chain = [target.model_key for target in config.get_scope_model_chain(role_name)]
        default_key = str(chain[0] if chain else config.resolve_role_model_key(role_name)).strip()
    if not default_key:
        target_label = f"role '{role_name}'" if role_name else f"model_key '{direct_model_key}'"
        raise ValueError(f"No model configured for {target_label}.")

    provider = FallbackProvider(config=config, model_chain=chain or [default_key], default_model_ref=default_key)
    return G3kuChatModelAdapter(
        chat_backend=provider,
        default_model=default_key,
        default_temperature=None,
        default_max_tokens=None,
        default_reasoning_effort=None,
    )
