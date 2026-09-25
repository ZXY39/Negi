"""语音气泡的后端契约：音频附件是给人回放的素材，不是给模型的输入。

钉住五面：
1. kind 必须判成 ``audio``，否则前端把它画成文件药丸，气泡上没有播放键；
2. 模型可见面（附件说明行 + ``UserInputMessage.attachments``）必须漏掉它，
   而 ``metadata`` 必须留着它——历史回放要能再播一次；
3. ``/api/ceo/external-upload-file`` 是新增的读文件车道，越界判定必须比
   "路径字符串看着像"更严；
4. 两条车道落盘前都把 PCM 压成 MP3，压不动就原样留 WAV（压缩不许把语音打回错误）；
5. 会话删除/清空要把两个上传根里该会话的目录一起带走。
"""

from __future__ import annotations

import base64
import io
import struct
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime import web_ceo_sessions as wcs
from g3ku.runtime.api import external_v1 as ext_api
from g3ku.runtime.api import websocket_ceo as ws_api
from g3ku.stt import audio as stt_audio
from g3ku.utils.helpers import safe_filename


def _wav_bytes(seconds: float = 1.0, rate: int = 16000) -> bytes:
    frames = int(seconds * rate)
    samples = struct.pack("<h", 3000) * frames
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(samples))
        + b"WAVEfmt "
        + struct.pack("<I", 16)
        + struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(samples))
        + samples
    )


class _FakeUpload:
    """starlette 的 ``UploadFile.file`` 是同步 SpooledTemporaryFile，
    这里按同一形状造假，避免把 await 加到同步 read 上。"""

    def __init__(self, filename: str, data: bytes, content_type: str):
        self.filename = filename
        self.content_type = content_type
        self.file = io.BytesIO(data)

WAVClip = {
    "name": "qq-voice.wav",
    "path": "C:/tmp/external-uploads/qq_ext_demo/ab12-qq-voice.wav",
    "mime_type": "audio/wav",
    "kind": "audio",
}
PNG = {
    "name": "pic.png",
    "path": "C:/tmp/web-ceo-uploads/sess/aa_pic.png",
    "mime_type": "image/png",
    "kind": "image",
}


def test_audio_mime_and_extension_both_map_to_the_audio_kind():
    assert ws_api._upload_kind(mime_type="audio/wav", name="x.bin") == "audio"
    # QQ 的 content_type 是类别词，命名只靠扩展名时必须还落在 audio，否则前端画成文件。
    assert ws_api._upload_kind(mime_type="application/octet-stream", name="voice.wav") == "audio"
    assert ws_api._upload_kind(mime_type="", name="pic.png") == "image"
    assert ws_api._upload_kind(mime_type="application/pdf", name="doc.pdf") == "file"


def test_voice_clip_is_absent_from_the_model_visible_attachment_note():
    note = ws_api._uploaded_files_note([PNG, WAVClip])
    assert "pic.png" in note
    assert "qq-voice.wav" not in note


def test_a_voice_only_turn_has_no_attachment_note_at_all():
    # 只剩标题行的说明比没有说明更糟：模型会以为有个待打开的附件。
    assert ws_api._uploaded_files_note([WAVClip]) == ""


def test_build_user_message_hides_the_clip_but_keeps_it_in_metadata():
    message = ws_api._build_user_message(
        "用户语音，机器识别结果：刚刚给你发了啥", [WAVClip, PNG]
    )
    assert message.attachments == [PNG["path"]]
    assert "qq-voice.wav" not in message.content
    kept = message.metadata["web_ceo_uploads"]
    assert [item["kind"] for item in kept] == ["audio", "image"]


def test_build_user_message_for_voice_only_sends_the_text_alone():
    message = ws_api._build_user_message("用户语音，机器识别结果：帮我订个会议室", [WAVClip])
    assert message.attachments == []
    assert message.content == "用户语音，机器识别结果：帮我订个会议室"
    assert message.metadata["web_ceo_uploads"] == [WAVClip]


def test_snapshot_lifts_channel_voice_into_a_playable_clip():
    message = {
        "role": "user",
        "content": "用户语音，机器识别结果：刚刚给你发了啥",
        "metadata": {"external_attachments": [dict(WAVClip)]},
    }
    items = ws_api._normalize_snapshot_attachments(message, "qq/ext/demo")
    assert [item["kind"] for item in items] == ["audio"]
    url = items[0]["url"]
    assert url.startswith("/api/ceo/external-upload-file?")
    assert "session_id=qq%2Fext%2Fdemo" in url
    assert "path=" in url


def test_snapshot_without_session_id_cannot_issue_a_url():
    # 读路由要 session_id 才能定边界；宁可前端画不出播放键，也不能给一条无界 URL。
    message = {"metadata": {"external_attachments": [dict(WAVClip)]}}
    assert ws_api._normalize_snapshot_attachments(message, "") == []


