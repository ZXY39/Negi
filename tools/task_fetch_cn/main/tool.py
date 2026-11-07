from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool

STATUS_PARAM = '状态'
LONG_TERM_PARAM = '长期'
UNREAD_PARAM = '只看未读'
SCOPE_PARAM = '查询范围'
SCOPE_GLOBAL = '全局'
_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


def _runtime_payload(runtime: dict[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    if isinstance(runtime, dict):
        return runtime
    fallback = kwargs.get('__g3ku_runtime')
    return fallback if isinstance(fallback, dict) else {}


class _TaskListHandler(Tool):
    def __init__(self, service) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return str(_MANIFEST.get('name') or 'task_list')

    @property
    def description(self) -> str:
        return str(_MANIFEST.get('description') or '')

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(_MANIFEST.get('parameters') or {'type': 'object', 'properties': {}, 'required': []})

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = _runtime_payload(__g3ku_runtime, kwargs)
        await self._service.startup()
        # 查询范围：全局=None（跨会话全量），缺省=当前会话
        scope = str(kwargs.get(SCOPE_PARAM) or '').strip()
        session_key = None if scope == SCOPE_GLOBAL else str(runtime.get('session_key') or 'web:shared')
        long_term = kwargs.get(LONG_TERM_PARAM)
        return self._service.get_tasks(
            session_key,
            status=str(kwargs.get(STATUS_PARAM) or '全部').strip(),
            long_term=None if long_term is None else bool(long_term),
            unread_only=bool(kwargs.get(UNREAD_PARAM)),
        )


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _TaskListHandler(service)
