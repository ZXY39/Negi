"""/api/update —— 已安装设备的版本状态、手动复检、发布说明与「重启并更新」。

四个端点都不做常驻工作：状态只读 `.g3ku/update-check.json` 台账（零网络），
复检是一次版本解析（git 与发布页两条车道，收在 `g3ku.update_check.resolve_latest_release`），
发布说明是一次 GitHub 读取（取不到只回错误码），
apply 只负责把脱离执行体踢起来后返回。关停语义不在这里复制 —— 执行体走现成的
`POST /api/bootstrap/exit`，因此"有在跑的活未确认"的 409 只有一份实现。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from loguru import logger

from g3ku import __version__
from g3ku.update_apply import PROJECT_ROOT, apply_in_flight, spawn_runner
from g3ku.update_check import (
    fetch_release_notes,
    ledger_newer_now,
    read_apply_result,
    read_update_ledger,
    release_in_local_head,
    release_page_url,
    run_update_check,
    upgrade_lane_available,
)

router = APIRouter()

_REF_RE = re.compile(r"^v?\d+\.\d+\.\d+$")


def _settings() -> tuple[bool, float, str]:
    """(enabled, interval_hours, git_path)；读不到配置就按默认放行。"""
    try:
        from g3ku.config.loader import load_config

        section = getattr(load_config(), "update_check", None)
        if section is None:
            return True, 5.0, ""
        return (
            bool(section.enabled),
            float(section.interval_hours),
            str(getattr(section, "git_path", "") or ""),
        )
    except Exception:
        return True, 5.0, ""


def _status_payload() -> dict[str, Any]:
    ledger = read_update_ledger()
    enabled, interval_hours, _git_path = _settings()
    return {
        "has_ledger": ledger is not None,
        "newer": ledger_newer_now(ledger),
        "latest_tag": str((ledger or {}).get("latest_tag") or ""),
        # 台账里那一版的发布页，供弹窗旁的「手动安装」用；没有版本就是空串。
        "release_url": release_page_url(str((ledger or {}).get("latest_tag") or "")),
        "current_version": __version__,
        "checked_at": str((ledger or {}).get("checked_at") or ""),
        "source": str((ledger or {}).get("source") or ""),
        # error 是失败档位（git_missing / remote_unreachable / timeout /
        # no_release_tag），空串表示这次查到了；detail 保留每条车道的原文。
        "error": str((ledger or {}).get("error") or ""),
        "lane": str((ledger or {}).get("lane") or ""),
        "detail": str((ledger or {}).get("detail") or ""),
        "degraded": bool((ledger or {}).get("degraded")),
        # actionable 答的是"报出去的这一版这台设备升不升得动"，前端据此决定给不给
        # 升级入口；每次读都现场判，台账里那次检查的环境可能已经变了。
        "actionable": upgrade_lane_available()[0],
        "enabled": enabled,
        "interval_hours": interval_hours,
        # 执行体自己写的终态：apply 端点回 restarting 时不代表升级成功过。
        "last_apply": read_apply_result(),
    }


@router.get("/update/status")
async def update_status():
    return {"ok": True, "item": _status_payload()}


@router.post("/update/check")
async def update_check_now():
    enabled, interval_hours, git_path = _settings()
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
            git_path=git_path,
        )
    except Exception as exc:
        logger.debug("manual update check failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "item": {**_status_payload(), "checked": ledger is not None}}


@router.get("/update/release-notes")
async def update_release_notes(ref: str = ""):
    """某一版的发布说明（GitHub release 正文），供「查看新版本」弹窗展示。

    取不到正文不是 5xx：升级走 git，说明只是阅读材料，前端照常允许更新。
    """
    tag = str(ref or "").strip()
    if not _REF_RE.match(tag):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_ref", "message": f"版本号格式不正确：{tag}"},
        )
    item = await asyncio.to_thread(fetch_release_notes, tag, project_root=PROJECT_ROOT)
    return {"ok": True, "item": item}


@router.post("/update/apply")
async def update_apply(request: Request, payload: dict | None = Body(default=None)):
    body = payload or {}
    requested_ref = str(body.get("ref") or "").strip()
    git_path = _settings()[2]
    # 单飞：上一次还挂在 started 就拒掉，不重跑安装、也不起第二个执行体。放在最前面，
    # 连远端那一次查询都不必花。
    pending = apply_in_flight()
    if pending:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "apply_in_flight",
                "message": "上一次重启并更新还在执行中，请等它结束再点。",
                "ref": str(pending.get("ref") or ""),
                "started_at": str(pending.get("at") or ""),
            },
        )
    ledger = read_update_ledger()
    # 先证这台设备自己动手升得动，再花远端那一次读：git 检出靠安装脚本 fetch，而脚本
    # 按名字找 git。放行下去的话执行体会先关停服务、再在 install 那步失败、再把旧版本
    # 拉回来 —— 用户白等一次停机，终态只剩一句 upgrade_failed。
    lane_ok, lane_code = upgrade_lane_available()
    if not lane_ok:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "git_unavailable",
                "message": (
                    "这台设备找不着 git，自动升级跑不起来：把 git 放回系统 PATH，或在配置里写"
                    " update_check.git_path，然后重启服务再点。（检查更新本身不受影响，"
                    "它已经能用发布页那条车道读版本。）"
                ),
                "lane": lane_code,
            },
        )
    if requested_ref:
        ref = requested_ref
    else:
        # 用户点「重启并更新」就是"升到当前最新"的动作本身，所以这里现场查一次，
        # 绝不照台账里的旧 latest_tag 动手：重启后台账要等满一个检查间隔才刷新，
        # 实盘 12:58 那次就拿 11:32 查出的 v1.0.9 去升，而那一刻 v1.0.10 已经带
        # 着桥修复发出去了 —— 照陈旧值升级会把带修复的树换回没有修复的旧版。
        # 不受 update_check.enabled 约束：那个开关管的是自动轮询，而 apply 本身
        # 就要求能访问远端。
        fresh = await asyncio.to_thread(run_update_check, source="manual", force=True, git_path=git_path)
        if not fresh or not str(fresh.get("latest_tag") or "").strip():
            code = str((fresh or {}).get("error") or "") or "no_release_tag"
            detail = str((fresh or {}).get("detail") or "").strip()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "check_failed",
                    "message": (
                        f"现在查不到远端版本（{code}{'：' + detail if detail else ''}），"
                        "两条车道分别是 git ls-remote 与发布页 releases/latest，先确认这台设备能访问 GitHub 再点。"
                    ),
                    "error": code,
                },
            )
        ledger = fresh
        ref = str(fresh.get("latest_tag") or "").strip()
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
    # 这一版已经在这棵树的 HEAD 历史里时先把话说清：换码只会把工作区切回那个标签，代码一行不会
    # 变新，却要付一次停机、一次本地改动 stash、以及浅仓化/游离 HEAD 的代价（开发者自己的树上
    # 这是真损失）。判不出来的设备（浅仓上标签对象不在本地、没有 git、超时）一律放行——读不出
    # 历史不能反过来拦掉正常升级。
    if release_in_local_head(ref, git_path=git_path) is True:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "already_at_or_ahead_of_release",
                "message": (
                    f"这棵树的代码已经包含 {ref}，再点一次只会把工作区切回那个标签"
                    "（代码不会变新，还会停一次服务）。要升到更新的版本就等下一次发布，"
                    "或点名那个版本。"
                ),
                "ref": ref,
            },
        )
    # 没点名版本时只在真有新版本时才动作，避免把设备无谓地重启一次。
    if not requested_ref and not ledger_newer_now(ledger):
        raise HTTPException(
            status_code=409,
            detail={"code": "no_update_available", "message": "当前已是最新版本，无需更新。"},
        )
    # 默认允许执行体确认暂停在跑的会话与任务；前端确认框负责把这句话讲给用户。
    pause_running_work = bool(body.get("pause_running_work", True))
    # 端口取"这次请求真正到达的端口"：config.web.port 只是配置值，进程可以带
    # --port 启动而不回写配置，用配置值会让执行体去关同机另一个实例，或在错误
    # 端口上空等 90 秒后放弃（实测复现过）。配置值只作兜底。
    live_port = request.url.port or request.base_url.port
    if not live_port:
        try:
            from g3ku.config.loader import load_config

            live_port = int(load_config().web.port or 0) or None
        except Exception:
            live_port = None
    try:
        spawn_runner(ref, port=live_port, pause_running_work=pause_running_work)
    except OSError as exc:
        logger.warning("update apply spawn failed: {}", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    logger.info("update apply scheduled ref={} port={}", ref, live_port)
    return {"ok": True, "item": {"restarting": True, "ref": ref, "port": live_port, "pause_running_work": pause_running_work}}
