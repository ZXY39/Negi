from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, HTTPException

from g3ku.runtime.web_ceo_sessions import (
    WebCeoStateStore,
    build_ceo_session_catalog,
    create_web_ceo_session,
    delete_web_ceo_session_artifacts,
    ensure_active_web_ceo_session,
    find_ceo_session_catalog_item,
    list_web_ceo_sessions,
    main_runtime_depth_limits,
    normalize_ceo_metadata,
    normalize_task_defaults,
    resolve_active_ceo_session_id,
    workspace_path,
)
from g3ku.shells.web import get_agent, get_runtime_manager, get_web_heartbeat_service
from main.protocol import build_envelope

router = APIRouter()


def _sessions():
    agent = get_agent()
    session_manager = getattr(agent, "sessions", None)
    if session_manager is None:
        raise HTTPException(status_code=503, detail="session_manager_unavailable")
    runtime_manager = get_runtime_manager(agent)
    state_store = WebCeoStateStore(workspace_path())
    return agent, session_manager, runtime_manager, state_store


def _runtime_session(runtime_manager, session_id: str):
    getter = getattr(runtime_manager, "get", None)
    return getter(session_id) if callable(getter) else None


def _session_is_running(runtime_manager, session_id: str) -> bool:
    session = _runtime_session(runtime_manager, session_id)
    if session is None:
        return False
    state = getattr(session, "state", None)
    status = str(getattr(state, "status", "") or "").strip().lower()
    return bool(getattr(state, "is_running", False)) or status == "running"


def _assert_known_session(session_manager, session_id: str):
    key = str(session_id or "").strip()
    if not key.startswith("web:"):
        raise HTTPException(status_code=404, detail="session_not_found")
    path = session_manager.get_path(key)
    if not path.exists():
        raise HTTPException(status_code=404, detail="session_not_found")
    return session_manager.get_or_create(key)


def _list_session_items(session_manager, runtime_manager, *, active_session_id: str) -> list[dict]:
    return list_web_ceo_sessions(
        session_manager,
        active_session_id=active_session_id,
        is_running_resolver=lambda session_id: _session_is_running(runtime_manager, session_id),
    )


def _is_channel_session_id(session_id: str) -> bool:
    return str(session_id or "").strip().startswith("china:")


def _raise_channel_session_readonly() -> None:
    raise HTTPException(status_code=409, detail="channel_session_readonly")


def _build_catalog(session_manager, runtime_manager, *, active_session_id: str) -> dict[str, object]:
    return build_ceo_session_catalog(
        session_manager,
        active_session_id=active_session_id,
        is_running_resolver=lambda session_id: _session_is_running(runtime_manager, session_id),
    )


def _publish_ceo_sessions_snapshot(
    agent,
    session_manager,
    runtime_manager,
    state_store,
    *,
    catalog: dict[str, object] | None = None,
    active_session_id: str | None = None,
) -> None:
    service = getattr(agent, 'main_task_service', None)
    registry = getattr(service, 'registry', None) if service is not None else None
    if registry is None or not hasattr(registry, 'publish_global_ceo'):
        return
    resolved_active_session_id = str(active_session_id or '').strip()
    if not resolved_active_session_id:
        resolved_active_session_id = resolve_active_ceo_session_id(session_manager, state_store)
    snapshot_catalog = catalog if isinstance(catalog, dict) else _build_catalog(
        session_manager,
        runtime_manager,
        active_session_id=resolved_active_session_id,
    )
    seq_session_id = resolved_active_session_id or 'web:shared'
    registry.publish_global_ceo(
        build_envelope(
            channel='ceo',
            session_id=seq_session_id,
            seq=registry.next_ceo_seq(seq_session_id),
            type='ceo.sessions.snapshot',
            data={
                'items': snapshot_catalog.get('items') or [],
                'channel_groups': snapshot_catalog.get('channel_groups') or [],
                'active_session_id': resolved_active_session_id,
                'active_session_family': snapshot_catalog.get('active_session_family') or 'local',
            },
        )
    )


async def _task_service(agent):
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        raise HTTPException(status_code=503, detail='main_task_service_unavailable')
    await service.startup()
    return service


def _session_task_delete_payload(service, session_id: str) -> dict:
    tasks = service.list_tasks_for_session(session_id)
    unfinished = [
        {
            'task_id': str(getattr(task, 'task_id', '') or '').strip(),
            'title': str(getattr(task, 'title', '') or '').strip(),
            'status': str(getattr(task, 'status', '') or '').strip(),
            'is_paused': bool(getattr(task, 'is_paused', False)),
        }
        for task in tasks
        if str(getattr(task, 'status', '') or '').strip().lower() == 'in_progress'
    ]
    counts = service.get_session_task_counts(session_id)
    can_delete = not unfinished
    message = '' if can_delete else '会话仍有未完成任务，无法删除。'
    return {
        'ok': True,
        'session_id': session_id,
        'can_delete': can_delete,
        'related_tasks': counts,
        'usage': {'tasks': unfinished},
        'message': message,
    }


