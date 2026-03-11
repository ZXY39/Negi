"""Legacy compatibility namespace for one-release bridge imports."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["AgentLoop", "LangGraphAgentLoop", "LegacyAgentSession"]


def __getattr__(name: str) -> Any:
    if name == "AgentLoop":
        return import_module("g3ku.agent.loop").AgentLoop
    if name == "LangGraphAgentLoop":
        return import_module("g3ku.agent.langgraph_loop").LangGraphAgentLoop
    if name == "LegacyAgentSession":
        return import_module("g3ku.runtime.session_runtime").LegacyAgentSession
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