def test_snapshot_leaves_channel_files_out_of_the_clip_lane():
    message = {
        "metadata": {
            "external_attachments": [{**WAVClip, "kind": "file"}, {**PNG, "kind": "image"}]
        }
    }
    assert ws_api._normalize_snapshot_attachments(message, "qq/ext/demo") == []


def test_external_message_keeps_the_clip_out_of_the_note_and_refs():
    message = ext_api._build_external_user_message(
        text="用户语音，机器识别结果：刚刚给你发了啥",
        attachments=[dict(WAVClip), dict(PNG)],
        sender=None,
        metadata=None,
    )
    text_block = message.content[0]["text"]
    assert "- image: pic.png" in text_block
    assert "qq-voice.wav" not in text_block
    assert message.attachments == [PNG["path"]]
    assert [item["kind"] for item in message.metadata["external_attachments"]] == ["audio", "image"]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    def _root():
        return tmp_path

    monkeypatch.setattr(ws_api, "workspace_path", _root)
    monkeypatch.setattr(ext_api, "workspace_path", _root)
    app = FastAPI()
    app.include_router(ws_api.router, prefix="/api")
    return tmp_path, TestClient(app)


def test_external_upload_route_serves_a_stored_clip_for_its_own_session(workspace):
    tmp_path, client = workspace
    path, size = ext_api._store_base64_attachment(
        "qq/ext/demo",
        {"kind": "audio", "name": "qq-voice.wav", "mime_type": "audio/wav",
         "data_base64": base64.b64encode(b"RIFF....waveform").decode("ascii")},
        max_bytes=5 * 1024 * 1024,
    )
    assert size > 0

    response = client.get(
        "/api/ceo/external-upload-file", params={"session_id": "qq/ext/demo", "path": path}
    )

    assert response.status_code == 200
    assert response.content == b"RIFF....waveform"
    assert response.headers["content-type"].startswith("audio/")


