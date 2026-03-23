from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from g3ku.security import DESTROY_CONFIRM_TEXT, get_bootstrap_security_service
from g3ku.shells.web import ensure_web_runtime_services, get_agent, shutdown_web_runtime

router = APIRouter()


def _service():
    return get_bootstrap_security_service()


def _status_payload(*, include_preview: bool = True) -> dict[str, Any]:
    service = _service()
    payload = dict(service.status())
    payload["destroy_confirm_text"] = DESTROY_CONFIRM_TEXT
    if include_preview and payload.get("legacy_detected"):
        try:
            payload["legacy_preview"] = service.export_legacy_state()
        except Exception:
            payload["legacy_preview"] = None
    return payload


async def _start_runtime_after_unlock() -> None:
    agent = get_agent()
    await ensure_web_runtime_services(agent)


@router.get("/bootstrap/status")
async def bootstrap_status():
    return {"ok": True, "item": _status_payload()}


@router.post("/bootstrap/setup")
async def bootstrap_setup(payload: dict = Body(...)):
    password = str(payload.get("password") or "")
    password_confirm = str(payload.get("password_confirm") or payload.get("passwordConfirm") or "")
    display_name = str(payload.get("display_name") or payload.get("displayName") or "").strip()
    confirm_legacy_reset = bool(payload.get("confirm_legacy_reset") or payload.get("confirmLegacyReset"))
    if password != password_confirm:
        raise HTTPException(status_code=400, detail="password_confirmation_mismatch")
    service = _service()
    try:
        service.setup_initial_realm(
            password=password,
            display_name=display_name,
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


@router.post("/bootstrap/lock")
async def bootstrap_lock():
    service = _service()
    try:
        await shutdown_web_runtime()
        item = service.lock()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@router.patch("/bootstrap/realm")
async def bootstrap_rename_realm(payload: dict = Body(...)):
    display_name = str(payload.get("display_name") or payload.get("displayName") or "").strip()
    try:
        item = _service().rename_current_realm(display_name=display_name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@router.post("/bootstrap/realms")
async def bootstrap_create_realm(payload: dict = Body(...)):
    password = str(payload.get("password") or "")
    password_confirm = str(payload.get("password_confirm") or payload.get("passwordConfirm") or "")
    display_name = str(payload.get("display_name") or payload.get("displayName") or "").strip()
    if password != password_confirm:
        raise HTTPException(status_code=400, detail="password_confirmation_mismatch")
    try:
        item = _service().create_realm(password=password, display_name=display_name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@router.post("/bootstrap/destroy-all-secrets")
async def bootstrap_destroy_all_secrets(payload: dict = Body(...)):
    confirm_text = str(payload.get("confirm_text") or payload.get("confirmText") or "")
    service = _service()
    try:
        await shutdown_web_runtime()
        item = service.destroy_all_secrets(confirm_text=confirm_text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": item}


__all__ = ["router"]
