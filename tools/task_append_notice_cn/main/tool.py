from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool


_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


def _runtime_payload(runtime: dict[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    if isinstance(runtime, dict):
        return runtime
    fallback = kwargs.get('__g3ku_runtime')
    return fallback if isinstance(fallback, dict) else {}


class _TaskAppendNoticeHandler(Tool):
    def __init__(self, service) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return str(_MANIFEST.get('name') or 'task_append_notice')

    @property
    def description(self) -> str:
        return str(_MANIFEST.get('description') or '')

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(_MANIFEST.get('parameters') or {'type': 'object', 'properties': {}, 'required': []})

    async def execute(
        self,
        task_ids: list[str] | None = None,
        node_ids: list[str] | None = None,
        message: str = '',
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        runtime = _runtime_payload(__g3ku_runtime, kwargs)
        result = await self._service.task_append_notice(
            task_ids=list(task_ids or []),
            node_ids=list(node_ids or []),
            message=str(message or '').strip(),
            session_id=str(runtime.get('session_key') or 'web:shared').strip() or 'web:shared',
        )
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _TaskAppendNoticeHandler(service)
