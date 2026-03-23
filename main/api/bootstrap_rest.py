from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from g3ku.security import get_bootstrap_security_service
from g3ku.shells.web import ensure_web_runtime_services, get_agent, get_runtime_manager
from g3ku.web.server_control import request_server_shutdown

router = APIRouter()


def _service():
    return get_bootstrap_security_service()


def _status_payload(*, include_preview: bool = True) -> dict[str, Any]:
    service = _service()
    payload = dict(service.status())
    if include_preview and payload.get("legacy_detected"):
        try:
            payload["legacy_preview"] = service.export_legacy_state()
        except Exception:
            payload["legacy_preview"] = None
    return payload


def _assert_unlocked() -> None:
    if not _service().is_unlocked():
        raise HTTPException(status_code=423, detail="project_locked")


async def _start_runtime_after_unlock() -> None:
    agent = get_agent()
    await ensure_web_runtime_services(agent)


def _runtime_session_is_running(runtime_manager, session_id: str) -> bool:
    session = runtime_manager.get(session_id) if hasattr(runtime_manager, "get") else None
    if session is None:
        return False
    state = getattr(session, "state", None)
    status = str(getattr(state, "status", "") or "").strip().lower()
    return bool(getattr(state, "is_running", False)) or status == "running"


async def _running_work_snapshot() -> dict[str, Any]:
    _assert_unlocked()
    agent = get_agent()
    runtime_manager = get_runtime_manager(agent)
    session_manager = getattr(agent, "sessions", None)
    service = getattr(agent, "main_task_service", None)
    if service is not None:
        await service.startup()

    running_sessions: list[dict[str, Any]] = []
    for session_id in runtime_manager.list_sessions():
        if not _runtime_session_is_running(runtime_manager, session_id):
            continue
        title = str(session_id)
        if session_manager is not None:
            try:
                session = session_manager.get_or_create(session_id)
                metadata = getattr(session, "metadata", None) or {}
                title = str(metadata.get("title") or session_id)
            except Exception:
                title = str(session_id)
        running_sessions.append({"session_id": session_id, "title": title})

    running_tasks: list[dict[str, Any]] = []
    if service is not None:
        for task in service.store.list_tasks():
            status = str(getattr(task, "status", "") or "").strip().lower()
            if status != "in_progress" or bool(getattr(task, "is_paused", False)):
                continue
            running_tasks.append(
                {
                    "task_id": str(getattr(task, "task_id", "") or ""),
                    "title": str(getattr(task, "title", "") or ""),
                    "session_id": str(getattr(task, "session_id", "") or ""),
                }
            )

    has_running_work = bool(running_sessions or running_tasks)
    parts = []
    if running_sessions:
        parts.append(f"{len(running_sessions)} 个进行中的对话")
    if running_tasks:
        parts.append(f"{len(running_tasks)} 个进行中的任务")
    return {
        "has_running_work": has_running_work,
        "running_sessions": running_sessions,
        "running_tasks": running_tasks,
        "summary_text": "、".join(parts) if parts else "当前没有进行中的对话或任务。",
    }


async def _stop_running_work() -> dict[str, int]:
    agent = get_agent()
    runtime_manager = get_runtime_manager(agent)
    service = getattr(agent, "main_task_service", None)
    if service is not None:
        await service.startup()

    stopped_sessions = 0
    for session_id in list(runtime_manager.list_sessions()):
        if not _runtime_session_is_running(runtime_manager, session_id):
            continue
        await runtime_manager.cancel(session_id, reason="project_exit")
        stopped_sessions += 1

    stopped_tasks = 0
    if service is not None:
        for task in list(service.store.list_tasks()):
            status = str(getattr(task, "status", "") or "").strip().lower()
            if status != "in_progress" or bool(getattr(task, "is_paused", False)):
                continue
            await service.cancel_task(task.task_id)
            stopped_tasks += 1

    for _ in range(20):
        await asyncio.sleep(0.1)
        snapshot = await _running_work_snapshot()
        if not snapshot["has_running_work"]:
            break

    return {"stopped_sessions": stopped_sessions, "stopped_tasks": stopped_tasks}


@router.get("/bootstrap/status")
async def bootstrap_status():
    return {"ok": True, "item": _status_payload()}


@router.post("/bootstrap/setup")
async def bootstrap_setup(payload: dict = Body(...)):
    password = str(payload.get("password") or "")
    password_confirm = str(payload.get("password_confirm") or payload.get("passwordConfirm") or "")
    confirm_legacy_reset = bool(payload.get("confirm_legacy_reset") or payload.get("confirmLegacyReset"))
    if password != password_confirm:
        raise HTTPException(status_code=400, detail="password_confirmation_mismatch")
    service = _service()
    try:
        service.setup_initial_realm(
            password=password,
            confirm_legacy_reset=confirm_legacy_reset,
        )
        await _start_runtime_after_unlock()
    except Exception as exc:
        service.lock()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": _status_payload(include_preview=False)}


@router.post("/bootstrap/unlock")
async def bootstrap_unlock(payload: dict = Body(...)):
    password = str(payload.get("password") or "")
    service = _service()
    try:
        service.unlock(password=password)
        await _start_runtime_after_unlock()
    except Exception as exc:
        service.lock()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": _status_payload(include_preview=False)}


@router.get("/bootstrap/exit-check")
async def bootstrap_exit_check():
    try:
        snapshot = await _running_work_snapshot()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": snapshot}


@router.post("/bootstrap/exit")
async def bootstrap_exit(payload: dict | None = Body(default=None)):
    stop_running_work = bool((payload or {}).get("stop_running_work") or (payload or {}).get("stopRunningWork"))
    snapshot = await _running_work_snapshot()
    if snapshot["has_running_work"] and not stop_running_work:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "running_work_requires_confirmation",
                "message": "请先确认停止正在进行的对话和任务。",
                **snapshot,
            },
        )
    stopped = {"stopped_sessions": 0, "stopped_tasks": 0}
    if snapshot["has_running_work"]:
        try:
            stopped = await _stop_running_work()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not request_server_shutdown():
        raise HTTPException(status_code=503, detail="server_shutdown_unavailable")
    return {
        "ok": True,
        "item": {
            "shutting_down": True,
            **snapshot,
            **stopped,
        },
    }


__all__ = ["router"]
