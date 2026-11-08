"""External Agent API — channel-agnostic headless surface (``/api/v1``).

Consumed by third-party bridge applications (IM bots, automation bridges).
Contract owner: ``docs/architecture/external-agent-api.md``.

Endpoints (all Bearer-authenticated via ``require_external_api``; the global
bootstrap lock middleware returns 423 while the project is locked):

- ``POST   /sessions``                     get-or-create by ``external_key``
- ``GET    /sessions``                     list this bridge's sessions
- ``PATCH  /sessions/{session_id}``        rename (title)
- ``DELETE /sessions/{session_id}``        clear semantics (context reset, entry kept)
- ``GET    /sessions/{session_id}/state``  running/queued/inflight snapshot
- ``POST   /sessions/{session_id}/messages`` submit turn (or queue follow-up)
- ``POST   /turns/{turn_id}/pause``        pause the turn's session
- ``POST   /sessions/{session_id}/cancel`` cancel session tasks
- ``GET    /sessions/{session_id}/events`` SSE event stream (replay via Last-Event-ID)
- ``GET    /outbox/pending``               pending durable pushes for this bridge (pump warm-up)
- ``POST   /sessions/{session_id}/outbox/{outbox_id}/ack`` mark a durable push delivered
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import mimetypes
import threading
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from loguru import logger

from g3ku.config.live_runtime import get_runtime_config
from g3ku.core.messages import UserInputMessage
from g3ku.qq_official.group_history import digest_artifact_task_id
from g3ku.runtime.api.ceo_sessions import _publish_ceo_sessions_snapshot
from g3ku.runtime.api.external_auth import ExternalApiPrincipal, require_external_api
from g3ku.runtime.api.external_turns import get_external_turn_service
from g3ku.runtime.attachments import channel_attachment_note
from g3ku.runtime.ceo_catalog_offload import build_ceo_session_catalog_async
from g3ku.runtime.external_events import (
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    get_session_event_hub,
)
from g3ku.runtime.external_member_roster import get_member_roster
from g3ku.runtime.external_outbox import (
    PROACTIVE_EVENT,
    REPLY_EVENT,
    ack_outbound_message,
    get_outbound_record,
    is_pull_consumable,
    load_pending_outbound,
)
from g3ku.runtime.external_sessions import (
    ExternalSessionEntry,
    get_external_session_registry,
)
from g3ku.runtime.qq_bot_identity import set_qq_bot_identity
from g3ku.runtime.session_keys import sanitize_channel_outbound_text
from g3ku.runtime.session_trust import (
    chat_type_from_external_key,
    is_multi_party_chat,
    normalize_chat_type,
    note_inbound_session_shape,
    qq_owner_openids,
)
from g3ku.runtime.web_ceo_sessions import (
    EXTERNAL_UPLOAD_ROOT,
    WEB_CEO_IMAGE_UPLOAD_MAX_BYTES,
    WebCeoStateStore,
    build_channel_ceo_session_item,
    clear_web_ceo_session_artifacts,
    workspace_path,
)
from g3ku.session.manager import SessionManager
from g3ku.shells.web import (
    get_runtime_manager,
    get_web_heartbeat_service,
    peek_global_agent,
)
from g3ku.utils.helpers import ensure_dir, safe_filename

router = APIRouter()

# 一次失败只唤醒一次：按 outbox_id 去重（不按时间；每次不同的失败都要提醒模型）。
# 投递失败与附件失败共用这一份 LRU，但键带前缀——同一条 ack 同时带两类事实时，
# 两个分支不能互相吃掉。
_DELIVERY_FAILURE_SEEN: "OrderedDict[str, None]" = OrderedDict()
_DELIVERY_FAILURE_LOCK = threading.RLock()
_DELIVERY_FAILURE_SEEN_MAX = 1024


def _remember_delivery_failure(outbox_id: str, *, prefix: str = "delivery") -> bool:
    target = str(outbox_id or "").strip()
    if not target:
        return False
    key = f"{prefix}:{target}"
    with _DELIVERY_FAILURE_LOCK:
        if key in _DELIVERY_FAILURE_SEEN:
            return False
        _DELIVERY_FAILURE_SEEN[key] = None
        while len(_DELIVERY_FAILURE_SEEN) > _DELIVERY_FAILURE_SEEN_MAX:
            _DELIVERY_FAILURE_SEEN.popitem(last=False)
        return True


# 附件失败记录白名单：桥回的字段进心跳事件前先裁掉不认识的键，列表与错误原文
# 各有一道上限（桥侧也截，这里是服务端边界）。
_ATTACHMENT_FAILURE_KEYS = (
    "name",
    "kind",
    "mime_type",
    "size",
    "file_type",
    "stage",
    "attempts",
    "elapsed_seconds",
    "failed_at",
    "http_status",
    "platform_code",
    "platform_message",
    "url_fallback",
)
_ATTACHMENT_FAILURES_MAX = 10
_ATTACHMENT_FAILURE_MESSAGE_MAX = 200


def _normalize_attachment_failures(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    failures: list[dict[str, Any]] = []
    for item in raw[:_ATTACHMENT_FAILURES_MAX]:
        if not isinstance(item, dict):
            continue
        record = {key: item[key] for key in _ATTACHMENT_FAILURE_KEYS if key in item}
        if not str(record.get("name") or "").strip() and not str(record.get("stage") or "").strip():
            continue
        if "platform_message" in record:
            record["platform_message"] = str(record.get("platform_message") or "")[
                :_ATTACHMENT_FAILURE_MESSAGE_MAX
            ]
        failures.append(record)
    return failures

# 图片附件沿用 web CEO 上传上限；文档/文件类附件单独放宽（渠道传文档常超
# 5MiB）。超限一律 413 ``attachment_too_large``，桥侧按同值预过滤避免必然失败。
EXTERNAL_FILE_UPLOAD_MAX_BYTES = 20 * 1024 * 1024
# 语音条转码的内存预算：60 秒 24 kHz WAV 是 2.9 MB，留到 8 MB 已经宽到不会把
# 真正的语音条挡在压缩外面，也不会因为有人从渠道丢大文件而吸进整块内存。
VOICE_CLIP_TRANSCODE_MAX_BYTES = 8 * 1024 * 1024


def _registry():
    return get_external_session_registry()


_QQ_BRIDGE_PREFIX = "qq-official-"


def _owner_openids_for_bridge(bridge_id: str) -> tuple[str, ...]:
    """操作员 openid 名单：所有 QQ 账号 ``ownerOpenids`` 的并集。

    白名单是**服务端配置**（``qqBot.accounts.<appId>.ownerOpenids``），不接受桥在
    请求体里自报——否则被攻破的桥一句 ``trust: owner`` 就把自己提成操作员。
    openid 按 AppID 隔离（同一人在不同号上是不同的串），但「谁是操作员」不随接入的
    是哪个号而变，所以这里不分账号：任一账号里填过的 openid，在任何 QQ 桥上都命中。
    """
    bridge = str(bridge_id or "").strip()
    if not bridge.startswith(_QQ_BRIDGE_PREFIX):
        return ()
    # 桥必须对应一个已配置的号：只剩 token 的孤儿桥不给名单（fail-closed）。
    try:
        config = get_runtime_config(force=False)[0]
    except Exception:  # noqa: BLE001 - 读不到配置就按没有白名单处理
        return ()
    accounts = dict(getattr(getattr(config, "qq_bot", None), "accounts", None) or {})
    if bridge[len(_QQ_BRIDGE_PREFIX) :] not in accounts:
        return ()
    return tuple(sorted(qq_owner_openids()))


def _validated_chat_type(value: Any) -> str:
    """白名单校验：空 = 未声明（走回退），非法值一律 400 而不是静默按 dm 处理。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = normalize_chat_type(raw)
    if not normalized:
        raise HTTPException(status_code=400, detail="chat_type_invalid")
    return normalized


def _effective_chat_type(*, claimed: str, entry: ExternalSessionEntry) -> str:
    """请求自报的形状与既有会话形状合并：**取更严的一档**。

    ``external_key`` 前缀（``qq:group:`` / ``qq:guild:``）是既有契约里的形状声明，
    与请求体不一致时按多人会话处理并留 WARNING——一条谎报 dm 的消息不能把群会话
    洗成私聊。
    """
    known = normalize_chat_type(getattr(entry, "chat_type", "")) or chat_type_from_external_key(
        getattr(entry, "external_key", "")
    )
    if claimed and known and claimed != known:
        merged = (
            claimed
            if is_multi_party_chat(chat_type=claimed)
            else known
        )
        if is_multi_party_chat(chat_type=merged):
            logger.warning(
                "external chat_type mismatch for {}: claimed={} key={} — 按更严的一档处理",
                getattr(entry, "session_key", ""),
                claimed,
                known,
            )
        return merged
    if is_multi_party_chat(chat_type=known) or is_multi_party_chat(chat_type=claimed):
        return normalize_chat_type(claimed) if is_multi_party_chat(chat_type=claimed) else known
    return claimed or known


def _own_entry(session_id: str, principal: ExternalApiPrincipal) -> ExternalSessionEntry:
    """本桥名下会话的唯一判据：跨桥寻址绝不走这里。

    注册表生命周期（改名、清空）与 outbox 销账都归"会话属于签发它的那一号"，
    放宽就等于让一个桥替另一个桥的账本做主。
    """
    raw = str(session_id or "").strip()
    entry = _registry().get_by_session_key(raw)
    if entry is None or entry.bridge_id != principal.bridge_id:
        raise HTTPException(status_code=404, detail="session_not_found")
    return entry


