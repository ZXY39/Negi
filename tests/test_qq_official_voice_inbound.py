"""QQ 入站语音：转成文字进正文，还是当文件转发，由 STT 是否就绪决定。

钉住的是"能力开关不能造成回退"：没启用/没就绪时语音必须仍然按今天的行为
落成 file 附件；就绪时才换成文字。另外转写失败要留下可见的说明行，
否则用户只会觉得机器人没听见。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import g3ku.qq_official.bridge as bridge
import g3ku.stt.engine as stt_engine
from g3ku.stt.engine import SttResult


def make_message(*items):
    return SimpleNamespace(
        id="msg-1",
        content="今天怎么样",
        attachments=[SimpleNamespace(**item) for item in items],
    )


VOICE = {
    "content_type": "audio/wav",
    "url": "https://example.test/voice.wav",
    "filename": "voice.wav",
    "id": "att-voice",
}
IMAGE = {
    "content_type": "image/png",
    "url": "https://example.test/pic.png",
    "filename": "pic.png",
    "id": "att-image",
}


def ready(value: bool):
    async def _stub(cfg=None):
        return value

    return _stub


def transcribes(result: SttResult):
    async def _stub(data, **kwargs):
        return result

    return _stub


@pytest.fixture(autouse=True)
def fake_download(monkeypatch: pytest.MonkeyPatch):
    async def _download(_client, _url, *, max_bytes):
        return b"payload-bytes"[:max_bytes] or b"x"

    monkeypatch.setattr(bridge, "_download_attachment_bytes", _download)


# autouse 桩会把真实现盖掉；测真下载函数本身的用例先把它换回来。
_REAL_DOWNLOAD = bridge._download_attachment_bytes


@pytest.mark.asyncio
async def test_voice_becomes_text_when_stt_is_ready(monkeypatch):
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(True))
    monkeypatch.setattr(
        stt_engine, "transcribe_bytes", transcribes(SttResult(True, text="帮我查一下昨天的任务", model="base"))
    )

    payloads, voice_lines = await bridge._collect_attachments(None, make_message(VOICE))

    assert payloads == []
    assert voice_lines == ["用户语音，机器识别结果：帮我查一下昨天的任务"]


@pytest.mark.asyncio
async def test_voice_stays_a_file_attachment_when_stt_is_off(monkeypatch):
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(False))

    payloads, voice_lines = await bridge._collect_attachments(None, make_message(VOICE))

    assert voice_lines == []
    assert [item["kind"] for item in payloads] == ["file"]
    assert payloads[0]["mime_type"] == "audio/wav"


@pytest.mark.asyncio
async def test_failed_transcription_reports_why_instead_of_disappearing(monkeypatch):
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(True))
    monkeypatch.setattr(
        stt_engine,
        "transcribe_bytes",
        transcribes(SttResult(False, error_code="stt_silent", error="没有录到声音")),
    )

    payloads, voice_lines = await bridge._collect_attachments(None, make_message(VOICE))

    assert payloads == []
    assert voice_lines == ["用户语音，机器识别失败：没有录到声音"]


@pytest.mark.asyncio
async def test_images_are_untouched_by_the_voice_lane(monkeypatch):
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(True))

    payloads, voice_lines = await bridge._collect_attachments(None, make_message(IMAGE))

    assert voice_lines == []
    assert [item["kind"] for item in payloads] == ["image"]


@pytest.mark.asyncio
async def test_mixed_message_keeps_image_and_voice_separately(monkeypatch):
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(True))
    monkeypatch.setattr(
        stt_engine,
        "transcribe_bytes",
        transcribes(SttResult(True, text="口述内容", model="base", seconds=2.0, wall_ms=120)),
    )

    payloads, voice_lines = await bridge._collect_attachments(None, make_message(IMAGE, VOICE))

    assert [item["kind"] for item in payloads] == ["image"]
    assert voice_lines == ["用户语音，机器识别结果：口述内容"]


@pytest.mark.asyncio
async def test_voice_without_url_is_skipped(monkeypatch):
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(True))
    called = []

    async def _record(*args, **kwargs):
        called.append(1)
        return SttResult(True, text="x")

    monkeypatch.setattr(stt_engine, "transcribe_bytes", _record)
    message = make_message({**VOICE, "url": ""})

    payloads, voice_lines = await bridge._collect_attachments(None, message)

    assert payloads == []
    assert voice_lines == []
    assert not called


@pytest.mark.asyncio
async def test_unfetchable_voice_still_reaches_the_user(monkeypatch):
    """纯语音消息在附件下载失败时，正文与附件都是空的，on_incoming 会早退——
    用户端表现为"发了语音然后什么都没有"。这条必须留下一行可回复的失败说明。"""
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(True))

    async def _dead(_client, _url, *, max_bytes):
        return None

    monkeypatch.setattr(bridge, "_download_attachment_bytes", _dead)

    payloads, voice_lines = await bridge._collect_attachments(None, make_message(VOICE))

    assert payloads == []
    assert voice_lines == ["用户语音，机器识别失败：语音附件下载失败"]


@pytest.mark.asyncio
async def test_unfetchable_voice_without_stt_keeps_the_old_degrade(monkeypatch):
    """开关关掉时不新增任何可见文本：今天附件取不到就是静默降级，这条不许被顺带改掉。"""
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(False))

    async def _dead(_client, _url, *, max_bytes):
        return None

    monkeypatch.setattr(bridge, "_download_attachment_bytes", _dead)

    payloads, voice_lines = await bridge._collect_attachments(None, make_message(VOICE))

    assert payloads == []
    assert voice_lines == []


class _FlakyResponse:
    status_code = 200

    async def aiter_bytes(self, _chunk_size):
        yield b"voice-bytes"


class _FlakyContext:
    def __init__(self, *, fail: bool):
        self._fail = fail

    async def __aenter__(self):
        if self._fail:
            # 实盘就是这一型：h11 送上来的 httpx.ConnectError 可以 __str__ 为空。
            raise httpx.ConnectError("")
        return _FlakyResponse()

    async def __aexit__(self, *args):
        return False


class _FlakyClient:
    def __init__(self):
        self.calls = 0

    def stream(self, _method, _url):
        self.calls += 1
        return _FlakyContext(fail=self.calls == 1)


@pytest.mark.asyncio
async def test_attachment_download_retries_once_then_succeeds(monkeypatch):
    monkeypatch.setattr(bridge, "_download_attachment_bytes", _REAL_DOWNLOAD)
    monkeypatch.setattr(bridge, "_ATTACHMENT_DOWNLOAD_RETRY_DELAY_SECONDS", 0.0)
    client = _FlakyClient()

    data = await bridge._download_attachment_bytes(client, "https://example.test/v.mp3", max_bytes=1024)

    assert data == b"voice-bytes"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_attachment_download_gives_up_after_the_retry_budget(monkeypatch):
    monkeypatch.setattr(bridge, "_download_attachment_bytes", _REAL_DOWNLOAD)
    monkeypatch.setattr(bridge, "_ATTACHMENT_DOWNLOAD_RETRY_DELAY_SECONDS", 0.0)

    class _AlwaysFailingClient(_FlakyClient):
        def stream(self, _method, _url):
            self.calls += 1
            return _FlakyContext(fail=True)

    client = _AlwaysFailingClient()
    data = await bridge._download_attachment_bytes(client, "https://example.test/v.mp3", max_bytes=1024)

    assert data is None
    assert client.calls == bridge._ATTACHMENT_DOWNLOAD_MAX_ATTEMPTS
