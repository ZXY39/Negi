from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool

_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))

_ROW_LIMIT = 50


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
        from g3ku.runtime.session_link_policy import linkable_session_candidates, may_auto_link
        from g3ku.runtime.session_trust import session_trust

        runtime = _runtime_payload(kwargs)
        session_key = str(runtime.get('session_key') or runtime.get('session_id') or '').strip()
        await self._service.startup()
        entries = frontdoor_reachable_session_roster(
            SimpleNamespace(main_task_service=self._service), session_key=session_key
        )
        sessions: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in entries:
            key = str(item.get('session_key') or '')
            if not key or key in seen:
                continue
            seen.add(key)
            sessions.append(
                {
                    'short_id': str(item.get('short_id') or ''),
                    'title': str(item.get('title') or ''),
                    'trust': session_trust(key),
                    'linked': True,
                }
            )
        # owner 档才给未关联候选：懒建边要先让模型拿得到 id，而第三方会话
        # 不该凭这份目录看见操作员有哪些会话。
        directory = may_auto_link(session_key)
        truncated = False
        if directory:
            candidates, truncated = linkable_session_candidates(session_key, limit=_ROW_LIMIT)
            for candidate in candidates:
                key = str(candidate.get('session_key') or '')
                if not key or key in seen:
                    continue
                if len(sessions) >= _ROW_LIMIT:
                    truncated = True
                    break
                seen.add(key)
                sessions.append(
                    {
                        'short_id': str(self._service.session_short_id(key) or ''),
                        'title': str(candidate.get('title') or ''),
                        'trust': session_trust(key),
                        'linked': False,
                    }
                )
        sessions = [row for row in sessions if row['short_id']]
        result: dict[str, Any] = {
            'ok': True,
            'sessions': sessions[:_ROW_LIMIT],
            'count': min(len(sessions), _ROW_LIMIT),
            'truncated': bool(truncated or len(sessions) > _ROW_LIMIT),
        }
        if not sessions:
            result['notice'] = (
                'No other sessions exist yet.'
                if directory
                else 'No linked sessions yet. The operator manages session links from the '
                'session menu ("关联其他会话").'
            )
        elif any(not row['linked'] for row in sessions):
            result['notice'] = (
                'Rows with linked=false are not linked yet; sending to their short_id links '
                'them automatically.'
            )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _ListSessionsHandler(service)