def _resolve_entry(session_id: str, principal: ExternalApiPrincipal) -> ExternalSessionEntry:
    """读、发信与中止车道的作用域判据。

    本桥名下 → 注册表条目原样返回。跨桥只在 token 被允许时放行，并且只认真实存在的
    两样东西：别桥的注册会话，或有转录文件的会话键（`web:ceo-*` / `qq:*` 这些不进
    注册表，没有转录就 404——否则拿到 token 的人能凭空造键）。

    合成条目的 `bridge_id` 记**发起方**：回合归属、幂等位与事件流都按发起方算，
    跨桥发进去的那一轮回复因此回到正在等的这个调用方，而不是目标会话原来的桥。
    """
    raw = str(session_id or "").strip()
    entry = _registry().get_by_session_key(raw)
    if entry is not None and entry.bridge_id == principal.bridge_id:
        return entry
    if not principal.cross_session:
        raise HTTPException(status_code=404, detail="session_not_found")
    if entry is None and not _session_manager().has_transcript(raw):
        raise HTTPException(status_code=404, detail="session_not_found")
    return ExternalSessionEntry(
        bridge_id=principal.bridge_id,
        external_key=str(entry.external_key or raw) if entry is not None else raw,
        session_key=raw,
        created_at=str(entry.created_at or "") if entry is not None else "",
        title=str(entry.title or "") if entry is not None else "",
    )


def _session_manager() -> SessionManager:
    return SessionManager(workspace_path())


def _publish_ceo_catalog_best_effort() -> None:
    """Push a fresh CEO session catalog to connected web clients.

    External sessions appear in the web CEO sidebar through the catalog; the
    browser only refetches the REST list on page load, so live visibility of a
    newly created/renamed ``ext:*`` session depends on this global publish.
    Best-effort by contract: it must never fail the /api/v1 call. It only
    observes the running web runtime (``peek_global_agent`` never constructs
    the agent); with no live runtime there is no browser to notify.

    渠道会话从隐身里回来时也靠这颗推送（见 external_turns.submit）。
    """
    try:
        agent = peek_global_agent()
        if agent is None:
            return
        session_manager = getattr(agent, "sessions", None)
        if session_manager is None:
            return
        runtime_manager = get_runtime_manager(agent)
        state_store = WebCeoStateStore(workspace_path())
        _publish_ceo_sessions_snapshot(agent, session_manager, runtime_manager, state_store)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.debug("external api ceo catalog publish skipped: {}", exc)


# -- sessions ---------------------------------------------------------------


