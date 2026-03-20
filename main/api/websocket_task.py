from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from g3ku.shells.web import get_agent
from main.api.websocket_utils import (
    WebSocketChannelClosed,
    websocket_close,
    websocket_receive_text,
    websocket_send_json,
)
from main.protocol import build_envelope

router = APIRouter()


def _service():
    agent = get_agent()
    service = getattr(agent, 'main_task_service', None)
    return service


def _worker_status_payload(service) -> dict[str, object]:
    return {
        'worker': service.latest_worker_status(),
        'worker_online': service.is_worker_online(),
    }


def _worker_status_signature(payload: dict[str, object]) -> tuple[str, str, bool]:
    worker = payload.get('worker') if isinstance(payload, dict) else None
    worker_id = str((worker or {}).get('worker_id') or '').strip() if isinstance(worker, dict) else ''
    worker_state = str((worker or {}).get('status') or (worker or {}).get('state') or '').strip() if isinstance(worker, dict) else ''
    return worker_id, worker_state, payload.get('worker_online') is not False


@router.websocket('/ws/tasks')
async def tasks_websocket(websocket: WebSocket):
    await websocket.accept()
    requested_session_id = str(websocket.query_params.get('session_id') or 'all').strip() or 'all'
    after_seq = int(websocket.query_params.get('after_seq') or 0)
    service = _service()
    try:
        if service is None:
            await websocket_send_json(
                websocket,
                build_envelope(
                    channel='task',
                    session_id=requested_session_id,
                    seq=after_seq,
                    type='error',
                    data={'code': 'task_service_unavailable'},
                ),
            )
            await websocket_close(websocket, code=4503)
            return
        await service.startup()
        effective_session_id = None if requested_session_id.lower() == 'all' else requested_session_id
        current_seq = max(after_seq, service.store.latest_task_event_seq(session_id=effective_session_id))
        snapshot = [item.model_dump(mode='json') for item in service.query_service.get_tasks(effective_session_id, 1)]
        worker_payload = _worker_status_payload(service)
        worker_signature = _worker_status_signature(worker_payload)
        await websocket_send_json(
            websocket,
            build_envelope(
                channel='task',
                session_id=requested_session_id,
                seq=current_seq,
                type='hello',
                data={'session_id': requested_session_id},
            ),
        )
        await websocket_send_json(
            websocket,
            build_envelope(
                channel='task',
                session_id=requested_session_id,
                seq=current_seq,
                type='task.list.snapshot',
                data={
                    'items': snapshot,
                    **worker_payload,
                },
            ),
        )
        while True:
            await websocket_receive_text(websocket, timeout=0.25)
            events = service.store.list_task_events(
                after_seq=current_seq,
                session_id=None if requested_session_id.lower() == 'all' else requested_session_id,
                limit=100,
            )
            for event in events:
                current_seq = max(current_seq, int(event.get('seq') or 0))
                event_type = str(event.get('event_type') or '')
                if event_type not in {'task.list.patch', 'task.deleted'}:
                    continue
                await websocket_send_json(
                    websocket,
                    build_envelope(
                        channel='task',
                        session_id=str(event.get('session_id') or requested_session_id),
                        task_id=str(event.get('task_id') or '') or None,
                        seq=int(event.get('seq') or 0),
                        type=event_type,
                        data=dict(event.get('payload') or {}),
                    )
                )
            next_worker_payload = _worker_status_payload(service)
            next_worker_signature = _worker_status_signature(next_worker_payload)
            if next_worker_signature != worker_signature:
                worker_signature = next_worker_signature
                await websocket_send_json(
                    websocket,
                    build_envelope(
                        channel='task',
                        session_id=requested_session_id,
                        seq=current_seq,
                        type='task.worker.status',
                        data=next_worker_payload,
                    ),
                )
    except (WebSocketDisconnect, WebSocketChannelClosed):
        logger.debug(
            'task-list ws disconnected: session_id={} after_seq={}',
            requested_session_id,
            after_seq,
        )
        return


@router.websocket('/ws/tasks/{task_id}')
async def task_websocket(websocket: WebSocket, task_id: str):
    await websocket.accept()
    requested_session_id = str(websocket.query_params.get('session_id') or '').strip()
    after_seq = int(websocket.query_params.get('after_seq') or 0)
    service = _service()
    try:
        if service is None:
            await websocket_send_json(
                websocket,
                build_envelope(
                    channel='task',
                    session_id=requested_session_id or 'web:shared',
                    task_id=task_id,
                    seq=after_seq,
                    type='error',
                    data={'code': 'task_service_unavailable'},
                ),
            )
            await websocket_close(websocket, code=4503)
            return
        await service.startup()
        task_id = service.normalize_task_id(task_id)
        current_seq = max(after_seq, service.store.latest_task_event_seq(task_id=task_id))
        payload = service.get_task_detail_payload(task_id, mark_read=False)
        if payload is None:
            await websocket_send_json(
                websocket,
                build_envelope(
                    channel='task',
                    session_id=requested_session_id or 'web:shared',
                    task_id=task_id,
                    seq=after_seq,
                    type='error',
                    data={'code': 'task_not_found'},
                ),
            )
            await websocket_close(websocket, code=4404)
            return
        session_id = requested_session_id or str(payload.get('task', {}).get('session_id') or 'web:shared')
        await websocket_send_json(
            websocket,
            build_envelope(
                channel='task',
                session_id=session_id,
                task_id=task_id,
                seq=current_seq,
                type='hello',
                data={'task_id': task_id, 'session_id': session_id},
            ),
        )
        await websocket_send_json(
            websocket,
            build_envelope(
                channel='task',
                session_id=session_id,
                task_id=task_id,
                seq=current_seq,
                type='task.snapshot',
                data=payload,
            ),
        )
        while True:
            await websocket_receive_text(websocket, timeout=0.25)
            events = service.store.list_task_events(after_seq=current_seq, task_id=task_id, limit=100)
            for event in events:
                current_seq = max(current_seq, int(event.get('seq') or 0))
                await websocket_send_json(
                    websocket,
                    build_envelope(
                        channel='task',
                        session_id=str(event.get('session_id') or session_id),
                        task_id=task_id,
                        seq=int(event.get('seq') or 0),
                        type=str(event.get('event_type') or ''),
                        data=dict(event.get('payload') or {}),
                    )
                )
    except (WebSocketDisconnect, WebSocketChannelClosed):
        logger.debug('task-detail ws disconnected: {}', task_id)
        return
