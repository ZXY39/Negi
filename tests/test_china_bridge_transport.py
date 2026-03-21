from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from g3ku.china_bridge.transport import ChinaBridgeTransport
from g3ku.core.events import AgentEvent
from g3ku.core.messages import UserInputMessage
from g3ku.runtime.channel_events import build_channel_outbound_message


class _FakeRuntimeBridge:
    def __init__(self):
        self.cancel_calls: list[tuple[str, str]] = []

    async def cancel(self, session_key: str, *, reason: str = "user_cancelled") -> int:
        self.cancel_calls.append((session_key, reason))
        return 2

    async def prompt(self, *args, **kwargs):
        raise AssertionError("prompt should not be called for stop command")


class _CaptureRuntimeBridge:
    def __init__(self):
        self.calls: list[tuple[object, dict]] = []

    async def cancel(self, session_key: str, *, reason: str = "user_cancelled") -> int:
        raise AssertionError(f"cancel should not be called: {session_key} {reason}")

    async def prompt(self, message, **kwargs):
        self.calls.append((message, kwargs))
        return SimpleNamespace(output="")


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


@pytest.mark.asyncio
async def test_transport_builds_multimodal_user_message_for_channel_attachments(tmp_path):
    frames: list[dict] = []
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"fake-image-bytes")
    file_path = tmp_path / "notes.txt"
    file_path.write_text("hello from attachment", encoding="utf-8")
    runtime_bridge = _CaptureRuntimeBridge()
    app_config = SimpleNamespace(china_bridge=SimpleNamespace(send_tool_hints=False, send_progress=True))
    transport = ChinaBridgeTransport(runtime_bridge=runtime_bridge, app_config=app_config)
    transport.set_sender(lambda payload: frames.append(payload))

    await transport.handle_frame(
        {
            "type": "inbound_message",
            "event_id": "evt-2",
            "channel": "qqbot",
            "account_id": "default",
            "peer": {"kind": "user", "id": "user-1"},
            "message": {
                "id": "msg-2",
                "text": "please inspect attachments",
                "attachments": [
                    {
                        "kind": "image",
                        "path": str(image_path),
                        "file_name": "sample.png",
                        "mime_type": "image/png",
                    },
                    {
                        "kind": "file",
                        "path": str(file_path),
                        "file_name": "notes.txt",
                        "mime_type": "text/plain",
                    },
                ],
            },
            "metadata": {},
        }
    )
    await asyncio.sleep(0.05)

    assert len(runtime_bridge.calls) == 1
    message, kwargs = runtime_bridge.calls[0]
    assert isinstance(message, UserInputMessage)
    assert message.attachments == [str(image_path), str(file_path)]
    assert kwargs["session_key"] == "china:qqbot:default:dm"

    content = message.content
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "Channel attachments:" in content[0]["text"]
    assert str(image_path) in content[0]["text"]
    assert str(file_path) in content[0]["text"]
    assert any(item.get("type") == "image_url" for item in content)
    image_block = next(item for item in content if item.get("type") == "image_url")
    assert str(image_block["image_url"]["url"]).startswith("data:image/png;base64,")
    assert message.metadata["china_bridge_attachments"][0]["path"] == str(image_path)
    assert message.metadata["china_bridge_attachments"][1]["path"] == str(file_path)

    assert frames[-1]["type"] == "turn_complete"
    assert frames[-1]["event_id"] == "evt-2"


@pytest.mark.asyncio
async def test_transport_send_outbound_skips_progress_and_tool_events():
    frames: list[dict] = []
    app_config = SimpleNamespace(china_bridge=SimpleNamespace(send_tool_hints=False, send_progress=True))
    transport = ChinaBridgeTransport(runtime_bridge=_CaptureRuntimeBridge(), app_config=app_config)
    transport.set_sender(lambda payload: frames.append(payload))

    await transport.send_outbound(
        SimpleNamespace(
            channel="qqbot",
            chat_id="default:dm:user-1",
            content="tool running",
            reply_to=None,
            metadata={"_progress": True, "_china_peer_id": "user-1", "_china_account_id": "default"},
        )
    )
    await transport.send_outbound(
        SimpleNamespace(
            channel="qqbot",
            chat_id="default:dm:user-1",
            content="final answer",
            reply_to=None,
            metadata={"_china_peer_id": "user-1", "_china_account_id": "default"},
        )
    )

    assert len(frames) == 1
    assert frames[0]["payload"]["text"] == "final answer"
    assert frames[0]["payload"]["mode"] == "final"


def test_channel_event_builder_no_longer_emits_outbound_messages() -> None:
    outbound = build_channel_outbound_message(
        event=AgentEvent(type="tool_execution_update", payload={"text": "tool running"}),
        session=SimpleNamespace(state=SimpleNamespace(session_key="qqbot:demo")),
        channel="qqbot",
        chat_id="default:dm:user-1",
        run_id="run-1",
        turn_id="turn-1",
        seq=1,
        base_metadata={},
    )

    assert outbound is None