@router.post("/sessions")
async def create_external_session(
    payload: dict[str, Any] = Body(...),
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    external_key = str(payload.get("external_key") or "").strip()
    if not external_key:
        raise HTTPException(status_code=400, detail="external_key_required")
    title = str(payload.get("title") or "").strip() or None
    chat_type = _validated_chat_type(payload.get("chat_type"))
    registry = _registry()
    # 隐身的行只有"再有人来注册"或"再有人发消息"才会回来。命中既有映射时 resolve_or_create
    # 顺手清掉 hidden，所以这里也得把目录推出去，否则网页要刷新页面才看得见。
    previous = registry.get_by_session_key(
        registry.get_session_key(bridge_id=principal.bridge_id, external_key=external_key)
    )
    was_hidden = bool(previous and previous.hidden)
    entry, created = registry.resolve_or_create(
        bridge_id=principal.bridge_id,
        external_key=external_key,
        title=title,
        chat_type=chat_type or None,
    )
    if created or was_hidden:
        _publish_ceo_catalog_best_effort()
    return {
        "ok": True,
        "session_id": entry.session_key,
        "external_key": entry.external_key,
        "title": entry.title,
        "chat_type": entry.chat_type,
        "created_at": entry.created_at,
        "created": created,
    }


async def _list_all_sessions(principal: ExternalApiPrincipal) -> dict[str, Any]:
    """全量会话目录（跨桥作用域）：复用网页那份装配，不另开一条枚举道。

    store 必须取**运行时那一份** `agent.sessions`，不能现场 new：目录构造里逐键
    `get_or_create` 的异常是被 `except: continue` 吞掉的，第二个实例在同一批转录上
    整表跳过时，接口只会安静地返回空 items（实盘第一版就栽在这，200 + 0 条，而
    `/api/ceo/sessions` 同刻给 175 条）。运行档位也顺手用同一个 runtime_manager 读，
    与 `/ws/ceo` 的判据同形。

    目录构建要遍历全部转录（渠道会话单份可达数十 MB），统一在 CEO 专用线程里跑并吃
    3 秒 TTL 缓存，事件循环只 await。条目形状以 `build_session_summary` 为唯一来源，
    这里只补两样运行侧事实——`transcript_path`（运行时算出的安全名，含键长超限的 digest
    逃生）与 `has_transcript`（该转录文件在不在），调用方不要自己把 session key 拼成
    文件名，也不该拿到路径后自己去 stat 一遍。
    """
    if not principal.cross_session:
        raise HTTPException(status_code=403, detail="cross_session_scope_required")
    agent = peek_global_agent()
    manager = getattr(agent, "sessions", None) if agent is not None else None
    if manager is None:
        manager = _session_manager()
    runtime_manager = get_runtime_manager(agent) if agent is not None else None

    def _session_state(session_key: str) -> Any:
        if runtime_manager is None:
            return None
        getter = getattr(runtime_manager, "get", None)
        if not callable(getter):
            return None
        session = getter(str(session_key or "").strip())
        return getattr(session, "state", None) if session is not None else None

    catalog = await build_ceo_session_catalog_async(
        manager,
        active_session_id=f"ext-api:{principal.bridge_id}",
        is_running_resolver=lambda key: bool(getattr(_session_state(key), "is_running", False))
        or str(getattr(_session_state(key), "status", "") or "").strip().lower() == "running",
        status_resolver=lambda key: str(getattr(_session_state(key), "status", "") or "").strip().lower(),
    )
    # 目录把渠道会话放在 channel_groups 里，"全部会话"必须把两侧合起来：
    # 只回 items 就等于把渠道端那些号藏起来。
    raw_entries = list(catalog.get("items") or [])
    for group in list(catalog.get("channel_groups") or []):
        if isinstance(group, dict):
            raw_entries.extend(list(group.get("items") or []))
    items: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for raw_item in raw_entries:
        if not isinstance(raw_item, dict):
            continue
        session_id = str(raw_item.get("session_id") or "").strip()
        if not session_id or session_id in seen_keys:
            continue
        seen_keys.add(session_id)
        item = dict(raw_item)
        item["transcript_path"] = str(manager.get_path(session_id))
        item["has_transcript"] = manager.has_transcript(session_id)
        items.append(item)
    return {
        "ok": True,
        "bridge_id": principal.bridge_id,
        "scope": "all",
        "items": items,
    }


@router.get("/sessions")
async def list_external_sessions(
    external_key: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    registry = _registry()
    if str(scope or "").strip().lower() == "all":
        return await _list_all_sessions(principal)
    if external_key:
        key = registry.get_session_key(bridge_id=principal.bridge_id, external_key=str(external_key).strip())
        entries = [registry.get_by_session_key(key)] if key else []
    else:
        entries = registry.list_bridge_sessions(principal.bridge_id)
    manager = _session_manager()
    agent = peek_global_agent()
    runtime_manager = get_runtime_manager(agent) if agent is not None else None

    def _running(session_key: str) -> bool:
        if runtime_manager is None:
            return False
        getter = getattr(runtime_manager, "get", None)
        if not callable(getter):
            return False
        state = getattr(getter(str(session_key or "").strip()) or object(), "state", None)
        return bool(getattr(state, "is_running", False)) or (
            str(getattr(state, "status", "") or "").strip().lower() == "running"
        )

    items: list[dict[str, Any]] = []
    for entry in entries:
        if entry is None:
            continue
        # 活动字段（预览/条数/updated_at/运行态）与 `scope=all` 用同一颗构造器：
        # 早先 own 只回 registry 的五字段，于是同一条会话在两个 scope 下读数不同形，
        # 调用方按描述里的"带预览与转录路径"读 own 就会读到空。
        row = build_channel_ceo_session_item(
            manager,
            entry.session_key,
            active_session_id=f"ext-api:{principal.bridge_id}",
            is_running=_running(entry.session_key),
        )
        item = dict(row or {})
        item.update(
            {
                "session_id": entry.session_key,
                "external_key": entry.external_key,
                "title": str(item.get("title") or entry.title or ""),
                "created_at": str(item.get("created_at") or "") or entry.created_at,
                "can_message": True,
                # 注册表条目与转录是两个轴：桥一注册就有线上条目（还没有转录文件）。
                # 这个读数说的是**文件在不在**——网页侧「删除渠道会话」会删掉它（空壳），
                # 桥侧 `DELETE /sessions/{id}` 的清除语义是清空并保存（文件留着）。
                "transcript_path": str(manager.get_path(entry.session_key)),
                "has_transcript": manager.has_transcript(entry.session_key),
            }
        )
        items.append(item)
    return {
        "ok": True,
        "bridge_id": principal.bridge_id,
        "items": items,
    }


@router.patch("/sessions/{session_id}")
async def rename_external_session(
    session_id: str,
    payload: dict[str, Any] = Body(...),
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    entry = _own_entry(session_id, principal)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title_required")
    _registry().update_title(entry.session_key, title)
    _publish_ceo_catalog_best_effort()
    return {"ok": True, "session_id": entry.session_key, "title": title}


@router.delete("/sessions/{session_id}")
async def clear_external_session(
    session_id: str,
    purge: str | None = Query(default=None),
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    """Clear semantics (matches the Channel Session Clear Contract): the
    registry entry stays visible, the transcript and side artifacts reset so
    the next turn starts from an empty context.

    ``?purge=true`` 换成删除语义：连注册表条目一起忘掉，这个 external_key 下次
    出现就是一条新会话。MCP 的 `negi_delete_conversation` 走这条——清完还留着条目，
    调用方列自己的会话时那条永远还在，删了等于没删。"""
    entry = _own_entry(session_id, principal)
    session_key = entry.session_key
    manager = _session_manager()
    if str(purge or "").strip().lower() in {"1", "true", "yes"}:
        deleted = manager.delete(session_key)
        _registry().forget(session_key)
        clear_web_ceo_session_artifacts(session_id=session_key)
        return {
            "ok": True,
            "deleted": True,
            "registry_forgotten": True,
            "transcript_removed": bool(deleted),
            "session_id": session_key,
        }
    session = manager.get_or_create(session_key)
    session.clear()
    manager.save(session)
    manager.invalidate(session_key)
    clear_web_ceo_session_artifacts(session_id=session_key)
    return {"ok": True, "cleared": True, "session_id": session_key}


LATEST_MODEL_TEXT_MAX_CHARS = 300
RECENT_TOOL_NAMES_MAX = 5
# 实测每任务节点数 p50=2、p90=3（1227 那份是基准副本），6 项已盖住常规树的两层；
# 超出时 `node_count` 报真实条数，截断可见。
TASK_NODE_PROJECTION_MAX = 6
WAKE_TASKS_MAX = 20
WAKE_REPLIES_MAX = 10


def summarize_external_session_activity(events: list[dict[str, Any]]) -> dict[str, Any]:
    """从事件缓冲区尾巴算"最新文本输出 + 最近用过哪几颗工具、各几次"。

    文本必须先过与终稿同一道出站消毒再截断：`reply.delta` 这条道**不做**消毒
    （`external_events.py` 只在 `message_end` 上 sanitize），把 hub 原文直接递出去
    等于把未清洗的思考段交给外部 token。截断量如实报：`latest_model_text_total_chars`
    是消毒后、截断前的字符数。

    工具项带 `calls`，因为"最近用过哪几种"回答不了"是在磨还是要收了"：实测一条 87
    分钟的审计会话把 114 轮花在同样的几颗名字上，去重名单看着永远一样。计数覆盖
    **整个会话事件缓冲**（hub 是 512 条环形），所以这里不再提前 break——全扫的代价是
    微秒级，而提前 break 报出来的次数就是假数。
    """
    latest_text = ""
    latest_total_chars = 0
    tool_counts: dict[str, int] = {}
    tool_order: list[str] = []
    for event in reversed(list(events or [])):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        if not latest_text and event_type in {"reply.delta", "reply.final"}:
            cleaned = sanitize_channel_outbound_text(str(event.get("text") or "")).strip()
            if cleaned:
                latest_text = cleaned[:LATEST_MODEL_TEXT_MAX_CHARS]
                latest_total_chars = len(cleaned)
        if event_type == "progress" and str(event.get("kind") or "") == "tool":
            name = str(event.get("tool_name") or "").strip()
            if not name:
                continue
            if name not in tool_counts:
                tool_counts[name] = 0
                tool_order.append(name)
            tool_counts[name] += 1
    return {
        "latest_model_text": latest_text,
        "latest_model_text_total_chars": latest_total_chars,
        "recent_tool_calls": [
            {"tool_name": name, "calls": tool_counts[name]} for name in tool_order[:RECENT_TOOL_NAMES_MAX]
        ],
    }


def _session_task_activity(agent: Any, session_key: str) -> dict[str, Any] | None:
    """本会话仍未完成的任务，以及它们最近一跳的时间。

    为什么必须有：派发回合几十秒就收尾了，节点还要跑几十分钟，而这段时间会话侧
    **本来就没有新事件** ⇒ 只看事件时间的调用方会把"沉默 100 秒"读成卡死（实盘真的
    就这么误判了一次）。任务侧的跳次时间才是"活不活"的正证。
    读不到任务服务时返回 None——"不知道"与"没有在跑的任务"是两回事，不折成 0。
    """
    service = getattr(agent, "main_task_service", None) if agent is not None else None
    if service is None:
        return None
    lister = getattr(service, "list_unfinished_tasks_for_session", None)
    if not callable(lister):
        return None
    try:
        tasks = list(lister(str(session_key or "").strip()) or [])
    except Exception:
        return None
    store = getattr(service, "store", None)
    entries: list[dict[str, Any]] = []
    for task in tasks[:8]:
        task_id = str(getattr(task, "task_id", "") or "").strip()
        if not task_id:
            continue
        hop_at = ""
        model_calls = 0
        if store is not None:
            try:
                rows = list(store.list_task_model_calls(task_id, limit=1) or [])
                hop_at = str((rows[0] or {}).get("created_at") or "") if rows else ""
            except Exception:
                hop_at = ""
            try:
                model_calls = int(store.count_task_model_calls(task_id) or 0)
            except Exception:
                model_calls = 0
        entry: dict[str, Any] = {
            "task_id": task_id,
            "status": str(getattr(task, "status", "") or "").strip(),
            "model_calls": model_calls,
            "last_hop_at": hop_at,
        }
        # 任务级 `in_progress` 说不清"卡在哪一格"：一个任务是执行节点 + 验收节点的树
        # （实测 p50=2、p90=3 个节点）。遗留样本 task:37a79544ee37 至今 in_progress，
        # 只有节点级时间能看出执行节点 00:18:41 停、验收节点 00:13:48 也停。
        nodes_reader = getattr(service, "list_task_node_activity", None)
        if callable(nodes_reader):
            try:
                entry["nodes"] = list(nodes_reader(task_id, limit=TASK_NODE_PROJECTION_MAX) or [])
            except Exception:
                entry["nodes"] = []
            counter = getattr(service, "count_task_nodes", None)
            entry["node_count"] = int(counter(task_id) or 0) if callable(counter) else len(entry["nodes"])
        entries.append(entry)
    return {"tasks_running": len(entries), "tasks": entries}


def _age_from_iso(value: str, *, now: datetime | None = None) -> int | None:
    """ISO 串到"距现在多少秒"。解不开就回 None，不猜。

    两种形状会在同一颗请求里相遇：会话侧的事件 `ts` 与回合 `started_at` 是本机 naive
    串，任务跳次 `created_at` 带 `+08:00`（`main/monitoring/log_service.py` 的
    `_now_iso`）。拿 naive 的去减 aware 的会 TypeError 打死整条 `/state`——实盘抓到
    过两次：会话有未完成任务且那任务已经跳过一次时，`negi_session_status` 整包失败。
    所以减数先按被减数的形状对齐，而不是假设调用方只喂一种。
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    reference = datetime.now() if now is None else now
    reference = reference.astimezone() if parsed.tzinfo is not None else reference.replace(tzinfo=None)
    return max(0, int((reference - parsed).total_seconds()))


@router.get("/sessions/{session_id}/state")
async def get_external_session_state(
    session_id: str,
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    entry = _resolve_entry(session_id, principal)
    service = get_external_turn_service()
    session = service._runtime_bridge.get_existing_session(entry.session_key)
    running = service._runtime_bridge.session_is_running(session)
    queued = 0
    last_error = None
    if session is not None:
        state = getattr(session, "state", None)
        queued = len(list(getattr(state, "queued_follow_up_messages", None) or []))
        error = getattr(state, "last_error", None)
        if error is not None:
            last_error = {
                "code": str(getattr(error, "code", "") or ""),
                "message": str(getattr(error, "message", "") or ""),
            }
    inflight_turn_id = service.inflight_turn_id_for(entry.session_key)
    turn_record = service.get_turn(inflight_turn_id)
    hub = get_session_event_hub(entry.session_key)
    tail_event = hub.tail_event() if hub is not None else None
    now = datetime.now()

    last_activity_at = str((tail_event or {}).get("ts") or "")
    activity = summarize_external_session_activity(list(hub.replay(0)) if hub is not None else [])
    # 会话自己就知道这一轮是怎么停的（`RuntimeAgentSession._last_stop_reason`，正常交/
    # 用户暂停/没交正文就收），以前只写进续跑状态、没往外给。调用方看到
    # 调用方看到 `turn.completed` 时，分辨"交付完了"与"没交正文就收"就靠它。
    stop_reason = str(getattr(session, "_last_stop_reason", "") or "")
    # 账本读是整份文件读（实测 31 条 pending ≈ 0.8ms）。它只在两个地方变小：消费方 ack
    # 落墓碑 + 每小时 compact，所以轮询这颗端点不会把它养成大读口。真养大了就该换成
    # 按会话索引的读法，而不是在这里加缓存。
    unconsumed = sum(
        1
        for record in load_pending_outbound()
        if str(record.get("session_key") or "") == entry.session_key and is_pull_consumable(record)
    )
    task_activity = _session_task_activity(peek_global_agent(), entry.session_key)
    task_hop_ceiling = _task_hop_ceiling(task_activity) if task_activity is not None else ""
    started_at = str(getattr(turn_record, "started_at", "") or "")
    activity_text = str((tail_event or {}).get("text") or "").strip()
    return {
        "ok": True,
        "session_id": entry.session_key,
        "external_key": entry.external_key,
        "running": running,
        "queued_follow_ups": queued,
        "inflight_turn_id": inflight_turn_id,
        # 时间与活动读数：光给 `last_seq` 一个计数器，调用方没法区分"在跑一个长活"
        # 与"已经卡死"（实测有 agent 为此累计阻塞近一小时）。事件本来就带 `ts`，
        # 回合本来就带 `started_at`，这里只把它们投影出来，不新增状态。
        "started_at": started_at,
        "elapsed_seconds": _age_from_iso(started_at, now=now),
        "last_activity_at": last_activity_at,
        "silence_seconds": _age_from_iso(last_activity_at, now=now),
        "last_event_seq": int((tail_event or {}).get("seq") or 0),
        "last_activity_kind": str((tail_event or {}).get("type") or ""),
        # hub 的 `reply.delta` 这条道不过出站消毒（sanitize 只长在 `message_end` 上），
        # 未清洗的思考段不能原样递出去——与 `latest_model_text` 同一条纪律。
        "last_activity_text": sanitize_channel_outbound_text(activity_text)[:160],
        # 活动文本与最新输出同一条纪律：hub 里的事件不过消毒，往外递之前先 sanitize。
        "latest_model_text": activity["latest_model_text"],
        "latest_model_text_total_chars": activity["latest_model_text_total_chars"],
        "recent_tool_calls": activity["recent_tool_calls"],
        # 结束方式与"这轮留没留下没被取走的正文"：回合已收而正文仍未被取走时，
        # 这两行是调用方唯一能分辨"交付完了"还是"中途早退"的读数。
        "stop_reason": stop_reason,
        "unconsumed_replies": unconsumed,
        "turn_left_unconsumed_reply": bool(unconsumed),
        "last_error": last_error,
        **(
            {
                "tasks_running": task_activity["tasks_running"],
                "tasks": task_activity["tasks"],
                "task_activity_at": task_hop_ceiling,
                "task_silence_seconds": _age_from_iso(task_hop_ceiling, now=now) if task_hop_ceiling else None,
            }
            if task_activity is not None
            else {}
        ),
    }


def _task_hop_ceiling(activity: dict[str, Any]) -> str:
    return max([str(item.get("last_hop_at") or "") for item in list(activity.get("tasks") or [])] or [""])


def summarize_external_session_wake(
    *,
    cursor: str,
    task_rows: list[dict[str, Any]],
    pending_records: list[dict[str, Any]],
    tasks_limit: int = WAKE_TASKS_MAX,
    replies_limit: int = WAKE_REPLIES_MAX,
) -> dict[str, Any]:
    """把两份既有 durable 事实合成一条唤醒读数：终态任务行 + 未销账的回复记录。

    只算不读，是为了让"哪条算终态、哪条算未消费、截断怎么写"这一套判据可测——
    真读口在 `tasks` 表与 outbox 账本里。可取集合由 `external_outbox.is_pull_consumable`
    单点决定：`reply.final`（回合的答案）和 `outbound.created`（发给用户的正文）都算，因为
    心跳来源的回合从不产 `reply.final`，它的正文只以主动推送记录进账——只认前一类会让这段
    正文躺在账本里对拉取式消费方永远不可读。每条回复自带 `event` 字段标出来路。

    水位线只认一种形状：本机墙钟到秒。两本时间源的串不一样——账本 `ts` 是
    `datetime.now().isoformat()`（naive、带微秒），`tasks.updated_at` 带 `+08:00` 且只到秒
    （活体读到的就是这两种）。混着比字符串时 `+` 排在 `.` 前面，同秒内 aware 串会被判成
    "更早"，于是严格大于的游标能把边界那一秒的终态**永久漏掉**。所以先归一化再比，
    边界取包含式（`>=`）：同一秒宁可重复报一次，唤醒是幂等信息。
    """
    tasks = [
        {
            "task_id": str(item.get("task_id") or ""),
            "status": str(item.get("status") or ""),
            "finished_at": str(item.get("updated_at") or ""),
        }
        for item in list(task_rows or [])[:tasks_limit]
        if str(item.get("task_id") or "")
    ]
    owned = [record for record in list(pending_records or []) if is_pull_consumable(record)]
    if cursor:
        owned = [record for record in owned if _wake_wall_second(str(record.get("ts") or "")) >= cursor]
    replies = [
        {
            "outbox_id": str(record.get("id") or ""),
            "ts": str(record.get("ts") or ""),
            "event": str(record.get("event") or ""),
            "text": str(record.get("text") or ""),
        }
        for record in owned[:replies_limit]
    ]
    stamps = [_wake_wall_second(item["finished_at"]) for item in tasks] + [
        _wake_wall_second(item["ts"]) for item in replies
    ]
    return {
        "tasks": tasks,
        # 终态按时间倒序取前 `tasks_limit` 条，撞上上限时最旧的那些没进来——
        # 截断必须可见，调用方才知道"这不是全部"。
        "tasks_truncated": len(list(task_rows or [])) > len(tasks),
        "replies": replies,
        "replies_pending": len(owned),
        "next_since": max([stamp for stamp in stamps if stamp] or [str(cursor or "")]),
    }


def _wake_wall_second(value: str) -> str:
    """ISO 串 → 本机墙钟到秒的裸串；解不开回空串（不当 0 用）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0).isoformat()


@router.get("/sessions/{session_id}/wake")
async def get_external_session_wake(
    session_id: str,
    since: str = Query(""),
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    """你不在的时候发生了什么：本会话新落终态的任务 + 还没被消费掉的回复。

    存在的理由：事件 hub 是进程内存里的 512 环，`turn.completed`/`turn.failed` 不进
    持久账本，而回复的取法只听活 SSE——调用方（stdio 网关、断线的桥）断开半小时再
    回来，没有任何一口能回答"我那两个任务结清了没"。这里不新增写侧记录：终态行落在
    `tasks` 表（落库后不再变），未消费回复落在持久 outbox 账本（销账才消失），两边
    都是既有的 durable 事实，所以唤醒不依赖"当时有没有人在听"。

    `since` 是调用方自己的游标（上一次返回的 `next_since`）；空串＝没有历史，回最近
    `WAKE_TASKS_MAX` 条终态，首次调用因此也能 bootstrap。

    机器**不**往出站账本写"任务已结束"这种文本：pump 会把账本记录当渠道消息投给
    用户（`g3ku/qq_official/bridge.py`），替模型决定推送是禁区。
    """
    entry = _resolve_entry(session_id, principal)
    # 调用方可能把两本时间源的原始串当游标递回来，先归一化成同一种形状再当下界；
    # 归一化失败就当"没有历史"（回最近一批），宁可重复不许漏。
    raw_since = str(since or "").strip()
    cursor = _wake_wall_second(raw_since) if raw_since else ""
    agent = peek_global_agent()
    task_service = getattr(agent, "main_task_service", None) if agent is not None else None
    lister = getattr(task_service, "list_finished_tasks_for_session_since", None)
    task_rows: list[dict[str, Any]] = []
    if callable(lister):
        try:
            # 多取一条只为了知道"还有没有"——截断信号不能被 SQL 的 LIMIT 吃掉。
            task_rows = list(lister(entry.session_key, cursor, limit=WAKE_TASKS_MAX + 1) or [])
        except Exception:
            task_rows = []
    pending = [
        record
        for record in load_pending_outbound()
        if str(record.get("session_key") or "") == entry.session_key
    ]
    return {
        "ok": True,
        "session_id": entry.session_key,
        "since": cursor,
        **summarize_external_session_wake(cursor=cursor, task_rows=task_rows, pending_records=pending),
    }


@router.get("/sessions/{session_id}/turns/{turn_id}")
async def get_external_session_turn(
    session_id: str,
    turn_id: str,
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    """按回合 id 重取转录里那一行的可见原文。

    存在的理由：调用方上下文被压缩后，只剩自己那份摘要；回合原文在转录里是 durable
    的（assistant 行带顶层 `turn_id`，`session_agent.py` 落盘时写的），而内存里的
    `TurnRecord` 只留 256 条终态记录且会淘汰，不能当历史读口。
    """
    entry = _resolve_entry(session_id, principal)
    manager = _session_manager()
    wanted = str(turn_id or "").strip()
    # 只读语义靠"文件在不在"判：manager 没有"不创建就取"的口，而在转录存在时
    # `get_or_create` 走的是加载分支，不会造出假 created_at 的空会话。
    if not manager.has_transcript(entry.session_key):
        raise HTTPException(status_code=404, detail="session_not_found")
    session = manager.get_or_create(entry.session_key)
    for message in list(getattr(session, "messages", []) or []):
        if str(message.get("role") or "") != "assistant":
            continue
        if str(message.get("turn_id") or "").strip() != wanted:
            continue
        metadata = dict(message.get("metadata") or {}) if isinstance(message.get("metadata"), dict) else {}
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        return {
            "ok": True,
            "session_id": entry.session_key,
            "turn_id": wanted,
            "text": str(message.get("content") or ""),
            "created_at": str(message.get("timestamp") or ""),
            "usage": dict(usage or {}),
            "task_ids": [str(item) for item in list(metadata.get("task_ids") or [])],
            "source": str(metadata.get("source") or ""),
            "silent_reply": bool(metadata.get("silent_reply")),
        }
    raise HTTPException(status_code=404, detail="turn_not_found")


# -- messages / turns -------------------------------------------------------


def _attachment_descriptor(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "").strip().lower()
    mime_type = str(item.get("mime_type") or "").strip().lower()
    path = str(item.get("path") or "").strip()
    url = str(item.get("url") or "").strip()
    if not kind:
        if mime_type.startswith("image/"):
            kind = "image"
        elif mime_type.startswith("audio/"):
            kind = "audio"
        elif mime_type.startswith("video/"):
            kind = "video"
        else:
            kind = "file"
    if not mime_type and path:
        mime_type = str(mimetypes.guess_type(path)[0] or "").strip()
    descriptor: dict[str, Any] = {
        "kind": kind,
        "name": str(item.get("name") or "").strip()
        or (Path(path).name if path else (url.split("/")[-1] if url else "attachment")),
        "mime_type": mime_type,
    }
    if path:
        descriptor["path"] = path
    if url:
        descriptor["url"] = url
    return descriptor


def _store_base64_attachment(
    session_key: str, item: dict[str, Any], *, max_bytes: int
) -> tuple[str, int]:
    raw = str(item.get("data_base64") or "")
    # 先按 base64 长度廉价估算拒绝明显超限载荷，避免白白解码大对象。
    if len(raw) > (max_bytes // 3 + 1) * 4 + 4:
        raise HTTPException(status_code=413, detail="attachment_too_large")
    data = base64.b64decode(raw)
    size = len(data)
    if size > max_bytes:
        raise HTTPException(status_code=413, detail="attachment_too_large")
    name = safe_filename(str(item.get("name") or "attachment")) or "attachment"
    target_dir = ensure_dir(workspace_path() / EXTERNAL_UPLOAD_ROOT / safe_filename(session_key))
    target = target_dir / f"{uuid.uuid4().hex[:8]}-{name}"
    target.write_bytes(data)
    return str(target), size


async def _compressed_voice_clip(item: dict[str, Any]) -> dict[str, Any]:
    """语音条落盘前先压成 MP3，压不动就原样交回。

    渠道语音到这里的字节是引擎解码后的 PCM WAV（QQ 的原始 silk 浏览器播不了），
    32 KB/秒 地留在盘上没有意义：实测 22.85 秒样本 731 KB → 92 KB，耗时 0.18 秒。
    没有 ffmpeg、不是 WAV、或大得不像语音条时一律退回原形状——压缩是优化，
    不是这条车道的前提。
    """
    from g3ku.stt import audio as stt_audio

    if str(item.get("kind") or "").strip().lower() != "audio":
        return item
    try:
        raw = base64.b64decode(str(item.get("data_base64") or ""), validate=False)
    except (binascii.Error, ValueError):
        return item
    if not raw or len(raw) > VOICE_CLIP_TRANSCODE_MAX_BYTES or not stt_audio.is_wav(raw):
        return item
    encoded = await asyncio.to_thread(stt_audio.encode_to_mp3, raw)
    if not encoded:
        return item
    stem = Path(str(item.get("name") or "voice")).stem or "voice"
    return {
        **item,
        "name": f"{stem}.mp3",
        "mime_type": "audio/mpeg",
        "data_base64": base64.b64encode(encoded).decode("ascii"),
    }


def _image_url_from_attachment(attachment: dict[str, Any]) -> str | None:
    mime_type = str(attachment.get("mime_type") or "").strip() or "image/png"
    path = str(attachment.get("path") or "").strip()
    if path:
        candidate = Path(path)
        if candidate.exists() and candidate.is_file():
            encoded = base64.b64encode(candidate.read_bytes()).decode("ascii")
            return f"data:{mime_type};base64,{encoded}"
    return str(attachment.get("url") or "").strip() or None


def _build_external_user_message(
    *,
    text: str,
    attachments: list[dict[str, Any]],
    sender: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> str | UserInputMessage:
    """Generalized from the legacy China transport message builder: attachment
    note + provider-visible image blocks for the current turn only."""
    message_metadata: dict[str, Any] = dict(metadata or {})
    if sender:
        message_metadata["external_sender"] = sender
    if not attachments and not message_metadata:
        return text
    if attachments:
        message_metadata["external_attachments"] = attachments
    if not attachments:
        return UserInputMessage(content=text, metadata=message_metadata)

    note = channel_attachment_note(attachments)

    text_value = str(text or "")
    merged_text = f"{text_value}\n\n{note}" if (note and text_value) else (note or text_value)
    content: list[dict[str, Any]] = []
    if merged_text:
        content.append({"type": "text", "text": merged_text})
    for attachment in attachments:
        if str(attachment.get("kind") or "") != "image":
            continue
        image_url = _image_url_from_attachment(attachment)
        if not image_url:
            continue
        content.append({"type": "image_url", "image_url": {"url": image_url}})
    attachment_refs = [
        str(item.get("path") or item.get("url") or "").strip()
        for item in attachments
        if str(item.get("kind") or "") != "audio"
        and str(item.get("path") or item.get("url") or "").strip()
    ]
    return UserInputMessage(
        content=content or [{"type": "text", "text": note or text_value}],
        attachments=attachment_refs,
        metadata=message_metadata,
    )


async def _maybe_handle_qq_master_binding(
    *,
    bridge_id: str,
    effective_chat_type: str,
    external_key: str,
    session_key: str,
    sender_id: str,
    text: str,
) -> dict[str, Any] | None:
    """Consume a one-time Master binding code before it becomes a turn.

    Only called while the account has an active code; the registry lookup is a
    dict hit, so steady state pays nothing. Group texts that match are consumed
    too: ``member_openid`` cannot bind the c2c identity, and leaving a leaked
    code active would let a group member use it in private chat.
    """
    from g3ku.runtime import qq_master_binding as binding

    app_id = binding.app_id_for_bridge(bridge_id)
    if not app_id or not str(sender_id or "").strip():
        return None
    registry = binding.get_qq_master_binding_registry()
    if not registry.has_active(app_id):
        return None
    if not registry.match(app_id, text):
        return None
    if is_multi_party_chat(chat_type=effective_chat_type, external_key=external_key):
        registry.consume(app_id)
        return {
            "ok": True,
            "session_id": session_key,
            "status": "queued",
            "receipt": "绑定码只接受私聊发送；这串已作废，请重新生成后在私聊里发给我。",
            "binding": True,
        }
    try:
        result = await binding.complete_binding(
            app_id, str(sender_id or ""), session_key=session_key
        )
    except Exception as exc:  # noqa: BLE001 - 给用户一句可重试的回执，失败不吞日志
        registry.consume(app_id)
        logger.warning("qq master binding failed for app {}: {}", app_id, exc)
        return {
            "ok": True,
            "session_id": session_key,
            "status": "queued",
            "receipt": "绑定失败，请重新生成绑定码后再试。",
            "binding": True,
        }
    _publish_ceo_catalog_best_effort()
    return {
        "ok": True,
        "session_id": session_key,
        "status": "queued",
        "receipt": "已注册为 Master。当前私聊现在可写本地记忆；群聊仍按第三方处理。",
        "binding": True,
        "registered": bool(result.get("registered")),
        "already_registered": bool(result.get("already_registered")),
    }


@router.post("/sessions/{session_id}/messages")
async def post_external_message(
    session_id: str,
    payload: dict[str, Any] = Body(...),
    principal: ExternalApiPrincipal = Depends(require_external_api),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    entry = _resolve_entry(session_id, principal)
    text = sanitize_channel_outbound_text(str(payload.get("text") or ""))
    raw_attachments = payload.get("attachments")
    if raw_attachments is not None and not isinstance(raw_attachments, list):
        raise HTTPException(status_code=400, detail="attachments_must_be_list")
    if not text and not raw_attachments:
        raise HTTPException(status_code=400, detail="message_required")

    descriptors: list[dict[str, Any]] = []
    for item in list(raw_attachments or []):
        if isinstance(item, dict) and str(item.get("data_base64") or "").strip():
            item = await _compressed_voice_clip(item)
        descriptor = _attachment_descriptor(item)
        if descriptor is None:
            continue
        if isinstance(item, dict) and str(item.get("data_base64") or "").strip():
            max_bytes = (
                WEB_CEO_IMAGE_UPLOAD_MAX_BYTES
                if str(descriptor.get("kind") or "") == "image"
                else EXTERNAL_FILE_UPLOAD_MAX_BYTES
            )
            path, size = _store_base64_attachment(
                entry.session_key, item, max_bytes=max_bytes
            )
            descriptor["path"] = path
            descriptor["size"] = size
        if not descriptor.get("path") and not descriptor.get("url"):
            continue
        descriptors.append(descriptor)

    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else None
    sender_payload = None
    if sender:
        sender_id = str(sender.get("id") or "").strip()
        if len(sender_id) > 128:
            raise HTTPException(status_code=400, detail="sender_id_too_long")
        sender_payload = {
            "id": sender_id,
            "name": str(sender.get("name") or "").strip()[:128],
        }
    extra_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None

    claimed_chat_type = _validated_chat_type(
        payload.get("chat_type") or (extra_metadata or {}).get("chat_type")
    )
    effective_chat_type = _effective_chat_type(claimed=claimed_chat_type, entry=entry)
    binding_response = await _maybe_handle_qq_master_binding(
        bridge_id=principal.bridge_id,
        effective_chat_type=effective_chat_type,
        external_key=entry.external_key,
        session_key=entry.session_key,
        sender_id=(sender_payload or {}).get("id", ""),
        text=text,
    )
    if binding_response is not None:
        return binding_response
    # 信任级在这里定型并落到注册表（粘性、只降不升）：记忆门、控制面拒绝与投影标注
    # 全部读同一个值，不各自再判一次来源。
    turn_trust = note_inbound_session_shape(
        entry.session_key,
        chat_type=effective_chat_type,
        external_key=entry.external_key,
        sender_id=(sender_payload or {}).get("id", ""),
        owner_openids=_owner_openids_for_bridge(principal.bridge_id),
        trusted_senders=principal.trusted_senders,
    )
    channel_metadata = dict(extra_metadata or {})
    if effective_chat_type:
        channel_metadata["chat_type"] = effective_chat_type
    # 服务端派生值，桥不能自报（请求体里的同名字段一律被覆盖）。
    channel_metadata["external_trust"] = turn_trust

    user_message = _build_external_user_message(
        text=text,
        attachments=descriptors,
        sender=sender_payload,
        metadata=channel_metadata,
    )

    try:
        service = get_external_turn_service()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="runtime_unavailable")
    result = await service.submit(
        entry=entry,
        user_message=user_message,
        idempotency_key=idempotency_key,
    )
    return {"ok": True, "session_id": entry.session_key, **result}


@router.post("/qq-official/identity")
async def report_qq_official_identity(
    payload: dict[str, Any] = Body(...),
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    """The in-process QQ adapter reports its own bot id/nickname for prompt notices."""
    bot_id = str(payload.get("bot_id") or "").strip()
    nickname = " ".join(str(payload.get("nickname") or "").split()).strip()
    if not bot_id and not nickname:
        raise HTTPException(status_code=400, detail="identity_required")
    if len(bot_id) > 128 or len(nickname) > 128:
        raise HTTPException(status_code=400, detail="identity_too_long")
    entry = set_qq_bot_identity(principal.bridge_id, bot_id=bot_id, nickname=nickname)
    return {"ok": True, "bridge_id": principal.bridge_id, **entry}


_CHANNEL_DIGEST_PREVIEW_CHAR_LIMIT = 39_000
_CHANNEL_DIGEST_TEXT_MAX_CHARS = 512_000
_CHANNEL_DIGEST_MEMBER_NAME_MAX_CHARS = 64
_CHANNEL_DIGEST_MEMBERS_MAX = 200
# 预览头在完整头之上追加 omitted / truncated 两个键；选行时预留这段，避免
# 选中后又因头部变长把正文顶出窗口。
_CHANNEL_DIGEST_PREVIEW_HEADER_RESERVE = 512
_CHANNEL_DIGEST_ROW_FIELDS = ("n", "t", "s", "text", "files")


def _digest_label(value: Any, limit: int = _CHANNEL_DIGEST_MEMBER_NAME_MAX_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _digest_time_text(value: Any, *, same_date: bool) -> str:
    text = str(value or "").strip()
    if same_date and len(text) >= 19 and text[10] == "T":
        return text[11:19]
    return text


def _digest_file_entries(raw: Any) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for item in list(raw or []):
        if not isinstance(item, dict):
            continue
        name = _digest_label(item.get("name"), 256)
        path = str(item.get("path") or "").strip()
        if not name and not path:
            continue
        entry: dict[str, str] = {}
        if name:
            entry["name"] = name
        if path:
            entry["path"] = path
        entries.append(entry)
    return entries


def _digest_row(item: dict[str, Any], *, sender_no: int, same_date: bool) -> dict[str, Any]:
    row: dict[str, Any] = {"n": int(item.get("n") or 0)}
    time_text = _digest_time_text(item.get("ts"), same_date=same_date)
    if time_text:
        row["t"] = time_text
    row["s"] = int(sender_no)
    text = str(item.get("text") or "")
    if text:
        row["text"] = text
    files = _digest_file_entries(item.get("files"))
    if files:
        row["files"] = files
    return row


def _digest_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _digest_header_payload(
    *,
    window_start: str,
    window_end: str,
    members: dict[str, str],
    omitted: dict[str, int] | None = None,
    truncated: dict[str, int] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "fields": list(_CHANNEL_DIGEST_ROW_FIELDS),
        "window": [window_start, window_end],
    }
    if members:
        payload["members"] = members
    if omitted:
        payload["omitted"] = omitted
    if truncated:
        payload["truncated"] = truncated
    return payload


def _truncate_digest_row(row: dict[str, Any], *, budget: int) -> tuple[str, int] | None:
    """单条消息自身超过窗口时只截它的 text 字段，行仍是合法 JSON。"""
    text = str(row.get("text") or "")
    if not text:
        return None
    original_chars = len(text)
    marker = f"…[truncated: {original_chars} chars total; full message in the digest artifact]"
    low, high = 0, original_chars
    best = ""
    while low <= high:
        middle = (low + high) // 2
        candidate = dict(row)
        candidate["text"] = text[:middle].rstrip() + marker
        serialized = _digest_json(candidate)
        if len(serialized) <= budget:
            best = serialized
            low = middle + 1
        else:
            high = middle - 1
    if not best:
        return None
    return best, original_chars


def _digest_preview_text(
    *,
    rows: list[dict[str, Any]],
    window_start: str,
    window_end: str,
    members: dict[str, str],
    limit: int = _CHANNEL_DIGEST_PREVIEW_CHAR_LIMIT,
) -> tuple[str, dict[str, Any]]:
    """从最新消息往回取整条，预览只装得下尾部；单条超窗才做字符截断。"""
    base_header = _digest_header_payload(
        window_start=window_start,
        window_end=window_end,
        members=members,
    )
    base_header_line = _digest_json(base_header)
    row_lines = [_digest_json(row) for row in rows]
    total = len(base_header_line) + sum(1 + len(line) for line in row_lines)
    if total <= limit:
        body = "\n".join([base_header_line, *row_lines]) if row_lines else base_header_line
        return body, {
            "complete": True,
            "n_start": int(rows[0].get("n") or 0) if rows else 0,
            "n_end": int(rows[-1].get("n") or 0) if rows else 0,
            "omitted": None,
            "truncated": None,
        }

    selection_limit = max(
        0,
        limit - min(_CHANNEL_DIGEST_PREVIEW_HEADER_RESERVE, limit // 10),
    )
    used = len(base_header_line)
    selected_lines: list[str] = []
    selected_rows: list[dict[str, Any]] = []
    truncated: dict[str, int] | None = None
    index = len(rows) - 1
    while index >= 0:
        row = rows[index]
        line = row_lines[index]
        if used + 1 + len(line) <= selection_limit:
            selected_lines.append(line)
            selected_rows.append(row)
            used += 1 + len(line)
            index -= 1
            continue
        if not selected_rows:
            truncated_row = _truncate_digest_row(row, budget=selection_limit - used - 1)
            if truncated_row is not None:
                line, original_chars = truncated_row
                selected_lines.append(line)
                selected_rows.append(row)
                truncated = {"n": int(row.get("n") or 0), "chars": original_chars}
                index -= 1
        break

    omitted_rows = rows[: index + 1]
    omitted = (
        {
            "from": int(omitted_rows[0].get("n") or 0),
            "to": int(omitted_rows[-1].get("n") or 0),
            "count": len(omitted_rows),
        }
        if omitted_rows
        else None
    )
    preview_header = _digest_header_payload(
        window_start=window_start,
        window_end=window_end,
        members=members,
        omitted=omitted,
        truncated=truncated,
    )
    preview_header_line = _digest_json(preview_header)
    selected_lines.reverse()
    selected_rows.reverse()
    body = (
        "\n".join([preview_header_line, *selected_lines])
        if selected_lines
        else preview_header_line
    )
    return body, {
        "complete": not omitted_rows and truncated is None,
        "n_start": int(selected_rows[0].get("n") or 0) if selected_rows else 0,
        "n_end": int(selected_rows[-1].get("n") or 0) if selected_rows else 0,
        "omitted": omitted,
        "truncated": truncated,
    }


@router.post("/sessions/{session_id}/digest")
async def post_external_digest(
    session_id: str,
    payload: dict[str, Any] = Body(...),
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    """Queue a background group-history digest as an internal heartbeat turn.

    The bridge owns the random timer and writes the history file. This endpoint
    only externalizes the model-facing digest text into an artifact and enqueues
    one ``channel_digest`` heartbeat; it never creates a normal user turn.
    """
    entry = _resolve_entry(session_id, principal)
    claimed_chat_type = _validated_chat_type(payload.get("chat_type"))
    effective_chat_type = _effective_chat_type(claimed=claimed_chat_type, entry=entry)
    if effective_chat_type not in {"group", "guild", "guild_dm"}:
        raise HTTPException(status_code=400, detail="digest_requires_group_session")
    raw_messages = payload.get("messages")
    messages = [item for item in list(raw_messages or []) if isinstance(item, dict)]
    text = str(payload.get("artifact_text") or "").strip()
    digest_preview = ""
    digest_preview_meta: dict[str, Any] = {}
    master_n: list[int] = []
    if messages:
        roster = get_member_roster(entry.session_key)
        trusted_masters = {
            str(item or "").strip().lower()
            for item in _owner_openids_for_bridge(principal.bridge_id)
            if str(item or "").strip()
        }
        members = roster.update_many(
            [
                {
                    "id": str(item.get("sender_id") or ""),
                    "name": str(item.get("sender_name") or ""),
                    "role": str(item.get("role") or ""),
                    "master": str(item.get("sender_id") or "").strip().lower() in trusted_masters,
                }
                for item in messages
            ]
        )
        times = [
            str(item.get("ts") or "").strip()
            for item in messages
            if str(item.get("ts") or "").strip()
        ]
        same_date = bool(times) and len({time_text[:10] for time_text in times}) == 1
        rendered_rows: list[dict[str, Any]] = []
        member_labels: dict[str, str] = {}
        file_count = 0
        sender_ids: set[str] = set()
        has_master = False
        numbers: list[int] = []
        for item in messages:
            sender_id = str(item.get("sender_id") or "").strip()
            member = members.get(sender_id) or {}
            master = sender_id.lower() in trusted_masters
            has_master = has_master or master
            if sender_id:
                sender_ids.add(sender_id)
            file_count += len(list(item.get("files") or []))
            if item.get("n"):
                numbers.append(int(item.get("n")))
            number = 1 if master else int(member.get("number") or 0)
            rendered_rows.append(_digest_row(item, sender_no=number, same_date=same_date))
            if master:
                master_n.append(int(item.get("n") or 0))
                continue
            label = _digest_label(member.get("name") or item.get("sender_name"))
            if number >= 2 and label and label.casefold() != "master":
                member_labels.setdefault(str(number), label)
        if len(member_labels) > _CHANNEL_DIGEST_MEMBERS_MAX:
            member_labels = dict(list(member_labels.items())[:_CHANNEL_DIGEST_MEMBERS_MAX])
        window_start = min(times) if times else str(payload.get("window_start") or "").strip()
        window_end = max(times) if times else str(payload.get("window_end") or "").strip()
        header_line = _digest_json(
            _digest_header_payload(
                window_start=window_start,
                window_end=window_end,
                members=member_labels,
            )
        )
        row_lines = [_digest_json(row) for row in rendered_rows]
        text = "\n".join([header_line, *row_lines]) if row_lines else header_line
        digest_preview, digest_preview_meta = _digest_preview_text(
            rows=rendered_rows,
            window_start=window_start,
            window_end=window_end,
            members=member_labels,
        )
        computed_stats = {
            "window_start": window_start,
            "window_end": window_end,
            "message_count": len(messages),
            "file_count": file_count,
            "participant_count": len(sender_ids),
            "has_master": has_master,
            "n_start": min(numbers) if numbers else 0,
            "n_end": max(numbers) if numbers else 0,
        }
    else:
        computed_stats = {}
    master_n = sorted({number for number in master_n if number > 0})
    if not text:
        raise HTTPException(status_code=400, detail="digest_text_required")
    if len(text) > _CHANNEL_DIGEST_TEXT_MAX_CHARS:
        raise HTTPException(status_code=413, detail="digest_text_too_large")

    artifact_ref = ""
    agent = peek_global_agent()
    content_store = getattr(getattr(agent, "main_task_service", None), "content_store", None)
    if content_store is not None:
        try:
            envelope = content_store.maybe_externalize_text(
                text,
                runtime={"task_id": digest_artifact_task_id(entry.session_key), "session_key": entry.session_key},
                force=True,
                display_name=f"QQ group digest {entry.external_key}",
                source_kind="channel_digest",
            )
            artifact_ref = str(getattr(envelope, "ref", "") or "").strip()
        except Exception as exc:  # noqa: BLE001 - fallback to inline digest
            logger.warning("channel digest artifact externalization failed: {}", exc)

    try:
        heartbeat = get_web_heartbeat_service(agent)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="heartbeat_unavailable") from exc
    enqueue = getattr(heartbeat, "enqueue_channel_digest", None)
    if not callable(enqueue):
        raise HTTPException(status_code=503, detail="heartbeat_unavailable")

    try:
        next_push_in_seconds = max(0.0, float(payload.get("next_push_in_seconds") or 0))
    except (TypeError, ValueError):
        next_push_in_seconds = 0.0
    normalized: dict[str, Any] = {
        "session": entry.external_key,
        "window_start": str(computed_stats.get("window_start") or payload.get("window_start") or ""),
        "window_end": str(computed_stats.get("window_end") or payload.get("window_end") or ""),
        "message_count": int(computed_stats.get("message_count") or payload.get("message_count") or 0),
        "file_count": int(computed_stats.get("file_count") or payload.get("file_count") or 0),
        "participant_count": int(computed_stats.get("participant_count") or payload.get("participant_count") or 0),
        "has_master": bool(computed_stats.get("has_master", payload.get("has_master"))),
        "n_start": int(computed_stats.get("n_start") or payload.get("n_start") or 0),
        "n_end": int(computed_stats.get("n_end") or payload.get("n_end") or 0),
        "digest_id": str(payload.get("digest_id") or ""),
        "artifact_ref": artifact_ref,
        "master_n": list(master_n),
        "digest_preview": digest_preview,
        "digest_preview_complete": bool(digest_preview_meta.get("complete")),
        "digest_preview_n_start": int(digest_preview_meta.get("n_start") or 0),
        "digest_preview_n_end": int(digest_preview_meta.get("n_end") or 0),
        "next_push_in_seconds": next_push_in_seconds,
        "flush_reason": str(payload.get("flush_reason") or "").strip(),
    }
    if digest_preview_meta.get("omitted"):
        normalized["digest_omitted"] = dict(digest_preview_meta["omitted"])
    if digest_preview_meta.get("truncated"):
        normalized["digest_truncated"] = dict(digest_preview_meta["truncated"])
    if not artifact_ref and not digest_preview:
        normalized["digest_text"] = text
    queued = bool(enqueue(entry.session_key, normalized))
    return {
        "ok": True,
        "queued": queued,
        "artifact_ref": artifact_ref,
        "session_id": entry.session_key,
    }


@router.post("/turns/{turn_id}/pause")
async def pause_external_turn(
    turn_id: str,
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    try:
        service = get_external_turn_service()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="runtime_unavailable")
    record = service.get_turn(turn_id)
    if record is None or record.bridge_id != principal.bridge_id:
        raise HTTPException(status_code=404, detail="turn_not_found")
    try:
        paused = await service.pause_turn(turn_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="turn_not_found")
    return {"ok": True, "paused": bool(paused), "turn_id": record.turn_id, "session_id": record.session_key}


_TASK_CANCEL_FALLBACK = "web 侧任务命令：task_commands / fail_node(根节点, force)"


@router.post("/sessions/{session_id}/cancel")
async def cancel_external_session(
    session_id: str,
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    """取消本会话的在飞工作：在跑的回合 + 本会话派出去还没落终态的任务。

    以前这里只停协程。外部回合是刻意以 `None` 键注册的（见 `external_turns` 的
    None-key 合同），所以回合一收尾协程表就空了，`cancelled:0` 会被调用方读成
    "没有在飞工作"，而它自己派生的、会写盘的任务还在跑——工具描述当时却写着"回合 +
    本会话派发的任务"。现在两半都停，并把**没停干净的**点名出来。

    取消是请求式的：分离部署（worker）下节点在下一个检查点才落终态，所以
    `tasks_still_running` 在紧随的那次读数里可能还是正数，这不是失败。
    """
    entry = _resolve_entry(session_id, principal)
    try:
        service = get_external_turn_service()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="runtime_unavailable")
    cancelled_turns = int(await service.cancel_session(entry.session_key) or 0)
    return {
        "ok": True,
        "session_id": entry.session_key,
        **await cancel_session_inflight_work(
            session_key=entry.session_key,
            cancelled_turns=cancelled_turns,
            task_service=getattr(peek_global_agent(), "main_task_service", None),
        ),
    }


async def cancel_session_inflight_work(
    *, session_key: str, cancelled_turns: int, task_service: Any
) -> dict[str, Any]:
    """把"本会话的在飞工作"停干净，并自报没停干净的部分。

    单独一颗函数是因为这圈循环得能被桩打：真任务服务只在活体里有，而这里错一次的代价
    是"调用方以为已经停了、任务还在同一棵工作树上改文件"。
    """
    lister = getattr(task_service, "list_unfinished_tasks_for_session", None)
    canceller = getattr(task_service, "cancel_task", None)
    cancelled_task_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    unfinished: list[Any] = []
    if callable(lister):
        try:
            unfinished = list(lister(str(session_key or "").strip()) or [])
        except Exception:
            unfinished = []
    for task in unfinished:
        task_id = str(getattr(task, "task_id", "") or "").strip()
        if not task_id:
            continue
        if not callable(canceller):
            skipped.append(
                {"target": task_id, "reason": "no_task_cancel_surface", "how_to_clear": _TASK_CANCEL_FALLBACK}
            )
            continue
        try:
            record = await canceller(task_id)
        except Exception as exc:
            skipped.append(
                {
                    "target": task_id,
                    "reason": str(exc).strip() or "cancel_refused",
                    "how_to_clear": _TASK_CANCEL_FALLBACK,
                }
            )
            continue
        if record is None:
            skipped.append(
                {"target": task_id, "reason": "task_not_found", "how_to_clear": _TASK_CANCEL_FALLBACK}
            )
            continue
        cancelled_task_ids.append(task_id)

    tasks_still_running = 0
    if callable(lister):
        try:
            tasks_still_running = len(list(lister(str(session_key or "").strip()) or []))
        except Exception:
            tasks_still_running = 0

    payload: dict[str, Any] = {
        # `cancelled` 是对接手册里已发布的字段，含义收口成"停掉的回合数 + 停掉的任务数"。
        "cancelled": int(cancelled_turns) + len(cancelled_task_ids),
        "cancelled_turns": int(cancelled_turns),
        "cancelled_tasks": len(cancelled_task_ids),
        "task_ids": cancelled_task_ids,
        "skipped": skipped,
        "tasks_still_running": tasks_still_running,
    }
    if skipped:
        payload["hint"] = (
            "有目标没被这次取消落掉，按 skipped[].how_to_clear 处置；"
            "分离部署下 tasks_still_running 会在之后几次读数里归零。"
        )
    return payload


# -- outbox (durable proactive push ledger) ---------------------------------

# 桥能写回的销账状态。其他值一律按 delivered 处理：账本出口必须是封闭集合，
# 不能让一个拼错的 status 变成"既没投递也没回收"的第三种状态。
_OUTBOX_ACK_STATUSES = frozenset(
    {"delivered", "unreachable", "undeliverable", "unconfirmed"}
)


@router.get("/outbox/pending")
async def list_pending_outbox_entries(
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    """Pending durable pushes owned by this bridge (pump warm-up list).

    进程重启后桥的 sessions 映射清空、pump 只等下一条入站消息才建；桥在启动时
    用本列表为有滞留推送的会话预热 pump。消息正文经各会话 SSE 流的重放投递，
    本端点只返回路由身份，不返回文本。
    """
    own_session_keys = {
        entry.session_key
        for entry in _registry().list_bridge_sessions(principal.bridge_id)
        if entry is not None
    }
    items = [
        {
            "outbox_id": str(record.get("id") or ""),
            "session_id": str(record.get("session_key") or ""),
            "external_key": str(record.get("external_key") or ""),
            "ts": str(record.get("ts") or ""),
        }
        for record in load_pending_outbound()
        if str(record.get("session_key") or "") in own_session_keys
    ]
    return {"ok": True, "items": items}


@router.post("/sessions/{session_id}/outbox/{outbox_id}/ack")
async def ack_outbox_entry(
    session_id: str,
    outbox_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    """Close one durable outbox entry (idempotent, session-scoped).

    桥在渠道 API 确认送达后调用；ack 落在 append-only 账本上，启动重放只投
    未 ack 的条目。跨会话的 outbox_id 一律拒绝（``acked=false``）。

    可选 body ``{"status": "undeliverable"}`` 让桥在投递预算耗尽时按终态销账：
    账本已不按年龄清理记录，这是永久拒投的目标唯一的重试上界。
    ``{"status": "unconfirmed"}`` 是另一条终态出口——请求已发给渠道但没拿到回执，
    桥据此不再重发这一条，避免用户收到第二份逐字相同的回答。不带 body 时
    仍是 ``delivered``，旧桥的请求形状不变。
    """
    entry = _own_entry(session_id, principal)
    raw = dict(payload or {})
    status = str(raw.get("status") or "").strip().lower()
    if status not in _OUTBOX_ACK_STATUSES:
        status = "delivered"
    kind = str(raw.get("kind") or "").strip().lower()
    shape = str(raw.get("shape") or "").strip().lower()
    passive_window_open = raw.get("passive_window_open")
    text_preview = str(raw.get("text_preview") or "").strip()
    attachment_failures = _normalize_attachment_failures(raw.get("attachment_failures"))
    acked = ack_outbound_message(outbox_id, session_key=entry.session_key, status=status)
    record = get_outbound_record(outbox_id)
    proactive = str((record or {}).get("event") or "") == PROACTIVE_EVENT
    failure_notice = False
    attachment_notice = False
    if proactive and status == "delivered" and shape == "active":
        # 主动形状送达成功 = 该群确实具备主动消息能力；清掉之前的 denied/quota。
        get_external_session_registry().note_active_message_state(entry.session_key, "")
    if proactive and status == "undeliverable" and kind:
        if kind == "active_denied":
            get_external_session_registry().note_active_message_state(entry.session_key, "denied")
        elif kind == "quota_exhausted":
            get_external_session_registry().note_active_message_state(entry.session_key, "quota")
        if _remember_delivery_failure(outbox_id, prefix="delivery"):
            failure_payload = {
                "session_id": entry.session_key,
                "external_key": entry.external_key,
                "outbox_id": str(outbox_id or ""),
                "kind": kind,
                "shape": shape,
                "passive_window_open": (
                    bool(passive_window_open) if passive_window_open is not None else False
                ),
                "text_preview": text_preview[:500],
                "detail": str(raw.get("detail") or "")[:1000],
                "received_at": datetime.now().isoformat(),
            }
            try:
                heartbeat = get_web_heartbeat_service()
                if heartbeat is not None:
                    failure_notice = bool(heartbeat.enqueue_delivery_failure(failure_payload))
            except Exception as exc:  # noqa: BLE001 - 唤醒失败不改变 ack 结果
                logger.warning("delivery failure heartbeat enqueue failed: {}", exc)
    if attachment_failures and _remember_delivery_failure(outbox_id, prefix="attachment"):
        # 正文送达、附件没投出去：这条车道与 delivery_failure 分开——那条 reason 会
        # 压掉模型的回复，而这里正文已经落地，模型要能回话或重发。
        attachment_payload = {
            "session_id": entry.session_key,
            "external_key": entry.external_key,
            "outbox_id": str(outbox_id or ""),
            "attachment_failures": attachment_failures,
            "received_at": datetime.now().isoformat(),
        }
        try:
            heartbeat = get_web_heartbeat_service()
            if heartbeat is not None:
                attachment_notice = bool(
                    heartbeat.enqueue_attachment_failure(attachment_payload)
                )
        except Exception as exc:  # noqa: BLE001 - 唤醒失败不改变 ack 结果
            logger.warning("attachment failure heartbeat enqueue failed: {}", exc)
    return {
        "ok": True,
        "acked": bool(acked),
        "outbox_id": str(outbox_id or ""),
        "status": status,
        "failure_notice": failure_notice,
        "attachment_notice": attachment_notice,
    }


# -- events (SSE) -----------------------------------------------------------


def _sse_chunk(event: dict[str, Any]) -> str:
    data = json.dumps(event, ensure_ascii=False)
    return f"id: {event.get('seq')}\nevent: {event.get('type')}\ndata: {data}\n\n"


def _ack_streamed_outbox_event(
    event: dict[str, Any],
    session_key: str,
    *,
    self_ack: bool = False,
) -> None:
    """外部 SSE 把 `reply.final` 写出去了 = 拉取式客户端已经拿到正文，就地销账。

    与桥在渠道 API 确认后发起的 ack 同义；重复 ack 是幂等墓碑。跨进程的
    MCP 网关（negi_get_reply 走这条 SSE）过去没有任何 ack 点，未取走的回复
    只能靠对账循环无限重投。

    `self_ack=True` 时这一条整体让位：连接方声明"我拿到哪条就自己销哪条"（MCP 网关
    就这么接），服务端替它销账会出事——这一次连接点名要的是别的回合，帧被它丢弃，
    账却已经签了，真正等这条回复的人此后永远取不到，账面还显示"已送达"。

    只销 `reply.final`：主动推送那一类（`outbound.created`）在这一条流上有现成的
    消费者（渠道桥），也有根本没有消费者的（纯拉取式会话），服务端看不出客户端会不会
    把这一帧丢掉。按帧类型一律销账会让"问一下有没有回复"这一次连接把账本里唯一的存货
    标成已交付——正文仍在转录里，但待投递这个事实没了。主动推送由真正取走它的一方
    ack：桥发送成功后调 `/outbox/{id}/ack`，拉取式客户端返回它时同样调这一颗。
    """
    if self_ack:
        return
    if str((event or {}).get("type") or "") != REPLY_EVENT:
        return
    outbox_id = str((event or {}).get("outbox_id") or "").strip()
    if not outbox_id:
        return
    try:
        ack_outbound_message(outbox_id, session_key=session_key)
    except Exception:
        pass


@router.get("/sessions/{session_id}/events")
async def stream_external_events(
    session_id: str,
    request: Request,
    principal: ExternalApiPrincipal = Depends(require_external_api),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    self_ack: bool = Query(
        default=False,
        description="连接方自己认领拿到的正文：为真时服务端不再在帧写出时替它销账。",
    ),
):
    entry = _resolve_entry(session_id, principal)
    hub = get_session_event_hub(entry.session_key)
    queue = hub.subscribe()
    try:
        last_seq = int(str(last_event_id or "0").strip() or 0)
    except ValueError:
        last_seq = 0
    # 服务端重启后 hub.seq 从 0 重新计数，独立进程桥缓存的却是旧进程大序号：
    # 越过本进程已发布序号的 Last-Event-ID 一律视为旧进程遗留，从头重放积压。
    if last_seq > hub.last_seq:
        last_seq = 0

    async def generate():
        # 订阅先于重放：这两者之间发布的事件既进了队列也留在缓冲区，按 seq 去重
        # 才能保证一条事件只落一次（重复的 reply.final 就是用户看到两条相同回复）。
        emitted_seq = last_seq
        try:
            for event in hub.replay(last_seq):
                emitted_seq = max(emitted_seq, int(event.get("seq") or 0))
                yield _sse_chunk(event)
                _ack_streamed_outbox_event(event, entry.session_key, self_ack=self_ack)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_INTERVAL_SECONDS)
                    seq = int(event.get("seq") or 0)
                    if seq and seq <= emitted_seq:
                        continue
                    emitted_seq = max(emitted_seq, seq)
                    yield _sse_chunk(event)
                    _ack_streamed_outbox_event(event, entry.session_key, self_ack=self_ack)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
