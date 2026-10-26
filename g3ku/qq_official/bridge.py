"""botpy-bound network runtime for the QQ official adapter.

This is the ONLY module that imports ``qq-botpy``, and it does so lazily, so
the rest of g3ku has no hard dependency on it. Everything on the g3ku side is
done through the generic External Agent API over loopback (see
``g3ku/qq_official/client.py``); this file only wires botpy events to that
client and QQ message posting back out.

Real-device seam: the botpy SDK surface is verified against ``qq-botpy==1.2.1`` —
``on_<event>`` handler dispatch, message field names (``group_openid``,
``author.user_openid``, ``guild_id``/``channel_id``/``id``/``content``), the
``attachments`` list on inbound messages (``content_type`` / ``url`` /
``filename`` — image and file payloads are downloaded and forwarded to
``/api/v1`` as ``data_base64`` attachments, see docs/architecture/external-agent-api.md
「回合契约」), and ``post_message`` / ``post_group_message`` /
``post_c2c_message`` / ``post_dms`` signatures. Outbound media uploads go
through ``file_data`` (base64) on the same `/files` routes: botpy's
``post_group_file`` / ``post_c2c_file`` wrappers only expose the ``url``
parameter, which the platform fetches itself (public reachability required),
so the bridge reuses the SDK's own ``Route`` and auth headers but posts the
upload on its own media client — the SDK's single ``Client(timeout=...)`` knob
(5s by default) applies to every API call including text messages, and an
upload needs a budget derived from the payload size instead. See
docs/architecture/external-agent-api.md「内置官方 QQ 适配器」.
IMPORTANT: ``Client.run()`` is a blocking entry point
(``loop.run_until_complete``) that raises ``RuntimeError: This event loop is
already running`` inside the web runtime's live loop — this bridge therefore
uses the async entry (``async with client: await client.start(...)``), sharing
the uvicorn loop so handlers can drive the loopback ``/api/v1`` client
directly. The remaining unknown is live QQ gateway behavior (credential
validation, intents/event subscription), which needs a real AppID/AppSecret.
All logic reachable without a QQ account (mapping, client, service,
provisioning, bridge wiring) is unit-tested elsewhere.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from loguru import logger

from g3ku.qq_official.client import ExternalApiClient
from g3ku.qq_official.messages import (
    OUTBOUND_EVENT,
    bridge_id_for_app_id,
    external_key_for_c2c,
    external_key_for_group,
    external_key_for_guild,
    external_key_for_guild_dm,
    idempotency_key_for,
    is_deliverable_event,
    parse_external_key,
)

StateCallback = Callable[[str, str], None]


class QqGatewaySessionEndedError(RuntimeError):
    """网关会话列表排空后的到期重登，不是故障。

    QQ 网关按约 1 小时用 close 4009（"Session timed out"）判 wss 会话过期，botpy
    于是把它那一份 ``_session_list`` 跑空；桥只跑一份列表（见 ``run_qq_official_bridge``
    的 ``ret_coro`` 注释），所以列表一空就得整体重登换新列表。实盘两个号各 36 次、
    间隔 60 分钟，服务层要把它和真崩溃分开记，否则折叠规则永远不生效、每小时往
    ``console.log`` 灌一份全栈，面板还每小时闪一次 ``error``。
    """


_BOTPY_TASK_PREFIX = "[botpy]"
_BOTPY_CORO_QUALNAMES = {
    "ConnectionSession._runner",
    "BotWebSocket.ws_connect",
    "BotWebSocket._send_heart",
    "Client._run_event",
}

# 入站附件上限，镜像 /api/v1 的双上限（``g3ku/runtime/api/external_v1.py``：
# 图片沿用 ``WEB_CEO_IMAGE_UPLOAD_MAX_BYTES``，其余文件类附件放宽到 20MiB）；
# 超限附件在服务端会被 413 拒绝，这里按 kind 预过滤并提前截断下载。
_MAX_INBOUND_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_INBOUND_FILE_BYTES = 20 * 1024 * 1024
_MAX_INBOUND_ATTACHMENTS = 4
_MEDIA_DOWNLOAD_CHUNK_BYTES = 64 * 1024
# QQ 媒体 URL 在事件落地的那一瞬间不一定可读（实测同一条语音链接先失败、几分钟后
# 返回 200 audio/mp3），所以取一次不算数：允许一次短延时重试。
_ATTACHMENT_DOWNLOAD_MAX_ATTEMPTS = 2
_ATTACHMENT_DOWNLOAD_RETRY_DELAY_SECONDS = 1.5
# 出站附件：桥从本机签名媒体 URL 取字节的下载上限（与服务端出站产出上限一致）。
# 超过即降级为签名链接文本，不向平台发起必然失败的巨型上传。
_MAX_OUTBOUND_ATTACHMENT_BYTES = 20 * 1024 * 1024

# 媒体上传的预算不能借 SDK 那枚全局超时：``botpy.Client(timeout=5)`` 套在桥的每一个
# API 请求上（文本消息也是它），且 ``http.request`` 对超时只打一条 WARNING 就返回
# ``None``。上传预算按请求体字节数推导——固定开销（握手与网关收下整段 base64 后落
# 媒体存储）加上按可接受的最低上行吞吐折算的传输时间；低于该吞吐就该判失败，而不是
# 无限等。固定开销这一档目前只有握手的实测（本机到 api.sgroup.qq.com 的 DNS+TLS
# 0.56s），网关落媒体要多久靠 ``qq-official file_data uploaded`` 逐发记录的实耗重定。
# 上限由上面的下载封顶决定：20MiB 原始文件的 base64 请求体约 27MiB，预算 68s，
# 且只发生在带附件的那一发上。
_MEDIA_UPLOAD_FIXED_BUDGET_SECONDS = 15.0
_MEDIA_UPLOAD_MIN_THROUGHPUT_BYTES_PER_SECOND = 512 * 1024


def _media_upload_timeout(body_bytes: int) -> float:
    """一次 ``file_data`` 上传的读写预算（秒）；连接超时另按 5 秒判死。"""
    transfer = body_bytes / _MEDIA_UPLOAD_MIN_THROUGHPUT_BYTES_PER_SECOND
    return _MEDIA_UPLOAD_FIXED_BUDGET_SECONDS + transfer


# 服务端排队回执兜底文案（正常取 /api/v1 响应里的 receipt）。
_QUEUED_RECEIPT_FALLBACK_TEXT = "收到，将在当前任务中一并处理。"

# 语音转写进正文时带来源标记：不带标记，听错的语音和手打文字在模型眼里无法区分，
# 它就没办法说"你刚发的语音里那句我没听清"。识别失败另起一行措辞——把失败也写成
# "机器识别结果：" 等于让模型把一句失败说明当成用户说的话。
_VOICE_TRANSCRIPT_PREFIX = "用户语音，机器识别结果："
_VOICE_FAILURE_PREFIX = "用户语音，机器识别失败："
# 附件取不到时也要给用户留一句话：纯图片/文件消息在下载失败或超限时正文与附件都是空的，
# 没有这行就会在 on_incoming 的早退里整条消失（"发了东西然后什么都没有"）。只报名字与
# 形态，绝不把带签名的媒体 URL 写进模型正文。
_ATTACHMENT_FAILURE_PREFIX = "用户附件，机器取不到："

# QQ 官方附件的 content_type 是**类别词**而不是 MIME：实盘一条语音收到的是
# `['voice']`（18:05:42），而 `startswith("audio/")` 永远不命中它，语音于是
# 被当文件转发、从来进不了转写道。两种形状都认，因为别处仍可能给 MIME。
_AUDIO_CONTENT_TYPES = {"voice", "audio", "sound", "speech"}
_IMAGE_CONTENT_TYPES = {"image", "picture", "photo"}
_VIDEO_CONTENT_TYPES = {"video"}


def _content_kind(content_type: str) -> str:
    """'image' | 'audio' | 'video' | 'other' from whatever the platform put there."""
    value = str(content_type or "").strip().lower()
    if not value:
        return "other"
    if value in _AUDIO_CONTENT_TYPES or value.startswith("audio/"):
        return "audio"
    if value in _IMAGE_CONTENT_TYPES or value.startswith("image/"):
        return "image"
    if value in _VIDEO_CONTENT_TYPES or value.startswith("video/"):
        return "video"
    return "other"


def _declared_mime(content_type: str) -> str:
    """The label only when it actually is a MIME type; QQ sends category words
    there, and forwarding `'voice'` as a mime_type misleads the server-side
    descriptor and the file-name extension guess."""
    value = str(content_type or "").strip().lower()
    return value if "/" in value else ""

# pump 重连退避。SSE 流断开（服务端事件循环阻塞超过读超时、网络抖动、进程重启）
# 后必须自动重连：pump 一旦终结且不再重建，该会话的所有主动推送（心跳升级、
# cron 提醒、任务终态）都会永久滞留在服务端事件缓冲里，形成"能收不能发"的
# 僵尸渠道。旧实现只打一条日志就结束任务，注释承诺的 reconnect loop 并不存在。
_PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS = 1.0
_PUMP_RECONNECT_MAX_BACKOFF_SECONDS = 60.0
# 同一条事件连续投递失败达到上限后跳过：投递失败不推进 seq，重连后服务端按
# last_seq 重放本条自动重试；但毒消息（如目标永久 4xx）不能把 pump 卡死在
# 同一条上，达到上限记 error 后放弃并继续消费后续事件。
_PUMP_DELIVER_MAX_ATTEMPTS = 5

# 群聊回复走平台的被动回复车道：入站 @消息的消息 id 在窗口内可以回它几发。不带
# ``msg_id`` 的形状按平台规则算**主动消息**，需要单独具备的「主动消息」权限——实盘群
# 目标没有它：每一次 POST 都被判 400 / code 40034105「主动消息失败, 无权限」，5 次投递
# 预算打穿后按 ``undeliverable`` 销账，形态是"网页有完整回复、群里什么都没有"（同形状
# 在单聊实测 66/66 发得出去，所以被动参数只给群聊这一条已经不通的车道）。两档上限都
# 抄平台口径：窗口 5 分钟、同一 msg_id 最多回 5 次，且相同 msg_id+msg_seq 重复发送会
# 失败——所以每一发换新 seq，超窗或回满就落回主动形状，而不是拿必然被拒的参数占住预算。
_PASSIVE_REPLY_WINDOW_SECONDS = 5 * 60.0
_PASSIVE_REPLY_MAX_PER_MESSAGE = 5
# 被动引用按目标数增长（一个群一条），LRU 兜住长期不再回复的键。
_PASSIVE_REF_TARGETS_MAX = 256

# 主动消息的额度：平台口径是单关系 20/qpm、每日 1000 条/目标。额度只对**主动形状**
# 计费——被动回复不占主动额度，所以下面这套节流只在没有窗口参数的那一发上生效。
# 每分钟留出余量（15 而不是 20）：平台同时还有 Bot 维度 60/qpm 与其它会话在共用。
_ACTIVE_SEND_PER_MINUTE = 15
_ACTIVE_SEND_PER_DAY = 1000
_ACTIVE_SEND_WINDOW_SECONDS = 60.0

# 不值得再花的投递预算：这三类失败重放多少次结果都一样，一次就够。
# 其余（网络、无回执、429、5xx、文案认不出的 4xx）照旧吃满 5 次预算。
_NON_RETRYABLE_SEND_KINDS = frozenset({"passive_expired", "active_denied", "quota_exhausted"})

# 桥侧周期 pending 对账：GET /outbox/pending 不再是启动一次性动作。服务端重启
# 落在 cron 触发与产出之间时，启动预热会扑空（账本尚空），滞留推送稍后才入账
# ——2026-09-14 23:00 定时日报正是这样丢掉的。周期对账为「有 pending 记录且无
# 存活 pump」的会话重建 pump，让 SSE 重放把账补齐。
_PENDING_RECONCILE_INTERVAL_SECONDS = 30.0
# outbox_id 维度投递历史（有界 LRU）：acked 集防服务端对账/重放副本重复投递；
# attempts 计数跨 seq 封顶同一 id 的总尝试次数（重放副本 seq 不同，per-seq
# failed_attempts 会对每个副本重新计数）；unconfirmed 集拦住「已发出但没等到回执」
# 那条的重放副本，见 ``DeliveryUnconfirmedError`` 与 pump 的出站幂等闸门。
_PUMP_OUTBOX_HISTORY_MAX_IDS = 1024


class DeliveryUnconfirmedError(RuntimeError):
    """渠道 API 无回执：请求已经写进这条连接，只是没等到响应。

    botpy 的 http 层把 ``asyncio.TimeoutError`` 转成一条 WARNING 后返回 ``None``，
    所以到这里已经分辨不出「没送达」与「送达了但回执丢了」。重放会给出第二份逐字
    相同的答案，因此 pump 把带 ``outbox_id`` 的这类失败记进投递历史并跳过后续副本；
    连接建立前的失败（``ClientConnectorError`` 一类）照旧走重放重试，不受这道闸影响。
    """


class ChannelSendError(RuntimeError):
    """渠道发送失败的归类，决定 pump 还要不要为这一发继续花投递预算。

    botpy 的 http 层把 HTTP 400 也抛成 ``ServerError``——只带平台文案、不带数字码，
    所以这里只能按文案认两类确定性失败（``passive_expired`` 窗口过期、
    ``active_denied`` 目标没有主动消息权限）。文案认不出的一律按 ``transient`` 处理：
    宁可多试几次，也不要把一次网络抖动判成永久失败。``rate_limited`` 是本地节流器
    自己抛的（还没发出去），同样可重试。
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind


