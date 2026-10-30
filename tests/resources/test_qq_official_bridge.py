"""Bridge wiring tests with a fake ``botpy`` module and a fake /api/v1 client.

The real botpy ``Client.run()`` is a blocking entry point that cannot run
inside the web runtime's live event loop; the bridge must use the async entry
(``async with client: await client.start(...)``). These fakes expose exactly
that surface, so any regression back to ``run()`` fails here with an
AttributeError instead of at runtime against the live instance.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import types
from collections import OrderedDict
from contextlib import suppress
from types import SimpleNamespace

import httpx
import pytest

from g3ku.qq_official import bridge as bridge_module


class FakeRoute:
    """botpy ``Route`` 替身：域名、路径参数与沙盒切换按真实 Route 的算法走。"""

    DOMAIN = "api.sgroup.qq.com"
    SANDBOX_DOMAIN = "sandbox.api.sgroup.qq.com"

    def __init__(self, method: str, path: str, **params) -> None:
        self.method = method
        self.path = path
        self.params = params
        self.is_sandbox = False

    @property
    def url(self) -> str:
        domain = self.SANDBOX_DOMAIN if self.is_sandbox else self.DOMAIN
        return f"https://{domain}{self.path.format(**self.params)}"


class FakeBotHttp:
    def __init__(self, api: "FakeBotApi", is_sandbox: bool = False) -> None:
        self._api = api
        self.is_sandbox = is_sandbox
        self._headers = {"Authorization": "QQBot fake-access-token", "X-Union-Appid": "100"}

    async def check_session(self):
        """真实实现续期 access_token、重建 ``_headers``，顺带拉起 SDK 自己的 session。"""
        self._api.check_session_calls += 1

    async def request(self, route: FakeRoute, **kwargs):
        # 出站媒体上传不走这条道（SDK 的 ``Client(timeout=5)`` 套在每一个 API 请求
        # 上，超时还只打一条 WARNING 就返回 None）。这里留作记录器：车道被搬回 SDK
        # 就会在 ``http_calls`` 上被断言抓到。
        self._api.http_calls.append(
            (route.path, {"method": route.method, "params": route.params, **kwargs})
        )
        return {"file_uuid": "uuid-fd", "file_info": "file-info-fd", "ttl": 0}


def _outbound_text(kwargs: dict) -> str:
    """从两种出站形状里取正文：纯文本走 ``content``，markdown 走 ``markdown.content``。"""
    markdown = kwargs.get("markdown")
    if isinstance(markdown, dict):
        return str(markdown.get("content") or "")
    return str(kwargs.get("content") or "")


class FakeBotApi:
    def __init__(self, is_sandbox: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.http_calls: list[tuple[str, dict]] = []
        self.check_session_calls = 0
        self._http = FakeBotHttp(self, is_sandbox=is_sandbox)

    # 真实 botpy 成功时返回响应 JSON、超时静默返回 None；fake 默认按成功返回
    # 回执 dict（deliver() 会把 None 判为投递失败）。
    async def post_message(self, **kwargs):
        self.calls.append(("post_message", kwargs))
        return {"id": "fake-message-id"}

    async def post_group_message(self, **kwargs):
        self.calls.append(("post_group_message", kwargs))
        return {"id": "fake-message-id"}

    async def post_c2c_message(self, **kwargs):
        self.calls.append(("post_c2c_message", kwargs))
        return {"id": "fake-message-id"}

    async def post_dms(self, **kwargs):
        self.calls.append(("post_dms", kwargs))
        return {"id": "fake-message-id"}

    async def post_group_file(self, **kwargs):
        self.calls.append(("post_group_file", kwargs))
        return {"file_uuid": "uuid-g", "file_info": "file-info-g", "ttl": 0}

    async def post_c2c_file(self, **kwargs):
        self.calls.append(("post_c2c_file", kwargs))
        return {"file_uuid": "uuid-c", "file_info": "file-info-c", "ttl": 0}


class FakeClient:
    """botpy.Client stand-in exposing only the async entry the bridge may use."""

    instances: list["FakeClient"] = []

    def __init__(self, intents=None, is_sandbox=False, ext_handlers=True, **kwargs) -> None:
        self.intents = intents
        self.is_sandbox = is_sandbox
        self.ext_handlers = ext_handlers
        self.init_kwargs = dict(kwargs)
        self.api = FakeBotApi(is_sandbox=is_sandbox)
        self.started = False
        self.entered = False
        FakeClient.instances.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def start(self, appid, secret, ret_coro=False):
        self.started = True
        await asyncio.Event().wait()  # block until cancelled, like the real ws loop


class FakeIntents:
    def __init__(self, **kwargs) -> None:
        self.value = len(kwargs)


class FakeExternalApiClient:
    instances: list["FakeExternalApiClient"] = []

    def __init__(self, base_url: str, token: str, transport=None) -> None:
        self.base_url = base_url
        self.token = token
        self.ensured: list[str] = []
        self.ensured_chat_types: list[str | None] = []
        self.sent: list[tuple[str, str, str, list]] = []
        self.sent_sender: list[dict] = []
        self.sent_metadata: list[dict] = []
        self.events: asyncio.Queue = asyncio.Queue()
        self.acked: list[tuple[str, str]] = []
        self.ack_status_by_id: dict[str, str] = {}
        self.ack_extra_by_id: dict[str, dict] = {}
        self.pending_outbox: list[dict] = []
        self.closed = False
        FakeExternalApiClient.instances.append(self)

    async def ensure_session(
        self,
        external_key: str,
        *,
        title: str | None = None,
        chat_type: str | None = None,
    ) -> str:
        _ = title
        self.ensured.append(external_key)
        self.ensured_chat_types.append(chat_type)
        return f"ext:qq-official:{external_key}"

    async def send_message(
        self,
        session_id: str,
        text: str,
        *,
        idempotency_key: str | None = None,
        attachments: list | None = None,
        sender: dict | None = None,
        metadata: dict | None = None,
    ) -> dict:
        self.sent.append((session_id, text, idempotency_key or "", list(attachments or [])))
        self.sent_sender.append(dict(sender or {}))
        self.sent_metadata.append(dict(metadata or {}))
        return {"ok": True, "turn_id": "t1"}

    async def list_pending_outbox(self) -> list[dict]:
        return list(self.pending_outbox)

    async def ack_outbox(self, session_id: str, outbox_id: str, *, status: str = "delivered", **extra) -> None:
        self.acked.append((session_id, outbox_id))
        self.ack_status_by_id[outbox_id] = status
        self.ack_extra_by_id[outbox_id] = dict(extra)

    async def stream_events(self, session_id: str, last_seq: int = 0):
        while True:
            event = await self.events.get()
            yield event

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_registries():
    FakeClient.instances.clear()
    FakeExternalApiClient.instances.clear()
    bridge_module.reset_bridge_delivery_state()
    yield
    FakeClient.instances.clear()
    FakeExternalApiClient.instances.clear()
    bridge_module.reset_bridge_delivery_state()


def _install_fake_botpy(
    monkeypatch: pytest.MonkeyPatch, intents_cls=FakeIntents, client_cls=FakeClient
) -> None:
    fake_http = types.ModuleType("botpy.http")
    fake_http.Route = FakeRoute
    fake_botpy = types.ModuleType("botpy")
    fake_botpy.Client = client_cls
    fake_botpy.Intents = intents_cls
    fake_botpy.http = fake_http
    monkeypatch.setitem(sys.modules, "botpy.http", fake_http)
    monkeypatch.setitem(sys.modules, "botpy", fake_botpy)


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for bridge condition")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_bridge_uses_async_entry_and_wires_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_botpy(monkeypatch)
    monkeypatch.setattr(bridge_module, "ExternalApiClient", FakeExternalApiClient)

    states: list[tuple[str, str]] = []
    task = asyncio.create_task(
        bridge_module.run_qq_official_bridge(
            app_id="100",
            app_secret="sekrit",
            sandbox=False,
            token="t",
            base_url="http://127.0.0.1:1/api/v1",
            on_state=lambda state, detail: states.append((state, detail)),
        ),
        name="test-qq-bridge",
    )
    try:
        await _wait_until(lambda: FakeClient.instances and FakeClient.instances[-1].started)
        client = FakeClient.instances[-1]
        assert client.entered is True
        assert ("connecting", "waiting for QQ gateway") in states

        await client.on_ready()
        assert ("connected", "") in states

        # Incoming group @-message → /api/v1 session + message with idempotency key.
        message = SimpleNamespace(group_openid="g1", content="@bot hi", id="m1")
        await client.on_group_at_message_create(message)
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        assert ext.ensured == ["qq:group:g1"]
        assert ext.sent == [("ext:qq-official:qq:group:g1", "@bot hi", "qq-m1", [])]

        # Proactive outbound push (cron reminder) → QQ c2c delivery.
        await ext.events.put(
            {"type": "outbound.created", "seq": 1, "text": "提醒", "external_key": "qq:c2c:u1"}
        )
        await _wait_until(lambda: client.api.calls)
        assert client.api.calls == [
            ("post_c2c_message", {"openid": "u1", "markdown": {"content": "提醒"}, "msg_type": 2})
        ]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert FakeExternalApiClient.instances[-1].closed is True


@pytest.mark.asyncio
async def test_bridge_raises_the_shared_http_timeout_above_the_sdk_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """botpy 那枚 timeout 是全桥共用的一把，默认 5 秒，且超时只打一条 SDK WARNING 就
    返回 None。实测群聊一发被动回复的 POST 会跑过 5 秒：平台收了、用户看得见，桥却只能
    记一次"无回执"，而出站幂等闸门为了不发第二份会把它按 unconfirmed 销账——一份已经生成
    好的回复就此变成"送达与否无从判断"。所以构造桥必须显式抬高这把，不能用 SDK 默认。"""
    _install_fake_botpy(monkeypatch)
    monkeypatch.setattr(bridge_module, "ExternalApiClient", FakeExternalApiClient)

    task = asyncio.create_task(
        bridge_module.run_qq_official_bridge(
            app_id="100",
            app_secret="sekrit",
            sandbox=False,
            token="t",
            base_url="http://127.0.0.1:1/api/v1",
            on_state=lambda state, detail: None,
        ),
        name="test-qq-bridge-timeout",
    )
    try:
        await _wait_until(lambda: FakeClient.instances and FakeClient.instances[-1].started)
        client = FakeClient.instances[-1]
        assert client.init_kwargs.get("timeout") == bridge_module._BRIDGE_HTTP_TIMEOUT_SECONDS
        assert bridge_module._BRIDGE_HTTP_TIMEOUT_SECONDS > 5
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_bridge_cancels_botpy_leftover_tasks_on_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_botpy(monkeypatch)
    monkeypatch.setattr(bridge_module, "ExternalApiClient", FakeExternalApiClient)

    async def fake_ws_loop() -> None:
        await asyncio.sleep(3600)

    fake_ws_loop.__qualname__ = "BotWebSocket.ws_connect"  # emulate a botpy receive loop

    leftover = asyncio.create_task(fake_ws_loop(), name="botpy-leftover")
    handler_task = asyncio.create_task(fake_ws_loop(), name="[botpy] on_ready")

    task = asyncio.create_task(
        bridge_module.run_qq_official_bridge(
            app_id="100",
            app_secret="sekrit",
            sandbox=False,
            token="t",
            base_url="http://127.0.0.1:1/api/v1",
            on_state=lambda state, detail: None,
        ),
        name="test-qq-bridge",
    )
    await _wait_until(lambda: FakeClient.instances and FakeClient.instances[-1].started)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert leftover.cancelled()
    assert handler_task.cancelled()


@pytest.mark.asyncio
async def test_bridge_reports_incompatible_intents(monkeypatch: pytest.MonkeyPatch) -> None:
    class BadIntents:
        def __init__(self, **kwargs) -> None:
            raise TypeError("flag unsupported")

    _install_fake_botpy(monkeypatch, intents_cls=BadIntents)
    monkeypatch.setattr(bridge_module, "ExternalApiClient", FakeExternalApiClient)

    states: list[tuple[str, str]] = []
    await bridge_module.run_qq_official_bridge(
        app_id="100",
        app_secret="sekrit",
        sandbox=False,
        token="t",
        base_url="http://127.0.0.1:1/api/v1",
        on_state=lambda state, detail: states.append((state, detail)),
    )
    assert states == [("error", "botpy Intents 与当前 qq-botpy 版本不兼容: flag unsupported")]
    assert FakeExternalApiClient.instances == []


def _media_transport(
    status_by_url: dict[str, tuple[int, bytes]], records: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if records is not None:
            records.append(request)
        status, body = status_by_url.get(str(request.url), (404, b""))
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


def _upload_reply(file_info: str = "file-info-fd") -> bytes:
    return f'{{"file_uuid":"uuid-1","file_info":"{file_info}","ttl":8200}}'.encode()


def _api_uploads(records: list[httpx.Request]) -> list[httpx.Request]:
    """媒体 client 上打到平台 API 的那些请求（上传车道），排除本机签名 URL 的下载。"""
    return [request for request in records if "sgroup.qq.com" in str(request.url)]


def _c2c_message(content: str, attachments: list | None = None, message_id: str = "m2") -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        id=message_id,
        author=SimpleNamespace(user_openid="u9"),
        attachments=attachments or [],
    )


def _image_attachment(url: str = "https://cdn.example/img.png", **overrides) -> SimpleNamespace:
    fields = {
        "content_type": "image/png",
        "url": url,
        "filename": "img.png",
        "id": "att-1",
        "size": None,
        "height": None,
        "width": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


async def _start_bridge(
    monkeypatch: pytest.MonkeyPatch,
    media_transport: httpx.MockTransport,
    client_cls: type[FakeExternalApiClient] = FakeExternalApiClient,
    sandbox: bool = False,
):
    _install_fake_botpy(monkeypatch)
    monkeypatch.setattr(bridge_module, "ExternalApiClient", client_cls)
    monkeypatch.setattr(
        bridge_module,
        "_create_media_client",
        lambda: httpx.AsyncClient(transport=media_transport),
    )
    states: list[tuple[str, str]] = []
    task = asyncio.create_task(
        bridge_module.run_qq_official_bridge(
            app_id="100",
            app_secret="sekrit",
            sandbox=sandbox,
            token="t",
            base_url="http://127.0.0.1:1/api/v1",
            on_state=lambda state, detail: states.append((state, detail)),
        ),
        name="test-qq-bridge",
    )
    await _wait_until(lambda: FakeClient.instances and FakeClient.instances[-1].started)
    return task, FakeClient.instances[-1]


@pytest.mark.asyncio
async def test_bridge_forwards_image_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
    media = _media_transport({"https://cdn.example/img.png": (200, b"img-bytes")})
    task, client = await _start_bridge(monkeypatch, media)
    try:
        message = _c2c_message("这是谁", [_image_attachment()])
        await client.on_c2c_message_create(message)
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        session_id, text, idem, attachments = ext.sent[0]
        assert (session_id, text, idem) == ("ext:qq-official:qq:c2c:u9", "这是谁", "qq-m2")
        assert attachments == [
            {
                "kind": "image",
                "name": "img.png",
                "mime_type": "image/png",
                "data_base64": base64.b64encode(b"img-bytes").decode("ascii"),
            }
        ]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_bridge_forwards_image_only_message(monkeypatch: pytest.MonkeyPatch) -> None:
    media = _media_transport({"https://cdn.example/img.png": (200, b"img-bytes")})
    task, client = await _start_bridge(monkeypatch, media)
    try:
        # QQ image-only messages carry empty content; they must not be dropped.
        message = _c2c_message("   ", [_image_attachment()])
        await client.on_c2c_message_create(message)
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        session_id, text, idem, attachments = ext.sent[0]
        assert (session_id, text, idem) == ("ext:qq-official:qq:c2c:u9", "", "qq-m2")
        assert len(attachments) == 1
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_bridge_names_an_unreachable_attachment_instead_of_vanishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯图片消息在图片取不到时仍要成回合，并点名丢了哪一张。

    QQ 的媒体 URL 带签名且会过期；此前非语音附件的失败出口什么都不留，正文与附件
    同时为空就被 on_incoming 的早退整条丢掉——用户发了张图，什么反馈都没有。
    """
    media = _media_transport({})  # 任何 URL 一律 404
    task, client = await _start_bridge(monkeypatch, media)
    try:
        message = _c2c_message("   ", [_image_attachment()])
        await client.on_c2c_message_create(message)
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        _session_id, text, _idem, attachments = ext.sent[0]
        assert not attachments
        assert "img.png" in text
        assert "附件" in text
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_bridge_drops_empty_message_without_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
    media = _media_transport({})
    task, client = await _start_bridge(monkeypatch, media)
    try:
        await client.on_c2c_message_create(_c2c_message("  ", []))
        await asyncio.sleep(0.05)
        ext = FakeExternalApiClient.instances[-1]
        assert ext.sent == []
        assert ext.ensured == []
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_bridge_skips_relative_and_undownloadable_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
    media = _media_transport({})
    task, client = await _start_bridge(monkeypatch, media)
    try:
        message = _c2c_message(
            "看下这个",
            [
                _image_attachment(content_type="application/octet-stream"),
                _image_attachment(url="/v2/relative-path.png"),
            ],
        )
        await client.on_c2c_message_create(message)
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        assert ext.sent[0][3] == []  # nothing downloadable, text still delivered
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_bridge_forwards_non_image_attachments_as_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """入站文档附件（非 image/* content_type）必须下载并以 kind=file 转发，
    而不是被白名单静默丢弃（本次重建前的行为）。"""
    media = _media_transport({"https://cdn.example/doc.pdf": (200, b"pdf-bytes")})
    task, client = await _start_bridge(monkeypatch, media)
    try:
        message = _c2c_message(
            "帮我看这个",
            [_image_attachment(url="https://cdn.example/doc.pdf", content_type="application/pdf", filename="doc.pdf", id="att-f")],
        )
        await client.on_c2c_message_create(message)
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        session_id, text, idem, attachments = ext.sent[0]
        assert (session_id, text, idem) == ("ext:qq-official:qq:c2c:u9", "帮我看这个", "qq-m2")
        assert attachments == [
            {
                "kind": "file",
                "name": "doc.pdf",
                "mime_type": "application/pdf",
                "data_base64": base64.b64encode(b"pdf-bytes").decode("ascii"),
            }
        ]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_bridge_degrades_to_text_on_download_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    media = _media_transport({"https://cdn.example/img.png": (500, b"")})
    task, client = await _start_bridge(monkeypatch, media)
    try:
        message = _c2c_message("这是谁", [_image_attachment()])
        await client.on_c2c_message_create(message)
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        session_id, text, idem, attachments = ext.sent[0]
        assert (session_id, text, idem) == ("ext:qq-official:qq:c2c:u9", "这是谁", "qq-m2")
        assert attachments == []
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_bridge_skips_oversized_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    oversized = b"x" * (bridge_module._MAX_INBOUND_IMAGE_BYTES + 1)
    media = _media_transport({"https://cdn.example/big.png": (200, oversized)})
    task, client = await _start_bridge(monkeypatch, media)
    try:
        message = _c2c_message("看看", [_image_attachment(url="https://cdn.example/big.png")])
        await client.on_c2c_message_create(message)
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        assert ext.sent[0][3] == []
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_bridge_caps_forwarded_attachment_count(monkeypatch: pytest.MonkeyPatch) -> None:
    urls = {f"https://cdn.example/img{i}.png": (200, f"b{i}".encode()) for i in range(6)}
    media = _media_transport(urls)
    task, client = await _start_bridge(monkeypatch, media)
    try:
        attachments = [_image_attachment(url=url, id=f"att-{i}") for i, url in enumerate(urls)]
        message = _c2c_message("多图", attachments)
        await client.on_c2c_message_create(message)
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        assert len(ext.sent[0][3]) == bridge_module._MAX_INBOUND_ATTACHMENTS
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_bridge_sends_no_idempotency_key_when_event_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺事件 id 时不得回退 external_key 当幂等键：external_key 对同一用户
    恒定，会撞掉该用户第一条消息的幂等位并永久丢弃后续消息（P2 根因之一）。"""
    media = _media_transport({})
    task, client = await _start_bridge(monkeypatch, media)
    try:
        message = _c2c_message("补发的消息", [], message_id="")
        await client.on_c2c_message_create(message)
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        session_id, text, idem, attachments = ext.sent[0]
        assert (session_id, text, attachments) == ("ext:qq-official:qq:c2c:u9", "补发的消息", [])
        assert not idem  # 无键提交，服务端按新消息处理
        assert idem != "qq:c2c:u9"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class _QueuedFakeExternalApiClient(FakeExternalApiClient):
    """Every submission reports the session busy so the bridge must surface the
    queued receipt to the QQ user instead of staying silent."""

    async def send_message(
        self,
        session_id: str,
        text: str,
        *,
        idempotency_key: str | None = None,
        attachments: list | None = None,
        sender: dict | None = None,
        metadata: dict | None = None,
    ) -> dict:
        _ = (sender, metadata)
        self.sent.append((session_id, text, idempotency_key or "", list(attachments or [])))
        if text == "有回执":
            return {"ok": True, "status": "queued", "receipt": "自定义排队回执", "turn_id": None}
        return {"ok": True, "status": "queued", "turn_id": None}


@pytest.mark.asyncio
async def test_bridge_delivers_queued_receipt_to_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """会话正忙、消息已排队时必须把回执送达用户：静默会让用户以为消息被吞
    而重复发送（P2）。响应缺 receipt 时用兜底文案。"""
    media = _media_transport({})
    task, client = await _start_bridge(monkeypatch, media, client_cls=_QueuedFakeExternalApiClient)
    try:
        await client.on_c2c_message_create(_c2c_message("有回执", [], message_id="q1"))
        await client.on_c2c_message_create(_c2c_message("无回执", [], message_id="q2"))
        await _wait_until(lambda: len(client.api.calls) >= 2)
        assert client.api.calls == [
            ("post_c2c_message", {"openid": "u9", "markdown": {"content": "自定义排队回执"}, "msg_type": 2}),
            (
                "post_c2c_message",
                {"openid": "u9", "markdown": {"content": bridge_module._QUEUED_RECEIPT_FALLBACK_TEXT}, "msg_type": 2},
            ),
        ]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class _FlakyStreamClient(FakeExternalApiClient):
    """第一条 SSE 流立即抛 ReadTimeout（模拟 pump 被 30s 读超时杀死的事故现场），
    后续流恢复正常。"""

    def __init__(self, base_url: str, token: str, transport=None) -> None:
        super().__init__(base_url, token, transport)
        self.stream_calls = 0

    async def stream_events(self, session_id: str, last_seq: int = 0):
        self.stream_calls += 1
        if self.stream_calls == 1:
            raise httpx.ReadTimeout("simulated keep-alive read timeout")
        while True:
            yield await self.events.get()


@pytest.mark.asyncio
async def test_pump_reconnects_after_stream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归（磁盘满事故断点②）：pump 因 SSE ReadTimeout 挂掉后必须自动重连。
    旧实现只打一条日志就终结任务，且 sessions 映射仍在，此后该会话所有主动
    推送（心跳升级、cron 提醒）永久滞留服务端事件缓冲，形成"能收不能发"。"""
    monkeypatch.setattr(bridge_module, "_PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS", 0.01)
    media = _media_transport({})
    task, client = await _start_bridge(monkeypatch, media, client_cls=_FlakyStreamClient)
    try:
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="r1"))
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.stream_calls >= 2)
        await ext.events.put(
            {"type": "outbound.created", "seq": 7, "text": "重连补投", "external_key": "qq:c2c:u9"}
        )
        await _wait_until(lambda: client.api.calls)
        assert client.api.calls == [
            ("post_c2c_message", {"openid": "u9", "markdown": {"content": "重连补投"}, "msg_type": 2})
        ]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class _ReplayStreamClient(FakeExternalApiClient):
    """模拟服务端 SSE 重放语义：每次流调用重放 feed 中 seq 大于 last_seq 的全部
    事件，然后挂起等待新事件。"""

    def __init__(self, base_url: str, token: str, transport=None) -> None:
        super().__init__(base_url, token, transport)
        self.feed: list[dict] = []

    async def stream_events(self, session_id: str, last_seq: int = 0):
        while True:
            for event in list(self.feed):
                seq = int(event.get("seq") or 0)
                if seq > last_seq:
                    yield event
                    last_seq = seq
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_pump_retries_failed_delivery_then_drops_poison(monkeypatch: pytest.MonkeyPatch) -> None:
    """投递失败不推进 seq：重连后服务端重放本条自动重试；连续失败达到上限的
    毒消息记 error 后跳过，后续事件继续消费（不再像旧实现那样整个 pump 死亡）。"""
    monkeypatch.setattr(bridge_module, "_PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(bridge_module, "_PUMP_DELIVER_MAX_ATTEMPTS", 2)

    attempts: dict[str, int] = {}
    delivered: list[tuple[str, dict]] = []

    async def _flaky_post_c2c(**kwargs):
        content = _outbound_text(kwargs)
        attempts[content] = attempts.get(content, 0) + 1
        if content == "poison":
            raise RuntimeError("simulated QQ API rejection")
        delivered.append(("post_c2c_message", kwargs))
        return {"id": "mid-1"}

    flaky_api = FakeBotApi()
    flaky_api.post_c2c_message = _flaky_post_c2c

    class _FlakyApiClient(FakeClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.api = flaky_api

    fake_botpy = types.ModuleType("botpy")
    fake_botpy.Client = _FlakyApiClient
    fake_botpy.Intents = FakeIntents
    monkeypatch.setitem(sys.modules, "botpy", fake_botpy)
    monkeypatch.setattr(bridge_module, "ExternalApiClient", _ReplayStreamClient)
    media = _media_transport({})
    monkeypatch.setattr(
        bridge_module, "_create_media_client", lambda: httpx.AsyncClient(transport=media)
    )
    task = asyncio.create_task(
        bridge_module.run_qq_official_bridge(
            app_id="100",
            app_secret="sekrit",
            sandbox=False,
            token="t",
            base_url="http://127.0.0.1:1/api/v1",
            on_state=lambda state, detail: None,
        ),
        name="test-qq-bridge-replay",
    )
    try:
        await _wait_until(lambda: FakeClient.instances and FakeClient.instances[-1].started)
        client = FakeClient.instances[-1]
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="p1"))
        ext = FakeExternalApiClient.instances[-1]
        ext.feed.append(
            {
                "type": "outbound.created",
                "seq": 1,
                "text": "poison",
                "external_key": "qq:c2c:u9",
                "outbox_id": "obx-poison-1",
            }
        )
        await _wait_until(lambda: attempts.get("poison", 0) >= 2)
        ext.feed.append({"type": "outbound.created", "seq": 2, "text": "正常补投", "external_key": "qq:c2c:u9"})
        await _wait_until(lambda: delivered)
        # 毒消息达到上限即放弃，不再无限重试；后续消息正常投递。
        assert attempts["poison"] == 2
        # 账本已不按年龄回收，判毒必须留下终态销账：否则每次桥重建（进程内
        # per-id 计数随桥消失）都会给一个平台永久拒投的目标新一轮预算。
        await _wait_until(lambda: ext.ack_status_by_id.get("obx-poison-1") == "undeliverable")
        assert delivered == [
            ("post_c2c_message", {"openid": "u9", "markdown": {"content": "正常补投"}, "msg_type": 2})
        ]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _single_failing_api(monkeypatch, *, method: str, error: Exception, max_attempts: int | None = None):
    """装一个只有某个发送方法会失败、且失败次数可数的桥（返回 attempts 计数器）。"""
    monkeypatch.setattr(bridge_module, "_PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS", 0.01)
    if max_attempts is not None:
        monkeypatch.setattr(bridge_module, "_PUMP_DELIVER_MAX_ATTEMPTS", max_attempts)
    attempts = {"n": 0}

    async def _fail(**kwargs):
        attempts["n"] += 1
        raise error

    api = FakeBotApi()
    setattr(api, method, _fail)

    class _ApiClient(FakeClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.api = api

    fake_botpy = types.ModuleType("botpy")
    fake_botpy.Client = _ApiClient
    fake_botpy.Intents = FakeIntents
    monkeypatch.setitem(sys.modules, "botpy", fake_botpy)
    monkeypatch.setattr(bridge_module, "ExternalApiClient", _ReplayStreamClient)
    monkeypatch.setattr(
        bridge_module,
        "_create_media_client",
        lambda: httpx.AsyncClient(transport=_media_transport({})),
    )
    return attempts


@pytest.mark.asyncio
async def test_passive_expiry_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """被动窗口过期是确定性失败：平台对同一条重放多少次都一样，一发就够。
    botpy 把 HTTP 400 只抛成带文案的 ServerError（没有数字码），所以判据是文案。"""
    attempts = _single_failing_api(
        monkeypatch, method="post_group_message", error=RuntimeError("回复消息msg_id已过期")
    )
    task = asyncio.create_task(
        bridge_module.run_qq_official_bridge(
            app_id="100",
            app_secret="sekrit",
            sandbox=False,
            token="t",
            base_url="http://127.0.0.1:1/api/v1",
            on_state=lambda state, detail: None,
        ),
        name="test-qq-bridge-expired",
    )
    try:
        await _wait_until(lambda: FakeClient.instances and FakeClient.instances[-1].started)
        client = FakeClient.instances[-1]
        await client.on_group_at_message_create(_group_message(message_id="gm-exp"))
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        ext.feed.append(
            {"type": "reply.final", "seq": 1, "text": "跑了很久的答案", "outbox_id": "obx-exp-1"}
        )
        await _wait_until(lambda: ext.ack_status_by_id.get("obx-exp-1") == "undeliverable")
        assert attempts["n"] == 1
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_active_permission_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """群目标没有主动消息权限也是确定性失败：一次判死，不烧满 5 次预算。"""
    attempts = _single_failing_api(
        monkeypatch, method="post_c2c_message", error=RuntimeError("主动消息失败, 无权限")
    )
    task = asyncio.create_task(
        bridge_module.run_qq_official_bridge(
            app_id="100",
            app_secret="sekrit",
            sandbox=False,
            token="t",
            base_url="http://127.0.0.1:1/api/v1",
            on_state=lambda state, detail: None,
        ),
        name="test-qq-bridge-denied",
    )
    try:
        await _wait_until(lambda: FakeClient.instances and FakeClient.instances[-1].started)
        client = FakeClient.instances[-1]
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="cm-denied"))
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        ext.feed.append(
            {
                "type": "outbound.created",
                "seq": 1,
                "text": "提醒",
                "external_key": "qq:c2c:u9",
                "outbox_id": "obx-denied-1",
            }
        )
        await _wait_until(lambda: ext.ack_status_by_id.get("obx-denied-1") == "undeliverable")
        assert attempts["n"] == 1
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_active_send_over_rate_budget_is_deferred_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主动消息按目标限速：撞到分钟档那一发根本不发出去（省一次必然被拒的请求），
    交给 pump 重连重试；它不是确定性失败，不该被判毒销账。"""
    monkeypatch.setattr(bridge_module, "_ACTIVE_SEND_PER_MINUTE", 1)
    monkeypatch.setattr(bridge_module, "_PUMP_DELIVER_MAX_ATTEMPTS", 99)
    monkeypatch.setattr(bridge_module, "_PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS", 0.01)
    task, client = await _start_bridge(monkeypatch, _media_transport({}))
    try:
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="cm-rate"))
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        await ext.events.put(
            {"type": "outbound.created", "seq": 1, "text": "第一条", "external_key": "qq:c2c:u9"}
        )
        await _wait_until(lambda: len(client.api.calls) >= 1)
        await ext.events.put(
            {
                "type": "outbound.created",
                "seq": 2,
                "text": "第二条",
                "external_key": "qq:c2c:u9",
                "outbox_id": "obx-rate-2",
            }
        )
        await asyncio.sleep(0.2)
        assert [_outbound_text(call[1]) for call in client.api.calls] == ["第一条"]
        assert ext.ack_status_by_id.get("obx-rate-2") != "undeliverable"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_passive_replies_do_not_consume_the_active_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """被动回复不占主动额度：分钟档只剩 1 个名额，两发被动也必须都发得出去。"""
    monkeypatch.setattr(bridge_module, "_ACTIVE_SEND_PER_MINUTE", 1)
    task, client = await _start_bridge(monkeypatch, _media_transport({}))
    try:
        await client.on_group_at_message_create(_group_message(message_id="gm-budget"))
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        await ext.events.put({"type": "reply.final", "seq": 1, "text": "第一发"})
        await ext.events.put({"type": "reply.final", "seq": 2, "text": "第二发"})
        await _wait_until(lambda: len(client.api.calls) >= 2)
        assert [call[1]["msg_seq"] for call in client.api.calls] == [1, 2]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_deliver_treats_empty_api_response_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """botpy http 层对请求超时静默返回 None：deliver 必须把无回执视为失败抛出、
    交给 pump 按服务端重放重试，而不是像旧实现那样记一次假成功。"""
    monkeypatch.setattr(bridge_module, "_PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS", 0.01)

    attempts = {"n": 0}
    delivered: list[tuple[str, dict]] = []

    async def _timeout_once_post_c2c(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return None  # botpy 超时：无任何回执
        delivered.append(("post_c2c_message", kwargs))
        return {"id": "m-1"}

    api = FakeBotApi()
    api.post_c2c_message = _timeout_once_post_c2c

    class _ApiFakeClient(FakeClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.api = api

    fake_botpy = types.ModuleType("botpy")
    fake_botpy.Client = _ApiFakeClient
    fake_botpy.Intents = FakeIntents
    monkeypatch.setitem(sys.modules, "botpy", fake_botpy)
    monkeypatch.setattr(bridge_module, "ExternalApiClient", _ReplayStreamClient)
    media = _media_transport({})
    monkeypatch.setattr(
        bridge_module, "_create_media_client", lambda: httpx.AsyncClient(transport=media)
    )
    task = asyncio.create_task(
        bridge_module.run_qq_official_bridge(
            app_id="100",
            app_secret="sekrit",
            sandbox=False,
            token="t",
            base_url="http://127.0.0.1:1/api/v1",
            on_state=lambda state, detail: None,
        ),
        name="test-qq-bridge-unconfirmed",
    )
    try:
        await _wait_until(lambda: FakeClient.instances and FakeClient.instances[-1].started)
        client = FakeClient.instances[-1]
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="u1"))
        ext = FakeExternalApiClient.instances[-1]
        # 这条事件不带 outbox_id：没有可幂等的身份，重放重发是唯一能保住投递的路径
        # （带 id 的那条闸门见下一个用例）。
        ext.feed.append({"type": "outbound.created", "seq": 1, "text": "回执缺失", "external_key": "qq:c2c:u9"})
        await _wait_until(lambda: delivered)
        # 第一次无回执 → 判失败 → 重连重放 → 第二次带回执成功。
        assert attempts["n"] == 2
        assert delivered == [("post_c2c_message", {"openid": "u9", "markdown": {"content": "回执缺失"}, "msg_type": 2})]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_pump_does_not_resend_unconfirmed_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """出站幂等闸门：无回执意味着请求已经发给渠道，重放副本再发一次就是给用户第二
    份逐字相同的回答（实盘 2026-10-17 01:14:22 `deliver failed ... seq 80` 紧接
    01:14:24 `delivered`）。带 `outbox_id` 的这类事件只允许 POST 一次，按
    `unconfirmed` 销账，且同会话后续事件继续消费。"""
    monkeypatch.setattr(bridge_module, "_PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS", 0.01)

    attempts: dict[str, int] = {}
    delivered: list[tuple[str, dict]] = []

    async def _silent_post_c2c(**kwargs):
        content = _outbound_text(kwargs)
        attempts[content] = attempts.get(content, 0) + 1
        if content == "回执未知":
            return None  # botpy 超时：请求已发出，回执一个都没回来
        delivered.append(("post_c2c_message", kwargs))
        return {"id": "m-1"}

    api = FakeBotApi()
    api.post_c2c_message = _silent_post_c2c

    class _ApiFakeClient(FakeClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.api = api

    fake_botpy = types.ModuleType("botpy")
    fake_botpy.Client = _ApiFakeClient
    fake_botpy.Intents = FakeIntents
    monkeypatch.setitem(sys.modules, "botpy", fake_botpy)
    monkeypatch.setattr(bridge_module, "ExternalApiClient", _ReplayStreamClient)
    media = _media_transport({})
    monkeypatch.setattr(
        bridge_module, "_create_media_client", lambda: httpx.AsyncClient(transport=media)
    )
    task = asyncio.create_task(
        bridge_module.run_qq_official_bridge(
            app_id="100",
            app_secret="sekrit",
            sandbox=False,
            token="t",
            base_url="http://127.0.0.1:1/api/v1",
            on_state=lambda state, detail: None,
        ),
        name="test-qq-bridge-idempotent-send",
    )
    try:
        await _wait_until(lambda: FakeClient.instances and FakeClient.instances[-1].started)
        client = FakeClient.instances[-1]
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="i1"))
        ext = FakeExternalApiClient.instances[-1]
        ext.feed.append(
            {
                "type": "outbound.created",
                "seq": 1,
                "text": "回执未知",
                "external_key": "qq:c2c:u9",
                "outbox_id": "obx-dup-1",
            }
        )
        await _wait_until(lambda: ext.ack_status_by_id.get("obx-dup-1") == "unconfirmed")
        ext.feed.append(
            {
                "type": "outbound.created",
                "seq": 2,
                "text": "下一条",
                "external_key": "qq:c2c:u9",
                "outbox_id": "obx-next-1",
            }
        )
        await _wait_until(lambda: delivered)
        # 重放副本被闸门拦下：同一条只 POST 一次，销账状态不冒充已送达。
        assert attempts["回执未知"] == 1
        assert delivered == [
            ("post_c2c_message", {"openid": "u9", "markdown": {"content": "下一条"}, "msg_type": 2})
        ]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_pump_acks_outbox_after_confirmed_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """outbound.created 携带 outbox_id 时：投递确认后必须 ack 销账，
    否则下次启动重放会重复投递。"""
    media = _media_transport({})
    task, client = await _start_bridge(monkeypatch, media)
    try:
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="a1"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {
                "type": "outbound.created",
                "seq": 1,
                "text": "带账本的推送",
                "external_key": "qq:c2c:u9",
                "outbox_id": "obx-test-1",
            }
        )
        await _wait_until(lambda: client.api.calls and ext.acked)
        assert client.api.calls == [
            ("post_c2c_message", {"openid": "u9", "markdown": {"content": "带账本的推送"}, "msg_type": 2})
        ]
        assert ext.acked == [("ext:qq-official:qq:c2c:u9", "obx-test-1")]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_pump_acks_reply_final_carried_outbox_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """回合回复也带 outbox_id（relay 发布前登记进账本）：pump 投递后必须销账，
    否则该记录会被服务端对账反复重注入。"""
    media = _media_transport({})
    task, client = await _start_bridge(monkeypatch, media)
    try:
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="r1"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {
                "type": "reply.final",
                "seq": 1,
                "text": "下载完成了",
                "outbox_id": "obx-reply-1",
            }
        )
        await _wait_until(lambda: client.api.calls and ext.acked)
        assert client.api.calls == [
            ("post_c2c_message", {"openid": "u9", "markdown": {"content": "下载完成了"}, "msg_type": 2})
        ]
        assert ext.acked == [("ext:qq-official:qq:c2c:u9", "obx-reply-1")]
        assert ext.ack_status_by_id["obx-reply-1"] == "delivered"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class _WarmUpStreamClient(FakeExternalApiClient):
    """构造时即携带 pending outbox 清单（模拟服务端重启前滞留的主动推送），
    订阅即重放对应的 outbound 事件（模拟启动重放已进 hub）。"""

    def __init__(self, base_url: str, token: str, transport=None) -> None:
        super().__init__(base_url, token, transport)
        self.pending_outbox = [
            {
                "outbox_id": "obx-warm-1",
                "session_id": "ext:qq-official:qq:c2c:u5",
                "external_key": "qq:c2c:u5",
                "ts": "2026-09-09T02:35:38.990625",
            }
        ]

    async def stream_events(self, session_id: str, last_seq: int = 0):
        if session_id == "ext:qq-official:qq:c2c:u5" and last_seq == 0:
            yield {
                "type": "outbound.created",
                "seq": 1,
                "text": "重启滞留的提醒",
                "external_key": "qq:c2c:u5",
                "outbox_id": "obx-warm-1",
            }
        while True:
            yield await self.events.get()


@pytest.mark.asyncio
async def test_bridge_warms_pumps_from_pending_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程重启后 sessions 映射清空、pump 只等入站消息才建：桥启动时必须按
    GET /outbox/pending 清单预热 pump，否则重启前滞留的主动推送无人消费。
    本测试全程不触发任何 on_incoming。"""
    media = _media_transport({})
    task, client = await _start_bridge(monkeypatch, media, client_cls=_WarmUpStreamClient)
    try:
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: client.api.calls)
        assert client.api.calls == [
            ("post_c2c_message", {"openid": "u5", "markdown": {"content": "重启滞留的提醒"}, "msg_type": 2})
        ]
        await _wait_until(lambda: ext.acked)
        assert ext.acked == [("ext:qq-official:qq:c2c:u5", "obx-warm-1")]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class _LatePendingStreamClient(FakeExternalApiClient):
    """启动时 pending 为空（预热扑空——复刻 2026-09-14 日报滞留事故时序），
    之后由测试体补入滞留条目；订阅目标会话时先重放滞留事件再挂起。"""

    def __init__(self, base_url: str, token: str, transport=None) -> None:
        super().__init__(base_url, token, transport)
        self.pending_outbox: list[dict] = []

    async def stream_events(self, session_id: str, last_seq: int = 0):
        if session_id == "ext:qq-official:qq:c2c:u7" and last_seq == 0:
            yield {
                "type": "outbound.created",
                "seq": 1,
                "text": "重启后才产出的日报",
                "external_key": "qq:c2c:u7",
                "outbox_id": "obx-late-1",
            }
        while True:
            yield await self.events.get()


