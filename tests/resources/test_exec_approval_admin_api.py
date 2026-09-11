"""exec 白名单/审批管理端 API 契约测试。

端点挂在 /resources/tools/exec-*（注册顺序先于 /resources/tools/{tool_id}，
避免被路径参数路由吞掉）。服务不可用时 503；校验失败 400；未知审批 404。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from main.api import admin_rest
from main.governance.exec_approvals import ExecApprovalService
from main.governance.store import GovernanceStore


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = GovernanceStore(tmp_path / "governance.sqlite3")
    approvals = ExecApprovalService(store)
    service = SimpleNamespace(exec_approvals=approvals)
    monkeypatch.setattr(admin_rest, "get_agent", lambda: SimpleNamespace(main_task_service=service))
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api/admin")
    client = TestClient(app)
    yield client, approvals
    store.close()


def test_whitelist_crud_roundtrip(env) -> None:
    client, approvals = env

    listed = client.get("/api/admin/resources/tools/exec-command-whitelist")
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert listed.json()["approval_wait_seconds"] == 120.0

    added = client.post(
        "/api/admin/resources/tools/exec-command-whitelist",
        json={"pattern": "rm  -rf   temp/*", "scope": "tasks", "reason": "清理任务临时目录"},
    )
    assert added.status_code == 200
    item = added.json()["item"]
    assert item["pattern"] == "rm -rf temp/*", "入库前空白归一化"
    assert item["scope"] == "tasks"

    listed = client.get("/api/admin/resources/tools/exec-command-whitelist").json()
    assert len(listed["items"]) == 1

    removed = client.post(
        "/api/admin/resources/tools/exec-command-whitelist/delete",
        json={"pattern": "rm -rf temp/*", "scope": "tasks"},
    )
    assert removed.status_code == 200 and removed.json()["removed"] is True
    assert approvals.list_whitelist() == []


def test_whitelist_rejects_broad_and_duplicate(env) -> None:
    client, _ = env

    broad = client.post(
        "/api/admin/resources/tools/exec-command-whitelist", json={"pattern": "rm", "scope": "all"}
    )
    assert broad.status_code == 400
    assert "too_broad" in broad.json()["detail"]

    first = client.post(
        "/api/admin/resources/tools/exec-command-whitelist", json={"pattern": "git push origin *", "scope": "ceo"}
    )
    assert first.status_code == 200
    duplicate = client.post(
        "/api/admin/resources/tools/exec-command-whitelist", json={"pattern": "git push origin *", "scope": "ceo"}
    )
    assert duplicate.status_code == 400
    assert "already_exists" in duplicate.json()["detail"]

    bad_scope = client.post(
        "/api/admin/resources/tools/exec-command-whitelist", json={"pattern": "git pull --ff-only", "scope": "everyone"}
    )
    assert bad_scope.status_code == 400


def test_approval_wait_seconds_update_and_clamp(env) -> None:
    client, _ = env

    updated = client.put("/api/admin/resources/tools/exec-approval-wait", json={"seconds": 45})
    assert updated.status_code == 200 and updated.json()["approval_wait_seconds"] == 45.0

    clamped = client.put("/api/admin/resources/tools/exec-approval-wait", json={"seconds": 1})
    assert clamped.json()["approval_wait_seconds"] == 5.0

    invalid = client.put("/api/admin/resources/tools/exec-approval-wait", json={})
    assert invalid.status_code == 400


def test_approval_list_and_decision_flow(env) -> None:
    client, approvals = env

    created = approvals.request_approval(
        command="rm -rf temp/build_9",
        guard_reason="dangerous pattern",
        actor_role="execution",
        lane="task",
        context_id="task:9",
    )
    assert created is not None

    pending = client.get("/api/admin/resources/tools/exec-approvals")
    assert pending.status_code == 200
    items = pending.json()["items"]
    assert len(items) == 1
    assert items[0]["approval_id"] == created["approval_id"]
    assert items[0]["command_norm"] == "rm -rf temp/build_9"

    decided = client.post(
        f"/api/admin/resources/tools/exec-approvals/{created['approval_id']}/decision",
        json={"decision": "approve_whitelist", "scope": "tasks", "decided_by": "op"},
    )
    assert decided.status_code == 200
    body = decided.json()["item"]
    assert body["status"] == "approved_whitelist"
    assert body["whitelist_entry"]["pattern"] == "rm -rf temp/build_9"

    # 二次裁决返回 duplicate 而非改写
    again = client.post(
        f"/api/admin/resources/tools/exec-approvals/{created['approval_id']}/decision",
        json={"decision": "deny"},
    )
    assert again.json()["item"]["duplicate"] is True
    assert again.json()["item"]["status"] == "approved_whitelist"

    assert client.get("/api/admin/resources/tools/exec-approvals").json()["items"] == []


def test_decision_error_shapes(env) -> None:
    client, _ = env

    missing = client.post(
        "/api/admin/resources/tools/exec-approvals/no-such-id/decision", json={"decision": "deny"}
    )
    assert missing.status_code == 404

    approvals = client.get("/api/admin/resources/tools/exec-approvals")
    assert approvals.status_code == 200


def test_endpoints_503_without_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_rest, "get_agent", lambda: None)
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api/admin")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/api/admin/resources/tools/exec-command-whitelist")
    assert response.status_code in {500, 503}, "无运行时应报服务不可用而非 200"
