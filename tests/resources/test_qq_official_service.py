"""Tests for the QQ-official lifecycle shell + /api/v1 client.

No botpy installed in this environment, so the bridge reports ``error`` and
the service still exercises token provisioning + the status machine. The
client is exercised against an httpx MockTransport (no real HTTP server).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import g3ku.qq_official.bridge as qq_bridge
import g3ku.qq_official.service as qq_service
from g3ku.config.schema import QqBotConfig
from g3ku.qq_official.client import ExternalApiClient
from g3ku.qq_official.service import QqOfficialService
from g3ku.security.bootstrap import get_bootstrap_security_service


def _write_config(workspace: Path, *, enabled: bool, app_id: str, app_secret: str) -> None:
    # Reuse the same minimal root config shape the external admin tests use.
    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
    payload = {
        "agents": {
            "defaults": {
                "workspace": ".",
                "runtime": "langgraph",
                "maxTokens": 1,
                "temperature": 0.1,
                "maxToolIterations": 1,
                "memoryWindow": 1,
                "reasoningEffort": "low",
            },
            "roleIterations": {"ceo": 40, "execution": 16, "inspection": 16},
            "multiAgent": {"orchestratorModelKey": None},
        },
        "models": {
            "catalog": [
                {
                    "key": "m",
                    "providerModel": "openai:gpt-4.1",
                    "apiKey": "demo-key",
                    "apiBase": None,
                    "extraHeaders": None,
                    "enabled": True,
                    "maxTokens": 1,
                    "temperature": 0.1,
                    "reasoningEffort": "low",
                    "retryOn": [],
                    "description": "",
                    "contextWindowTokens": 128000,
                }
            ],
            "roles": {"ceo": ["m"], "execution": ["m"], "inspection": ["m"]},
        },
        "providers": {"openai": {"apiKey": "", "apiBase": None, "extraHeaders": None}},
        "web": {"host": "127.0.0.1", "port": 1},
        "toolSecrets": {},
        "resources": {
            "enabled": True,
            "skillsDir": "skills",
            "toolsDir": "tools",
            "manifestName": "resource.yaml",
            "reload": {
                "enabled": True,
                "pollIntervalMs": 1000,
                "debounceMs": 400,
                "lazyReloadOnAccess": True,
                "keepLastGoodVersion": True,
            },
            "locks": {"lockDir": ".g3ku/resource-locks", "logicalDeleteGuard": True, "windowsFsLock": True},
            "statePath": ".g3ku/resources.state.json",
        },
        "mainRuntime": {
            "enabled": True,
            "storePath": ".g3ku/main-runtime/runtime.sqlite3",
            "filesBaseDir": ".g3ku/main-runtime/tasks",
            "artifactDir": ".g3ku/main-runtime/artifacts",
            "governanceStorePath": ".g3ku/main-runtime/governance.sqlite3",
            "defaultMaxDepth": 1,
            "hardMaxDepth": 4,
            "nodeDispatchConcurrency": {"execution": 8, "inspection": 4},
        },
        "qqBot": {"enabled": enabled, "appId": app_id, "appSecret": app_secret, "sandbox": False},
    }
    (workspace / ".g3ku" / "config.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(ws)
    return ws


@pytest.mark.asyncio
async def test_service_disabled_reports_enabled_off(workspace: Path) -> None:
    _write_config(workspace, enabled=False, app_id="", app_secret="")
    get_bootstrap_security_service(workspace).setup_initial_realm(password="owner-password")

    service = QqOfficialService()
    await service.sync_from_config()
    assert service.status()["state"] == "enabled_off"
    assert service._task is None


@pytest.mark.asyncio
async def test_service_enabled_without_secret_reports_not_configured(workspace: Path) -> None:
    _write_config(workspace, enabled=True, app_id="123", app_secret="")
    get_bootstrap_security_service(workspace).setup_initial_realm(password="owner-password")

    service = QqOfficialService()
    await service.sync_from_config()
    assert service.status()["state"] == "not_configured"
    await service.stop()


@pytest.mark.asyncio
async def test_service_provisions_token_and_bridge_errors_without_botpy(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(workspace, enabled=True, app_id="123", app_secret="")
    security = get_bootstrap_security_service(workspace)
    security.setup_initial_realm(password="owner-password")
    # The appSecret arrives via the overlay (like the real save path), never inline.
    security.set_overlay_values({"config.qqBot.appSecret": "sekrit"})
    # Simulate an environment without qq-botpy: the bridge must report it, not crash.
    import sys

    monkeypatch.setitem(sys.modules, "botpy", None)

    service = QqOfficialService()
    await service.sync_from_config()
    if service._task is not None:
        await service._task

    assert service.status()["state"] == "error"
    assert "botpy" in service.status()["detail"]

    overlay = security.current_overlay()
    assert overlay.get("config.qqBot.appSecret") == "sekrit"
    assert overlay.get("config.externalApi.tokens.qq-official.token")
    await service.stop()


@pytest.mark.asyncio
async def test_client_sessions_messages_and_events() -> None:
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.endswith("/sessions"):
            return httpx.Response(200, json={"ok": True, "session_id": "ext:qq-official:abc123"})
        if request.method == "POST" and url.endswith("/messages"):
            captured["messages"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "turn_id": "t1", "status": "started"})
        if request.method == "GET" and url.endswith("/events"):
            body = (
                b'id: 1\nevent: reply.final\ndata: {"type":"reply.final","seq":1,"text":"hi"}\n\n'
                b'id: 2\nevent: outbound.created\n'
                b'data: {"type":"outbound.created","seq":2,"text":"remind","external_key":"qq:c2c:u1"}\n\n'
            )
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        raise RuntimeError(f"unexpected request: {request.method} {url}")

    client = ExternalApiClient("http://127.0.0.1:18790/api/v1", "tok", transport=httpx.MockTransport(handler))
    session_id = await client.ensure_session("qq:c2c:u1")
    assert session_id == "ext:qq-official:abc123"

    sent = await client.send_message(session_id, "hello", idempotency_key="qq-1")
    assert sent["turn_id"] == "t1"
    assert captured["messages"] == {"text": "hello"}

    attachment = {"kind": "image", "name": "a.png", "mime_type": "image/png", "data_base64": "aGk="}
    await client.send_message(session_id, "看", idempotency_key="qq-2", attachments=[attachment])
    assert captured["messages"] == {"text": "看", "attachments": [attachment]}

    events = [event async for event in client.stream_events(session_id, last_seq=0)]
    assert [event["type"] for event in events] == ["reply.final", "outbound.created"]
    assert events[1]["external_key"] == "qq:c2c:u1"
    await client.close()


def _enable_qq_config(workspace: Path) -> None:
    _write_config(workspace, enabled=True, app_id="123", app_secret="")
    security = get_bootstrap_security_service(workspace)
    security.setup_initial_realm(password="owner-password")
    # The appSecret arrives via the overlay (like the real save path), never inline.
    security.set_overlay_values({"config.qqBot.appSecret": "sekrit"})


def _shrink_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qq_service, "_BRIDGE_RETRY_INITIAL_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(qq_service, "_BRIDGE_RETRY_MAX_BACKOFF_SECONDS", 0.02)


@pytest.mark.asyncio
async def test_service_retries_bridge_crash_until_connected(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """瞬时崩溃（如 botpy Robot(None) 的 AttributeError）必须退避重试直到连上。"""
    _enable_qq_config(workspace)
    _shrink_retry_backoff(monkeypatch)

    calls = {"n": 0}

    async def fake_bridge(**kwargs) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise AttributeError("'NoneType' object has no attribute 'get'")
        kwargs["on_state"]("connected", "")
        await asyncio.Event().wait()

    monkeypatch.setattr(qq_bridge, "run_qq_official_bridge", fake_bridge)

    service = QqOfficialService()
    await service.sync_from_config()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while service.status()["state"] != "connected":
            assert loop.time() < deadline, f"bridge never reconnected: {service.status()}"
            await asyncio.sleep(0.01)
        assert calls["n"] >= 3
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_stop_cancels_bridge_retry_loop(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """持续崩溃时 stop() 必须终结重试循环，且错误态保留异常摘要与重试提示。"""
    _enable_qq_config(workspace)
    _shrink_retry_backoff(monkeypatch)

    calls = {"n": 0}

    async def fake_bridge(**kwargs) -> None:
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(qq_bridge, "run_qq_official_bridge", fake_bridge)

    service = QqOfficialService()
    await service.sync_from_config()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while calls["n"] < 3:
        assert loop.time() < deadline, f"bridge was not retried: calls={calls['n']}"
        await asyncio.sleep(0.01)
    assert service.status()["state"] == "error"
    assert "boom" in service.status()["detail"]
    assert "重试" in service.status()["detail"]

    await service.stop()
    assert service._task is None
    settled = calls["n"]
    await asyncio.sleep(0.05)
    assert calls["n"] == settled, "retry loop kept running after stop()"


@pytest.mark.asyncio
async def test_bridge_retry_backoff_resets_after_healthy_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """健康运行超过阈值后再崩溃，退避必须重置回起始值而不是继续翻倍。"""
    q = QqBotConfig(enabled=True, app_id="1", app_secret="s", sandbox=False)
    service = QqOfficialService(base_url="http://127.0.0.1:1/api/v1")

    calls = {"n": 0}

    async def fake_bridge(**kwargs) -> None:
        calls["n"] += 1
        if calls["n"] > 4:
            return  # 干净返回：结束重试循环
        raise RuntimeError("boom")

    monkeypatch.setattr(qq_bridge, "run_qq_official_bridge", fake_bridge)

    # 每轮崩溃消耗两个时刻（started / except）；第 2 轮"健康运行"80s ≥ 60s 阈值 → 重置。
    clock = iter([0.0, 10.0, 20.0, 100.0, 110.0, 120.0, 130.0, 140.0, 150.0])
    monkeypatch.setattr(qq_service, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await service._run(q, "tok")
    # 第 2 轮若未重置，应睡 2.0；重置后序列为 1, 1, 2, 4。
    assert delays == [1.0, 1.0, 2.0, 4.0]
    assert calls["n"] == 5
