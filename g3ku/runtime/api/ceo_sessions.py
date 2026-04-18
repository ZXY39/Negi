from __future__ import annotations

from datetime import datetime
from inspect import isawaitable
from types import SimpleNamespace

from fastapi import APIRouter, Body, HTTPException, Query

from g3ku.runtime.frontdoor.checkpoint_inspection import (
    get_frontdoor_checkpoint,
    get_frontdoor_checkpoint_history,
)
from g3ku.runtime.web_ceo_sessions import (
    SESSION_TASK_DEFAULTS_SCOPE_KEY,
    SESSION_TASK_DEFAULTS_SCOPE_SESSION,
    WebCeoStateStore,
    build_ceo_session_catalog,
    ceo_session_task_defaults_scope,
    ceo_session_family,
    clear_web_ceo_session_artifacts,
    create_web_ceo_session,
    delete_web_ceo_session_artifacts,
    ensure_ceo_session_metadata,
    ensure_active_web_ceo_session,
    find_ceo_session_catalog_item,
    list_web_ceo_sessions,
    main_runtime_depth_limits,
    normalize_ceo_metadata,
    normalize_task_defaults,
    read_completed_continuity_snapshot,
    read_inflight_turn_snapshot,
    read_paused_execution_context,
    resolve_active_ceo_session_id,
    workspace_path,
)
from g3ku.session.manager import SessionManager
from g3ku.shells.web import (
    get_agent,
    get_runtime_manager,
    get_web_heartbeat_service,
    is_no_ceo_model_configured_error,
)
from main.protocol import build_envelope

router = APIRouter()


_STANDALONE_RUNTIME_MANAGER = SimpleNamespace(
    get=lambda _session_id: None,
    remove=lambda _session_id: None,
)


def _sessions():
    state_store = WebCeoStateStore(workspace_path())
    try:
        agent = get_agent()
    except Exception as exc:
        if not is_no_ceo_model_configured_error(exc):
            raise
        return None, SessionManager(workspace_path()), _STANDALONE_RUNTIME_MANAGER, state_store
    session_manager = getattr(agent, "sessions", None)
    if session_manager is None:
        raise HTTPException(status_code=503, detail="session_manager_unavailable")
    runtime_manager = get_runtime_manager(agent)
    return agent, session_manager, runtime_manager, state_store


def _runtime_session(runtime_manager, session_id: str):
    getter = getattr(runtime_manager, "get", None)
    return getter(session_id) if callable(getter) else None


def _recreate_runtime_session(runtime_manager, session) -> object | None:
    creator = getattr(runtime_manager, "get_or_create", None)
    if not callable(creator):
        return None
    paused_snapshot = read_paused_execution_context(session.key) or {}
    if not isinstance(paused_snapshot, dict) or not paused_snapshot:
        return None
    normalized_metadata = normalize_ceo_metadata(getattr(session, "metadata", None), session_key=session.key)
    memory_scope = dict(normalized_metadata.get("memory_scope") or {})
    session_key = str(getattr(session, "key", "") or "").strip()
    chat_id = session_key.split(":", 1)[1] if ":" in session_key else session_key
    runtime_session = creator(
        session_key=session_key,
        channel="web",
        chat_id=chat_id or "shared",
        memory_channel=str(memory_scope.get("channel") or "").strip() or None,
        memory_chat_id=str(memory_scope.get("chat_id") or "").strip() or None,
    )
    state = getattr(runtime_session, "state", None)
    if state is not None:
        if not getattr(state, "pending_interrupts", None):
            setattr(state, "pending_interrupts", list(paused_snapshot.get("interrupts") or []))
        if not bool(getattr(state, "is_running", False)):
            setattr(state, "status", str(paused_snapshot.get("status") or "paused"))
        if hasattr(state, "paused"):
            setattr(state, "paused", str(paused_snapshot.get("status") or "paused").strip().lower() == "paused")
    request_body_messages = [
        dict(item)
        for item in list(paused_snapshot.get("frontdoor_request_body_messages") or [])
        if isinstance(item, dict)
    ]
    if request_body_messages or "frontdoor_request_body_messages" in paused_snapshot:
        setattr(runtime_session, "_frontdoor_request_body_messages", request_body_messages)
    if "frontdoor_history_shrink_reason" in paused_snapshot:
        setattr(
            runtime_session,
            "_frontdoor_history_shrink_reason",
            str(paused_snapshot.get("frontdoor_history_shrink_reason") or "").strip(),
        )
    return runtime_session


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
    has_restorable_artifact = (
        read_inflight_turn_snapshot(key) is not None
        or read_paused_execution_context(key) is not None
        or read_completed_continuity_snapshot(key) is not None
    )
    if not path.exists() and not has_restorable_artifact:
        raise HTTPException(status_code=404, detail="session_not_found")
    session = session_manager.get_or_create(key)
    if path.exists() and ensure_ceo_session_metadata(session):
        session_manager.save(session)
    return session


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


