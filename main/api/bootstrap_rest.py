from __future__ import annotations

import asyncio
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from loguru import logger

from g3ku.config.config_bundle import (
    BUNDLE_EXTENSION,
    BUNDLE_OUTPUT_DIR,
    export_bundle,
    import_bundle,
)
from g3ku.config.loader import load_config
from g3ku.deployment.data_root import DataRootError, describe_data_root, write_data_root_pointer
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
from main.api.admin_rest import _refresh_runtime_after_save

router = APIRouter()

_BUNDLE_FILENAME_RE = re.compile(r"^g3ku-config-bundle-\d{8}-\d{6}\.g3kucb$")


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
    payload["data_root"] = describe_data_root()
    payload["dir_picker"] = {"available": dir_picker_available()}
    if include_preview and payload.get("legacy_detected"):
        try:
            payload["legacy_preview"] = service.export_legacy_state()
        except Exception:
            payload["legacy_preview"] = None
    return payload


def _assert_unlocked() -> None:
    if not _service().is_unlocked():
        raise HTTPException(status_code=423, detail="project_locked")


def _apply_data_root_choice(raw: object) -> None:
    """首次初始化时记录数据目录；已建好口令的安装不走这个入口改锚。"""
    text = str(raw or "").strip()
    if not text:
        return
    mode = str((_service().status() or {}).get("mode") or "").strip().lower()
    if mode != "setup":
        raise HTTPException(
            status_code=409,
            detail={"code": "data_root_requires_setup", "message": "数据目录只能在首次初始化时指定。"},
        )
    try:
        write_data_root_pointer(text)
    except DataRootError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc


_DIR_PICKER_LOCK = threading.Lock()


def dir_picker_available() -> bool:
    """本机目录选择框只在 Windows + 可导入 tkinter 时提供；其余环境回手填。"""
    if os.name != "nt":
        return False
    try:
        import tkinter  # noqa: F401
    except Exception:
        return False
    return True


def _ask_directory() -> str:
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        root.update_idletasks()
        return str(filedialog.askdirectory(title="选择项目数据地址") or "").strip()
    finally:
        root.destroy()


@router.post("/bootstrap/pick-data-dir")
async def bootstrap_pick_data_dir():
    if not dir_picker_available():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "dir_picker_unavailable",
                "message": "当前环境不提供本机目录选择框，请手动填写绝对路径。",
            },
        )
    mode = str((_service().status() or {}).get("mode") or "").strip().lower()
    if mode != "setup":
        raise HTTPException(
            status_code=409,
            detail={"code": "data_root_requires_setup", "message": "数据目录只能在首次初始化时指定。"},
        )
    if not _DIR_PICKER_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail={"code": "dir_picker_busy", "message": "已有一个目录选择框在等待操作。"},
        )
    try:
        path = await asyncio.to_thread(_ask_directory)
    except Exception as exc:
        logger.warning("bootstrap data dir picker failed: {}", exc)
        raise HTTPException(
            status_code=503,
            detail={"code": "dir_picker_failed", "message": f"目录选择框启动失败：{exc}"},
        ) from exc
    finally:
        _DIR_PICKER_LOCK.release()
    return {"ok": True, "item": {"path": path, "cancelled": not path}}


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


_BUNDLE_ERROR_CODES: tuple[tuple[str, str, int], ...] = (
    ("project is locked", "project_locked", 423),
    ("invalid password", "bundle_password_invalid", 400),
    ("password is required", "bundle_password_required", 400),
    ("unlock password is not configured", "bundle_project_password_unavailable", 400),
    ("rejected", "bundle_path_rejected", 400),
    ("unsupported config bundle version", "bundle_version_unsupported", 400),
    ("config bundle", "bundle_file_invalid", 400),
    ("master key", "bundle_file_invalid", 400),
)


def _bundle_http_exception(exc: Exception) -> HTTPException:
    message = str(exc or "").strip()
    for needle, code, status in _BUNDLE_ERROR_CODES:
        if needle in message:
            return HTTPException(status_code=status, detail=code)
    return HTTPException(status_code=400, detail="config_bundle_failed")