@pytest.mark.asyncio
async def test_bridge_periodically_respawns_pumps_from_pending_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归（2026-09-14 日报滞留事故）：桥启动预热在账本为空时扑空，滞留推送
    稍后才入账，且无任何入站消息——常驻 pending 对账必须自动建 pump 并经 SSE
    重放完成投递，而不是等重启或用户消息。全程不触发 on_incoming。"""
    monkeypatch.setattr(bridge_module, "_PENDING_RECONCILE_INTERVAL_SECONDS", 0.05)
    media = _media_transport({})
    task, client = await _start_bridge(monkeypatch, media, client_cls=_LatePendingStreamClient)
    try:
        ext = FakeExternalApiClient.instances[-1]
        assert ext.pending_outbox == []  # 启动首跑确实扑空
        # 滞留记录在桥就绪之后才入账（服务端 cron 产出晚于桥预热）。
        ext.pending_outbox.append(
            {
                "outbox_id": "obx-late-1",
                "session_id": "ext:qq-official:qq:c2c:u7",
                "external_key": "qq:c2c:u7",
                "ts": "2026-09-14T23:03:48.651494",
            }
        )
        await _wait_until(lambda: client.api.calls)
        assert client.api.calls == [
            ("post_c2c_message", {"openid": "u7", "markdown": {"content": "重启后才产出的日报"}, "msg_type": 2})
        ]
        await _wait_until(lambda: ext.acked)
        assert ext.acked == [("ext:qq-official:qq:c2c:u7", "obx-late-1")]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class _FlakyListClient(FakeExternalApiClient):
    """前两次 list_pending_outbox 失败（模拟桥先于服务端就绪），之后恢复。"""

    def __init__(self, base_url: str, token: str, transport=None) -> None:
        super().__init__(base_url, token, transport)
        self.list_calls = 0
        self.pending_outbox = [
            {
                "outbox_id": "obx-flaky-1",
                "session_id": "ext:qq-official:qq:c2c:u6",
                "external_key": "qq:c2c:u6",
                "ts": "2026-09-14T23:03:48",
            }
        ]

    async def list_pending_outbox(self) -> list[dict]:
        self.list_calls += 1
        if self.list_calls <= 2:
            raise httpx.ConnectError("simulated server not ready")
        return list(self.pending_outbox)

    async def stream_events(self, session_id: str, last_seq: int = 0):
        if session_id == "ext:qq-official:qq:c2c:u6" and last_seq == 0:
            yield {
                "type": "outbound.created",
                "seq": 1,
                "text": "服务端就绪后的补投",
                "external_key": "qq:c2c:u6",
                "outbox_id": "obx-flaky-1",
            }
        while True:
            yield await self.events.get()


@pytest.mark.asyncio
async def test_bridge_pending_reconcile_survives_list_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动首跑与前几轮对账可能碰上服务端未就绪：list 失败不得杀死桥或对账
    循环，就绪后自动完成投递。"""
    monkeypatch.setattr(bridge_module, "_PENDING_RECONCILE_INTERVAL_SECONDS", 0.05)
    media = _media_transport({})
    task, client = await _start_bridge(monkeypatch, media, client_cls=_FlakyListClient)
    try:
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: client.api.calls)
        assert ext.list_calls >= 3  # 前两次失败没有终结循环
        assert client.api.calls == [
            ("post_c2c_message", {"openid": "u6", "markdown": {"content": "服务端就绪后的补投"}, "msg_type": 2})
        ]
        await _wait_until(lambda: ext.acked)
        assert ext.acked == [("ext:qq-official:qq:c2c:u6", "obx-flaky-1")]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_pump_skips_duplicate_copy_of_acked_outbox_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务端周期对账/重启重放会产生同一 outbox_id 的多份副本：首份 ack 成功
    后，后续副本必须跳过，不得重复推送给用户；无 id 的事件不受影响。"""
    media = _media_transport({})
    task, client = await _start_bridge(monkeypatch, media)
    try:
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="d1"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {
                "type": "outbound.created",
                "seq": 1,
                "text": "日报",
                "external_key": "qq:c2c:u9",
                "outbox_id": "obx-dup",
            }
        )
        await _wait_until(lambda: ext.acked)
        # 同 id 的两份重复副本（对账注入的形态）+ 一条无 id 的正常事件。
        await ext.events.put(
            {"type": "outbound.created", "seq": 2, "text": "日报", "external_key": "qq:c2c:u9", "outbox_id": "obx-dup"}
        )
        await ext.events.put(
            {"type": "outbound.created", "seq": 3, "text": "日报", "external_key": "qq:c2c:u9", "outbox_id": "obx-dup"}
        )
        await ext.events.put(
            {"type": "outbound.created", "seq": 4, "text": "新推送", "external_key": "qq:c2c:u9"}
        )
        await _wait_until(lambda: len(client.api.calls) >= 2)
        await asyncio.sleep(0.05)  # 给潜在的错误重投留出窗口
        assert client.api.calls == [
            ("post_c2c_message", {"openid": "u9", "markdown": {"content": "日报"}, "msg_type": 2}),
            ("post_c2c_message", {"openid": "u9", "markdown": {"content": "新推送"}, "msg_type": 2}),
        ]
        assert ext.acked == [("ext:qq-official:qq:c2c:u9", "obx-dup")]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_pump_caps_total_attempts_per_outbox_id_across_seqs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 outbox_id 的重放副本 seq 各不相同，per-seq 计数会对每份副本重置：
    id 级总预算保证毒消息不会随副本反复获得完整重试轮次。"""
    monkeypatch.setattr(bridge_module, "_PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(bridge_module, "_PUMP_DELIVER_MAX_ATTEMPTS", 2)

    attempts: dict[str, int] = {}
    delivered: list[tuple[str, dict]] = []

    async def _flaky_post_c2c(**kwargs):
        content = _outbound_text(kwargs)
        attempts[content] = attempts.get(content, 0) + 1
        if content == "poison":
            raise RuntimeError("simulated QQ API rejection")
        delivered.append(("post_c2c_message", kwargs))
        return {"id": "mid-1"}

    flaky_api = FakeBotApi()
    flaky_api.post_c2c_message = _flaky_post_c2c

    class _FlakyApiClient(FakeClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.api = flaky_api

    fake_botpy = types.ModuleType("botpy")
    fake_botpy.Client = _FlakyApiClient
    fake_botpy.Intents = FakeIntents
    monkeypatch.setitem(sys.modules, "botpy", fake_botpy)
    monkeypatch.setattr(bridge_module, "ExternalApiClient", _ReplayStreamClient)
    media = _media_transport({})
    monkeypatch.setattr(
        bridge_module, "_create_media_client", lambda: httpx.AsyncClient(transport=media)
    )
    task = asyncio.create_task(
        bridge_module.run_qq_official_bridge(
            app_id="100",
            app_secret="sekrit",
            sandbox=False,
            token="t",
            base_url="http://127.0.0.1:1/api/v1",
            on_state=lambda state, detail: None,
        ),
        name="test-qq-bridge-attempts",
    )
    try:
        await _wait_until(lambda: FakeClient.instances and FakeClient.instances[-1].started)
        client = FakeClient.instances[-1]
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="p2"))
        ext = FakeExternalApiClient.instances[-1]
        ext.feed.append(
            {"type": "outbound.created", "seq": 1, "text": "poison", "external_key": "qq:c2c:u9", "outbox_id": "obx-p"}
        )
        await _wait_until(lambda: attempts.get("poison", 0) >= 2)
        # 同 id 重放副本（新 seq）：per-seq 预算虽被重置，id 级预算已耗尽——
        # 零尝试直接丢弃；后续正常事件不受影响。
        ext.feed.append(
            {"type": "outbound.created", "seq": 2, "text": "poison", "external_key": "qq:c2c:u9", "outbox_id": "obx-p"}
        )
        ext.feed.append(
            {"type": "outbound.created", "seq": 3, "text": "正常", "external_key": "qq:c2c:u9", "outbox_id": "obx-ok"}
        )
        await _wait_until(lambda: delivered)
        await asyncio.sleep(0.05)
        assert attempts["poison"] == 2  # 副本没有触发新的投递尝试
        assert delivered == [
            ("post_c2c_message", {"openid": "u9", "markdown": {"content": "正常"}, "msg_type": 2})
        ]
        assert ("ext:qq-official:qq:c2c:u9", "obx-ok") in ext.acked
        # 判毒的 id 必须留下终态销账：账本不按年龄回收，投递预算是它唯一的上界，
        # 而进程内 per-id 计数随桥重建清零（实测每小时一次），不销账就是无限重试。
        # 销账状态必须是 undeliverable，不能冒充"已送达"。
        assert ("ext:qq-official:qq:c2c:u9", "obx-p") in ext.acked
        assert ext.ack_status_by_id["obx-p"] == "undeliverable"
        assert ext.ack_status_by_id["obx-ok"] == "delivered"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _outbound_attachment(name: str, mime_type: str, token: str = "t1") -> dict:
    return {
        "name": name,
        "mime_type": mime_type,
        "size": 9,
        "url": f"/api/ceo/media/original?token={token}",
    }


