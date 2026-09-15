"""记忆管理新增管理面（失败停车区 + 当前记忆编辑/删除）的端点与运行时测试。"""

from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from main.api import admin_rest


def _load_memory_agent_runtime_module():
    assert importlib.util.find_spec("g3ku.agent.memory_agent_runtime") is not None
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _memory_cfg():
    from g3ku.config.schema import MemoryToolsConfig

    payload = MemoryToolsConfig().model_dump(mode="python")
    payload["document"] = {
        "summary_max_chars": 250,
        "document_max_chars": 20000,
        "memory_file": "memory/MEMORY.md",
        "notes_dir": "memory/notes",
    }
    payload["queue"] = {
        "queue_file": "memory/queue.jsonl",
        "ops_file": "memory/ops.jsonl",
        "failed_file": "memory/failed.jsonl",
        "batch_max_chars": 50000,
        "max_wait_seconds": 3,
        "review_interval_turns": 5,
    }
    return MemoryToolsConfig.model_validate(payload)


class _StubMemoryManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.calls: list[tuple[str, object]] = []

    async def list_failed_page(self, *, limit: int = 20, offset: int = 0):
        self.calls.append(("list_failed_page", {"limit": limit, "offset": offset}))
        return {
            "items": [{"failed_id": "failed_1", "status": "parked", "category": "provider_error"}],
            "total": 1,
            "has_more": False,
        }

    async def retry_failed_record(self, failed_id: str, *, reason: str = "manual"):
        self.calls.append(("retry_failed_record", failed_id))
        if failed_id == "failed_missing":
            raise KeyError(f"failed memory record not found: {failed_id}")
        if failed_id == "failed_requeued":
            raise ValueError("failed memory record is not parked (status=requeued)")
        return {"failed_id": failed_id, "request_ids": ["write_1"], "trigger": "manual"}

    async def discard_failed_record(self, failed_id: str, *, reason: str = "manual"):
        self.calls.append(("discard_failed_record", failed_id))
        if failed_id == "failed_missing":
            raise KeyError(f"failed memory record not found: {failed_id}")
        return {"failed_id": failed_id, "status": "discarded", "request_ids": ["write_1"]}

    def list_current_memories(self):
        return [{"memory_id": "Ab12Z9", "memory_body": "旧内容", "minimal_memory": "old->body"}]

    async def update_current_memory(self, memory_id: str, *, memory_body: str, minimal_memory=None):
        self.calls.append(("update_current_memory", (memory_id, memory_body, minimal_memory)))
        if memory_id == "missing":
            raise KeyError(f"memory not found: {memory_id}")
        if not str(memory_body or "").strip():
            raise ValueError("memory_body must not be empty")
        return {"memory_id": memory_id, "memory_body": memory_body, "updated_at": "2026-09-16T10:00:00+08:00"}

    async def delete_current_memories(self, memory_ids, *, reason: str = ""):
        self.calls.append(("delete_current_memories", (list(memory_ids), reason)))
        deleted = [mid for mid in memory_ids if mid != "missing"]
        missing = [mid for mid in memory_ids if mid == "missing"]
        return {"deleted": deleted, "missing": missing, "deleted_at": "2026-09-16T10:00:00+08:00"}


