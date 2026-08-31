from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import g3ku.china_bridge.transport as transport_module
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

    def get_existing_session(self, session_key: str):
        return None

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


@pytest.mark.asyncio
async def test_transport_send_outbound_raises_when_sender_missing():
    app_config = SimpleNamespace(china_bridge=SimpleNamespace(send_tool_hints=False, send_progress=True))
    transport = ChinaBridgeTransport(runtime_bridge=_CaptureRuntimeBridge(), app_config=app_config)
    # No set_sender(...): bus-driven outbound must fail loudly so the drain
    # loop retries instead of silently dropping the message.
    with pytest.raises(RuntimeError):
        await transport.send_outbound(
            SimpleNamespace(
                channel="qqbot",
                chat_id="default:dm:user-1",
                content="reminder",
                reply_to=None,
                metadata={},
            )
        )


def test_sanitize_channel_outbound_text_truncates_session_events_marker():
    from g3ku.china_bridge.protocol import sanitize_channel_outbound_text

    text = "Visible reply.\n[SESSION EVENTS]\n## EVENT BUNDLE\ninternal stuff"
    assert sanitize_channel_outbound_text(text) == "Visible reply."


def test_sanitize_channel_outbound_text_internal_only_returns_empty():
    from g3ku.china_bridge.protocol import sanitize_channel_outbound_text

    assert sanitize_channel_outbound_text("[SESSION EVENTS]\ninternal") == ""


def test_sanitize_channel_outbound_text_removes_runtime_tool_contract_echo():
    from g3ku.china_bridge.protocol import sanitize_channel_outbound_text

    contract = (
        "## Runtime Tool Contract\n"
        "kind: frontdoor_runtime_tool_contract\n"
        "callable_tools: `exec`"
    )
    assert sanitize_channel_outbound_text(contract) == ""
    assert sanitize_channel_outbound_text("Visible answer\n\n" + contract) == "Visible answer"


@pytest.mark.asyncio
async def test_transport_send_outbound_skips_message_that_sanitizes_to_empty():
    frames: list[dict] = []
    app_config = SimpleNamespace(china_bridge=SimpleNamespace(send_tool_hints=False, send_progress=True))
    transport = ChinaBridgeTransport(runtime_bridge=_CaptureRuntimeBridge(), app_config=app_config)
    transport.set_sender(lambda payload: frames.append(payload))

    # Must return normally (not raise) so the drain loop acks the message.
    await transport.send_outbound(
        SimpleNamespace(
            channel="qqbot",
            chat_id="default:dm:user-1",
            content="[SESSION EVENTS]\ninternal only\n",
            reply_to=None,
            metadata={"_china_peer_id": "user-1", "_china_account_id": "default"},
        )
    )

    assert frames == []


@pytest.mark.asyncio
async def test_transport_deliver_truncates_session_events_from_final_reply():
    bridge = _ScriptedRuntimeBridge(
        prompt_result=SimpleNamespace(output="The real result text.\n[SESSION EVENTS]\ninternal")
    )
    transport, frames = _make_transport(bridge)

    await transport.handle_frame(_inbound_frame(event_id="evt-strip", text="hello"))
    await _wait_for_terminal(frames)

    final_frames = [
        frame
        for frame in frames
        if frame["type"] == "deliver_message" and frame["payload"].get("mode") == "final"
    ]
    assert [frame["payload"]["text"] for frame in final_frames] == ["The real result text."]


def test_build_deliver_frame_returns_none_for_internal_only_text():
    from g3ku.china_bridge.protocol import build_deliver_frame

    frame = build_deliver_frame(
        event_id="evt",
        delivery_id="d1",
        channel="qqbot",
        account_id="default",
        target_kind="user",
        target_id="user-1",
        text="[SESSION EVENTS]\ninternal\n",
        mode="final",
    )
    assert frame is None

    normal = build_deliver_frame(
        event_id="evt",
        delivery_id="d2",
        channel="qqbot",
        account_id="default",
        target_kind="user",
        target_id="user-1",
        text="hello there",
        mode="final",
    )
    assert normal is not None
    assert normal["payload"]["text"] == "hello there"


