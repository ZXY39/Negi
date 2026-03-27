from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from g3ku.shells.web import get_agent, is_no_ceo_model_configured_error

router = APIRouter()


def _service():
    try:
        agent = get_agent()
    except Exception as exc:
        if not is_no_ceo_model_configured_error(exc):
            raise
        raise HTTPException(status_code=503, detail='no_model_configured') from exc
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        raise HTTPException(status_code=503, detail='main_task_service_unavailable')
    return service


def _ensure_task_route_id(task_id: str) -> str:
    if task_id == 'summary':
        raise HTTPException(status_code=404, detail='task_not_found')
    return task_id


@router.get('/tasks')
async def list_tasks(session_id: str = Query('web:shared'), scope: int = Query(1)):
    service = _service()
    await service.startup()
    normalized_session_id = str(session_id or 'web:shared').strip() or 'web:shared'
    effective_session_id = None if normalized_session_id.lower() == 'all' else normalized_session_id
    items = service.query_service.get_tasks(effective_session_id, int(scope))
    return {
        'ok': True,
        'session_id': 'all' if effective_session_id is None else effective_session_id,
        'scope': int(scope),
        'items': [item.model_dump(mode='json') for item in items],
        **service.worker_status_payload(),
    }


@router.get('/tasks/{task_id}')
async def get_task(task_id: str, mark_read: bool = Query(False)):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    payload = service.get_task_detail_payload(task_id, mark_read=bool(mark_read))
    if payload is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, **payload}


@router.get('/tasks/{task_id}/nodes/{node_id}')
async def get_task_node_detail(task_id: str, node_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    payload = service.get_node_detail_payload(task_id, node_id)
    if payload is None:
        raise HTTPException(status_code=404, detail='node_not_found')
    return payload


@router.post('/tasks/{task_id}/pause')
async def pause_task(task_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    task_id = service.normalize_task_id(task_id)
    try:
        record = await service.pause_task(task_id)
    except ValueError as exc:
        detail = str(exc)
        status_code = 503 if detail == 'task_worker_offline' else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    if record is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, 'task': record.model_dump(mode='json')}


@router.post('/tasks/{task_id}/resume')
async def resume_task(task_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    task_id = service.normalize_task_id(task_id)
    try:
        record = await service.resume_task(task_id)
    except ValueError as exc:
        detail = str(exc)
        status_code = 503 if detail == 'task_worker_offline' else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    if record is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, 'task': record.model_dump(mode='json')}


@router.post('/tasks/{task_id}/retry')
async def retry_task(task_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    task_id = service.normalize_task_id(task_id)
    try:
        record = await service.retry_task(task_id)
    except ValueError as exc:
        detail = str(exc)
        if detail == 'task_not_failed':
            raise HTTPException(status_code=409, detail=detail) from exc
        if detail == 'task_worker_offline':
            raise HTTPException(status_code=503, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    if record is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, 'task': record.model_dump(mode='json')}


@router.post('/tasks/{task_id}/cancel')
async def cancel_task(task_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    task_id = service.normalize_task_id(task_id)
    try:
        record = await service.cancel_task(task_id)
    except ValueError as exc:
        detail = str(exc)
        status_code = 503 if detail == 'task_worker_offline' else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    if record is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, 'task': record.model_dump(mode='json')}


@router.delete('/tasks/{task_id}')
async def delete_task(task_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    try:
        record = await service.delete_task(task_id)
    except ValueError as exc:
        detail = str(exc)
        if detail in {'task_not_paused', 'task_still_stopping'}:
            raise HTTPException(status_code=409, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    if record is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, 'deleted': True, 'task': record.model_dump(mode='json')}


@router.get('/tasks/{task_id}/artifacts')
async def list_artifacts(task_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    items = service.list_artifacts(task_id)
    return {'ok': True, 'items': [item.model_dump(mode='json') for item in items]}


@router.get('/tasks/{task_id}/artifacts/{artifact_id}')
async def get_artifact(
    task_id: str,
    artifact_id: str,
    full: bool = Query(False),
    start_line: int | None = Query(None),
    end_line: int | None = Query(None),
    around_line: int | None = Query(None),
    window: int | None = Query(None),
):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    artifact = service.get_artifact(artifact_id)
    if artifact is None or artifact.task_id != task_id:
        raise HTTPException(status_code=404, detail='artifact_not_found')
    ref = f'artifact:{artifact.artifact_id}'
    content = ''
    excerpt = None
    if full:
        content = Path(artifact.path).read_text(encoding='utf-8') if artifact.path and Path(artifact.path).exists() else ''
    else:
        excerpt = service.open_content(
            ref=ref,
            start_line=start_line,
            end_line=end_line,
            around_line=around_line,
            window=window,
        )
        content = str(excerpt.get('excerpt') or '')
    return {'ok': True, 'artifact': artifact.model_dump(mode='json'), 'content': content, 'excerpt': excerpt}


@router.get('/content/describe')
async def describe_content(ref: str | None = Query(None), path: str | None = Query(None)):
    service = _service()
    await service.startup()
    return {'ok': True, **service.describe_content(ref=ref, path=path)}


@router.get('/content/search')
async def search_content(
    query: str = Query(...),
    ref: str | None = Query(None),
    path: str | None = Query(None),
    limit: int = Query(10),
    before: int = Query(2),
    after: int = Query(2),
):
    service = _service()
    await service.startup()
    return {'ok': True, **service.search_content(query=query, ref=ref, path=path, limit=limit, before=before, after=after)}


@router.get('/content/open')
async def open_content(
    ref: str | None = Query(None),
    path: str | None = Query(None),
    start_line: int | None = Query(None),
    end_line: int | None = Query(None),
    around_line: int | None = Query(None),
    window: int | None = Query(None),
):
    service = _service()
    await service.startup()
    return {
        'ok': True,
        **service.open_content(
            ref=ref,
            path=path,
            start_line=start_line,
            end_line=end_line,
            around_line=around_line,
            window=window,
        ),
    }


@router.post('/tasks/{task_id}/artifacts/{artifact_id}/apply')
async def apply_patch_artifact(task_id: str, artifact_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    task_id = service.normalize_task_id(task_id)
    try:
        result = await service.apply_patch_artifact(task_id, artifact_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail='artifact_not_found')
    return {'ok': True, **result}
