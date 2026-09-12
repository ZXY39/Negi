from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query

from g3ku.content import artifact_ref_from_id
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
async def get_task_tree_snapshot(
    task_id: str,
    max_nodes: int | None = Query(None),
    after_node_id: str | None = Query(None),
):
    """整树快照；max_nodes 分块（按稳定排序切片）供大树打开的分块加载。

    响应带 truncated / total_node_count / next_after_node_id：前端以
    `after_node_id=next_after_node_id` 续传，直至 truncated=false。
    """
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    payload = service.get_task_tree_snapshot_payload(
        task_id,
        max_nodes=max_nodes,
        after_node_id=str(after_node_id or '').strip(),
    )
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


@router.post('/tasks/{task_id}/nodes/{node_id}/pause')
async def pause_task_node(task_id: str, node_id: str, payload: dict | None = Body(default=None)):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    try:
        node = await service.pause_node(
            service.normalize_task_id(task_id),
            node_id,
            cascade=bool((payload or {}).get('cascade', False)),
            reason='manual',
            remark=str((payload or {}).get('remark') or ''),
        )
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=_task_control_error_status(detail), detail=detail) from exc
    if node is None:
        raise HTTPException(status_code=404, detail='node_not_found')
    return {'ok': True, 'node': node.model_dump(mode='json')}


@router.post('/tasks/{task_id}/nodes/{node_id}/resume')
async def resume_task_node(task_id: str, node_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    try:
        node = await service.resume_node(service.normalize_task_id(task_id), node_id)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=_task_control_error_status(detail), detail=detail) from exc
    if node is None:
        raise HTTPException(status_code=404, detail='node_not_found')
    return {'ok': True, 'node': node.model_dump(mode='json')}


@router.post('/tasks/{task_id}/nodes/{node_id}/notice')
async def append_task_node_notice(task_id: str, node_id: str, payload: dict | None = Body(default=None)):
    """网页端定向通知：对以该节点为根的子树追加用户通知。

    与 task_append_notice 工具同一条服务路径（子树屏障分发），但不做会话
    归属校验——任务详情 UI 是操作员面，可作用于任何未完成任务。
    """
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    message = str((payload or {}).get('message') or '').strip()
    try:
        result_text = await service.append_notice_to_targets(
            task_ids=None,
            node_ids=[node_id],
            message=message,
            session_id='',
            require_session_ownership=False,
        )
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=_task_control_error_status(detail), detail=detail) from exc
    return {'ok': True, 'task_id': service.normalize_task_id(task_id), 'node_id': node_id, 'result': result_text}


@router.get('/tasks/{task_id}/error-log')
async def get_task_error_log(task_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    payload = service.get_task_error_log_payload(service.normalize_task_id(task_id))
    if payload is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return payload


@router.get('/tasks/{task_id}/nodes/{node_id}/error-log')
async def get_task_node_error_log(task_id: str, node_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    payload = service.get_task_node_error_log_payload(service.normalize_task_id(task_id), node_id)
    if payload is None:
        raise HTTPException(status_code=404, detail='task_not_found')
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


@router.post('/tasks/{task_id}/pin')
async def pin_task(task_id: str, payload: dict = Body(default_factory=dict)):
    """磁盘治理（P2）：书签固定/取消固定。pinned 任务豁免压缩与删除渐进。

    web 直写 DB（tasks.payload_json metadata），不经 worker 命令。
    """
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    pinned = bool((payload or {}).get('pinned', True))
    record = service.set_task_pin(service.normalize_task_id(task_id), pinned)
    if record is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, 'task': record.model_dump(mode='json')}


@router.post('/tasks/{task_id}/compress')
async def compress_task(task_id: str):
    """磁盘治理（P2）：手动压缩任务归档（web 模式经 worker 命令转发）。"""
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    try:
        result = await service.compress_task(task_id, reason='manual')
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=_task_control_error_status(detail), detail=detail) from exc
    return {'ok': True, 'result': result}


@router.post('/tasks/{task_id}/decompress')
async def decompress_task(task_id: str):
    """磁盘治理（P2）：解压任务归档；空间不足返回 result=insufficient_space。"""
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    try:
        result = await service.decompress_task(task_id)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=_task_control_error_status(detail), detail=detail) from exc
    return {'ok': True, 'result': result}


@router.get('/tasks/{task_id}/pause-state')
async def get_task_pause_state(task_id: str):
    """Pause-drain state for the synchronous task-card pause hint.

    `draining=true` means the worker has not finished stopping this task's
    actors yet even though the durable paused flags are set; the card keeps
    showing 暂停中 until draining flips to false.
    """
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    payload = service.get_task_pause_state_payload(service.normalize_task_id(task_id))
    if payload is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, 'item': payload}


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


@router.post('/tasks/bulk-delete')
async def bulk_delete_tasks(payload: dict | None = Body(default=None)):
    service = _service()
    await service.startup()
    try:
        raw_task_ids = (payload or {}).get('task_ids')
        if not isinstance(raw_task_ids, list):
            raise ValueError('task_ids must be an array')
        normalized_task_ids = [service.normalize_task_id(str(task_id or '').strip()) for task_id in list(raw_task_ids or [])]
        result = await service.bulk_delete_tasks(normalized_task_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, **result}


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
    ref = artifact_ref_from_id(artifact.artifact_id)
    content = ''
    excerpt = None
    if full:
        # 磁盘治理（P0）：统一读端，兼容 gzip artifact；延迟导入保持 rest 模块轻量。
        from main.storage.artifact_store import read_artifact_text

        content = read_artifact_text(artifact)
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