def _absolute_media(token: str) -> str:
    # base_url 是 http://127.0.0.1:1/api/v1，媒体 URL 按 origin 拼接。
    return f"http://127.0.0.1:1/api/ceo/media/original?token={token}"


@pytest.mark.asyncio
async def test_pump_delivers_event_attachment_via_file_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """出站附件以 file_data（base64）直传：桥从本机签名 URL 取字节后，在自己的媒体
    client 上打平台媒体上传接口——既不经 botpy 只暴露 url 的包装，也不占 SDK 那枚
    套在每个 API 请求上的全局超时。拿到 file_info 以 msg_type=7 发送，正文另发一条文本。"""
    records: list[httpx.Request] = []
    files_url = "https://api.sgroup.qq.com/v2/users/u9/files"
    media = _media_transport(
        {_absolute_media("t1"): (200, b"doc-bytes"), files_url: (200, _upload_reply())},
        records,
    )
    task, client = await _start_bridge(monkeypatch, media)
    try:
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="f1"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {
                "type": "outbound.created",
                "seq": 1,
                "text": "日报已生成：日报",
                "external_key": "qq:c2c:u9",
                "attachments": [_outbound_attachment("日报.docx", "application/octet-stream")],
            }
        )
        await _wait_until(lambda: len(client.api.calls) >= 2)

        uploads = _api_uploads(records)
        assert len(uploads) == 1
        upload = uploads[0]
        assert upload.method == "POST"
        assert str(upload.url) == files_url
        assert upload.headers["authorization"] == "QQBot fake-access-token"
        assert upload.headers["x-union-appid"] == "100"
        body = json.loads(upload.content)
        assert body["file_type"] == 4  # 任意文件
        assert base64.b64decode(body["file_data"]) == b"doc-bytes"
        assert body["srv_send_msg"] is False
        # 预算从请求体字节数推导：12 字节的 base64 只落在固定开销那一档，连接判死仍是 5 秒。
        timeout = upload.extensions["timeout"]
        assert timeout["connect"] == 5.0
        assert timeout["read"] == pytest.approx(15.0, abs=0.01)
        # 这一发不经 botpy 的 http.request（那条道带的是文本消息用的共用旋钮）。
        assert client.api.http_calls == []
        # 全局旋钮是 15 秒（SDK 默认 5 秒对文本太紧，见 test_bridge_raises_the_shared_http_
        # timeout_above_the_sdk_default），但它服务不了上传：20MiB 那一档按字节推导需要
        # 68 秒，把共用一把抬到那个量级就等于让一个卡住的文本 POST 顶住这条会话的 pump。
        assert client.init_kwargs.get("timeout") == bridge_module._BRIDGE_HTTP_TIMEOUT_SECONDS
        big_upload_body = len(base64.b64encode(bytes(20 * 1024 * 1024)))
        assert (
            bridge_module._media_upload_timeout(big_upload_body)
            > bridge_module._BRIDGE_HTTP_TIMEOUT_SECONDS
        )
        assert client.api.check_session_calls == 1
        assert client.api.calls == [
            (
                "post_c2c_message",
                {"openid": "u9", "msg_type": 7, "media": {"file_info": "file-info-fd"}},
            ),
            (
                "post_c2c_message",
                {"openid": "u9", "markdown": {"content": "日报已生成：日报"}, "msg_type": 2},
            ),
        ]
        # 未回退到 url 上传路径（那条路径要求平台能回源本服务端）。
        assert not [call for call in client.api.calls if call[0] == "post_c2c_file"]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_pump_falls_back_to_url_upload_when_file_data_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """file_data 被平台拒（非 2xx）时回退 url 回源上传（要求本服务端公网可达），
    回退成功仍以 msg_type=7 送达，不降级。"""
    records: list[httpx.Request] = []
    files_url = "https://api.sgroup.qq.com/v2/users/u9/files"
    media = _media_transport(
        {
            _absolute_media("t2"): (200, b"pdf-bytes"),
            files_url: (400, b'{"code":40093001,"message":"file_data rejected"}'),
        },
        records,
    )
    task, client = await _start_bridge(monkeypatch, media)
    try:
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="f2"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {
                "type": "reply.final",
                "seq": 1,
                "text": "简历已渲染：简历.pdf",
                "attachments": [_outbound_attachment("简历.pdf", "application/pdf", "t2")],
            }
        )
        await _wait_until(lambda: len(client.api.calls) >= 2)

        uploads = _api_uploads(records)
        assert len(uploads) == 1
        assert json.loads(uploads[0].content)["file_type"] == 4
        assert ("post_c2c_file", {"openid": "u9", "file_type": 4, "url": _absolute_media("t2"), "srv_send_msg": False}) in client.api.calls
        assert (
            "post_c2c_message",
            {"openid": "u9", "msg_type": 7, "media": {"file_info": "file-info-c"}},
        ) in client.api.calls
        assert client.api.calls[-1] == (
            "post_c2c_message",
            {"openid": "u9", "markdown": {"content": "简历已渲染：简历.pdf"}, "msg_type": 2},
        )
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_pump_maps_attachment_mime_to_platform_file_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """mime → file_type 映射：图片 1、mp4 2、语音 3、其余（文档）4；群聊走群接口。

    群聊这一轮的三发都带被动回复参数，seq 按实际发出顺序递增（两枚附件各占一发，
    正文排在其后）——同一个 msg_id 上重复 msg_seq 会被平台拒。
    """
    records: list[httpx.Request] = []
    group_files_url = "https://api.sgroup.qq.com/v2/groups/g1/files"
    media = _media_transport(
        {
            _absolute_media("i1"): (200, b"png-bytes"),
            _absolute_media("d1"): (200, b"docx-bytes"),
            group_files_url: (200, _upload_reply()),
        },
        records,
    )
    task, client = await _start_bridge(monkeypatch, media)
    try:
        await client.on_group_at_message_create(
            SimpleNamespace(group_openid="g1", content="hi", id="m9", attachments=[])
        )
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {
                "type": "outbound.created",
                "seq": 1,
                "text": "好了",
                "external_key": "qq:group:g1",
                "attachments": [
                    _outbound_attachment("chart.png", "image/png", "i1"),
                    _outbound_attachment("报告.docx", "application/octet-stream", "d1"),
                ],
            }
        )
        await _wait_until(lambda: len(client.api.calls) >= 3)

        uploads = _api_uploads(records)
        assert [str(request.url) for request in uploads] == [group_files_url, group_files_url]
        assert [json.loads(request.content)["file_type"] for request in uploads] == [1, 4]
        assert client.api.calls == [
            (
                "post_group_message",
                {
                    "group_openid": "g1",
                    "msg_type": 7,
                    "media": {"file_info": "file-info-fd"},
                    "msg_id": "m9",
                    "msg_seq": 1,
                },
            ),
            (
                "post_group_message",
                {
                    "group_openid": "g1",
                    "msg_type": 7,
                    "media": {"file_info": "file-info-fd"},
                    "msg_id": "m9",
                    "msg_seq": 2,
                },
            ),
            (
                "post_group_message",
                {
                    "group_openid": "g1",
                    "markdown": {"content": "好了"},
                    "msg_type": 2,
                    "msg_id": "m9",
                    "msg_seq": 3,
                },
            ),
        ]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_group_mentions_use_markdown_at_user_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task, client = await _start_bridge(monkeypatch, _media_transport({}, []))
    try:
        await client.on_group_at_message_create(_group_message("hi", "m-mention"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {
                "type": "outbound.created",
                "seq": 1,
                "text": "需要你看一下",
                "external_key": "qq:group:g1",
                "mentions": ["ABCDEF1234567890"],
            }
        )
        await _wait_until(lambda: any(name == "post_group_message" for name, _ in client.api.calls))
        call = next(kwargs for name, kwargs in client.api.calls if name == "post_group_message")
        assert call["msg_type"] == 2
        assert call["markdown"]["content"] == (
            '<qqbot-at-user id="ABCDEF1234567890" />\n需要你看一下'
        )
        assert call["msg_id"] == "m-mention"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_c2c_markdown_body_carries_no_content_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平台合同：传了 markdown 之后 ``content`` 字段必须留空，两个同时给会被拒。"""
    task, client = await _start_bridge(monkeypatch, _media_transport({}, []))
    try:
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="md1"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {"type": "reply.final", "seq": 1, "text": "结论如下：\n1. 先做这个"}
        )
        await _wait_until(lambda: bool(client.api.calls))
        kwargs = client.api.calls[0][1]
        assert kwargs["msg_type"] == 2
        assert "content" not in kwargs
        assert kwargs["markdown"] == {"content": "结论如下：\n\n1. 先做这个"}
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_markdown_rejection_falls_back_to_text_shape_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平台拒收 markdown 形状时同一份正文换纯文本再发一次：不回落就会被判 transient、
    吃满 5 次预算后按 undeliverable 销账，用户一条都收不到。"""
    task, client = await _start_bridge(monkeypatch, _media_transport({}, []))
    original = client.api.post_c2c_message
    seen: list[dict] = []

    async def _reject_markdown(**kwargs):
        seen.append(kwargs)
        if "markdown" in kwargs:
            raise RuntimeError("400 Markdown参数有换行符, 请移除Markdown参数中的换行符")
        return await original(**kwargs)

    client.api.post_c2c_message = _reject_markdown
    try:
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="fb1"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put({"type": "reply.final", "seq": 1, "text": "回落正文"})
        await _wait_until(lambda: any("content" in kwargs for kwargs in seen))
        await asyncio.sleep(0.05)
        assert seen[0]["msg_type"] == 2
        assert seen[-1] == {"openid": "u9", "content": "回落正文", "msg_type": 0}
        # 一次回落即成功：不进去重/判毒那 5 次预算
        assert len(seen) == 2
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _group_message(content: str = "在吗", message_id: str = "gm1") -> SimpleNamespace:
    return SimpleNamespace(group_openid="g1", content=content, id=message_id, attachments=[])


def _webp_bytes() -> bytes:
    from PIL import Image, features

    if not features.check("webp"):
        pytest.skip("本机 Pillow 没有 webp 编解码器")
    output = io.BytesIO()
    Image.new("RGBA", (8, 8), (220, 40, 40, 160)).save(output, format="WEBP")
    return output.getvalue()


@pytest.mark.asyncio
async def test_pump_converts_webp_attachment_into_the_image_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`.webp` 表情包原先带着 application/octet-stream 落到 file_type=4，用户收到的是
    文件而不是表情。mime 修好之后还要保证字节是平台画得出的格式：非 png/jpg/gif
    的图片先转 PNG 再上传。实盘 2026-10-30 00:56:01 `005.webp (file_type=4)`。"""
    records: list[httpx.Request] = []
    files_url = "https://api.sgroup.qq.com/v2/users/u9/files"
    webp = _webp_bytes()
    media = _media_transport(
        {_absolute_media("w1"): (200, webp), files_url: (200, _upload_reply())}, records
    )
    task, client = await _start_bridge(monkeypatch, media)
    try:
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="f9"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {
                "type": "outbound.created",
                "seq": 1,
                "text": "在的",
                "external_key": "qq:c2c:u9",
                "attachments": [_outbound_attachment("005.webp", "image/webp", "w1")],
            }
        )
        await _wait_until(lambda: len(client.api.calls) >= 2)

        uploads = _api_uploads(records)
        assert len(uploads) == 1
        body = json.loads(uploads[0].content)
        assert body["file_type"] == 1  # 图片档，不是任意文件
        assert base64.b64decode(body["file_data"]).startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_bridge_drops_the_face_placeholder_when_the_sticker_image_rides_along(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯表情包消息的正文就是 `<faceType=6,…>` 占位符（ext 的 base64 里 text 为空），
    真正的图在同一条消息的 attachments 里。占位符既不该进模型上下文，也不该出现在
    网页气泡里。"""
    media = _media_transport({"https://cdn.example/sticker.jpg": (200, b"jpg-bytes")})
    task, client = await _start_bridge(monkeypatch, media)
    try:
        message = _c2c_message(
            '<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">',
            [_image_attachment("https://cdn.example/sticker.jpg", content_type="image",
                               filename="sticker.jpg")],
        )
        await client.on_c2c_message_create(message)
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        _session_id, text, _idem, attachments = ext.sent[0]
        assert text == ""
        assert len(attachments) == 1
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_bridge_keeps_a_readable_token_for_a_face_without_an_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = _media_transport({})
    task, client = await _start_bridge(monkeypatch, media)
    try:
        await client.on_c2c_message_create(
            _c2c_message('好耶<faceType=1,faceId="13",ext="eyJ0ZXh0IjoiIn0=">')
        )
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        _session_id, text, _idem, _attachments = ext.sent[0]
        assert text == "好耶[表情]"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_pump_delivers_audio_attachment_as_voice_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`audio/*` 落平台语音那一档（file_type=3），仍以 msg_type=7 富媒体发出。

    上一条映射用例的 docstring 写了"语音 3"，断言里却只有 [1, 4]——音频这一档从未被
    喂过，而它是渠道语音回复的唯一出口。这里按群目标打，因为群里那一发还要带被动参数。
    """
    records: list[httpx.Request] = []
    group_files_url = "https://api.sgroup.qq.com/v2/groups/g1/files"
    media = _media_transport(
        {_absolute_media("v1"): (200, b"mp3-bytes"), group_files_url: (200, _upload_reply())},
        records,
    )
    task, client = await _start_bridge(monkeypatch, media)
    try:
        await client.on_group_at_message_create(_group_message("念一下", message_id="mv1"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {
                "type": "reply.final",
                "seq": 1,
                "text": "给你：早间问候.mp3",
                "attachments": [_outbound_attachment("早间问候.mp3", "audio/mpeg", "v1")],
            }
        )
        await _wait_until(lambda: len(client.api.calls) >= 2)

        uploads = _api_uploads(records)
        assert [str(request.url) for request in uploads] == [group_files_url]
        body = json.loads(uploads[0].content)
        assert body["file_type"] == 3  # 平台口径的"语音"档
        assert base64.b64decode(body["file_data"]) == b"mp3-bytes"
        assert client.api.calls == [
            (
                "post_group_message",
                {
                    "group_openid": "g1",
                    "msg_type": 7,
                    "media": {"file_info": "file-info-fd"},
                    "msg_id": "mv1",
                    "msg_seq": 1,
                },
            ),
            (
                "post_group_message",
                {
                    "group_openid": "g1",
                    "markdown": {"content": "给你：早间问候.mp3"},
                    "msg_type": 2,
                    "msg_id": "mv1",
                    "msg_seq": 2,
                },
            ),
        ]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_group_reply_replies_to_the_inbound_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """群里 @机器人后的回复带 msg_id/msg_seq 走被动回复道。不带它按平台规则算**主动
    消息**，而群目标没有这项权限：实盘每一次 POST 被判 400 / code 40034105「主动消息
    失败, 无权限」，5 次投递预算打穿后按 undeliverable 销账，形态就是"网页有完整回复、
    群里什么都没有"。"""
    task, client = await _start_bridge(monkeypatch, _media_transport({}))
    try:
        await client.on_group_at_message_create(_group_message(message_id="gm1"))
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        await ext.events.put({"type": "reply.final", "seq": 1, "text": "在。"})
        await _wait_until(lambda: client.api.calls)
        assert client.api.calls == [
            (
                "post_group_message",
                {
                    "group_openid": "g1",
                    "markdown": {"content": "在。"},
                    "msg_type": 2,
                    "msg_id": "gm1",
                    "msg_seq": 1,
                },
            )
        ]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_group_reply_falls_back_to_active_shape_outside_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """被动窗口过了就不再带 msg_id：落回主动形状（那是唯一还能试的形状），而不是拿一
    个必然被拒的窗口参数占住投递预算。"""
    monkeypatch.setattr(bridge_module, "_PASSIVE_REPLY_WINDOW_SECONDS", 0.0)
    task, client = await _start_bridge(monkeypatch, _media_transport({}))
    try:
        await client.on_group_at_message_create(_group_message(message_id="gm2"))
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        await ext.events.put({"type": "reply.final", "seq": 1, "text": "迟到的回答"})
        await _wait_until(lambda: client.api.calls)
        kwargs = client.api.calls[0][1]
        assert kwargs == {"group_openid": "g1", "markdown": {"content": "迟到的回答"}, "msg_type": 2}
        assert "msg_id" not in kwargs
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_group_reply_stops_using_passive_params_after_five_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一个 msg_id 最多回 5 次，且相同 msg_id+msg_seq 重复发送会失败——所以每一发换
    新 seq，回满 5 次后不再用被动参数。"""
    task, client = await _start_bridge(monkeypatch, _media_transport({}))
    try:
        await client.on_group_at_message_create(_group_message(message_id="gm3"))
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        for index in range(1, 7):
            await ext.events.put({"type": "reply.final", "seq": index, "text": f"r{index}"})
        await _wait_until(lambda: len(client.api.calls) >= 6)

        assert [call[1]["msg_seq"] for call in client.api.calls[:5]] == [1, 2, 3, 4, 5]
        assert all(call[1]["msg_id"] == "gm3" for call in client.api.calls[:5])
        assert "msg_id" not in client.api.calls[5][1]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_c2c_reply_keeps_the_active_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """单聊不换道是决定，不是遗漏：同一主动形状实测 66/66 发得出去，所以被动参数只给群
    聊这条本来就不通的车道，不给一条能用的车道换风险。"""
    task, client = await _start_bridge(monkeypatch, _media_transport({}))
    try:
        await client.on_c2c_message_create(_c2c_message("在吗", [], message_id="cm1"))
        ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: ext.sent)
        await ext.events.put({"type": "reply.final", "seq": 1, "text": "在。"})
        await _wait_until(lambda: client.api.calls)
        assert client.api.calls == [
            ("post_c2c_message", {"openid": "u9", "markdown": {"content": "在。"}, "msg_type": 2})
        ]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_pump_degrades_failed_attachment_to_signed_link_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """两条上传路径都失败时降级为签名下载链接文本：用户至少拿到可下载链接，
    附件不能静默丢失。"""
    files_url = "https://api.sgroup.qq.com/v2/users/u9/files"
    media = _media_transport(
        {
            _absolute_media("t3"): (200, b"doc-bytes"),
            files_url: (400, b'{"code":40093001,"message":"file_data rejected"}'),
        }
    )
    task, client = await _start_bridge(monkeypatch, media)
    try:
        async def _reject(**kwargs):
            raise RuntimeError("simulated url upload rejection")

        client.api.post_c2c_file = _reject
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="f3"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {
                "type": "reply.final",
                "seq": 1,
                "text": "日报已生成：日报",
                "attachments": [_outbound_attachment("日报.docx", "application/octet-stream", "t3")],
            }
        )
        await _wait_until(lambda: client.api.calls)
        await asyncio.sleep(0.05)
        assert client.api.calls == [
            (
                "post_c2c_message",
                {
                    "openid": "u9",
                    "markdown": {"content": f"日报已生成：日报\n📎 日报.docx: {_absolute_media('t3')}"},
                    "msg_type": 2,
                },
            )
        ]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def test_media_upload_budget_derives_from_payload_size() -> None:
    """上传预算由请求体字节数推导（固定开销 + 按最低吞吐折算的传输时间），不是拍的：
    零字节只吃固定档，1MiB 加 2 秒，桥的下载封顶（20MiB 原始文件）对应的 base64
    请求体落在 68 秒档——这是带附件的那一发能占住 pump 的最长时间。"""
    budget = bridge_module._media_upload_timeout
    mib = 1024 * 1024
    assert budget(0) == bridge_module._MEDIA_UPLOAD_FIXED_BUDGET_SECONDS
    assert budget(1 * mib) == pytest.approx(17.0)
    body_for_capped_file = len(base64.b64encode(bytes(20 * mib)))
    assert budget(body_for_capped_file) == pytest.approx(68.3, abs=0.1)
    assert budget(body_for_capped_file) > budget(1 * mib)


@pytest.mark.asyncio
async def test_pump_falls_back_to_url_upload_when_upload_never_got_a_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上传连回执都没有（连接断/写超时）也照旧回退 url 路径：botpy 的 http 层在这
    种情况下只打它自己的 WARNING 就返回 None，桥侧什么都不留；换到自己的媒体 client
    之后这条失败有日志锚点，且不再静默吞掉整发附件。"""
    files_url = "https://api.sgroup.qq.com/v2/users/u9/files"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == files_url:
            raise httpx.ConnectError("simulated upload transport failure")
        return httpx.Response(200, content=b"pdf-bytes")

    task, client = await _start_bridge(monkeypatch, httpx.MockTransport(handler))
    try:
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="f4"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {
                "type": "reply.final",
                "seq": 1,
                "text": "简历已渲染：简历.pdf",
                "attachments": [_outbound_attachment("简历.pdf", "application/pdf", "t2")],
            }
        )
        await _wait_until(lambda: client.api.calls)
        assert (
            "post_c2c_file",
            {"openid": "u9", "file_type": 4, "url": _absolute_media("t2"), "srv_send_msg": False},
        ) in client.api.calls
        assert client.api.http_calls == []
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_pump_uploads_media_to_sandbox_domain_for_sandbox_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """沙盒号的文件上传打到沙盒域名：域名与鉴权头现在由桥自己从 SDK 取，
    ``is_sandbox`` 漏带就会把上传甩到正式域名上换成一个 401。"""
    records: list[httpx.Request] = []
    sandbox_files_url = "https://sandbox.api.sgroup.qq.com/v2/users/u9/files"
    media = _media_transport(
        {
            _absolute_media("t1"): (200, b"doc-bytes"),
            sandbox_files_url: (200, _upload_reply()),
        },
        records,
    )
    task, client = await _start_bridge(monkeypatch, media, sandbox=True)
    try:
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="f5"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {
                "type": "outbound.created",
                "seq": 1,
                "text": "日报",
                "external_key": "qq:c2c:u9",
                "attachments": [_outbound_attachment("日报.docx", "application/octet-stream")],
            }
        )
        await _wait_until(lambda: _api_uploads(records))
        assert str(_api_uploads(records)[0].url) == sandbox_files_url
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def test_lru_remember_evicts_oldest() -> None:
    """投递历史 LRU 有界：超限逐出最旧条目，move_to_end 刷新的条目存活。"""
    mapping: OrderedDict[str, None] = OrderedDict()
    for key in ("a", "b", "c"):
        bridge_module._lru_remember(mapping, key, limit=4)
    bridge_module._lru_remember(mapping, "a", limit=4)  # 刷新 a
    bridge_module._lru_remember(mapping, "d", limit=4)
    bridge_module._lru_remember(mapping, "e", limit=4)  # 超限，逐出最旧的 b
    assert list(mapping) == ["c", "a", "d", "e"]
    assert "b" not in mapping


