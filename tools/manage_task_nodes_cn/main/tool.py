from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool


_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


class _ManageTaskNodesHandler(Tool):
    def __init__(self, service) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return str(_MANIFEST.get('name') or 'manage_task_nodes')

    @property
    def description(self) -> str:
        return str(_MANIFEST.get('description') or '')

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(_MANIFEST.get('parameters') or {'type': 'object', 'properties': {}, 'required': []})

    async def execute(
        self,
        task_id: str,
        node_ids: list[str],
        action: str,
        remark: str = '',
        **_: Any,
    ) -> str:
        result = await self._service.control_nodes(
            str(task_id or '').strip(),
            [str(item or '').strip() for item in list(node_ids or []) if str(item or '').strip()],
            str(action or '').strip().lower(),
            remark=str(remark or ''),
        )
        return json.dumps(result, ensure_ascii=False)


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _ManageTaskNodesHandler(service)
