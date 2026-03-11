from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from g3ku.runtime.multi_agent.dynamic.types import BackgroundTaskRecord, DynamicSubagentRequest


class BackgroundTaskStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @staticmethod
    def utcnow() -> str:
        return datetime.now(UTC).isoformat()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS background_tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    parent_session_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_summary TEXT NOT NULL,
                    error TEXT,
                    metadata TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_background_parent ON background_tasks(parent_session_id)")

    def save(self, record: BackgroundTaskRecord) -> BackgroundTaskRecord:
        payload = record.model_dump(mode="json")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO background_tasks (
                    task_id, session_id, parent_session_id, category, status,
                    created_at, updated_at, result_summary, error, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["task_id"],
                    payload.get("session_id") or "",
                    payload["parent_session_id"],
                    payload["category"],
                    payload["status"],
                    payload["created_at"],
                    payload["updated_at"],
                    payload.get("result_summary") or "",
                    payload.get("error"),
                    json.dumps(payload.get("metadata") or {}, ensure_ascii=False),
                ),
            )
        return record

    def get(self, task_id: str) -> BackgroundTaskRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM background_tasks WHERE task_id = ?", (str(task_id or ""),)).fetchone()
        return self._row_to_record(row) if row else None

    def update(self, task_id: str, **changes: Any) -> BackgroundTaskRecord | None:
        record = self.get(task_id)
        if record is None:
            return None
        payload = record.model_dump()
        payload.update({key: value for key, value in changes.items() if value is not None})
        payload["updated_at"] = self.utcnow()
        updated = BackgroundTaskRecord.model_validate(payload)
        return self.save(updated)

    def list_by_parent(self, parent_session_id: str) -> list[BackgroundTaskRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM background_tasks WHERE parent_session_id = ? ORDER BY created_at DESC",
                (str(parent_session_id or ""),),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_all(self) -> list[BackgroundTaskRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM background_tasks ORDER BY created_at DESC").fetchall()
        return [self._row_to_record(row) for row in rows]

    def _row_to_record(self, row: sqlite3.Row) -> BackgroundTaskRecord:
        return BackgroundTaskRecord.model_validate(
            {
                "task_id": row["task_id"],
                "session_id": row["session_id"] or "",
                "parent_session_id": row["parent_session_id"],
                "category": row["category"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "result_summary": row["result_summary"] or "",
                "error": row["error"],
                "metadata": json.loads(row["metadata"] or "{}"),
            }
        )


class BackgroundPool:
    def __init__(self, *, controller, store: BackgroundTaskStore, max_parallel_tasks: int = 8) -> None:
        self._controller = controller
        self._store = store
        self._queue: asyncio.Queue[tuple[str, DynamicSubagentRequest, str | None]] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._active: dict[str, asyncio.Task[Any]] = {}
        self._max_parallel_tasks = max(1, int(max_parallel_tasks or 1))
        self._started = False
        self._closing = False

    @property
    def store(self) -> BackgroundTaskStore:
        return self._store

    def _utcnow(self) -> str:
        return self._store.utcnow()

    def ensure_started(self) -> None:
        if self._started or self._closing:
            return
        self._started = True
        for idx in range(self._max_parallel_tasks):
            self._workers.append(asyncio.create_task(self._worker_loop(idx), name=f"g3ku-bg-worker-{idx}"))

    async def _worker_loop(self, index: int) -> None:
        while True:
            try:
                task_id, request, session_id = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                if self._closing:
                    self._mark_task_canceled(task_id)
                    continue
                record = self._store.get(task_id)
                if record is None:
                    continue
                if record.status == "paused":
                    continue
                run_task = asyncio.create_task(
                    self._controller.run_background_job(task_id=task_id, request=request, session_id=session_id),
                    name=f"g3ku-background-job-{task_id}",
                )
                self._active[task_id] = run_task
                try:
                    await run_task
                except asyncio.CancelledError:
                    self._mark_task_canceled(task_id)
                finally:
                    self._active.pop(task_id, None)
            finally:
                self._queue.task_done()

    async def launch(self, request: DynamicSubagentRequest) -> BackgroundTaskRecord:
        self.ensure_started()
        task_id = self._controller.allocate_task_id()
        now = self._utcnow()
        record = BackgroundTaskRecord(
            task_id=task_id,
            session_id="",
            parent_session_id=request.parent_session_id,
            category=request.category or "",
            status="pending",
            created_at=now,
            updated_at=now,
            metadata={"request": request.model_dump(mode="json")},
        )
        self._store.save(record)
        await self._queue.put((task_id, request, None))
        return record

    async def pause(self, *, task_id: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        record = self._resolve_record(task_id=task_id, session_id=session_id)
        if record is None:
            return {"ok": False, "status": "not_found"}
        active = self._active.get(record.task_id)
        if active is not None and not active.done():
            active.cancel()
        self._store.update(record.task_id, status="paused")
        return {"ok": True, "status": "paused", "task_id": record.task_id, "session_id": record.session_id}

    async def resume(self, *, task_id: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        record = self._resolve_record(task_id=task_id, session_id=session_id)
        if record is None:
            return {"ok": False, "status": "not_found"}
        request_payload = dict(record.metadata.get("request") or {})
        if not request_payload:
            return {"ok": False, "status": "missing_request", "task_id": record.task_id}
        request = DynamicSubagentRequest.model_validate(request_payload)
        self._store.update(record.task_id, status="pending")
        await self._queue.put((record.task_id, request, record.session_id or None))
        return {"ok": True, "status": "pending", "task_id": record.task_id, "session_id": record.session_id}

    async def cancel(self, *, task_id: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        record = self._resolve_record(task_id=task_id, session_id=session_id)
        if record is None:
            return {"ok": False, "status": "not_found"}
        active = self._active.get(record.task_id)
        if active is not None and not active.done():
            active.cancel()
        self._mark_task_canceled(record.task_id)
        return {"ok": True, "status": "canceled", "task_id": record.task_id, "session_id": record.session_id}

    def status(self, *, task_id: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        record = self._resolve_record(task_id=task_id, session_id=session_id)
        if record is None:
            return {"ok": False, "status": "not_found"}
        return {"ok": True, **record.model_dump(mode="json")}

    async def cancel_by_parent_session(self, parent_session_id: str) -> int:
        total = 0
        for record in self._store.list_by_parent(parent_session_id):
            if record.status in {"completed", "failed", "canceled"}:
                continue
            await self.cancel(task_id=record.task_id)
            total += 1
        return total

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True

        while True:
            try:
                task_id, _request, _session_id = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                self._mark_task_canceled(task_id)
            finally:
                self._queue.task_done()

        active_tasks = list(self._active.items())
        for _task_id, task in active_tasks:
            if not task.done():
                task.cancel()
        if active_tasks:
            await asyncio.gather(*(task for _, task in active_tasks), return_exceptions=True)
        self._active.clear()

        workers = list(self._workers)
        for worker in workers:
            if not worker.done():
                worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        self._started = False

    def list_by_parent(self, parent_session_id: str) -> list[BackgroundTaskRecord]:
        return self._store.list_by_parent(parent_session_id)

    def _resolve_record(self, *, task_id: str | None = None, session_id: str | None = None) -> BackgroundTaskRecord | None:
        if task_id:
            return self._store.get(task_id)
        if session_id:
            for record in self._store.list_all():
                if record.session_id == session_id:
                    return record
        return None

    def _mark_task_canceled(self, task_id: str) -> None:
        record = self._store.update(task_id, status="canceled")
        if record is not None and record.session_id:
            self._controller.session_store.update(record.session_id, status="canceled")

