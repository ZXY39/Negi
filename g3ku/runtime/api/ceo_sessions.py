from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, HTTPException

from g3ku.runtime.web_ceo_sessions import (
    WebCeoStateStore,
    create_web_ceo_session,
    delete_web_ceo_session_artifacts,
    ensure_active_web_ceo_session,
    list_web_ceo_sessions,
    main_runtime_depth_limits,
    normalize_ceo_metadata,
    normalize_task_defaults,
    workspace_path,
)
from g3ku.shells.web import get_agent, get_runtime_manager

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


def _assert_no_running_turn(runtime_manager, session_id: str):
    if _session_is_running(runtime_manager, session_id):
        raise HTTPException(status_code=409, detail="ceo_turn_in_progress")


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
    _agent, session_manager, _runtime_manager, state_store = _sessions()
    active_session_id = ensure_active_web_ceo_session(session_manager, state_store)
    items = list_web_ceo_sessions(session_manager, active_session_id=active_session_id)
    return {"ok": True, "items": items, "active_session_id": active_session_id}


@router.post("/ceo/sessions")
async def create_ceo_session(payload: dict | None = Body(default=None)):
    _agent, session_manager, runtime_manager, state_store = _sessions()
    current_active = ensure_active_web_ceo_session(session_manager, state_store)
    _assert_no_running_turn(runtime_manager, current_active)
    session = create_web_ceo_session(session_manager, title=str((payload or {}).get("title") or "").strip() or None)
    state_store.set_active_session_id(session.key)
    items = list_web_ceo_sessions(session_manager, active_session_id=session.key)
    item = next((entry for entry in items if entry["session_id"] == session.key), None)
    return {"ok": True, "item": item, "items": items, "active_session_id": session.key}


@router.patch("/ceo/sessions/{session_id}")
async def rename_ceo_session(session_id: str, payload: dict = Body(...)):
    _agent, session_manager, runtime_manager, state_store = _sessions()
    current_active = ensure_active_web_ceo_session(session_manager, state_store)
    _assert_no_running_turn(runtime_manager, current_active)
    session = _assert_known_session(session_manager, session_id)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title_required")
    session.metadata = dict(session.metadata or {})
    session.metadata["title"] = title
    session.updated_at = datetime.now()
    session_manager.save(session)
    active_session_id = ensure_active_web_ceo_session(session_manager, state_store)
    items = list_web_ceo_sessions(session_manager, active_session_id=active_session_id)
    item = next((entry for entry in items if entry["session_id"] == session.key), None)
    return {"ok": True, "item": item, "items": items, "active_session_id": active_session_id}


@router.get("/ceo/sessions/{session_id}/task-defaults")
async def get_ceo_session_task_defaults(session_id: str):
    _agent, session_manager, _runtime_manager, _state_store = _sessions()
    session = _assert_known_session(session_manager, session_id)
    if normalize_ceo_metadata(getattr(session, "metadata", None), session_key=session.key) != getattr(session, "metadata", None):
        session.metadata = normalize_ceo_metadata(getattr(session, "metadata", None), session_key=session.key)
        session_manager.save(session)
    return {"ok": True, **_task_defaults_response(session)}


@router.patch("/ceo/sessions/{session_id}/task-defaults")
async def update_ceo_session_task_defaults(session_id: str, payload: dict = Body(...)):
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
    session_manager.save(session)
    return {"ok": True, **_task_defaults_response(session)}


@router.post("/ceo/sessions/{session_id}/activate")
async def activate_ceo_session(session_id: str):
    _agent, session_manager, runtime_manager, state_store = _sessions()
    target = _assert_known_session(session_manager, session_id)
    current_active = ensure_active_web_ceo_session(session_manager, state_store)
    if current_active != session_id:
        _assert_no_running_turn(runtime_manager, current_active)
    state_store.set_active_session_id(target.key)
    items = list_web_ceo_sessions(session_manager, active_session_id=target.key)
    item = next((entry for entry in items if entry["session_id"] == target.key), None)
    return {"ok": True, "item": item, "items": items, "active_session_id": target.key}


@router.delete("/ceo/sessions/{session_id}")
async def delete_ceo_session(session_id: str):
    agent, session_manager, runtime_manager, state_store = _sessions()
    current_active = ensure_active_web_ceo_session(session_manager, state_store)
    _assert_no_running_turn(runtime_manager, current_active)
    session = _assert_known_session(session_manager, session_id)
    delete_web_ceo_session_artifacts(session_manager=session_manager, session_id=session.key)
    remover = getattr(runtime_manager, "remove", None)
    if callable(remover):
        remover(session.key)
    cancel = getattr(agent, "cancel_session_tasks", None)
    if callable(cancel):
        await cancel(session.key)
    active_session_id = ensure_active_web_ceo_session(session_manager, state_store)
    state_store.set_active_session_id(active_session_id)
    items = list_web_ceo_sessions(session_manager, active_session_id=active_session_id)
    return {
        "ok": True,
        "deleted": True,
        "session_id": session.key,
        "items": items,
        "active_session_id": active_session_id,
    }
