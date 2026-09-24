from __future__ import annotations

import asyncio
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
        # 读 24h 窗口要解析几千行 JSON（实测 ~100ms），与 rest.py 把 worker-status
        # 查询卸载到线程池同一理由：同步跑会把事件循环按住，连带饿死轻读请求。
        return await asyncio.to_thread(
            self._service.perf_report,
            mode=mode,
            window_minutes=window_minutes,
        )


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _PerfInspectHandler(service)
