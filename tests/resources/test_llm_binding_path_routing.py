from __future__ import annotations

import importlib

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _load_admin_rest():
    return importlib.import_module("main.api.admin_rest")


def test_llm_binding_update_route_accepts_model_keys_with_slashes(monkeypatch) -> None:
    admin_rest = _load_admin_rest()
    captured: dict[str, object] = {}

    class _StubFacade:
        def update_binding(self, config, *, model_key: str, draft_payload: dict):
            captured["config"] = config
            captured["model_key"] = model_key
            captured["draft_payload"] = dict(draft_payload)
            return {"key": model_key, "retry_count": draft_payload.get("retry_count", 0)}

    class _StubManager:
        def __init__(self):
            self.config = object()
            self.facade = _StubFacade()

        def _revalidate(self):
            captured["revalidated"] = True

        def save(self):
            captured["saved"] = True

    async def _fake_refresh(*, force: bool = False, reason: str = "runtime") -> bool:
        captured["force"] = force
        captured["reason"] = reason
        return True

    monkeypatch.setattr(admin_rest.ModelManager, "load", classmethod(lambda cls: _StubManager()))
    monkeypatch.setattr(admin_rest, "refresh_web_agent_runtime", _fake_refresh)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    client = TestClient(app)

    response = client.put(
        "/api/llm/bindings/qwen%2Fqwen3.6-plus%3Afree",
        json={"retry_count": 4},
    )

    assert response.status_code == 200
    assert captured["model_key"] == "qwen/qwen3.6-plus:free"
    assert captured["draft_payload"] == {"retry_count": 4}
    assert captured["revalidated"] is True
    assert captured["saved"] is True
    assert captured["reason"] == "admin_llm_binding_update"
