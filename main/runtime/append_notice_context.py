from __future__ import annotations

import json
from typing import Any

APPEND_NOTICE_CONTEXT_KEY = 'append_notice_context'
APPEND_NOTICE_TAIL_PREFIX = '[G3KU_APPEND_NOTICE_TAIL_V1]'


def normalize_append_notice_context(payload: Any) -> dict[str, Any]:
    current = dict(payload or {}) if isinstance(payload, dict) else {}
    notice_records: list[dict[str, Any]] = []
    seen_notice_ids: set[str] = set()
    for item in list(current.get('notice_records') or []):
        if not isinstance(item, dict):
            continue
        notification_id = str(item.get('notification_id') or '').strip()
        if not notification_id or notification_id in seen_notice_ids:
            continue
        seen_notice_ids.add(notification_id)
        notice_records.append(
            {
                'notification_id': notification_id,
                'epoch_id': str(item.get('epoch_id') or '').strip(),
                'source_node_id': str(item.get('source_node_id') or '').strip(),
                'message': str(item.get('message') or '').strip(),
                'consumed_at': str(item.get('consumed_at') or '').strip(),
                'compression_stage_id': str(item.get('compression_stage_id') or '').strip(),
            }
        )
    compression_segments: list[dict[str, Any]] = []
    seen_segment_ids: set[str] = set()
    for item in list(current.get('compression_segments') or []):
        if not isinstance(item, dict):
            continue
        compression_stage_id = str(item.get('compression_stage_id') or '').strip()
        if not compression_stage_id or compression_stage_id in seen_segment_ids:
            continue
        seen_segment_ids.add(compression_stage_id)
        notice_ids = [
            str(notice_id or '').strip()
            for notice_id in list(item.get('notice_ids') or [])
            if str(notice_id or '').strip()
        ]
        compression_segments.append(
            {
                'compression_stage_id': compression_stage_id,
                'created_at': str(item.get('created_at') or '').strip(),
                'notice_ids': notice_ids,
                'notice_count': max(0, int(item.get('notice_count') or len(notice_ids))),
                'summary_text': str(item.get('summary_text') or '').strip(),
            }
        )
    return {
        'notice_records': notice_records,
        'compression_segments': compression_segments,
    }


def record_consumed_notifications(
    context: Any,
    *,
    notifications: list[dict[str, Any]],
    consumed_at: str,
) -> dict[str, Any]:
    normalized = normalize_append_notice_context(context)
    existing_ids = {str(item.get('notification_id') or '').strip() for item in list(normalized.get('notice_records') or [])}
    for item in list(notifications or []):
        if not isinstance(item, dict):
            continue
        notification_id = str(item.get('notification_id') or '').strip()
        if not notification_id or notification_id in existing_ids:
            continue
        existing_ids.add(notification_id)
        normalized['notice_records'].append(
            {
                'notification_id': notification_id,
                'epoch_id': str(item.get('epoch_id') or '').strip(),
                'source_node_id': str(item.get('source_node_id') or '').strip(),
                'message': str(item.get('message') or '').strip(),
                'consumed_at': str(item.get('consumed_at') or consumed_at or '').strip(),
                'compression_stage_id': '',
            }
        )
    return normalized


def roll_append_notice_context_for_compression_stage(
    context: Any,
    *,
    compression_stage_id: str,
    created_at: str,
) -> dict[str, Any]:
    normalized = normalize_append_notice_context(context)
    normalized_stage_id = str(compression_stage_id or '').strip()
    if not normalized_stage_id:
        return normalized
    if any(str(item.get('compression_stage_id') or '').strip() == normalized_stage_id for item in list(normalized.get('compression_segments') or [])):
        return normalized
    uncovered = [
        item
        for item in list(normalized.get('notice_records') or [])
        if not str(item.get('compression_stage_id') or '').strip()
        and str(item.get('message') or '').strip()
    ]
    if not uncovered:
        return normalized
    summary_lines = []
    for item in uncovered:
        consumed_at = str(item.get('consumed_at') or '').strip()
        message = str(item.get('message') or '').strip()
        if not message:
            continue
        if consumed_at:
            summary_lines.append(f'- [{consumed_at}] {message}')
        else:
            summary_lines.append(f'- {message}')
    summary_text = '\n'.join(summary_lines).strip()
    normalized['compression_segments'].append(
        {
            'compression_stage_id': normalized_stage_id,
            'created_at': str(created_at or '').strip(),
            'notice_ids': [str(item.get('notification_id') or '').strip() for item in uncovered],
            'notice_count': len(uncovered),
            'summary_text': summary_text,
        }
    )
    next_notice_records: list[dict[str, Any]] = []
    covered_ids = {str(item.get('notification_id') or '').strip() for item in uncovered}
    for item in list(normalized.get('notice_records') or []):
        payload = dict(item or {})
        if str(payload.get('notification_id') or '').strip() in covered_ids:
            payload['compression_stage_id'] = normalized_stage_id
        next_notice_records.append(payload)
    normalized['notice_records'] = next_notice_records
    return normalized


def build_append_notice_tail_messages(
    context: Any,
    *,
    visible_user_messages: list[str] | None = None,
) -> list[dict[str, Any]]:
    normalized = normalize_append_notice_context(context)
    visible_texts = {str(item or '').strip() for item in list(visible_user_messages or []) if str(item or '').strip()}
    messages: list[dict[str, Any]] = []
    for segment in list(normalized.get('compression_segments') or []):
        payload = {
            'kind': 'compressed_notice_window',
            'compression_stage_id': str(segment.get('compression_stage_id') or '').strip(),
            'notice_count': int(segment.get('notice_count') or 0),
            'summary_text': str(segment.get('summary_text') or '').strip(),
        }
        messages.append(
            {
                'role': 'assistant',
                'content': f'{APPEND_NOTICE_TAIL_PREFIX}\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}',
            }
        )
    raw_notices = [
        {
            'notification_id': str(item.get('notification_id') or '').strip(),
            'epoch_id': str(item.get('epoch_id') or '').strip(),
            'source_node_id': str(item.get('source_node_id') or '').strip(),
            'message': str(item.get('message') or '').strip(),
            'consumed_at': str(item.get('consumed_at') or '').strip(),
        }
        for item in list(normalized.get('notice_records') or [])
        if not str(item.get('compression_stage_id') or '').strip()
        and str(item.get('message') or '').strip()
        and str(item.get('message') or '').strip() not in visible_texts
    ]
    if raw_notices:
        payload = {
            'kind': 'raw_notice_window',
            'notice_count': len(raw_notices),
            'notices': raw_notices,
        }
        messages.append(
            {
                'role': 'assistant',
                'content': f'{APPEND_NOTICE_TAIL_PREFIX}\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}',
            }
        )
    return messages


__all__ = [
    'APPEND_NOTICE_CONTEXT_KEY',
    'APPEND_NOTICE_TAIL_PREFIX',
    'build_append_notice_tail_messages',
    'normalize_append_notice_context',
    'record_consumed_notifications',
    'roll_append_notice_context_for_compression_stage',
]
