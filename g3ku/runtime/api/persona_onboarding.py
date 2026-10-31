"""First-run onboarding and persona read endpoints.

The routes are deliberately thin: state and file work live in
`g3ku.runtime.persona_onboarding`, memory writes go through the memory manager,
and the photo-reading turn runs in a hidden `internal:` session so it never
appears in the web session catalog or in memory review.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from loguru import logger

from g3ku.agent.persona import PERSONA_NOTE_REF
from g3ku.core.messages import UserInputMessage
from g3ku.runtime.persona_onboarding import (
    PERSONA_DESCRIPTION_MAX_CHARS,
    PERSONA_PHOTO_MAX_BYTES,
    PERSONA_PHOTO_MAX_FILES,
    OnboardingStore,
    append_onboarding_photos,
    build_photo_session_prompt,
    list_persona_photos,
    persona_photos_dir,
    record_photo_session_error,
    store_persona_photo,
    write_persona_file,
)
from g3ku.runtime.web_ceo_sessions import (
    ensure_ceo_session_metadata,
    workspace_path,
)

router = APIRouter()

PERSONA_PHOTO_SESSION_KEY = "internal:persona_photo"
# 引导页与设置页都是操作员本地面：人设整理请求必须带 owner 会话键，否则被记忆门丢弃。
PERSONA_MEMORY_SESSION_KEY = "internal:persona_memory"
_PHOTO_SESSION_TASKS: set[asyncio.Task] = set()


def _store() -> OnboardingStore:
    return OnboardingStore(workspace_path())


def _memory_manager():
    from g3ku.shells.web import get_agent

    agent = get_agent()
    manager = getattr(agent, "memory_manager", None)
    if manager is None:
        service = getattr(agent, "main_task_service", None)
        manager = getattr(service, "memory_manager", None) if service is not None else None
    if manager is None:
        raise HTTPException(status_code=503, detail={"code": "memory_manager_unavailable"})
    return manager


def _model_manager():
    from g3ku.config.model_manager import ModelManager

    return ModelManager.load()


def _has_chat_models() -> bool:
    try:
        return bool(_model_manager().list_models())
    except Exception:
        return False


def _role_chain(scope: str) -> list[str]:
    try:
        return list(_model_manager().config.get_role_model_keys(scope))
    except Exception:
        return []


def _read_persona_file() -> str:
    """Best-effort read for the pre-model onboarding status.

    A fresh install has no CEO model, so `get_agent()` cannot build the memory manager
    yet. The default notes directory is enough for the status/read surface; the
    authoritative body still goes through `MemoryManager.persona_text()` once the
    runtime exists.
    """
    try:
        path = workspace_path() / "memory" / "notes" / f"{PERSONA_NOTE_REF}.md"
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except Exception:
        return ""


def _persona_text() -> str:
    try:
        return str(_memory_manager().persona_text() or "").strip()
    except Exception:
        return _read_persona_file()


def _photo_session_task(store: OnboardingStore, photo_names: list[str]) -> None:
    async def _run() -> None:
        try:
            from g3ku.shells.web import ensure_web_runtime_services, get_agent, get_runtime_manager

            agent = get_agent()
            await ensure_web_runtime_services(agent)
            session_manager = getattr(agent, "sessions", None)
            if session_manager is None:
                raise RuntimeError("session_manager_unavailable")
            persisted = session_manager.get_or_create(PERSONA_PHOTO_SESSION_KEY)
            ensure_ceo_session_metadata(persisted)
            persisted.metadata["title"] = "人设照片"
            persisted.updated_at = datetime.now()
            session_manager.save(persisted)

            runtime_manager = get_runtime_manager(agent)
            runtime_session = runtime_manager.get_or_create(
                session_key=PERSONA_PHOTO_SESSION_KEY,
                channel="internal",
                chat_id="persona_photo",
            )
            directory = persona_photos_dir(workspace_path())
            attachments = [str(directory / name) for name in photo_names]
            content = build_photo_session_prompt(workspace_path(), photo_names)
            await runtime_session.prompt(
                UserInputMessage(content=content, attachments=attachments),
            )
        except Exception as exc:
            logger.warning("persona photo session failed: {}", exc)
            record_photo_session_error(store, str(exc))

    task = asyncio.create_task(_run())
    _PHOTO_SESSION_TASKS.add(task)
    task.add_done_callback(_PHOTO_SESSION_TASKS.discard)


def _start_photo_session(store: OnboardingStore, photo_names: list[str]) -> bool:
    if not photo_names:
        return False
    try:
        _photo_session_task(store, photo_names)
        return True
    except Exception as exc:
        logger.warning("failed to schedule persona photo session: {}", exc)
        record_photo_session_error(store, str(exc))
        return False


@router.get("/onboarding/status")
async def get_onboarding_status():
    store = _store()
    has_models = _has_chat_models()
    should_run = store.should_run(has_models=has_models)
    state = store.load()
    photos = list(state.get("photos") or [])
    return {
        "ok": True,
        "should_run": bool(should_run),
        "completed": bool(state["completed"]),
        "persona_step_done": bool(state["persona_step_done"]),
        "has_models": bool(has_models),
        "has_persona": bool(_persona_text()),
        "has_persona_input": bool(str(state.get("description") or "").strip() or photos),
        "description": str(state.get("description") or ""),
        "photos": photos,
        "photo_album": list_persona_photos(workspace_path()),
    }


@router.post("/onboarding/persona")
async def save_onboarding_persona(payload: dict | None = Body(default=None)):
    body = dict(payload or {})
    description = str(body.get("description") or "").strip()
    if len(description) > PERSONA_DESCRIPTION_MAX_CHARS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "onboarding_persona_too_long",
                "message": f"persona description exceeds {PERSONA_DESCRIPTION_MAX_CHARS} characters",
            },
        )
    if description:
        # The settings surface reads the reserved note, not the onboarding stage. Write
        # the body as soon as the step is submitted; the complete call still owns the
        # memory-agent review enqueue once the model chains exist.
        try:
            manager = _memory_manager()
            manager.set_persona(description)
        except Exception:
            write_persona_file(workspace_path(), description)
    state = _store().update(description=description, persona_step_done=True)
    return {
        "ok": True,
        "description": state["description"],
        "persona_step_done": True,
        "written": bool(description),
    }


@router.post("/onboarding/skip-persona")
async def skip_onboarding_persona():
    state = _store().update(description="", persona_step_done=True)
    return {"ok": True, "photos": list(state.get("photos") or [])}


@router.post("/onboarding/complete")
async def complete_onboarding():
    store = _store()
    state = store.load()
    if state["completed"]:
        return {"ok": True, "already_completed": True, "jobs": []}

    ceo_chain = _role_chain("ceo")
    memory_chain = _role_chain("memory")
    if not ceo_chain:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "onboarding_chains_missing",
                "message": "main model chain must not be empty",
                "missing": ["ceo"],
            },
        )

    jobs: list[str] = []
    description = str(state.get("description") or "").strip()
    if description:
        manager = _memory_manager()
        manager.set_persona(description)
        if memory_chain:
            if not str(state.get("persona_request_id") or "").strip():
                result = await manager.enqueue_operator_request(
                    op="write",
                    decision_source="user",
                    trigger_source="persona_onboarding",
                    session_key=PERSONA_MEMORY_SESSION_KEY,
                    payload_text=(
                        "用户刚在引导页写下了人设描述，并已写入人设 note。"
                        "请核对这份描述：需要整理就直接改人设 note；不要新增普通记忆，也不要重复追加已有内容。\n"
                        "---\n"
                        f"{description}"
                    ),
                )
                state["persona_request_id"] = str((result or {}).get("request_id") or "").strip()
            jobs.append("persona_enqueued")
        else:
            # 记忆 Agent 没有模型链：人设 note 已经落盘，但没有可执行整理请求的模型。
            # 不往队列塞一条注定 blocked 的请求；操作员之后配置记忆链时再走正常写入。
            jobs.append("persona_saved")
        state["description"] = ""

    photo_names = [str(name) for name in list(state.get("photos") or []) if str(name).strip()]
    if photo_names and not str(state.get("photo_session_started_at") or "").strip():
        if not memory_chain:
            # 照片回合的产出走 `memory_update`，没有记忆模型时启动它只会造一个
            # 注定无法落记忆的隐藏回合。
            jobs.append("photo_session_skipped_memory_missing")
        elif _start_photo_session(store, photo_names):
            state["photo_session_started_at"] = datetime.now().astimezone().isoformat()
            jobs.append("photo_session_started")

    state["completed"] = True
    store.save(state)
    return {"ok": True, "jobs": jobs}


@router.post("/onboarding/reset")
async def reset_onboarding():
    state = _store().reset()
    return {"ok": True, "state": state}


@router.get("/persona")
async def get_persona():
    return {
        "ok": True,
        "description": _persona_text(),
        "photos": list_persona_photos(workspace_path()),
    }


@router.post("/persona")
async def save_persona(payload: dict | None = Body(default=None)):
    """Initialize the persona from the settings dialog when it is still empty.

    A non-empty persona stays read-only in settings; later changes go through the
    memory agent. This endpoint is the one operator write that can happen before the
    memory runtime exists (fresh project without models).
    """
    body = dict(payload or {})
    description = str(body.get("description") or "").strip()
    if not description:
        raise HTTPException(
            status_code=422,
            detail={"code": "persona_empty", "message": "persona description must not be empty"},
        )
    if len(description) > PERSONA_DESCRIPTION_MAX_CHARS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "persona_too_long",
                "message": f"persona description exceeds {PERSONA_DESCRIPTION_MAX_CHARS} characters",
            },
        )
    try:
        manager = _memory_manager()
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        path = write_persona_file(workspace_path(), description)
        return {"ok": True, "description": description, "enqueued": False, "path": str(path)}
    except Exception:
        # A fresh project has no CEO model, so get_agent() raises before the memory
        # manager exists. The settings dialog can still initialize the reserved note.
        path = write_persona_file(workspace_path(), description)
        return {"ok": True, "description": description, "enqueued": False, "path": str(path)}

    manager.set_persona(description)
    enqueued = False
    if _role_chain("memory"):
        try:
            result = await manager.enqueue_operator_request(
                op="write",
                decision_source="user",
                trigger_source="persona_settings",
                session_key=PERSONA_MEMORY_SESSION_KEY,
                payload_text=(
                    "用户刚在设置页写下了人设描述，并已写入人设 note。"
                    "请核对这份描述：需要整理就直接改人设 note；不要新增普通记忆，也不要重复追加已有内容。\n"
                    "---\n"
                    f"{description}"
                ),
            )
            enqueued = bool(str((result or {}).get("request_id") or "").strip())
        except Exception as exc:
            logger.warning("persona settings enqueue failed: {}", exc)
    return {"ok": True, "description": description, "enqueued": enqueued}


@router.post("/persona/photos")
async def upload_persona_photos(files: list[UploadFile] = File(...)):
    if len(list(files or [])) > PERSONA_PHOTO_MAX_FILES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "persona_photos_too_many",
                "message": f"at most {PERSONA_PHOTO_MAX_FILES} images per upload",
            },
        )
    workspace = workspace_path()
    items: list[dict] = []
    for upload in list(files or []):
        try:
            data = await upload.read(PERSONA_PHOTO_MAX_BYTES + 1)
            if len(data) > PERSONA_PHOTO_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "code": "persona_photo_too_large",
                        "message": f"image exceeds {PERSONA_PHOTO_MAX_BYTES} bytes",
                    },
                )
            try:
                item = await asyncio.to_thread(
                    store_persona_photo,
                    workspace,
                    data,
                    filename=str(upload.filename or ""),
                    mime_type=str(getattr(upload, "content_type", "") or ""),
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "persona_photo_invalid", "message": str(exc)},
                ) from exc
            items.append(item)
        finally:
            await upload.close()
    if not items:
        raise HTTPException(status_code=400, detail={"code": "persona_photos_empty"})
    store = _store()
    if store.exists():
        append_onboarding_photos(store, [item["name"] for item in items])
    return {"ok": True, "items": items}