# 平台文案里的确定性失败标记：认不出就不当确定性失败（见 ``ChannelSendError``）。
_ACTIVE_DENIED_MARKERS = ("无权限",)
_PASSIVE_EXPIRED_MARKERS = ("msg_id", "过期")


def _classify_send_failure(exc: Exception) -> str:
    text = str(exc)
    if any(marker in text for marker in _ACTIVE_DENIED_MARKERS):
        return "active_denied"
    lowered = text.lower()
    if all(marker in lowered for marker in _PASSIVE_EXPIRED_MARKERS):
        return "passive_expired"
    return "transient"


def _lru_remember(mapping: "OrderedDict[str, Any]", key: str, value: Any = None, *, limit: int = _PUMP_OUTBOX_HISTORY_MAX_IDS) -> None:
    """Bounded LRU write: remember ``key`` and evict the oldest beyond limit."""
    mapping[key] = value
    mapping.move_to_end(key)
    while len(mapping) > max(1, int(limit)):
        mapping.popitem(last=False)


@dataclass(slots=True)
class _SendPacer:
    """按目标给**主动**消息计数并节流。

    被动回复不占主动额度，所以只有拿不到窗口参数的那一发才过这里。撞到分钟档就抛
    ``rate_limited``（pump 按重连退避重试，天然不会齐发）；撞到日档是 ``quota_exhausted``
    ——那条今天没机会了，不该继续烧预算。
    """

    windows: dict[str, list[float]] = field(default_factory=dict)
    daily: dict[str, list] = field(default_factory=dict)

    def _prune(self, target: str, now: float) -> list[float]:
        recent = [stamp for stamp in self.windows.get(target, []) if now - stamp < _ACTIVE_SEND_WINDOW_SECONDS]
        self.windows[target] = recent
        return recent

    def _today(self, target: str) -> list:
        day = time.strftime("%Y-%m-%d")
        entry = self.daily.get(target)
        if not isinstance(entry, list) or entry[0] != day:
            entry = [day, 0]
            self.daily[target] = entry
        return entry

    def check(self, target: str) -> str:
        """返回 "" 表示可以发；否则返回该拒绝的 kind。"""
        now = time.monotonic()
        if len(self._prune(target, now)) >= _ACTIVE_SEND_PER_MINUTE:
            return "rate_limited"
        if int(self._today(target)[1]) >= _ACTIVE_SEND_PER_DAY:
            return "quota_exhausted"
        return ""

    def record(self, target: str) -> None:
        now = time.monotonic()
        self._prune(target, now).append(now)
        entry = self._today(target)
        entry[1] = int(entry[1]) + 1


