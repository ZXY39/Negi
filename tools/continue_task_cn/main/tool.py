from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from g3ku.agent.tools.base import Tool


_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


def _runtime_payload(runtime: dict[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    if isinstance(runtime, dict):
        return runtime
    fallback = kwargs.get('__g3ku_runtime')
    return fallback if isinstance(fallback, dict) else {}


class _ContinueTaskHandler(Tool):
    def __init__(self, service) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return str(_MANIFEST.get('name') or 'continue_task')

    @property
    def description(self) -> str:
        return str(_MANIFEST.get('description') or '')

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(_MANIFEST.get('parameters') or {'type': 'object', 'properties': {}, 'required': []})

    async def execute(
        self,
        mode: str,
        target_task_id: str,
        continuation_instruction: str,
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        runtime = _runtime_payload(__g3ku_runtime, kwargs)
        result = await self._service.continue_task(
            mode=mode,
            target_task_id=target_task_id,
            continuation_instruction=continuation_instruction,
            execution_policy=kwargs.get('execution_policy'),
            requires_final_acceptance=kwargs.get('requires_final_acceptance'),
            final_acceptance_prompt=str(kwargs.get('final_acceptance_prompt') or ''),
            reuse_existing=True if kwargs.get('reuse_existing') in (None, '') else bool(kwargs.get('reuse_existing')),
            reason=str(kwargs.get('reason') or '').strip(),
            source='heartbeat' if bool(runtime.get('heartbeat_internal')) else 'ceo',
        )
        payload = dict(result or {})
        for key in ('target_task', 'continuation_task', 'resumed_task'):
            value = payload.get(key)
            if value is not None and hasattr(value, 'model_dump'):
                payload[key] = value.model_dump(mode='json')
        return json.dumps(payload, ensure_ascii=False)


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _ContinueTaskHandler(service)
