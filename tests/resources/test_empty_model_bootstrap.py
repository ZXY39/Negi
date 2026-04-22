from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.config.loader import ensure_startup_config_ready, load_config
from g3ku.llm_config.enums import ProbeStatus, ProtocolAdapter
from g3ku.llm_config.models import ProbeResult
from g3ku.llm_config.service import ConfigService
import g3ku.runtime.api.ceo_sessions as ceo_sessions_api
import g3ku.runtime.api.websocket_ceo as websocket_ceo
import main.api.admin_rest as admin_rest
import main.api.rest as task_rest
import main.api.websocket_task as websocket_task


class _UnlockedSecurity:
    def is_unlocked(self) -> bool:
        return True


def _raise_no_model_configured():
    raise ValueError("No model configured for role 'ceo'.")


def test_ensure_startup_config_ready_bootstraps_empty_model_catalog(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)

    changed = ensure_startup_config_ready()

    assert changed is True
    raw = json.loads((workspace / ".g3ku" / "config.json").read_text(encoding="utf-8"))
    assert raw["models"]["catalog"] == []
    assert raw["models"]["roles"] == {"ceo": [], "execution": [], "inspection": [], "memory": []}
    assert not (workspace / ".g3ku" / "llm-config").exists()

    cfg = load_config()
    assert cfg.get_role_model_keys("ceo") == []
    assert cfg.get_role_model_keys("execution") == []
    assert cfg.get_role_model_keys("inspection") == []


def test_first_model_can_be_created_without_role_assignments(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)
    ensure_startup_config_ready()

    async def _fake_refresh(*, force: bool = False, reason: str = "runtime") -> bool:
        _ = force, reason
        return False

    def _fake_probe(self, draft):
        _ = self
        return ProbeResult(
            status=ProbeStatus.SUCCESS,
            success=True,
            provider_id=draft.provider_id,
            protocol_adapter=ProtocolAdapter.OPENAI_RESPONSES,
            capability=draft.capability,
            resolved_base_url=draft.base_url,
            checked_model=draft.default_model,
            message="ok",
            diagnostics={},
        )

    monkeypatch.setattr(admin_rest, "refresh_web_agent_runtime", _fake_refresh)
    monkeypatch.setattr(ConfigService, "probe_draft", _fake_probe)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    client = TestClient(app)

    response = client.post(
        "/api/models",
        json={
            "key": "primary",
            "provider_model": "openai:gpt-4.1",
            "api_key": "demo-key",
            "api_base": "https://api.openai.com/v1",
            "enabled": True,
            "scopes": [],
            "context_window_tokens": 32000,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["item"]["key"] == "primary"

    saved = json.loads((workspace / ".g3ku" / "config.json").read_text(encoding="utf-8"))
    assert saved["models"]["roles"] == {"ceo": [], "execution": [], "inspection": [], "memory": []}

    reloaded = load_config()
    assert [item.key for item in reloaded.models.catalog] == ["primary"]
    assert reloaded.get_role_model_keys("ceo") == []


def test_load_config_dedupes_duplicate_model_keys_preserving_first_entry(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)
    ensure_startup_config_ready()

    config_path = workspace / ".g3ku" / "config.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["models"]["catalog"] = [
        {
            "key": "primary",
            "llmConfigId": "cfg-first",
            "enabled": True,
            "retryOn": ["network"],
            "retryCount": 1,
            "description": "first",
        },
        {
            "key": "primary",
            "llmConfigId": "cfg-second",
            "enabled": True,
            "retryOn": ["429"],
            "retryCount": 2,
            "description": "second",
        },
    ]
    raw["models"]["roles"] = {
        "ceo": ["primary"],
        "execution": ["primary"],
        "inspection": ["primary"],
    }
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    cfg = load_config()

    assert [item.key for item in cfg.models.catalog] == ["primary"]
    assert cfg.models.catalog[0].llm_config_id == "cfg-first"

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert [item["key"] for item in saved["models"]["catalog"]] == ["primary"]
    assert saved["models"]["catalog"][0]["llmConfigId"] == "cfg-first"


def test_ceo_sessions_list_works_without_model(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)
    ensure_startup_config_ready()

    monkeypatch.setattr(ceo_sessions_api, "get_agent", _raise_no_model_configured)

    app = FastAPI()
    app.include_router(ceo_sessions_api.router, prefix="/api")
    client = TestClient(app)

    response = client.get("/api/ceo/sessions")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["active_session_id"].startswith("web:")
    assert len(payload["items"]) == 1


def test_ceo_websocket_reports_no_model_configured(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)
    ensure_startup_config_ready()

    monkeypatch.setattr(websocket_ceo, "get_bootstrap_security_service", lambda: _UnlockedSecurity())
    monkeypatch.setattr(websocket_ceo, "get_agent", _raise_no_model_configured)

    app = FastAPI()
    app.include_router(websocket_ceo.router, prefix="/api")
    client = TestClient(app)

    with client.websocket_connect("/api/ws/ceo") as ws:
        payload = ws.receive_json()

    assert payload["type"] == "error"
    assert payload["data"]["code"] == "no_model_configured"


def test_tasks_endpoints_report_no_model_configured(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)
    ensure_startup_config_ready()

    monkeypatch.setattr(websocket_task, "get_bootstrap_security_service", lambda: _UnlockedSecurity())
    monkeypatch.setattr(task_rest, "get_agent", _raise_no_model_configured)
    monkeypatch.setattr(websocket_task, "get_agent", _raise_no_model_configured)

    app = FastAPI()
    app.include_router(task_rest.router, prefix="/api")
    app.include_router(websocket_task.router, prefix="/api")
    client = TestClient(app)

    response = client.get("/api/tasks")
    assert response.status_code == 503
    assert response.json()["detail"] == "no_model_configured"

    with client.websocket_connect("/api/ws/tasks?session_id=all&after_seq=0") as ws:
        payload = ws.receive_json()

    assert payload["type"] == "error"
    assert payload["data"]["code"] == "no_model_configured"
