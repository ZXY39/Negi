from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from g3ku.shells.web import ensure_web_runtime_services, get_agent, get_web_heartbeat_service
from main.protocol import now_iso
from main.service.task_terminal_callback import (
    normalize_task_terminal_payload,
    resolve_task_terminal_callback_token,
)

router = APIRouter()


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
        return {'ok': True, 'duplicate': True, 'dedupe_key': dedupe_key}

    accepted = heartbeat.enqueue_task_terminal_payload(normalized)
    if not accepted:
        return {'ok': True, 'duplicate': True, 'dedupe_key': dedupe_key}
    return {'ok': True, 'duplicate': False, 'dedupe_key': dedupe_key}
