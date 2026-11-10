"""任务进程面板的 REST 形状：列表、停止与错误码。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from main.api import rest


class _Store:
    def __init__(self, task) -> None:
        self.task = task

    def get_task(self, task_id: str):
        return self.task


class _Service:
    def __init__(self, *, task=object(), item=None, stop_result=None) -> None:
        self.store = _Store(task)
        self.item = item or {
            "pid": 123,
            "purpose": "dev server :3000",
            "cmd": "node server.js",
            "started_at": "2026-11-10T19:22:59",
            "log_path": "C:/log/123.log",
            "node_id": "node:a",
            "node_goal": "渲染 ep03",
            "node_status": "in_progress",
        }
        self.stop_result = stop_result or {
            "ok": True,
            "pid": 123,
            "status": "terminated",
            "purpose": "dev server :3000",
            "log_path": "C:/log/123.log",
        }
        self.stop_calls: list[tuple[str, int]] = []

    async def startup(self) -> None:
        return None

    @staticmethod
    def normalize_task_id(value: str) -> str:
        raw = str(value or "").strip()
        return raw if raw.startswith("task:") else f"task:{raw}"

    def list_task_owned_processes(self, task_id: str):
        return [self.item]

    async def stop_task_owned_process(self, task_id: str, pid: int):
        self.stop_calls.append((task_id, pid))
        return dict(self.stop_result)


def _client(monkeypatch: pytest.MonkeyPatch, service: _Service) -> TestClient:
    monkeypatch.setattr(rest, "_service", lambda: service)
    app = FastAPI()
    app.include_router(rest.router, prefix="/api")
    return TestClient(app)


def test_list_task_processes_returns_purpose_and_node(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _Service())

    response = client.get("/api/tasks/abc/processes")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["task_id"] == "task:abc"
    assert payload["count"] == 1
    assert payload["items"][0]["purpose"] == "dev server :3000"
    assert payload["items"][0]["node_id"] == "node:a"
    assert payload["items"][0]["node_goal"] == "渲染 ep03"


def test_list_task_processes_returns_404_for_missing_task(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _Service(task=None))

    response = client.get("/api/tasks/abc/processes")

    assert response.status_code == 404


def test_stop_task_process_passes_task_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _Service()
    client = _client(monkeypatch, service)

    response = client.post("/api/tasks/abc/processes/123/stop")

    assert response.status_code == 200
    assert response.json()["status"] == "terminated"
    assert service.stop_calls == [("task:abc", 123)]


def test_stop_task_process_reports_not_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _Service(stop_result={"ok": False, "pid": 123, "status": "not_owned"})
    client = _client(monkeypatch, service)

    response = client.post("/api/tasks/abc/processes/123/stop")

    assert response.status_code == 404
    assert response.json()["detail"] == "process_not_found"


def test_stop_task_process_maps_already_exited_to_success(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _Service(stop_result={"ok": False, "pid": 123, "status": "already_exited"})
    client = _client(monkeypatch, service)

    response = client.post("/api/tasks/abc/processes/123/stop")

    assert response.status_code == 200
    assert response.json()["ok"] is True
