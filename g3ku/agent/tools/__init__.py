"""Agent tools module."""

from __future__ import annotations

__all__ = ["Tool", "ToolRegistry"]


def __getattr__(name: str):
    if name == "Tool":
        from g3ku.agent.tools.base import Tool

        return Tool
    if name == "ToolRegistry":
        from g3ku.agent.tools.registry import ToolRegistry

        return ToolRegistry
    raise AttributeError(f"module 'g3ku.agent.tools' has no attribute {name!r}")