@pytest.mark.asyncio
async def test_transport_turn_error_frame_hides_raw_exception():
    bridge = _ScriptedRuntimeBridge(
        prompt_side_effect=RuntimeError("Cannot operate on a closed database")
    )
    transport, frames = _make_transport(bridge)

    await transport.handle_frame(_inbound_frame(event_id="evt-err", text="hello"))
    await _wait_for_terminal(frames)

    error_frames = [frame for frame in frames if frame["type"] == "turn_error"]
    assert len(error_frames) == 1
    frame = error_frames[0]
    # The user-visible error must be the friendly text, not the raw exception.
    assert "closed database" not in str(frame["error"])
    assert frame["error"] == transport_module.TURN_FAILED_FRIENDLY_TEXT
    # The raw exception is preserved for troubleshooting in ``detail``.
    assert "Cannot operate on a closed database" in str(frame["detail"])


def test_build_turn_error_frame_omits_detail_when_empty():
    from g3ku.china_bridge.protocol import build_turn_error_frame

    frame = build_turn_error_frame(event_id="evt", error="something went wrong")
    assert frame["error"] == "something went wrong"
    assert "detail" not in frame

    with_detail = build_turn_error_frame(event_id="evt", error="oops", detail="raw traceback")
    assert with_detail["detail"] == "raw traceback"


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


# ── Phase 0-3：终止帧泄漏修复 / 暂停 / 运行中注入 / 排空循环 / 过程信息流 ──


class _ScriptedRuntimeBridge:
    def __init__(
        self,
        *,
        session=None,
        prompt_result=None,
        prompt_batch_result=None,
        prompt_side_effect=None,
        pause_result: int = 0,
    ):
        self.session = session
        self.prompt_result = (
            prompt_result if prompt_result is not None else SimpleNamespace(output="")
        )
        self.prompt_batch_result = (
            prompt_batch_result
            if prompt_batch_result is not None
            else SimpleNamespace(output="")
        )
        self.prompt_side_effect = prompt_side_effect
        self.pause_result = pause_result
        self.cancel_calls: list[tuple[str, str]] = []
        self.pause_calls: list[tuple[str, bool]] = []
        self.prompt_calls: list[tuple[object, dict]] = []
        self.prompt_batch_calls: list[tuple[list, dict]] = []

    async def cancel(self, session_key: str, *, reason: str = "user_cancelled") -> int:
        self.cancel_calls.append((session_key, reason))
        return 2

    async def pause(self, session_key: str, *, manual: bool = True) -> int:
        self.pause_calls.append((session_key, manual))
        return self.pause_result

    def get_existing_session(self, session_key: str):
        return self.session

    async def prompt(self, message, **kwargs):
        self.prompt_calls.append((message, kwargs))
        if self.prompt_side_effect is not None:
            raise self.prompt_side_effect
        return self.prompt_result

    async def prompt_batch(self, messages, **kwargs):
        self.prompt_batch_calls.append((list(messages), kwargs))
        return self.prompt_batch_result


class _FakeSession:
    def __init__(self, *, is_running: bool = False, drain_batches: list[list] | None = None):
        status = "running" if is_running else "idle"
        self.state = SimpleNamespace(
            is_running=is_running,
            status=status,
            session_key="china:qqbot:default:dm",
        )
        self.queue_calls: list[tuple[list, bool]] = []
        self._drain_batches = list(drain_batches or [])
        self.archive_calls: list[set] = []

    async def queue_follow_up_batch(self, messages, *, persist_transcript: bool = True):
        self.queue_calls.append((list(messages), persist_transcript))

    def drain_queued_follow_up_messages(self):
        if self._drain_batches:
            return self._drain_batches.pop(0)
        return []

    async def archive_follow_up_chain_transition(self, *, pending_follow_up_turn_ids=None):
        self.archive_calls.append(set(pending_follow_up_turn_ids or set()))


def _make_transport(runtime_bridge) -> tuple[ChinaBridgeTransport, list[dict]]:
    frames: list[dict] = []
    app_config = SimpleNamespace(
        china_bridge=SimpleNamespace(send_tool_hints=False, send_progress=True)
    )
    transport = ChinaBridgeTransport(runtime_bridge=runtime_bridge, app_config=app_config)
    transport.set_sender(lambda payload: frames.append(payload))
    return transport, frames


def _inbound_frame(
    *, event_id: str, text: str, channel: str = "qqbot", message_id: str = "msg-x"
) -> dict:
    return {
        "type": "inbound_message",
        "event_id": event_id,
        "channel": channel,
        "account_id": "default",
        "peer": {"kind": "user", "id": "user-1"},
        "message": {"id": message_id, "text": text, "attachments": []},
        "metadata": {},
    }


