"""按需取得语音资产这条车道（引擎单飞 + /api/ceo/voice/status 与 /prepare）。

新设备默认就开着语音，但缺那 157 MB。钉住的是"谁提的、提了几次、失败怎么说"：

* 单飞——第二个入口（网页再点一次、渠道又来一条语音）不许重开一个下载；
* 许可归 `stt.enabled`——关掉它之后任何入口都不能替操作员下载；
* 失败可重试但有冷却——渠道语音没有"操作员再点一次"这个出口，弱网里每条语音都重开
  一次下载会把线程池占满；网页上的一次点击是明确动作，立刻重试。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime.api import websocket_ceo as ws_api
from g3ku.stt import engine as stt_engine


@pytest.fixture(autouse=True)
def clean_state():
    stt_engine._PROVISION_STATE.update(
        {"state": "idle", "stage": "", "done_bytes": 0, "total_bytes": None, "error": ""}
    )
    stt_engine._provision_task = None
    stt_engine._provision_finished_at = 0.0
    yield
    task = stt_engine._provision_task
    if task is not None and not task.done():
        task.cancel()
    stt_engine._provision_task = None


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


def _records_preparations(monkeypatch):
    calls: list[str] = []

    def fake_prepare_binary(cfg=None, on_progress=None):
        calls.append("binary")
        return {"ok": True}

    def fake_prepare_model(cfg=None, on_progress=None):
        calls.append("model")
        return {"ok": True}

    monkeypatch.setattr(stt_engine, "prepare_binary", fake_prepare_binary)
    monkeypatch.setattr(stt_engine, "prepare_model", fake_prepare_model)
    return calls


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
    calls = _records_preparations(monkeypatch)

    body = client.post("/api/ceo/voice/prepare").json()

    assert body["state"] == "ready"
    assert calls == []


@pytest.mark.asyncio
async def test_run_provisioning_walks_both_parts_and_records_progress(monkeypatch):
    """下载顺序与台账：二进制先、模型后，进度按阶段回写。

    这里直接调协程而不是走 TestClient——TestClient 每个请求跑在自己的 loop 上，
    后台任务会随请求结束一起被切掉，测不到"跨请求还在下"。
    """
    order = []

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

    await stt_engine._run_provisioning()

    assert order == ["binary", "model"]
    snapshot = stt_engine.provisioning_snapshot()
    assert snapshot["state"] == "ready"
    assert snapshot["stage"] == "model"
    assert snapshot["done_bytes"] == 147951465


@pytest.mark.asyncio
async def test_a_second_request_reuses_the_running_download(monkeypatch):
    started = []

    async def slow_provisioning():
        started.append(1)
        await asyncio.sleep(5)

    monkeypatch.setattr(stt_engine, "status", lambda cfg=None: {"enabled": True, "ready": False})
    monkeypatch.setattr(stt_engine, "_run_provisioning", slow_provisioning)

    first = await stt_engine.request_provisioning(source="web-composer")
    second = await stt_engine.request_provisioning(source="qq-voice")

    assert first["state"] == "running"
    assert second["state"] == "running"
    assert started == [], "第二个入口不许再起一个下载"
    assert stt_engine._provision_task is not None


@pytest.mark.asyncio
async def test_a_disabled_switch_refuses_every_surface(monkeypatch):
    """`stt.enabled=false` 是操作员的许可：关掉之后网页首点与渠道语音都不该下载。"""
    _not_ready(monkeypatch, enabled=False)
    calls = _records_preparations(monkeypatch)

    snapshot = await stt_engine.request_provisioning(source="qq-voice")

    assert snapshot["state"] == "disabled"
    assert calls == []


@pytest.mark.asyncio
async def test_a_failed_download_is_reported_not_swallowed(monkeypatch):
    def boom(cfg=None, on_progress=None):
        raise stt_engine.SttProvisionError("下载包校验失败")

    monkeypatch.setattr(stt_engine, "prepare_binary", boom)

    await stt_engine._run_provisioning()

    snapshot = stt_engine.provisioning_snapshot()
    assert snapshot["state"] == "error"
    assert "校验失败" in snapshot["error"]


@pytest.mark.asyncio
async def test_channel_voice_retries_a_failed_download_only_after_the_cooldown(monkeypatch):
    """渠道侧没有"再点一次"的出口：刚失败就每条语音重开一次下载等于把线程池占满。"""
    _not_ready(monkeypatch)
    calls = _records_preparations(monkeypatch)
    stt_engine._PROVISION_STATE["state"] = "error"

    stt_engine._provision_finished_at = time.monotonic()
    in_cooldown = await stt_engine.request_provisioning(source="qq-voice")
    assert in_cooldown["state"] == "error"
    assert calls == []

    # 冷却窗走完，下一条语音自己把下载再提起来。
    stt_engine._provision_finished_at = time.monotonic() - (stt_engine._PROVISION_RETRY_AFTER_SECONDS + 1)
    after_cooldown = await stt_engine.request_provisioning(source="qq-voice")

    assert after_cooldown["state"] == "running"
    # 请求只负责把任务挂上 loop，两步下载在任务里跑：断言要等它落完。
    await stt_engine._provision_task
    assert calls == ["binary", "model"]


@pytest.mark.asyncio
async def test_an_operator_click_ignores_the_cooldown(monkeypatch):
    _not_ready(monkeypatch)
    calls = _records_preparations(monkeypatch)
    stt_engine._PROVISION_STATE["state"] = "error"
    stt_engine._provision_finished_at = time.monotonic()

    snapshot = await stt_engine.request_provisioning(source="web-composer", force=True)

    assert snapshot["state"] == "running"
    await stt_engine._provision_task
    assert calls == ["binary", "model"]