@pytest.mark.asyncio
async def test_sse_stream_read_timeout_tolerates_missed_keepalives() -> None:
    """SSE 这条流不能沿用客户端默认的 30s 读超时：服务端每 SSE_HEARTBEAT_INTERVAL_SECONDS
    才发一条 ``: keep-alive``，30s 上限等于让任何一次事件循环抖动（大会话转录重写）掐断
    这条流并甩一串 ReadTimeout 栈，把真故障埋进重连噪音里。锁住"至少容得下 3 次心跳缺席"。"""
    from g3ku.qq_official.client import ExternalApiClient
    from g3ku.runtime.external_events import SSE_HEARTBEAT_INTERVAL_SECONDS

    recorded: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.update(dict(request.extensions.get("timeout") or {}))
        return httpx.Response(200, text="")

    client = ExternalApiClient("http://test", "token", transport=httpx.MockTransport(handler))
    try:
        [event async for event in client.stream_events("ext:qq-official:pump-x", last_seq=1)]
    finally:
        await client.close()

    assert recorded["read"] >= 3 * SSE_HEARTBEAT_INTERVAL_SECONDS, recorded
    assert recorded["connect"] <= 5.0, recorded


class _DrainedSessionClient(FakeClient):
    """``start(ret_coro=True)`` 交回一份"会话已经跑完"的协程。

    真实 botpy 在第一次 ``multi_run`` 把自己的 ``_session_list`` 原地取空之后就是
    这个形状：再让 botpy 自己回到 ``while not self._closed`` 去重取一次，拿到的
    是永不挂起的协程，整个 web 事件循环会被钉死。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ret_coro_seen: bool | None = None
        self.gateway_calls = 0

    async def start(self, appid, secret, ret_coro=False):
        self.started = True
        self.ret_coro_seen = ret_coro

        async def gateway():
            self.gateway_calls += 1

        if ret_coro:
            return gateway()
        await gateway()
        while True:  # 旧路径：把收尾交给 botpy 的 while，测试里等价于不返回
            await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_bridge_hands_gateway_end_back_to_the_retry_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """会话跑完必须以异常回到 ``service._run`` 的退避重连，绝不再进 botpy 的 while。"""
    _install_fake_botpy(monkeypatch, client_cls=_DrainedSessionClient)
    monkeypatch.setattr(bridge_module, "ExternalApiClient", FakeExternalApiClient)

    states: list[tuple[str, str]] = []
    with pytest.raises(RuntimeError, match="会话全部结束"):
        await asyncio.wait_for(
            bridge_module.run_qq_official_bridge(
                app_id="100",
                app_secret="sekrit",
                sandbox=False,
                token="t",
                base_url="http://127.0.0.1:1/api/v1",
                on_state=lambda state, detail: states.append((state, detail)),
            ),
            timeout=2.0,
        )

    client = FakeClient.instances[-1]
    assert client.ret_coro_seen is True, "必须走 ret_coro，否则 botpy 自己会重入 while"
    assert client.gateway_calls == 1, "一份会话列表只许跑一次"


@pytest.mark.asyncio
async def test_drained_botpy_multi_run_never_yields() -> None:
    """钉死根因的可复现面：drained 之后的 ``multi_run`` 从不让出循环。

    用真 botpy 而不是替身，替身证不了 vendor 的 ``while not self._closed``。
    """
    pytest.importorskip("botpy")
    from botpy.connection import ConnectionSession

    loop = asyncio.get_running_loop()

    async def _connect(session):
        await asyncio.sleep(0.01)

    conn = ConnectionSession(
        max_async=1, connect=_connect, dispatch=lambda *args: None, loop=loop, api=None
    )
    conn.add(
        {
            "session_id": "s1",
            "last_seq": 0,
            "intent": 1,
            "token": "t",
            "url": "ws://127.0.0.1:1",
            "shards": {"shard_id": 0, "shard_count": 1},
        }
    )

    ticks = 0
    heartbeat_stop = asyncio.Event()

    async def _heartbeat():
        nonlocal ticks
        while not heartbeat_stop.is_set():
            ticks += 1
            await asyncio.sleep(0)

    heartbeat = asyncio.create_task(_heartbeat())
    try:
        await conn.multi_run(0)
        assert ticks > 0, "首跑要连接并 sleep，否则这组对比没有意义"
        assert conn._session_list == [], "multi_run 会原地 pop 空自己的会话列表"

        ticks = 0
        await conn.multi_run(0)
        assert ticks == 0, "drained 之后仍会让出循环的话，忙等就不成立"
    finally:
        heartbeat_stop.set()
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


class _SharedHubReplayClient(FakeExternalApiClient):
    """服务端事件缓冲跨桥重建存活（hub 长在 web 进程里），而每轮登录都 new 一个新
    客户端实例，所以重放语义必须挂在类上：全部实例共用同一份缓冲与同一套 seq 判据。"""

    feed: list[dict] = []
    connect_last_seqs: list[int] = []

    async def stream_events(self, session_id: str, last_seq: int = 0):
        type(self).connect_last_seqs.append(int(last_seq or 0))
        seen = int(last_seq or 0)
        while True:
            for event in list(self.feed):
                seq = int(event.get("seq") or 0)
                if seq > seen:
                    yield event
                    seen = seq
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_bridge_relogin_resumes_from_last_delivered_seq(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归（实盘 2026-10-12 03:59:50）：QQ 网关到期重登后，历史回答与新回答一起
    被重复推送。桥重建不得抹掉投递游标与去重集——服务端事件缓冲跨重登存活，新
    pump 从 ``last_seq=0`` 起就把已 ack 的回复再投一遍，而账本的 ack 墓碑每小时随
    compact 落盘，两边都拿不到「这条送过了」的证据。"""
    monkeypatch.setattr(bridge_module, "_PUMP_RECONNECT_INITIAL_BACKOFF_SECONDS", 0.01)

    api = FakeBotApi()
    delivered: list[str] = []

    async def _record_post_c2c(**kwargs):
        delivered.append(_outbound_text(kwargs))
        return {"id": "mid-1"}

    api.post_c2c_message = _record_post_c2c

    class _SharedApiFakeClient(FakeClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.api = api

    _SharedHubReplayClient.feed = [
        {
            "type": "reply.final",
            "seq": 1,
            "turn_id": "t1",
            "text": "上一轮的回答",
            "outbox_id": "obx-old",
        }
    ]
    _SharedHubReplayClient.connect_last_seqs = []

    _install_fake_botpy(monkeypatch, client_cls=_SharedApiFakeClient)
    monkeypatch.setattr(bridge_module, "ExternalApiClient", _SharedHubReplayClient)
    monkeypatch.setattr(
        bridge_module,
        "_create_media_client",
        lambda: httpx.AsyncClient(transport=_media_transport({})),
    )

    async def _login():
        # 每轮登录都会 new 一份 botpy Client 与一份 /api/v1 客户端：必须等"新出现的那
        # 一个"就绪，否则第二次登录会立刻拿到上一轮已经 started 的替身，测的是僵尸句柄。
        before = len(FakeClient.instances)
        task = asyncio.create_task(
            bridge_module.run_qq_official_bridge(
                app_id="100",
                app_secret="sekrit",
                sandbox=False,
                token="t",
                base_url="http://127.0.0.1:1/api/v1",
                on_state=lambda state, detail: None,
            ),
            name="test-qq-bridge-relogin",
        )
        await _wait_until(lambda: len(FakeClient.instances) > before and FakeClient.instances[-1].started)
        return task, FakeClient.instances[-1]

    first_task, first_client = await _login()
    try:
        await first_client.on_c2c_message_create(_c2c_message("第一条", [], message_id="rl-1"))
        first_ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: delivered == ["上一轮的回答"])
        await _wait_until(lambda: first_ext.acked == [("ext:qq-official:qq:c2c:u9", "obx-old")])
    finally:
        first_task.cancel()
        with suppress(asyncio.CancelledError):
            await first_task

    # 网关 close 4009 判会话到期 → 桥重建（约每小时一次）。缓冲里这时才多出一条
    # 网页发起的回合回复，它才是该送的那一条。
    _SharedHubReplayClient.feed.append(
        {
            "type": "reply.final",
            "seq": 2,
            "turn_id": "t2",
            "text": "这一轮的回答",
            "outbox_id": "obx-new",
        }
    )

    second_task, second_client = await _login()
    try:
        await second_client.on_c2c_message_create(_c2c_message("第二条", [], message_id="rl-2"))
        await _wait_until(lambda: delivered == ["上一轮的回答", "这一轮的回答"])
        assert _SharedHubReplayClient.connect_last_seqs == [0, 1], (
            "重登后的首连必须续在最后一次成功投递的 seq 上，否则就是在重放历史"
        )
        second_ext = FakeExternalApiClient.instances[-1]
        await _wait_until(lambda: second_ext.acked == [("ext:qq-official:qq:c2c:u9", "obx-new")])
    finally:
        second_task.cancel()
        with suppress(asyncio.CancelledError):
            await second_task


