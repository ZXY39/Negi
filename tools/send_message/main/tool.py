from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool

_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


def _runtime_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
    runtime = kwargs.get('__g3ku_runtime')
    return runtime if isinstance(runtime, dict) else {}


class _FrontdoorSendMessageHandler(Tool):
    def __init__(self, service) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return str(_MANIFEST.get('name') or 'send_message')

    @property
    def description(self) -> str:
        return str(_MANIFEST.get('description') or '')

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(_MANIFEST.get('parameters') or {'type': 'object', 'properties': {}, 'required': []})

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not str((params or {}).get('content') or '').strip():
            errors.append('content must not be empty')
        return errors

    async def execute(self, to: Any = None, content: str = '', ref: str = '', **kwargs: Any) -> str:
        runtime = _runtime_payload(kwargs)
        session_id = str(runtime.get('session_key') or runtime.get('session_id') or 'web:shared').strip()
        await self._service.startup()
        result = await self._service.deliver_frontdoor_node_message(
            session_id=session_id or 'web:shared',
            to=to,
            content=str(content or '').strip(),
            ref=str(ref or '').strip(),
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _FrontdoorSendMessageHandler(service)
