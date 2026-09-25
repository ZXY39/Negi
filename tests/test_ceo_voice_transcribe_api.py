"""POST /api/ceo/transcribe 的 HTTP 契约（TestClient + 打桩引擎）。

钉住三件对外可见的事：错误以 200+error_code 回来而不是 HTTP 异常、
超限是 413 且带稳定 code、以及"录音不落盘"——文档承诺了这一点，
所以它必须是一条会被跑坏的测试。
"""

from __future__ import annotations

import struct

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime.api import websocket_ceo as ws_api
from g3ku.stt import engine as stt_engine
from g3ku.stt.engine import SttResult


def _wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    frames = int(seconds * rate)
    samples = struct.pack("<h", 0) * frames
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


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ws_api.router, prefix="/api")
    return app


@pytest.fixture
def client():
    return TestClient(_build_app())


def test_transcribe_returns_the_engine_envelope(client, monkeypatch):
    seen = {}

    async def fake_transcribe(data, *, filename="", mime_type="", source="", cfg=None):
        seen.update({"bytes": len(data), "filename": filename, "mime": mime_type, "source": source})
        return SttResult(True, text="帮我查一下昨天的任务", model="base", seconds=2.0, wall_ms=9500)

    monkeypatch.setattr(stt_engine, "transcribe_bytes", fake_transcribe)

    response = client.post(
        "/api/ceo/transcribe",
        files={"file": ("voice.wav", _wav(), "audio/wav")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["text"] == "帮我查一下昨天的任务"
    assert payload["wall_ms"] == 9500
    assert seen["filename"] == "voice.wav"
    assert seen["mime"] == "audio/wav"
    assert seen["source"] == "web-composer"
    assert seen["bytes"] > 44


def test_not_ready_is_a_200_with_a_code_not_an_http_error(client, monkeypatch):
    async def disabled(data, **kwargs):
        return SttResult(False, error_code="stt_disabled", error="语音识别未启用。", model="base")

    monkeypatch.setattr(stt_engine, "transcribe_bytes", disabled)

    response = client.post("/api/ceo/transcribe", files={"file": ("voice.wav", _wav(), "audio/wav")})

    assert response.status_code == 200
    assert response.json()["error_code"] == "stt_disabled"


def test_oversized_recording_is_rejected_before_the_engine(client, monkeypatch):
    calls = []

    async def spy(data, **kwargs):
        calls.append(len(data))
        return SttResult(True, text="x")

    monkeypatch.setattr(stt_engine, "transcribe_bytes", spy)
    oversized = _wav() + b"\x00" * (3 * 1024 * 1024)

    response = client.post("/api/ceo/transcribe", files={"file": ("voice.wav", oversized, "audio/wav")})

    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail["code"] == "voice_too_large"
    assert detail["limit_bytes"] == ws_api.WEB_CEO_VOICE_UPLOAD_MAX_BYTES
    assert calls == []


def test_transcription_writes_nothing_to_the_upload_root(client, monkeypatch, tmp_path):
    from g3ku.runtime import web_ceo_sessions as wcs

    uploads = tmp_path / "web-ceo-uploads"
    monkeypatch.setattr(wcs, "WEB_CEO_UPLOAD_ROOT", uploads)

    async def fake(data, **kwargs):
        return SttResult(True, text="好", model="base", seconds=1.0, wall_ms=10)

    monkeypatch.setattr(stt_engine, "transcribe_bytes", fake)

    client.post("/api/ceo/transcribe", files={"file": ("voice.wav", _wav(), "audio/wav")})

    assert not uploads.exists() or list(uploads.rglob("*")) == []
