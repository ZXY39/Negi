from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main.api.admin_rest as admin_rest
from g3ku.config import context_refresh as context_refresh_module
from g3ku.config.context_refresh import idle_boundary_exceeded
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.session_agent import FRONTDOOR_LAST_SEND_AT_ATTR
from main.runtime.react_loop import ReActToolLoop


def _config(*, enabled: bool = True, idle_minutes: int = 120):
    return SimpleNamespace(
        context_refresh=SimpleNamespace(enabled=enabled, idle_minutes=idle_minutes)
    )


def _stamp(**kwargs) -> str:
    return (datetime.now().astimezone() - timedelta(**kwargs)).isoformat()


def test_idle_boundary_fires_only_after_the_configured_window() -> None:
    config = _config(idle_minutes=120)

    assert idle_boundary_exceeded(_stamp(minutes=119), config=config) is False
    assert idle_boundary_exceeded(_stamp(minutes=121), config=config) is True


def test_idle_boundary_is_disabled_or_unreadable_means_never_refresh() -> None:
    assert idle_boundary_exceeded(_stamp(days=9), config=_config(enabled=False)) is False
    assert idle_boundary_exceeded("", config=_config()) is False
    assert idle_boundary_exceeded("not-a-timestamp", config=_config()) is False


def test_idle_boundary_fails_closed_when_live_config_cannot_be_read(monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise RuntimeError("config store unavailable")

    monkeypatch.setattr(context_refresh_module, "get_runtime_config", _boom)

    assert idle_boundary_exceeded(_stamp(days=9)) is False


def test_node_recommit_reason_reports_compression_before_idle() -> None:
    decide = ReActToolLoop._node_bundle_recommit_reason

    assert decide(history_shrink_reason="token_compression", idle_boundary=True) == "token_compression"
    assert decide(history_shrink_reason="stage_compaction", idle_boundary=True) == "idle_refresh"
    assert decide(history_shrink_reason="stage_compaction", idle_boundary=False) == ""


class _MemoryManager:
    def __init__(self) -> None:
        self.reads = 0

    def snapshot_text(self, **_kwargs) -> str:
        self.reads += 1
        return "## 长期记忆\n- refreshed by idle boundary"


def _frontdoor_session(*, last_send_at: str) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(session_key="web:ceo-idle-probe"),
        **{FRONTDOOR_LAST_SEND_AT_ATTR: last_send_at},
    )


def _frontdoor_ops(*, idle_minutes: int = 120, last_send_at: str):
    class _Runner(CeoFrontDoorRuntimeOps):
        def __init__(self, memory_manager) -> None:
            self._loop = SimpleNamespace(memory_manager=memory_manager)

    memory_manager = _MemoryManager()
    runner = _Runner(memory_manager)
    session = _frontdoor_session(last_send_at=last_send_at)
    monkeypatched = _config(idle_minutes=idle_minutes)
    return runner, session, memory_manager, monkeypatched


def test_frontdoor_idle_boundary_does_not_reprint_or_read_the_snapshot(monkeypatch) -> None:
    """空闲窗口不再是重印点：tools[]/roster 只在压缩跳重印，记忆靠修订流投递。"""
    runner, session, memory_manager, config = _frontdoor_ops(last_send_at=_stamp(minutes=180))
    monkeypatch.setattr(context_refresh_module, "get_runtime_config", lambda **_k: (config, 1, False))

    boundary = runner._frontdoor_bundle_recommit_boundary(session=session, state={})

    assert boundary is False
    assert memory_manager.reads == 0
    assert not hasattr(session, "_frontdoor_memory_snapshot_adoption_reason")


def test_frontdoor_fresh_send_keeps_the_bundle_pinned(monkeypatch) -> None:
    runner, session, memory_manager, config = _frontdoor_ops(last_send_at=_stamp(minutes=5))
    monkeypatch.setattr(context_refresh_module, "get_runtime_config", lambda **_k: (config, 1, False))

    boundary = runner._frontdoor_bundle_recommit_boundary(session=session, state={})

    assert boundary is False
    assert memory_manager.reads == 0


def _write_config(workspace) -> None:
    from g3ku.config.loader import save_config
    from g3ku.config.schema import Config

    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
    save_config(Config(), workspace / ".g3ku" / "config.json")


def _admin_client(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_config(workspace)
    monkeypatch.chdir(workspace)
    captured: dict[str, object] = {}

    async def _fake_refresh(*, force: bool = False, reason: str = "runtime", force_memory_sync: bool = False) -> bool:
        captured["force"] = force
        captured["reason"] = reason
        return True

    monkeypatch.setattr(admin_rest, "refresh_web_agent_runtime", _fake_refresh)
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    return TestClient(app), captured, workspace


def test_context_refresh_endpoint_defaults_to_enabled_120_minutes(tmp_path, monkeypatch) -> None:
    client, _captured, _workspace = _admin_client(tmp_path, monkeypatch)

    response = client.get("/api/context-refresh/settings")

    assert response.status_code == 200
    assert response.json()["context_refresh"] == {"enabled": True, "idle_minutes": 120}


def test_context_refresh_endpoint_applies_and_persists(tmp_path, monkeypatch) -> None:
    client, captured, workspace = _admin_client(tmp_path, monkeypatch)

    response = client.put("/api/context-refresh/settings", json={"enabled": True, "idleMinutes": 45})

    assert response.status_code == 200
    assert response.json()["context_refresh"] == {"enabled": True, "idle_minutes": 45}
    assert captured["reason"] == "admin_context_refresh_update"
    saved = json.loads((workspace / ".g3ku" / "config.json").read_text(encoding="utf-8"))
    assert saved["contextRefresh"] == {"enabled": True, "idleMinutes": 45}


@pytest.mark.parametrize("payload", [{"idleMinutes": 0}, {"idleMinutes": -3}, {"idleMinutes": "abc"}, {}])
def test_context_refresh_endpoint_rejects_unusable_minutes(tmp_path, monkeypatch, payload) -> None:
    client, captured, workspace = _admin_client(tmp_path, monkeypatch)

    response = client.put("/api/context-refresh/settings", json={"enabled": True, **payload})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_idle_minutes"
    assert "reason" not in captured
    saved = json.loads((workspace / ".g3ku" / "config.json").read_text(encoding="utf-8"))
    assert saved.get("contextRefresh") == {"enabled": True, "idleMinutes": 120}


def test_stored_config_without_the_new_section_still_loads(tmp_path, monkeypatch) -> None:
    """`contextRefresh` 是可选段：存量 config.json 没有它也必须能加载。

    显式字段闸门（`_ensure_runtime_fields_explicit`）按 `_runtime_config_payload` 逐条要求，
    新节没进那份清单就等于自动豁免——一旦有人把它加进去，所有已安装设备的配置会当场失效。
    """
    from g3ku.config.loader import load_config

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_config(workspace)
    monkeypatch.chdir(workspace)
    path = workspace / ".g3ku" / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "contextRefresh" in data, "save_config 应把新节写回磁盘"
    data.pop("contextRefresh")
    path.write_text(json.dumps(data), encoding="utf-8")

    cfg = load_config(path)

    assert cfg.context_refresh.enabled is True
    assert cfg.context_refresh.idle_minutes == 120
