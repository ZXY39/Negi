"""Session CRUD REST tests for the External Agent API."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime.api import external_v1
from g3ku.runtime.api.external_auth import ExternalApiPrincipal, require_external_api
from g3ku.runtime.external_sessions import (
    ExternalSessionRegistry,
    reset_external_session_registry,
)
from g3ku.session.manager import SessionManager


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def registry(workspace):
    reset_external_session_registry()
    return ExternalSessionRegistry(workspace)


@pytest.fixture
def cleared_artifacts():
    return []


@pytest.fixture
def client(monkeypatch, workspace, registry, cleared_artifacts):
    monkeypatch.setattr(external_v1, "get_external_session_registry", lambda: registry)
    monkeypatch.setattr(external_v1, "_session_manager", lambda: SessionManager(workspace))
    monkeypatch.setattr(external_v1, "workspace_path", lambda: workspace)
    monkeypatch.setattr(
        external_v1,
        "clear_web_ceo_session_artifacts",
        lambda **kwargs: cleared_artifacts.append(kwargs),
    )

    app = FastAPI()
    app.include_router(external_v1.router, prefix="/api/v1")
    app.dependency_overrides[require_external_api] = lambda: ExternalApiPrincipal(
        bridge_id="test-bridge", label="test"
    )
    return TestClient(app)


def test_create_session_get_or_create(client, registry):
    first = client.post("/api/v1/sessions", json={"external_key": "qq:group:1", "title": "群A"})
    assert first.status_code == 200
    payload = first.json()
    assert payload["created"] is True
    assert payload["session_id"].startswith("ext:test-bridge:")
    assert payload["external_key"] == "qq:group:1"

    second = client.post("/api/v1/sessions", json={"external_key": "qq:group:1"})
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["session_id"] == payload["session_id"]
    # title from first create survives the second get
    assert second.json()["title"] == "群A"

    assert registry.path.exists()


def test_create_session_requires_external_key(client):
    response = client.post("/api/v1/sessions", json={})
    assert response.status_code == 400
    assert response.json()["detail"] == "external_key_required"


def test_list_sessions_filters_by_bridge(client, registry):
    client.post("/api/v1/sessions", json={"external_key": "qq:dm:1"})
    client.post("/api/v1/sessions", json={"external_key": "qq:dm:2"})
    # another bridge's session must not leak into this bridge's listing
    registry.resolve_or_create(bridge_id="other-bridge", external_key="qq:dm:9")

    response = client.get("/api/v1/sessions")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    assert {item["external_key"] for item in items} == {"qq:dm:1", "qq:dm:2"}

    filtered = client.get("/api/v1/sessions", params={"external_key": "qq:dm:1"})
    assert len(filtered.json()["items"]) == 1


def test_rename_session(client):
    created = client.post("/api/v1/sessions", json={"external_key": "qq:dm:1"}).json()
    session_id = created["session_id"]

    response = client.patch(f"/api/v1/sessions/{session_id}", json={"title": "新标题"})
    assert response.status_code == 200
    assert response.json()["title"] == "新标题"

    missing_title = client.patch(f"/api/v1/sessions/{session_id}", json={"title": " "})
    assert missing_title.status_code == 400


def test_unknown_or_foreign_session_404(client, registry):
    registry.resolve_or_create(bridge_id="other-bridge", external_key="qq:dm:9")
    foreign = registry.get_session_key(bridge_id="other-bridge", external_key="qq:dm:9")

    assert client.get("/api/v1/sessions/ext:test-bridge:unknown/state").status_code == 404
    assert client.delete(f"/api/v1/sessions/{foreign}").status_code == 404
    assert client.patch(f"/api/v1/sessions/{foreign}", json={"title": "x"}).status_code == 404


def test_delete_session_clears_context_keeps_entry(client, workspace, registry, cleared_artifacts):
    created = client.post("/api/v1/sessions", json={"external_key": "qq:dm:5"}).json()
    session_id = created["session_id"]

    manager = SessionManager(workspace)
    session = manager.get_or_create(session_id)
    session.add_message("user", "old context")
    manager.save(session)

    response = client.delete(f"/api/v1/sessions/{session_id}")
    assert response.status_code == 200
    assert response.json()["cleared"] is True

    # transcript reset, registry entry kept
    reloaded = SessionManager(workspace).get_or_create(session_id)
    assert len(reloaded.messages) == 0
    assert registry.get_by_session_key(session_id) is not None
    assert cleared_artifacts and cleared_artifacts[0]["session_id"] == session_id
