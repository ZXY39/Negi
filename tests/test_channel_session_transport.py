from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from g3ku.china_bridge.transport import ChinaBridgeTransport


class _ReplyingRuntimeBridge:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def cancel(self, session_key: str, *, reason: str = "user_cancelled") -> int:
        raise AssertionError(f"cancel should not be called: {session_key} {reason}")

    async def prompt(self, message, **kwargs):
        self.calls.append({"message": message, "kwargs": dict(kwargs)})
        return SimpleNamespace(output="final answer")


@pytest.mark.asyncio
async def test_channel_transport_delivers_runtime_reply_without_message_tool() -> None:
    frames: list[dict] = []
    runtime_bridge = _ReplyingRuntimeBridge()
    app_config = SimpleNamespace(china_bridge=SimpleNamespace(send_tool_hints=False, send_progress=True))
    transport = ChinaBridgeTransport(runtime_bridge=runtime_bridge, app_config=app_config)
    transport.set_sender(lambda payload: frames.append(payload))

    await transport.handle_frame(
        {
            "type": "inbound_message",
            "event_id": "evt-transport-1",
            "channel": "qqbot",
            "account_id": "default",
            "peer": {"kind": "user", "id": "user-1"},
            "message": {"id": "msg-1", "text": "hello", "attachments": []},
            "metadata": {},
        }
    )
    await asyncio.sleep(0.05)

    assert len(runtime_bridge.calls) == 1
    prompt_call = runtime_bridge.calls[0]
    assert prompt_call["kwargs"]["session_key"] == "china:qqbot:default:dm"
    assert prompt_call["kwargs"]["chat_id"] == "default:dm:user-1"

    assert len(frames) == 2
    assert frames[0]["type"] == "deliver_message"
    assert frames[0]["payload"]["text"] == "final answer"
    assert frames[0]["payload"]["mode"] == "final"
    assert frames[1]["type"] == "turn_complete"
    assert frames[1]["event_id"] == "evt-transport-1"
