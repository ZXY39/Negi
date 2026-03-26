from __future__ import annotations

import json
from typing import Any, Callable

from g3ku.agent.tools.base import Tool


def _runtime_session_key(runtime: dict[str, Any] | None) -> str:
    payload = runtime if isinstance(runtime, dict) else {}
    return str(payload.get('session_key') or 'web:shared').strip() or 'web:shared'


def _runtime_actor_role(runtime: dict[str, Any] | None) -> str:
    payload = runtime if isinstance(runtime, dict) else {}
    value = str(payload.get('actor_role') or '').strip().lower()
    return value or 'ceo'


class _MainRuntimeTool(Tool):
    def __init__(self, service_getter: Callable[[], Any]):
        self._service_getter = service_getter

    async def _service(self):
        service = self._service_getter()
        await service.startup()
        return service


class LoadSkillContextTool(_MainRuntimeTool):
    @property
    def name(self) -> str:
        return 'load_skill_context'

    @property
    def description(self) -> str:
        return 'Load full context for a currently visible skill by exact skill_id. Do not use this tool to discover or search for skills.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'skill_id': {'type': 'string', 'description': 'The exact visible skill id to load.'},
            },
            'required': ['skill_id'],
        }

    async def execute(
        self,
        skill_id: str,
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        search_text = str(kwargs.get('search_query') or '').strip()
        if search_text:
            return json.dumps(
                {
                    'ok': False,
                    'error': 'skill_search_not_allowed',
                    'message': 'load_skill_context only loads a known visible skill by exact skill_id.',
                },
                ensure_ascii=False,
            )
        service = await self._service()
        skill_name = str(skill_id or '').strip()
        if not skill_name:
            return json.dumps({'ok': False, 'error': 'skill_id_required'}, ensure_ascii=False)
        if hasattr(service, 'load_skill_context_v2'):
            kwargs_v2: dict[str, Any] = {
                'actor_role': _runtime_actor_role(__g3ku_runtime),
                'session_id': _runtime_session_key(__g3ku_runtime),
                'skill_id': skill_name,
            }
            payload = service.load_skill_context_v2(**kwargs_v2)
        else:
            kwargs_v1: dict[str, Any] = {
                'actor_role': _runtime_actor_role(__g3ku_runtime),
                'session_id': _runtime_session_key(__g3ku_runtime),
                'skill_id': skill_name,
            }
            payload = service.load_skill_context(**kwargs_v1)
        return json.dumps(payload, ensure_ascii=False)


class LoadToolContextTool(_MainRuntimeTool):
    @property
    def name(self) -> str:
        return 'load_tool_context'

    @property
    def description(self) -> str:
        return 'Load full context for a currently visible tool or registered external tool, or search visible tool candidates by natural language using search_query.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'tool_id': {'type': 'string', 'description': 'The tool id to load.'},
                'search_query': {'type': 'string', 'description': 'Optional natural-language query used to search visible tool candidates when tool_id is omitted.'},
                'limit': {'type': 'integer', 'description': 'Optional candidate limit for search mode. Defaults to 5.'},
            },
            'required': [],
        }

    async def execute(
        self,
        tool_id: str = '',
        search_query: str = '',
        limit: int | None = None,
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        service = await self._service()
        tool_name = str(tool_id or '').strip()
        search_text = str(search_query or '')
        if hasattr(service, 'load_tool_context_v2'):
            kwargs_v2: dict[str, Any] = {
                'actor_role': _runtime_actor_role(__g3ku_runtime),
                'session_id': _runtime_session_key(__g3ku_runtime),
                'tool_id': tool_name,
            }
            if search_text:
                kwargs_v2['search_query'] = search_text
                kwargs_v2['limit'] = limit
            payload = service.load_tool_context_v2(**kwargs_v2)
        else:
            kwargs_v1: dict[str, Any] = {
                'actor_role': _runtime_actor_role(__g3ku_runtime),
                'session_id': _runtime_session_key(__g3ku_runtime),
                'tool_id': tool_name,
            }
            if search_text:
                kwargs_v1['search_query'] = search_text
                kwargs_v1['limit'] = limit
            payload = service.load_tool_context(**kwargs_v1)
        return json.dumps(payload, ensure_ascii=False)
