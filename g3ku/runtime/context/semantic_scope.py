from __future__ import annotations

from typing import Any


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


def _visible_tool_ids(visible_tools: list[Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for record in list(visible_tools or []):
        tool_id = str(_item_value(record, 'tool_id') or '').strip()
        if not tool_id or tool_id in seen:
            continue
        seen.add(tool_id)
        ordered.append(tool_id)
    return ordered


async def semantic_catalog_rankings(
    *,
    loop: Any | None = None,
    memory_manager: Any | None,
    query_text: str,
    visible_skills: list[Any],
    visible_tools: list[Any],
    skill_limit: int,
    tool_limit: int,
) -> dict[str, Any]:
    """Catalog narrowing is unavailable; callers fall back to visible inventories."""
    del loop, memory_manager, query_text, visible_skills, visible_tools, skill_limit, tool_limit
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