async def _pause_running_work_for_bundle_import(confirmed: bool) -> None:
    # 锁定态没有 web 侧运行时可被打断，此时唯一的代价是 worker 要重启才换钥匙。
    if not _service().is_unlocked():
        return
    snapshot = await _running_work_snapshot()
    if not snapshot["has_running_work"]:
        return
    if not confirmed:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "running_work_requires_confirmation",
                "message": "导入会整体替换配置面，请先确认暂停正在进行的所有对话和任务。",
                **snapshot,
            },
        )
    await _pause_running_work()


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
    _apply_data_root_choice(payload.get("data_dir") or payload.get("dataDir"))
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
    remember = bool(
        payload.get("remember")
        or payload.get("save_auto_unlock")
        or payload.get("saveAutoUnlock")
    )
    service = _service()
    try:
        service.unlock(password=password)
        if remember:
            service.set_auto_unlock(enabled=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        await _start_runtime_after_unlock()
    except Exception as exc:
        logger.warning("bootstrap unlock succeeded but runtime startup is deferred: {}", exc)
    return {"ok": True, "item": _status_payload(include_preview=False)}


@router.post("/bootstrap/change-password")
async def bootstrap_change_password(payload: dict = Body(...)):
    current_password = str(payload.get("current_password") or payload.get("currentPassword") or "")
    new_password = str(payload.get("new_password") or payload.get("newPassword") or "")
    password_confirm = str(payload.get("password_confirm") or payload.get("passwordConfirm") or "")
    if new_password != password_confirm:
        raise HTTPException(status_code=400, detail="password_confirmation_mismatch")
    try:
        item = _service().change_password(current_password=current_password, new_password=new_password)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@router.post("/bootstrap/auto-unlock")
async def bootstrap_auto_unlock(payload: dict | None = Body(default=None)):
    body = payload if isinstance(payload, dict) else {}
    if "enabled" not in body:
        raise HTTPException(status_code=400, detail="enabled_required")
    try:
        item = _service().set_auto_unlock(enabled=bool(body.get("enabled")))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@router.post("/bootstrap/lock")
async def bootstrap_lock():
    # 只清掉本进程的内存主密钥：后台任务、会话与 worker 继续跑。
    _assert_unlocked()
    return {"ok": True, "item": _service().lock()}


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


@router.post("/bootstrap/config-bundle/export")
async def bootstrap_config_bundle_export(payload: dict = Body(...)):
    raw = payload.get("use_project_password")
    if raw is None:
        raw = payload.get("useProjectPassword")
    use_project = True if raw is None else bool(raw)
    try:
        item = export_bundle(
            Path.cwd(),
            password=str(payload.get("password") or ""),
            use_project_password=use_project,
        )
    except Exception as exc:
        raise _bundle_http_exception(exc) from exc
    item.pop("path", None)
    return {"ok": True, "item": item}


@router.get("/bootstrap/config-bundle/download")
async def bootstrap_config_bundle_download(filename: str):
    if not _BUNDLE_FILENAME_RE.match(str(filename or "")):
        raise HTTPException(status_code=400, detail="bundle_filename_rejected")
    path = Path.cwd() / BUNDLE_OUTPUT_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="bundle_not_found")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@router.post("/bootstrap/config-bundle/import")
async def bootstrap_config_bundle_import(
    file: UploadFile = File(...),
    password: str = Form(...),
    confirm_running_work: bool = Form(False),
):
    try:
        await _pause_running_work_for_bundle_import(confirm_running_work)
    except HTTPException:
        raise
    except Exception as exc:
        # 读不出在跑的工作不代表不能导入：新装设备的运行时本来就没起来，这里
        # 拦下来只会让人无法导入。
        logger.warning("config bundle import: running-work check unavailable: {}", exc)
    staging_dir = Path.cwd() / BUNDLE_OUTPUT_DIR / "incoming"
    staged = staging_dir / f"{uuid4().hex}{BUNDLE_EXTENSION}"
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        with staged.open("wb") as handle:
            shutil.copyfileobj(file.file, handle)
    except Exception as exc:
        logger.warning("config bundle import: staging failed: {}", exc)
        staged.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="bundle_staging_failed") from exc
    try:
        try:
            item = import_bundle(Path.cwd(), archive_path=staged, password=password)
        except Exception as exc:
            raise _bundle_http_exception(exc) from exc
    finally:
        staged.unlink(missing_ok=True)
    try:
        refresh = await _refresh_runtime_after_save('admin_config_bundle_import')
    except Exception as exc:
        # 配置已经落盘。让这一步把请求打成 500，界面会报"失败"而实际已经导入，
        # 是最坏的一类误报。
        logger.exception("config bundle imported but runtime refresh threw")
        refresh = {
            "saved": True,
            "web_refreshed": False,
            "code": "runtime_refresh_crashed",
            "error": str(exc),
        }
    logger.info("Config bundle imported: {} entries restored", item["entry_count"])
    # 只有重启才让托管 worker 换掉内存里的旧主密钥，界面必须把这条说清楚。
    return {"ok": True, "item": {**item, "refresh": refresh, "restart_required": True}}


__all__ = ["router"]
