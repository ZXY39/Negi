"""Factory for building LangChain BaseChatModel instances from g3ku config."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from loguru import logger

from g3ku.config.schema import Config, ProviderConfig
from g3ku.providers.fallback import FallbackProvider
from g3ku.providers.base_chat_model_adapter import G3kuChatModelAdapter
from g3ku.providers.litellm_provider import LiteLLMProvider
from g3ku.providers.openai_codex_provider import OpenAICodexProvider
from g3ku.providers.registry import ProviderSpec, find_by_name
from g3ku.providers.responses_provider import ResponsesProvider

_SPECIAL_PROVIDER_BRIDGES = {"openai_codex", "responses", "github_copilot"}


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


def _build_special_provider_bridge(
    *,
    model_key: str,
    provider_id: str,
    model_id: str,
    provider_cfg: ProviderConfig | None,
    config: Config,
) -> BaseChatModel:
    """Build controlled bridge adapters for exceptional providers."""
    logger.info("special-provider-bridge provider={} model={}", provider_id, model_id)

    if provider_id == "openai_codex":
        provider = OpenAICodexProvider(default_model=f"openai_codex/{model_id}")
    elif provider_id == "responses":
        api_key = (provider_cfg.api_key if provider_cfg else "") or ""
        if not api_key:
            raise ValueError(
                "Missing API key for providers.responses.\n"
                "Original field: providers.responses.api_key = ''\n"
                "New required field: providers.responses.api_key\n"
                "Example fix: set providers.responses.api_key and model 'responses:gpt-5.1'."
            )
        provider = ResponsesProvider(
            api_key=api_key,
            api_base=config.get_api_base(model_key) or "http://localhost:8000/v1/responses",
            default_model=model_id,
        )
    elif provider_id == "github_copilot":
        # Avoid long device-flow polling in chat runtime when OAuth cache is absent.
        if not (provider_cfg and provider_cfg.api_key):
            _validate_github_copilot_oauth_cache()
        provider = LiteLLMProvider(
            api_key=(provider_cfg.api_key or None) if provider_cfg else None,
            api_base=config.get_api_base(model_key),
            default_model=f"github_copilot/{model_id}",
            extra_headers=(provider_cfg.extra_headers if provider_cfg else None),
            provider_name="github_copilot",
        )
    else:  # pragma: no cover - defensive branch
        raise ValueError(f"Unsupported special bridge provider: {provider_id}")

    return G3kuChatModelAdapter(
        chat_backend=provider,
        default_model=provider.get_default_model(),
        default_temperature=None,
        default_max_tokens=None,
        default_reasoning_effort=None,
    )


def _validate_provider_credentials(
    spec: ProviderSpec,
    provider_id: str,
    provider_cfg: ProviderConfig | None,
    api_base: str | None,
) -> None:
    """Validate provider credentials for non-special providers."""
    api_key = (provider_cfg.api_key if provider_cfg else "") or ""

    if spec.is_oauth:
        return

    if spec.is_local:
        if not api_base and not (provider_cfg and provider_cfg.api_base):
            raise ValueError(
                f"Missing api_base for local provider '{provider_id}'.\n"
                f"Original field: providers.{provider_id}.api_base = null\n"
                f"New required field: providers.{provider_id}.api_base\n"
                "Example fix: set providers.vllm.api_base = 'http://localhost:8000/v1'."
            )
        return

    if not api_key:
        raise ValueError(
            f"Missing API key for provider '{provider_id}'.\n"
            f"Original field: providers.{provider_id}.api_key = ''\n"
            f"New required field: providers.{provider_id}.api_key\n"
            f"Example fix: set providers.{provider_id}.api_key and keep model '{provider_id}:...'."
        )


def _setup_litellm_env(
    spec: ProviderSpec,
    provider_cfg: ProviderConfig | None,
    api_base: str | None,
) -> None:
    """Mirror LiteLLM env setup behavior for community ChatLiteLLM runtime."""
    if provider_cfg is None:
        return

    api_key = provider_cfg.api_key or ""
    if spec.env_key and api_key:
        if spec.is_gateway or spec.is_local:
            os.environ[spec.env_key] = api_key
        else:
            os.environ.setdefault(spec.env_key, api_key)

    effective_base = api_base or spec.default_api_base
    for env_name, template in spec.env_extras:
        resolved = template.replace("{api_key}", api_key).replace("{api_base}", effective_base)
        if resolved:
            os.environ.setdefault(env_name, resolved)


def _resolve_litellm_model(provider_id: str, model_id: str) -> str:
    """Resolve model id into the expected LiteLLM routed model string."""
    spec = find_by_name(provider_id)
    if spec is None:
        return model_id

    resolved = model_id.strip()
    if not resolved:
        return resolved

    if "/" in resolved:
        explicit_prefix, remainder = resolved.split("/", 1)
        if explicit_prefix.lower().replace("-", "_") == provider_id and remainder:
            resolved = remainder

    if spec.strip_model_prefix and "/" in resolved:
        resolved = resolved.split("/")[-1]

    if spec.litellm_prefix and not any(resolved.startswith(prefix) for prefix in spec.skip_prefixes):
        if not resolved.startswith(f"{spec.litellm_prefix}/"):
            resolved = f"{spec.litellm_prefix}/{resolved}"

    return resolved


def _validate_github_copilot_oauth_cache() -> None:
    """Validate LiteLLM GitHub Copilot OAuth cache files before runtime calls."""
    token_dir = Path(
        os.getenv(
            "GITHUB_COPILOT_TOKEN_DIR",
            str(Path.home() / ".config" / "litellm" / "github_copilot"),
        )
    ).expanduser()
    access_token_file = token_dir / os.getenv("GITHUB_COPILOT_ACCESS_TOKEN_FILE", "access-token")
    api_key_file = token_dir / os.getenv("GITHUB_COPILOT_API_KEY_FILE", "api-key.json")

    if not access_token_file.exists():
        raise ValueError(
            "GitHub Copilot OAuth token cache not found.\n"
            "Original provider: github_copilot\n"
            f"Missing file: {access_token_file}\n"
            "Fix: run `g3ku provider login github-copilot` first."
        )

    try:
        access_token = access_token_file.read_text(encoding="utf-8").strip()
    except Exception as exc:
        raise ValueError(
            "GitHub Copilot OAuth cache is unreadable.\n"
            "Original provider: github_copilot\n"
            f"File: {access_token_file}\n"
            "Fix: run `g3ku provider login github-copilot` to refresh credentials."
        ) from exc

    if not access_token:
        raise ValueError(
            "GitHub Copilot OAuth access token is empty.\n"
            "Original provider: github_copilot\n"
            f"File: {access_token_file}\n"
            "Fix: run `g3ku provider login github-copilot`."
        )

    if not api_key_file.exists():
        raise ValueError(
            "GitHub Copilot API key cache not found.\n"
            "Original provider: github_copilot\n"
            f"Missing file: {api_key_file}\n"
            "Fix: run `g3ku provider login github-copilot` first."
        )

    try:
        payload = json.loads(api_key_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(
            "GitHub Copilot API key cache is invalid JSON.\n"
            "Original provider: github_copilot\n"
            f"File: {api_key_file}\n"
            "Fix: run `g3ku provider login github-copilot` to refresh credentials."
        ) from exc

    token = payload.get("token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError(
            "GitHub Copilot API key cache is missing token.\n"
            "Original provider: github_copilot\n"
            f"File: {api_key_file}\n"
            "Fix: run `g3ku provider login github-copilot`."
        )

    expires_at = payload.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at <= time.time():
        raise ValueError(
            "GitHub Copilot API key cache is expired.\n"
            "Original provider: github_copilot\n"
            f"File: {api_key_file}\n"
            "Fix: run `g3ku provider login github-copilot` to refresh credentials."
        )

