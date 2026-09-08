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


async def _start_bridge(monkeypatch: pytest.MonkeyPatch, media_transport: httpx.MockTransport):
    _install_fake_botpy(monkeypatch)
    monkeypatch.setattr(bridge_module, "ExternalApiClient", FakeExternalApiClient)
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
