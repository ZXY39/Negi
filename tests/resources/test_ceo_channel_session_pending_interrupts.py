"""Channel sessions are read-only archive views: the CEO composer and tool
approval never run there, so ``pending-interrupts`` returns an empty list by
construction instead of 404ing (the web frontend polls this endpoint for the
active session, including channel sessions)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime.api import ceo_sessions


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")
    return TestClient(app)


@pytest.mark.parametrize(
    "session_id",
    [
        "china:qqbot:default:dm",
        "ext:qq-official:f8a8001865631301",
    ],
)
def test_channel_session_pending_interrupts_returns_empty(client, session_id):
    response = client.get(f"/api/ceo/sessions/{session_id}/pending-interrupts")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["session_id"] == session_id
    assert payload["items"] == []
