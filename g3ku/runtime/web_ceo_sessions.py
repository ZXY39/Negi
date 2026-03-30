from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from g3ku.china_bridge.session_keys import build_session_key, parse_china_session_key
from g3ku.config.loader import load_config
from g3ku.runtime.frontdoor.interaction_trace import (
    CEO_STAGE_STATUS_ACTIVE,
    normalize_interaction_trace,
)
from g3ku.runtime.memory_scope import DEFAULT_WEB_MEMORY_SCOPE, normalize_memory_scope
from g3ku.utils.helpers import ensure_dir, safe_filename

DEFAULT_CEO_SESSION_TITLE = "新会话"
WEB_CEO_STATE_FILE = Path(".g3ku") / "web-ceo-state.json"
WEB_CEO_UPLOAD_ROOT = Path(".g3ku") / "web-ceo-uploads"
WEB_CEO_INFLIGHT_ROOT = Path(".g3ku") / "web-ceo-inflight"
WEB_CEO_PAUSED_ROOT = Path(".g3ku") / "web-ceo-paused"
DEFAULT_TASK_MAX_DEPTH = 1
DEFAULT_TASK_HARD_MAX_DEPTH = 4
DEFAULT_LIVE_RAW_TAIL_TURNS = 4
TASK_MEMORY_VERSION = 2
_TASK_MEMORY_MAX_IDS = 3
_TASK_ID_PATTERN = re.compile(r'task:[A-Za-z0-9][\w:-]*')
_RECENT_HISTORY_TOOL_TRACE_LIMIT = 2
_RECENT_HISTORY_TOOL_TEXT_MAX_CHARS = 96
_RECENT_HISTORY_STAGE_GOAL_MAX_CHARS = 120
_TASK_RESULT_OUTPUT_MAX_CHARS = 480
_TASK_RESULT_REASON_MAX_CHARS = 180


def _normalize_task_ids(values: Any, *, limit: int = _TASK_MEMORY_MAX_IDS) -> list[str]:
    items = list(values) if isinstance(values, (list, tuple, set)) else [values]
    normalized: list[str] = []
    for raw in items:
        task_id = str(raw or '').strip()
        if not task_id or not task_id.startswith('task:'):
            continue
        if task_id in normalized:
            continue
        normalized.append(task_id)
        if len(normalized) >= max(1, int(limit or _TASK_MEMORY_MAX_IDS)):
            break
    return normalized


def _extract_task_ids_from_text(text: Any, *, limit: int = _TASK_MEMORY_MAX_IDS) -> list[str]:
    return _normalize_task_ids(_TASK_ID_PATTERN.findall(str(text or '')), limit=limit)


def normalize_task_memory(payload: Any) -> dict[str, Any]:
    source = dict(payload or {}) if isinstance(payload, dict) else {}
    return {
        'version': TASK_MEMORY_VERSION,
        'task_ids': _normalize_task_ids(source.get('task_ids')),
        'source': str(source.get('source') or '').strip(),
        'reason': str(source.get('reason') or '').strip(),
        'updated_at': str(source.get('updated_at') or '').strip(),
        'task_results': _normalize_task_results(source.get('task_results', source.get('taskResults'))),
    }


def _normalize_task_results(values: Any, *, limit: int = _TASK_MEMORY_MAX_IDS) -> list[dict[str, str]]:
    items = list(values) if isinstance(values, (list, tuple, set)) else [values]
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        task_id = _normalize_task_ids(raw.get('task_id') or raw.get('taskId'), limit=1)
        task_id_text = task_id[0] if task_id else ''
        node_id = str(raw.get('node_id') or raw.get('nodeId') or '').strip()
        if not task_id_text:
            continue
        dedupe_key = (task_id_text, node_id)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        item = {
            'task_id': task_id_text,
            'node_id': node_id,
            'node_kind': str(raw.get('node_kind') or raw.get('nodeKind') or '').strip(),
            'node_reason': str(raw.get('node_reason') or raw.get('nodeReason') or '').strip(),
            'output_excerpt': summarize_preview_text(
                raw.get('output_excerpt') or raw.get('output') or '',
                max_chars=_TASK_RESULT_OUTPUT_MAX_CHARS,
            ),
            'output_ref': str(raw.get('output_ref') or raw.get('outputRef') or '').strip(),
            'check_result': summarize_preview_text(raw.get('check_result') or raw.get('checkResult') or '', max_chars=_TASK_RESULT_REASON_MAX_CHARS),
            'failure_reason': summarize_preview_text(raw.get('failure_reason') or raw.get('failureReason') or '', max_chars=_TASK_RESULT_REASON_MAX_CHARS),
        }
        normalized.append({key: value for key, value in item.items() if value})
        if len(normalized) >= max(1, int(limit or _TASK_MEMORY_MAX_IDS)):
            break
    return normalized


def _extract_task_ids_from_message(message: dict[str, Any], *, limit: int = _TASK_MEMORY_MAX_IDS) -> list[str]:
    metadata = message.get('metadata') if isinstance(message.get('metadata'), dict) else {}
    task_ids: list[str] = []
    task_ids.extend(_normalize_task_ids(metadata.get('task_ids'), limit=limit))
    task_ids.extend(_extract_task_ids_from_text(message.get('content'), limit=limit))
    tool_events = message.get('tool_events') if isinstance(message.get('tool_events'), list) else []
    for item in tool_events:
        if not isinstance(item, dict):
            continue
        task_ids.extend(_extract_task_ids_from_text(item.get('text'), limit=limit))
    interaction_trace = message.get('interaction_trace') if isinstance(message.get('interaction_trace'), dict) else {}
    task_ids.extend(_extract_task_ids_from_text(interaction_trace.get('final_output'), limit=limit))
    return _normalize_task_ids(task_ids, limit=limit)


def is_internal_ceo_user_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    role = str(message.get('role') or '').strip().lower()
    if role != 'user':
        return False
    metadata = message.get('metadata') if isinstance(message.get('metadata'), dict) else {}
    return bool(metadata.get('heartbeat_internal')) or bool(metadata.get('cron_internal'))


def build_last_task_memory(session: Any) -> dict[str, Any]:
    remembered: list[str] = []
    remembered_results: list[dict[str, str]] = []
    source = ''
    reason = ''
    updated_at = ''
    for raw in reversed(list(getattr(session, 'messages', []) or [])):
        if not isinstance(raw, dict):
            continue
        if is_internal_ceo_user_message(raw):
            continue
        task_ids = _extract_task_ids_from_message(raw)
        if not task_ids:
            continue
        for task_id in task_ids:
            if task_id not in remembered:
                remembered.append(task_id)
                if len(remembered) >= _TASK_MEMORY_MAX_IDS:
                    break
        metadata = raw.get('metadata') if isinstance(raw.get('metadata'), dict) else {}
        for item in _normalize_task_results(metadata.get('task_results')):
            if item not in remembered_results:
                remembered_results.append(item)
        if not source:
            source = str(metadata.get('source') or '').strip() or 'transcript'
        if not reason:
            reason = str(metadata.get('reason') or '').strip()
        if not updated_at:
            updated_at = str(raw.get('timestamp') or '').strip()
        if len(remembered) >= _TASK_MEMORY_MAX_IDS:
            break
    return normalize_task_memory(
        {
            'task_ids': remembered,
            'source': source,
            'reason': reason,
            'updated_at': updated_at,
            'task_results': remembered_results,
        }
    )


