from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from g3ku.china_bridge.transport import ChinaBridgeTransport


class _FakeRuntimeBridge:
    def __init__(self):
        self.cancel_calls: list[tuple[str, str]] = []

    async def cancel(self, session_key: str, *, reason: str = "user_cancelled") -> int:
        self.cancel_calls.append((session_key, reason))
        return 2

    async def prompt(self, *args, **kwargs):
        raise AssertionError("prompt should not be called for stop command")


@pytest.mark.asyncio
async def test_transport_handles_stop_command_without_prompt():
    frames: list[dict] = []
    app_config = SimpleNamespace(china_bridge=SimpleNamespace(send_tool_hints=False, send_progress=True))
    transport = ChinaBridgeTransport(runtime_bridge=_FakeRuntimeBridge(), app_config=app_config)
    transport.set_sender(lambda payload: frames.append(payload))

    await transport.handle_frame(
        {
            "type": "inbound_message",
            "event_id": "evt-1",
            "channel": "qqbot",
            "account_id": "default",
            "peer": {"kind": "user", "id": "user-1"},
            "message": {"id": "msg-1", "text": "/stop", "attachments": []},
            "metadata": {},
        }
    )
    await asyncio.sleep(0.05)

    assert frames[0]["type"] == "deliver_message"
    assert frames[0]["payload"]["text"] == "Stopped 2 task(s)."
    assert frames[1]["type"] == "turn_complete"
    assert frames[1]["event_id"] == "evt-1"
