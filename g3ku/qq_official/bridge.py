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
so the bridge issues the request directly with the SDK's own route/http pair
(see docs/architecture/external-agent-api.md「内置官方 QQ 适配器」).
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
from collections import OrderedDict
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from loguru import logger

from g3ku.qq_official.client import ExternalApiClient
from g3ku.qq_official.messages import (
    OUTBOUND_EVENT,
    external_key_for_c2c,
    external_key_for_group,
    external_key_for_guild,
    external_key_for_guild_dm,
    idempotency_key_for,
    is_deliverable_event,
    parse_external_key,
)

StateCallback = Callable[[str, str], None]

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

# 服务端排队回执兜底文案（正常取 /api/v1 响应里的 receipt）。
_QUEUED_RECEIPT_FALLBACK_TEXT = "收到，将在当前任务中一并处理。"

# 语音转写进正文时带来源标记：不带标记，听错的语音和手打文字在模型眼里无法区分，
# 它就没办法说"你刚发的语音里那句我没听清"。识别失败另起一行措辞——把失败也写成
# "机器识别结果：" 等于让模型把一句失败说明当成用户说的话。
_VOICE_TRANSCRIPT_PREFIX = "用户语音，机器识别结果："
_VOICE_FAILURE_PREFIX = "用户语音，机器识别失败："

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

# 桥侧周期 pending 对账：GET /outbox/pending 不再是启动一次性动作。服务端重启
# 落在 cron 触发与产出之间时，启动预热会扑空（账本尚空），滞留推送稍后才入账
# ——2026-09-14 23:00 定时日报正是这样丢掉的。周期对账为「有 pending 记录且无
# 存活 pump」的会话重建 pump，让 SSE 重放把账补齐。
_PENDING_RECONCILE_INTERVAL_SECONDS = 30.0
# outbox_id 维度投递历史（有界 LRU）：acked 集防服务端对账/重放副本重复投递；
# attempts 计数跨 seq 封顶同一 id 的总尝试次数（重放副本 seq 不同，per-seq
# failed_attempts 会对每个副本重新计数）。
_PUMP_OUTBOX_HISTORY_MAX_IDS = 1024


