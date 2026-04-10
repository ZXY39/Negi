from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from g3ku.runtime.context.summarizer import score_query


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
    legacy_monolith_names = {
        name
        for name in (
            tool_id,
            "content" if tool_id == "content_navigation" else "",
            "filesystem" if tool_id == "filesystem" else "",
        )
        if name
    }
    split_prefixes = {
        prefix
        for prefix in (
            f"{tool_id}_",
            "filesystem_" if tool_id == "filesystem" else "",
            "content_" if tool_id == "content_navigation" else "",
        )
        if prefix
    }
    split_executors_present = any(
        any(name.startswith(prefix) for prefix in split_prefixes)
        for name in _family_executor_names(family)
    )
    ranked: list[tuple[tuple[int, int, int, str], str]] = []
    for index, name in enumerate(_family_executor_names(family)):
        is_primary = bool(primary_executor_name) and name == primary_executor_name
        is_legacy_monolith = name in legacy_monolith_names
        is_split_executor = any(name.startswith(prefix) for prefix in split_prefixes)
        if split_executors_present and is_legacy_monolith:
            continue
        rank = (
            0 if is_primary else 1,
            0 if is_split_executor and not is_legacy_monolith else 1,
            1 if is_legacy_monolith else 0,
            index,
            name,
        )
        ranked.append((rank, name))
    return [name for _, name in sorted(ranked)]


def _normalized_query(*, prompt: str, goal: str, core_requirement: str) -> str:
    return " \n".join(
        [
            str(prompt or "").strip(),
            str(goal or "").strip(),
            str(core_requirement or "").strip(),
        ]
    ).strip()


def _intent_boost(*, query_text: str, tool_id: str, executor_name: str, action_id: str) -> float:
    query = str(query_text or "").lower()
    normalized_tool_id = str(tool_id or "").strip().lower()
    normalized_executor = str(executor_name or "").strip().lower()
    normalized_action = str(action_id or "").strip().lower()
    score = 0.0

    web_terms = (
        "网页",
        "网站",
        "页面",
        "搜索",
        "搜集",
        "抓取",
        "来源",
        "链接",
        "url",
        "官网",
        "榜单",
        "poll",
        "search",
        "fetch",
        "source",
        "browse",
        "web",
        "site",
    )
    memory_terms = (
        "记忆",
        "memory",
        "偏好",
        "长期",
        "事实",
        "remember",
        "durable",
        "history",
    )
    if any(term in query for term in web_terms):
        if normalized_tool_id == "web_fetch" or normalized_executor == "web_fetch":
            score += 3.0
        if normalized_tool_id == "agent_browser" or normalized_executor == "agent_browser":
            score += 2.5
        if normalized_tool_id == "content_navigation" or normalized_executor.startswith("content_"):
            score += 2.0
    if any(term in query for term in memory_terms):
        if normalized_tool_id == "memory" or normalized_executor.startswith("memory_"):
            score += 2.0
    if "search" in normalized_action and any(term in query for term in web_terms):
        score += 0.5
    return score