def _normalize_execution_snapshot(snapshot: Any) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict) or not snapshot:
        return None
    normalized = dict(snapshot)
    interaction_trace = normalized.get('interaction_trace')
    if isinstance(interaction_trace, dict):
        normalized['interaction_trace'] = normalize_interaction_trace(interaction_trace)
    return normalized


def _session_key_for_execution_sources(runtime_session: Any | None, persisted_session: Any | None) -> str:
    session_key = str(
        getattr(getattr(runtime_session, 'state', None), 'session_key', '')
        or getattr(persisted_session, 'key', '')
        or ''
    ).strip()
    return session_key


def _runtime_execution_snapshot(runtime_session: Any | None) -> tuple[dict[str, Any] | None, str]:
    if runtime_session is None:
        return None, ''
    snapshot_supplier = getattr(runtime_session, 'inflight_turn_snapshot', None)
    snapshot = snapshot_supplier() if callable(snapshot_supplier) else None
    normalized_snapshot = _normalize_execution_snapshot(snapshot)
    if normalized_snapshot is not None:
        return normalized_snapshot, 'live_runtime'
    trace_supplier = getattr(runtime_session, 'interaction_trace_snapshot', None)
    trace = trace_supplier() if callable(trace_supplier) else None
    normalized_trace = normalize_interaction_trace(trace)
    if normalized_trace.get('stages'):
        synthetic_snapshot: dict[str, Any] = {'interaction_trace': normalized_trace}
        stage_supplier = getattr(runtime_session, 'current_stage_snapshot', None)
        stage = stage_supplier() if callable(stage_supplier) else None
        if isinstance(stage, dict) and stage:
            synthetic_snapshot['stage'] = dict(stage)
        return synthetic_snapshot, 'live_runtime'
    paused_supplier = getattr(runtime_session, 'paused_execution_context_snapshot', None)
    paused_snapshot = paused_supplier() if callable(paused_supplier) else None
    normalized_paused = _normalize_execution_snapshot(paused_snapshot)
    if normalized_paused is not None:
        return normalized_paused, 'paused_execution'
    return None, ''


def resolve_execution_snapshot(
    runtime_session: Any | None,
    persisted_session: Any | None = None,
) -> tuple[dict[str, Any] | None, str]:
    runtime_snapshot, runtime_source = _runtime_execution_snapshot(runtime_session)
    if runtime_snapshot is not None:
        return runtime_snapshot, runtime_source
    session_key = _session_key_for_execution_sources(runtime_session, persisted_session)
    if session_key:
        paused_snapshot = read_paused_execution_context(session_key)
        normalized_paused = _normalize_execution_snapshot(paused_snapshot)
        if normalized_paused is not None:
            return normalized_paused, 'paused_execution'
    return None, ''


