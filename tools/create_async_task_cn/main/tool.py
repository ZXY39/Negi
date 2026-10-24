from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool
from main.models import normalize_execution_policy_metadata
from main.service.create_async_task_contract import (
    normalize_create_async_task_file_targets,
    normalize_create_async_task_inbound_params,
    normalize_create_async_task_seed_tools,
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


def _collect_matched_task_ids(precheck: dict[str, Any]) -> list[str]:
    raw_ids = precheck.get('matched_task_ids')
    if isinstance(raw_ids, (list, tuple)):
        ids: list[str] = []
        seen: set[str] = set()
        for raw in raw_ids:
            task_id = str(raw or '').strip()
            if task_id and task_id not in seen:
                seen.add(task_id)
                ids.append(task_id)
        if ids:
            return ids
    single = str(precheck.get('matched_task_id') or '').strip()
    return [single] if single else []


def _duplicate_rejection_text(precheck: dict[str, Any]) -> str:
    matched_ids = _collect_matched_task_ids(precheck)
    reason = str(precheck.get('reason') or '').strip()
    ids_text = '、'.join(matched_ids) if matched_ids else '(unknown)'
    return (
        f'任务未创建：正在运行的任务 {ids_text} 与新任务完全相同（命中：{reason or "unknown"}）。\n'
        '- 重复拦截仅针对正在运行的任务：暂停旧任务或等待其进入终态（如失败）后，即可重新创建；\n'
        '- 如只是想补充约束、验收细节或更新要求 → 先暂停或结束旧任务，再创建包含新约束的任务；\n'
        '- 如确需重做该工作 → 先与用户确认意图；确需删除旧任务时，先征求用户同意再操作。'
    )


def _seed_tools_receipt_line(service, record, declared: list[str]) -> str:
    """声明过才回执；没声明时整行不出现——空回执对派发方是纯噪音。

    `accepted` 与 `dropped` 两键恒在：省略与「全被丢弃」在派发方看来无法区分。
    """
    if not declared:
        return ''
    root = service.get_node(str(getattr(record, 'root_node_id', '') or '').strip()) if record is not None else None
    receipt = dict((getattr(root, 'metadata', None) or {}).get('seed_receipt') or {})
    payload = {
        'accepted': list(receipt.get('accepted') or []),
        'dropped': list(receipt.get('dropped') or []),
    }
    return f'\nseed_tools: {json.dumps(payload, ensure_ascii=False)}'


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
        normalized_seed_tools = normalize_create_async_task_seed_tools(kwargs.get('seed_tools'))
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
            return _duplicate_rejection_text(precheck)
        if decision == 'reject_use_append_notice':
            return (
                f'任务未创建：现有任务 {matched_task_id} 已经覆盖这次要求。'
                f'请对该任务用 manage_task_nodes 继续推进（例如 resume），不要新建重复任务。原因：{reason}'
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
                return _duplicate_rejection_text(dict(create_guard or {}))
            if guard_decision == 'reject_use_append_notice':
                return (
                    f'任务未创建：现有任务 {guard_task_id} 已经覆盖这次要求。'
                    f'请对该任务用 manage_task_nodes 继续推进（例如 resume），不要新建重复任务。'
                    f'原因：{guard_reason}'
                )
        record = await self._service.create_task(
            str(task or ''),
            session_id=session_id,
            max_depth=explicit_max_depth,
            metadata={
                'core_requirement': normalized_core_requirement,
                'execution_policy': normalized_execution_policy.model_dump(mode='json'),
                'file_targets': normalized_file_targets,
                'seed_tools': normalized_seed_tools,
                'final_acceptance': {
                    'required': requires_final_acceptance,
                    'prompt': final_acceptance_prompt,
                    'node_id': '',
                    'status': 'pending',
                },
            },
        )
        return f'创建任务成功{record.task_id}' + _seed_tools_receipt_line(self._service, record, normalized_seed_tools)


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _CreateAsyncTaskHandler(service)
