from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from g3ku.org_graph.integration.web_bridge import get_org_graph_service
from g3ku.org_graph.protocol import build_envelope

router = APIRouter()


@router.websocket('/ws/ceo')
async def ceo_websocket(websocket: WebSocket):
    await websocket.accept()
    session_id = str(websocket.query_params.get('session_id') or 'web:shared')
    service = get_org_graph_service()
    queue = await service.registry.subscribe_ceo(session_id)

    async def sender() -> None:
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)

    sender_task = asyncio.create_task(sender())
    try:
        await websocket.send_json(build_envelope(channel='ceo', session_id=session_id, type='hello', data={'session_id': session_id}))
        while True:
            data = await websocket.receive_json()
            if str(data.get('type') or '') != 'client.user_message':
                continue
            text = str(data.get('text') or '').strip()
            if not text:
                continue
            reply = await service.handle_ceo_message(session_id, text)
            await websocket.send_json(build_envelope(channel='ceo', session_id=session_id, type='ceo.reply.final', data={'text': reply}))
    except WebSocketDisconnect:
        pass
    finally:
        sender_task.cancel()
        await asyncio.gather(sender_task, return_exceptions=True)
        await service.registry.unsubscribe_ceo(session_id, queue)