def test_external_upload_route_refuses_another_sessions_clip(workspace):
    tmp_path, client = workspace
    path, _ = ext_api._store_base64_attachment(
        "qq/ext/demo",
        {"kind": "audio", "name": "qq-voice.wav", "mime_type": "audio/wav",
         "data_base64": base64.b64encode(b"x").decode("ascii")},
        max_bytes=1024,
    )

    response = client.get(
        "/api/ceo/external-upload-file",
        params={"session_id": "qq/ext/other", "path": path},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "upload_path_outside_session_dir"


def test_external_upload_route_refuses_a_missing_session(workspace):
    _, client = workspace
    response = client.get(
        "/api/ceo/external-upload-file",
        params={"session_id": "", "path": "anything.wav"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_session_id"


def test_external_upload_route_refuses_a_path_traversal(workspace):
    tmp_path, client = workspace
    outside = tmp_path / "private.txt"
    outside.write_text("not a clip", encoding="utf-8")
    inside = tmp_path / ".g3ku" / "external-uploads" / "qq_ext_demo"
    inside.mkdir(parents=True)
    escaped = str(inside / ".." / ".." / ".." / "private.txt")

    response = client.get(
        "/api/ceo/external-upload-file",
        params={"session_id": "qq/ext/demo", "path": escaped},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "upload_path_outside_session_dir"


def test_external_upload_route_404s_on_a_gone_file(workspace):
    tmp_path, client = workspace
    gone = tmp_path / ".g3ku" / "external-uploads" / "qq_ext_demo" / "aa-deleted.wav"

    response = client.get(
        "/api/ceo/external-upload-file",
        params={"session_id": "qq/ext/demo", "path": str(gone)},
    )

    assert response.status_code == 404


# --- 落盘前压缩 ---------------------------------------------------------


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """上传目录有两个根：web 侧挂在 ``data_root()``、渠道侧挂在 ``workspace_path()``，
    而且两侧模块各自持有这两个函数——只 patch 一个命名空间会让描述符算出
    ``relative_to`` 失败的绝对路径。"""
    from g3ku.runtime.api import external_v1 as _ext

    def _root():
        return tmp_path

    for module in (ws_api, wcs, _ext):
        monkeypatch.setattr(module, "data_root", _root, raising=False)
        monkeypatch.setattr(module, "workspace_path", _root, raising=False)
    return tmp_path


@pytest.mark.asyncio
async def test_web_voice_clip_is_stored_as_mp3(roots, monkeypatch):
    if not stt_audio.ffmpeg_available():
        pytest.skip("本机没有 ffmpeg：按设计退回 WAV，另有用例覆盖")
    wav = _wav_bytes(3.0)

    item = await ws_api._store_uploaded_file("web:probe", _FakeUpload("voice.wav", wav, "audio/wav"))

    assert item["kind"] == "audio"
    assert item["mime_type"] == "audio/mpeg"
    assert item["name"].endswith(".mp3")
    assert item["size"] < len(wav) / 4
    assert Path(item["path"]).read_bytes()[:4] != b"RIFF"


@pytest.mark.asyncio
async def test_web_voice_clip_keeps_the_wav_when_the_encoder_declines(roots, monkeypatch):
    """压缩是优化，不是前提：没有 ffmpeg 时语音必须照样能发出去。"""
    monkeypatch.setattr(stt_audio, "encode_to_mp3", lambda data, **kwargs: None)
    wav = _wav_bytes(1.0)

    item = await ws_api._store_uploaded_file("web:probe", _FakeUpload("voice.wav", wav, "audio/wav"))

    assert item["name"].endswith(".wav")
    assert item["mime_type"] == "audio/wav"
    assert Path(item["path"]).read_bytes() == wav


@pytest.mark.asyncio
async def test_web_oversized_audio_is_streamed_through_untouched(roots, monkeypatch):
    """大过语音条预算的音频不是语音条（是有人从附件口丢文件）：不读进内存、不转码。"""
    seen = []
    monkeypatch.setattr(stt_audio, "encode_to_mp3", lambda data, **kwargs: seen.append(1))

    oversized = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * (ws_api.WEB_CEO_VOICE_UPLOAD_MAX_BYTES + 8)
    item = await ws_api._store_uploaded_file("web:probe", _FakeUpload("big.wav", oversized, "audio/wav"))

    assert seen == []
    assert item["name"].endswith(".wav")
    assert item["size"] == len(oversized)


@pytest.mark.asyncio
async def test_web_image_upload_is_not_a_transcode_target(roots):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

    item = await ws_api._store_uploaded_file("web:probe", _FakeUpload("pic.png", png, "image/png"))

    assert item["kind"] == "image"
    assert item["name"].endswith("pic.png")


@pytest.mark.asyncio
async def test_channel_voice_clip_is_compressed_before_storage():
    if not stt_audio.ffmpeg_available():
        pytest.skip("本机没有 ffmpeg：按设计退回 WAV，另有用例覆盖")
    item = {
        "kind": "audio",
        "name": "qq-voice.wav",
        "mime_type": "audio/wav",
        "data_base64": base64.b64encode(_wav_bytes(3.0)).decode("ascii"),
    }

    out = await ext_api._compressed_voice_clip(item)

    assert out["name"] == "qq-voice.mp3"
    assert out["mime_type"] == "audio/mpeg"
    assert len(base64.b64decode(out["data_base64"])) < len(item["data_base64"]) / 4


@pytest.mark.asyncio
async def test_channel_lane_leaves_everything_else_untouched(monkeypatch):
    image = {"kind": "image", "name": "p.png", "mime_type": "image/png", "data_base64": "AAA="}
    assert await ext_api._compressed_voice_clip(image) is image

    not_wav = {"kind": "audio", "name": "v.mp3", "mime_type": "audio/mpeg", "data_base64": "ABCD"}
    assert await ext_api._compressed_voice_clip(not_wav) is not_wav

    too_big = {
        "kind": "audio",
        "name": "v.wav",
        "mime_type": "audio/wav",
        "data_base64": base64.b64encode(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 512).decode("ascii"),
    }
    monkeypatch.setattr(ext_api, "VOICE_CLIP_TRANSCODE_MAX_BYTES", 64)
    assert await ext_api._compressed_voice_clip(too_big) is too_big


@pytest.mark.asyncio
async def test_channel_clip_survives_a_missing_encoder(monkeypatch):
    item = {
        "kind": "audio",
        "name": "v.wav",
        "mime_type": "audio/wav",
        "data_base64": base64.b64encode(_wav_bytes(1.0)).decode("ascii"),
    }
    monkeypatch.setattr(stt_audio, "encode_to_mp3", lambda data, **kwargs: None)

    assert await ext_api._compressed_voice_clip(item) is item


# --- 会话删除连带清目录 -------------------------------------------------


def _seed_upload_dirs(root: Path, slug: str) -> tuple[Path, Path]:
    web_dir = root / ".g3ku" / "web-ceo-uploads" / slug
    external_dir = root / ".g3ku" / "external-uploads" / slug
    for directory in (web_dir, external_dir):
        directory.mkdir(parents=True)
        (directory / "clip.mp3").write_bytes(b"x")
    return web_dir, external_dir


def test_session_clear_removes_both_upload_roots(roots):
    key = "ext:qq-official-1:abc"
    slug = safe_filename(key)
    web_dir, external_dir = _seed_upload_dirs(roots, slug)
    neighbour, _ = _seed_upload_dirs(roots, "other_session")

    wcs.clear_web_ceo_session_artifacts(session_id=key)

    assert not web_dir.exists()
    assert not external_dir.exists()
    assert neighbour.exists()


def test_empty_session_id_never_wipes_the_external_root(roots):
    """``safe_filename('')`` 是空串，指向的是 external-uploads 根本身——
    一次误清空就会带走所有渠道会话的附件。"""
    _seed_upload_dirs(roots, "someone")

    wcs.clear_web_ceo_session_artifacts(session_id="")

    assert (roots / ".g3ku" / "external-uploads" / "someone" / "clip.mp3").exists()
