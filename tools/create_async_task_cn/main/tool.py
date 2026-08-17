from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool
from main.models import normalize_execution_policy_metadata
from main.service.create_async_task_contract import (
    normalize_create_async_task_file_targets,
    normalize_create_async_task_inbound_params,
    validate_create_async_task_file_targets,
)


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

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        normalized = normalize_create_async_task_inbound_params(params)
        errors = super().validate_params(normalized)
        if 'core_requirement' in (normalized or {}):
            core_requirement = str((normalized or {}).get('core_requirement') or '').strip()
            if not core_requirement:
                errors.append('core_requirement must not be empty')
        if 'continuation_of_task_id' in (normalized or {}) or 'reuse_existing' in (normalized or {}):
            errors.append('create_async_task_no_longer_supports_continuation')
        requires_final_acceptance = (normalized or {}).get('requires_final_acceptance')
        final_acceptance_prompt = str((normalized or {}).get('final_acceptance_prompt') or '').strip()
        if requires_final_acceptance is True and not final_acceptance_prompt:
            errors.append('final_acceptance_prompt is required when requires_final_acceptance=true')
        errors.extend(validate_create_async_task_file_targets((normalized or {}).get('file_targets')))
        return errors

    async def execute(
        self,
        task: str,
        core_requirement: str = '',
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        runtime = _runtime_payload(__g3ku_runtime, kwargs)
        if 'continuation_of_task_id' in kwargs or 'reuse_existing' in kwargs:
            raise ValueError('create_async_task_no_longer_supports_continuation')
        session_id = str(runtime.get('session_key') or 'web:shared').strip() or 'web:shared'
        explicit_max_depth = kwargs.get('max_depth', kwargs.get('maxDepth'))
        if explicit_max_depth in (None, ''):
            explicit_max_depth = _runtime_task_default_max_depth(runtime)
        normalized_core_requirement = str(core_requirement or kwargs.get('core_requirement') or '').strip() or str(task or '').strip()
        normalized_execution_policy = normalize_execution_policy_metadata(kwargs.get('execution_policy'))
        normalized_file_targets = normalize_create_async_task_file_targets(kwargs.get('file_targets'))
        file_target_errors = validate_create_async_task_file_targets(normalized_file_targets)
        if file_target_errors:
            raise ValueError('; '.join(file_target_errors))
        final_acceptance_prompt = str(kwargs.get('final_acceptance_prompt') or '').strip()
        raw_requires_final_acceptance = kwargs.get('requires_final_acceptance')
        requires_final_acceptance = bool(raw_requires_final_acceptance) or (
            raw_requires_final_acceptance in (None, '') and bool(final_acceptance_prompt)
        )
        precheck = await self._service.precheck_async_task_creation(
            session_id=session_id,
            task_text=str(task or '').strip(),
            core_requirement=normalized_core_requirement,
            execution_policy=normalized_execution_policy.model_dump(mode='json'),
            requires_final_acceptance=requires_final_acceptance,
            final_acceptance_prompt=final_acceptance_prompt,
        )
        decision = str(precheck.get('decision') or '').strip()
        matched_task_id = str(precheck.get('matched_task_id') or '').strip()
        reason = str(precheck.get('reason') or '').strip()
        if decision == 'reject_duplicate':
            return f'任务未创建：与进行中任务 {matched_task_id} 高度重复。原因：{reason}'
        if decision == 'reject_use_append_notice':
            return (
                f'任务未创建：现有任务 {matched_task_id} 需要追加通知而不是新建。'
                f'请改用 task_append_notice。原因：{reason}'
            )
        revalidate = getattr(self._service, 'revalidate_async_task_creation_before_create', None)
        if callable(revalidate):
            create_guard = revalidate(
                session_id=session_id,
                task_text=str(task or '').strip(),
                core_requirement=normalized_core_requirement,
                execution_policy=normalized_execution_policy.model_dump(mode='json'),
                requires_final_acceptance=requires_final_acceptance,
                final_acceptance_prompt=final_acceptance_prompt,
            )
            guard_decision = str((create_guard or {}).get('decision') or '').strip()
            guard_task_id = str((create_guard or {}).get('matched_task_id') or '').strip()
            guard_reason = str((create_guard or {}).get('reason') or '').strip()
            if guard_decision == 'reject_duplicate':
                return f'任务未创建：与进行中任务 {guard_task_id} 高度重复。原因：{guard_reason}'
            if guard_decision == 'reject_use_append_notice':
                return (
                    f'任务未创建：现有任务 {guard_task_id} 需要追加通知而不是新建。'
                    f'请改用 task_append_notice。原因：{guard_reason}'
                )
        record = await self._service.create_task(
            str(task or ''),
            session_id=session_id,
            max_depth=explicit_max_depth,
            metadata={
                'core_requirement': normalized_core_requirement,
                'execution_policy': normalized_execution_policy.model_dump(mode='json'),
                'file_targets': normalized_file_targets,
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
