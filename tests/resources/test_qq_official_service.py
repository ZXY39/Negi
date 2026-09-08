"""Tests for the QQ-official lifecycle shell + /api/v1 client.

No botpy installed in this environment, so the bridge reports ``error`` and
the service still exercises token provisioning + the status machine. The
client is exercised against an httpx MockTransport (no real HTTP server).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from g3ku.security.bootstrap import get_bootstrap_security_service
from g3ku.qq_official.client import ExternalApiClient
from g3ku.qq_official.service import QqOfficialService


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