from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from g3ku.shells.web import get_agent
from main.protocol import build_envelope

router = APIRouter()


@router.websocket('/ws/tasks/{task_id}')
async def task_websocket(websocket: WebSocket, task_id: str):
    await websocket.accept()
    requested_session_id = str(websocket.query_params.get('session_id') or '').strip()
    after_seq = int(websocket.query_params.get('after_seq') or 0)
    agent = get_agent()
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        await websocket.send_json(build_envelope(channel='task', session_id=requested_session_id or 'web:shared', task_id=task_id, seq=after_seq, type='error', data={'code': 'task_service_unavailable'}))
        await websocket.close(code=4503)
        return
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    payload = service.get_task_detail_payload(task_id, mark_read=False)
    if payload is None:
        await websocket.send_json(build_envelope(channel='task', session_id=requested_session_id or 'web:shared', task_id=task_id, seq=after_seq, type='error', data={'code': 'task_not_found'}))
        await websocket.close(code=4404)
        return
    session_id = requested_session_id or str(payload.get('task', {}).get('session_id') or 'web:shared')
    queue = await service.registry.subscribe_global_task(task_id)

    async def sender() -> None:
        while True:
            item = await queue.get()
            await websocket.send_json(item)

    sender_task = asyncio.create_task(sender())
    try:
        await websocket.send_json(build_envelope(channel='task', session_id=session_id, task_id=task_id, seq=after_seq, type='hello', data={'task_id': task_id, 'session_id': session_id}))
        await websocket.send_json(build_envelope(channel='task', session_id=session_id, task_id=task_id, seq=after_seq, type='snapshot.task', data=payload))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        sender_task.cancel()
        await asyncio.gather(sender_task, return_exceptions=True)
        await service.registry.unsubscribe_global_task(task_id, queue)
