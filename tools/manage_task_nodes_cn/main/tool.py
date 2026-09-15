from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool


_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


def _error(details: str) -> str:
    return json.dumps({'ok': False, 'error': 'invalid_param', 'details': details}, ensure_ascii=False)


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
        node_ids: list[str] | None = None,
        action: str = '',
        remark: str = '',
        cascade: bool = False,
        targets: list[dict[str, Any]] | None = None,
        **_: Any,
    ) -> str:
        if not str(task_id or '').strip():
            return _error('task_id is required')
        legacy_node_ids = [str(item or '').strip() for item in list(node_ids or []) if str(item or '').strip()]
        legacy_action = str(action or '').strip().lower()
        has_legacy = bool(legacy_node_ids) or bool(legacy_action)
        has_targets = isinstance(targets, list) and len(list(targets)) > 0
        # 两形态互斥校验放在 execute（schema 校验器不支持 oneOf）。
        if has_legacy and has_targets:
            return _error('provide either node_ids+action or targets, not both')
        if not has_legacy and not has_targets:
            return _error('provide node_ids+action, or targets')
        if has_targets:
            normalized_targets: list[dict[str, Any]] = []
            for position, item in enumerate(list(targets or [])):
                if not isinstance(item, dict):
                    return _error(f'targets[{position}] must be an object with node_id and action')
                node_id = str(item.get('node_id') or '').strip()
                item_action = str(item.get('action') or '').strip().lower()
                if not node_id:
                    return _error(f'targets[{position}].node_id is required')
                if not item_action:
                    return _error(f'targets[{position}].action is required')
                normalized_targets.append({'node_id': node_id, 'action': item_action, 'cascade': bool(item.get('cascade', False))})
            result = await self._service.control_nodes(
                str(task_id or '').strip(),
                [],
                '',
                remark=str(remark or ''),
                targets=normalized_targets,
            )
            return json.dumps(result, ensure_ascii=False)
        if not legacy_node_ids:
            return _error('node_ids must contain at least one node id')
        if not legacy_action:
            return _error('action is required together with node_ids')
        result = await self._service.control_nodes(
            str(task_id or '').strip(),
            legacy_node_ids,
            legacy_action,
            remark=str(remark or ''),
            cascade=bool(cascade),
        )
        return json.dumps(result, ensure_ascii=False)


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _ManageTaskNodesHandler(service)