@dataclass(slots=True)
class _BridgeDeliveryState:
    """一个号的投递账本，活过一轮 QQ 网关登录。

    账本必须比桥活得久，因为「这条已经送过了」的证据在别的地方一律留不住：服务端
    的事件缓冲（`g3ku.runtime.external_events` 的 hub）长在 web 进程里，桥重登不动
    它；账本的 ack 墓碑每小时随 `compact_outbox` 落盘，桥侧也拿不到持久读数。账本
    长在桥的闭包里时，每次重登都同时抹掉游标与去重集，于是新 pump 只能以
    ``last_seq=0`` 连上 SSE，缓冲里整段历史被重新交给桥——已投递且已销账的回复再投
    一遍（实盘 2026-10-12：03:29:14 网关到期重登，03:59:50 一次 `pump connecting
    last_seq=0` 打出三条 `qq-official delivered`，其中两条是 02:33:43 与 02:40:12
    已 ack 过的回答；账本里那一会话只剩 03:59:48 那一条 `reply.final`）。
    """

    # external_key -> session_id
    sessions: dict[str, str] = field(default_factory=dict)
    # session_id -> 本号最后一次成功投递（或判定跳过）的 hub seq
    seqs: dict[str, int] = field(default_factory=dict)
    # outbox_id 维度投递历史：acked 集跳过对账/重放产生的已销账副本；attempts 跨
    # seq 封顶同一 id 的总投递尝试（毒消息副本不随每份新 seq 重新获得完整预算）；
    # unconfirmed 集是「这条已经发过一份、只是没拿到回执」的凭据，重放副本据此不再
    # 第二次 POST（代价是该条可能真空未达，换掉的是每条无回执都可能翻倍）。
    acked_outbox_ids: OrderedDict[str, None] = field(default_factory=OrderedDict)
    outbox_attempts: OrderedDict[str, int] = field(default_factory=OrderedDict)
    unconfirmed_outbox_ids: OrderedDict[str, None] = field(default_factory=OrderedDict)
    # external_key -> [入站消息 id, 下一个 msg_seq, 被动窗口截止（monotonic 秒）]
    passive_refs: OrderedDict[str, list] = field(default_factory=OrderedDict)
    # 主动消息的分钟档与日档计数（被动回复不过这里）
    pacer: _SendPacer = field(default_factory=_SendPacer)


_DELIVERY_STATES: dict[str, _BridgeDeliveryState] = {}


def bridge_delivery_state(bridge_id: str) -> _BridgeDeliveryState:
    """按 bridge_id 取该号的投递账本（一个 AppID 一份，号与号之间不共享）。"""
    key = str(bridge_id or "")
    state = _DELIVERY_STATES.get(key)
    if state is None:
        state = _BridgeDeliveryState()
        _DELIVERY_STATES[key] = state
    return state


def reset_bridge_delivery_state() -> None:
    """Test hook: 丢掉所有号的账本（进程级状态需要这条隔离缝，同
    ``external_events.reset_session_event_hubs``）。"""
    _DELIVERY_STATES.clear()


def _note_passive_ref(state: _BridgeDeliveryState, external_key: str, event_id: Any) -> None:
    """记下入站消息 id——它是这一轮所有回复的被动窗口凭据。

    消息 id 此前只用来拼幂等键，用完就丢，于是每一发回复都只能以主动消息形状发出。
    """
    msg_id = str(event_id or "").strip()
    if not msg_id:
        return
    _lru_remember(
        state.passive_refs,
        external_key,
        [msg_id, 1, time.monotonic() + _PASSIVE_REPLY_WINDOW_SECONDS],
        limit=_PASSIVE_REF_TARGETS_MAX,
    )


def _passive_reply_kwargs(
    state: _BridgeDeliveryState, kind: str, external_key: str
) -> dict[str, Any]:
    """取这一发的被动回复参数；不给就是主动形状（平台按主动消息判，要权限）。

    每发换新 ``msg_seq``：相同的 msg_id+msg_seq 重复发送会被平台拒掉，而投递失败后
    pump 的重放正是"同一 msg_id 的下一发"。窗口外或回满上限时把这条引用摘掉。
    """
    if kind != "group":
        return {}
    ref = state.passive_refs.get(external_key)
    if not isinstance(ref, list):
        return {}
    msg_id, next_seq, deadline = ref
    if time.monotonic() >= float(deadline) or int(next_seq) > _PASSIVE_REPLY_MAX_PER_MESSAGE:
        state.passive_refs.pop(external_key, None)
        return {}
    ref[1] = int(next_seq) + 1
    return {"msg_id": str(msg_id), "msg_seq": int(next_seq)}


def _gate_active_send(
    state: _BridgeDeliveryState, kind: str, external_key: str, passive: dict[str, Any]
) -> None:
    """主动形状的那一发先过额度闸；被动回复不占主动额度，直接放行。"""
    if passive or kind not in ("group", "c2c"):
        return
    blocked = state.pacer.check(external_key)
    if blocked:
        raise ChannelSendError(blocked, f"active send {blocked} for {external_key}")


def _is_botpy_task(task: asyncio.Task) -> bool:
    """True for tasks botpy spawned on the shared loop.

    botpy creates its runner/heartbeat/websocket receive loops with
    ``ensure_future`` / ``create_task`` and never cancels them: on a bridge
    stop they would keep the QQ websocket alive. Detection: the ``[botpy]``
    task-name prefix (event handler tasks), the known botpy coroutine
    qualnames, or a coroutine defined inside the ``botpy`` package.
    """
    if task is asyncio.current_task():
        return False
    if str(task.get_name() or "").startswith(_BOTPY_TASK_PREFIX):
        return True
    coro = task.get_coro()
    if str(getattr(coro, "__qualname__", "") or "") in _BOTPY_CORO_QUALNAMES:
        return True
    filename = str(getattr(getattr(coro, "cr_code", None), "co_filename", "") or "")
    if not filename:
        return False
    return "botpy" in {part.lower() for part in filename.replace("\\", "/").split("/")}


def _create_media_client() -> httpx.AsyncClient:
    """Dedicated client for downloading inbound QQ media (separate from the
    loopback /api/v1 client so a slow CDN never blocks turn submission)."""
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=True)


