"""记忆管理新增管理面（失败停车区 + 当前记忆编辑/删除）的端点与运行时测试。"""

from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime.session_trust import is_owner_session
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
        self.enqueue_kwargs: list[dict[str, object]] = []

    async def list_failed_page(self, *, limit: int = 20, offset: int = 0):
        self.calls.append(("list_failed_page", {"limit": limit, "offset": offset}))
        return {
            "items": [{"failed_id": "failed_1", "status": "parked", "category": "provider_error"}],
            "total": 1,
            "has_more": False,
        }

    async def get_payload_window(self, kind: str, key: str, *, request_id: str = "", start_char: int = 1):
        self.calls.append(("get_payload_window", (kind, key, request_id, start_char)))
        if key == "failed_missing" or key == "batch_missing":
            raise KeyError(f"memory payload not found: {kind}/{key}")
        total = 50000
        start = max(1, int(start_char))
        end = min(start + 19999, total)
        return {
            "kind": kind,
            "key": key,
            "request_id": request_id or "write_1",
            "text": "y" * (end - start + 1),
            "start_char": start,
            "end_char": end,
            "char_count": total,
            "truncated": end < total,
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

    def list_consolidation_categories(self):
        self.calls.append(("list_consolidation_categories", ()))
        return [
            {
                "note_ref": "note_local_env",
                "handle": "n0bfc7",
                "scope_line": "大类:端口、反代、18790",
                "scope_line_over_limit": False,
                "entry_count": 2,
                "unusable": False,
            },
            {
                "note_ref": "note_gone",
                "handle": "n1a2b3",
                "scope_line": "",
                "scope_line_over_limit": False,
                "entry_count": 1,
                "unusable": True,
            },
            {
                "note_ref": "note_long",
                "handle": "c9f8e7",
                "scope_line": "大类:" + "词" * 600,
                "scope_line_over_limit": True,
                "entry_count": 3,
                "unusable": True,
            },
        ]

    async def enqueue_operator_request(
        self,
        *,
        op: str,
        decision_source: str,
        payload_text: str,
        trigger_source: str,
        session_key: str = "",
    ):
        self.calls.append((op, decision_source, payload_text, trigger_source))
        self.enqueue_kwargs.append(
            {
                "op": op,
                "decision_source": decision_source,
                "payload_text": payload_text,
                "trigger_source": trigger_source,
                "session_key": session_key,
            }
        )
        if not str(payload_text or "").strip():
            raise ValueError("payload_text is required")
        return {"ok": True, "status": "queued", "request_id": f"{op}_1"}

    async def uncompress_current_memory(self, memory_id: str):
        self.calls.append(("uncompress_current_memory", memory_id))
        if memory_id == "missing":
            raise KeyError(f"memory not found: {memory_id}")
        if memory_id == "boom":
            raise ValueError("memory document exceeds 20000 chars")
        return {
            "memory_id": memory_id,
            "changed": True,
            "resident_chars_after": 1421,
            "compress_trigger_chars": 16000,
            "document_max_chars": 20000,
        }

    async def update_note(self, ref, *, body: str):
        self.calls.append(("update_note", (ref, body)))
        if ref == "note_missing":
            raise KeyError(f"memory note not found: {ref}")
        if not str(body or "").strip():
            raise ValueError("note body must not be empty")
        return {"ref": ref, "body": body, "updated_at": "2026-09-16T10:00:00+08:00"}


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
    assert payload["mutations_enabled"] is True


def _payload_window_assertions(client, manager, url, kind, key, expected_request_id="write_1"):
    response = client.get(url)
    assert response.status_code == 200
    item = response.json()["item"]
    assert item["kind"] == kind
    assert item["key"] == key
    assert item["start_char"] == 4001
    assert item["end_char"] == 24000
    assert len(item["text"]) == 20000
    assert item["char_count"] == 50000
    assert item["truncated"] is True
    assert manager.calls[-1] == ("get_payload_window", (kind, key, expected_request_id, 4001))


def test_memory_payload_routes_page_by_chars_across_three_surfaces(stub_env):
    """三面共用一个分页口：路径不同、寻址形状相同，交付区间原样透出。"""
    workspace, manager, client = stub_env

    _payload_window_assertions(
        client, manager,
        "/api/memory/failed/failed_1/payload?request_id=write_1&start_char=4001",
        "failed", "failed_1",
    )
    _payload_window_assertions(
        client, manager,
        "/api/memory/processed/batch_1/payload?request_id=write_1&start_char=4001",
        "processed", "batch_1",
    )
    _payload_window_assertions(
        client, manager,
        "/api/memory/queue/write_9/payload?start_char=4001",
        # 队列面的 key 本身就是 request_id，路由不再重复传一个。
        "queue", "write_9", "",
    )


def test_memory_payload_route_maps_missing_record_to_404(stub_env):
    workspace, manager, client = stub_env

    missing = client.get("/api/memory/failed/failed_missing/payload")

    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "memory_payload_not_found"


def test_memory_failed_mutations_work_without_feature_flag(stub_env, monkeypatch):
    """失败重试/放弃面已取消 env 门控：未设置开关也应可用（审计仍记录）。"""
    workspace, manager, client = stub_env
    monkeypatch.delenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", raising=False)

    retry = client.post("/api/memory/failed/failed_1/retry", json={"reason": "manual"})
    discard = client.post("/api/memory/failed/failed_1/discard", json={"reason": "manual"})

    assert retry.status_code == 200
    assert retry.json()["item"]["failed_id"] == "failed_1"
    assert discard.status_code == 200
    assert discard.json()["item"]["status"] == "discarded"
    audit = _audit_lines(workspace)
    assert [record["action"] for record in audit] == ["retry_failed", "discard_failed"]


def test_legacy_retry_head_keeps_feature_flag_gate(stub_env, monkeypatch):
    """遗留 retry-head 端点仍保留 env 门控（与新记忆运维面不同）。"""
    workspace, manager, client = stub_env
    monkeypatch.delenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", raising=False)

    response = client.post("/api/memory/admin/retry-head", json={"reason": "manual"})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "memory_admin_mutation_disabled"


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


def test_current_memory_mutations_work_without_feature_flag(stub_env, monkeypatch):
    """记忆内容编辑/删除面已取消 env 门控：两条动作现在都只是入队，审计照记。"""
    workspace, manager, client = stub_env
    monkeypatch.delenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", raising=False)

    update = client.post("/api/memory/current/update", json={"memory_id": "Ab12Z9", "memory_body": "新内容"})
    delete = client.post("/api/memory/current/delete", json={"memory_ids": ["Ab12Z9"]})

    assert update.status_code == 200
    assert update.json()["item"]["status"] == "queued"
    assert delete.status_code == 200
    assert delete.json()["item"]["status"] == "queued"
    assert [call[0] for call in manager.calls] == ["write", "delete"], "neither route touches the store"
    audit = _audit_lines(workspace)
    assert [record["action"] for record in audit] == ["update_current_memory", "delete_current_memories"]
    assert [record["result"] for record in audit] == ["queued", "queued"]


def test_get_current_memories_reports_mutation_flag(stub_env, monkeypatch):
    workspace, manager, client = stub_env
    monkeypatch.setenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", "1")

    response = client.get("/api/memory/current")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mutations_enabled"] is True
    assert payload["items"][0]["memory_id"] == "Ab12Z9"


def test_current_memory_update_enqueues_a_request_and_maps_errors(stub_env, monkeypatch):
    workspace, manager, client = stub_env
    monkeypatch.setenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", "1")

    ok = client.post(
        "/api/memory/current/update",
        json={"memory_id": "Ab12Z9", "memory_body": "新内容", "minimal_memory": "新的->正文", "reason": "ui"},
        headers={"x-request-id": "req-update-1"},
    )
    assert ok.status_code == 200
    assert ok.json()["item"]["request_id"] == "write_1"
    assert ok.json()["item"]["audit_logged"] is True
    op, decision_source, payload_text, trigger_source = manager.calls[0]
    assert op == "write" and decision_source == "user" and trigger_source == "admin_ui_edit"
    assert "id:Ab12Z9" in payload_text and "新内容" in payload_text and "新的->正文" in payload_text
    assert "只改这一条记忆" in payload_text, "the request bounds its own scope"
    enqueued = manager.enqueue_kwargs[0]
    assert enqueued["session_key"] == admin_rest.MEMORY_ADMIN_SESSION_KEY
    assert is_owner_session(str(enqueued["session_key"]))

    empty = client.post("/api/memory/current/update", json={"memory_id": "Ab12Z9", "memory_body": "  "})
    assert empty.status_code == 400
    assert empty.json()["detail"]["code"] == "memory_current_invalid"

    no_id = client.post("/api/memory/current/update", json={"memory_body": "x"})
    assert no_id.status_code == 400
    assert no_id.json()["detail"]["code"] == "memory_current_invalid_id"

    audit = _audit_lines(workspace)
    assert len(audit) == 1
    assert audit[0]["action"] == "update_current_memory"
    assert audit[0]["memory_id"] == "Ab12Z9"
    assert audit[0]["request_id"] == "req-update-1"


def test_current_memory_update_separates_clear_from_unchanged(stub_env, monkeypatch):
    """三态：没带 minimal_memory 才是不改，带了但为空是显式清空。

    清空被读成"保持不变"时，操作员点保存、界面报成功，那一格永远还在——请求正文
    是这个字段唯一的载体，所以判据必须分得开这两种意图。
    """
    workspace, manager, client = stub_env
    monkeypatch.setenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", "1")

    cleared = client.post(
        "/api/memory/current/update",
        json={"memory_id": "Ab12Z9", "memory_body": "新内容", "minimal_memory": ""},
    )
    assert cleared.status_code == 200
    cleared_text = manager.calls[0][2]
    assert "清空" in cleared_text
    assert "保持不变" not in cleared_text

    kept = client.post("/api/memory/current/update", json={"memory_id": "Ab12Z9", "memory_body": "新内容"})
    assert kept.status_code == 200
    assert "保持不变" in manager.calls[1][2]
    assert "清空" not in manager.calls[1][2]

    replaced = client.post(
        "/api/memory/current/update",
        json={"memory_id": "Ab12Z9", "memory_body": "新内容", "minimal_memory": "新的->正文"},
    )
    assert replaced.status_code == 200
    replaced_text = manager.calls[2][2]
    assert "新最小记忆：新的->正文" in replaced_text
    assert "保持不变" not in replaced_text and "清空" not in replaced_text


def test_current_memory_delete_enqueues_one_request_per_action(stub_env, monkeypatch):
    workspace, manager, client = stub_env
    monkeypatch.setenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", "1")

    response = client.post(
        "/api/memory/current/delete",
        json={"memory_ids": ["Ab12Z9", "missing"], "reason": "cleanup", "note_refs": ["note_keep"]},
    )

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["status"] == "queued"
    assert item["memory_ids"] == ["Ab12Z9", "missing"]
    assert item["audit_logged"] is True
    op, _source, payload_text, trigger_source = manager.calls[0]
    assert op == "delete" and trigger_source == "admin_ui_delete"
    assert "id:Ab12Z9" in payload_text and "id:missing" in payload_text and "cleanup" in payload_text
    enqueued = manager.enqueue_kwargs[0]
    assert enqueued["session_key"] == admin_rest.MEMORY_ADMIN_SESSION_KEY
    assert is_owner_session(str(enqueued["session_key"]))
    # 笔记不进请求体：界面没有这个动作，回收只认所有权列。多余的 note_refs 字段照旧被忽略。
    assert "note_keep" not in payload_text
    audit = _audit_lines(workspace)
    assert audit[0]["action"] == "delete_current_memories"
    assert audit[0]["memory_ids"] == ["Ab12Z9", "missing"]
    assert "note_refs_requested" not in audit[0]
    assert audit[0]["queued_request_id"] == "delete_1"

    empty = client.post("/api/memory/current/delete", json={"memory_ids": []})
    assert empty.status_code == 400
    assert empty.json()["detail"]["code"] == "memory_current_invalid_id"


def test_admin_ui_queue_requests_survive_the_memory_trust_gate(tmp_path: Path):
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(
        tmp_path,
        _memory_cfg(),
        review_eligibility=is_owner_session,
    )
    try:
        admin = module.MemoryQueueRequest(
            op="delete",
            decision_source="user",
            payload_text="操作员在管理页要求删除长期记忆：第 8 行。",
            created_at="2026-10-28T14:34:34+08:00",
            request_id="delete_admin",
            trigger_source="admin_ui_delete",
            session_key=admin_rest.MEMORY_ADMIN_SESSION_KEY,
        )
        # 回归样本：修复前管理页入队不带 session_key，空键被记忆门按第三方丢弃。
        anonymous = module.MemoryQueueRequest(
            op="delete",
            decision_source="user",
            payload_text="操作员在管理页要求删除长期记忆：第 9 行。",
            created_at="2026-10-28T14:34:35+08:00",
            request_id="delete_anonymous",
            trigger_source="admin_ui_delete",
            session_key="",
        )
        batch, dropped = manager._partition_queue_batch_by_eligibility(
            module.MemoryBatch(op="delete", items=[admin, anonymous])
        )
        assert [item.request_id for item in batch.items] == ["delete_admin"]
        assert [item.request_id for item in dropped] == ["delete_anonymous"]
    finally:
        manager.close()


def test_current_memory_uncompress_route_warms_one_row(stub_env, monkeypatch):
    workspace, manager, client = stub_env
    monkeypatch.setenv("G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS", "1")

    ok = client.post("/api/memory/current/uncompress", json={"memory_id": "Ab12Z9", "reason": "operator"})
    assert ok.status_code == 200
    assert ok.json()["item"]["changed"] is True
    assert ok.json()["item"]["resident_chars_after"] == 1421

    missing = client.post("/api/memory/current/uncompress", json={"memory_id": "missing"})
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "memory_current_not_found"

    # 顶到硬顶的回暖被整笔拒回（ValueError 带 "exceeds"），这条道不能留下
    # "库加了字符、镜像写不下"的中间态；对操作员它就是"这条放不回去"。
    refused = client.post("/api/memory/current/uncompress", json={"memory_id": "boom"})
    assert refused.status_code == 400
    assert refused.json()["detail"]["code"] == "memory_current_invalid"
    assert "exceeds" in refused.json()["detail"]["message"]

    no_id = client.post("/api/memory/current/uncompress", json={})
    assert no_id.status_code == 400

    audit = _audit_lines(workspace)
    assert [record["action"] for record in audit] == ["uncompress_current_memory"]
    assert audit[0]["changed"] is True


def test_current_memory_routes_have_no_direct_store_writers_left(stub_env, monkeypatch):
    """一扇门：管理页不再有任何绕过记忆 agent 的写道。"""
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager
    for removed in ("update_current_memory", "delete_current_memories"):
        assert not hasattr(manager, removed), f"{removed} would be a second write door with its own gates"
    repository = module._MemorySqliteRepository
    assert not hasattr(repository, "update_memory"), "the row-level writer went with it"


# ---------------------------------------------------------------------------
# MemoryManager 真实运行时：note 正文编辑（记忆行的写一律走队列）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MemoryManager 真实运行时：note 编辑与删除记忆时同步删除勾选笔记
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_note_writes_body_and_rejects_empty_or_missing(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        note_path = manager.notes_dir / "note_demo1.md"
        note_path.write_text("旧 note 正文", encoding="utf-8")

        result = await manager.update_note("note_demo1", body="新 note 正文\n第二行")
        assert result["ref"] == "note_demo1"
        assert note_path.read_text(encoding="utf-8") == "新 note 正文\n第二行"

        with pytest.raises(KeyError):
            await manager.update_note("note_missing", body="x")
        with pytest.raises(ValueError):
            await manager.update_note("note_demo1", body="   ")
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# 归并记忆浏览面（只读列表）
# ---------------------------------------------------------------------------


def test_index_notes_endpoint_lists_classes_and_keeps_broken_ones(stub_env):
    workspace, manager, client = stub_env

    response = client.get("/api/memory/index-notes")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["total"] == 3
    assert payload["items"][0]["note_ref"] == "note_local_env"
    assert payload["items"][0]["handle"] == "n0bfc7"
    # 断链分类照样返回：浏览面少一类比多一类更难查
    assert payload["items"][1]["unusable"] is True
    # 超线的说明行不进常驻块，但这一位必须留给浏览面去说
    assert payload["items"][2]["scope_line_over_limit"] is True
    assert payload["items"][2]["unusable"] is True
    assert ("list_consolidation_categories", ()) in manager.calls
    assert _audit_lines(workspace) == []
