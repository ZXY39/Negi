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
    family_ids: set[str] = field(default_factory=set)
    executor_names: set[str] = field(default_factory=set)
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


def family_executor_names(family: Any) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for action in list(getattr(family, "actions", []) or []):
        for executor_name in list(getattr(action, "executor_names", []) or []):
            text = str(executor_name or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            names.append(text)
    return names


def resolve_core_tool_targets(
    core_tools: Sequence[str] | set[str] | None,
    families: Sequence[Any] | None,
) -> CoreToolResolution:
    family_ids = {
        str(getattr(family, "tool_id", "") or "").strip(): family
        for family in list(families or [])
        if str(getattr(family, "tool_id", "") or "").strip()
    }
    executor_to_family_ids: dict[str, set[str]] = {}
    family_to_executor_names: dict[str, list[str]] = {}
    for tool_id, family in family_ids.items():
        executor_names = family_executor_names(family)
        family_to_executor_names[tool_id] = executor_names
        for executor_name in executor_names:
            executor_to_family_ids.setdefault(executor_name, set()).add(tool_id)

    resolved = CoreToolResolution()
    for entry in _normalize_items(core_tools):
        matched = False
        family = family_ids.get(entry)
        if family is not None:
            matched = True
            resolved.family_ids.add(entry)
            resolved.executor_names.update(family_to_executor_names.get(entry, []))
        family_names = executor_to_family_ids.get(entry)
        if family_names:
            matched = True
            resolved.executor_names.add(entry)
            resolved.family_ids.update(family_names)
        if not matched:
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