def _assert_known_catalog_session(
    session_manager,
    runtime_manager,
    state_store,
    session_id: str,
) -> tuple[str, dict[str, object]]:
    requested_session_id = str(session_id or "").strip()
    if not requested_session_id:
        raise HTTPException(status_code=404, detail="session_not_found")
    catalog = _build_catalog(session_manager, runtime_manager, active_session_id=requested_session_id)
    item = find_ceo_session_catalog_item(catalog, requested_session_id)
    if item is None:
        for group in list(catalog.get("channel_groups") or []):
            for candidate in list((group or {}).get("items") or []):
                if str(candidate.get("session_id") or "").strip() == requested_session_id:
                    item = candidate
                    break
            if item is not None:
                break
    if item is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    resolved_session_id = str(item.get("session_id") or "").strip() or requested_session_id
    return resolved_session_id, catalog


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
                'active_session_family': snapshot_catalog.get('active_session_family') or ceo_session_family(resolved_active_session_id),
            },
        )
    )


async def _task_service(agent):
    if agent is None:
        raise HTTPException(status_code=503, detail='no_model_configured')
    service = getattr(agent, 'main_task_service', None)
    if service is None:
        raise HTTPException(status_code=503, detail='main_task_service_unavailable')
    await service.startup()
    return service


def _session_task_delete_payload(service, session_id: str) -> dict:
    tasks = service.list_tasks_for_session(session_id)
    completed: list[dict] = []
    paused: list[dict] = []
    in_progress: list[dict] = []
    for task in tasks:
        item = {
            'task_id': str(getattr(task, 'task_id', '') or '').strip(),
            'title': str(getattr(task, 'title', '') or '').strip(),
            'status': str(getattr(task, 'status', '') or '').strip(),
            'is_paused': bool(getattr(task, 'is_paused', False)),
        }
        status = str(item.get('status') or '').strip().lower()
        if status == 'in_progress':
            if item['is_paused']:
                paused.append(item)
            else:
                in_progress.append(item)
        else:
            completed.append(item)
    counts = service.get_session_task_counts(session_id)
    return {
        'ok': True,
        'session_id': session_id,
        'can_delete': True,
        'related_tasks': counts,
        'usage': {
            'tasks': in_progress,
            'completed_tasks': completed,
            'paused_tasks': paused,
            'in_progress_tasks': in_progress,
        },
        'message': '',
    }


def _task_defaults_response(session) -> dict:
    depth_limits = main_runtime_depth_limits()
    metadata = getattr(session, "metadata", None) or {}
    scope = ceo_session_task_defaults_scope(metadata) or "global"
    task_defaults = normalize_task_defaults(
        metadata.get("task_defaults") if scope == SESSION_TASK_DEFAULTS_SCOPE_SESSION else None,
        default_max_depth=depth_limits["default_max_depth"],
        hard_max_depth=depth_limits["hard_max_depth"],
    )
    return {
        "session_id": str(getattr(session, "key", "") or ""),
        "task_defaults": task_defaults,
        "main_runtime": depth_limits,
        "scope": scope,
    }


@router.get("/ceo/sessions/{session_id}/checkpoint")
async def get_ceo_session_checkpoint(
    session_id: str,
    checkpoint_id: str | None = Query(None),
):
    agent, session_manager, _runtime_manager, _state_store = _sessions()
    if agent is None:
        raise HTTPException(status_code=503, detail="no_model_configured")
    session = _assert_known_session(session_manager, session_id)
    item = get_frontdoor_checkpoint(
        agent,
        session_id=session.key,
        checkpoint_id=checkpoint_id,
    )
    if isawaitable(item):
        item = await item
    if item is None:
        raise HTTPException(status_code=404, detail="checkpoint_not_found")
    return {"ok": True, "session_id": session.key, "item": item}


@router.get("/ceo/sessions/{session_id}/checkpoint-history")
async def get_ceo_session_checkpoint_history(
    session_id: str,
    limit: int = Query(20, ge=1, le=100),
    before_checkpoint_id: str | None = Query(None),
):
    agent, session_manager, _runtime_manager, _state_store = _sessions()
    if agent is None:
        raise HTTPException(status_code=503, detail="no_model_configured")
    session = _assert_known_session(session_manager, session_id)
    items = get_frontdoor_checkpoint_history(
        agent,
        session_id=session.key,
        limit=limit,
        before_checkpoint_id=before_checkpoint_id,
    )
    if isawaitable(items):
        items = await items
    return {"ok": True, "session_id": session.key, "items": items}


