from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool

_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


def _runtime_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
    runtime = kwargs.get('__g3ku_runtime')
    return runtime if isinstance(runtime, dict) else {}


def _signed_text(*, task_id: str, source_node_id: str, goal: str, content: str) -> str:
    label = f'来自节点「{goal or source_node_id}」 (node:{source_node_id}, task:{task_id})：'
    return f'{label}\n{content}' if content else label


class _FrontdoorMessageHistoryHandler(Tool):
    def __init__(self, service) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return str(_MANIFEST.get('name') or 'view_message_history')

    @property
    def description(self) -> str:
        return str(_MANIFEST.get('description') or '')

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(_MANIFEST.get('parameters') or {'type': 'object', 'properties': {}, 'required': []})

    async def execute(self, **kwargs: Any) -> str:
        runtime = _runtime_payload(kwargs)
        session_id = str(runtime.get('session_key') or runtime.get('session_id') or 'web:shared').strip()
        await self._service.startup()
        rows = self._service.list_frontdoor_node_messages(session_id)
        items: list[dict[str, Any]] = []
        for row in rows:
            node = None
            try:
                node = self._service.get_node(str(getattr(row, 'source_node_id', '') or '').strip())
            except Exception:
                node = None
            goal = str(getattr(node, 'goal', '') or '').strip() if node is not None else ''
            task_id = str(getattr(row, 'task_id', '') or '').strip()
            source_node_id = str(getattr(row, 'source_node_id', '') or '').strip()
            content = str(getattr(row, 'content', '') or '').strip()
            items.append(
                {
                    'message_id': str(getattr(row, 'message_id', '') or '').strip(),
                    'created_at': str(getattr(row, 'created_at', '') or '').strip(),
                    'task_id': task_id,
                    'source_node_id': source_node_id,
                    'source_goal': goal,
                    'status': str(getattr(row, 'status', '') or '').strip(),
                    'content': content,
                    'ref': str(getattr(row, 'ref', '') or '').strip(),
                    'signed_text': _signed_text(
                        task_id=task_id,
                        source_node_id=source_node_id,
                        goal=goal,
                        content=content,
                    ),
                }
            )
        if not items:
            return json.dumps(
                {'ok': True, 'count': 0, 'ref': '', 'summary': 'no messages received'},
                ensure_ascii=False,
                sort_keys=True,
            )
        artifact_text = json.dumps(
            {'session_id': session_id, 'messages': items},
            ensure_ascii=False,
            indent=2,
        )
        summary, ref = self._service.log_service.externalize_node_artifact(
            task_id=str(items[0].get('task_id') or 'frontdoor'),
            node_id=session_id,
            content=artifact_text,
            display_name=f'frontdoor-inbox:{session_id}',
            source_kind='frontdoor_node_message_history',
        )
        if not str(ref or '').strip():
            return json.dumps(
                {'ok': False, 'error': 'history_artifact_unavailable', 'count': len(items)},
                ensure_ascii=False,
                sort_keys=True,
            )
        self._service.mark_frontdoor_node_messages_consumed(
            [str(item.get('message_id') or '') for item in items]
        )
        return json.dumps(
            {
                'ok': True,
                'count': len(items),
                'ref': str(ref).strip(),
                'summary': str(summary or '')[:500],
                'latest_at': items[-1].get('created_at', ''),
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _FrontdoorMessageHistoryHandler(service)
