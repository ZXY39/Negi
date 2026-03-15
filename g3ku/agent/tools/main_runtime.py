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
        return 'Load layered context for a currently visible skill. Defaults to L1 overview; use level=l2 for query-aware excerpts.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'skill_id': {'type': 'string', 'description': 'The skill id to load.'},
                'level': {'type': 'string', 'enum': ['l0', 'l1', 'l2'], 'description': 'Requested context level. Defaults to l1.'},
                'query': {'type': 'string', 'description': 'Optional query used to extract a focused L2 excerpt.'},
                'max_tokens': {'type': 'integer', 'description': 'Optional output token budget for the returned content.'},
            },
            'required': ['skill_id'],
        }

    async def execute(
        self,
        skill_id: str,
        level: str = 'l1',
        query: str = '',
        max_tokens: int | None = None,
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        service = await self._service()
        if hasattr(service, 'load_skill_context_v2'):
            payload = service.load_skill_context_v2(
                actor_role=_runtime_actor_role(__g3ku_runtime),
                session_id=_runtime_session_key(__g3ku_runtime),
                skill_id=str(skill_id or '').strip(),
                level=str(level or 'l1').strip().lower() or 'l1',
                query=str(query or ''),
                max_tokens=max_tokens,
            )
        else:
            payload = service.load_skill_context(
                actor_role=_runtime_actor_role(__g3ku_runtime),
                session_id=_runtime_session_key(__g3ku_runtime),
                skill_id=str(skill_id or '').strip(),
            )
        return json.dumps(payload, ensure_ascii=False)


class LoadToolContextTool(_MainRuntimeTool):
    @property
    def name(self) -> str:
        return 'load_tool_context'

    @property
    def description(self) -> str:
        return 'Load layered context for a currently visible tool. Defaults to L1 overview; use level=l2 for query-aware excerpts.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'tool_id': {'type': 'string', 'description': 'The tool id to load.'},
                'level': {'type': 'string', 'enum': ['l0', 'l1', 'l2'], 'description': 'Requested context level. Defaults to l1.'},
                'query': {'type': 'string', 'description': 'Optional query used to extract a focused L2 excerpt.'},
                'max_tokens': {'type': 'integer', 'description': 'Optional output token budget for the returned content.'},
            },
            'required': ['tool_id'],
        }

    async def execute(
        self,
        tool_id: str,
        level: str = 'l1',
        query: str = '',
        max_tokens: int | None = None,
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        service = await self._service()
        if hasattr(service, 'load_tool_context_v2'):
            payload = service.load_tool_context_v2(
                actor_role=_runtime_actor_role(__g3ku_runtime),
                session_id=_runtime_session_key(__g3ku_runtime),
                tool_id=str(tool_id or '').strip(),
                level=str(level or 'l1').strip().lower() or 'l1',
                query=str(query or ''),
                max_tokens=max_tokens,
            )
        else:
            payload = service.load_tool_context(
                actor_role=_runtime_actor_role(__g3ku_runtime),
                session_id=_runtime_session_key(__g3ku_runtime),
                tool_id=str(tool_id or '').strip(),
            )
        return json.dumps(payload, ensure_ascii=False)
