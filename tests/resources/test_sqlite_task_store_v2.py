from __future__ import annotations

import threading
from pathlib import Path

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