def _execution_snapshot_history_messages(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    normalized_snapshot = _normalize_execution_snapshot(snapshot)
    if normalized_snapshot is None:
        return []
    messages: list[dict[str, Any]] = []
    user_message = normalized_snapshot.get('user_message')
    if isinstance(user_message, dict):
        user_content = str(user_message.get('content') or '').strip()
        if user_content:
            messages.append({'role': 'user', 'content': user_content})
    assistant_message: dict[str, Any] = {'role': 'assistant'}
    assistant_text = summarize_preview_text(normalized_snapshot.get('assistant_text') or '', max_chars=480)
    if assistant_text:
        assistant_message['content'] = assistant_text
    tool_events = normalized_snapshot.get('tool_events')
    if isinstance(tool_events, list) and tool_events:
        assistant_message['tool_events'] = list(tool_events)
    interaction_trace = normalize_interaction_trace(normalized_snapshot.get('interaction_trace'))
    if interaction_trace.get('stages'):
        assistant_message['interaction_trace'] = interaction_trace
    if assistant_message.get('content') or assistant_message.get('tool_events') or assistant_message.get('interaction_trace'):
        messages.append(_history_entry_from_message(assistant_message))
    return messages


def _build_task_memory_from_messages(
    messages: list[dict[str, Any]],
    *,
    source: str,
    reason: str,
    updated_at: str,
) -> dict[str, Any]:
    remembered: list[str] = []
    remembered_results: list[dict[str, str]] = []
    for raw in reversed(list(messages or [])):
        if not isinstance(raw, dict):
            continue
        task_ids = _extract_task_ids_from_message(raw)
        if not task_ids:
            continue
        for task_id in task_ids:
            if task_id not in remembered:
                remembered.append(task_id)
                if len(remembered) >= _TASK_MEMORY_MAX_IDS:
                    break
        metadata = raw.get('metadata') if isinstance(raw.get('metadata'), dict) else {}
        for item in _normalize_task_results(metadata.get('task_results')):
            if item not in remembered_results:
                remembered_results.append(item)
        if len(remembered) >= _TASK_MEMORY_MAX_IDS:
            break
    return normalize_task_memory(
        {
            'task_ids': remembered,
            'source': source,
            'reason': reason,
            'updated_at': updated_at,
            'task_results': remembered_results,
        }
    )


def _task_memory_from_execution_snapshot(snapshot: dict[str, Any] | None, *, source: str) -> dict[str, Any]:
    messages = _execution_snapshot_history_messages(snapshot)
    if not messages:
        return normalize_task_memory(None)
    return _build_task_memory_from_messages(
        messages,
        source=source,
        reason='paused execution context',
        updated_at=_inflight_updated_at(snapshot),
    )


def _merge_task_memory_layers(
    *layers: dict[str, Any],
    limit: int = _TASK_MEMORY_MAX_IDS,
) -> dict[str, Any]:
    remembered_ids: list[str] = []
    remembered_results: list[dict[str, str]] = []
    sources: list[str] = []
    reasons: list[str] = []
    updated_at = ''
    normalized_limit = max(1, int(limit or _TASK_MEMORY_MAX_IDS))
    for raw in layers:
        memory = normalize_task_memory(raw)
        source = str(memory.get('source') or '').strip()
        reason = str(memory.get('reason') or '').strip()
        if source and source not in sources:
            sources.append(source)
        if reason and reason not in reasons:
            reasons.append(reason)
        if not updated_at and str(memory.get('updated_at') or '').strip():
            updated_at = str(memory.get('updated_at') or '').strip()
        for task_id in list(memory.get('task_ids') or []):
            if task_id and task_id not in remembered_ids:
                remembered_ids.append(task_id)
                if len(remembered_ids) >= normalized_limit:
                    break
        for result in list(memory.get('task_results') or []):
            if result not in remembered_results:
                remembered_results.append(result)
        if len(remembered_ids) >= normalized_limit:
            break
    return normalize_task_memory(
        {
            'task_ids': remembered_ids[:normalized_limit],
            'source': ' + '.join(sources),
            'reason': ' + '.join(reasons),
            'updated_at': updated_at,
            'task_results': remembered_results[:normalized_limit],
        }
    )


def build_task_continuity_payload(
    *,
    session: Any | None,
    runtime_session: Any | None = None,
    active_tasks: Any = None,
    limit: int = _TASK_MEMORY_MAX_IDS,
) -> dict[str, Any] | None:
    normalized_active: list[dict[str, str]] = []
    seen_task_ids: set[str] = set()
    for raw in list(active_tasks or []):
        if not isinstance(raw, dict):
            continue
        task_id = str(raw.get('task_id') or '').strip()
        if not task_id or task_id in seen_task_ids:
            continue
        seen_task_ids.add(task_id)
        normalized_active.append(
            {
                key: value
                for key, value in {
                    'task_id': task_id,
                    'title': summarize_preview_text(raw.get('title') or '', max_chars=96),
                    'core_requirement': summarize_preview_text(raw.get('core_requirement') or '', max_chars=140),
                    'continuation_of_task_id': str(raw.get('continuation_of_task_id') or '').strip(),
                    'status': str(raw.get('status') or '').strip(),
                    'updated_at': str(raw.get('updated_at') or '').strip(),
                }.items()
                if value
            }
        )
        if len(normalized_active) >= max(1, int(limit or _TASK_MEMORY_MAX_IDS)):
            break

    execution_snapshot, execution_source = resolve_execution_snapshot(runtime_session, session)
    execution_task_memory = _task_memory_from_execution_snapshot(execution_snapshot, source=execution_source)
    last_task_memory = normalize_task_memory(
        getattr(session, 'metadata', {}).get('last_task_memory') if session is not None else None
    )
    if not last_task_memory.get('task_ids') and session is not None:
        last_task_memory = build_last_task_memory(session)
    merged_memory = _merge_task_memory_layers(
        execution_task_memory,
        last_task_memory,
        limit=limit,
    )

    if not normalized_active and not merged_memory.get('task_ids'):
        return None

    last_results = list(merged_memory.get('task_results') or [])
    active_task_ids = {
        str(item.get('task_id') or '').strip()
        for item in normalized_active
        if str(item.get('task_id') or '').strip()
    }
    for task in normalized_active:
        task_id = str(task.get('task_id') or '').strip()
        if not task_id:
            continue
        matched = [
            result
            for result in last_results
            if str(result.get('task_id') or '').strip() == task_id
        ]
        if matched:
            task['task_results'] = matched[:1]
    if active_task_ids:
        merged_memory = normalize_task_memory(
            {
                **merged_memory,
                'task_ids': [
                    task_id
                    for task_id in list(merged_memory.get('task_ids') or [])
                    if task_id not in active_task_ids
                ],
                'task_results': [
                    result
                    for result in list(merged_memory.get('task_results') or [])
                    if str(result.get('task_id') or '').strip() not in active_task_ids
                ],
            }
        )
    source_parts: list[str] = []
    if normalized_active:
        source_parts.append('active_tasks')
    if execution_task_memory.get('task_ids'):
        source_parts.append(execution_source or 'paused_execution')
    if last_task_memory.get('task_ids'):
        source_parts.append('session_metadata')
    return {
        'active_tasks': normalized_active,
        'last_task_memory': merged_memory,
        'source': ' + '.join(source_parts),
    }


def render_task_continuity_markdown(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ''
    active_tasks = list(payload.get('active_tasks') or [])
    memory = normalize_task_memory(payload.get('last_task_memory'))
    if not active_tasks and not memory.get('task_ids'):
        return ''
    lines = ['## Task Continuity']
    if active_tasks:
        lines.append('### Active Tasks')
        for item in active_tasks:
            task_id = str(item.get('task_id') or '').strip()
            if not task_id:
                continue
            detail_parts = []
            if item.get('title'):
                detail_parts.append(f"title={item['title']}")
            if item.get('core_requirement'):
                detail_parts.append(f"core_requirement={item['core_requirement']}")
            if item.get('continuation_of_task_id'):
                detail_parts.append(f"continuation_of_task_id={item['continuation_of_task_id']}")
            if item.get('status'):
                detail_parts.append(f"status={item['status']}")
            if item.get('updated_at'):
                detail_parts.append(f"updated_at={item['updated_at']}")
            lines.append(f"- `{task_id}`: {'; '.join(detail_parts)}")
            for result in list(item.get('task_results') or [])[:1]:
                summary = summarize_preview_text(
                    result.get('output_excerpt') or result.get('failure_reason') or result.get('check_result') or '',
                    max_chars=_TASK_RESULT_OUTPUT_MAX_CHARS,
                )
                if summary:
                    lines.append(f"  Recent result: {summary}")
    if memory.get('task_ids'):
        lines.append('### Last Confirmed Task Memory')
        lines.append(f"- task_ids: {', '.join(memory['task_ids'])}")
        if memory.get('reason'):
            lines.append(f"- reason: {memory['reason']}")
        if memory.get('source'):
            lines.append(f"- source: {memory['source']}")
        for result in list(memory.get('task_results') or [])[:2]:
            task_id = str(result.get('task_id') or '').strip()
            excerpt = summarize_preview_text(
                result.get('output_excerpt') or result.get('failure_reason') or result.get('check_result') or '',
                max_chars=_TASK_RESULT_OUTPUT_MAX_CHARS,
            )
            if task_id and excerpt:
                lines.append(f"- `{task_id}` result: {excerpt}")
    return '\n'.join(lines).strip()


def _compact_task_meta_payload(message: dict[str, Any]) -> dict[str, Any] | None:
    metadata = message.get('metadata') if isinstance(message.get('metadata'), dict) else {}
    payload: dict[str, Any] = {}
    task_ids = _normalize_task_ids(metadata.get('task_ids'))
    if task_ids:
        payload['task_ids'] = task_ids
    source = str(metadata.get('source') or '').strip()
    if source:
        payload['source'] = source
    reason = str(metadata.get('reason') or '').strip()
    if reason:
        payload['reason'] = reason
    task_results = _normalize_task_results(metadata.get('task_results'))
    if task_results:
        payload['task_results'] = task_results
    return payload or None


def _compact_tool_trace_payload(message: dict[str, Any]) -> list[dict[str, str]]:
    tool_events = message.get('tool_events') if isinstance(message.get('tool_events'), list) else []
    summaries: list[dict[str, str]] = []
    for item in tool_events:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get('tool_name') or 'tool').strip() or 'tool'
        if tool_name == 'submit_next_stage':
            continue
        status = str(item.get('status') or '').strip().lower()
        if status not in {'success', 'error'}:
            continue
        text = summarize_preview_text(item.get('text') or '', max_chars=_RECENT_HISTORY_TOOL_TEXT_MAX_CHARS)
        if not text:
            continue
        summaries.append({'tool': tool_name, 'status': status, 'text': text})
    return summaries[-_RECENT_HISTORY_TOOL_TRACE_LIMIT:]


def _compact_stage_trace_payload(message: dict[str, Any]) -> dict[str, Any] | None:
    interaction_trace = message.get('interaction_trace') if isinstance(message.get('interaction_trace'), dict) else {}
    stages = [item for item in list(interaction_trace.get('stages') or []) if isinstance(item, dict)]
    if not stages:
        return None
    latest = stages[-1]
    payload: dict[str, Any] = {}
    status = str(latest.get('status') or '').strip()
    if status:
        payload['status'] = status
    stage_goal = summarize_preview_text(latest.get('stage_goal') or '', max_chars=_RECENT_HISTORY_STAGE_GOAL_MAX_CHARS)
    if stage_goal:
        payload['stage_goal'] = stage_goal
    tool_rounds_used = latest.get('tool_rounds_used')
    if isinstance(tool_rounds_used, int):
        payload['tool_rounds_used'] = tool_rounds_used
    return payload or None


def _history_content_from_message(message: dict[str, Any]) -> str:
    blocks: list[str] = []
    content = str(message.get('content') or '').strip()
    if content:
        blocks.append(content)
    task_meta = _compact_task_meta_payload(message)
    if task_meta is not None:
        lines = ['Task metadata:']
        task_ids = list(task_meta.get('task_ids') or [])
        if task_ids:
            lines.append(f"- task_ids: {', '.join(task_ids)}")
        if task_meta.get('source'):
            lines.append(f"- source: {task_meta['source']}")
        if task_meta.get('reason'):
            lines.append(f"- reason: {task_meta['reason']}")
        for result in list(task_meta.get('task_results') or [])[:2]:
            task_id = str(result.get('task_id') or '').strip()
            excerpt = summarize_preview_text(
                result.get('output_excerpt') or result.get('failure_reason') or result.get('check_result') or '',
                max_chars=_TASK_RESULT_REASON_MAX_CHARS,
            )
            if task_id and excerpt:
                lines.append(f"- {task_id}: {excerpt}")
        blocks.append('\n'.join(lines))
    tool_trace = _compact_tool_trace_payload(message)
    if tool_trace:
        lines = ['Recent tool results:']
        for item in tool_trace:
            lines.append(
                f"- {item.get('tool', 'tool')} ({item.get('status', 'info')}): {item.get('text', '')}"
            )
        blocks.append('\n'.join(lines))
    stage_trace = _compact_stage_trace_payload(message)
    if stage_trace is not None:
        lines = ['Stage snapshot:']
        if stage_trace.get('status'):
            lines.append(f"- status: {stage_trace['status']}")
        if stage_trace.get('stage_goal'):
            lines.append(f"- goal: {stage_trace['stage_goal']}")
        if stage_trace.get('tool_rounds_used') is not None:
            lines.append(f"- tool_rounds_used: {stage_trace['tool_rounds_used']}")
        blocks.append('\n'.join(lines))
    return '\n'.join(block for block in blocks if block).strip()


def _history_entry_from_message(message: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "role": str(message.get("role") or ""),
        "content": _history_content_from_message(message),
    }
    for key in ("tool_calls", "tool_call_id", "name"):
        if key in message:
            entry[key] = message[key]
    return entry


def transcript_messages(session: Any) -> list[dict[str, Any]]:
    return [
        item
        for item in list(getattr(session, 'messages', []) or [])
        if (
            isinstance(item, dict)
            and str(item.get('role') or '').strip().lower() in {'user', 'assistant'}
            and not is_internal_ceo_user_message(item)
        )
    ]


def extract_live_raw_tail_context(
    session: Any | None,
    *,
    turn_limit: int = DEFAULT_LIVE_RAW_TAIL_TURNS,
) -> tuple[list[dict[str, Any]], str]:
    if session is None:
        return [], ''
    messages = transcript_messages(session)
    normalized_turns = max(1, int(turn_limit or DEFAULT_LIVE_RAW_TAIL_TURNS))
    if not messages:
        return [], 'transcript'
    turn_start_indexes: list[int] = []
    current_user_index: int | None = None
    for index, message in enumerate(messages):
        role = str(message.get('role') or '').strip().lower()
        if role == 'user':
            current_user_index = index
            continue
        if role == 'assistant' and current_user_index is not None:
            turn_start_indexes.append(current_user_index)
            current_user_index = None
    if turn_start_indexes:
        start_index = (
            turn_start_indexes[-normalized_turns]
            if len(turn_start_indexes) >= normalized_turns
            else turn_start_indexes[0]
        )
    else:
        start_index = max(0, len(messages) - max(1, normalized_turns * 2))
    return [_history_entry_from_message(message) for message in messages[start_index:]], 'transcript'


def extract_live_raw_tail(
    session: Any,
    *,
    turn_limit: int = DEFAULT_LIVE_RAW_TAIL_TURNS,
) -> list[dict[str, Any]]:
    return extract_live_raw_tail_context(session, turn_limit=turn_limit)[0]


def extract_execution_live_raw_tail(
    runtime_session: Any | None,
    persisted_session: Any | None,
    *,
    turn_limit: int = DEFAULT_LIVE_RAW_TAIL_TURNS,
    require_active_stage: bool,
) -> tuple[list[dict[str, Any]], str]:
    snapshot, source = resolve_execution_snapshot(runtime_session, persisted_session)
    normalized_snapshot = _normalize_execution_snapshot(snapshot)
    if normalized_snapshot is not None:
        interaction_trace = normalize_interaction_trace(normalized_snapshot.get('interaction_trace'))
        has_active_stage = any(
            str(stage.get('status') or '').strip() == CEO_STAGE_STATUS_ACTIVE
            for stage in list(interaction_trace.get('stages') or [])
        )
        messages = _execution_snapshot_history_messages(normalized_snapshot)
        if messages and (has_active_stage or not require_active_stage):
            return messages, source
    if persisted_session is None:
        return [], ''
    return extract_live_raw_tail_context(persisted_session, turn_limit=turn_limit)


def extract_active_stage_raw_tail(
    runtime_session: Any | None,
    persisted_session: Any | None,
    *,
    turn_limit: int = DEFAULT_LIVE_RAW_TAIL_TURNS,
) -> list[dict[str, Any]]:
    return extract_execution_live_raw_tail(
        runtime_session,
        persisted_session,
        turn_limit=turn_limit,
        require_active_stage=True,
    )[0]


def latest_interaction_trace(runtime_session: Any | None, persisted_session: Any | None) -> tuple[dict[str, Any], str]:
    snapshot, source = resolve_execution_snapshot(runtime_session, persisted_session)
    normalized = normalize_interaction_trace((snapshot or {}).get('interaction_trace'))
    if normalized.get('stages'):
        return normalized, source
    if persisted_session is not None:
        for raw in reversed(transcript_messages(persisted_session)):
            normalized = normalize_interaction_trace(raw.get('interaction_trace'))
            if normalized.get('stages'):
                return normalized, 'transcript'
    return normalize_interaction_trace(None), ''


def build_completed_stage_abstracts(trace: Any, *, limit: int = 4) -> list[str]:
    normalized = normalize_interaction_trace(trace)
    results: list[str] = []
    for stage in list(normalized.get('stages') or []):
        if str(stage.get('status') or '').strip() == CEO_STAGE_STATUS_ACTIVE:
            continue
        stage_index = int(stage.get('stage_index') or len(results) + 1)
        stage_goal = summarize_preview_text(stage.get('stage_goal') or '', max_chars=160)
        status = str(stage.get('status') or '').strip() or 'completed'
        key_result = ''
        failure_reason = ''
        for round_item in reversed(list(stage.get('rounds') or [])):
            tools = list(round_item.get('tools') or [])
            for tool in reversed(tools):
                output_ref = str(tool.get('output_ref') or '').strip()
                output_text = summarize_preview_text(tool.get('output_text') or '', max_chars=180)
                tool_status = str(tool.get('status') or '').strip().lower()
                if tool_status == 'error' and not failure_reason:
                    failure_reason = output_text or output_ref
                if not key_result and (output_text or output_ref):
                    key_result = output_text or output_ref
                if key_result and failure_reason:
                    break
            if key_result and failure_reason:
                break
        lines = [
            f"Stage {stage_index}",
            f"- Goal: {stage_goal or '(unspecified)'}",
            f"- Status: {status}",
        ]
        if failure_reason:
            lines.append(f"- Failure reason: {failure_reason}")
        elif key_result:
            lines.append(f"- Key result: {key_result}")
        results.append('\n'.join(lines))
        if len(results) >= max(1, int(limit or 4)):
            break
    return results


def _complete_transcript_turns(session: Any) -> list[list[dict[str, Any]]]:
    turns: list[list[dict[str, Any]]] = []
    current_user: dict[str, Any] | None = None
    for raw in list(getattr(session, "messages", []) or []):
        if not isinstance(raw, dict):
            continue
        if is_internal_ceo_user_message(raw):
            continue
        role = str(raw.get("role") or "").strip().lower()
        if role == "user":
            current_user = raw
            continue
        if role == "assistant" and current_user is not None:
            turns.append([current_user, raw])
            current_user = None
    return turns


def count_frontdoor_turns(session: Any) -> int:
    return len(_complete_transcript_turns(session))


def workspace_path() -> Path:
    try:
        return Path(load_config().workspace_path).resolve()
    except Exception:
        return Path.cwd().resolve()


def new_web_ceo_session_id() -> str:
    return f"web:ceo-{uuid.uuid4().hex[:12]}"


def summarize_session_title(text: str, *, max_chars: int = 24) -> str:
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return DEFAULT_CEO_SESSION_TITLE
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 1].rstrip()}…"


