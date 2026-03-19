from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


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


def build_task_terminal_payload(task: Any) -> dict[str, str]:
    task_id = str(getattr(task, 'task_id', '') or '').strip()
    session_id = str(getattr(task, 'session_id', '') or '').strip()
    status = str(getattr(task, 'status', '') or '').strip().lower()
    finished_at = str(getattr(task, 'finished_at', '') or '').strip()
    return {
        'dedupe_key': build_task_terminal_dedupe_key(task_id=task_id, status=status, finished_at=finished_at),
        'task_id': task_id,
        'session_id': session_id,
        'title': str(getattr(task, 'title', '') or task_id).strip() or task_id,
        'status': status,
        'brief_text': str(getattr(task, 'brief_text', '') or '').strip(),
        'failure_reason': str(getattr(task, 'failure_reason', '') or '').strip(),
        'finished_at': finished_at,
    }


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
        'brief_text': str(source.get('brief_text') or source.get('briefText') or '').strip(),
        'failure_reason': str(source.get('failure_reason') or source.get('failureReason') or '').strip(),
        'finished_at': finished_at,
    }