async def _wait_for_terminal(frames: list[dict]) -> None:
    for _ in range(500):
        if any(frame.get("type") in {"turn_complete", "turn_error"} for frame in frames):
            return
        await asyncio.sleep(0.002)


@pytest.mark.asyncio
async def test_transport_emits_turn_complete_when_prompt_is_cancelled():
    """Phase 0：CancelledError 穿过 except Exception 后仍必须发终止帧，
    否则宿主 pending 永不 settle、串行队列永久卡死。"""
    bridge = _ScriptedRuntimeBridge(prompt_side_effect=asyncio.CancelledError())
    transport, frames = _make_transport(bridge)

    await transport.handle_frame(_inbound_frame(event_id="evt-cancel", text="hello"))
    await _wait_for_terminal(frames)

    assert [frame["type"] for frame in frames] == ["turn_complete"]
    assert frames[0]["event_id"] == "evt-cancel"


@pytest.mark.asyncio
async def test_transport_pause_command_pauses_running_session():
    bridge = _ScriptedRuntimeBridge(pause_result=1)
    transport, frames = _make_transport(bridge)

    await transport.handle_frame(_inbound_frame(event_id="evt-pause", text="暂停"))
    await _wait_for_terminal(frames)

    assert bridge.pause_calls == [("china:qqbot:default:dm", True)]
    assert bridge.prompt_calls == []
    assert frames[0]["type"] == "deliver_message"
    assert frames[0]["payload"]["text"] == "已暂停。"
    assert frames[1]["type"] == "turn_complete"
    assert frames[1]["event_id"] == "evt-pause"


@pytest.mark.asyncio
async def test_transport_pause_command_idle_session_acknowledges_nothing_running():
    bridge = _ScriptedRuntimeBridge(pause_result=0)
    transport, frames = _make_transport(bridge)

    await transport.handle_frame(_inbound_frame(event_id="evt-pause-idle", text="/pause"))
    await _wait_for_terminal(frames)

    assert frames[0]["payload"]["text"] == "当前没有正在进行的任务。"
    assert frames[1]["type"] == "turn_complete"


@pytest.mark.asyncio
async def test_transport_normalizes_punctuated_control_commands():
    pause_bridge = _ScriptedRuntimeBridge(pause_result=1)
    pause_transport, pause_frames = _make_transport(pause_bridge)
    await pause_transport.handle_frame(_inbound_frame(event_id="evt-p1", text="暂停。"))
    await _wait_for_terminal(pause_frames)
    assert len(pause_bridge.pause_calls) == 1
    assert pause_bridge.prompt_calls == []

    stop_bridge = _ScriptedRuntimeBridge()
    stop_transport, stop_frames = _make_transport(stop_bridge)
    await stop_transport.handle_frame(_inbound_frame(event_id="evt-s1", text="停止。"))
    await _wait_for_terminal(stop_frames)
    assert stop_bridge.cancel_calls == [("china:qqbot:default:dm", "china_stop")]
    assert stop_bridge.prompt_calls == []


@pytest.mark.asyncio
async def test_transport_queues_follow_up_when_session_running():
    session = _FakeSession(is_running=True)
    bridge = _ScriptedRuntimeBridge(session=session)
    transport, frames = _make_transport(bridge)

    await transport.handle_frame(
        _inbound_frame(event_id="evt-inject", text="while you are busy")
    )
    await _wait_for_terminal(frames)

    assert len(session.queue_calls) == 1
    queued_messages, persist_transcript = session.queue_calls[0]
    assert persist_transcript is True
    assert len(queued_messages) == 1
    assert bridge.prompt_calls == []
    assert frames[0]["payload"]["text"] == "收到，将在当前任务中一并处理。"
    assert frames[-1]["type"] == "turn_complete"
    assert frames[-1]["event_id"] == "evt-inject"


