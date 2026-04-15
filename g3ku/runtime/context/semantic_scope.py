from __future__ import annotations

from typing import Any

from g3ku.runtime.context.frontdoor_catalog_selection import build_frontdoor_catalog_selection


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


def semantic_search_enabled(memory_manager: Any | None) -> bool:
    store = getattr(memory_manager, 'store', None) if memory_manager is not None else None
    return bool(getattr(store, '_dense_enabled', False))


def catalog_record_suffix(record_id: str, *, prefix: str) -> str:
    text = str(record_id or '').strip()
    if not text.startswith(prefix):
        return ''
    return text[len(prefix) :].strip()


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
    payload = {
        'mode': 'disabled',
        'available': False,
        'skill_ids': [],
        'tool_ids': [],
        'trace': {
            'queries': {},
            'dense': {'skills': [], 'tools': []},
            'rerank': {'skills': {}, 'tools': {}},
        },
    }
    query = str(query_text or '').strip()
    if not query or memory_manager is None or not hasattr(memory_manager, 'semantic_search_context_records'):
        return payload
    if not semantic_search_enabled(memory_manager):
        payload['mode'] = 'unavailable'
        return payload

    try:
        return await build_frontdoor_catalog_selection(
            loop=loop,
            memory_manager=memory_manager,
            query_text=query,
            visible_skills=visible_skills,
            visible_families=visible_families,
            skill_limit=skill_limit,
            tool_limit=tool_limit,
        )
    except Exception:
        return {
            **payload,
            'mode': 'error',
        }


def plan_retrieval_scope(
    *,
    visible_skills: list[Any],
    visible_families: list[Any],
    semantic_frontdoor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    semantic_trace = dict((semantic_frontdoor or {}).get('trace') or {})
    semantic_available = bool((semantic_frontdoor or {}).get('available', semantic_trace.get('available', False)))
    visible_skill_ids = _visible_skill_ids(visible_skills)
    visible_tool_ids = _visible_tool_ids(visible_families)
    semantic_skill_ids = [
        str(item or '').strip()
        for item in list((semantic_frontdoor or {}).get('skill_ids') or [])
        if str(item or '').strip()
    ]
    semantic_tool_ids = [
        str(item or '').strip()
        for item in list((semantic_frontdoor or {}).get('tool_ids') or [])
        if str(item or '').strip()
    ]

    if semantic_available:
        targeted_skill_ids = semantic_skill_ids[:3] if semantic_skill_ids else visible_skill_ids
        targeted_tool_ids = semantic_tool_ids[:3] if semantic_tool_ids else visible_tool_ids
        scope_mode = 'dense_only'
    else:
        targeted_skill_ids = visible_skill_ids
        targeted_tool_ids = visible_tool_ids
        scope_mode = 'rbac_fallback'

    search_context_types = ['memory']
    if targeted_skill_ids:
        search_context_types.append('skill')
    if targeted_tool_ids:
        search_context_types.append('resource')

    return {
        'mode': scope_mode,
        'search_context_types': search_context_types,
        'allowed_context_types': list(search_context_types),
        'allowed_resource_record_ids': [f'tool:{item}' for item in targeted_tool_ids] if targeted_tool_ids else [],
        'allowed_skill_record_ids': [f'skill:{item}' for item in targeted_skill_ids] if targeted_skill_ids else [],
    }
