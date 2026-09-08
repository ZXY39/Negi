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
``filename`` — image payloads are downloaded and forwarded to ``/api/v1`` as
``data_base64`` attachments, see docs/architecture/external-agent-api.md
「回合契约」), and ``post_message`` / ``post_group_message`` /
``post_c2c_message`` / ``post_dms`` signatures. IMPORTANT: ``Client.run()`` is a blocking entry point
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
from typing import Any, Callable

import httpx
from loguru import logger

from g3ku.qq_official.client import ExternalApiClient
from g3ku.qq_official.messages import (
    OUTBOUND_EVENT,
    REPLY_DELTA_EVENT,
    REPLY_FINAL_EVENT,
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

# Mirrors the /api/v1 per-attachment cap (``WEB_CEO_IMAGE_UPLOAD_MAX_BYTES`` in
# ``g3ku/runtime/web_ceo_sessions.py``); anything larger is rejected server-side
# with 413, so oversized downloads are skipped here instead.
_MAX_INBOUND_ATTACHMENT_BYTES = 5 * 1024 * 1024
_MAX_INBOUND_IMAGE_ATTACHMENTS = 4
_MEDIA_DOWNLOAD_CHUNK_BYTES = 64 * 1024

# 服务端排队回执兜底文案（正常取 /api/v1 响应里的 receipt）。
_QUEUED_RECEIPT_FALLBACK_TEXT = "收到，将在当前任务中一并处理。"


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


async def _download_attachment_bytes(client: httpx.AsyncClient, url: str) -> bytes | None:
    """Fetch one attachment; ``None`` on transport errors, non-200, or when the
    stream exceeds the /api/v1 per-attachment cap."""
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
                if total > _MAX_INBOUND_ATTACHMENT_BYTES:
                    logger.warning(
                        "qq-official attachment exceeds {} bytes and was skipped: {}",
                        _MAX_INBOUND_ATTACHMENT_BYTES,
                        url,
                    )
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError as exc:
        logger.warning("qq-official attachment download failed for {}: {}", url, exc)
        return None


def _attachment_name(item: Any, content_type: str) -> str:
    name = str(getattr(item, "filename", "") or "").strip()
    if name:
        return name
    extension = mimetypes.guess_extension(content_type) or ".png"
    attachment_id = str(getattr(item, "id", "") or "").strip() or "image"
    return f"qq-{attachment_id}{extension}"


async def _collect_image_attachments(
    media_client: httpx.AsyncClient, message: Any
) -> list[dict[str, str]]:
    """Download the image attachments referenced by a botpy message and return
    them as ``/api/v1`` attachment payloads (inline ``data_base64``).

    botpy exposes ``message.attachments`` items with ``content_type`` / ``url``
    / ``filename`` / ``id``. Only ``image/*`` items with absolute http(s) URLs
    are forwarded; failures degrade to text-only delivery.
    """
    raw_items = list(getattr(message, "attachments", None) or [])
    if not raw_items:
        return []
    payloads: list[dict[str, str]] = []
    for item in raw_items:
        if len(payloads) >= _MAX_INBOUND_IMAGE_ATTACHMENTS:
            logger.warning(
                "qq-official message {} carries more than {} image attachments; extras skipped",
                getattr(message, "id", ""),
                _MAX_INBOUND_IMAGE_ATTACHMENTS,
            )
            break
        content_type = str(getattr(item, "content_type", "") or "").strip().lower()
        url = str(getattr(item, "url", "") or "").strip()
        if not content_type.startswith("image/") or not url.lower().startswith(("http://", "https://")):
            continue
        data = await _download_attachment_bytes(media_client, url)
        if not data:
            continue
        payloads.append(
            {
                "kind": "image",
                "name": _attachment_name(item, content_type),
                "mime_type": content_type,
                "data_base64": base64.b64encode(data).decode("ascii"),
            }
        )
    return payloads


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

    async def on_incoming(
        external_key: str,
        text: str,
        event_id: str,
        attachments: list[dict[str, str]] | None = None,
    ) -> None:
        attachment_payloads = list(attachments or [])
        if not text.strip() and not attachment_payloads:
            return
        session_id = sessions.get(external_key)
        if session_id is None:
            session_id = await client.ensure_session(external_key)
            sessions[external_key] = session_id
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

    async def deliver(external_key: str, text: str) -> None:
        kind, target = parse_external_key(external_key)
        if kind == "group":
            await bridge_api.post_group_message(group_openid=target["group_openid"], content=text, msg_type=0)
        elif kind == "c2c":
            await bridge_api.post_c2c_message(openid=target["user_openid"], content=text, msg_type=0)
        elif kind == "guild":
            await bridge_api.post_message(channel_id=target["channel_id"], content=text)
        elif kind == "guilddm":
            await bridge_api.post_dms(guild_id=target["guild_id"], content=text)
        else:
            logger.warning("qq-official cannot deliver to target external_key={}", external_key)

    async def _pump(session_id: str, external_key: str) -> None:
        seen = seqs.get(session_id, 0)
        try:
            async for event in client.stream_events(session_id, last_seq=seen):
                seqs[session_id] = int(event.get("seq") or seen)
                event_type = str(event.get("type") or "")
                if not is_deliverable_event(event_type):
                    continue
                text = str(event.get("text") or "").strip()
                if not text:
                    continue
                target_key = external_key
                if event_type == OUTBOUND_EVENT:
                    target_key = str(event.get("external_key") or external_key)
                await deliver(target_key, text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - reconnect loop keeps the pump alive
            logger.exception("qq-official event pump error for session {}", session_id)

    def _spawn_pump(session_id: str, external_key: str) -> None:
        task = asyncio.create_task(_pump(session_id, external_key), name=f"qq-official-pump:{session_id}")
        pumps.add(task)
        task.add_done_callback(pumps.discard)

    class QqOfficialClient(botpy.Client):
        async def on_ready(self):
            on_state("connected", "")

        async def on_group_at_message_create(self, message):
            await on_incoming(
                external_key_for_group(getattr(message, "group_openid", "")),
                _content_of(message),
                getattr(message, "id", ""),
                await _collect_image_attachments(media_client, message),
            )

        async def on_c2c_message_create(self, message):
            await on_incoming(
                external_key_for_c2c(_openid_of(message)),
                _content_of(message),
                getattr(message, "id", ""),
                await _collect_image_attachments(media_client, message),
            )

        async def on_at_message_create(self, message):
            await on_incoming(
                external_key_for_guild(getattr(message, "guild_id", ""), getattr(message, "channel_id", "")),
                _content_of(message),
                getattr(message, "id", ""),
                await _collect_image_attachments(media_client, message),
            )

        async def on_direct_message_create(self, message):
            await on_incoming(
                external_key_for_guild_dm(getattr(message, "guild_id", ""), getattr(getattr(message, "author", None), "id", "")),
                _content_of(message),
                getattr(message, "id", ""),
                await _collect_image_attachments(media_client, message),
            )

    def _content_of(message: Any) -> str:
        content = getattr(message, "content", "")
        return str(content or "").strip()

    def _openid_of(message: Any) -> str:
        author = getattr(message, "author", None) or {}
        return str(getattr(author, "user_openid", "") or getattr(author, "id", "") or "").strip()

    bridge_api: Any = None

    try:
        bridge_client = QqOfficialClient(intents=intents, is_sandbox=sandbox, ext_handlers=False)
        bridge_api = getattr(bridge_client, "api", None)
        on_state("connecting", "waiting for QQ gateway")
        # NOTE: botpy's ``Client.run()`` is blocking (``run_until_complete`` on the
        # loop captured at construction) and raises "This event loop is already
        # running" when awaited inside the web runtime's live loop. The async
        # entry below keeps botpy on the same loop as uvicorn.
        async with bridge_client:
            await bridge_client.start(appid=app_id, secret=app_secret)
    finally:
        for pump in list(pumps):
            pump.cancel()
        # botpy's websocket/heartbeat/runner tasks survive the cancellation of
        # ``start()`` (its ``asyncio.wait`` does not cancel its children, and the
        # ws rides a dedicated aiohttp session that ``Client.close()`` cannot
        # reach) — reap them here so a stop/restart really drops the connection.
        leftovers = [task for task in asyncio.all_tasks(asyncio.get_running_loop()) if _is_botpy_task(task)]
        for task in leftovers:
            task.cancel()
        pending = list(pumps) + leftovers
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await media_client.aclose()
        await client.close()