async def _download_attachment_bytes(
    client: httpx.AsyncClient, url: str, *, max_bytes: int
) -> bytes | None:
    """Fetch one attachment; ``None`` on transport errors, non-200, or when the
    stream exceeds the per-kind /api/v1 cap.

    QQ media URLs are not always readable at the instant the event lands: a voice
    note that failed here with an empty exception string at 17:40:55 returned
    ``200 audio/mp3`` when the same URL was fetched minutes later. One retry is
    therefore part of the contract, not a nicety — without it a pure voice message
    degrades into nothing at all.
    """
    for attempt in range(1, _ATTACHMENT_DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    logger.warning(
                        "qq-official attachment download returned status {} for {}",
                        response.status_code,
                        url,
                    )
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes(_MEDIA_DOWNLOAD_CHUNK_BYTES):
                    total += len(chunk)
                    if total > max_bytes:
                        logger.warning(
                            "qq-official attachment exceeds {} bytes and was skipped: {}",
                            max_bytes,
                            url,
                        )
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:
            # 异常类型必须进日志：h11 送上来的 httpx.HTTPError 可以有空 __str__，
            # 只打 {} 会留一条读不出任何信息的 WARNING（实盘就是这样）。
            if attempt >= _ATTACHMENT_DOWNLOAD_MAX_ATTEMPTS:
                logger.warning(
                    "qq-official attachment download failed for {}: {}: {}",
                    url,
                    type(exc).__name__,
                    exc,
                )
                return None
            logger.info(
                "qq-official attachment download attempt {} failed ({}: {}); retrying in {:.1f}s",
                attempt,
                type(exc).__name__,
                exc,
                _ATTACHMENT_DOWNLOAD_RETRY_DELAY_SECONDS,
            )
            await asyncio.sleep(_ATTACHMENT_DOWNLOAD_RETRY_DELAY_SECONDS)
    return None


def _attachment_name(item: Any, content_type: str) -> str:
    name = str(getattr(item, "filename", "") or "").strip()
    if name:
        return name
    extension = mimetypes.guess_extension(content_type) or ".png"
    attachment_id = str(getattr(item, "id", "") or "").strip() or "image"
    return f"qq-{attachment_id}{extension}"


async def _collect_attachments(
    media_client: httpx.AsyncClient, message: Any
) -> tuple[list[dict[str, str]], list[str]]:
    """Download the attachments referenced by a botpy message.

    Returns ``(/api/v1 attachment payloads, voice transcript lines)``.

    botpy exposes ``message.attachments`` items with ``content_type`` / ``url``
    / ``filename`` / ``id``. All items with absolute http(s) URLs are
    forwarded: ``image/*`` as ``kind:"image"``, everything else (documents,
    archives, ...) as ``kind:"file"`` — the server stores non-image files and
    surfaces them to the model as local-path notes. Failures degrade to
    text-only delivery; a message that is *only* attachments and yields nothing
    is named and submitted by ``on_incoming`` instead of being dropped.

    ``audio/*`` is the exception: forwarded as a file it reaches the model as
    one local-path line and the utterance itself is lost, so when local
    speech-to-text is enabled the bytes are transcribed here and the text is
    returned instead of an attachment. With speech-to-text off, audio keeps
    its old file treatment byte-for-byte — the feature must never make an
    existing deployment see less than it used to.
    """
    from g3ku.stt import engine as stt_engine

    payloads: list[dict[str, str]] = []
    voice_lines: list[str] = []
    raw_items = list(getattr(message, "attachments", None) or [])
    if raw_items:
        # 平台到底给什么 content_type 只能这样看见：语音这条道的所有判据都挂在它上面，
        # 而事件本身不落在任何日志里（17:40 那次"没反应"事后无从区分"事件没到"与
        # "附件取不到"）。
        logger.info(
            "qq-official message {} carries {} attachment(s): {}",
            getattr(message, "id", ""),
            len(raw_items),
            [str(getattr(item, "content_type", "") or "") for item in raw_items],
        )
    if not raw_items:
        return payloads, voice_lines
    stt_ready = await stt_engine.inbound_voice_enabled()
    for item in raw_items:
        if len(payloads) >= _MAX_INBOUND_ATTACHMENTS:
            logger.warning(
                "qq-official message {} carries more than {} attachments; extras skipped",
                getattr(message, "id", ""),
                _MAX_INBOUND_ATTACHMENTS,
            )
            break
        content_type = str(getattr(item, "content_type", "") or "").strip().lower()
        url = str(getattr(item, "url", "") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        kind = _content_kind(content_type)
        if kind == "other":
            filename = str(getattr(item, "filename", "") or "").strip()
            kind = _content_kind(str(mimetypes.guess_type(filename)[0] or ""))
        is_image = kind == "image"
        is_audio = kind == "audio"
        max_bytes = _MAX_INBOUND_IMAGE_BYTES if is_image else _MAX_INBOUND_FILE_BYTES
        data = await _download_attachment_bytes(media_client, url, max_bytes=max_bytes)
        if not data:
            if is_audio and stt_ready:
                # 纯语音消息取不到字节时，正文与附件都是空的，on_incoming 会直接早退
                # ——用户端就是"发了东西然后什么都没有"。留下一行失败说明，回合照常提交。
                voice_lines.append(f"{_VOICE_FAILURE_PREFIX}语音附件下载失败")
            continue
        if not is_audio and stt_engine.is_voice_payload(data):
            # 标签不可信时按字节兜底：语音条的真身是腾讯 silk，content_type 却
            # 可能是 'voice' 之外的任何东西（含留空）。
            is_audio = True
        if is_audio and stt_ready:
            # 'voice' 不是 MIME，直接拿去猜扩展名会得到 .png（语音条被命名成图片）。
            name = _attachment_name(item, _declared_mime(content_type) or "audio/wav")
            result = await stt_engine.transcribe_bytes(
                data, filename=name, mime_type=_declared_mime(content_type), source="qq-voice"
            )
            if result.ok and result.text:
                voice_lines.append(f"{_VOICE_TRANSCRIPT_PREFIX}{result.text}")
                if result.wav_bytes:
                    # 让用户能回放原语音。存的必须是引擎解码后的 WAV 而不是收到的
                    # 字节：QQ 语音条是腾讯 silk，浏览器播不出来。
                    payloads.append(
                        {
                            "kind": "audio",
                            "name": f"{Path(name).stem or 'qq-voice'}.wav",
                            "mime_type": "audio/wav",
                            "data_base64": base64.b64encode(result.wav_bytes).decode("ascii"),
                        }
                    )
            else:
                logger.warning(
                    "qq-official voice {} was not transcribed: {} ({})",
                    name,
                    result.error_code,
                    result.error,
                )
                voice_lines.append(
                    f"{_VOICE_FAILURE_PREFIX}{result.error or result.error_code}"
                )
            continue
        # 走文件车道时同理：类别词不能当 MIME 用，扩展名要按识别出的媒体形态兜。
        declared = _declared_mime(content_type)
        # 猜扩展名用可猜的 MIME（guess_extension('audio/x-silk') 是 None → 会退回 .png）；
        # 对外声明的 mime_type 仍只在真是 MIME 时才填。
        fallback_mime = declared or ("audio/wav" if is_audio else "application/octet-stream")
        payloads.append(
            {
                "kind": "image" if is_image else "file",
                "name": _attachment_name(item, fallback_mime),
                "mime_type": declared or "application/octet-stream",
                "data_base64": base64.b64encode(data).decode("ascii"),
            }
        )
    return payloads, voice_lines


async def run_qq_official_bridge(
    *,
    app_id: str,
    app_secret: str,
    sandbox: bool,
    token: str,
    base_url: str,
    on_state: StateCallback,
) -> None:
    """Run until cancelled/error. ``on_state`` reports coarse status."""
    try:
        import botpy
    except Exception as exc:  # noqa: BLE001
        on_state("error", f"botpy 未安装（pip install qq-botpy）: {exc}")
        return

    try:
        intents = botpy.Intents(public_guild_messages=True, direct_message=True, public_messages=True)
    except TypeError as exc:
        on_state("error", f"botpy Intents 与当前 qq-botpy 版本不兼容: {exc}")
        return

    client = ExternalApiClient(base_url=base_url, token=token)
    media_client = _create_media_client()
    # 投递账本按号取，跨登录存活（见 ``_BridgeDeliveryState``）：pump 因此从上一次
    # 成功投递的 seq 续拉，而不是每次重登都从 0 重放整段服务端缓冲。
    state = bridge_delivery_state(bridge_id_for_app_id(app_id))
    sessions = state.sessions
    seqs = state.seqs
    acked_outbox_ids = state.acked_outbox_ids
    outbox_attempts = state.outbox_attempts
    unconfirmed_outbox_ids = state.unconfirmed_outbox_ids
    # pump 句柄属于这一轮登录，随桥的 finally 一起取消，绝不跨登录复用任务。
    pumps: set[asyncio.Task] = set()
    pump_tasks: dict[str, asyncio.Task] = {}

    async def on_incoming(
        external_key: str,
        text: str,
        event_id: str,
        attachments: list[dict[str, str]] | None = None,
        voice_lines: list[str] | None = None,
        attachment_names: list[str] | None = None,
    ) -> None:
        attachment_payloads = list(attachments or [])
        if voice_lines:
            # 语音转出的文字并入正文：QQ 语音往往是口语化的独立内容，并入而不是
            # 另起回合，用户才有"发了一段话 + 一段语音"的连贯语义。
            text = "\n".join([str(text or "").strip(), *[line for line in voice_lines if line]]).strip()
        if not text.strip() and not attachment_payloads:
            if attachment_names:
                # 一条纯附件消息在所有附件都取不到时（QQ 的媒体 URL 带签名且会过期）
                # 正文与附件同时为空。以前这里直接 return，用户端就是"发了张图，什么
                # 都没有"。契约要求纯附件消息照常提交，所以补一行点名式的说明再提交；
                # 混合正文的场景不加注，保持"降级为仅文本提交"原样。
                named = "、".join([name for name in attachment_names if name][:3]) or f"{len(attachment_names)} 项"
                text = f"{_ATTACHMENT_FAILURE_PREFIX}{named}：均未能取到"
            else:
                return
        session_id = sessions.get(external_key)
        if session_id is None:
            session_id = await client.ensure_session(external_key)
            sessions[external_key] = session_id
        # 先记下这条入站消息的 id，回复它的那几发才拿得到被动窗口参数。回合回复通常
        # 在入站后几秒落地（实盘 9 秒），远在窗口之内；不记就只能走主动消息形状。
        _note_passive_ref(state, external_key, event_id)
        # 每次入站都幂等校验 pump 存活：pump 意外终结（如 shutdown 竞态取消）时
        # 靠下一条用户消息自愈，而不是让该会话的出站永久无人消费。
        _spawn_pump(session_id, external_key)
        idem = idempotency_key_for(event_id)
        # 绝不回退到 external_key 当幂等键：external_key 对同一用户恒定，
        # 缺事件 id 的消息会用它撞掉该用户第一条消息的幂等位并被永久丢弃。
        # 没有幂等键时提交不带键的请求，服务端按新消息处理。
        response = await client.send_message(
            session_id,
            text,
            idempotency_key=idem or None,
            attachments=attachment_payloads or None,
        )
        payload = response if isinstance(response, dict) else {}
        if str(payload.get("status") or "").strip() == "queued":
            # 会话正忙、消息已排队：回执必须送达用户，否则用户看不到任何反馈，
            # 会以为消息被吞掉而重复发送。
            receipt = str(payload.get("receipt") or "").strip() or _QUEUED_RECEIPT_FALLBACK_TEXT
            try:
                await deliver(external_key, receipt)
            except Exception:  # noqa: BLE001 - 回执失败不影响消息本身
                logger.warning("qq-official failed to deliver queued receipt to {}", external_key)

    def _absolute_media_url(url: str) -> str:
        """Event attachment URLs are root-relative signed viewer paths; the
        platform-facing upload/download calls need an absolute URL. Joining
        happens against the base_url ORIGIN only: ``base_url`` carries the
        ``/api/v1`` suffix while the media viewer lives under the web root
        (``/api/ceo/...``)."""
        raw = str(url or "").strip()
        if not raw:
            return ""
        if raw.lower().startswith(("http://", "https://")):
            return raw
        parsed = urlparse(str(base_url))
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return f"{origin}{raw if raw.startswith('/') else '/' + raw}"

    def _media_file_type(mime_type: str) -> int:
        mime = str(mime_type or "").lower()
        if mime.startswith("image/"):
            return 1  # 图片（平台支持 png/jpg）
        if mime == "video/mp4":
            return 2  # 视频
        if mime.startswith("audio/"):
            return 3  # 语音
        return 4  # 任意文件（docx/pdf/zip…）

    def _media_files_route(kind: str, target: dict[str, str]) -> Any:
        from botpy.http import Route  # 惰性：botpy 只在桥运行时可用

        if kind == "group":
            return Route(
                "POST", "/v2/groups/{group_openid}/files", group_openid=target["group_openid"]
            )
        return Route("POST", "/v2/users/{openid}/files", openid=target["user_openid"])

    async def _upload_media_data(
        kind: str, target: dict[str, str], data: bytes, file_type: int
    ) -> str:
        """``file_data``（base64）直传媒体，返回 ``file_info``（空串=失败）。

        botpy 的 ``post_group_file`` / ``post_c2c_file`` 只暴露 ``url`` 参数——那
        条路径由 QQ 服务器回源下载，要求本服务端公网可达；``file_data`` 不回源，
        服务端只监听回环地址时同样成立。这一发不走 botpy 的 ``http.request``：SDK
        把 ``Client(timeout=...)``（默认 5 秒）那一枚套在桥的每一个 API 请求上，
        而媒体上传的预算和一条文本消息的预算不是同一回事；它超时也只打一条 SDK
        自己的 WARNING 就返回 ``None``，桥侧连失败原因都拿不到。这里沿用 SDK 的
        Route（域名与路径参数）和它的鉴权头续期，把 POST 发到桥自己的媒体 client
        上，预算由 ``_media_upload_timeout`` 按请求体字节数推导。
        """
        file_data = base64.b64encode(data).decode("ascii")
        body = {"file_type": file_type, "file_data": file_data, "srv_send_msg": False}
        budget = _media_upload_timeout(len(file_data))
        route = _media_files_route(kind, target)
        started = time.perf_counter()
        try:
            await bridge_api._http.check_session()
            route.is_sandbox = bridge_api._http.is_sandbox
            response = await media_client.post(
                route.url,
                json=body,
                headers=dict(bridge_api._http._headers or {}),
                timeout=httpx.Timeout(budget, connect=5.0),
            )
        except Exception as exc:  # noqa: BLE001 - 回退 url 路径
            logger.warning(
                "qq-official file_data media upload failed after {:.1f}s (budget {:.1f}s, {}); "
                "falling back to url upload",
                time.perf_counter() - started,
                budget,
                exc,
            )
            return ""
        try:
            uploaded = response.json()
        except ValueError:
            uploaded = None
        file_info = ""
        if isinstance(uploaded, dict):
            file_info = str(uploaded.get("file_info") or "").strip()
        if not file_info:
            logger.warning(
                "qq-official file_data media upload rejected status={} body={} after {:.1f}s "
                "(budget {:.1f}s); falling back to url upload",
                response.status_code,
                str(response.text)[:200],
                time.perf_counter() - started,
                budget,
            )
            return ""
        # 逐发实耗是这条车道的读数：预算的固定开销档靠它重定。
        logger.info(
            "qq-official file_data uploaded {} bytes in {:.1f}s (budget {:.1f}s)",
            len(file_data),
            time.perf_counter() - started,
            budget,
        )
        return file_info

    async def _upload_media_url(
        kind: str, target: dict[str, str], url: str, file_type: int
    ) -> str:
        """回退路径：平台按 URL 回源下载（要求本服务端公网可达）。"""
        try:
            if kind == "group":
                media = await bridge_api.post_group_file(
                    group_openid=target["group_openid"],
                    file_type=file_type,
                    url=url,
                    srv_send_msg=False,
                )
            else:
                media = await bridge_api.post_c2c_file(
                    openid=target["user_openid"],
                    file_type=file_type,
                    url=url,
                    srv_send_msg=False,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("qq-official url media upload failed ({})", exc)
            return ""
        return str(media.get("file_info") or "").strip() if isinstance(media, dict) else ""

    async def _deliver_attachment(
        kind: str, target: dict[str, str], external_key: str, attachment: dict[str, Any]
    ) -> bool:
        """Send one attachment as a real QQ file/media message; False means the
        caller must degrade it to a signed-URL text line.

        group/c2c：桥先从本机签名媒体 URL 取字节，再以 ``file_data``（base64）
        直传给平台媒体上传接口——不回源、不要求公网可达；``file_data`` 被拒时
        回退平台回源上传（``url`` 路径）。拿到 ``file_info`` 后以 ``msg_type=7``
        富媒体消息发出。
        guild/guilddm：平台没有文件接口，图片用 ``file_image`` 字节直发，其余降级。
        """
        name = str(attachment.get("name") or "attachment")
        url = _absolute_media_url(str(attachment.get("url") or ""))
        mime = str(attachment.get("mime_type") or "").lower()
        if not url:
            logger.warning("qq-official attachment {} carries no url; degrading", name)
            return False
        try:
            if kind in ("group", "c2c"):
                file_type = _media_file_type(mime)
                data = await _download_attachment_bytes(
                    media_client, url, max_bytes=_MAX_OUTBOUND_ATTACHMENT_BYTES
                )
                if not data:
                    raise RuntimeError(
                        "signed media download failed or exceeds the outbound attachment cap"
                    )
                file_info = await _upload_media_data(kind, target, data, file_type)
                if not file_info:
                    file_info = await _upload_media_url(kind, target, url, file_type)
                if not file_info:
                    raise RuntimeError("media upload failed on both file_data and url paths")
                if kind == "group":
                    media_passive = _passive_reply_kwargs(state, kind, external_key)
                    _gate_active_send(state, kind, external_key, media_passive)
                    try:
                        result = await bridge_api.post_group_message(
                            group_openid=target["group_openid"],
                            msg_type=7,
                            media={"file_info": file_info},
                            **media_passive,
                        )
                    except (asyncio.CancelledError, DeliveryUnconfirmedError):
                        raise
                    except Exception as exc:
                        failure = _classify_send_failure(exc)
                        raise ChannelSendError(
                            failure, f"group media send failed ({failure}): {exc}"
                        ) from exc
                    if not media_passive:
                        state.pacer.record(external_key)
                else:
                    result = await bridge_api.post_c2c_message(
                        openid=target["user_openid"],
                        msg_type=7,
                        media={"file_info": file_info},
                    )
                if result is None:
                    raise RuntimeError("unconfirmed media message (empty API response)")
                logger.info("qq-official delivered {} attachment {}", kind, name)
                return True
            if mime.startswith("image/"):
                data = await _download_attachment_bytes(
                    media_client, url, max_bytes=_MAX_OUTBOUND_ATTACHMENT_BYTES
                )
                if not data:
                    raise RuntimeError("image attachment download failed")
                if kind == "guild":
                    result = await bridge_api.post_message(
                        channel_id=target["channel_id"], file_image=data
                    )
                else:
                    result = await bridge_api.post_dms(
                        guild_id=target["guild_id"], file_image=data
                    )
                if result is None:
                    raise RuntimeError("unconfirmed guild media message (empty API response)")
                logger.info("qq-official delivered {} image attachment {}", kind, name)
                return True
            logger.warning(
                "qq-official has no file API for {} targets; attachment {} degraded to text link",
                kind,
                name,
            )
            return False
        except (asyncio.CancelledError, ChannelSendError):
            # 额度/确定性失败必须让整条事件走 pump 的重试与判毒，不能降级成链接文本
            # ——降级会把它当成"发出去了"，额度闸就此被绕过。
            raise
        except Exception as exc:  # noqa: BLE001 - degrade to signed-URL text line
            logger.warning(
                "qq-official attachment delivery failed for {} ({}); degrading to text link",
                name,
                exc,
            )
            return False

    async def deliver(
        external_key: str, text: str, attachments: list[dict[str, Any]] | None = None
    ) -> None:
        kind, target = parse_external_key(external_key)
        if kind not in ("group", "c2c", "guild", "guilddm"):
            logger.warning("qq-official cannot deliver to target external_key={}", external_key)
            return
        # 附件先于正文投递；失败的附件降级为签名下载链接行并入正文，用户至少
        # 拿得到可下载的链接。全部附件成功且正文为空时不再补发空文本消息。
        fallback_lines: list[str] = []
        for attachment in list(attachments or []):
            if not isinstance(attachment, dict):
                continue
            if await _deliver_attachment(kind, target, external_key, attachment):
                continue
            name = str(attachment.get("name") or "attachment")
            fallback_lines.append(f"📎 {name}: {_absolute_media_url(str(attachment.get('url') or ''))}")
        body = "\n".join(part for part in [str(text or "").strip(), *fallback_lines] if part).strip()
        if not body:
            return
        passive: dict[str, Any] = {}
        if kind == "group":
            passive = _passive_reply_kwargs(state, kind, external_key)
        _gate_active_send(state, kind, external_key, passive)
        try:
            if kind == "group":
                result = await bridge_api.post_group_message(
                    group_openid=target["group_openid"],
                    content=body,
                    msg_type=0,
                    **passive,
                )
            elif kind == "c2c":
                result = await bridge_api.post_c2c_message(openid=target["user_openid"], content=body, msg_type=0)
            elif kind == "guild":
                result = await bridge_api.post_message(channel_id=target["channel_id"], content=body)
            else:
                result = await bridge_api.post_dms(guild_id=target["guild_id"], content=body)
        except (asyncio.CancelledError, DeliveryUnconfirmedError):
            raise
        except Exception as exc:
            failure = _classify_send_failure(exc)
            logger.warning(
                "qq-official send failed kind={} shape={} target={} ({})",
                failure,
                "passive" if passive else "active",
                external_key,
                str(exc)[:200],
            )
            raise ChannelSendError(failure, f"{kind} send failed ({failure}): {exc}") from exc
        if not passive and kind in ("group", "c2c"):
            state.pacer.record(external_key)
        # botpy 的 http 层对请求超时只打一条 WARNING 就返回 None（吞掉
        # asyncio.TimeoutError）：无回执必须视为投递失败抛出，不能记一次假成功。
        # 抛的是 ``DeliveryUnconfirmedError``——pump 据此把这条 outbox 标记成
        # 「已发出、回执未知」，重放副本不再第二次 POST。
        if result is None:
            raise DeliveryUnconfirmedError(
                f"qq-official delivery unconfirmed (empty API response) for target {external_key}"
            )
        message_id = ""
        if isinstance(result, dict):
            message_id = str(result.get("id") or result.get("message_id") or "").strip()
        # 送达回执：排查"发没发出去"时以这行为准（published to hub 不代表送达）。
        # passive 这一栏分形状：`<消息id>#<seq>` 是被动回复，`-` 是主动消息形状——
        # 群目标被平台按主动消息拒投（40034105）时，只有这行能说明那一发是出了窗口。
        logger.info(
            "qq-official delivered {} message to {} (id={}, attachments={}, passive={})",
            kind,
            external_key,
            message_id or "-",
            len([item for item in list(attachments or []) if isinstance(item, dict)]),
            f"{passive['msg_id']}#{passive['msg_seq']}" if passive else "-",
        )

    async def _pump(session_id: str, external_key: str) -> None:
        """Per-session SSE consumer with a real reconnect loop.

        投递语义：只有 deliver 成功（或事件被判定不可投递/毒消息达到重试上限）
        才推进 ``seqs``；失败时跳出重连，服务端按 last_seq 重放，本条自动重试，
        重试节奏随重连退避（1s→60s 封顶）递增。SSE 干净结束（服务端重启）同样
        重连，绝不静默终结任务。

        唯一的例外是无回执失败（``DeliveryUnconfirmedError``）：请求已经发给渠道，
        重放只会给出第二份逐字相同的答案，所以带 ``outbox_id`` 的这类事件被记进
        ``unconfirmed_outbox_ids``，其重放副本直接按 ``unconfirmed`` 终态销账。
        """
        backoff = _PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS
        failed_seq = 0
        failed_attempts = 0
        while True:
            seen = seqs.get(session_id, 0)
            try:
                # 排查"入站正常、出站黑洞"的唯一分界证据：这条在意味着任务真的在跑，
                # 缺席就意味着 pump 从未被执行（任务没被调度），而不是连上了没事件。
                logger.info(
                    "qq-official pump connecting session={} last_seq={}",
                    session_id,
                    seen,
                )
                async for event in client.stream_events(session_id, last_seq=seen):
                    seq = int(event.get("seq") or seen)
                    event_type = str(event.get("type") or "")
                    text = str(event.get("text") or "").strip()
                    raw_attachments = event.get("attachments")
                    attachments = (
                        [item for item in raw_attachments if isinstance(item, dict)]
                        if isinstance(raw_attachments, list)
                        else []
                    )
                    if not is_deliverable_event(event_type) or (not text and not attachments):
                        seqs[session_id] = seq
                        backoff = _PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS
                        continue
                    target_key = external_key
                    # 回合回复也带 outbox_id（relay 发布前登记进账本）：去重与销账
                    # 必须按同一条 id 走，否则重放副本会被当成新消息重复投递。
                    outbox_id = str(event.get("outbox_id") or "").strip()
                    if event_type == OUTBOUND_EVENT:
                        target_key = str(event.get("external_key") or external_key)
                    if outbox_id and outbox_id in acked_outbox_ids:
                        # 已送达并销账的 id：服务端对账/重放产生的副本直接跳过，
                        # 只推进 seq（否则周期对账会造成重复推送）。
                        seqs[session_id] = seq
                        backoff = _PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS
                        continue
                    if outbox_id and outbox_id in unconfirmed_outbox_ids:
                        # 出站幂等闸门：上一份 POST 已发给渠道、只是没等到回执，
                        # 这里再发就是给用户第二份逐字相同的回答。按 unconfirmed
                        # 终态销账；销账失败也只多一条 pending 记录，副本仍被这道闸
                        # 拦住（凭据在本进程的投递历史里）。
                        logger.warning(
                            "qq-official suppressed replayed send for outbox {} (previous attempt unconfirmed, seq {})",
                            outbox_id,
                            seq,
                        )
                        try:
                            await client.ack_outbox(
                                session_id, outbox_id, status="unconfirmed"
                            )
                        except Exception as exc:  # noqa: BLE001 - 销账失败不影响本条已判定
                            logger.warning(
                                "qq-official unconfirmed ack failed for {}: {}",
                                outbox_id,
                                exc,
                            )
                        seqs[session_id] = seq
                        backoff = _PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS
                        continue
                    if outbox_id and int(outbox_attempts.get(outbox_id, 0)) >= _PUMP_DELIVER_MAX_ATTEMPTS:
                        # per-seq 预算已被之前的副本耗尽：同 id 的后续副本零尝试
                        # 丢弃，防毒消息随每份新 seq 重新获得完整重试预算。
                        logger.error(
                            "qq-official dropping duplicate copy of undeliverable outbox {} for session {} (seq {})",
                            outbox_id,
                            session_id,
                            seq,
                        )
                        seqs[session_id] = seq
                        backoff = _PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS
                        continue
                    try:
                        await deliver(target_key, text, attachments)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - retry via reconnect replay
                        failure_kind = str(getattr(exc, "kind", "") or "transient")
                        deterministic = failure_kind in _NON_RETRYABLE_SEND_KINDS
                        if outbox_id:
                            _lru_remember(
                                outbox_attempts,
                                outbox_id,
                                int(outbox_attempts.get(outbox_id, 0)) + 1,
                            )
                            if isinstance(exc, DeliveryUnconfirmedError):
                                _lru_remember(unconfirmed_outbox_ids, outbox_id)
                        failed_attempts = (
                            1
                            if deterministic
                            else (failed_attempts + 1 if seq == failed_seq else 1)
                        )
                        failed_seq = seq
                        logger.exception(
                            "qq-official deliver failed for session {} seq {} (attempt {}/{}, kind={})",
                            session_id,
                            seq,
                            failed_attempts,
                            _PUMP_DELIVER_MAX_ATTEMPTS,
                            failure_kind,
                        )
                        if not deterministic and failed_attempts < _PUMP_DELIVER_MAX_ATTEMPTS:
                            # 重连后由服务端按 last_seq 重放本条；外层循环负责记一条
                            # `event pump error ... reconnecting` 与退避。
                            raise
                        if deterministic:
                            logger.error(
                                "qq-official dropping undeliverable event seq {} for session {} (kind={}, no retry)",
                                seq,
                                session_id,
                                failure_kind,
                            )
                        else:
                            logger.error(
                                "qq-official dropping undeliverable event seq {} for session {} after {} attempts",
                                seq,
                                session_id,
                                failed_attempts,
                            )
                        if outbox_id:
                            # 账本不再有年龄上限，投递预算就是它唯一的上界：判毒后
                            # 按终态销账。不销的话，一次进程重启（per-id 计数随进程
                            # 消失，实测网关每小时那次重登已不再清它）就让一个平台
                            # 永久拒绝的目标重新拿到 5 次预算。
                            try:
                                await client.ack_outbox(
                                    session_id, outbox_id, status="undeliverable"
                                )
                            except Exception as exc:  # noqa: BLE001 - 销账失败只多几份副本
                                logger.warning(
                                    "qq-official undeliverable ack failed for {}: {}",
                                    outbox_id,
                                    exc,
                                )
                            else:
                                _lru_remember(acked_outbox_ids, outbox_id)
                        seqs[session_id] = seq
                        failed_seq, failed_attempts = 0, 0
                        backoff = _PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS
                        continue
                    seqs[session_id] = seq
                    failed_seq, failed_attempts = 0, 0
                    backoff = _PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS
                    if outbox_id:
                        try:
                            await client.ack_outbox(session_id, outbox_id)
                        except Exception as exc:  # noqa: BLE001 - ack 失败不影响本条已送达
                            # 代价是下次重启后本条可能重复投递一次（at-least-once）。
                            logger.warning(
                                "qq-official outbox ack failed for {} ({}); may redeliver after restart",
                                outbox_id,
                                exc,
                            )
                        else:
                            # 仅 ack 成功才记入去重集：服务端仍视为 pending 而
                            # 对账重放时，本桥进程内跳过重复副本；ack 失败不入
                            # 集，副本可再投（保持 at-least-once）。
                            _lru_remember(acked_outbox_ids, outbox_id)
                # SSE 流干净结束（服务端重启/空闲关闭）：同样必须重连续拉。
                logger.warning(
                    "qq-official event stream ended for session {}; reconnecting",
                    session_id,
                )
            except asyncio.CancelledError:
                raise
            except httpx.TimeoutException as exc:  # noqa: BLE001 - idle stream is routine, replay covers it
                # 读超时只代表这条流安静得过头（事件循环被大会话转录重写占住、代理掐线）：
                # 重连按 last_seq 重放积压，不丢投递。真故障仍走下面的 exception 栈。
                logger.warning(
                    "qq-official event stream idle timeout for session {} ({}); reconnecting in {:.0f}s",
                    session_id,
                    type(exc).__name__,
                    backoff,
                )
            except Exception:  # noqa: BLE001 - reconnect loop keeps the pump alive
                logger.exception(
                    "qq-official event pump error for session {}; reconnecting in {:.0f}s",
                    session_id,
                    backoff,
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, _PUMP_RECONNECT_MAX_BACKOFF_SECONDS)

    def _note_pump_exit(session_id: str, task: asyncio.Task) -> None:
        """pump 的死因必须留痕：实测它能在打完一条 `pump connecting` 之后静默结束，
        当时只能靠数 18790 的 ESTABLISHED 连接才判断出来。正是这条回执点名了真凶——
        入站消息建好 pump 0.3 秒后出现的 ``cancelled`` 来自桥自身被重启，而不是任务
        建在了别的 loop 上（见 service.py ``stop()`` 不清签名的理由）。
        """
        if task.cancelled():
            logger.warning("qq-official pump cancelled for session {}", session_id)
            return
        error = None
        try:
            error = task.exception()
        except Exception:  # noqa: BLE001 - done callback must never raise
            pass
        if error is not None:
            logger.warning("qq-official pump exited with error for session {}: {}", session_id, error)

    def _spawn_pump(session_id: str, external_key: str) -> bool:
        """Spawn the session pump if absent/dead; True when a new task was created."""
        existing = pump_tasks.get(session_id)
        if existing is not None and not existing.done():
            return False
        task = asyncio.create_task(_pump(session_id, external_key), name=f"qq-official-pump:{session_id}")
        pump_tasks[session_id] = task
        pumps.add(task)
        task.add_done_callback(pumps.discard)
        task.add_done_callback(lambda finished: _note_pump_exit(session_id, finished))
        return True

    async def _reconcile_pending_pumps() -> None:
        """按持久 outbox 的 pending 清单为无存活 pump 的会话建 pump。

        启动时跑一次（进程重启后 sessions 映射清空，pump 只等下一条入站消息才
        建；在那之前服务端启动重放进 hub 的滞留推送没有消费者），随后由常驻
        循环周期调用：服务端重启窗口里账本可能为空导致启动首跑扑空，滞留推送
        稍后才入账（2026-09-14 日报事故）。list 失败只记 warning——对账是兜底
        路径，绝不能让循环或桥启动因它而死。
        """
        try:
            pending = await client.list_pending_outbox()
        except Exception as exc:  # noqa: BLE001 - 对账失败不阻断桥运行
            logger.warning("qq-official pending outbox reconcile skipped: {}", exc)
            return
        spawned = 0
        for item in pending:
            pending_session = str(item.get("session_id") or "").strip()
            pending_key = str(item.get("external_key") or "").strip()
            if not pending_session or not pending_key:
                continue
            sessions.setdefault(pending_key, pending_session)
            if _spawn_pump(pending_session, pending_key):
                spawned += 1
        if spawned:
            # 只计真正新建的 pump：常驻对账每 30s 跑一次，幂等重建不得刷假日志。
            logger.info("qq-official spawned {} pump(s) from pending outbox entries", spawned)

    async def _pending_reconcile_loop() -> None:
        while True:
            await asyncio.sleep(_PENDING_RECONCILE_INTERVAL_SECONDS)
            try:
                await _reconcile_pending_pumps()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconcile loop must survive
                logger.exception("qq-official pending outbox reconcile pass failed")

    class QqOfficialClient(botpy.Client):
        async def on_ready(self):
            on_state("connected", "")

        async def _dispatch(self, external_key: str, message):
            attachments, voice_lines = await _collect_attachments(media_client, message)
            await on_incoming(
                external_key,
                _content_of(message),
                getattr(message, "id", ""),
                attachments,
                voice_lines,
                [
                    str(getattr(item, "filename", "") or "").strip()
                    for item in (getattr(message, "attachments", None) or [])
                ],
            )

        async def on_group_at_message_create(self, message):
            await self._dispatch(
                external_key_for_group(getattr(message, "group_openid", "")), message
            )

        async def on_c2c_message_create(self, message):
            await self._dispatch(external_key_for_c2c(_openid_of(message)), message)

        async def on_at_message_create(self, message):
            await self._dispatch(
                external_key_for_guild(
                    getattr(message, "guild_id", ""), getattr(message, "channel_id", "")
                ),
                message,
            )

        async def on_direct_message_create(self, message):
            await self._dispatch(
                external_key_for_guild_dm(
                    getattr(message, "guild_id", ""),
                    getattr(getattr(message, "author", None), "id", ""),
                ),
                message,
            )

    def _content_of(message: Any) -> str:
        content = getattr(message, "content", "")
        return str(content or "").strip()

    def _openid_of(message: Any) -> str:
        author = getattr(message, "author", None) or {}
        return str(getattr(author, "user_openid", "") or getattr(author, "id", "") or "").strip()

    bridge_api: Any = None
    reconcile_task: asyncio.Task | None = None

    try:
        bridge_client = QqOfficialClient(intents=intents, is_sandbox=sandbox, ext_handlers=False)
        bridge_api = getattr(bridge_client, "api", None)
        await _reconcile_pending_pumps()  # 启动首跑（原预热），失败不阻断启动
        # 常驻对账在进入 botpy 网关前拉起：登录期间也在轮询，「服务端晚于桥
        # 就绪/首跑扑空」的窗口由 30s 后的下一轮自动补齐。
        reconcile_task = asyncio.create_task(
            _pending_reconcile_loop(), name="qq-official-pending-reconcile"
        )
        on_state("connecting", "waiting for QQ gateway")
        # NOTE: botpy's ``Client.run()`` is blocking (``run_until_complete`` on the
        # loop captured at construction) and raises "This event loop is already
        # running" when awaited inside the web runtime's live loop. The async
        # entry below keeps botpy on the same loop as uvicorn.
        async with bridge_client:
            # ``ret_coro=True`` 不是可选的：botpy 的 ``_pool_init`` 用
            # ``while not self._closed: await multi_run()`` 驱动，而 ``multi_run``
            # 第一次就把它自己的 ``_session_list`` 原地 pop 空，之后每次返回的都是
            # 一个从不挂起的协程 —— 那个 while 就成了持有 GIL 的忙等，实测把 web 的
            # 事件循环整钉 20 分钟（单核 92%，连 /api/bootstrap/exit 都无人处理）。
            # 所以一份会话列表只跑一次，跑完就交给 ``service._run`` 的退避重连，
            # 由它重新登录换一份新列表。
            gateway = await bridge_client.start(appid=app_id, secret=app_secret, ret_coro=True)
            await gateway
            raise QqGatewaySessionEndedError("QQ 网关会话全部结束，需重新登录")
    finally:
        # 对账循环是本模块定义的普通任务，_is_botpy_task 收割器认不出它，
        # 必须显式取消。
        if reconcile_task is not None:
            reconcile_task.cancel()
        for pump in list(pumps):
            pump.cancel()
        # botpy's websocket/heartbeat/runner tasks survive the cancellation of
        # ``start()`` (its ``asyncio.wait`` does not cancel its children, and the
        # ws rides a dedicated aiohttp session that ``Client.close()`` cannot
        # reach) — reap them here so a stop/restart really drops the connection.
        leftovers = [task for task in asyncio.all_tasks(asyncio.get_running_loop()) if _is_botpy_task(task)]
        for task in leftovers:
            task.cancel()
        reap = list(pumps) + leftovers + ([reconcile_task] if reconcile_task is not None else [])
        if reap:
            await asyncio.gather(*reap, return_exceptions=True)
        await media_client.aclose()
        await client.close()
