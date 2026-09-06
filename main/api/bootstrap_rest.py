from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from loguru import logger

from g3ku.config.loader import load_config
from g3ku.security import get_bootstrap_security_service
from g3ku.shells.web import (
    describe_web_runtime_services,
    ensure_web_runtime_services,
    get_agent,
    get_runtime_manager,
    shutdown_web_runtime,
)
from g3ku.shells.web import wait_shutdown_pause_commands_drained
from g3ku.web.server_control import request_server_shutdown

router = APIRouter()


def _service():
    return get_bootstrap_security_service()


def _status_payload(*, include_preview: bool = True) -> dict[str, Any]:
    service = _service()
    payload = dict(service.status())
    runtime = describe_web_runtime_services() if payload.get("mode") == "unlocked" else {
        "agent_ready": False,
        "main_runtime_ready": False,
        "heartbeat_ready": False,
        "bootstrapping": False,
        "ready": False,
    }
    payload["runtime"] = runtime
    payload["runtime_ready"] = bool(runtime.get("ready"))
    payload["runtime_bootstrapping"] = bool(runtime.get("bootstrapping"))
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
    try:
        config = load_config()
    except Exception as exc:
        logger.warning("bootstrap runtime preflight skipped while reading config: {}", exc)
        config = None
    if config is not None and not config.get_role_model_keys("ceo"):
        logger.info("Skipping runtime startup after unlock because no CEO model is configured yet.")
        return
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
    parts: list[str] = []
    if running_sessions:
        parts.append(f"{len(running_sessions)} 个进行中的对话")
    if running_tasks:
        parts.append(f"{len(running_tasks)} 个进行中的任务")
    return {
        "has_running_work": has_running_work,
        "running_sessions": running_sessions,
        "running_tasks": running_tasks,
        "summary_text": "，".join(parts) if parts else "当前没有进行中的对话或任务。",
    }


async def _pause_running_work() -> dict[str, int]:
    agent = get_agent()
    runtime_manager = get_runtime_manager(agent)
    service = getattr(agent, "main_task_service", None)
    if service is not None:
        await service.startup()
    store = getattr(service, "store", None)
    record_entry = getattr(store, "record_shutdown_pause_entry", None)

    def _record(kind: str, ref_id: str, *, channel: str = "", chat_id: str = "") -> None:
        if not callable(record_entry):
            return
        try:
            record_entry(kind=kind, ref_id=ref_id, channel=channel, chat_id=chat_id)
        except Exception:
            logger.debug("shutdown pause ledger write skipped for {} {}", kind, ref_id)

    paused_sessions = 0
    for session_id in list(runtime_manager.list_sessions()):
        if not _runtime_session_is_running(runtime_manager, session_id):
            continue
        pause = getattr(runtime_manager, "pause", None)
        if callable(pause):
            await pause(session_id, manual=True)
        else:
            session = runtime_manager.get(session_id) if hasattr(runtime_manager, "get") else None
            if session is not None and hasattr(session, "pause"):
                await session.pause(manual=True)
        session_meta = getattr(runtime_manager, "session_meta", None)
        channel, chat_id = "", ""
        if callable(session_meta):
            try:
                resolved = session_meta(session_id)
                if isinstance(resolved, tuple) and len(resolved) == 2:
                    channel, chat_id = str(resolved[0] or ""), str(resolved[1] or "")
            except Exception:
                channel, chat_id = "", ""
        _record("session", session_id, channel=channel, chat_id=chat_id)
        paused_sessions += 1

    paused_tasks = 0
    paused_task_ids: set[str] = set()
    if service is not None:
        for task in list(service.store.list_tasks()):
            status = str(getattr(task, "status", "") or "").strip().lower()
            if status != "in_progress" or bool(getattr(task, "is_paused", False)):
                continue
            pause_impl = getattr(service, "force_pause_task_durably", None)
            if callable(pause_impl):
                await pause_impl(str(getattr(task, "task_id", "") or ""))
            else:
                await service.pause_task(task.task_id)
            paused_task_ids.add(str(getattr(task, "task_id", "") or ""))
            _record("task", str(getattr(task, "task_id", "") or ""))
            paused_tasks += 1

    for _ in range(20):
        await asyncio.sleep(0.1)
        snapshot = await _running_work_snapshot()
        if not snapshot["has_running_work"]:
            break
    else:
        raise TimeoutError("running work did not pause before exit")

    if service is not None and paused_task_ids:
        # Durable pause flags are set; wait for the worker to actually stop
        # the running task actors (pause commands finished) before exiting,
        # so "paused" never hides work still executing in the background.
        await wait_shutdown_pause_commands_drained(service, task_ids=paused_task_ids)

    return {"paused_sessions": paused_sessions, "paused_tasks": paused_tasks}


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
    except Exception as exc:
        service.lock()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        await _start_runtime_after_unlock()
    except Exception as exc:
        logger.warning("bootstrap setup completed but runtime startup is deferred: {}", exc)
    return {"ok": True, "item": _status_payload(include_preview=False)}


@router.post("/bootstrap/unlock")
async def bootstrap_unlock(payload: dict = Body(...)):
    password = str(payload.get("password") or "")
    service = _service()
    try:
        service.unlock(password=password)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        await _start_runtime_after_unlock()
    except Exception as exc:
        logger.warning("bootstrap unlock succeeded but runtime startup is deferred: {}", exc)
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
    pause_running_work = bool(
        (payload or {}).get("pause_running_work")
        or (payload or {}).get("pauseRunningWork")
        or (payload or {}).get("stop_running_work")
        or (payload or {}).get("stopRunningWork")
    )
    snapshot = await _running_work_snapshot()
    if snapshot["has_running_work"] and not pause_running_work:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "running_work_requires_confirmation",
                "message": "请先确认暂停正在进行的所有对话和任务。",
                **snapshot,
            },
        )
    paused = {"paused_sessions": 0, "paused_tasks": 0}
    if snapshot["has_running_work"]:
        try:
            paused = await _pause_running_work()
        except TimeoutError:
            latest_snapshot = await _running_work_snapshot()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "running_work_pause_incomplete",
                    "message": "仍有对话或任务未暂停完成，项目不会退出。",
                    **latest_snapshot,
                },
            ) from None
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    await shutdown_web_runtime()
    if not request_server_shutdown():
        raise HTTPException(status_code=503, detail="server_shutdown_unavailable")
    return {
        "ok": True,
        "item": {
            "shutting_down": True,
            **snapshot,
            **paused,
        },
    }


__all__ = ["router"]