def _raise_session_delete_blocked(*, session_id: str, payload: dict) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            'code': 'session_has_unfinished_tasks',
            'message': str(payload.get('message') or '会话仍有未完成任务，无法删除。'),
            'session_id': session_id,
            'usage': dict(payload.get('usage') or {'tasks': []}),
            'related_tasks': dict(payload.get('related_tasks') or {}),
        },
    )


def _task_defaults_response(session) -> dict:
    depth_limits = main_runtime_depth_limits()
    task_defaults = normalize_task_defaults(
        (getattr(session, "metadata", None) or {}).get("task_defaults"),
        default_max_depth=depth_limits["default_max_depth"],
        hard_max_depth=depth_limits["hard_max_depth"],
    )
    return {
        "session_id": str(getattr(session, "key", "") or ""),
        "task_defaults": task_defaults,
        "main_runtime": depth_limits,
    }


@router.get("/ceo/sessions")
async def list_ceo_sessions():
    _agent, session_manager, runtime_manager, state_store = _sessions()
    active_session_id = resolve_active_ceo_session_id(session_manager, state_store)
    catalog = _build_catalog(session_manager, runtime_manager, active_session_id=active_session_id)
    return {
        "ok": True,
        "items": catalog.get("items") or [],
        "channel_groups": catalog.get("channel_groups") or [],
        "active_session_id": active_session_id,
        "active_session_family": catalog.get("active_session_family") or "local",
    }


@router.post("/ceo/sessions")
async def create_ceo_session(payload: dict | None = Body(default=None)):
    agent, session_manager, runtime_manager, state_store = _sessions()
    resolve_active_ceo_session_id(session_manager, state_store)
    session = create_web_ceo_session(session_manager, title=str((payload or {}).get("title") or "").strip() or None)
    state_store.set_active_session_id(session.key)
    catalog = _build_catalog(session_manager, runtime_manager, active_session_id=session.key)
    item = next((entry for entry in list(catalog.get("items") or []) if entry["session_id"] == session.key), None)
    _publish_ceo_sessions_snapshot(
        agent,
        session_manager,
        runtime_manager,
        state_store,
        catalog=catalog,
        active_session_id=session.key,
    )
    return {
        "ok": True,
        "item": item,
        "items": catalog.get("items") or [],
        "channel_groups": catalog.get("channel_groups") or [],
        "active_session_id": session.key,
        "active_session_family": catalog.get("active_session_family") or "local",
    }


@router.patch("/ceo/sessions/{session_id}")
async def rename_ceo_session(session_id: str, payload: dict = Body(...)):
    agent, session_manager, runtime_manager, state_store = _sessions()
    if _is_channel_session_id(session_id):
        _raise_channel_session_readonly()
    session = _assert_known_session(session_manager, session_id)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title_required")
    session.metadata = dict(session.metadata or {})
    session.metadata["title"] = title
    session.updated_at = datetime.now()
    session_manager.save(session)
    active_session_id = resolve_active_ceo_session_id(session_manager, state_store)
    catalog = _build_catalog(session_manager, runtime_manager, active_session_id=active_session_id)
    item = next((entry for entry in list(catalog.get("items") or []) if entry["session_id"] == session.key), None)
    _publish_ceo_sessions_snapshot(
        agent,
        session_manager,
        runtime_manager,
        state_store,
        catalog=catalog,
        active_session_id=active_session_id,
    )
    return {
        "ok": True,
        "item": item,
        "items": catalog.get("items") or [],
        "channel_groups": catalog.get("channel_groups") or [],
        "active_session_id": active_session_id,
        "active_session_family": catalog.get("active_session_family") or "local",
    }


@router.get("/ceo/sessions/{session_id}/task-defaults")
async def get_ceo_session_task_defaults(session_id: str):
    if _is_channel_session_id(session_id):
        _raise_channel_session_readonly()
    _agent, session_manager, _runtime_manager, _state_store = _sessions()
    session = _assert_known_session(session_manager, session_id)
    if normalize_ceo_metadata(getattr(session, "metadata", None), session_key=session.key) != getattr(session, "metadata", None):
        session.metadata = normalize_ceo_metadata(getattr(session, "metadata", None), session_key=session.key)
        session_manager.save(session)
    return {"ok": True, **_task_defaults_response(session)}


