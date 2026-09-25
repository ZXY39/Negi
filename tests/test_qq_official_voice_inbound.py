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
# 实盘形状（2026-09-25 18:05:42 的 INFO 日志）：QQ 给的是类别词 'voice'，不是 MIME。
# 早先用 "audio/wav" 写测试，所以判据错了也全绿。
QQ_REAL_VOICE = {
    "content_type": "voice",
    "url": "https://multimedia.nt.qq.com.cn/download?appid=1402&fileid=x",
    "filename": "",
    "id": "att-qq-voice",
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
async def test_real_qq_category_label_voice_reaches_the_transcription_lane(monkeypatch):
    """QQ 的 content_type 是 'voice'，`startswith('audio/')` 永不命中——实盘因此把
    语音当文件转发，转写从来没跑过。"""
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(True))
    seen = {}

    async def _capture(data, **kwargs):
        seen.update(kwargs)
        return SttResult(True, text="刚刚给你发了啥", model="base", seconds=4.34, wall_ms=8000)

    monkeypatch.setattr(stt_engine, "transcribe_bytes", _capture)

    payloads, voice_lines = await bridge._collect_attachments(None, make_message(QQ_REAL_VOICE))

    assert payloads == []
    assert voice_lines == ["用户语音，机器识别结果：刚刚给你发了啥"]
    # 'voice' 不是 MIME，不能原样声明给服务端，也不能拿它猜扩展名（会得到 .png）。
    assert seen["mime_type"] == ""
    assert not seen["filename"].endswith(".png")


@pytest.mark.asyncio
async def test_silk_bytes_override_a_wrong_or_missing_label(monkeypatch):
    """标签说不是语音、字节是腾讯 silk 时，仍要走转写：语音条的真实容器只有字节可信。"""
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(True))
    monkeypatch.setattr(stt_engine, "is_voice_payload", lambda data: True)
    called = []

    async def _record(data, **kwargs):
        called.append(1)
        return SttResult(True, text="按字节认出来的", model="base")

    monkeypatch.setattr(stt_engine, "transcribe_bytes", _record)
    message = make_message({**QQ_REAL_VOICE, "content_type": "file"})

    payloads, voice_lines = await bridge._collect_attachments(None, message)

    assert called == [1]
    assert payloads == []
    assert voice_lines == ["用户语音，机器识别结果：按字节认出来的"]


@pytest.mark.asyncio
async def test_voice_without_stt_is_not_named_like_an_image(monkeypatch):
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(False))

    payloads, voice_lines = await bridge._collect_attachments(None, make_message(QQ_REAL_VOICE))

    assert voice_lines == []
    assert payloads[0]["kind"] == "file"
    assert payloads[0]["mime_type"] == "application/octet-stream"
    assert not payloads[0]["name"].endswith(".png")


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


DECODED_WAV = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 32


@pytest.mark.asyncio
async def test_transcribed_voice_also_ships_a_playable_clip(monkeypatch):
    """气泡要能回放，所以转写成功时得把解码后的 WAV 一起交出去——注意交出去的
    不能是收到的字节：QQ 语音条是腾讯 silk，浏览器播不了。"""
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(True))
    monkeypatch.setattr(
        stt_engine,
        "transcribe_bytes",
        transcribes(SttResult(True, text="刚刚给你发了啥", model="base", wav_bytes=DECODED_WAV)),
    )

    payloads, voice_lines = await bridge._collect_attachments(None, make_message(QQ_REAL_VOICE))

    assert voice_lines == ["用户语音，机器识别结果：刚刚给你发了啥"]
    assert [item["kind"] for item in payloads] == ["audio"]
    import base64 as _b64

    assert _b64.b64decode(payloads[0]["data_base64"]) == DECODED_WAV
    assert payloads[0]["mime_type"] == "audio/wav"
    # 语音条被命名成 .png 会让前端把它画成图片；扩展名必须跟着解码后的容器走。
    assert payloads[0]["name"].endswith(".wav")
    assert not payloads[0]["name"].endswith(".png")


@pytest.mark.asyncio
async def test_voice_lane_without_a_decoded_clip_stays_text_only(monkeypatch):
    """老行为不能回退：引擎没给 WAV（比如解码失败）时就只有转写行，不产生空附件。"""
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(True))
    monkeypatch.setattr(
        stt_engine,
        "transcribe_bytes",
        transcribes(SttResult(True, text="只有文字", model="base")),
    )

    payloads, voice_lines = await bridge._collect_attachments(None, make_message(VOICE))

    assert payloads == []
    assert voice_lines == ["用户语音，机器识别结果：只有文字"]


@pytest.mark.asyncio
async def test_failed_voice_ships_no_clip(monkeypatch):
    monkeypatch.setattr(stt_engine, "inbound_voice_enabled", ready(True))
    monkeypatch.setattr(
        stt_engine,
        "transcribe_bytes",
        transcribes(SttResult(False, error_code="stt_failed", error="引擎退出码 1", wav_bytes=DECODED_WAV)),
    )

    payloads, voice_lines = await bridge._collect_attachments(None, make_message(VOICE))

    assert payloads == []
    assert voice_lines == ["用户语音，机器识别失败：引擎退出码 1"]


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
