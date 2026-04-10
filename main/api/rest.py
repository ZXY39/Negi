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


def _task_control_error_status(detail: str) -> int:
    return 503 if detail in {'task_worker_offline', 'task_worker_starting', 'task_worker_stale'} else 400


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


@router.get('/tasks/worker-status')
async def get_task_worker_status():
    service = _service()
    await service.startup()
    return {'ok': True, **service.worker_status_payload()}


@router.get('/tasks/{task_id}')
async def get_task(
    task_id: str,
    mark_read: bool = Query(False),
):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    payload = service.get_task_detail_payload(task_id, mark_read=bool(mark_read))
    if payload is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, **payload}


@router.get('/tasks/{task_id}/tree-snapshot')
async def get_task_tree_snapshot(task_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    payload = service.get_task_tree_snapshot_payload(task_id)
    if payload is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return payload


@router.get('/tasks/{task_id}/nodes/{node_id}')
async def get_task_node_detail(
    task_id: str,
    node_id: str,
    detail_level: str = Query('summary'),
):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    payload = service.node_detail(task_id, node_id, detail_level=detail_level)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=404, detail='node_not_found')
    return payload


@router.get('/tasks/{task_id}/nodes/{node_id}/latest-context')
async def get_task_node_latest_context(task_id: str, node_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    payload = service.get_node_latest_context_payload(task_id, node_id)
    if payload is None:
        raise HTTPException(status_code=404, detail='node_not_found')
    return payload


@router.get('/tasks/{task_id}/nodes/{node_id}/tree-subtree')
async def get_task_node_tree_subtree(
    task_id: str,
    node_id: str,
    round_id: str | None = Query(None),
):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    payload = service.get_task_tree_subtree_payload(
        task_id,
        node_id,
        round_id=round_id,
    )
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
        raise HTTPException(status_code=_task_control_error_status(detail), detail=detail) from exc
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
        raise HTTPException(status_code=_task_control_error_status(detail), detail=detail) from exc
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
        if detail in {'task_not_failed', 'task_not_retryable'}:
            raise HTTPException(status_code=409, detail=detail) from exc
        if detail in {'task_worker_offline', 'task_worker_starting', 'task_worker_stale'}:
            raise HTTPException(status_code=503, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    if record is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, 'task': record.model_dump(mode='json')}


@router.post('/tasks/{task_id}/continue-evaluate')
async def continue_evaluate_task(task_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    task_id = service.normalize_task_id(task_id)
    try:
        payload = await service.continue_evaluate_task(task_id)
    except ValueError as exc:
        detail = str(exc)
        if detail == 'task_not_unpassed':
            raise HTTPException(status_code=409, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    if payload is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    continuation_task = payload.get('continuation_task')
    return {
        'ok': True,
        'decision': str(payload.get('decision') or '').strip(),
        'reply_text': str(payload.get('reply_text') or ''),
        'task': (payload.get('task').model_dump(mode='json') if payload.get('task') is not None else None),
        'continuation_task': (continuation_task.model_dump(mode='json') if continuation_task is not None else None),
    }


@router.post('/tasks/{task_id}/continue')
async def continue_task(task_id: str, payload: dict):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    task_id = service.normalize_task_id(task_id)
    request_payload = dict(payload or {})
    request_payload['target_task_id'] = task_id
    try:
        result = await service.continue_task(**request_payload)
    except ValueError as exc:
        detail = str(exc)
        if detail in {'task_not_failed', 'task_not_retryable', 'task_not_unpassed'}:
            raise HTTPException(status_code=409, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    if result is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    continuation_task = result.get('continuation_task')
    resumed_task = result.get('resumed_task')
    target_task = result.get('target_task')
    return {
        'ok': True,
        'status': str(result.get('status') or '').strip(),
        'mode': str(result.get('mode') or '').strip(),
        'message': str(result.get('message') or '').strip(),
        'target_task_id': str(result.get('target_task_id') or task_id).strip(),
        'target_task_terminal_status': str(result.get('target_task_terminal_status') or '').strip(),
        'target_task_finished_at': str(result.get('target_task_finished_at') or '').strip(),
        'reused_existing': bool(result.get('reused_existing')),
        'target_task': (target_task.model_dump(mode='json') if target_task is not None else None),
        'continuation_task': (continuation_task.model_dump(mode='json') if continuation_task is not None else None),
        'resumed_task': (resumed_task.model_dump(mode='json') if resumed_task is not None else None),
    }


@router.post('/tasks/{task_id}/cancel')
async def cancel_task(task_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    task_id = service.normalize_task_id(task_id)
    try:
        record = await service.cancel_task(task_id)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=_task_control_error_status(detail), detail=detail) from exc
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
            view='raw',
            start_line=start_line,
            end_line=end_line,
            around_line=around_line,
            window=window,
        )
        content = str(excerpt.get('excerpt') or '')
    return {'ok': True, 'artifact': artifact.model_dump(mode='json'), 'content': content, 'excerpt': excerpt}


@router.get('/content/describe')
async def describe_content(ref: str | None = Query(None), path: str | None = Query(None), view: str = Query('canonical')):
    service = _service()
    await service.startup()
    return {'ok': True, **service.describe_content(ref=ref, path=path, view=view)}


@router.get('/content/search')
async def search_content(
    query: str = Query(...),
    ref: str | None = Query(None),
    path: str | None = Query(None),
    view: str = Query('canonical'),
    limit: int = Query(10),
    before: int = Query(2),
    after: int = Query(2),
):
    service = _service()
    await service.startup()
    return {'ok': True, **service.search_content(query=query, ref=ref, path=path, view=view, limit=limit, before=before, after=after)}


@router.get('/content/open')
async def open_content(
    ref: str | None = Query(None),
    path: str | None = Query(None),
    view: str = Query('canonical'),
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
            view=view,
            start_line=start_line,
            end_line=end_line,
            around_line=around_line,
            window=window,
        ),
    }


@router.get('/content/read')
async def read_content(
    ref: str | None = Query(None),
    path: str | None = Query(None),
    view: str = Query('canonical'),
):
    service = _service()
    await service.startup()
    return {
        'ok': True,
        **service.read_content(
            ref=ref,
            path=path,
            view=view,
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