def _lru_remember(mapping: "OrderedDict[str, Any]", key: str, value: Any = None, *, limit: int = _PUMP_OUTBOX_HISTORY_MAX_IDS) -> None:
    """Bounded LRU write: remember ``key`` and evict the oldest beyond limit."""
    mapping[key] = value
    mapping.move_to_end(key)
    while len(mapping) > max(1, int(limit)):
        mapping.popitem(last=False)


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
    text-only delivery.

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
        is_image = content_type.startswith("image/")
        is_audio = content_type.startswith("audio/")
        if not content_type:
            name = str(getattr(item, "filename", "") or "").strip()
            content_type = str(mimetypes.guess_type(name)[0] or "").strip().lower()
            is_image = content_type.startswith("image/")
            is_audio = content_type.startswith("audio/")
        max_bytes = _MAX_INBOUND_IMAGE_BYTES if is_image else _MAX_INBOUND_FILE_BYTES
        data = await _download_attachment_bytes(media_client, url, max_bytes=max_bytes)
        if not data:
            if is_audio and stt_ready:
                # 纯语音消息取不到字节时，正文与附件都是空的，on_incoming 会直接早退
                # ——用户端就是"发了东西然后什么都没有"。留下一行失败说明，回合照常提交。
                voice_lines.append(f"{_VOICE_FAILURE_PREFIX}语音附件下载失败")
            continue
        if is_audio and stt_ready:
            name = _attachment_name(item, content_type or "audio/wav")
            result = await stt_engine.transcribe_bytes(
                data, filename=name, mime_type=content_type, source="qq-voice"
            )
            if result.ok and result.text:
                voice_lines.append(f"{_VOICE_TRANSCRIPT_PREFIX}{result.text}")
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
        payloads.append(
            {
                "kind": "image" if is_image else "file",
                "name": _attachment_name(item, content_type or "application/octet-stream"),
                "mime_type": content_type or "application/octet-stream",
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
    # session bookkeeping: session_id -> external_key, and cached last SSE seq.
    sessions: dict[str, str] = {}
    seqs: dict[str, int] = {}
    pumps: set[asyncio.Task] = set()
    pump_tasks: dict[str, asyncio.Task] = {}
    # outbox_id 维度投递历史（进程内有界 LRU，重启清零 = at-least-once 重投一次）：
    # acked 集跳过对账/重放产生的已销账副本；attempts 跨 seq 封顶同一 id 的总
    # 投递尝试（毒消息副本不随每份新 seq 重新获得完整预算）。
    acked_outbox_ids: OrderedDict[str, None] = OrderedDict()
    outbox_attempts: OrderedDict[str, int] = OrderedDict()

    async def on_incoming(
        external_key: str,
        text: str,
        event_id: str,
        attachments: list[dict[str, str]] | None = None,
        voice_lines: list[str] | None = None,
    ) -> None:
        attachment_payloads = list(attachments or [])
        if voice_lines:
            # 语音转出的文字并入正文：QQ 语音往往是口语化的独立内容，并入而不是
            # 另起回合，用户才有"发了一段话 + 一段语音"的连贯语义。
            text = "\n".join([str(text or "").strip(), *[line for line in voice_lines if line]]).strip()
        if not text.strip() and not attachment_payloads:
            return
        session_id = sessions.get(external_key)
        if session_id is None:
            session_id = await client.ensure_session(external_key)
            sessions[external_key] = session_id
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
        服务端只监听回环地址时同样成立。故这里用与 botpy 内部完全相同的写法
        直接发请求，仅补上 SDK 未暴露的入参。
        """
        payload = {
            "file_type": file_type,
            "file_data": base64.b64encode(data).decode("ascii"),
            "srv_send_msg": False,
        }
        try:
            media = await bridge_api._http.request(_media_files_route(kind, target), json=payload)
        except Exception as exc:  # noqa: BLE001 - 回退 url 路径
            logger.warning(
                "qq-official file_data media upload failed ({}); falling back to url upload", exc
            )
            return ""
        return str(media.get("file_info") or "").strip() if isinstance(media, dict) else ""

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
        kind: str, target: dict[str, str], attachment: dict[str, Any]
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
                    result = await bridge_api.post_group_message(
                        group_openid=target["group_openid"],
                        msg_type=7,
                        media={"file_info": file_info},
                    )
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
            if await _deliver_attachment(kind, target, attachment):
                continue
            name = str(attachment.get("name") or "attachment")
            fallback_lines.append(f"📎 {name}: {_absolute_media_url(str(attachment.get('url') or ''))}")
        body = "\n".join(part for part in [str(text or "").strip(), *fallback_lines] if part).strip()
        if not body:
            return
        if kind == "group":
            result = await bridge_api.post_group_message(group_openid=target["group_openid"], content=body, msg_type=0)
        elif kind == "c2c":
            result = await bridge_api.post_c2c_message(openid=target["user_openid"], content=body, msg_type=0)
        elif kind == "guild":
            result = await bridge_api.post_message(channel_id=target["channel_id"], content=body)
        else:
            result = await bridge_api.post_dms(guild_id=target["guild_id"], content=body)
        # botpy 的 http 层对请求超时只打一条 WARNING 就返回 None（吞掉
        # asyncio.TimeoutError）：无回执必须视为投递失败抛出，交给 pump 按
        # 重放重试，而不是记一次假成功。
        if result is None:
            raise RuntimeError(
                f"qq-official delivery unconfirmed (empty API response) for target {external_key}"
            )
        message_id = ""
        if isinstance(result, dict):
            message_id = str(result.get("id") or result.get("message_id") or "").strip()
        # 送达回执：排查"发没发出去"时以这行为准（published to hub 不代表送达）。
        logger.info(
            "qq-official delivered {} message to {} (id={}, attachments={})",
            kind,
            external_key,
            message_id or "-",
            len([item for item in list(attachments or []) if isinstance(item, dict)]),
        )

    async def _pump(session_id: str, external_key: str) -> None:
        """Per-session SSE consumer with a real reconnect loop.

        投递语义：只有 deliver 成功（或事件被判定不可投递/毒消息达到重试上限）
        才推进 ``seqs``；失败时跳出重连，服务端按 last_seq 重放，本条自动重试，
        重试节奏随重连退避（1s→60s 封顶）递增。SSE 干净结束（服务端重启）同样
        重连，绝不静默终结任务。
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
                    outbox_id = ""
                    if event_type == OUTBOUND_EVENT:
                        target_key = str(event.get("external_key") or external_key)
                        outbox_id = str(event.get("outbox_id") or "").strip()
                    if outbox_id and outbox_id in acked_outbox_ids:
                        # 已送达并销账的 id：服务端对账/重放产生的副本直接跳过，
                        # 只推进 seq（否则周期对账会造成重复推送）。
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
                    except Exception:  # noqa: BLE001 - retry via reconnect replay
                        if outbox_id:
                            _lru_remember(
                                outbox_attempts,
                                outbox_id,
                                int(outbox_attempts.get(outbox_id, 0)) + 1,
                            )
                        failed_attempts = failed_attempts + 1 if seq == failed_seq else 1
                        failed_seq = seq
                        logger.exception(
                            "qq-official deliver failed for session {} seq {} (attempt {}/{})",
                            session_id,
                            seq,
                            failed_attempts,
                            _PUMP_DELIVER_MAX_ATTEMPTS,
                        )
                        if failed_attempts >= _PUMP_DELIVER_MAX_ATTEMPTS:
                            logger.error(
                                "qq-official dropping undeliverable event seq {} for session {} after {} attempts",
                                seq,
                                session_id,
                                failed_attempts,
                            )
                            seqs[session_id] = seq
                            failed_seq, failed_attempts = 0, 0
                            backoff = _PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS
                            continue
                        raise
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
            await bridge_client.start(appid=app_id, secret=app_secret)
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
