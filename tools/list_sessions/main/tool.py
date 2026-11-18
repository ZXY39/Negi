from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool

_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


def _runtime_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
    runtime = kwargs.get('__g3ku_runtime')
    return runtime if isinstance(runtime, dict) else {}


class _ListSessionsHandler(Tool):
    def __init__(self, service) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return str(_MANIFEST.get('name') or 'list_sessions')

    @property
    def description(self) -> str:
        return str(_MANIFEST.get('description') or '')

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(_MANIFEST.get('parameters') or {'type': 'object', 'properties': {}, 'required': []})

    async def execute(self, **kwargs: Any) -> str:
        from g3ku.runtime.frontdoor.tool_contract import frontdoor_reachable_session_roster
        from g3ku.runtime.session_trust import session_trust

        runtime = _runtime_payload(kwargs)
        session_key = str(runtime.get('session_key') or runtime.get('session_id') or '').strip()
        await self._service.startup()
        entries = frontdoor_reachable_session_roster(
            SimpleNamespace(main_task_service=self._service), session_key=session_key
        )
        sessions = [
            {
                'alias': str(item.get('alias') or ''),
                'title': str(item.get('title') or ''),
                'trust': session_trust(item.get('session_key')),
            }
            for item in entries
        ]
        result: dict[str, Any] = {'ok': True, 'sessions': sessions, 'count': len(sessions)}
        if not sessions:
            result['notice'] = (
                'No linked sessions yet. The operator manages session links from the '
                'session menu ("关联其他会话").'
            )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _ListSessionsHandler(service)