@router.get("/ceo/sessions/{session_id}/pending-interrupts")
async def get_ceo_session_pending_interrupts(session_id: str):
    _agent, session_manager, runtime_manager, _state_store = _sessions()
    session = _assert_known_session(session_manager, session_id)
    runtime_session = _runtime_session(runtime_manager, session.key)
    items = list(getattr(getattr(runtime_session, "state", None), "pending_interrupts", []) or [])
    if not items:
        snapshot = read_paused_execution_context(session.key) or {}
        items = list(snapshot.get("interrupts") or [])
    return {"ok": True, "session_id": session.key, "items": items}


@router.post("/ceo/sessions/{session_id}/resume-interrupt")
async def resume_ceo_session_interrupt(session_id: str, payload: dict | None = Body(default=None)):
    agent, session_manager, runtime_manager, _state_store = _sessions()
    if agent is None:
        raise HTTPException(status_code=503, detail="no_model_configured")
    session = _assert_known_session(session_manager, session_id)
    runtime_session = _runtime_session(runtime_manager, session.key)
    if runtime_session is None:
        runtime_session = _recreate_runtime_session(runtime_manager, session)
    if runtime_session is None or not hasattr(runtime_session, "resume_frontdoor_interrupt"):
        raise HTTPException(status_code=409, detail="interrupt_resume_unavailable")
    result = await runtime_session.resume_frontdoor_interrupt(resume_value=(payload or {}).get("resume"))
    return {"ok": True, "session_id": session.key, "output": result.output, "state": runtime_session.state_dict()}


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
        SESSION_TASK_DEFAULTS_SCOPE_KEY: SESSION_TASK_DEFAULTS_SCOPE_SESSION,
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
    agent, session_manager, runtime_manager, state_store = _sessions()
    service = await _task_service(agent)
    if _is_channel_session_id(session_id):
        resolved_session_id, _catalog = _assert_known_catalog_session(
            session_manager,
            runtime_manager,
            state_store,
            session_id,
        )
        return _session_task_delete_payload(service, resolved_session_id)
    session = _assert_known_session(session_manager, session_id)
    return _session_task_delete_payload(service, session.key)


@router.delete("/ceo/sessions/{session_id}")
async def delete_ceo_session(session_id: str, payload: dict | None = Body(default=None)):
    agent, session_manager, runtime_manager, state_store = _sessions()
    service = await _task_service(agent)
    is_channel_session = _is_channel_session_id(session_id)
    if is_channel_session:
        resolved_session_id, _catalog = _assert_known_catalog_session(
            session_manager,
            runtime_manager,
            state_store,
            session_id,
        )
        session_key = resolved_session_id
    else:
        session = _assert_known_session(session_manager, session_id)
        session_key = session.key
    stopped_background_tool_count = 0
    tool_execution_manager = getattr(agent, 'tool_execution_manager', None)
    if tool_execution_manager is not None and hasattr(tool_execution_manager, 'stop_session_executions'):
        stopped_results = await tool_execution_manager.stop_session_executions(
            session_key,
            reason='session_cleared' if is_channel_session else 'session_deleted',
        )
        stopped_background_tool_count = len(list(stopped_results or []))
    heartbeat = get_web_heartbeat_service(agent)
    if heartbeat is not None:
        heartbeat.clear_session(session_key)
    delete_task_records = bool((payload or {}).get('delete_task_records'))
    deleted_task_count = 0
    if delete_task_records:
        try:
            deleted_task_count = await service.delete_task_records_for_session(session_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if is_channel_session:
        delete_web_ceo_session_artifacts(
            session_manager=session_manager,
            session_id=session_key,
            task_service=service,
        )
    else:
        delete_web_ceo_session_artifacts(
            session_manager=session_manager,
            session_id=session_key,
            task_service=service,
        )
    remover = getattr(runtime_manager, "remove", None)
    if callable(remover):
        remover(session_key)
    cancel = getattr(agent, "cancel_session_tasks", None)
    if callable(cancel):
        await cancel(session_key)
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
        "deleted": not is_channel_session,
        "cleared": is_channel_session,
        "session_id": session_key,
        "deleted_task_count": deleted_task_count,
        "stopped_background_tool_count": stopped_background_tool_count,
        "items": catalog.get("items") or [],
        "channel_groups": catalog.get("channel_groups") or [],
        "active_session_id": active_session_id,
        "active_session_family": catalog.get("active_session_family") or "local",
    }
