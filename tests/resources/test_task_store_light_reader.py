"""轻量读连接回归测试。

背景：任务大厅高频轮询的轻查询（worker-status / 任务列表）曾与打开大任务时的
重读取（MB 级 payload）串行在同一条读连接上，轻查询被重读阻塞 1s+。
修复（方案1）：为高频轻读单独开一条读连接（`_light_read_conn`），WAL 下与
重读并发互不阻塞。本测试固化该行为：轻读必须走独立连接，且重读进行中轻读
不被其拖慢。
"""

from __future__ import annotations

import threading
import time

from main.models import TaskRecord
from main.storage.sqlite_store import SQLiteTaskStore


def _make_store(tmp_path) -> SQLiteTaskStore:
    return SQLiteTaskStore(path=tmp_path / "runtime.sqlite3")


def test_light_reads_use_dedicated_connection(tmp_path) -> None:
    store = _make_store(tmp_path)
    try:
        assert store._light_read_conn is not store._read_conn
        assert store._light_read_lock is not store._read_lock
    finally:
        store.close()


def test_light_read_not_blocked_by_heavy_read(tmp_path) -> None:
    store = _make_store(tmp_path)
    try:
        # 用 30 个 1MB user_request 的任务制造一次可观的重读（整包 list_tasks）。
        big = "x" * (1024 * 1024)
        for index in range(30):
            store.upsert_task(TaskRecord(
                task_id=f"task:big{index}",
                session_id="web:s",
                title=f"t{index}",
                user_request=big,
                status="success",
                root_node_id="node:root",
                created_at="2026-09-14T10:00:00+08:00",
                updated_at=f"2026-09-14T10:{index:02d}:00+08:00",
            ))
        store.upsert_worker_status(
            worker_id="worker:test",
            role="task_worker",
            status="online",
            updated_at="2026-09-14T10:00:00+08:00",
            payload={"ok": True},
        )

        timing: dict[str, float] = {}

        def heavy() -> None:
            start = time.perf_counter()
            rows = store.list_tasks()
            timing["heavy"] = time.perf_counter() - start
            assert len(rows) == 30

        def light() -> None:
            time.sleep(0.03)  # 让重读先跑起来
            start = time.perf_counter()
            items = store.list_worker_status(role="task_worker")
            timing["light"] = time.perf_counter() - start
            assert items and items[0]["worker_id"] == "worker:test"

        heavy_thread = threading.Thread(target=heavy)
        light_thread = threading.Thread(target=light)
        heavy_thread.start()
        light_thread.start()
        heavy_thread.join()
        light_thread.join()

        # 轻读不能被重读拖到与其同量级：独立连接下应远快于重读。
        assert timing["light"] < timing["heavy"]
        assert timing["light"] < max(0.05, timing["heavy"] * 0.5)
    finally:
        store.close()


def test_light_read_methods_return_correct_rows(tmp_path) -> None:
    store = _make_store(tmp_path)
    try:
        store.upsert_task(TaskRecord(
            task_id="task:a",
            session_id="web:one",
            title="A",
            brief_text="brief-A",
            user_request="req",
            status="in_progress",
            root_node_id="node:root",
            created_at="2026-09-14T10:00:00+08:00",
            updated_at="2026-09-14T11:00:00+08:00",
        ))
        store.upsert_worker_status(
            worker_id="worker:test",
            role="task_worker",
            status="online",
            updated_at="2026-09-14T10:00:00+08:00",
            payload={"ok": True},
        )
        store.upsert_task_disk_usage("task:a", 1234)

        summaries = store.list_task_summaries("web:one")
        assert len(summaries) == 1
        assert summaries[0]["task_id"] == "task:a"
        assert summaries[0]["status"] == "in_progress"

        assert store.get_task_disk_usages(["task:a"]) == {"task:a": 1234}

        workers = store.list_worker_status(role="task_worker")
        assert len(workers) == 1
        assert workers[0]["payload"] == {"ok": True}
    finally:
        store.close()
