from __future__ import annotations

from pathlib import Path

from .enums import Capability
from .facade import LLMConfigFacade
from .models import RuntimeTarget


def resolve_chat_target(config, model_key: str, *, workspace: Path | None = None) -> RuntimeTarget:
    target = LLMConfigFacade(workspace or config.workspace_path).resolve_target(config, model_key)
    if target.capability != Capability.CHAT:
        raise ValueError(f"Model key {model_key} is not configured for chat capability")
    return target
