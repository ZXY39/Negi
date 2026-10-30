"""沙箱档位 API：会话级开关 + 安装态读数。

契约 owner：``docs/architecture/sandbox-execution-plane.md``「档位模型（意图 × 安装态）」。

- `GET  /api/ceo/sessions/{id}/sandbox` —— 当前意图、解析出的档位、安装态、逐维度强制力，
  以及**服务端给出的 UI 标签**（前端不自己判安装态，避免两处口径漂移）。
- `PATCH /api/ceo/sessions/{id}/sandbox` —— 只改意图；**任何信任级的开/关沙箱**
  （mode 与当前意图不同）都必须带项目解锁口令（服务端只读校验，不顺便解锁项目）。
  只改 `writable_roots`、或 mode 与原意图相同，则不需要口令。
- `GET  /api/sandbox/status` —— 全局安装态与后端读数（进程内缓存，`?refresh=1` 重探）。
- `POST /api/sandbox/refresh` —— 重探安装态。

沙箱页面里的文件/包/磁盘一族端点属于 jail 后端落地之后的事，本模块不提供——
没有后端时"管理沙箱内容"没有对象可管。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from loguru import logger

from g3ku.audit_events import emit_audit_event
from g3ku.runtime.external_sessions import get_external_session_registry
from g3ku.runtime.session_trust import TRUST_OWNER, session_trust
from g3ku.sandbox.errors import SandboxDeniedError, SandboxUnavailableError
from g3ku.sandbox.installer import (
    build_install_plan,
    install_status,
    start_install,
    start_uninstall,
)
from g3ku.sandbox.policy import (
    LIMITED_DEFAULT_TOOL_NAMES,
    LIMITED_FIXED_TOOL_NAMES,
    LIMITED_TOOL_WARNINGS,
    MODE_RESTRICTED,
    MODE_UNRESTRICTED,
    TIER_HOST_FULL,
    TIER_JAILED,
    TIER_LIMITED,
    TIER_READONLY,
    default_mode_for_trust,
    session_policy,
)
from g3ku.sandbox.probe import BACKEND_WSL_BWRAP
from g3ku.sandbox.state import (
    get_session_mode,
    get_writable_roots,
    install_state,
    install_state_name,
    note_operator_unlock,
    operator_unlock_note,
    refresh_install_state,
    sandbox_persona_text,
    set_sandbox_persona_text,
    set_session_allowed_tools,
    set_session_mode,
    set_writable_roots,
)
from g3ku.utils.helpers import safe_filename

router = APIRouter()

_LABELS_NOT_INSTALLED = {"restricted": "受限", "unrestricted": "全权限"}
_LABELS_INSTALLED = {"restricted": "开启沙箱", "unrestricted": "关闭沙箱"}
_TIER_LABELS = {
    TIER_LIMITED: "受限",
    TIER_READONLY: "只读",
    TIER_JAILED: "沙箱",
    TIER_HOST_FULL: "宿主全权限",
}


def _normalized_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw not in {MODE_RESTRICTED, MODE_UNRESTRICTED}:
        raise HTTPException(status_code=400, detail="sandbox_mode_invalid")
    return raw


def _ui_labels(reading: dict[str, Any]) -> dict[str, str]:
    state = str(reading.get("state") or "")
    if state in {"ready", "partial"}:
        return dict(_LABELS_INSTALLED)
    return dict(_LABELS_NOT_INSTALLED)


def _session_exists(session_key: str) -> bool:
    if not session_key:
        return False
    try:
        if get_external_session_registry().get_by_session_key(session_key) is not None:
            return True
    except Exception:  # noqa: BLE001 - 注册表不可读时退回转录判定
        pass
    if session_key.startswith("web:"):
        # 本地会话键由前端自由生成，没有"注册"这一步；存在性由目录接口负责。
        return True
    try:
        from g3ku.runtime.api.external_v1 import _session_manager

        return _session_manager().has_transcript(session_key)
    except Exception:  # noqa: BLE001
        return False


def _session_item(session_key: str) -> dict[str, Any]:
    reading = install_state()
    policy = session_policy(session_key)
    stored_mode = get_session_mode(session_key)
    unlock_note = operator_unlock_note(session_key)
    return {
        "session_id": session_key,
        "trust": policy.trust,
        "mode": policy.mode,
        "stored_mode": stored_mode,
        "default_mode": default_mode_for_trust(policy.trust),
        "resolved_tier": policy.tier,
        "tier_label": _TIER_LABELS.get(policy.tier, policy.tier),
        # 受限模式的按会话工具列表；其他档位不读这份。
        "allowed_tools": (
            sorted(getattr(policy, "allowlist", None) or ())
            if policy.tier == TIER_LIMITED
            else []
        ),
        "allowed_tools_customized": getattr(policy, "explicit_allowed_tool_names", None) is not None,
        "default_allowed_tools": sorted(LIMITED_DEFAULT_TOOL_NAMES | LIMITED_FIXED_TOOL_NAMES),
        "fixed_tools": sorted(LIMITED_FIXED_TOOL_NAMES),
        "tool_warnings": dict(LIMITED_TOOL_WARNINGS),
        "exec_allowed": bool(getattr(policy, "exec_allowed", False)),
        "exec_requires_governed": bool(getattr(policy, "exec_requires_governed", False)),
        "install_state": str(reading.get("state") or ""),
        "backend": str(reading.get("backend") or ""),
        "enforcement": dict(reading.get("enforcement") or {}),
        "install_detail": dict(reading.get("detail") or {}),
        "ui_labels": _ui_labels(reading),
        # 开/关沙箱都是安全边界改变，任何信任级都要口令（后端在 PATCH 里只读校验）。
        "unlock_required": True,
        "operator_unlock": unlock_note,
        # 受限档不派任务：UI 用它来决定任务按钮的可用性提示。
        "tasks_allowed": policy.tier == TIER_HOST_FULL,
        # D14：owner 的额外可写根；third_party 恒空（列表只读，写入走 PATCH 的 owner 闸门）。
        "writable_roots": get_writable_roots(session_key),
    }


def _roots_unreachable_in_jail(session_key: str) -> bool:
    """这一档会不会落到 WSL jail——那里的宿主路径永远挂不上。

    `/mnt` 在禁绑前缀里，`E:\\…` 又不是发行版路径，profile 只会静默跳过这类根。存下
    一个做不到的承诺比拒绝更坏：操作者以为交付目录已挂上，模型每轮都撞
    `host path is not reachable inside the sandbox`。
    """
    reading = install_state()
    if str(reading.get("backend") or "") != BACKEND_WSL_BWRAP:
        return False
    return str(session_policy(session_key).tier or "") == TIER_JAILED


def _verify_unlock_password(password: str) -> bool:
    """只读校验：解得开本机信封即通过，不激活、不落盘。"""
    from g3ku.security.bootstrap import verify_unlock_password

    return verify_unlock_password(password)


@router.get("/ceo/sessions/{session_id}/sandbox")
async def get_session_sandbox(session_id: str):
    key = str(session_id or "").strip()
    if not _session_exists(key):
        raise HTTPException(status_code=404, detail="session_not_found")
    return {"ok": True, "item": _session_item(key)}


@router.patch("/ceo/sessions/{session_id}/sandbox")
async def patch_session_sandbox(session_id: str, payload: dict = Body(...)):
    key = str(session_id or "").strip()
    if not _session_exists(key):
        raise HTTPException(status_code=404, detail="session_not_found")
    trust = session_trust(key)
    current = session_policy(key)
    mode = _normalized_mode(payload.get("mode")) if "mode" in payload else None
    # 开/关沙箱都是安全边界的改变：任何信任级都要用项目解锁口令二次确认，
    # 不只是第三方会话切 unrestricted。只改可写根（mode 缺省或与原意图相同）不走这道闸。
    mode_changed = mode is not None and mode != current.mode
    if mode_changed:
        password = str(payload.get("unlock_password") or payload.get("unlockPassword") or "")
        if not password:
            raise HTTPException(status_code=403, detail="unlock_password_required")
        if not _verify_unlock_password(password):
            raise HTTPException(status_code=403, detail="unlock_password_invalid")
        from datetime import datetime

        note_operator_unlock(key, at=datetime.now().astimezone().isoformat(), by="operator")
    if mode is not None:
        set_session_mode(key, mode)
    if "writable_roots" in payload:
        if trust != TRUST_OWNER:
            raise HTTPException(status_code=403, detail="writable_roots_owner_only")
        raw_roots = payload.get("writable_roots")
        if not isinstance(raw_roots, list):
            raise HTTPException(status_code=400, detail="writable_roots_must_be_list")
        if raw_roots and _roots_unreachable_in_jail(key):
            raise HTTPException(status_code=400, detail="writable_roots_unreachable_in_backend")
        set_writable_roots(key, raw_roots)
    # 档位只在回合边界生效：这里只落意图，正在跑的回合不受影响。
    item = _session_item(key)
    item["effective_from"] = "next_turn"
    if mode is not None:
        try:
            emit_audit_event(
                "sandbox",
                "info",
                "mode_changed",
                "session sandbox mode changed",
                detail={
                    "session_id": key,
                    "mode": mode,
                    "previous_mode": current.mode,
                    "changed": mode_changed,
                    "tier": item.get("resolved_tier"),
                    "trust": item.get("trust"),
                    "unlocked": mode_changed,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 审计失败不阻塞开关
            logger.warning("sandbox mode audit emit failed: {}", exc)
    return {"ok": True, "item": item}


def _normalized_allowed_tool_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail="allowed_tools_must_be_list")
    cleaned: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > 128:
            raise HTTPException(status_code=400, detail="tool_id_too_long")
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


@router.get("/ceo/sessions/{session_id}/sandbox/tools")
async def get_session_sandbox_tools(session_id: str):
    key = str(session_id or "").strip()
    if not _session_exists(key):
        raise HTTPException(status_code=404, detail="session_not_found")
    policy = session_policy(key)
    item = _session_item(key)
    item["applicable"] = policy.tier == TIER_LIMITED
    return {"ok": True, "item": item}


@router.patch("/ceo/sessions/{session_id}/sandbox/tools")
async def patch_session_sandbox_tools(session_id: str, payload: dict = Body(...)):
    key = str(session_id or "").strip()
    if not _session_exists(key):
        raise HTTPException(status_code=404, detail="session_not_found")
    password = str(payload.get("unlock_password") or payload.get("unlockPassword") or "")
    if not password:
        raise HTTPException(status_code=403, detail="unlock_password_required")
    if not _verify_unlock_password(password):
        raise HTTPException(status_code=403, detail="unlock_password_invalid")
    selected = _normalized_allowed_tool_list(payload.get("allowed_tools"))
    fixed = set(LIMITED_FIXED_TOOL_NAMES)
    effective = set(selected) | fixed
    default_set = set(LIMITED_DEFAULT_TOOL_NAMES) | fixed
    previous = session_policy(key)
    previous_allowed = set(previous.allowlist or ()) if previous.tier == TIER_LIMITED else set()
    # 与默认集合完全一致时清掉 override，未来默认集合调整能自动跟随。
    set_session_allowed_tools(key, [] if effective == default_set else sorted(effective))
    from datetime import datetime

    note_operator_unlock(key, at=datetime.now().astimezone().isoformat(), by="operator")
    current = session_policy(key)
    current_allowed = set(current.allowlist or ()) if current.tier == TIER_LIMITED else set()
    try:
        emit_audit_event(
            "sandbox",
            "info",
            "sandbox_tools_changed",
            "session allowed tools changed",
            detail={
                "session_id": key,
                "tier": current.tier,
                "added": sorted(current_allowed - previous_allowed),
                "removed": sorted(previous_allowed - current_allowed),
                "exec_allowed": current.exec_allowed,
                "customized": current.explicit_allowed_tool_names is not None,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 审计失败不阻塞保存
        logger.warning("sandbox tools audit emit failed: {}", exc)
    item = _session_item(key)
    item["applicable"] = current.tier == TIER_LIMITED
    item["effective_from"] = "next_turn"
    return {"ok": True, "item": item}


@router.get("/ceo/sessions/{session_id}/sandbox/files")
async def get_session_sandbox_files(
    session_id: str,
    area: str = "workspace",
    limit: int = 200,
):
    handle, manager = _installed_handle(session_id)
    from g3ku.sandbox.bridge_files import list_area

    normalized_area = str(area or "workspace").strip().lower()
    entries = await asyncio.to_thread(list_area, handle, area=normalized_area, max_entries=max(1, min(int(limit or 200), 1000)))
    return {"ok": True, "item": {"area": normalized_area, "entries": entries}}


@router.post("/ceo/sessions/{session_id}/sandbox/import")
async def post_session_sandbox_import(session_id: str, file: UploadFile = File(...)):
    handle, _manager = _installed_handle(session_id)
    from g3ku.sandbox.bridge_files import import_file

    staging_dir = await asyncio.to_thread(lambda: _staging_dir())
    staged = staging_dir / f"{uuid4().hex}-{safe_filename(file.filename or 'upload.bin') or 'upload.bin'}"
    try:
        with staged.open("wb") as output:
            await asyncio.to_thread(shutil.copyfileobj, file.file, output)
        result = await asyncio.to_thread(import_file, handle, staged)
    except SandboxDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SandboxUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        staged.unlink(missing_ok=True)
    try:
        emit_audit_event(
            "sandbox",
            "info",
            "files_imported",
            "file imported into the sandbox inbox",
            detail={"session_id": session_id, "name": result.get("name"), "bytes": result.get("bytes")},
        )
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "item": result}


@router.get("/ceo/sessions/{session_id}/sandbox/export")
async def get_session_sandbox_export(
    session_id: str,
    name: str,
    area: str = "outbox",
):
    handle, _manager = _installed_handle(session_id)
    from g3ku.sandbox.bridge_files import export_file

    try:
        resolved_name, payload = await asyncio.to_thread(export_file, handle, name, area=str(area or "outbox").lower())
    except SandboxDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SandboxUnavailableError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{resolved_name}"'},
    )


@router.post("/ceo/sessions/{session_id}/sandbox/reset")
async def post_session_sandbox_reset(session_id: str, payload: dict = Body(...)):
    if not bool(payload.get("confirm")):
        raise HTTPException(status_code=400, detail="reset_confirmation_required")
    handle, manager = _installed_handle(session_id)
    key = str(session_id or "").strip()
    trust = session_trust(key)
    await asyncio.to_thread(manager.forget, key)
    new_handle = await asyncio.to_thread(manager.ensure, key, trust=trust)
    try:
        emit_audit_event(
            "sandbox",
            "warning",
            "volume_reset",
            "sandbox volume reset",
            detail={"session_id": key, "sandbox_id": new_handle.sandbox_id},
        )
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "item": _session_item(key), "paths": dict(new_handle.paths)}


def _staging_dir() -> Path:
    from g3ku.deployment.data_root import data_work_path

    directory = data_work_path("sandbox") / "incoming"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _sandbox_manager():
    from g3ku.sandbox.factory import get_sandbox_manager

    return get_sandbox_manager()


def _installed_handle(session_id: str):
    """文件管理只在装好之后才有对象可管；没装就是 409，不是空列表。"""
    key = str(session_id or "").strip()
    if not _session_exists(key):
        raise HTTPException(status_code=404, detail="session_not_found")
    reading = install_state()
    if str(reading.get("state") or "") not in {"ready", "partial"}:
        raise HTTPException(status_code=409, detail="sandbox_not_installed")

    manager = _sandbox_manager()
    if manager is None:
        raise HTTPException(status_code=409, detail="sandbox_backend_unavailable")
    try:
        handle = manager.ensure(key, trust=session_trust(key))
    except SandboxUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return handle, manager


@router.get("/sandbox/status")
async def get_sandbox_status(refresh: int = Query(0)):
    reading = refresh_install_state() if int(refresh or 0) else install_state()
    plan = build_install_plan(reading)
    return {
        "ok": True,
        "item": {
            "install_state": str(reading.get("state") or ""),
            "backend": str(reading.get("backend") or ""),
            "reason": str(reading.get("reason") or ""),
            "enforcement": dict(reading.get("enforcement") or {}),
            "detail": dict(reading.get("detail") or {}),
            "ui_labels": _ui_labels(reading),
            "install_available": bool(plan.installable),
            "install_plan_reason": plan.reason,
            "file_management_available": str(reading.get("state") or "") in {"ready", "partial"},
        },
    }


@router.get("/sandbox/install/plan")
async def get_install_plan():
    plan = build_install_plan()
    return {"ok": True, "item": plan.to_dict(), "status": install_status()}


@router.post("/sandbox/install")
async def post_sandbox_install(payload: dict = Body(...)):
    result = start_install(confirm=bool(payload.get("confirm")))
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=str(result.get("detail") or "install_rejected"))
    try:
        emit_audit_event(
            "sandbox",
            "info",
            "install_started",
            "sandbox runtime install started",
            detail={"steps": [step["id"] for step in result["plan"]["steps"]]},
        )
    except Exception as exc:  # noqa: BLE001 - 审计失败不阻塞安装
        logger.warning("sandbox install audit emit failed: {}", exc)
    return result


@router.get("/sandbox/install/status")
async def get_install_status():
    return {"ok": True, "item": install_status()}


@router.post("/sandbox/uninstall")
async def post_sandbox_uninstall(payload: dict = Body(...)):
    result = start_uninstall(confirm=bool(payload.get("confirm")))
    if result.get("ok"):
        try:
            emit_audit_event(
                "sandbox",
                "info",
                "uninstall_finished",
                "sandbox runtime uninstall finished",
                detail={"results": result.get("results") or []},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sandbox uninstall audit emit failed: {}", exc)
        return result
    detail = str(result.get("detail") or "uninstall_rejected")
    if detail == "nothing_we_installed":
        return {"ok": False, **result}
    raise HTTPException(status_code=409, detail=detail)


@router.get("/sandbox/persona")
async def get_sandbox_persona():
    return {"ok": True, "item": {"persona": sandbox_persona_text()}}


@router.put("/sandbox/persona")
async def put_sandbox_persona(payload: dict = Body(...)):
    text = set_sandbox_persona_text(payload.get("persona"))
    try:
        emit_audit_event(
            "sandbox",
            "info",
            "persona_updated",
            "third-party persona updated",
            detail={"chars": len(text)},
        )
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "item": {"persona": text}}


@router.post("/sandbox/refresh")
async def post_sandbox_refresh():
    reading = refresh_install_state()
    return {
        "ok": True,
        "item": {
            "install_state": install_state_name(),
            "backend": str(reading.get("backend") or ""),
            "enforcement": dict(reading.get("enforcement") or {}),
        },
    }
