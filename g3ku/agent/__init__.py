"""Agent core module (lazy exports to avoid heavy optional deps at import-time)."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "AgentLoop",
    "MemoryStore",
    "SkillsLoader",
]

_EXPORT_MAP = {
    "AgentLoop": ("g3ku.agent.loop", "AgentLoop"),
    "MemoryStore": ("g3ku.agent.memory", "MemoryStore"),
    "SkillsLoader": ("g3ku.agent.skills", "SkillsLoader"),
}
_SUBMODULES = {
    "loop",
    "memory",
    "skills",
    "middleware",
    "rag_memory",
    "session_commit",
}


def __getattr__(name: str) -> Any:
    if name in _SUBMODULES:
        module = import_module(f"g3ku.agent.{name}")
        globals()[name] = module
        return module
    target = _EXPORT_MAP.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attr_name = target
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value

