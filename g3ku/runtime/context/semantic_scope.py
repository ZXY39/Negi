from __future__ import annotations

from typing import Any


CATALOG_NAMESPACE: tuple[str, ...] = ("catalog", "global")


def _visible_skill_ids(visible_skills: list[Any]) -> list[str]:
    return [
        str(getattr(item, 'skill_id', '') or '').strip()
        for item in list(visible_skills or [])
        if str(getattr(item, 'skill_id', '') or '').strip()
    ]


def _visible_tool_ids(visible_families: list[Any]) -> list[str]:
    return [
        str(getattr(item, 'tool_id', '') or '').strip()
        for item in list(visible_families or [])
        if str(getattr(item, 'tool_id', '') or '').strip()
    ]


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
    memory_manager: Any | None,
    query_text: str,
    visible_skills: list[Any],
    visible_families: list[Any],
    skill_limit: int,
    tool_limit: int,
) -> dict[str, Any]:
    payload = {
        'skill_ids': [],
        'tool_ids': [],
        'trace': {
            'mode': 'disabled',
            'available': False,
            'skills': [],
            'tools': [],
        },
    }
    query = str(query_text or '').strip()
    if not query or memory_manager is None or not hasattr(memory_manager, 'semantic_search_context_records'):
        return payload
    if not semantic_search_enabled(memory_manager):
        payload['trace']['mode'] = 'unavailable'
        return payload

    skill_visible = set(_visible_skill_ids(visible_skills))
    tool_visible = set(_visible_tool_ids(visible_families))

    async def _search(context_type: str, limit: int) -> list[Any]:
        try:
            return list(
                await memory_manager.semantic_search_context_records(
                    namespace_prefix=CATALOG_NAMESPACE,
                    query=query,
                    limit=max(limit, 1),
                    context_type=context_type,
                )
                or []
            )
        except Exception:
            return []

    skill_records = await _search('skill', skill_limit)
    tool_records = await _search('resource', tool_limit)

    ranked_skill_ids: list[str] = []
    ranked_tool_ids: list[str] = []
    skill_seen: set[str] = set()
    tool_seen: set[str] = set()
    skill_trace: list[dict[str, Any]] = []
    tool_trace: list[dict[str, Any]] = []

    for index, record in enumerate(skill_records, start=1):
        record_id = str(getattr(record, 'record_id', '') or '').strip()
        skill_id = catalog_record_suffix(record_id, prefix='skill:')
        if not skill_id or skill_id not in skill_visible or skill_id in skill_seen:
            continue
        skill_seen.add(skill_id)
        ranked_skill_ids.append(skill_id)
        skill_trace.append({'record_id': record_id, 'skill_id': skill_id, 'rank': index})

    for index, record in enumerate(tool_records, start=1):
        record_id = str(getattr(record, 'record_id', '') or '').strip()
        tool_id = catalog_record_suffix(record_id, prefix='tool:')
        if not tool_id or tool_id not in tool_visible or tool_id in tool_seen:
            continue
        tool_seen.add(tool_id)
        ranked_tool_ids.append(tool_id)
        tool_trace.append({'record_id': record_id, 'tool_id': tool_id, 'rank': index})

    return {
        'skill_ids': ranked_skill_ids,
        'tool_ids': ranked_tool_ids,
        'trace': {
            'mode': 'dense_only',
            'available': True,
            'skills': skill_trace,
            'tools': tool_trace,
        },
    }


def plan_retrieval_scope(
    *,
    visible_skills: list[Any],
    visible_families: list[Any],
    semantic_frontdoor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    semantic_trace = dict((semantic_frontdoor or {}).get('trace') or {})
    semantic_available = bool(semantic_trace.get('available', False))
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
        targeted_skill_ids = semantic_skill_ids[:3]
        targeted_tool_ids = semantic_tool_ids[:3]
        scope_mode = 'dense_only'
    else:
        targeted_skill_ids = _visible_skill_ids(visible_skills)
        targeted_tool_ids = _visible_tool_ids(visible_families)
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