@pytest.fixture()
def stub_env(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    manager = _StubMemoryManager(workspace)
    monkeypatch.setattr(admin_rest, "_runtime_memory_manager", lambda: manager)
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    client = TestClient(app)
    return workspace, manager, client


def _audit_lines(workspace: Path) -> list[dict]:
    audit_file = workspace / "memory" / "admin_audit.jsonl"
    if not audit_file.exists():
        return []
    return [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 失败停车区端点
# ---------------------------------------------------------------------------


def test_get_memory_failed_returns_items_and_mutation_flag(stub_env, monkeypatch):
    workspace, manager, client = stub_env
    monkeypatch.delenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", raising=False)

    response = client.get("/api/memory/failed?limit=10&offset=0")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["total"] == 1
    assert payload["items"][0]["failed_id"] == "failed_1"
    assert payload["mutations_enabled"] is False


def test_memory_failed_mutations_are_disabled_without_feature_flag(stub_env, monkeypatch):
    workspace, manager, client = stub_env
    monkeypatch.delenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", raising=False)

    retry = client.post("/api/memory/failed/failed_1/retry", json={"reason": "manual"})
    discard = client.post("/api/memory/failed/failed_1/discard", json={"reason": "manual"})

    assert retry.status_code == 403
    assert retry.json()["detail"]["code"] == "memory_admin_mutation_disabled"
    assert discard.status_code == 403
    assert discard.json()["detail"]["code"] == "memory_admin_mutation_disabled"
    assert manager.calls == []


def test_memory_failed_retry_writes_audit_record(stub_env, monkeypatch):
    workspace, manager, client = stub_env
    monkeypatch.setenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", "1")

    response = client.post(
        "/api/memory/failed/failed_1/retry",
        json={"reason": "operator"},
        headers={"x-request-id": "req-failed-retry-1"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["item"]["failed_id"] == "failed_1"
    assert payload["item"]["audit_logged"] is True
    audit = _audit_lines(workspace)
    assert len(audit) == 1
    assert audit[0]["action"] == "retry_failed"
    assert audit[0]["failed_id"] == "failed_1"
    assert audit[0]["reason"] == "operator"
    assert audit[0]["request_id"] == "req-failed-retry-1"


def test_memory_failed_retry_maps_missing_and_conflict(stub_env, monkeypatch):
    workspace, manager, client = stub_env
    monkeypatch.setenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", "1")

    missing = client.post("/api/memory/failed/failed_missing/retry", json={})
    conflict = client.post("/api/memory/failed/failed_requeued/retry", json={})

    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "memory_failed_not_found"
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "memory_failed_not_parked"


def test_memory_failed_discard_writes_audit_record(stub_env, monkeypatch):
    workspace, manager, client = stub_env
    monkeypatch.setenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", "1")

    response = client.post("/api/memory/failed/failed_1/discard", json={"reason": "give-up"})

    assert response.status_code == 200
    assert response.json()["item"]["status"] == "discarded"
    audit = _audit_lines(workspace)
    assert len(audit) == 1
    assert audit[0]["action"] == "discard_failed"
    assert audit[0]["reason"] == "give-up"


# ---------------------------------------------------------------------------
# 当前记忆编辑 / 批量删除端点
# ---------------------------------------------------------------------------


def test_current_memory_mutations_are_disabled_without_feature_flag(stub_env, monkeypatch):
    workspace, manager, client = stub_env
    monkeypatch.delenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", raising=False)

    update = client.post("/api/memory/current/update", json={"memory_id": "Ab12Z9", "memory_body": "新内容"})
    delete = client.post("/api/memory/current/delete", json={"memory_ids": ["Ab12Z9"]})

    assert update.status_code == 403
    assert update.json()["detail"]["code"] == "memory_admin_mutation_disabled"
    assert delete.status_code == 403
    assert manager.calls == []


def test_get_current_memories_reports_mutation_flag(stub_env, monkeypatch):
    workspace, manager, client = stub_env
    monkeypatch.setenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", "1")

    response = client.get("/api/memory/current")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mutations_enabled"] is True
    assert payload["items"][0]["memory_id"] == "Ab12Z9"


def test_current_memory_update_writes_audit_and_maps_errors(stub_env, monkeypatch):
    workspace, manager, client = stub_env
    monkeypatch.setenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", "1")

    ok = client.post(
        "/api/memory/current/update",
        json={"memory_id": "Ab12Z9", "memory_body": "新内容", "minimal_memory": "new->body", "reason": "ui"},
        headers={"x-request-id": "req-update-1"},
    )
    assert ok.status_code == 200
    assert ok.json()["item"]["memory_body"] == "新内容"
    assert ok.json()["item"]["audit_logged"] is True

    empty = client.post("/api/memory/current/update", json={"memory_id": "Ab12Z9", "memory_body": "  "})
    assert empty.status_code == 400
    assert empty.json()["detail"]["code"] == "memory_current_invalid"

    missing = client.post("/api/memory/current/update", json={"memory_id": "missing", "memory_body": "x"})
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "memory_current_not_found"

    no_id = client.post("/api/memory/current/update", json={"memory_body": "x"})
    assert no_id.status_code == 400
    assert no_id.json()["detail"]["code"] == "memory_current_invalid_id"

    audit = _audit_lines(workspace)
    assert len(audit) == 1
    assert audit[0]["action"] == "update_current_memory"
    assert audit[0]["memory_id"] == "Ab12Z9"
    assert audit[0]["request_id"] == "req-update-1"


def test_current_memory_delete_reports_missing_and_writes_audit(stub_env, monkeypatch):
    workspace, manager, client = stub_env
    monkeypatch.setenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", "1")

    response = client.post("/api/memory/current/delete", json={"memory_ids": ["Ab12Z9", "missing"], "reason": "cleanup"})

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["deleted"] == ["Ab12Z9"]
    assert item["missing"] == ["missing"]
    assert item["audit_logged"] is True
    audit = _audit_lines(workspace)
    assert len(audit) == 1
    assert audit[0]["action"] == "delete_current_memories"
    assert audit[0]["memory_ids"] == ["Ab12Z9", "missing"]

    empty = client.post("/api/memory/current/delete", json={"memory_ids": []})
    assert empty.status_code == 400
    assert empty.json()["detail"]["code"] == "memory_current_invalid_id"


# ---------------------------------------------------------------------------
# MemoryManager 真实运行时：编辑 / 删除当前记忆并同步 MEMORY.md 镜像
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_current_memory_updates_sqlite_and_rebuilds_mirror(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        created = manager._memory_repo.create_memory(
            memory_body="完成任务必须说明任务总耗时",
            minimal_memory="任务完成->说明总耗时",
            source="user",
            from_user=True,
            now_iso="2026-09-16T10:00:00+08:00",
        )
        memory_id = str(created.get("memory_id") or "").strip()
        assert memory_id
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-09-16T10:00:00+08:00")
        assert "完成任务必须说明任务总耗时" in manager.snapshot_text()

        result = await manager.update_current_memory(
            memory_id,
            memory_body="完成任务必须说明任务总耗时与阻塞原因",
            minimal_memory="任务完成->说明总耗时与阻塞",
        )
        assert result["memory_id"] == memory_id

        rows = {str(row.get("memory_id")): row for row in manager.list_current_memories()}
        assert "完成任务必须说明任务总耗时与阻塞原因" in str(rows[memory_id]["memory_body"])
        assert str(rows[memory_id]["minimal_memory"]) == "任务完成->说明总耗时与阻塞"
        # 镜像同步重建
        assert "完成任务必须说明任务总耗时与阻塞原因" in manager.snapshot_text()

        with pytest.raises(KeyError):
            await manager.update_current_memory("NoPe12", memory_body="x")
        with pytest.raises(ValueError):
            await manager.update_current_memory(memory_id, memory_body="   ")
        with pytest.raises(ValueError):
            await manager.update_current_memory(memory_id, memory_body="x" * 400)
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_delete_current_memories_removes_rows_and_rebuilds_mirror(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        first = manager._memory_repo.create_memory(
            memory_body="第一条记忆内容",
            minimal_memory="first->memory",
            source="user",
            from_user=True,
            now_iso="2026-09-16T10:00:00+08:00",
        )
        second = manager._memory_repo.create_memory(
            memory_body="第二条记忆内容",
            minimal_memory="second->memory",
            source="user",
            from_user=True,
            now_iso="2026-09-16T10:01:00+08:00",
        )
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-09-16T10:02:00+08:00")
        assert "第一条记忆内容" in manager.snapshot_text()
        assert "第二条记忆内容" in manager.snapshot_text()

        result = await manager.delete_current_memories(
            [str(first["memory_id"]), "Missing9"],
            reason="cleanup",
        )
        assert result["deleted"] == [str(first["memory_id"])]
        assert result["missing"] == ["Missing9"]

        remaining = manager.list_current_memories()
        assert [str(row.get("memory_id")) for row in remaining] == [str(second["memory_id"])]
        assert "第一条记忆内容" not in manager.snapshot_text()
        assert "第二条记忆内容" in manager.snapshot_text()

        with pytest.raises(ValueError):
            await manager.delete_current_memories([])
    finally:
        manager.close()
