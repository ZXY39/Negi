from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ExecutionToolSelectionResult:
    lightweight_tool_ids: list[str]
    hydrated_tool_names: list[str]
    schema_chars: int
    trace: dict[str, Any] = field(default_factory=dict)


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _family_executor_names(family: Any) -> list[str]:
    executor_names: list[str] = []
    for action in list(_field(family, "actions") or []):
        for raw_name in list(_field(action, "executor_names") or []):
            name = str(raw_name or "").strip()
            if name and name not in executor_names:
                executor_names.append(name)
    return executor_names


def _preferred_family_executor_names(family: Any) -> list[str]:
    tool_id = str(_field(family, "tool_id") or "").strip()
    primary_executor_name = str(_field(family, "primary_executor_name") or "").strip()
    split_prefixes = {
        prefix
        for prefix in (
            f"{tool_id}_",
            "filesystem_" if tool_id == "filesystem" else "",
            "content_" if tool_id == "content_navigation" else "",
        )
        if prefix
    }
    ranked: list[tuple[tuple[int, int, str], str]] = []
    for index, name in enumerate(_family_executor_names(family)):
        is_primary = bool(primary_executor_name) and name == primary_executor_name
        is_legacy_monolith = bool(tool_id) and name == tool_id
        is_split_executor = any(name.startswith(prefix) for prefix in split_prefixes)
        rank = (
            0 if is_primary else 1,
            0 if is_split_executor and not is_legacy_monolith else 1,
            1 if is_legacy_monolith else 0,
            index,
            name,
        )
        ranked.append((rank, name))
    return [name for _, name in sorted(ranked)]


def build_execution_tool_selection(
    *,
    prompt: str,
    goal: str,
    core_requirement: str,
    visible_tool_families: list[Any],
    visible_tool_names: list[str],
    schema_size_by_executor: dict[str, int],
    always_callable_tool_names: list[str],
    promoted_tool_names: list[str] | None = None,
    max_schema_chars: int,
) -> ExecutionToolSelectionResult:
    normalized_visible_names = [
        str(name or "").strip()
        for name in list(visible_tool_names or [])
        if str(name or "").strip()
    ]
    hydrated: list[str] = []
    seen: set[str] = set()
    visible_name_set = set(normalized_visible_names)
    budget = max(0, int(max_schema_chars or 0))
    used = 0
    optional_candidates: list[str] = []
    selected_promoted_tool_names: list[str] = []
    family_by_tool_id: dict[str, Any] = {}
    for family in list(visible_tool_families or []):
        tool_id = str(_field(family, "tool_id") or "").strip()
        if tool_id and tool_id not in family_by_tool_id:
            family_by_tool_id[tool_id] = family

    def _preferred_promoted_tool_name(name: str) -> str:
        family = family_by_tool_id.get(name)
        if family is None:
            return name
        for preferred_name in _preferred_family_executor_names(family):
            if preferred_name in visible_name_set:
                return preferred_name
        return name

    for tool_name in list(always_callable_tool_names or []):
        normalized = str(tool_name or "").strip()
        if not normalized or normalized in seen or normalized not in normalized_visible_names:
            continue
        size = max(0, int(schema_size_by_executor.get(normalized, 0) or 0))
        hydrated.append(normalized)
        seen.add(normalized)
        used += size

    lightweight_tool_ids: list[str] = []
    optional_hydration_enabled = budget > 0
    promoted_candidates: list[str] = []
    if optional_hydration_enabled:
        for raw_name in list(promoted_tool_names or []):
            normalized_name = str(raw_name or "").strip()
            if not normalized_name:
                continue
            preferred_name = _preferred_promoted_tool_name(normalized_name)
            if preferred_name not in visible_name_set or preferred_name in promoted_candidates:
                continue
            promoted_candidates.append(preferred_name)
        for promoted_name in promoted_candidates:
            if promoted_name in seen:
                continue
            optional_candidates.append(promoted_name)
            size = max(0, int(schema_size_by_executor.get(promoted_name, 0) or 0))
            if used + size > budget:
                continue
            hydrated.append(promoted_name)
            seen.add(promoted_name)
            used += size
            selected_promoted_tool_names.append(promoted_name)

    for family in list(visible_tool_families or []):
        tool_id = str(_field(family, "tool_id") or "").strip()
        if tool_id and tool_id not in lightweight_tool_ids:
            lightweight_tool_ids.append(tool_id)
        if not optional_hydration_enabled:
            continue
        preferred_executor_names = _preferred_family_executor_names(family)
        if any(name in seen for name in preferred_executor_names):
            continue
        for executor in preferred_executor_names:
            if not executor or executor in seen or executor not in normalized_visible_names:
                continue
            optional_candidates.append(executor)
            size = max(0, int(schema_size_by_executor.get(executor, 0) or 0))
            if used + size > budget:
                continue
            hydrated.append(executor)
            seen.add(executor)
            used += size
            break

    return ExecutionToolSelectionResult(
        lightweight_tool_ids=lightweight_tool_ids,
        hydrated_tool_names=hydrated,
        schema_chars=used,
        trace={
            "prompt": str(prompt or ""),
            "goal": str(goal or ""),
            "core_requirement": str(core_requirement or ""),
            "budget": budget,
            "optional_hydration_enabled": optional_hydration_enabled,
            "always_callable_tool_names": list(always_callable_tool_names or []),
            "promoted_tool_names": list(promoted_tool_names or []),
            "promoted_candidates": promoted_candidates,
            "selected_promoted_tool_names": selected_promoted_tool_names,
            "optional_candidates": optional_candidates,
            "selected_optional_tool_names": [
                name for name in hydrated if name not in list(always_callable_tool_names or [])
            ],
        },
    )
