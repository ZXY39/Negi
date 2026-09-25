"""/api/update —— 已安装设备的版本状态、手动复检与「重启并更新」。

三个端点都不做常驻工作：状态只读 `.g3ku/update-check.json` 台账（零网络），
复检是一次 `git ls-remote`，apply 只负责把脱离执行体踢起来后返回。关停语义不
在这里复制 —— 执行体走现成的 `POST /api/bootstrap/exit`，因此"有在跑的活未确认"
的 409 只有一份实现。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from loguru import logger

from g3ku import __version__
from g3ku.update_apply import spawn_runner
from g3ku.update_check import read_update_ledger, run_update_check

router = APIRouter()

_REF_RE = re.compile(r"^v?\d+\.\d+\.\d+$")


def _settings() -> tuple[bool, float]:
    try:
        from g3ku.config.loader import load_config

        section = getattr(load_config(), "update_check", None)
        if section is None:
            return True, 5.0
        return bool(section.enabled), float(section.interval_hours)
    except Exception:
        return True, 5.0


def _status_payload() -> dict[str, Any]:
    ledger = read_update_ledger()
    enabled, interval_hours = _settings()
    return {
        "has_ledger": ledger is not None,
        "newer": bool((ledger or {}).get("newer")),
        "latest_tag": str((ledger or {}).get("latest_tag") or ""),
        "current_version": __version__,
        "checked_at": str((ledger or {}).get("checked_at") or ""),
        "source": str((ledger or {}).get("source") or ""),
        "error": str((ledger or {}).get("error") or ""),
        "enabled": enabled,
        "interval_hours": interval_hours,
    }


@router.get("/update/status")
async def update_status():
    return {"ok": True, "item": _status_payload()}


@router.post("/update/check")
async def update_check_now():
    enabled, interval_hours = _settings()
    if not enabled:
        raise HTTPException(
            status_code=409,
            detail={"code": "update_check_disabled", "message": "自动检查已在配置里关闭。"},
        )
    try:
        ledger = await asyncio.to_thread(
            run_update_check,
            source="manual",
            interval_seconds=max(1.0, interval_hours * 3600.0),
            force=True,
        )
    except Exception as exc:
        logger.debug("manual update check failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "item": {**_status_payload(), "checked": ledger is not None}}


@router.post("/update/apply")
async def update_apply(payload: dict | None = Body(default=None)):
    body = payload or {}
    requested_ref = str(body.get("ref") or "").strip()
    ledger = read_update_ledger()
    ref = requested_ref or str((ledger or {}).get("latest_tag") or "").strip()
    if not ref:
        raise HTTPException(
            status_code=409,
            detail={"code": "no_update_available", "message": "还没查出可用的新版本。"},
        )
    if not _REF_RE.match(ref):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_ref", "message": f"版本号格式不正确：{ref}"},
        )
    # 没点名版本时只在真有新版本时才动作，避免把设备无谓地重启一次。
    if not requested_ref and not (ledger or {}).get("newer"):
        raise HTTPException(
            status_code=409,
            detail={"code": "no_update_available", "message": "当前已是最新版本，无需更新。"},
        )
    # 默认允许执行体确认暂停在跑的会话与任务；前端确认框负责把这句话讲给用户。
    pause_running_work = bool(body.get("pause_running_work", True))
    try:
        # 把当前监听端口交给执行体：让它自己猜端口，可能关掉同机的另一个实例。
        from g3ku.config.loader import load_config

        live_port = int(load_config().web.port or 0) or None
    except Exception:
        live_port = None
    try:
        spawn_runner(ref, port=live_port, pause_running_work=pause_running_work)
    except OSError as exc:
        logger.warning("update apply spawn failed: {}", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    logger.info("update apply scheduled ref={}", ref)
    return {"ok": True, "item": {"restarting": True, "ref": ref, "pause_running_work": pause_running_work}}
