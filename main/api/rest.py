from __future__ import annotations

import asyncio

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


def _content_action(action, *args, **kwargs):
    """内容 ref 失效是常态（临时产物/artifact 已被清理），不能让 FileNotFoundError
    逃到中间件——那会被记成 web_api_5xx 审计事件并淹没日志板块。"""
    try:
        return action(*args, **kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={'code': 'content_not_found', 'message': str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={'code': 'content_ref_invalid', 'message': str(exc)},
        ) from exc


@router.get('/tasks')
async def list_tasks(session_id: str = Query('web:shared'), scope: int = Query(1)):
    service = _service()
    await service.startup()
    normalized_session_id = str(session_id or 'web:shared').strip() or 'web:shared'
    effective_session_id = None if normalized_session_id.lower() == 'all' else normalized_session_id

    def _items_payload() -> list[dict[str, object]]:
        items = service.query_service.get_tasks(effective_session_id, int(scope))
        return [item.model_dump(mode='json') for item in items]

    # 查询与序列化是同步 CPU/IO 重活：卸载到线程池，避免独占事件循环
    # 把 worker-status/WS 握手等轻请求一起挂起（任务大厅返回卡顿的根因）。
    items, worker_payload = await asyncio.gather(
        asyncio.to_thread(_items_payload),
        asyncio.to_thread(service.worker_status_payload),
    )
    return {
        'ok': True,
        'session_id': 'all' if effective_session_id is None else effective_session_id,
        'scope': int(scope),
        'items': items,
        **worker_payload,
    }


@router.get('/tasks/worker-status')
async def get_task_worker_status():
    service = _service()
    await service.startup()
    payload = await asyncio.to_thread(service.worker_status_payload)
    return {'ok': True, **payload}


@router.get('/tasks/{task_id}')
async def get_task(
    task_id: str,
    mark_read: bool = Query(False),
):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    if mark_read:
        # mark_read 会经 registry 推送 task.summary.patch（asyncio.Queue 只能
        # 由事件循环线程操作），必须留在循环内执行；纯读的快照构建造到线程池。
        service.log_service.mark_task_read(task_id)
    payload = await asyncio.to_thread(service.get_task_detail_payload, task_id, mark_read=False)
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
    payload = await asyncio.to_thread(
        service.get_task_tree_snapshot_payload,
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
    payload = await asyncio.to_thread(service.node_detail, task_id, node_id, detail_level=detail_level)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=404, detail='node_not_found')
    return payload


@router.get('/tasks/{task_id}/nodes/{node_id}/latest-context')
async def get_task_node_latest_context(task_id: str, node_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    payload = await asyncio.to_thread(service.get_node_latest_context_payload, task_id, node_id)
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
    payload = await asyncio.to_thread(
        service.get_task_tree_subtree_payload,
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
    payload = await asyncio.to_thread(service.get_task_error_log_payload, service.normalize_task_id(task_id))
    if payload is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return payload


@router.get('/tasks/{task_id}/nodes/{node_id}/error-log')
async def get_task_node_error_log(task_id: str, node_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    payload = await asyncio.to_thread(service.get_task_node_error_log_payload, service.normalize_task_id(task_id), node_id)
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
    payload = await asyncio.to_thread(service.get_task_pause_state_payload, service.normalize_task_id(task_id))
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


@router.post('/tasks/{task_id}/clear-temp')
async def clear_task_temp_files(task_id: str):
    task_id = _ensure_task_route_id(task_id)
    service = _service()
    await service.startup()
    task_id = service.normalize_task_id(task_id)
    try:
        result = await service.clear_task_temp_files(task_id)
    except ValueError as exc:
        detail = str(exc)
        if detail == 'task_not_terminal':
            raise HTTPException(status_code=409, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    if result is None:
        raise HTTPException(status_code=404, detail='task_not_found')
    return {'ok': True, **result}


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

        content = _content_action(read_artifact_text, artifact)
    else:
        excerpt = _content_action(
            service.open_content,
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
    return {
        'ok': True,
        **_content_action(service.describe_content, ref=ref, path=path, view=view),
    }


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
    return {
        'ok': True,
        **_content_action(
            service.search_content,
            query=query,
            ref=ref,
            path=path,
            view=view,
            limit=limit,
            before=before,
            after=after,
        ),
    }


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
        **_content_action(
            service.open_content,
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
        **_content_action(
            service.read_content,
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
