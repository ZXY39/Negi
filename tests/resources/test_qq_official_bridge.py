"""Bridge wiring tests with a fake ``botpy`` module and a fake /api/v1 client.

The real botpy ``Client.run()`` is a blocking entry point that cannot run
inside the web runtime's live event loop; the bridge must use the async entry
(``async with client: await client.start(...)``). These fakes expose exactly
that surface, so any regression back to ``run()`` fails here with an
AttributeError instead of at runtime against the live instance.
"""

from __future__ import annotations

import asyncio
import sys
import types
from contextlib import suppress
from types import SimpleNamespace

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
        self.sent: list[tuple[str, str, str]] = []
        self.events: asyncio.Queue = asyncio.Queue()
        self.closed = False
        FakeExternalApiClient.instances.append(self)

    async def ensure_session(self, external_key: str) -> str:
        self.ensured.append(external_key)
        return f"ext:qq-official:{external_key}"

    async def send_message(self, session_id: str, text: str, idempotency_key: str = "") -> dict:
        self.sent.append((session_id, text, idempotency_key))
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
        assert ext.sent == [("ext:qq-official:qq:group:g1", "@bot hi", "qq-m1")]

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