def build_execution_tool_selection(
    *,
    prompt: str,
    goal: str,
    core_requirement: str,
    visible_tool_families: list[Any],
    visible_tool_names: list[str],
    always_callable_tool_names: list[str],
    promoted_tool_names: list[str] | None = None,
    schema_size_by_executor: dict[str, int] | None = None,
    max_schema_chars: int | None = None,
) -> ExecutionToolSelectionResult:
    normalized_visible_names = [
        str(name or "").strip()
        for name in list(visible_tool_names or [])
        if str(name or "").strip()
    ]
    hydrated: list[str] = []
    seen: set[str] = set()
    visible_name_set = set(normalized_visible_names)
    optional_candidates: list[str] = []
    selected_promoted_tool_names: list[str] = []
    family_by_tool_id: dict[str, Any] = {}
    query_text = _normalized_query(
        prompt=prompt,
        goal=goal,
        core_requirement=core_requirement,
    )
    for family in list(visible_tool_families or []):
        tool_id = str(_field(family, "tool_id") or "").strip()
        if tool_id and tool_id not in family_by_tool_id:
            family_by_tool_id[tool_id] = family
        if tool_id == "content_navigation" and "content" not in family_by_tool_id:
            family_by_tool_id["content"] = family

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
        hydrated.append(normalized)
        seen.add(normalized)

    lightweight_tool_ids: list[str] = []
    promoted_candidates: list[str] = []
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
        hydrated.append(promoted_name)
        seen.add(promoted_name)
        selected_promoted_tool_names.append(promoted_name)

    candidate_executor_scores: list[dict[str, Any]] = []
    stable_tiebreak_order: list[str] = []
    for family in list(visible_tool_families or []):
        tool_id = str(_field(family, "tool_id") or "").strip()
        if tool_id and tool_id not in lightweight_tool_ids:
            lightweight_tool_ids.append(tool_id)
        preferred_executor_names = _preferred_family_executor_names(family)
        display_name = str(_field(family, "display_name") or tool_id).strip()
        description = str(_field(family, "description") or "").strip()
        l0 = str(_field(family, "l0") or "").strip()
        l1 = str(_field(family, "l1") or "").strip()
        action_map: dict[str, str] = {}
        for action in list(_field(family, "actions") or []):
            action_id = str(_field(action, "action_id") or "").strip()
            for executor_name in list(_field(action, "executor_names") or []):
                normalized_executor = str(executor_name or "").strip()
                if normalized_executor and normalized_executor not in action_map:
                    action_map[normalized_executor] = action_id
        for stable_index, executor in enumerate(preferred_executor_names):
            if not executor or executor in seen or executor not in normalized_visible_names:
                continue
            optional_candidates.append(executor)
            stable_tiebreak_order.append(executor)
            action_id = action_map.get(executor, "")
            semantic_score = score_query(
                query_text,
                tool_id,
                display_name,
                description,
                l0,
                l1,
                action_id,
                executor,
            )
            boosted_score = semantic_score + _intent_boost(
                query_text=query_text,
                tool_id=tool_id,
                executor_name=executor,
                action_id=action_id,
            )
            candidate_executor_scores.append(
                {
                    "tool_id": tool_id,
                    "executor_name": executor,
                    "action_id": action_id,
                    "semantic_score": float(semantic_score),
                    "boosted_score": float(boosted_score),
                    "stable_index": len(stable_tiebreak_order) - 1,
                }
            )

    scored_candidates = sorted(
        candidate_executor_scores,
        key=lambda item: (
            -float(item.get("boosted_score") or 0.0),
            int(item.get("stable_index") or 0),
            str(item.get("executor_name") or ""),
        ),
    )
    selected_executor_scores: list[dict[str, Any]] = []
    for item in scored_candidates:
        executor = str(item.get("executor_name") or "").strip()
        if not executor or executor in seen:
            continue
        hydrated.append(executor)
        seen.add(executor)
        selected_executor_scores.append(dict(item))

    return ExecutionToolSelectionResult(
        lightweight_tool_ids=lightweight_tool_ids,
        hydrated_tool_names=hydrated,
        schema_chars=sum(
            max(0, int((schema_size_by_executor or {}).get(name, 0) or 0))
            for name in hydrated
        ),
        trace={
            "prompt": str(prompt or ""),
            "goal": str(goal or ""),
            "core_requirement": str(core_requirement or ""),
            "always_callable_tool_names": list(always_callable_tool_names or []),
            "promoted_tool_names": list(promoted_tool_names or []),
            "promoted_candidates": promoted_candidates,
            "selected_promoted_tool_names": selected_promoted_tool_names,
            "optional_candidates": optional_candidates,
            "selected_optional_tool_names": [
                name
                for name in hydrated
                if name not in list(always_callable_tool_names or [])
                and name not in selected_promoted_tool_names
            ],
            "candidate_executor_scores": candidate_executor_scores,
            "selected_executor_scores": selected_executor_scores,
            "stable_tiebreak_order": stable_tiebreak_order,
        },
    )