@pytest.mark.asyncio
async def test_outbound_after_text_attachment_goes_after_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """表情包附件（after_text）：正文先发、附件后发。"""
    order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/files"):
            order.append("upload")
            return httpx.Response(200, json={"file_info": "fi-1", "file_uuid": "u1", "ttl": 0})
        order.append("download")
        return httpx.Response(200, content=b"gif-bytes")

    task, client = await _start_bridge(monkeypatch, httpx.MockTransport(handler))
    try:
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="ao-1"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {
                "type": "reply.final",
                "seq": 1,
                "text": "好耶",
                "attachments": [
                    {
                        "kind": "image",
                        "name": "happy.gif",
                        "mime_type": "image/gif",
                        "url": "/api/ceo/media/original?token=t9",
                        "after_text": True,
                    }
                ],
            }
        )
        await _wait_until(lambda: any(call[1].get("msg_type") == 7 for call in client.api.calls))

        assert order == ["download", "upload"]
        calls = list(client.api.calls)
        assert calls[0] == (
            "post_c2c_message",
            {"openid": "u9", "markdown": {"content": "好耶"}, "msg_type": 2},
        )
        assert calls[1][0] == "post_c2c_message"
        assert calls[1][1]["msg_type"] == 7
        assert calls[1][1]["media"] == {"file_info": "fi-1"}
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_outbound_sticker_only_reply_sends_the_media_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯表情回合：正文为空时不补发空文本消息，只发那一条富媒体。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/files"):
            return httpx.Response(200, json={"file_info": "fi-2", "file_uuid": "u2", "ttl": 0})
        return httpx.Response(200, content=b"png-bytes")

    task, client = await _start_bridge(monkeypatch, httpx.MockTransport(handler))
    try:
        await client.on_c2c_message_create(_c2c_message("hi", [], message_id="ao-2"))
        ext = FakeExternalApiClient.instances[-1]
        await ext.events.put(
            {
                "type": "reply.final",
                "seq": 1,
                "text": "",
                "attachments": [
                    {
                        "kind": "image",
                        "name": "smug.png",
                        "mime_type": "image/png",
                        "url": "/api/ceo/media/original?token=t10",
                        "after_text": True,
                    }
                ],
            }
        )
        await _wait_until(lambda: any(call[1].get("msg_type") == 7 for call in client.api.calls))
        await asyncio.sleep(0.2)

        assert [call[1].get("msg_type") for call in client.api.calls] == [7]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
