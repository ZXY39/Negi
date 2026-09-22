from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool


_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


class _PerfInspectHandler(Tool):
    def __init__(self, service) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return str(_MANIFEST.get('name') or 'perf_inspect')

    @property
    def description(self) -> str:
        return str(_MANIFEST.get('description') or '')

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(_MANIFEST.get('parameters') or {'type': 'object', 'properties': {}, 'required': []})

    async def execute(self, *, mode: str = 'window', window_minutes: Any = None, **_kwargs: Any) -> str:
        await self._service.startup()
        return self._service.perf_report(mode=mode, window_minutes=window_minutes)


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _PerfInspectHandler(service)
