from __future__ import annotations

import gzip
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from main.models import NodeRecord, TaskRecord, TokenUsageSummary
from main.storage.sqlite_store import SQLiteTaskStore


def _task_record(task_id: str, root_node_id: str) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id="web:shared",
        title="demo",
        user_request="demo",
        status="in_progress",
        root_node_id=root_node_id,
        max_depth=1,
        created_at="2026-03-29T00:00:00+08:00",
        updated_at="2026-03-29T00:00:00+08:00",
        token_usage=TokenUsageSummary(tracked=True),
        metadata={},
    )


def _node_record(task_id: str, node_id: str) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        task_id=task_id,
        parent_node_id=None,
        root_node_id=node_id,
        depth=0,
        node_kind="execution",
        status="in_progress",
        goal="demo",
        prompt="demo",
        input="demo",
        output=[],
        check_result="",
        final_output="",
        can_spawn_children=False,
        created_at="2026-03-29T00:00:00+08:00",
        updated_at="2026-03-29T00:00:00+08:00",
        token_usage=TokenUsageSummary(tracked=True),
        token_usage_by_model=[],
        metadata={},
    )


def test_sqlite_task_store_uses_separate_query_connection(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    try:
        assert store._read_conn is not store._conn
        query_only = store._read_conn.execute("PRAGMA query_only").fetchone()
        assert int(query_only[0]) == 1
        assert store.writer_queue_depth() >= 0
    finally:
        store.close()


def test_sqlite_task_store_read_connection_observes_writer_updates(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    try:
        task_id = "task:demo"
        root_node_id = "node:root"
        store.upsert_task(_task_record(task_id, root_node_id))
        store.upsert_node(_node_record(task_id, root_node_id))

        task = store.get_task(task_id)
        node = store.get_node(root_node_id)

        assert task is not None
        assert task.task_id == task_id
        assert node is not None
        assert node.node_id == root_node_id
    finally:
        store.close()


def test_sqlite_task_store_uses_dedicated_writer_thread_for_serialized_writes(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    try:
        assert store._writer_thread is not None
        assert store._writer_thread.is_alive()

        task_id = "task:demo"
        root_node_id = "node:root"
        store.upsert_task(_task_record(task_id, root_node_id))

        failures: list[BaseException] = []

        def write_event(index: int) -> None:
            try:
                store.append_task_event(
                    task_id=task_id,
                    session_id="web:shared",
                    event_type="task.summary.patch",
                    created_at=f"2026-03-29T00:00:{index:02d}+08:00",
                    payload={"index": index},
                )
            except BaseException as exc:  # pragma: no cover - surfaced in assertion
                failures.append(exc)

        threads = [threading.Thread(target=write_event, args=(index,)) for index in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert failures == []
        events = store.list_task_events(task_id=task_id, limit=20)
        assert len(events) == 10
        assert sorted(int(event["payload"]["index"]) for event in events) == list(range(10))
    finally:
        store.close()


def test_sqlite_task_store_task_summary_outbox_keeps_latest_payload(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    try:
        first = store.put_task_summary_outbox(
            task_id="task:demo",
            session_id="web:shared",
            created_at="2026-03-29T00:00:00+08:00",
            payload={
                "event_type": "task.summary.patch",
                "session_id": "web:shared",
                "task_id": "task:demo",
                "data": {"task": {"task_id": "task:demo", "updated_at": "2026-03-29T00:00:00+08:00", "token_usage": {"input_tokens": 1}}},
            },
        )
        second = store.put_task_summary_outbox(
            task_id="task:demo",
            session_id="web:shared",
            created_at="2026-03-29T00:00:05+08:00",
            payload={
                "event_type": "task.summary.patch",
                "session_id": "web:shared",
                "task_id": "task:demo",
                "data": {"task": {"task_id": "task:demo", "updated_at": "2026-03-29T00:00:05+08:00", "token_usage": {"input_tokens": 9}}},
            },
        )

        pending = store.list_pending_task_summary_outbox(limit=10)
        assert len(pending) == 1
        assert first["version"] == 1
        assert second["version"] == 2
        assert pending[0]["payload"]["data"]["task"]["updated_at"] == "2026-03-29T00:00:05+08:00"
        assert pending[0]["payload"]["data"]["task"]["token_usage"]["input_tokens"] == 9
    finally:
        store.close()


def test_sqlite_task_store_live_patch_single_snapshot_overwrite(tmp_path: Path) -> None:
    """live.patch 单份快照：覆盖写 latest.json.gz，不写 task_events 行。"""
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    try:
        payload = {
            "task_id": "task:demo",
            "runtime_summary": {
                "active_node_ids": ["node:root"],
                "runnable_node_ids": ["node:root"],
                "waiting_node_ids": [],
                "frames": [],
            },
            "frame": {"node_id": "node:root", "phase": "before_model", "tool_calls": []},
            "removed_node_id": "",
        }
        assert store.write_task_live_snapshot("task:demo", json.dumps(payload, ensure_ascii=False))
        snapshot_path = store._event_history_dir / "task_demo" / "latest.json.gz"
        assert snapshot_path.exists()

        # 覆盖写：第二次写后仍只有一个文件，内容为最新 payload
        payload["frame"]["phase"] = "after_model"
        assert store.write_task_live_snapshot("task:demo", json.dumps(payload, ensure_ascii=False))
        files = [item.name for item in (store._event_history_dir / "task_demo").iterdir() if item.is_file()]
        assert files == ["latest.json.gz"]
        with gzip.open(snapshot_path, "rt", encoding="utf-8") as handle:
            stored = json.loads(handle.read())
        assert stored["frame"]["phase"] == "after_model"

        # 不产生 task_events 行
        assert store.list_task_events(task_id="task:demo", limit=10) == []

        # 空 task_id / 空 payload / 关闭开关均拒绝写
        assert not store.write_task_live_snapshot("", "{}")
        assert not store.write_task_live_snapshot("task:demo", "")
    finally:
        store.close()

    (tmp_path / "disabled").mkdir()
    disabled = SQLiteTaskStore(tmp_path / "disabled" / "runtime.sqlite3", event_history_enabled=False)
    try:
        assert not disabled.write_task_live_snapshot("task:demo", json.dumps({"frame": {}}))
        assert not (disabled._event_history_dir / "task_demo").exists()
    finally:
        disabled.close()


def test_sqlite_task_store_logs_sqlite_errorcode_and_name_for_read_queries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    captured: dict[str, object] = {}

    class _FakeBoundLogger:
        def error(self, message: str, *args) -> None:
            captured["message"] = message
            captured["args"] = args

    class _FakeLogger:
        def opt(self, *, exception=None):
            captured["exception"] = exception
            return _FakeBoundLogger()

    class _FailingReadConnection:
        def execute(self, sql: str, params: tuple[object, ...]):
            _ = (sql, params)
            exc = sqlite3.OperationalError("disk I/O error")
            exc.sqlite_errorcode = 10
            exc.sqlite_errorname = "SQLITE_IOERR"
            raise exc

        def close(self) -> None:
            return None

    monkeypatch.setattr("main.storage.sqlite_store.logger", _FakeLogger(), raising=False)
    store._read_conn = _FailingReadConnection()

    try:
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            store.get_task("task:demo")
    finally:
        store.close()

    assert captured["message"] == "sqlite read query failed: operation={} sqlite_errorcode={} sqlite_errorname={} path={}"
    assert captured["args"] == ("SELECT", 10, "SQLITE_IOERR", str(store.path))
    exc = captured["exception"]
    assert isinstance(exc, sqlite3.OperationalError)
    assert getattr(exc, "sqlite_errorcode", None) == 10
    assert getattr(exc, "sqlite_errorname", None) == "SQLITE_IOERR"
