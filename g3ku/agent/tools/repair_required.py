from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from g3ku.agent.tools.base import Tool


class RepairRequiredTool(Tool):
    def __init__(self, descriptor, *, reason: str = '') -> None:
        self._descriptor = descriptor
        self._reason = str(reason or '').strip() or 'resource_unavailable'

    @property
    def name(self) -> str:
        return str(self._descriptor.name)

    @property
    def description(self) -> str:
        base = str(getattr(self._descriptor, 'description', '') or self.name).strip() or self.name
        return f'【待修复】{base} This tool is registered but currently unavailable. Call it to receive repair guidance before retrying the real operation.'

    @property
    def parameters(self) -> dict[str, Any]:
        schema = deepcopy(getattr(self._descriptor, 'parameters', None) or {'type': 'object', 'properties': {}, 'required': []})
        properties = dict(schema.get('properties') or {})
        properties['_g3ku_tool_state'] = {
            'type': 'string',
            'enum': ['repair_required'],
            'description': '【待修复】Read-only marker that indicates this tool must be repaired before the real capability can run.',
        }
        schema['type'] = 'object'
        schema['properties'] = properties
        schema['required'] = []
        return schema

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        _ = params
        return []

    async def execute(self, **kwargs: Any) -> str:
        argument_preview = {k: v for k, v in dict(kwargs or {}).items() if k != '_g3ku_tool_state'}
        return json.dumps(
            {
                'ok': False,
                'repair_required': True,
                'tool_id': self.name,
                'tool_state': 'repair_required',
                'reason': self._reason,
                'warnings': list(getattr(self._descriptor, 'warnings', []) or []),
                'errors': list(getattr(self._descriptor, 'errors', []) or []),
                'message': f'Tool "{self.name}" is registered but requires repair before it can be used.',
                'next_actions': [
                    f'load_tool_context(tool_id="{self.name}")',
                    f'Use $repair-tool to repair "{self.name}" before retrying it.',
                ],
                'argument_preview': argument_preview,
                'metadata': {
                    'tool_type': str(getattr(self._descriptor, 'tool_type', 'internal') or 'internal'),
                    'install_dir': str(getattr(self._descriptor, 'install_dir', '') or '') or None,
                },
            },
            ensure_ascii=False,
        )
