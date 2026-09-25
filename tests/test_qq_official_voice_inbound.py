"""QQ 入站语音：转成文字进正文，还是当文件转发，由 STT 是否就绪决定。

钉住的是"能力开关不能造成回退"：没启用/没就绪时语音必须仍然按今天的行为
落成 file 附件；就绪时才换成文字。另外转写失败要留下可见的说明行，
否则用户只会觉得机器人没听见。
"""

from __future__ import annotations

from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_voice_becomes_text_when_stt_is_ready(monkeypatch):
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(True))
    monkeypatch.setattr(
        stt_engine, "transcribe_bytes", transcribes(SttResult(True, text="帮我查一下昨天的任务", model="base"))
    )

    payloads, voice_lines = await bridge._collect_attachments(None, make_message(VOICE))

    assert payloads == []
    assert voice_lines == ["[语音转文字] 帮我查一下昨天的任务"]


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
    assert voice_lines == ["[语音转文字] [未能识别：没有录到声音]"]


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
    assert voice_lines == ["[语音转文字] 口述内容"]


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
