from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from g3ku.org_graph.integration.web_bridge import get_org_graph_service
from g3ku.org_graph.protocol import build_envelope

router = APIRouter()


@router.websocket('/ws/projects/{project_id}')
async def project_websocket(websocket: WebSocket, project_id: str):
    await websocket.accept()
    session_id = str(websocket.query_params.get('session_id') or 'web:shared')
    after_seq = int(websocket.query_params.get('after_seq') or 0)
    service = get_org_graph_service()
    project = service.get_project(project_id)
    if project is None or project.session_id != session_id:
        await websocket.send_json(build_envelope(channel='project', session_id=session_id, project_id=project_id, type='error', data={'code': 'project_not_found'}))
        await websocket.close(code=4404)
        return
    queue = await service.registry.subscribe_project(session_id, project_id)

    async def sender() -> None:
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)

    sender_task = asyncio.create_task(sender())
    try:
        await websocket.send_json(build_envelope(channel='project', session_id=session_id, project_id=project_id, seq=after_seq, type='hello', data={'project_id': project_id}))
        await websocket.send_json(build_envelope(channel='project', session_id=session_id, project_id=project_id, seq=after_seq, type='snapshot.project', data=project.model_dump(mode='json')))
        tree = service.get_tree(project_id)
        if tree is not None:
            await websocket.send_json(build_envelope(channel='project', session_id=session_id, project_id=project_id, seq=after_seq, type='snapshot.tree', data=tree.model_dump(mode='json')))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        sender_task.cancel()
        await asyncio.gather(sender_task, return_exceptions=True)
        await service.registry.unsubscribe_project(session_id, project_id, queue)