def summarize_preview_text(text: str, *, max_chars: int = 96) -> str:
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return ""
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 1].rstrip()}…"


def _has_visible_message_content(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(_has_visible_message_content(item) for item in content)
    if isinstance(content, dict):
        return any(_has_visible_message_content(value) for value in content.values())
    return content is not None


def latest_llm_output_at(session: Any) -> str:
    for item in reversed(list(getattr(session, "messages", []) or [])):
        if str(item.get("role") or "").strip().lower() != "assistant":
            continue
        if not _has_visible_message_content(item.get("content")):
            continue
        timestamp = str(item.get("timestamp") or "").strip()
        if timestamp:
            return timestamp
    return ""


def main_runtime_depth_limits() -> dict[str, int]:
    try:
        cfg = load_config()
        default_max_depth = int(getattr(getattr(cfg, "main_runtime", None), "default_max_depth", DEFAULT_TASK_MAX_DEPTH) or DEFAULT_TASK_MAX_DEPTH)
        hard_max_depth = int(getattr(getattr(cfg, "main_runtime", None), "hard_max_depth", DEFAULT_TASK_HARD_MAX_DEPTH) or DEFAULT_TASK_HARD_MAX_DEPTH)
    except Exception:
        default_max_depth = DEFAULT_TASK_MAX_DEPTH
        hard_max_depth = DEFAULT_TASK_HARD_MAX_DEPTH
    default_max_depth = max(0, default_max_depth)
    hard_max_depth = max(default_max_depth, hard_max_depth)
    return {
        "default_max_depth": default_max_depth,
        "hard_max_depth": hard_max_depth,
    }


def normalize_task_defaults(
    payload: Any,
    *,
    default_max_depth: int,
    hard_max_depth: int,
) -> dict[str, int]:
    source = payload if isinstance(payload, dict) else {}
    raw_depth = source.get("max_depth", source.get("maxDepth", default_max_depth))
    try:
        max_depth = int(raw_depth)
    except (TypeError, ValueError):
        max_depth = int(default_max_depth)
    max_depth = max(0, min(max_depth, int(hard_max_depth)))
    return {"max_depth": max_depth}


def normalize_ceo_metadata(metadata: Any, *, session_key: str) -> dict[str, Any]:
    payload = dict(metadata or {}) if isinstance(metadata, dict) else {}
    payload.pop("frontdoor_context", None)
    title = str(payload.get("title") or "").strip() or DEFAULT_CEO_SESSION_TITLE
    preview_text = summarize_preview_text(payload.get("last_preview_text") or payload.get("preview_text") or "")
    manual_pause_waiting_reason = bool(payload.get("manual_pause_waiting_reason"))
    depth_limits = main_runtime_depth_limits()
    if str(session_key or "").startswith("web:"):
        memory_scope = normalize_memory_scope(
            payload.get("memory_scope"),
            fallback_channel=DEFAULT_WEB_MEMORY_SCOPE["channel"],
            fallback_chat_id=DEFAULT_WEB_MEMORY_SCOPE["chat_id"],
        )
    else:
        memory_scope = normalize_memory_scope(payload.get("memory_scope"), fallback_session_key=session_key)
    task_defaults = normalize_task_defaults(
        payload.get("task_defaults", payload.get("taskDefaults")),
        default_max_depth=depth_limits["default_max_depth"],
        hard_max_depth=depth_limits["hard_max_depth"],
    )
    last_task_memory = normalize_task_memory(payload.get('last_task_memory', payload.get('lastTaskMemory')))
    return {
        **payload,
        "title": title,
        "last_preview_text": preview_text,
        "memory_scope": memory_scope,
        "task_defaults": task_defaults,
        "manual_pause_waiting_reason": manual_pause_waiting_reason,
        'last_task_memory': last_task_memory,
    }


def ensure_ceo_session_metadata(session: Any) -> bool:
    normalized = normalize_ceo_metadata(getattr(session, "metadata", None), session_key=str(getattr(session, "key", "") or ""))
    current = getattr(session, "metadata", None)
    if current == normalized:
        return False
    session.metadata = normalized
    return True


def update_ceo_session_after_turn(
    session: Any,
    *,
    user_text: str,
    assistant_text: str,
    route_kind: str | None = None,
) -> bool:
    changed = ensure_ceo_session_metadata(session)
    metadata = dict(getattr(session, "metadata", {}) or {})
    if metadata.get("title") == DEFAULT_CEO_SESSION_TITLE and str(user_text or "").strip():
        next_title = summarize_session_title(user_text)
        if metadata.get("title") != next_title:
            metadata["title"] = next_title
            changed = True
    preview_source = str(assistant_text or "").strip() or str(user_text or "").strip()
    next_preview = summarize_preview_text(preview_source)
    if metadata.get("last_preview_text") != next_preview:
        metadata["last_preview_text"] = next_preview
        changed = True
    if 'frontdoor_context' in metadata:
        metadata.pop('frontdoor_context', None)
        changed = True
    next_task_memory = build_last_task_memory(session)
    if metadata.get('last_task_memory') != next_task_memory:
        metadata['last_task_memory'] = next_task_memory
        changed = True
    if changed:
        session.metadata = metadata
    return changed


def upload_dir_for_session(session_id: str, *, create: bool = True) -> Path:
    safe_session = safe_filename(str(session_id or "web_shared").replace(":", "_")) or "web_shared"
    path = workspace_path() / WEB_CEO_UPLOAD_ROOT / safe_session
    return ensure_dir(path) if create else path


def inflight_snapshot_path_for_session(session_id: str, *, create: bool = True) -> Path:
    safe_session = safe_filename(str(session_id or "web_shared").replace(":", "_")) or "web_shared"
    root = workspace_path() / WEB_CEO_INFLIGHT_ROOT
    directory = ensure_dir(root) if create else root
    return directory / f"{safe_session}.json"


def paused_execution_context_path_for_session(session_id: str, *, create: bool = True) -> Path:
    safe_session = safe_filename(str(session_id or "web_shared").replace(":", "_")) or "web_shared"
    root = workspace_path() / WEB_CEO_PAUSED_ROOT
    directory = ensure_dir(root) if create else root
    return directory / f"{safe_session}.json"


def is_restorable_inflight_turn_snapshot(snapshot: Any) -> bool:
    if not isinstance(snapshot, dict) or not snapshot:
        return False
    status = str(snapshot.get("status") or "").strip().lower()
    if not status:
        return True
    return status in {"running", "paused"}


def read_inflight_turn_snapshot(session_id: str) -> dict[str, Any] | None:
    path = inflight_snapshot_path_for_session(session_id, create=False)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if is_restorable_inflight_turn_snapshot(payload) else None


def write_inflight_turn_snapshot(session_id: str, snapshot: dict[str, Any] | None) -> None:
    key = str(session_id or "").strip()
    if not key:
        return
    path = inflight_snapshot_path_for_session(key)
    if not isinstance(snapshot, dict) or not snapshot:
        path.unlink(missing_ok=True)
        return
    payload = dict(snapshot)
    payload["session_id"] = key
    payload["persisted_at"] = datetime.now().isoformat()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_inflight_turn_snapshot(session_id: str) -> None:
    path = inflight_snapshot_path_for_session(session_id, create=False)
    path.unlink(missing_ok=True)


def read_paused_execution_context(session_id: str) -> dict[str, Any] | None:
    path = paused_execution_context_path_for_session(session_id, create=False)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if is_restorable_inflight_turn_snapshot(payload) else None


def write_paused_execution_context(session_id: str, snapshot: dict[str, Any] | None) -> None:
    key = str(session_id or "").strip()
    if not key:
        return
    path = paused_execution_context_path_for_session(key)
    if not isinstance(snapshot, dict) or not snapshot:
        path.unlink(missing_ok=True)
        return
    payload = dict(snapshot)
    payload["session_id"] = key
    payload["persisted_at"] = datetime.now().isoformat()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_paused_execution_context(session_id: str) -> None:
    path = paused_execution_context_path_for_session(session_id, create=False)
    path.unlink(missing_ok=True)


def _decode_inflight_session_id(path: Path, snapshot: dict[str, Any] | None = None) -> str:
    payload = snapshot if isinstance(snapshot, dict) else {}
    raw = str(payload.get("session_id") or payload.get("session_key") or "").strip()
    if raw.startswith("web:"):
        return raw
    stem = str(path.stem or "").strip()
    if stem.startswith("web_"):
        return f"web:{stem[4:]}"
    return ""


def list_inflight_web_ceo_sessions() -> dict[str, dict[str, Any]]:
    root = workspace_path() / WEB_CEO_INFLIGHT_ROOT
    if not root.exists():
        return {}
    items: dict[str, dict[str, Any]] = {}
    for path in root.glob("*.json"):
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not is_restorable_inflight_turn_snapshot(snapshot):
            continue
        session_id = _decode_inflight_session_id(path, snapshot)
        if not session_id:
            continue
        items[session_id] = snapshot
    return items


def _inflight_user_message(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    user_message = snapshot.get("user_message")
    return user_message if isinstance(user_message, dict) else None


def _inflight_preview_text(snapshot: dict[str, Any] | None) -> str:
    user_message = _inflight_user_message(snapshot)
    if user_message is not None:
        preview = summarize_preview_text(user_message.get("content") or "")
        if preview:
            return preview
    assistant_preview = summarize_preview_text((snapshot or {}).get("assistant_text") or "")
    if assistant_preview:
        return assistant_preview
    for item in reversed(list((snapshot or {}).get("tool_events") or [])):
        if not isinstance(item, dict):
            continue
        preview = summarize_preview_text(item.get("text") or "")
        if preview:
            return preview
    return ""


def _inflight_updated_at(snapshot: dict[str, Any] | None) -> str:
    if not isinstance(snapshot, dict):
        return ""
    candidates: list[str] = []
    user_message = _inflight_user_message(snapshot)
    if user_message is not None:
        timestamp = str(user_message.get("timestamp") or "").strip()
        if timestamp:
            candidates.append(timestamp)
    for item in list(snapshot.get("tool_events") or []):
        if not isinstance(item, dict):
            continue
        timestamp = str(item.get("timestamp") or "").strip()
        if timestamp:
            candidates.append(timestamp)
    persisted_at = str(snapshot.get("persisted_at") or "").strip()
    if persisted_at:
        candidates.append(persisted_at)
    return max(candidates) if candidates else ""


def build_session_summary(
    session: Any,
    *,
    is_active: bool,
    is_running: bool = False,
    inflight_turn: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_ceo_session_metadata(session)
    messages = [
        item
        for item in list(getattr(session, "messages", []) or [])
        if isinstance(item, dict) and not is_internal_ceo_user_message(item)
    ]
    preview_text = str(session.metadata.get("last_preview_text") or "").strip()
    if not preview_text:
        for item in reversed(messages):
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                preview_text = summarize_preview_text(content)
                break
    inflight_preview = _inflight_preview_text(inflight_turn)
    if inflight_preview:
        preview_text = inflight_preview
    created_at = getattr(session, "created_at", None)
    updated_at = getattr(session, "updated_at", None)
    inflight_updated_at = _inflight_updated_at(inflight_turn)
    updated_at_text = updated_at.isoformat() if isinstance(updated_at, datetime) else str(updated_at or "")
    if inflight_updated_at and inflight_updated_at > updated_at_text:
        updated_at_text = inflight_updated_at
    title = str(session.metadata.get("title") or DEFAULT_CEO_SESSION_TITLE)
    if title == DEFAULT_CEO_SESSION_TITLE:
        user_message = _inflight_user_message(inflight_turn)
        if user_message is not None:
            candidate_title = summarize_session_title(user_message.get("content") or "")
            if candidate_title:
                title = candidate_title
    message_count = len(messages)
    if _inflight_user_message(inflight_turn) is not None:
        message_count += 1
    last_llm_output = latest_llm_output_at(session)
    return {
        "session_id": str(getattr(session, "key", "") or ""),
        "title": title,
        "preview_text": preview_text,
        "message_count": message_count,
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else str(created_at or ""),
        "updated_at": updated_at_text,
        "last_llm_output_at": last_llm_output,
        "is_active": bool(is_active),
        "is_running": bool(is_running),
        "task_defaults": dict(session.metadata.get("task_defaults") or {}),
        "session_family": "local",
        "session_origin": "web",
        "is_readonly": False,
        "can_rename": True,
        "can_delete": True,
    }


CHINA_SESSION_CHANNEL_SPECS = (
    {"attr": "qqbot", "channel_id": "qqbot", "label": "QQ Bot"},
    {"attr": "dingtalk", "channel_id": "dingtalk", "label": "DingTalk"},
    {"attr": "wecom", "channel_id": "wecom", "label": "企业微信"},
    {"attr": "wecom_app", "channel_id": "wecom-app", "label": "企业微信应用"},
    {"attr": "feishu_china", "channel_id": "feishu-china", "label": "飞书"},
)

CHINA_CHANNEL_LABELS = {
    spec["channel_id"]: spec["label"]
    for spec in CHINA_SESSION_CHANNEL_SPECS
}


def _non_empty(value: Any) -> bool:
    return value not in (None, "", [], {}, False)


def _channel_session_kind(parsed) -> str:
    if parsed is None:
        return "dm"
    return "thread" if parsed.thread_id else parsed.chat_type


def _channel_label(channel_id: str) -> str:
    return CHINA_CHANNEL_LABELS.get(str(channel_id or "").strip(), str(channel_id or "渠道").strip() or "渠道")


def _channel_title(parsed) -> str:
    label = _channel_label(parsed.channel)
    if parsed.thread_id:
        return f"{label} · {parsed.account_id} · Thread · {str(parsed.thread_id).strip()[:24]}"
    if parsed.chat_type == "group":
        peer = str(parsed.peer_id or "group").strip()[:24]
        return f"{label} · {parsed.account_id} · Group · {peer}"
    return f"{label} · {parsed.account_id} · DM"


def _channel_preview_text(session: Any, *, fallback_text: str = "") -> str:
    messages = [
        item
        for item in list(getattr(session, "messages", []) or [])
        if isinstance(item, dict) and not is_internal_ceo_user_message(item)
    ]
    for item in reversed(messages):
        content = item.get("content") if isinstance(item, dict) else ""
        preview = summarize_preview_text(content or "")
        if preview:
            return preview
    return str(fallback_text or "").strip()


def _session_time_value(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "").strip()


def _session_created_at(session: Any) -> str:
    return _session_time_value(getattr(session, "created_at", None))


def _session_updated_at(session: Any) -> str:
    updated_at = _session_time_value(getattr(session, "updated_at", None))
    if updated_at:
        return updated_at
    messages = [
        item
        for item in list(getattr(session, "messages", []) or [])
        if isinstance(item, dict) and not is_internal_ceo_user_message(item)
    ]
    for item in reversed(messages):
        if isinstance(item, dict) and str(item.get("timestamp") or "").strip():
            return str(item.get("timestamp") or "").strip()
    return ""


def _session_last_assistant_at(session: Any) -> str:
    return latest_llm_output_at(session)


def _canonical_china_session_id(parsed) -> str:
    if parsed.chat_type == "group":
        return build_session_key(
            channel=parsed.channel,
            account_id=parsed.account_id,
            peer_kind="group",
            peer_id=str(parsed.peer_id or ""),
            thread_id=parsed.thread_id,
        )
    return build_session_key(
        channel=parsed.channel,
        account_id=parsed.account_id,
        peer_kind="user",
        peer_id=str(parsed.peer_id or ""),
        thread_id=parsed.thread_id,
    )


def _top_level_channel_payload(channel_cfg: Any) -> dict[str, Any]:
    if channel_cfg is None:
        return {}
    if hasattr(channel_cfg, "model_dump"):
        data = channel_cfg.model_dump(by_alias=True, exclude_none=True)
        return data if isinstance(data, dict) else {}
    if isinstance(channel_cfg, dict):
        return dict(channel_cfg)
    return {}


def _has_base_channel_account(channel_cfg: Any) -> bool:
    payload = _top_level_channel_payload(channel_cfg)
    ignore = {"enabled", "name", "defaultAccount", "default_account", "accounts"}
    return any(key not in ignore and _non_empty(value) for key, value in payload.items())


def _iter_enabled_channel_accounts() -> list[dict[str, str]]:
    cfg = load_config()
    rows: list[dict[str, str]] = []
    channels_cfg = getattr(getattr(cfg, "china_bridge", None), "channels", None)
    if channels_cfg is None:
        return rows
    for spec in CHINA_SESSION_CHANNEL_SPECS:
        channel_cfg = getattr(channels_cfg, spec["attr"], None)
        payload = _top_level_channel_payload(channel_cfg)
        if not bool(payload.get("enabled")):
            continue
        accounts = payload.get("accounts") if isinstance(payload.get("accounts"), dict) else {}
        seen_accounts: set[str] = set()
        if _has_base_channel_account(channel_cfg):
            rows.append(
                {
                    "channel_id": spec["channel_id"],
                    "label": spec["label"],
                    "account_id": "default",
                }
            )
            seen_accounts.add("default")
        for account_id, account_payload in sorted(accounts.items()):
            if not isinstance(account_payload, dict):
                continue
            normalized_account_id = str(account_id or "").strip() or "default"
            if normalized_account_id in seen_accounts:
                continue
            if account_payload.get("enabled") is False:
                continue
            rows.append(
                {
                    "channel_id": spec["channel_id"],
                    "label": spec["label"],
                    "account_id": normalized_account_id,
                }
            )
            seen_accounts.add(normalized_account_id)
        if not rows or rows[-1]["channel_id"] != spec["channel_id"]:
            if not accounts:
                rows.append(
                    {
                        "channel_id": spec["channel_id"],
                        "label": spec["label"],
                        "account_id": "default",
                    }
                )
    return rows


def _channel_session_summary_from_entry(
    *,
    session_id: str,
    parsed,
    is_active: bool,
    is_running: bool,
    preview_text: str,
    message_count: int,
    created_at: str,
    updated_at: str,
    last_llm_output_at: str,
    is_virtual: bool,
) -> dict[str, Any]:
    kind = _channel_session_kind(parsed)
    return {
        "session_id": session_id,
        "title": _channel_title(parsed),
        "preview_text": preview_text,
        "message_count": message_count,
        "created_at": created_at,
        "updated_at": updated_at,
        "last_llm_output_at": last_llm_output_at,
        "is_active": bool(is_active),
        "is_running": bool(is_running),
        "session_family": "channel",
        "session_origin": "china",
        "is_readonly": True,
        "can_rename": False,
        "can_delete": False,
        "channel_id": parsed.channel,
        "account_id": parsed.account_id,
        "chat_type": kind,
        "peer_id": parsed.peer_id,
        "thread_id": parsed.thread_id,
        "is_virtual": bool(is_virtual),
    }


def list_local_ceo_sessions(
    session_manager: Any,
    *,
    active_session_id: str,
    is_running_resolver: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    changed_keys: list[str] = []
    persisted_keys = {
        str(item.get("key") or "").strip()
        for item in session_manager.list_sessions()
        if str(item.get("key") or "").strip().startswith("web:")
    }
    inflight_by_session = list_inflight_web_ceo_sessions()
    session_keys = persisted_keys | set(inflight_by_session.keys())
    for key in sorted(session_keys):
        if not key.startswith("web:"):
            continue
        try:
            session = session_manager.get_or_create(key)
        except Exception:
            continue
        if ensure_ceo_session_metadata(session):
            if key in persisted_keys:
                changed_keys.append(key)
        is_running = False
        if callable(is_running_resolver):
            try:
                is_running = bool(is_running_resolver(key))
            except Exception:
                is_running = False
        rows.append(
            build_session_summary(
                session,
                is_active=key == active_session_id,
                is_running=is_running,
                inflight_turn=inflight_by_session.get(key),
            )
        )
    for key in changed_keys:
        session_manager.save(session_manager.get_or_create(key))
    rows.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("session_id") or "")), reverse=True)
    return rows


def list_channel_ceo_sessions(
    session_manager: Any,
    *,
    active_session_id: str,
    is_running_resolver: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}

    for account in _iter_enabled_channel_accounts():
        session_id = f"china:{account['channel_id']}:{account['account_id']}:dm"
        parsed = parse_china_session_key(session_id)
        if parsed is None:
            continue
        summaries[session_id] = _channel_session_summary_from_entry(
            session_id=session_id,
            parsed=parsed,
            is_active=session_id == active_session_id,
            is_running=bool(callable(is_running_resolver) and is_running_resolver(session_id)),
            preview_text="等待该渠道的私聊消息",
            message_count=0,
            created_at="",
            updated_at="",
            last_llm_output_at="",
            is_virtual=True,
        )

    for item in session_manager.list_sessions():
        key = str(item.get("key") or "").strip()
        if not key.startswith("china:"):
            continue
        parsed = parse_china_session_key(key)
        if parsed is None:
            continue
        session = session_manager.get_or_create(key)
        canonical_id = _canonical_china_session_id(parsed)
        canonical_parsed = parse_china_session_key(canonical_id) or parsed
        visible_messages = [
            entry
            for entry in list(getattr(session, "messages", []) or [])
            if isinstance(entry, dict) and not is_internal_ceo_user_message(entry)
        ]
        summary = _channel_session_summary_from_entry(
            session_id=canonical_id,
            parsed=canonical_parsed,
            is_active=canonical_id == active_session_id,
            is_running=bool(callable(is_running_resolver) and is_running_resolver(canonical_id)),
            preview_text=_channel_preview_text(session),
            message_count=len(visible_messages),
            created_at=_session_created_at(session),
            updated_at=_session_updated_at(session),
            last_llm_output_at=_session_last_assistant_at(session),
            is_virtual=False,
        )
        existing = summaries.get(canonical_id)
        if existing is None:
            summaries[canonical_id] = summary
            continue
        existing_updated_at = str(existing.get("updated_at") or existing.get("last_llm_output_at") or "")
        summary_updated_at = str(summary.get("updated_at") or summary.get("last_llm_output_at") or "")
        merged = dict(existing)
        merged["message_count"] = int(existing.get("message_count") or 0) + int(summary.get("message_count") or 0)
        merged["is_virtual"] = bool(existing.get("is_virtual")) and bool(summary.get("is_virtual"))
        merged["is_running"] = bool(existing.get("is_running")) or bool(summary.get("is_running"))
        if summary_updated_at >= existing_updated_at:
            for key_name in ("preview_text", "created_at", "updated_at", "last_llm_output_at"):
                merged[key_name] = summary.get(key_name) or merged.get(key_name)
        summaries[canonical_id] = merged

    rows = list(summaries.values())
    rows = [
        item
        for item in rows
        if item.get("chat_type") == "dm"
        or int(item.get("message_count") or 0) > 0
        or not bool(item.get("is_virtual"))
    ]
    rows.sort(
        key=lambda item: (
            0 if str(item.get("chat_type") or "") == "dm" else 1,
            str(item.get("last_llm_output_at") or item.get("updated_at") or item.get("created_at") or ""),
            str(item.get("session_id") or ""),
        ),
        reverse=True,
    )
    return rows


def build_ceo_session_catalog(
    session_manager: Any,
    *,
    active_session_id: str,
    is_running_resolver: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    local_items = list_local_ceo_sessions(
        session_manager,
        active_session_id=active_session_id,
        is_running_resolver=is_running_resolver,
    )
    channel_items = list_channel_ceo_sessions(
        session_manager,
        active_session_id=active_session_id,
        is_running_resolver=is_running_resolver,
    )
    grouped: dict[str, dict[str, Any]] = {}
    for item in channel_items:
        channel_id = str(item.get("channel_id") or "").strip()
        if not channel_id:
            continue
        bucket = grouped.get(channel_id)
        if bucket is None:
            bucket = {
                "channel_id": channel_id,
                "label": _channel_label(channel_id),
                "items": [],
            }
            grouped[channel_id] = bucket
        bucket["items"].append(item)
    channel_groups = [grouped[key] for key in [spec["channel_id"] for spec in CHINA_SESSION_CHANNEL_SPECS] if key in grouped]
    channel_ids = {str(item.get("session_id") or "") for item in channel_items}
    active_family = "channel" if str(active_session_id or "") in channel_ids else "local"
    return {
        "items": local_items,
        "channel_groups": channel_groups,
        "active_session_id": str(active_session_id or "").strip(),
        "active_session_family": active_family,
        "_channel_items": channel_items,
    }


def find_ceo_session_catalog_item(catalog: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    target = str(session_id or "").strip()
    if not target:
        return None
    for item in list(catalog.get("items") or []):
        if str(item.get("session_id") or "").strip() == target:
            return item
    for item in list(catalog.get("_channel_items") or []):
        if str(item.get("session_id") or "").strip() == target:
            return item
    return None


class WebCeoStateStore:
    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace or workspace_path()).resolve()
        self.path = ensure_dir(self.workspace / ".g3ku") / "web-ceo-state.json"

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def write(self, *, active_session_id: str | None) -> dict[str, Any]:
        payload = {
            "active_session_id": str(active_session_id or "").strip(),
            "updated_at": datetime.now().isoformat(),
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def get_active_session_id(self) -> str:
        return str(self.read().get("active_session_id") or "").strip()

    def set_active_session_id(self, session_id: str | None) -> dict[str, Any]:
        return self.write(active_session_id=session_id)


def create_web_ceo_session(session_manager: Any, *, session_id: str | None = None, title: str | None = None) -> Any:
    key = str(session_id or "").strip() or new_web_ceo_session_id()
    session = session_manager.get_or_create(key)
    ensure_ceo_session_metadata(session)
    next_title = str(title or "").strip()
    if next_title:
        session.metadata["title"] = next_title
    session.updated_at = datetime.now()
    session_manager.save(session)
    return session


def delete_web_ceo_session_artifacts(*, session_manager: Any, session_id: str) -> None:
    path = session_manager.get_path(session_id)
    if path.exists():
        path.unlink()
    session_manager.invalidate(session_id)
    clear_inflight_turn_snapshot(session_id)
    clear_paused_execution_context(session_id)
    upload_dir = upload_dir_for_session(session_id, create=False)
    if upload_dir.exists():
        shutil.rmtree(upload_dir, ignore_errors=True)


def list_web_ceo_sessions(
    session_manager: Any,
    *,
    active_session_id: str,
    is_running_resolver: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    return list_local_ceo_sessions(
        session_manager,
        active_session_id=active_session_id,
        is_running_resolver=is_running_resolver,
    )


def resolve_active_ceo_session_id(session_manager: Any, state_store: WebCeoStateStore) -> str:
    requested = state_store.get_active_session_id()
    catalog = build_ceo_session_catalog(session_manager, active_session_id=requested)
    if find_ceo_session_catalog_item(catalog, requested) is not None:
        return requested
    local_items = list(catalog.get("items") or [])
    if local_items:
        fallback = str(local_items[0].get("session_id") or "").strip()
        state_store.set_active_session_id(fallback)
        return fallback
    created = create_web_ceo_session(session_manager)
    state_store.set_active_session_id(created.key)
    return str(created.key)


def ensure_active_web_ceo_session(session_manager: Any, state_store: WebCeoStateStore) -> str:
    active_session_id = state_store.get_active_session_id()
    catalog = build_ceo_session_catalog(session_manager, active_session_id=active_session_id)
    local_items = list(catalog.get("items") or [])
    available_ids = [str(item.get("session_id") or "").strip() for item in local_items if str(item.get("session_id") or "").strip()]
    if active_session_id and active_session_id in available_ids:
        return active_session_id
    if available_ids:
        fallback = available_ids[0]
        state_store.set_active_session_id(fallback)
        return fallback
    created = create_web_ceo_session(session_manager)
    state_store.set_active_session_id(created.key)
    return str(created.key)