@pytest.mark.asyncio
async def test_transport_drains_follow_ups_after_prompt_completes():
    drained_items = [
        SimpleNamespace(metadata={"_transcript_turn_id": "turn-a"}),
        SimpleNamespace(metadata={"_transcript_turn_id": "turn-b"}),
    ]
    session = _FakeSession(is_running=False, drain_batches=[drained_items])
    bridge = _ScriptedRuntimeBridge(
        session=session,
        prompt_result=SimpleNamespace(output="first answer"),
        prompt_batch_result=SimpleNamespace(output="second answer"),
    )
    transport, frames = _make_transport(bridge)

    await transport.handle_frame(_inbound_frame(event_id="evt-drain", text="hello"))
    await _wait_for_terminal(frames)

    assert len(bridge.prompt_calls) == 1
    assert len(bridge.prompt_batch_calls) == 1
    batch_messages, batch_kwargs = bridge.prompt_batch_calls[0]
    assert batch_messages == drained_items
    assert batch_kwargs["session_key"] == "china:qqbot:default:dm"
    assert session.archive_calls == [{"turn-a", "turn-b"}]

    deliver_texts = [
        frame["payload"]["text"]
        for frame in frames
        if frame["type"] == "deliver_message"
    ]
    assert deliver_texts == ["first answer", "second answer"]
    terminal_frames = [frame for frame in frames if frame["type"] == "turn_complete"]
    assert len(terminal_frames) == 1
    assert terminal_frames[0]["event_id"] == "evt-drain"


class _EventEmittingBridge(_ScriptedRuntimeBridge):
    async def prompt(self, message, **kwargs):
        self.prompt_calls.append((message, kwargs))
        listeners = list(kwargs.get("listeners") or [])
        for listener in listeners:
            await listener(
                AgentEvent(
                    type="tool_execution_start",
                    payload={"text": "search_web started"},
                )
            )
            await listener(
                AgentEvent(
                    type="message_delta",
                    payload={"kind": "progress", "text": "正在整理结果"},
                )
            )
        # 给节流 flush 任务时间发送（测试里把最小间隔 monkeypatch 到 10ms）。
        await asyncio.sleep(0.05)
        return SimpleNamespace(output="done")


@pytest.mark.asyncio
async def test_transport_emits_throttled_progress_frames_for_qqbot(monkeypatch):
    monkeypatch.setattr(transport_module, "QQBOT_PROGRESS_MIN_INTERVAL_SECONDS", 0.01)
    bridge = _EventEmittingBridge()
    transport, frames = _make_transport(bridge)

    await transport.handle_frame(
        _inbound_frame(event_id="evt-progress", text="go", message_id="m-progress")
    )
    await _wait_for_terminal(frames)

    progress_frames = [
        frame
        for frame in frames
        if frame["type"] == "deliver_message" and frame["payload"].get("mode") == "progress"
    ]
    assert len(progress_frames) == 1
    progress = progress_frames[0]
    assert progress["event_id"] == "evt-progress"
    assert "🔧 search_web started" in progress["payload"]["text"]
    assert "正在整理结果" in progress["payload"]["text"]
    assert progress["reply_to"] == "m-progress"
    assert progress["metadata"]["session_key"] == "china:qqbot:default:dm"
    assert progress["metadata"]["progress_kind"] == "milestone"

    final_frames = [
        frame
        for frame in frames
        if frame["type"] == "deliver_message" and frame["payload"].get("mode") == "final"
    ]
    assert [frame["payload"]["text"] for frame in final_frames] == ["done"]


@pytest.mark.asyncio
async def test_transport_non_qqbot_channel_keeps_legacy_behavior():
    # 忙碌会话不走注入分流（仅 QQ 渠道启用）。
    session = _FakeSession(is_running=True)
    bridge = _ScriptedRuntimeBridge(session=session, prompt_result=SimpleNamespace(output="ok"))
    transport, frames = _make_transport(bridge)

    await transport.handle_frame(
        _inbound_frame(event_id="evt-dt", text="hello", channel="dingtalk")
    )
    await _wait_for_terminal(frames)

    assert session.queue_calls == []
    assert len(bridge.prompt_calls) == 1
    _, prompt_kwargs = bridge.prompt_calls[0]
    assert prompt_kwargs.get("listeners") is None

    # 暂停命令也仅 QQ 渠道启用。
    pause_bridge = _ScriptedRuntimeBridge(pause_result=1)
    pause_transport, pause_frames = _make_transport(pause_bridge)
    await pause_transport.handle_frame(
        _inbound_frame(event_id="evt-dt2", text="暂停", channel="dingtalk")
    )
    await _wait_for_terminal(pause_frames)

    assert pause_bridge.pause_calls == []
    assert len(pause_bridge.prompt_calls) == 1
