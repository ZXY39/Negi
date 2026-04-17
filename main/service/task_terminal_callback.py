from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from main.models import normalize_failure_class, normalize_final_acceptance_metadata


TASK_TERMINAL_CALLBACK_PATH = '/api/internal/task-terminal'
TASK_TERMINAL_CALLBACK_URL_ENV = 'G3KU_INTERNAL_CALLBACK_URL'
TASK_TERMINAL_CALLBACK_TOKEN_ENV = 'G3KU_INTERNAL_CALLBACK_TOKEN'
TASK_TERMINAL_CALLBACK_FILE = Path('.g3ku') / 'internal-callback.json'


def callback_config_path(*, workspace: Path | str | None = None) -> Path:
    root = Path(workspace) if workspace is not None else Path.cwd()
    return root / TASK_TERMINAL_CALLBACK_FILE


def load_task_terminal_callback_config(*, workspace: Path | str | None = None) -> dict[str, Any]:
    path = callback_config_path(workspace=workspace)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_task_terminal_callback_config(
    *,
    workspace: Path | str | None = None,
    url: str,
    token: str,
) -> dict[str, str]:
    path = callback_config_path(workspace=workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'url': str(url or '').strip(),
        'token': str(token or '').strip(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload


def resolve_task_terminal_callback_url(*, workspace: Path | str | None = None) -> str:
    env_value = str(os.getenv(TASK_TERMINAL_CALLBACK_URL_ENV, '') or '').strip()
    if env_value:
        return env_value
    return str(load_task_terminal_callback_config(workspace=workspace).get('url') or '').strip()


def resolve_task_terminal_callback_token(*, workspace: Path | str | None = None) -> str:
    env_value = str(os.getenv(TASK_TERMINAL_CALLBACK_TOKEN_ENV, '') or '').strip()
    if env_value:
        return env_value
    return str(load_task_terminal_callback_config(workspace=workspace).get('token') or '').strip()


def build_task_terminal_dedupe_key(*, task_id: str, status: str, finished_at: str) -> str:
    return f'task-terminal:{str(task_id or '').strip()}:{str(status or '').strip().lower()}:{str(finished_at or '').strip()}'


def _normalize_task_terminal_text(value: Any) -> str:
    return str(value or '').strip()


def _task_terminal_delivery_payload(
    task: Any,
    *,
    node_detail_getter: Callable[[str, str], dict[str, Any] | None] | None = None,
) -> dict[str, str]:
    task_id = _normalize_task_terminal_text(getattr(task, 'task_id', ''))
    root_node_id = _normalize_task_terminal_text(getattr(task, 'root_node_id', ''))
    metadata = getattr(task, 'metadata', None) if isinstance(getattr(task, 'metadata', None), dict) else {}
    final_acceptance = normalize_final_acceptance_metadata((metadata or {}).get('final_acceptance'))
    acceptance_node_id = _normalize_task_terminal_text(getattr(final_acceptance, 'node_id', ''))
    acceptance_failed = bool(
        getattr(final_acceptance, 'required', False)
        and _normalize_task_terminal_text(getattr(final_acceptance, 'status', '')).lower() == 'failed'
        and acceptance_node_id
    )
    terminal_node_id = acceptance_node_id if acceptance_failed else root_node_id
    terminal_node_kind = 'acceptance' if acceptance_failed else 'execution'
    terminal_node_reason = 'acceptance_failed' if acceptance_failed else 'root_terminal'
    terminal_output = ''
    terminal_output_ref = ''
    terminal_check_result = ''
    terminal_failure_reason = ''

    detail_item = None
    if task_id and terminal_node_id and callable(node_detail_getter):
        try:
            detail_payload = node_detail_getter(task_id, terminal_node_id)
        except Exception:
            detail_payload = None
        if isinstance(detail_payload, dict) and isinstance(detail_payload.get('item'), dict):
            detail_item = dict(detail_payload.get('item') or {})

    if isinstance(detail_item, dict):
        terminal_output = _normalize_task_terminal_text(
            detail_item.get('final_output')
            or detail_item.get('output')
            or detail_item.get('check_result')
            or detail_item.get('failure_reason')
        )
        terminal_output_ref = _normalize_task_terminal_text(
            detail_item.get('final_output_ref')
            or detail_item.get('output_ref')
            or detail_item.get('check_result_ref')
        )
        terminal_check_result = _normalize_task_terminal_text(detail_item.get('check_result'))
        terminal_failure_reason = _normalize_task_terminal_text(detail_item.get('failure_reason'))

    if not terminal_output:
        if acceptance_failed:
            terminal_output = _normalize_task_terminal_text(getattr(task, 'failure_reason', ''))
        else:
            terminal_output = _normalize_task_terminal_text(getattr(task, 'final_output', ''))
    if not terminal_output_ref and not acceptance_failed:
        terminal_output_ref = _normalize_task_terminal_text(getattr(task, 'final_output_ref', ''))
    if not terminal_failure_reason:
        terminal_failure_reason = _normalize_task_terminal_text(getattr(task, 'failure_reason', ''))

    return {
        'root_node_id': root_node_id,
        'acceptance_node_id': acceptance_node_id,
        'terminal_node_id': terminal_node_id,
        'terminal_node_kind': terminal_node_kind,
        'terminal_node_reason': terminal_node_reason,
        'terminal_output': terminal_output,
        'terminal_output_ref': terminal_output_ref,
        'terminal_check_result': terminal_check_result,
        'terminal_failure_reason': terminal_failure_reason,
    }


def build_task_terminal_payload(task: Any) -> dict[str, str]:
    task_id = str(getattr(task, 'task_id', '') or '').strip()
    session_id = str(getattr(task, 'session_id', '') or '').strip()
    status = str(getattr(task, 'status', '') or '').strip().lower()
    finished_at = str(getattr(task, 'finished_at', '') or '').strip()
    payload = {
        'dedupe_key': build_task_terminal_dedupe_key(task_id=task_id, status=status, finished_at=finished_at),
        'task_id': task_id,
        'session_id': session_id,
        'title': str(getattr(task, 'title', '') or task_id).strip() or task_id,
        'status': status,
        'failure_class': normalize_failure_class((getattr(task, 'metadata', None) or {}).get('failure_class')),
        'final_acceptance_status': str(normalize_final_acceptance_metadata((getattr(task, 'metadata', None) or {}).get('final_acceptance')).status or '').strip().lower(),
        'brief_text': str(getattr(task, 'brief_text', '') or '').strip(),
        'failure_reason': str(getattr(task, 'failure_reason', '') or '').strip(),
        'finished_at': finished_at,
    }
    payload.update(_task_terminal_delivery_payload(task))
    return payload


def enrich_task_terminal_payload(
    payload: dict[str, Any] | None,
    *,
    task: Any | None = None,
    task_getter: Callable[[str], Any | None] | None = None,
    node_detail_getter: Callable[[str, str], dict[str, Any] | None] | None = None,
) -> dict[str, str]:
    normalized = normalize_task_terminal_payload(payload)
    if not normalized:
        return {}
    task_record = task
    if task_record is None and callable(task_getter):
        try:
            task_record = task_getter(str(normalized.get('task_id') or '').strip())
        except Exception:
            task_record = None
    if task_record is None:
        return normalized
    normalized.update(_task_terminal_delivery_payload(task_record, node_detail_getter=node_detail_getter))
    return normalize_task_terminal_payload(normalized)


def normalize_task_terminal_payload(payload: dict[str, Any] | None) -> dict[str, str]:
    source = payload if isinstance(payload, dict) else {}
    task_id = str(source.get('task_id') or source.get('taskId') or '').strip()
    if task_id and not task_id.startswith('task:') and ':' not in task_id:
        task_id = f'task:{task_id}'
    session_id = str(source.get('session_id') or source.get('sessionId') or '').strip() or 'web:shared'
    status = str(source.get('status') or '').strip().lower()
    finished_at = str(source.get('finished_at') or source.get('finishedAt') or '').strip()
    if not task_id or status not in {'success', 'failed'}:
        return {}
    dedupe_key = str(source.get('dedupe_key') or source.get('dedupeKey') or '').strip()
    if not dedupe_key:
        dedupe_key = build_task_terminal_dedupe_key(task_id=task_id, status=status, finished_at=finished_at)
    return {
        'dedupe_key': dedupe_key,
        'task_id': task_id,
        'session_id': session_id,
        'title': str(source.get('title') or task_id).strip() or task_id,
        'status': status,
        'failure_class': normalize_failure_class(source.get('failure_class') or source.get('failureClass')),
        'final_acceptance_status': _normalize_task_terminal_text(source.get('final_acceptance_status') or source.get('finalAcceptanceStatus')).lower(),
        'brief_text': str(source.get('brief_text') or source.get('briefText') or '').strip(),
        'failure_reason': str(source.get('failure_reason') or source.get('failureReason') or '').strip(),
        'finished_at': finished_at,
        'root_node_id': _normalize_task_terminal_text(source.get('root_node_id') or source.get('rootNodeId')),
        'acceptance_node_id': _normalize_task_terminal_text(source.get('acceptance_node_id') or source.get('acceptanceNodeId')),
        'terminal_node_id': _normalize_task_terminal_text(source.get('terminal_node_id') or source.get('terminalNodeId')),
        'terminal_node_kind': _normalize_task_terminal_text(source.get('terminal_node_kind') or source.get('terminalNodeKind')),
        'terminal_node_reason': _normalize_task_terminal_text(source.get('terminal_node_reason') or source.get('terminalNodeReason')),
        'terminal_output': _normalize_task_terminal_text(source.get('terminal_output') or source.get('terminalOutput')),
        'terminal_output_ref': _normalize_task_terminal_text(source.get('terminal_output_ref') or source.get('terminalOutputRef')),
        'terminal_check_result': _normalize_task_terminal_text(source.get('terminal_check_result') or source.get('terminalCheckResult')),
        'terminal_failure_reason': _normalize_task_terminal_text(source.get('terminal_failure_reason') or source.get('terminalFailureReason')),
    }
