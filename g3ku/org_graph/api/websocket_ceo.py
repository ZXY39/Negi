from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from g3ku.core.events import AgentEvent
from g3ku.org_graph.integration.web_bridge import get_org_graph_service
from g3ku.org_graph.protocol import build_envelope
from g3ku.shells.web import get_agent, get_runtime_manager

router = APIRouter()


def _coerce_event_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get('data')
    return data if isinstance(data, dict) else {}


def _should_forward_tool_event(*, session_id: str, event: AgentEvent) -> bool:
    _ = session_id
    if event.type not in {'tool_execution_start', 'tool_execution_end'}:
        return False
    payload = event.payload if isinstance(event.payload, dict) else {}
    data = _coerce_event_data(payload)
    parent_session_id = str(data.get('parent_session_id') or '').strip()
    current_session_id = str(data.get('current_session_id') or '').strip()
    return not parent_session_id and not current_session_id


def _serialize_tool_event(event: AgentEvent) -> dict[str, Any] | None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    tool_name = str(payload.get('tool_name') or 'tool').strip() or 'tool'
    text = str(payload.get('text') or '').strip()
    is_error = bool(payload.get('is_error'))
    if event.type == 'tool_execution_start':
        status = 'running'
    elif event.type == 'tool_execution_end':
        status = 'error' if is_error else 'success'
    else:
        return None
    return {
        'status': status,
        'tool_name': tool_name,
        'text': text,
        'timestamp': event.timestamp,
        'tool_call_id': str(payload.get('tool_call_id') or ''),
        'is_error': is_error,
    }


@router.websocket('/ws/ceo')
async def ceo_websocket(websocket: WebSocket):
    await websocket.accept()
    session_id = str(websocket.query_params.get('session_id') or 'web:shared')
    agent = get_agent()
    runtime_manager = get_runtime_manager(agent)
    service = get_org_graph_service()
    await service.startup()
    queue = await service.registry.subscribe_ceo(session_id)
    stream_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    if ':' in session_id:
        default_channel, default_chat_id = session_id.split(':', 1)
    else:
        default_channel, default_chat_id = 'web', session_id
    session = runtime_manager.get_or_create(
        session_key=session_id,
        channel=default_channel or 'web',
        chat_id=default_chat_id or 'shared',
    )

    async def _safe_send(payload: dict[str, Any]) -> None:
        await websocket.send_json(payload)

    async def sender(source_queue: asyncio.Queue[dict[str, Any]]) -> None:
        while True:
            payload = await source_queue.get()
            await _safe_send(payload)

    async def relay_session_event(event: AgentEvent) -> None:
        if not _should_forward_tool_event(session_id=session_id, event=event):
            return
        serialized = _serialize_tool_event(event)
        if serialized is None:
            return
        try:
            await stream_queue.put(
                build_envelope(
                    channel='ceo',
                    session_id=session_id,
                    type='ceo.agent.tool',
                    data=serialized,
                )
            )
        except RuntimeError:
            return

    unsubscribe = session.subscribe(relay_session_event)
    sender_task = asyncio.create_task(sender(queue))
    stream_task = asyncio.create_task(sender(stream_queue))
    try:
        await _safe_send(build_envelope(channel='ceo', session_id=session_id, type='hello', data={'session_id': session_id}))
        while True:
            data = await websocket.receive_json()
            if str(data.get('type') or '') != 'client.user_message':
                continue
            text = str(data.get('text') or '').strip()
            if not text:
                continue
            result = await runtime_manager.prompt(
                text,
                session_key=session_id,
                channel=default_channel or 'web',
                chat_id=default_chat_id or 'shared',
            )
            reply = str(result.output or '')
            await _safe_send(build_envelope(channel='ceo', session_id=session_id, type='ceo.reply.final', data={'text': reply}))
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe()
        sender_task.cancel()
        stream_task.cancel()
        await asyncio.gather(sender_task, stream_task, return_exceptions=True)
        await service.registry.unsubscribe_ceo(session_id, queue)
