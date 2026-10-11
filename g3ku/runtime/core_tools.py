from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from g3ku.resources.tool_settings import (
    MemoryRuntimeSettings,
    raw_tool_settings_from_descriptor,
    validate_tool_settings,
)


@dataclass(slots=True)
class CoreToolResolution:
    tool_ids: set[str] = field(default_factory=set)
    unresolved_entries: list[str] = field(default_factory=list)


def _normalize_items(values: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in list(values or []):
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def resolve_core_tool_targets(
    core_tools: Sequence[str] | set[str] | None,
    tools: Sequence[Any] | None,
) -> CoreToolResolution:
    known_tool_ids = {
        str(getattr(tool, "tool_id", "") or "").strip()
        for tool in list(tools or [])
    }
    known_tool_ids.discard("")

    resolved = CoreToolResolution()
    for entry in _normalize_items(core_tools):
        if entry in known_tool_ids:
            resolved.tool_ids.add(entry)
            continue
        resolved.unresolved_entries.append(entry)
    return resolved


def configured_core_tools(*, resource_manager: Any | None = None) -> list[str]:
    descriptor = None
    if resource_manager is not None and hasattr(resource_manager, "get_tool_descriptor"):
        descriptor = resource_manager.get_tool_descriptor("memory_runtime")
    if descriptor is not None:
        try:
            settings = validate_tool_settings(
                MemoryRuntimeSettings,
                raw_tool_settings_from_descriptor(descriptor),
                tool_name="memory_runtime",
            )
            return _normalize_items(getattr(getattr(settings, "assembly", None), "core_tools", []))
        except Exception:
            pass
    return _normalize_items(MemoryRuntimeSettings().assembly.core_tools)
