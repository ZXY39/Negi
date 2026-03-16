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


def resolve_embedding_target(config, model_key: str, *, workspace: Path | None = None) -> RuntimeTarget:
    target = LLMConfigFacade(workspace or config.workspace_path).resolve_target(config, model_key)
    if target.capability != Capability.EMBEDDING:
        raise ValueError(f"Model key {model_key} is not configured for embedding capability")
    return target


def resolve_rerank_target(config, model_key: str, *, workspace: Path | None = None) -> RuntimeTarget:
    target = LLMConfigFacade(workspace or config.workspace_path).resolve_target(config, model_key)
    if target.capability != Capability.RERANK:
        raise ValueError(f"Model key {model_key} is not configured for rerank capability")
    return target


def resolve_memory_embedding_target(*, workspace: Path) -> RuntimeTarget:
    target = LLMConfigFacade(workspace).resolve_memory_target("embedding")
    if target.capability != Capability.EMBEDDING:
        raise ValueError("Memory embedding config is not configured for embedding capability")
    return target


def resolve_memory_rerank_target(*, workspace: Path) -> RuntimeTarget:
    target = LLMConfigFacade(workspace).resolve_memory_target("rerank")
    if target.capability != Capability.RERANK:
        raise ValueError("Memory rerank config is not configured for rerank capability")
    return target

