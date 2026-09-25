"""语音气泡的后端契约：音频附件是给人回放的素材，不是给模型的输入。

钉住三面：
1. kind 必须判成 ``audio``，否则前端把它画成文件药丸，气泡上没有播放键；
2. 模型可见面（附件说明行 + ``UserInputMessage.attachments``）必须漏掉它，
   而 ``metadata`` 必须留着它——历史回放要能再播一次；
3. ``/api/ceo/external-upload-file`` 是本期新增的读文件车道，越界判定必须比
   "路径字符串看着像"更严。
"""

from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime.api import external_v1 as ext_api
from g3ku.runtime.api import websocket_ceo as ws_api

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
