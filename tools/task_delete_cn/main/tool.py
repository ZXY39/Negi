from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool


TASK_ID_LIST_PARAM = '\u4efb\u52a1id\u5217\u8868'
_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


def _runtime_payload(runtime: dict[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    if isinstance(runtime, dict):
        return runtime
    fallback = kwargs.get('__g3ku_runtime')
    return fallback if isinstance(fallback, dict) else {}


class _TaskDeleteHandler(Tool):
    def __init__(self, service) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return str(_MANIFEST.get('name') or 'task_delete')

    @property
    def description(self) -> str:
        return str(_MANIFEST.get('description') or '')

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(_MANIFEST.get('parameters') or {'type': 'object', 'properties': {}, 'required': []})

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = _runtime_payload(__g3ku_runtime, kwargs)
        await self._service.startup()
        normalized_mode = str(kwargs.get('mode') or '').strip().lower()
        session_id = str(runtime.get('session_key') or 'web:shared').strip() or 'web:shared'
        if normalized_mode == 'preview':
            result = self._service.task_delete_preview(
                task_ids=kwargs.get(TASK_ID_LIST_PARAM),
                session_id=session_id,
            )
        else:
            result = self._service.task_delete_confirm(
                task_ids=kwargs.get(TASK_ID_LIST_PARAM),
                confirmation_token=str(kwargs.get('confirmation_token') or '').strip(),
                session_id=session_id,
            )
            if inspect.isawaitable(result):
                result = await result
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _TaskDeleteHandler(service)
