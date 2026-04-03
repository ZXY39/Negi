from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from g3ku.shells.web import ensure_web_runtime_services, get_agent, get_web_heartbeat_service
from main.protocol import now_iso
from main.service.task_event_callback import normalize_task_event_payload
from main.service.task_stall_callback import normalize_task_stall_payload
from main.service.task_terminal_callback import (
    normalize_task_terminal_payload,
    resolve_task_terminal_callback_token,
)

router = APIRouter()


@router.post('/internal/task-event')
async def post_task_event_callback(
    payload: dict,
    x_g3ku_internal_token: str | None = Header(default=None, alias='x-g3ku-internal-token'),
):
    expected_token = resolve_task_terminal_callback_token()
    if expected_token and str(x_g3ku_internal_token or '').strip() != expected_token:
        raise HTTPException(status_code=403, detail='internal_callback_forbidden')

    normalized = normalize_task_event_payload(payload)
    if not normalized:
        raise HTTPException(status_code=400, detail='task_event_payload_invalid')

    agent = get_agent()
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        raise HTTPException(status_code=503, detail='main_task_service_unavailable')

    await ensure_web_runtime_services(agent)
    accepted = bool(getattr(service, 'forward_live_task_event', lambda _payload: False)(normalized))
    return {'ok': True, 'accepted': accepted, 'event_type': normalized.get('event_type')}


@router.post('/internal/task-event-batch')
async def post_task_event_batch_callback(
    payload: dict,
    x_g3ku_internal_token: str | None = Header(default=None, alias='x-g3ku-internal-token'),
):
    expected_token = resolve_task_terminal_callback_token()
    if expected_token and str(x_g3ku_internal_token or '').strip() != expected_token:
        raise HTTPException(status_code=403, detail='internal_callback_forbidden')

    raw_items = list(payload.get('items') or []) if isinstance(payload, dict) else []
    if not raw_items:
        raise HTTPException(status_code=400, detail='task_event_batch_payload_invalid')

    normalized_items: list[dict[str, object]] = []
    for item in raw_items:
        normalized = normalize_task_event_payload(item if isinstance(item, dict) else None)
        if not normalized or str(normalized.get('event_type') or '').strip() != 'task.summary.patch':
            raise HTTPException(status_code=400, detail='task_event_batch_payload_invalid')
        normalized_items.append(normalized)

    agent = get_agent()
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        raise HTTPException(status_code=503, detail='main_task_service_unavailable')

    await ensure_web_runtime_services(agent)
    accepted = 0
    for normalized in normalized_items:
        if bool(getattr(service, 'forward_live_task_event', lambda _payload: False)(normalized)):
            accepted += 1
    return {'ok': True, 'accepted': accepted, 'items': len(normalized_items)}


@router.post('/internal/task-terminal')
async def post_task_terminal_callback(
    payload: dict,
    x_g3ku_internal_token: str | None = Header(default=None, alias='x-g3ku-internal-token'),
):
    expected_token = resolve_task_terminal_callback_token()
    if expected_token and str(x_g3ku_internal_token or '').strip() != expected_token:
        raise HTTPException(status_code=403, detail='internal_callback_forbidden')

    normalized = normalize_task_terminal_payload(payload)
    if not normalized:
        raise HTTPException(status_code=400, detail='task_terminal_payload_invalid')

    agent = get_agent()
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        raise HTTPException(status_code=503, detail='main_task_service_unavailable')

    await ensure_web_runtime_services(agent)
    heartbeat = get_web_heartbeat_service(agent)
    if heartbeat is None:
        raise HTTPException(status_code=503, detail='web_heartbeat_unavailable')

    dedupe_key = str(normalized.get('dedupe_key') or '').strip()
    entry = service.store.put_task_terminal_outbox(
        dedupe_key=dedupe_key,
        task_id=str(normalized.get('task_id') or '').strip(),
        session_id=str(normalized.get('session_id') or '').strip() or 'web:shared',
        created_at=str(normalized.get('finished_at') or now_iso()).strip() or now_iso(),
        payload=normalized,
    )
    if str(entry.get('delivery_state') or '').strip().lower() == 'delivered':
        return {
            'ok': True,
            'duplicate': True,
            'accepted': False,
            'dedupe_key': dedupe_key,
            'rejected_reason': 'already_delivered',
        }

    accepted = heartbeat.enqueue_task_terminal_payload(normalized)
    rejected_reason = ''
    if not accepted:
        reason_getter = getattr(heartbeat, 'task_terminal_rejection_reason', None)
        if callable(reason_getter):
            try:
                rejected_reason = str(reason_getter(dedupe_key) or '').strip()
            except Exception:
                rejected_reason = ''
    mark_enqueue_result = getattr(service.store, 'mark_task_terminal_outbox_enqueue_result', None)
    if callable(mark_enqueue_result) and dedupe_key:
        mark_enqueue_result(
            dedupe_key,
            accepted=accepted,
            rejected_reason=rejected_reason,
            updated_at=now_iso(),
        )
    if not accepted:
        return {
            'ok': True,
            'duplicate': True,
            'accepted': False,
            'dedupe_key': dedupe_key,
            'rejected_reason': rejected_reason,
        }
    return {
        'ok': True,
        'duplicate': False,
        'accepted': True,
        'dedupe_key': dedupe_key,
        'rejected_reason': '',
    }


@router.post('/internal/task-stall')
async def post_task_stall_callback(
    payload: dict,
    x_g3ku_internal_token: str | None = Header(default=None, alias='x-g3ku-internal-token'),
):
    expected_token = resolve_task_terminal_callback_token()
    if expected_token and str(x_g3ku_internal_token or '').strip() != expected_token:
        raise HTTPException(status_code=403, detail='internal_callback_forbidden')

    normalized = normalize_task_stall_payload(payload)
    if not normalized:
        raise HTTPException(status_code=400, detail='task_stall_payload_invalid')

    agent = get_agent()
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        raise HTTPException(status_code=503, detail='main_task_service_unavailable')

    await ensure_web_runtime_services(agent)
    heartbeat = get_web_heartbeat_service(agent)
    if heartbeat is None:
        raise HTTPException(status_code=503, detail='web_heartbeat_unavailable')

    dedupe_key = str(normalized.get('dedupe_key') or '').strip()
    entry = service.store.put_task_stall_outbox(
        dedupe_key=dedupe_key,
        task_id=str(normalized.get('task_id') or '').strip(),
        session_id=str(normalized.get('session_id') or '').strip() or 'web:shared',
        created_at=str(normalized.get('last_visible_output_at') or now_iso()).strip() or now_iso(),
        payload=normalized,
    )
    if str(entry.get('delivery_state') or '').strip().lower() == 'delivered':
        return {'ok': True, 'duplicate': True, 'dedupe_key': dedupe_key}

    accepted = heartbeat.enqueue_task_stall_payload(normalized)
    if not accepted:
        return {'ok': True, 'duplicate': True, 'dedupe_key': dedupe_key}
    return {'ok': True, 'duplicate': False, 'dedupe_key': dedupe_key}
