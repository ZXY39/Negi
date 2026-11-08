"""Tests for the QQ scan-to-bind admin endpoints and protocol client.

协议侧用 httpx.MockTransport 锁 q.qq.com/lite/* 线协议与 AES-256-GCM 密文布局
（nonce12‖密文‖tag）；端点侧锁：锁定态 409、completed 落库（AppSecret 进覆盖层、
扫码人按单 Master 规则登记、已有 Master 不换人）、过期即换码、响应永不回显明文。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from g3ku.qq_official import qr_bind
from g3ku.security.bootstrap import get_bootstrap_security_service
from tests.resources.test_qq_official_admin import _client_for, _write_config

OWNER_OPENID = "EB6C8D4341C1238A627FF73CBE540DAE"
SCAN_APP_ID = "999888777"
SCAN_SECRET = "scanned-secret-app"


def _sealed_blob(key: bytes, nonce: bytes, plaintext: str) -> str:
    """按腾讯的 blob 布局（nonce12‖密文‖tag）造密文；AESGCM.encrypt 只给 密文‖tag。"""
    blob = nonce + AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(blob).decode("ascii")


@pytest.fixture(autouse=True)
def _reset_qr_registry():
    qr_bind.reset_qq_qr_bind_registry()
    yield
    qr_bind.reset_qq_qr_bind_registry()


# ---- 协议层：MockTransport 直打 q.qq.com/lite/* -----------------------------


def test_create_bind_task_wire_protocol() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "q.qq.com"
        assert request.url.path == "/lite/create_bind_task"
        body = json.loads(request.content.decode("utf-8"))
        assert len(base64.b64decode(body["key"])) == 32
        return httpx.Response(200, json={"retcode": 0, "data": {"task_id": "t-1"}})

    task = asyncio.run(qr_bind.create_bind_task(transport=httpx.MockTransport(handler)))
    assert task.task_id == "t-1"
    assert len(task.key) == 32
    url = qr_bind.build_connect_url(task.task_id)
    assert url.startswith("https://q.qq.com/qqbot/openclaw/connect.html?task_id=t-1&source=")
    assert url.endswith("&_wv=2")


def test_poll_bind_result_completed_maps_and_decrypts() -> None:
    key = b"\x21" * 32
    encrypted = _sealed_blob(key, b"\x01" * 12, SCAN_SECRET)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/lite/poll_bind_result"
        assert json.loads(request.content.decode("utf-8"))["task_id"] == "t-1"
        return httpx.Response(200, json={
            "retcode": 0,
            "data": {
                "status": 2,
                "bot_appid": SCAN_APP_ID,
                "bot_encrypt_secret": encrypted,
                "user_openid": OWNER_OPENID,
            },
        })

    result = asyncio.run(qr_bind.poll_bind_result("t-1", transport=httpx.MockTransport(handler)))
    assert result.status == "completed"
    assert result.bot_app_id == SCAN_APP_ID
    assert result.user_openid == OWNER_OPENID
    assert qr_bind.decrypt_secret(result.bot_encrypted_secret, key) == SCAN_SECRET


def test_poll_bind_result_waiting_expired_and_upstream_error() -> None:
    statuses = iter([1, 3])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"retcode": 0, "data": {"status": next(statuses)}})

    waiting = asyncio.run(qr_bind.poll_bind_result("t-1", transport=httpx.MockTransport(handler)))
    assert waiting.status == "waiting"
    expired = asyncio.run(qr_bind.poll_bind_result("t-1", transport=httpx.MockTransport(handler)))
    assert expired.status == "expired"
    assert not waiting.bot_encrypted_secret

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"retcode": 9, "msg": "task gone"})

    with pytest.raises(qr_bind.QrBindUpstreamError):
        asyncio.run(qr_bind.poll_bind_result("t-1", transport=httpx.MockTransport(failing)))


def test_decrypt_secret_rejects_short_blob_and_wrong_key() -> None:
    key = b"\x31" * 32
    with pytest.raises(qr_bind.QrBindError):
        qr_bind.decrypt_secret(base64.b64encode(b"tooshort").decode("ascii"), key)
    blob = AESGCM(key).encrypt(b"\x01" * 12, SCAN_SECRET.encode("utf-8"), None)
    with pytest.raises(qr_bind.QrBindError):
        qr_bind.decrypt_secret(base64.b64encode(blob).decode("ascii"), b"\x32" * 32)


def test_render_qr_svg_data_url() -> None:
    data_url = qr_bind.render_qr_svg_data_url(qr_bind.build_connect_url("t-1"))
    assert data_url.startswith("data:image/svg+xml;base64,")
    decoded = base64.b64decode(data_url.split(",", 1)[1]).decode("utf-8", errors="ignore")
    assert "svg" in decoded.lower()


# ---- 端点层 ----------------------------------------------------------------


def _install_upstream(
    monkeypatch: pytest.MonkeyPatch,
    *,
    statuses: list[dict] | None = None,
    create_error: str | None = None,
    poll_error: str | None = None,
) -> dict:
    """把腾讯上游替换成可控假件；statuses 按次序出结果，出尽后重复最后一条。"""
    holder: dict = {}
    queue = list(statuses or [])

    async def fake_create(*, transport=None):
        if create_error:
            raise qr_bind.QrBindUpstreamError(create_error)
        holder["key"] = b"\x21" * 32
        return qr_bind.QrBindTask(task_id="task-1", key=holder["key"], expires_at_monotonic=time.monotonic() + 300)

    async def fake_poll(task_id, *, transport=None):
        if poll_error:
            raise qr_bind.QrBindUpstreamError(poll_error)
        item = queue.pop(0) if queue else (statuses[-1] if statuses else {"status": "waiting"})
        encrypted = ""
        if item.get("status") == "completed" and "secret" in item:
            encrypted = _sealed_blob(holder["key"], b"\x01" * 12, str(item["secret"]))
        return qr_bind.QrBindPollResult(
            status=str(item.get("status") or "waiting"),
            bot_app_id=str(item.get("app_id") or ""),
            bot_encrypted_secret=encrypted,
            user_openid=str(item.get("openid") or ""),
        )

    monkeypatch.setattr(qr_bind, "create_bind_task", fake_create)
    monkeypatch.setattr(qr_bind, "poll_bind_result", fake_poll)
    return holder


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    workspace = tmp_path / "workspace"
    _write_config(workspace)
    return _client_for(workspace, monkeypatch)


def _row(payload: dict, app_id: str) -> dict:
    return next(row for row in payload["accounts"] if row["app_id"] == app_id)


def test_qr_bind_create_requires_unlocked_runtime(client: TestClient) -> None:
    get_bootstrap_security_service(Path.cwd()).lock()
    response = client.post("/api/qq-bot/qr-bind")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "runtime_locked"
    assert qr_bind.get_qq_qr_bind_registry().get("task-1") is None


def test_qr_bind_create_returns_qr_and_url(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_upstream(monkeypatch)
    response = client.post("/api/qq-bot/qr-bind")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["task_id"] == "task-1"
    assert "source=" in body["connect_url"] and "_wv=2" in body["connect_url"]
    assert body["qr_svg_data_url"].startswith("data:image/svg+xml;base64,")
    assert body["expires_in_seconds"] > 0
    # 任务进了槽位，明文 secret 不可能出现在任何响应里（还没扫到）。
    assert qr_bind.get_qq_qr_bind_registry().get("task-1") is not None
    assert "scanned-secret-app" not in json.dumps(body, ensure_ascii=False)


def test_qr_bind_create_upstream_error_maps_502(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_upstream(monkeypatch, create_error="retcode 5")
    response = client.post("/api/qq-bot/qr-bind")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "qr_bind_upstream_error"
    assert qr_bind.get_qq_qr_bind_registry().get("task-1") is None


def test_qr_bind_status_waiting_then_completed_registers_master(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # 假件必须先于 create 就位：真件会直接出网。
    _install_upstream(monkeypatch, statuses=[
        {"status": "waiting"},
        {
            "status": "completed",
            "app_id": SCAN_APP_ID,
            "secret": SCAN_SECRET,
            "openid": OWNER_OPENID,
        },
    ])
    created = client.post("/api/qq-bot/qr-bind")
    assert created.status_code == 200
    task_id = created.json()["task_id"]

    waiting = client.get("/api/qq-bot/qr-bind/status", params={"task_id": task_id})
    assert waiting.status_code == 200
    assert waiting.json() == {"ok": True, "status": "waiting"}

    completed = client.get("/api/qq-bot/qr-bind/status", params={"task_id": task_id})
    assert completed.status_code == 200
    body = completed.json()
    assert body["status"] == "completed"
    assert body["app_id"] == SCAN_APP_ID
    assert body["bridge_id"] == f"qq-official-{SCAN_APP_ID}"
    assert body["master"] == "registered"
    assert body["sender_id_masked"]
    # 默认配置总开关是关的：扫码注册的"注册即用"语义把它打开。
    assert body["enabled"] is True
    row = _row(body, SCAN_APP_ID)
    assert row["has_secret"] is True
    assert row["label"] == "扫码注册"
    assert row["owner_openids"] == [OWNER_OPENID]
    assert SCAN_SECRET not in json.dumps(body, ensure_ascii=False)

    # 落库契约与 settings PUT 同一条链：盘上占位、覆盖层存真值。
    on_disk = json.loads((Path(".g3ku") / "config.json").read_text(encoding="utf-8"))
    assert on_disk["qqBot"]["accounts"][SCAN_APP_ID]["appSecret"] == ""
    overlay = get_bootstrap_security_service(Path.cwd()).current_overlay()
    assert overlay.get(f"config.qqBot.accounts.{SCAN_APP_ID}.appSecret") == SCAN_SECRET

    # 任务槽已消费；再轮询同一 task_id 是 404 而不是重复落库。
    again = client.get("/api/qq-bot/qr-bind/status", params={"task_id": task_id})
    assert again.status_code == 404
    assert again.json()["detail"]["code"] == "qr_bind_task_not_found"


def test_qr_bind_completed_keeps_existing_master_and_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    _write_config(workspace, {
        "enabled": True,
        "accounts": {
            SCAN_APP_ID: {
                "appSecret": "",
                "enabled": True,
                "label": "我的老号",
                "ownerOpenids": [OWNER_OPENID],
            }
        },
    })
    client = _client_for(workspace, monkeypatch)
    _install_upstream(monkeypatch, statuses=[{
        "status": "completed",
        "app_id": SCAN_APP_ID,
        "secret": SCAN_SECRET,
        "openid": OWNER_OPENID,
    }])
    created = client.post("/api/qq-bot/qr-bind")
    task_id = created.json()["task_id"]

    completed = client.get("/api/qq-bot/qr-bind/status", params={"task_id": task_id})
    assert completed.status_code == 200
    body = completed.json()
    assert body["master"] == "kept"
    row = _row(body, SCAN_APP_ID)
    assert row["label"] == "我的老号"
    assert row["owner_openids"] == [OWNER_OPENID]
    overlay = get_bootstrap_security_service(Path.cwd()).current_overlay()
    assert overlay.get(f"config.qqBot.accounts.{SCAN_APP_ID}.appSecret") == SCAN_SECRET


def test_qr_bind_skips_owner_when_openid_missing(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_upstream(monkeypatch, statuses=[{
        "status": "completed",
        "app_id": SCAN_APP_ID,
        "secret": SCAN_SECRET,
        "openid": "",
    }])
    created = client.post("/api/qq-bot/qr-bind")
    task_id = created.json()["task_id"]
    completed = client.get("/api/qq-bot/qr-bind/status", params={"task_id": task_id})
    assert completed.status_code == 200
    body = completed.json()
    assert body["master"] == "skipped"
    assert _row(body, SCAN_APP_ID)["owner_openids"] == []


def test_qr_bind_expired_consumes_task(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_upstream(monkeypatch, statuses=[{"status": "expired"}])
    created = client.post("/api/qq-bot/qr-bind")
    task_id = created.json()["task_id"]
    response = client.get("/api/qq-bot/qr-bind/status", params={"task_id": task_id})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "expired"}
    again = client.get("/api/qq-bot/qr-bind/status", params={"task_id": task_id})
    assert again.status_code == 404


def test_qr_bind_status_unknown_task_is_404(client: TestClient) -> None:
    response = client.get("/api/qq-bot/qr-bind/status", params={"task_id": "nope"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "qr_bind_task_not_found"


def test_qr_bind_locked_at_completion_consumes_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    _write_config(workspace)
    client = _client_for(workspace, monkeypatch)
    _install_upstream(monkeypatch, statuses=[{
        "status": "completed",
        "app_id": SCAN_APP_ID,
        "secret": SCAN_SECRET,
        "openid": OWNER_OPENID,
    }])
    created = client.post("/api/qq-bot/qr-bind")
    task_id = created.json()["task_id"]
    get_bootstrap_security_service(Path.cwd()).lock()
    response = client.get("/api/qq-bot/qr-bind/status", params={"task_id": task_id})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "runtime_locked"
    # 任务已消费：解锁后不会拿旧扫码结果偷偷落库，操作员重新扫码。
    assert qr_bind.get_qq_qr_bind_registry().get(task_id) is None
