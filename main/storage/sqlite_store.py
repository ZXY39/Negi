from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from main.models import NodeRecord, TaskArtifactRecord, TaskRecord

T = TypeVar('T', bound=BaseModel)


class SQLiteTaskStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute('PRAGMA journal_mode=WAL')
        self._setup()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _setup(self) -> None:
        statements = [
            '''
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                parent_node_id TEXT,
                root_node_id TEXT NOT NULL,
                depth INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                node_id TEXT,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            'CREATE INDEX IF NOT EXISTS idx_nodes_task_id ON nodes(task_id)',
            'CREATE INDEX IF NOT EXISTS idx_nodes_parent_node_id ON nodes(parent_node_id)',
            'CREATE INDEX IF NOT EXISTS idx_artifacts_task_id ON artifacts(task_id)',
            'CREATE INDEX IF NOT EXISTS idx_artifacts_node_id ON artifacts(node_id)',
        ]
        with self._lock, self._conn:
            for statement in statements:
                self._conn.execute(statement)

    def upsert_task(self, record: TaskRecord) -> TaskRecord:
        self._upsert(
            'tasks',
            ['task_id', 'session_id', 'status', 'updated_at', 'payload_json'],
            [record.task_id, record.session_id, record.status, record.updated_at, record.model_dump_json()],
            'task_id',
        )
        return record

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._fetchone('SELECT payload_json FROM tasks WHERE task_id = ?', (task_id,))
        return self._parse(row['payload_json'], TaskRecord) if row else None

    def list_tasks(self, session_id: str | None = None) -> list[TaskRecord]:
        if session_id:
            rows = self._fetchall('SELECT payload_json FROM tasks WHERE session_id = ? ORDER BY updated_at DESC', (session_id,))
        else:
            rows = self._fetchall('SELECT payload_json FROM tasks ORDER BY updated_at DESC')
        return [self._parse(row['payload_json'], TaskRecord) for row in rows]

    def update_task(self, task_id: str, mutator) -> TaskRecord | None:
        with self._lock, self._conn:
            row = self._conn.execute('SELECT payload_json FROM tasks WHERE task_id = ?', (task_id,)).fetchone()
            if row is None:
                return None
            record = self._parse(row['payload_json'], TaskRecord)
            updated = mutator(record)
            self._upsert_unlocked(
                'tasks',
                ['task_id', 'session_id', 'status', 'updated_at', 'payload_json'],
                [updated.task_id, updated.session_id, updated.status, updated.updated_at, updated.model_dump_json()],
                'task_id',
            )
            return updated

    def upsert_node(self, record: NodeRecord) -> NodeRecord:
        self._upsert(
            'nodes',
            ['node_id', 'task_id', 'parent_node_id', 'root_node_id', 'depth', 'status', 'created_at', 'updated_at', 'payload_json'],
            [
                record.node_id,
                record.task_id,
                record.parent_node_id,
                record.root_node_id,
                record.depth,
                record.status,
                record.created_at,
                record.updated_at,
                record.model_dump_json(),
            ],
            'node_id',
        )
        return record

    def get_node(self, node_id: str) -> NodeRecord | None:
        row = self._fetchone('SELECT payload_json FROM nodes WHERE node_id = ?', (node_id,))
        return self._parse(row['payload_json'], NodeRecord) if row else None

    def list_nodes(self, task_id: str) -> list[NodeRecord]:
        rows = self._fetchall('SELECT payload_json FROM nodes WHERE task_id = ? ORDER BY created_at ASC, node_id ASC', (task_id,))
        return [self._parse(row['payload_json'], NodeRecord) for row in rows]

    def list_children(self, parent_node_id: str) -> list[NodeRecord]:
        rows = self._fetchall('SELECT payload_json FROM nodes WHERE parent_node_id = ? ORDER BY created_at ASC, node_id ASC', (parent_node_id,))
        return [self._parse(row['payload_json'], NodeRecord) for row in rows]

    def update_node(self, node_id: str, mutator) -> NodeRecord | None:
        with self._lock, self._conn:
            row = self._conn.execute('SELECT payload_json FROM nodes WHERE node_id = ?', (node_id,)).fetchone()
            if row is None:
                return None
            record = self._parse(row['payload_json'], NodeRecord)
            updated = mutator(record)
            self._upsert_unlocked(
                'nodes',
                ['node_id', 'task_id', 'parent_node_id', 'root_node_id', 'depth', 'status', 'created_at', 'updated_at', 'payload_json'],
                [
                    updated.node_id,
                    updated.task_id,
                    updated.parent_node_id,
                    updated.root_node_id,
                    updated.depth,
                    updated.status,
                    updated.created_at,
                    updated.updated_at,
                    updated.model_dump_json(),
                ],
                'node_id',
            )
            return updated

    def upsert_artifact(self, record: TaskArtifactRecord) -> TaskArtifactRecord:
        self._upsert(
            'artifacts',
            ['artifact_id', 'task_id', 'node_id', 'created_at', 'payload_json'],
            [record.artifact_id, record.task_id, record.node_id, record.created_at, record.model_dump_json()],
            'artifact_id',
        )
        return record

    def get_artifact(self, artifact_id: str) -> TaskArtifactRecord | None:
        row = self._fetchone('SELECT payload_json FROM artifacts WHERE artifact_id = ?', (artifact_id,))
        return self._parse(row['payload_json'], TaskArtifactRecord) if row else None

    def list_artifacts(self, task_id: str) -> list[TaskArtifactRecord]:
        rows = self._fetchall('SELECT payload_json FROM artifacts WHERE task_id = ? ORDER BY created_at ASC, artifact_id ASC', (task_id,))
        return [self._parse(row['payload_json'], TaskArtifactRecord) for row in rows]

    def delete_task(self, task_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM artifacts WHERE task_id = ?', (task_id,))
            self._conn.execute('DELETE FROM nodes WHERE task_id = ?', (task_id,))
            self._conn.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))

    def _upsert(self, table: str, columns: list[str], values: list[object], primary_key: str) -> None:
        with self._lock, self._conn:
            self._upsert_unlocked(table, columns, values, primary_key)

    def _upsert_unlocked(self, table: str, columns: list[str], values: list[object], primary_key: str) -> None:
        placeholders = ', '.join('?' for _ in columns)
        updates = ', '.join(f"{column}=excluded.{column}" for column in columns if column != primary_key)
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT({primary_key}) DO UPDATE SET {updates}"
        self._conn.execute(sql, values)

    def _fetchone(self, sql: str, params: tuple[object, ...]) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    @staticmethod
    def _parse(payload_json: str, model_cls: type[T]) -> T:
        return model_cls.model_validate(json.loads(payload_json))
