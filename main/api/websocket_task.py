from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from g3ku.security import get_bootstrap_security_service
from g3ku.shells.web import get_agent, is_no_ceo_model_configured_error, no_ceo_model_configured_payload
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
    return service.worker_status_payload()


def _load_task_list_snapshot_state(service, *, requested_session_id: str, effective_session_id: str | None, after_seq: int) -> tuple[list[dict[str, object]], dict[str, object], int, int]:
    snapshot: list[dict[str, object]] = []
    worker_payload: dict[str, object] = {}
    boundary_seq = service.registry.current_task_list_seq(requested_session_id)
    for _ in range(3):
        candidate_boundary = service.registry.current_task_list_seq(requested_session_id)
        snapshot = [item.model_dump(mode='json') for item in service.query_service.get_tasks(effective_session_id, 1)]
        worker_payload = _worker_status_payload(service)
        confirm_seq = service.registry.current_task_list_seq(requested_session_id)
        boundary_seq = candidate_boundary
        if confirm_seq == candidate_boundary:
            boundary_seq = confirm_seq
            break
    return snapshot, worker_payload, max(int(after_seq or 0), int(boundary_seq or 0)), int(boundary_seq or 0)


def _load_task_detail_snapshot_state(service, *, task_id: str, after_seq: int) -> tuple[dict[str, object] | None, int, int]:
    payload: dict[str, object] | None = None
    boundary_seq = service.registry.current_global_task_seq(task_id)
    for _ in range(3):
        candidate_boundary = service.registry.current_global_task_seq(task_id)
        payload = service.get_task_detail_payload(task_id, mark_read=False)
        confirm_seq = service.registry.current_global_task_seq(task_id)
        boundary_seq = candidate_boundary
        if confirm_seq == candidate_boundary:
            boundary_seq = confirm_seq
            break
    return payload, max(int(after_seq or 0), int(boundary_seq or 0)), int(boundary_seq or 0)


async def _queue_sender(
    websocket: WebSocket,
    queue,
    *,
    min_seq: int,
) -> None:
    while True:
        payload = await queue.get()
        try:
            seq = int(payload.get('seq') or 0)
        except Exception:
            seq = 0
        if seq <= int(min_seq or 0):
            continue
        await websocket_send_json(websocket, payload)


@router.websocket('/ws/tasks')
async def tasks_websocket(websocket: WebSocket):
    await websocket.accept()
    requested_session_id = str(websocket.query_params.get('session_id') or 'all').strip() or 'all'
    after_seq = int(websocket.query_params.get('after_seq') or 0)
    service = None
    queue = None
    sender_task = None
    if not get_bootstrap_security_service().is_unlocked():
        await websocket_send_json(
            websocket,
            build_envelope(
                channel='task',
                session_id=requested_session_id,
                seq=after_seq,
                type='error',
                data={'code': 'project_locked'},
            ),
        )
        await websocket_close(websocket, code=4423)
        return
    try:
        service = _service()
    except Exception as exc:
        if not is_no_ceo_model_configured_error(exc):
            raise
        await websocket_send_json(
            websocket,
            build_envelope(
                channel='task',
                session_id=requested_session_id,
                seq=after_seq,
                type='error',
                data=no_ceo_model_configured_payload(),
            ),
        )
        await websocket_close(websocket, code=4503)
        return
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
        queue = await service.registry.subscribe_task_list(requested_session_id)
        snapshot, worker_payload, current_seq, boundary_seq = _load_task_list_snapshot_state(
            service,
            requested_session_id=requested_session_id,
            effective_session_id=effective_session_id,
            after_seq=after_seq,
        )
        sender_task = asyncio.create_task(
            _queue_sender(websocket, queue, min_seq=boundary_seq),
            name=f'task-list-ws-sender:{requested_session_id}',
        )
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
            await websocket_receive_text(websocket)
    except (WebSocketDisconnect, WebSocketChannelClosed):
        logger.debug(
            'task-list ws disconnected: session_id={} after_seq={}',
            requested_session_id,
            after_seq,
        )
        return
    finally:
        if sender_task is not None:
            sender_task.cancel()
            await asyncio.gather(sender_task, return_exceptions=True)
        if service is not None and queue is not None:
            await service.registry.unsubscribe_task_list(requested_session_id, queue)


@router.websocket('/ws/tasks/{task_id}')
async def task_websocket(websocket: WebSocket, task_id: str):
    await websocket.accept()
    requested_session_id = str(websocket.query_params.get('session_id') or '').strip()
    after_seq = int(websocket.query_params.get('after_seq') or 0)
    service = None
    queue = None
    sender_task = None
    if not get_bootstrap_security_service().is_unlocked():
        await websocket_send_json(
            websocket,
            build_envelope(
                channel='task',
                session_id=requested_session_id or 'web:shared',
                task_id=task_id,
                seq=after_seq,
                type='error',
                data={'code': 'project_locked'},
            ),
        )
        await websocket_close(websocket, code=4423)
        return
    try:
        service = _service()
    except Exception as exc:
        if not is_no_ceo_model_configured_error(exc):
            raise
        await websocket_send_json(
            websocket,
            build_envelope(
                channel='task',
                session_id=requested_session_id or 'web:shared',
                task_id=task_id,
                seq=after_seq,
                type='error',
                data=no_ceo_model_configured_payload(),
            ),
        )
        await websocket_close(websocket, code=4503)
        return
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
        queue = await service.registry.subscribe_global_task(task_id)
        payload, current_seq, boundary_seq = _load_task_detail_snapshot_state(
            service,
            task_id=task_id,
            after_seq=after_seq,
        )
        if payload is None:
            await service.registry.unsubscribe_global_task(task_id, queue)
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
        sender_task = asyncio.create_task(
            _queue_sender(websocket, queue, min_seq=boundary_seq),
            name=f'task-detail-ws-sender:{task_id}',
        )
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
            await websocket_receive_text(websocket)
    except (WebSocketDisconnect, WebSocketChannelClosed):
        logger.debug('task-detail ws disconnected: {}', task_id)
        return
    finally:
        if sender_task is not None:
            sender_task.cancel()
            await asyncio.gather(sender_task, return_exceptions=True)
        if service is not None and queue is not None:
            await service.registry.unsubscribe_global_task(task_id, queue)
