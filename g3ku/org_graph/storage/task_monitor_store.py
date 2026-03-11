from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from g3ku.org_graph.monitoring.models import TaskMonitorNodeRecord, TaskMonitorProjectRecord

T = TypeVar('T', bound=BaseModel)


class TaskMonitorStore:
    def __init__(self, path: Path):
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
            CREATE TABLE IF NOT EXISTS monitor_projects (
                project_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS monitor_nodes (
                node_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
        ]
        with self._lock, self._conn:
            for statement in statements:
                self._conn.execute(statement)

    def upsert_project(self, record: TaskMonitorProjectRecord) -> TaskMonitorProjectRecord:
        self._upsert(
            'monitor_projects',
            ['project_id', 'session_id', 'updated_at', 'payload_json'],
            [record.project_id, record.session_id, record.updated_at, record.model_dump_json()],
            'project_id',
        )
        return record

    def get_project(self, project_id: str) -> TaskMonitorProjectRecord | None:
        row = self._fetchone('SELECT payload_json FROM monitor_projects WHERE project_id = ?', (project_id,))
        return self._parse(row['payload_json'], TaskMonitorProjectRecord) if row else None

    def list_projects(self, session_id: str | None = None) -> list[TaskMonitorProjectRecord]:
        if session_id:
            rows = self._fetchall('SELECT payload_json FROM monitor_projects WHERE session_id = ? ORDER BY updated_at DESC', (session_id,))
        else:
            rows = self._fetchall('SELECT payload_json FROM monitor_projects ORDER BY updated_at DESC')
        return [self._parse(row['payload_json'], TaskMonitorProjectRecord) for row in rows]

    def upsert_node(self, record: TaskMonitorNodeRecord) -> TaskMonitorNodeRecord:
        self._upsert(
            'monitor_nodes',
            ['node_id', 'project_id', 'session_id', 'updated_at', 'payload_json'],
            [record.node_id, record.project_id, record.session_id, record.updated_at, record.model_dump_json()],
            'node_id',
        )
        return record

    def get_node(self, node_id: str) -> TaskMonitorNodeRecord | None:
        row = self._fetchone('SELECT payload_json FROM monitor_nodes WHERE node_id = ?', (node_id,))
        return self._parse(row['payload_json'], TaskMonitorNodeRecord) if row else None

    def list_nodes(self, project_id: str) -> list[TaskMonitorNodeRecord]:
        rows = self._fetchall('SELECT payload_json FROM monitor_nodes WHERE project_id = ? ORDER BY updated_at ASC', (project_id,))
        return [self._parse(row['payload_json'], TaskMonitorNodeRecord) for row in rows]

    def delete_nodes(self, node_ids: list[str]) -> None:
        values = [str(item or '').strip() for item in node_ids if str(item or '').strip()]
        if not values:
            return
        placeholders = ', '.join('?' for _ in values)
        with self._lock, self._conn:
            self._conn.execute(f'DELETE FROM monitor_nodes WHERE node_id IN ({placeholders})', tuple(values))

    def delete_project(self, project_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM monitor_nodes WHERE project_id = ?', (project_id,))
            self._conn.execute('DELETE FROM monitor_projects WHERE project_id = ?', (project_id,))

    def _upsert(self, table: str, columns: list[str], values: list[object], primary_key: str) -> None:
        placeholders = ', '.join('?' for _ in columns)
        updates = ', '.join(f"{column}=excluded.{column}" for column in columns if column != primary_key)
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT({primary_key}) DO UPDATE SET {updates}"
        with self._lock, self._conn:
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
