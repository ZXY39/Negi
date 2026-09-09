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
import sys
import types
from contextlib import suppress
from types import SimpleNamespace

import httpx
import pytest

from g3ku.qq_official import bridge as bridge_module


class FakeBotApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def post_message(self, **kwargs):
        self.calls.append(("post_message", kwargs))

    async def post_group_message(self, **kwargs):
        self.calls.append(("post_group_message", kwargs))

    async def post_c2c_message(self, **kwargs):
        self.calls.append(("post_c2c_message", kwargs))

    async def post_dms(self, **kwargs):
        self.calls.append(("post_dms", kwargs))


class FakeClient:
    """botpy.Client stand-in exposing only the async entry the bridge may use."""

    instances: list["FakeClient"] = []

    def __init__(self, intents=None, is_sandbox=False, ext_handlers=True, **kwargs) -> None:
        self.intents = intents
        self.is_sandbox = is_sandbox
        self.ext_handlers = ext_handlers
        self.api = FakeBotApi()
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
        self.sent: list[tuple[str, str, str, list]] = []
        self.events: asyncio.Queue = asyncio.Queue()
        self.closed = False
        FakeExternalApiClient.instances.append(self)

    async def ensure_session(self, external_key: str) -> str:
        self.ensured.append(external_key)
        return f"ext:qq-official:{external_key}"

    async def send_message(
        self,
        session_id: str,
        text: str,
        *,
        idempotency_key: str = "",
        attachments: list | None = None,
    ) -> dict:
        self.sent.append((session_id, text, idempotency_key, list(attachments or [])))
        return {"ok": True, "turn_id": "t1"}

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
    yield
    FakeClient.instances.clear()
    FakeExternalApiClient.instances.clear()


def _install_fake_botpy(monkeypatch: pytest.MonkeyPatch, intents_cls=FakeIntents) -> None:
    fake_botpy = types.ModuleType("botpy")
    fake_botpy.Client = FakeClient
    fake_botpy.Intents = intents_cls
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
            ("post_c2c_message", {"openid": "u1", "content": "提醒", "msg_type": 0})
        ]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert FakeExternalApiClient.instances[-1].closed is True


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


def _media_transport(status_by_url: dict[str, tuple[int, bytes]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        status, body = status_by_url.get(str(request.url), (404, b""))
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


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
            sandbox=False,
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
async def test_bridge_skips_non_image_and_relative_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
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
    oversized = b"x" * (bridge_module._MAX_INBOUND_ATTACHMENT_BYTES + 1)
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
        assert len(ext.sent[0][3]) == bridge_module._MAX_INBOUND_IMAGE_ATTACHMENTS
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
        idempotency_key: str = "",
        attachments: list | None = None,
    ) -> dict:
        self.sent.append((session_id, text, idempotency_key, list(attachments or [])))
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
            ("post_c2c_message", {"openid": "u9", "content": "自定义排队回执", "msg_type": 0}),
            (
                "post_c2c_message",
                {"openid": "u9", "content": bridge_module._QUEUED_RECEIPT_FALLBACK_TEXT, "msg_type": 0},
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
            ("post_c2c_message", {"openid": "u9", "content": "重连补投", "msg_type": 0})
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
        content = str(kwargs.get("content") or "")
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
        ext.feed.append({"type": "outbound.created", "seq": 1, "text": "poison", "external_key": "qq:c2c:u9"})
        await _wait_until(lambda: attempts.get("poison", 0) >= 2)
        ext.feed.append({"type": "outbound.created", "seq": 2, "text": "正常补投", "external_key": "qq:c2c:u9"})
        await _wait_until(lambda: delivered)
        # 毒消息达到上限即放弃，不再无限重试；后续消息正常投递。
        assert attempts["poison"] == 2
        assert delivered == [
            ("post_c2c_message", {"openid": "u9", "content": "正常补投", "msg_type": 0})
        ]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
