from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool


TASK_KEYWORDS_PARAM = '\u4efb\u52a1\u5173\u952e\u8bcd'
TASK_ID_LIST_PARAM = '\u4efb\u52a1id\u5217\u8868'
_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


class _TaskStatsHandler(Tool):
    def __init__(self, service) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return str(_MANIFEST.get('name') or 'task_stats')

    @property
    def description(self) -> str:
        return str(_MANIFEST.get('description') or '')

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(_MANIFEST.get('parameters') or {'type': 'object', 'properties': {}, 'required': []})

    async def execute(self, **kwargs: Any) -> str:
        await self._service.startup()
        result = self._service.task_stats(
            mode=str(kwargs.get('mode') or '').strip(),
            task_keywords=kwargs.get(TASK_KEYWORDS_PARAM),
            date_from=str(kwargs.get('from') or '').strip(),
            date_to=str(kwargs.get('to') or '').strip(),
            task_ids=kwargs.get(TASK_ID_LIST_PARAM),
        )
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _TaskStatsHandler(service)
