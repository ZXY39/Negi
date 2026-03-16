from __future__ import annotations

import json
from typing import Any, Callable

from g3ku.agent.tools.base import Tool


def _runtime_session_key(runtime: dict[str, Any] | None) -> str:
    payload = runtime if isinstance(runtime, dict) else {}
    return str(payload.get('session_key') or 'web:shared').strip() or 'web:shared'


class _ProjectServiceTool(Tool):
    def __init__(self, service_getter: Callable[[], Any]):
        self._service_getter = service_getter

    async def _service(self):
        service = self._service_getter()
        await service.startup()
        return service


class LoadSkillContextTool(_ProjectServiceTool):
    @property
    def name(self) -> str:
        return 'load_skill_context'

    @property
    def description(self) -> str:
        return 'Load the detailed body of a currently visible skill so the 主Agent can use it.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'skill_id': {'type': 'string', 'description': 'The skill id to load.'},
            },
            'required': ['skill_id'],
        }

    async def execute(self, skill_id: str, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        service = await self._service()
        session_id = _runtime_session_key(__g3ku_runtime)
        visible = {item.skill_id: item for item in service.list_visible_skill_resources(actor_role='ceo', session_id=session_id)}
        record = visible.get(str(skill_id or '').strip())
        if record is None:
            return json.dumps({'ok': False, 'error': f'Skill not visible for 主Agent: {skill_id}'}, ensure_ascii=False)
        content = ''
        if record.skill_doc_path:
            content = __import__('pathlib').Path(record.skill_doc_path).read_text(encoding='utf-8')
        return json.dumps({'ok': True, 'skill_id': record.skill_id, 'content': content}, ensure_ascii=False)


class LoadToolContextTool(_ProjectServiceTool):
    @property
    def name(self) -> str:
        return 'load_tool_context'

    @property
    def description(self) -> str:
        return 'Load the detailed usage guide for a currently visible tool or registered external tool so the 主Agent can install, update, or use it correctly.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'tool_id': {'type': 'string', 'description': 'The tool id to load.'},
            },
            'required': ['tool_id'],
        }

    async def execute(self, tool_id: str, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        service = await self._service()
        session_id = _runtime_session_key(__g3ku_runtime)
        payload = service.load_tool_context(actor_role='ceo', session_id=session_id, tool_id=str(tool_id or '').strip())
        return json.dumps(payload, ensure_ascii=False)
