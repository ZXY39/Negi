from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query

from g3ku.shells.web import get_agent

router = APIRouter()



def _service():
    agent = get_agent()
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        raise HTTPException(status_code=503, detail='main_task_service_unavailable')
    return service


@router.post('/tasks')
async def create_task(payload: dict = Body(...)):
    service = _service()
    record = await service.create_task(
        str(payload.get('task') or '').strip(),
        session_id=str(payload.get('session_id') or 'web:shared').strip() or 'web:shared',
        max_depth=payload.get('max_depth'),
        title=payload.get('title'),
        metadata=payload.get('metadata') if isinstance(payload.get('metadata'), dict) else None,
    )
    return {'ok': True, 'task': record.model_dump(mode='json')}


@router.get('/tasks/summary')
async def tasks_summary(session_id: str = Query('web:shared')):
    service = _service()
    await service.startup()
    summary = service.query_service.summary(session_id)
    return {'ok': True, **summary.model_dump(mode='json')}


@router.get('/tasks')
async def list_tasks(session_id: str = Query('web:shared'), scope: int = Query(1)):
    service = _service()
    await service.startup()
    items = service.query_service.get_tasks(session_id, int(scope))
    return {
        'ok': True,
        'scope': int(scope),
        'items': [item.model_dump(mode='json') for item in items],
    }


@router.get('/tasks/{task_id}')
async def get_task(task_id: str, mark_read: bool = Query(False)):
    service = _service()
    await service.startup()
    payload = service.get_task_detail_payload(task_id, mark_read=bool(mark_read))
    if payload is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, **payload}


@router.post('/tasks/{task_id}/pause')
async def pause_task(task_id: str):
    service = _service()
    record = await service.pause_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, 'task': record.model_dump(mode='json')}


@router.post('/tasks/{task_id}/resume')
async def resume_task(task_id: str):
    service = _service()
    record = await service.resume_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, 'task': record.model_dump(mode='json')}


@router.post('/tasks/{task_id}/cancel')
async def cancel_task(task_id: str):
    service = _service()
    record = await service.cancel_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, 'task': record.model_dump(mode='json')}


@router.get('/tasks/{task_id}/tree')
async def task_tree(task_id: str, mark_read: bool = Query(False)):
    service = _service()
    await service.startup()
    payload = service.get_task_detail_payload(task_id, mark_read=bool(mark_read))
    if payload is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    progress = payload['progress']
    return {'ok': True, **progress}


@router.get('/tasks/{task_id}/artifacts')
async def list_artifacts(task_id: str):
    service = _service()
    await service.startup()
    items = service.list_artifacts(task_id)
    return {'ok': True, 'items': [item.model_dump(mode='json') for item in items]}


@router.get('/tasks/{task_id}/artifacts/{artifact_id}')
async def get_artifact(task_id: str, artifact_id: str):
    service = _service()
    await service.startup()
    artifact = service.get_artifact(artifact_id)
    if artifact is None or artifact.task_id != task_id:
        raise HTTPException(status_code=404, detail='artifact_not_found')
    content = Path(artifact.path).read_text(encoding='utf-8') if artifact.path and Path(artifact.path).exists() else ''
    return {'ok': True, 'artifact': artifact.model_dump(mode='json'), 'content': content}


@router.post('/tasks/{task_id}/artifacts/{artifact_id}/apply')
async def apply_patch_artifact(task_id: str, artifact_id: str):
    service = _service()
    try:
        result = await service.apply_patch_artifact(task_id, artifact_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail='artifact_not_found')
    return {'ok': True, **result}


@router.get('/monitor/summary')
async def monitor_summary(session_id: str = Query('web:shared')):
    service = _service()
    await service.startup()
    summary = service.query_service.summary(session_id)
    return {'ok': True, **summary.model_dump(mode='json')}


@router.get('/monitor/tasks')
async def monitor_tasks(session_id: str = Query('web:shared'), scope: int = Query(1)):
    service = _service()
    await service.startup()
    items = service.query_service.get_tasks(session_id, int(scope))
    return {'ok': True, 'scope': int(scope), 'items': [item.model_dump(mode='json') for item in items]}


@router.get('/monitor/tasks/{task_id}')
async def monitor_task_progress(task_id: str, mark_read_progress: bool = Query(False)):
    service = _service()
    await service.startup()
    payload = service.get_task_detail_payload(task_id, mark_read=bool(mark_read_progress))
    if payload is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, **payload}
