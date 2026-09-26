"""首次点麦克风时的按需下载车道（/api/ceo/voice/status 与 /prepare）。

新设备默认就开着语音，但缺那 157 MB：这里钉住的是"点一次就下、下完自己好"
的对外契约——单飞（第二个点击不许重开一个下载）、进度可见、失败有原因，
以及状态口只报布尔与字节，不把绝对路径漏给浏览器。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime.api import websocket_ceo as ws_api
from g3ku.stt import engine as stt_engine


@pytest.fixture(autouse=True)
def clean_state():
    ws_api._VOICE_PREPARE.update({"state": "idle", "stage": "", "done_bytes": 0, "total_bytes": None, "error": ""})
    ws_api._voice_prepare_task = None
    yield
    ws_api._voice_prepare_task = None


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ws_api.router, prefix="/api")
    return TestClient(app)


def _not_ready(monkeypatch, *, enabled=True):
    def fake_status(cfg=None):
        return {
            "enabled": enabled,
            "ready": False,
            "binary_present": False,
            "model_present": False,
            "model": "base",
        }

    monkeypatch.setattr(stt_engine, "status", fake_status)
    monkeypatch.setattr(
        stt_engine, "download_plan", lambda cfg=None: {"binary_bytes": 8573270, "model_bytes": 147951465}
    )


def test_status_reports_gates_sizes_and_progress_without_paths(client, monkeypatch):
    _not_ready(monkeypatch)

    body = client.get("/api/ceo/voice/status").json()

    assert body["ready"] is False
    assert body["enabled"] is True
    assert body["download"] == {"binary_bytes": 8573270, "model_bytes": 147951465}
    assert body["prepare"]["state"] == "idle"
    assert "binary_path" not in body and "model_path" not in body


def test_prepare_on_a_ready_box_starts_nothing(client, monkeypatch):
    monkeypatch.setattr(stt_engine, "status", lambda cfg=None: {"enabled": True, "ready": True})
    calls = []
    monkeypatch.setattr(stt_engine, "prepare_binary", lambda *a, **k: calls.append("binary"))

    body = client.post("/api/ceo/voice/prepare").json()

    assert body["state"] == "ready"
    assert calls == []


@pytest.mark.asyncio
async def test_run_voice_prepare_walks_both_parts_and_records_progress(monkeypatch):
    """下载顺序与台账：二进制先、模型后，进度按阶段回写。

    这里直接调协程而不是走 TestClient——TestClient 每个请求跑在自己的 loop 上，
    后台任务会随请求结束一起被切掉，测不到"跨请求还在下"。
    """
    order = []
    seen = []

    def fake_prepare_binary(cfg=None, on_progress=None):
        order.append("binary")
        if on_progress:
            on_progress("binary", 4_000_000, 8_573_270)
        return {"ok": True}

    def fake_prepare_model(cfg=None, on_progress=None):
        order.append("model")
        if on_progress:
            on_progress("model", 147951465, 147951465)
        return {"ok": True}

    monkeypatch.setattr(stt_engine, "prepare_binary", fake_prepare_binary)
    monkeypatch.setattr(stt_engine, "prepare_model", fake_prepare_model)

    await ws_api._run_voice_prepare()

    assert order == ["binary", "model"]
    snapshot = ws_api._voice_prepare_snapshot()
    assert snapshot["state"] == "ready"
    assert snapshot["stage"] == "model"
    assert snapshot["done_bytes"] == 147951465
    assert seen == []


@pytest.mark.asyncio
async def test_a_second_click_reuses_the_running_download(monkeypatch):
    started = []

    async def slow_prepare(cfg=None, on_progress=None):
        started.append(1)
        await asyncio.sleep(5)

    monkeypatch.setattr(stt_engine, "status", lambda cfg=None: {"enabled": True, "ready": False})
    monkeypatch.setattr(ws_api, "_run_voice_prepare", slow_prepare)

    first = await ws_api.start_ceo_voice_prepare()
    second = await ws_api.start_ceo_voice_prepare()

    assert first["state"] == "running"
    assert second["state"] == "running"
    assert started == [], "第二个点击不许再起一个下载"
    assert ws_api._voice_prepare_task is not None
    ws_api._voice_prepare_task.cancel()


@pytest.mark.asyncio
async def test_a_failed_download_is_reported_not_swallowed(monkeypatch):
    def boom(cfg=None, on_progress=None):
        raise stt_engine.SttProvisionError("下载包校验失败")

    monkeypatch.setattr(stt_engine, "prepare_binary", boom)

    await ws_api._run_voice_prepare()

    snapshot = ws_api._voice_prepare_snapshot()
    assert snapshot["state"] == "error"
    assert "校验失败" in snapshot["error"]