@router.patch("/ceo/sessions/{session_id}/task-defaults")
async def update_ceo_session_task_defaults(session_id: str, payload: dict = Body(...)):
    if _is_channel_session_id(session_id):
        _raise_channel_session_readonly()
    _agent, session_manager, _runtime_manager, _state_store = _sessions()
    session = _assert_known_session(session_manager, session_id)
    depth_limits = main_runtime_depth_limits()
    normalized_metadata = normalize_ceo_metadata(getattr(session, "metadata", None), session_key=session.key)
    merged_defaults = dict(normalized_metadata.get("task_defaults") or {})
    if isinstance(payload, dict):
        merged_defaults.update({key: value for key, value in payload.items() if key in {"max_depth", "maxDepth"}})
    session.metadata = {
        **normalized_metadata,
        "task_defaults": normalize_task_defaults(
            merged_defaults,
            default_max_depth=depth_limits["default_max_depth"],
            hard_max_depth=depth_limits["hard_max_depth"],
        ),
    }
    session.updated_at = datetime.now()
    session_manager.save(session)
    return {"ok": True, **_task_defaults_response(session)}


@router.post("/ceo/sessions/{session_id}/activate")
async def activate_ceo_session(session_id: str):
    agent, session_manager, runtime_manager, state_store = _sessions()
    requested_session_id = str(session_id or "").strip()
    if not requested_session_id:
        raise HTTPException(status_code=404, detail="session_not_found")
    catalog = _build_catalog(session_manager, runtime_manager, active_session_id=requested_session_id)
    item = find_ceo_session_catalog_item(catalog, requested_session_id)
    if item is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    target_id = str(item.get("session_id") or "").strip()
    if target_id and target_id != requested_session_id:
        catalog = _build_catalog(session_manager, runtime_manager, active_session_id=target_id)
        item = find_ceo_session_catalog_item(catalog, target_id)
    state_store.set_active_session_id(target_id)
    _publish_ceo_sessions_snapshot(
        agent,
        session_manager,
        runtime_manager,
        state_store,
        catalog=catalog,
        active_session_id=target_id,
    )
    return {
        "ok": True,
        "item": item,
        "items": catalog.get("items") or [],
        "channel_groups": catalog.get("channel_groups") or [],
        "active_session_id": target_id,
        "active_session_family": catalog.get("active_session_family") or "local",
    }


@router.get('/ceo/sessions/{session_id}/delete-check')
async def get_ceo_session_delete_check(session_id: str):
    if _is_channel_session_id(session_id):
        _raise_channel_session_readonly()
    agent, session_manager, _runtime_manager, _state_store = _sessions()
    session = _assert_known_session(session_manager, session_id)
    service = await _task_service(agent)
    return _session_task_delete_payload(service, session.key)


@router.delete("/ceo/sessions/{session_id}")
async def delete_ceo_session(session_id: str, payload: dict | None = Body(default=None)):
    if _is_channel_session_id(session_id):
        _raise_channel_session_readonly()
    agent, session_manager, runtime_manager, state_store = _sessions()
    service = await _task_service(agent)
    session = _assert_known_session(session_manager, session_id)
    delete_check = _session_task_delete_payload(service, session.key)
    if not bool(delete_check.get('can_delete')):
        _raise_session_delete_blocked(session_id=session.key, payload=delete_check)
    stopped_background_tool_count = 0
    tool_execution_manager = getattr(agent, 'tool_execution_manager', None)
    if tool_execution_manager is not None and hasattr(tool_execution_manager, 'stop_session_executions'):
        stopped_results = await tool_execution_manager.stop_session_executions(
            session.key,
            reason='session_deleted',
        )
        stopped_background_tool_count = len(list(stopped_results or []))
    heartbeat = get_web_heartbeat_service(agent)
    if heartbeat is not None:
        heartbeat.clear_session(session.key)
    delete_task_records = bool((payload or {}).get('delete_task_records'))
    deleted_task_count = 0
    if delete_task_records:
        try:
            deleted_task_count = await service.delete_task_records_for_session(session.key)
        except ValueError as exc:
            if str(exc) == 'session_has_unfinished_tasks':
                latest = _session_task_delete_payload(service, session.key)
                _raise_session_delete_blocked(session_id=session.key, payload=latest)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    delete_web_ceo_session_artifacts(session_manager=session_manager, session_id=session.key)
    remover = getattr(runtime_manager, "remove", None)
    if callable(remover):
        remover(session.key)
    cancel = getattr(agent, "cancel_session_tasks", None)
    if callable(cancel):
        await cancel(session.key)
    active_session_id = resolve_active_ceo_session_id(session_manager, state_store)
    state_store.set_active_session_id(active_session_id)
    catalog = _build_catalog(session_manager, runtime_manager, active_session_id=active_session_id)
    _publish_ceo_sessions_snapshot(
        agent,
        session_manager,
        runtime_manager,
        state_store,
        catalog=catalog,
        active_session_id=active_session_id,
    )
    return {
        "ok": True,
        "deleted": True,
        "session_id": session.key,
        "deleted_task_count": deleted_task_count,
        "stopped_background_tool_count": stopped_background_tool_count,
        "items": catalog.get("items") or [],
        "channel_groups": catalog.get("channel_groups") or [],
        "active_session_id": active_session_id,
        "active_session_family": catalog.get("active_session_family") or "local",
    }
