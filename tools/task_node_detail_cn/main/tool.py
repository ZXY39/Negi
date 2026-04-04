from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool


TASK_ID_PARAM = '\u4efb\u52a1id'
NODE_ID_PARAM = '\u8282\u70b9id'
_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


class _TaskNodeDetailHandler(Tool):
    def __init__(self, service) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return str(_MANIFEST.get('name') or 'task_node_detail')

    @property
    def description(self) -> str:
        return str(_MANIFEST.get('description') or '')

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(_MANIFEST.get('parameters') or {'type': 'object', 'properties': {}, 'required': []})

    async def execute(self, **kwargs: Any) -> str:
        await self._service.startup()
        task_id = str(kwargs.get(TASK_ID_PARAM) or '').strip()
        node_id = str(kwargs.get(NODE_ID_PARAM) or '').strip()
        detail_level = str(kwargs.get('detail_level') or 'summary').strip()
        try:
            result = self._service.node_detail(task_id, node_id, detail_level=detail_level)
        except TypeError as exc:
            if "unexpected keyword argument 'detail_level'" not in str(exc):
                raise
            result = self._service.node_detail(task_id, node_id)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _TaskNodeDetailHandler(service)
