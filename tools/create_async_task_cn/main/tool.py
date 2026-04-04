from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool
from main.models import normalize_execution_policy_metadata


_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


def _runtime_payload(runtime: dict[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    if isinstance(runtime, dict):
        return runtime
    fallback = kwargs.get('__g3ku_runtime')
    return fallback if isinstance(fallback, dict) else {}


def _runtime_task_default_max_depth(runtime: dict[str, Any] | None) -> int | None:
    payload = runtime if isinstance(runtime, dict) else {}
    task_defaults = payload.get('task_defaults')
    if not isinstance(task_defaults, dict):
        return None
    raw_depth = task_defaults.get('max_depth', task_defaults.get('maxDepth'))
    if raw_depth in (None, ''):
        return None
    try:
        return int(raw_depth)
    except (TypeError, ValueError):
        return None


def _normalize_continuation_task_id(value: Any) -> str:
    task_id = str(value or '').strip()
    return task_id if task_id.startswith('task:') else ''


class _CreateAsyncTaskHandler(Tool):
    def __init__(self, service) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return str(_MANIFEST.get('name') or 'create_async_task')

    @property
    def description(self) -> str:
        return str(_MANIFEST.get('description') or '')

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(_MANIFEST.get('parameters') or {'type': 'object', 'properties': {}, 'required': []})

    async def execute(
        self,
        task: str,
        core_requirement: str = '',
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        runtime = _runtime_payload(__g3ku_runtime, kwargs)
        session_id = str(runtime.get('session_key') or 'web:shared').strip() or 'web:shared'
        explicit_max_depth = kwargs.get('max_depth', kwargs.get('maxDepth'))
        if explicit_max_depth in (None, ''):
            explicit_max_depth = _runtime_task_default_max_depth(runtime)
        normalized_core_requirement = str(core_requirement or kwargs.get('core_requirement') or '').strip() or str(task or '').strip()
        normalized_execution_policy = normalize_execution_policy_metadata(kwargs.get('execution_policy'))
        final_acceptance_prompt = str(kwargs.get('final_acceptance_prompt') or '').strip()
        raw_requires_final_acceptance = kwargs.get('requires_final_acceptance')
        requires_final_acceptance = bool(raw_requires_final_acceptance) or (
            raw_requires_final_acceptance in (None, '') and bool(final_acceptance_prompt)
        )
        continuation_of_task_id = _normalize_continuation_task_id(kwargs.get('continuation_of_task_id'))
        raw_reuse_existing = kwargs.get('reuse_existing')
        reuse_existing = True if raw_reuse_existing in (None, '') else bool(raw_reuse_existing)
        created_by_source = ''
        if continuation_of_task_id:
            created_by_source = 'heartbeat_auto_continue' if bool(runtime.get('heartbeat_internal')) else 'ceo_user_rebuild'
            if reuse_existing:
                finder = getattr(self._service, 'find_reusable_continuation_task', None)
                existing = (
                    finder(session_id=session_id, continuation_of_task_id=continuation_of_task_id)
                    if callable(finder)
                    else None
                )
                if existing is not None:
                    return f'复用进行中任务{existing.task_id}'
        record = await self._service.create_task(
            str(task or ''),
            session_id=session_id,
            max_depth=explicit_max_depth,
            metadata={
                'core_requirement': normalized_core_requirement,
                'execution_policy': normalized_execution_policy.model_dump(mode='json'),
                'continuation_of_task_id': continuation_of_task_id,
                'created_by_source': created_by_source,
                'final_acceptance': {
                    'required': requires_final_acceptance,
                    'prompt': final_acceptance_prompt,
                    'node_id': '',
                    'status': 'pending',
                },
            },
        )
        return f'创建任务成功{record.task_id}'


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _CreateAsyncTaskHandler(service)
