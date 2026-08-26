from __future__ import annotations

from typing import Any


CATALOG_NAMESPACE: tuple[str, ...] = ("catalog", "global")


def _item_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _visible_skill_ids(visible_skills: list[Any]) -> list[str]:
    return [
        str(_item_value(item, 'skill_id') or '').strip()
        for item in list(visible_skills or [])
        if str(_item_value(item, 'skill_id') or '').strip()
    ]


def _visible_tool_ids(visible_families: list[Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for family in list(visible_families or []):
        executor_names: list[str] = []
        for action in list(_item_value(family, 'actions') or []):
            for raw_name in list(_item_value(action, 'executor_names') or []):
                name = str(raw_name or '').strip()
                if name and name not in executor_names:
                    executor_names.append(name)
        tool_id = str(_item_value(family, 'tool_id') or '').strip()
        if not executor_names and tool_id:
            executor_names.append(tool_id)
        for executor_name in executor_names:
            if executor_name in seen:
                continue
            seen.add(executor_name)
            ordered.append(executor_name)
    return ordered


async def semantic_catalog_rankings(
    *,
    loop: Any | None = None,
    memory_manager: Any | None,
    query_text: str,
    visible_skills: list[Any],
    visible_families: list[Any],
    skill_limit: int,
    tool_limit: int,
) -> dict[str, Any]:
    """Catalog narrowing is unavailable; callers fall back to visible inventories."""
    del loop, memory_manager, query_text, visible_skills, visible_families, skill_limit, tool_limit
    return {
        'mode': 'unavailable',
        'available': False,
        'skill_ids': [],
        'tool_ids': [],
        'trace': {
            'queries': {},
            'dense': {'skills': [], 'tools': []},
            'rerank': {'skills': {}, 'tools': {}},
        },
    }
