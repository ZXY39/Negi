from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from g3ku.org_graph.models import (
    PendingProjectNotice,
    ProjectArtifactRecord,
    ProjectRecord,
    UnitAgentRecord,
    UnitStageRecord,
)

T = TypeVar('T', bound=BaseModel)


class ProjectStore:
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
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS units (
                unit_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                parent_unit_id TEXT,
                level INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS stages (
                stage_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS notices (
                notice_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                acknowledged INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                unit_id TEXT,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
        ]
        with self._lock, self._conn:
            for statement in statements:
                self._conn.execute(statement)

    def upsert_project(self, record: ProjectRecord) -> ProjectRecord:
        self._upsert(
            'projects',
            ['project_id', 'session_id', 'status', 'updated_at', 'payload_json'],
            [record.project_id, record.session_id, record.status, record.updated_at, record.model_dump_json()],
            'project_id',
        )
        return record

    def list_projects(self, session_id: str | None = None) -> list[ProjectRecord]:
        if session_id:
            rows = self._fetchall('SELECT payload_json FROM projects WHERE session_id = ? ORDER BY updated_at DESC', (session_id,))
        else:
            rows = self._fetchall('SELECT payload_json FROM projects ORDER BY updated_at DESC')
        return [self._parse(row['payload_json'], ProjectRecord) for row in rows]

    def get_project(self, project_id: str) -> ProjectRecord | None:
        row = self._fetchone('SELECT payload_json FROM projects WHERE project_id = ?', (project_id,))
        return self._parse(row['payload_json'], ProjectRecord) if row else None

    def upsert_unit(self, record: UnitAgentRecord) -> UnitAgentRecord:
        self._upsert(
            'units',
            ['unit_id', 'project_id', 'parent_unit_id', 'level', 'status', 'created_at', 'updated_at', 'payload_json'],
            [record.unit_id, record.project_id, record.parent_unit_id, record.level, record.status, record.created_at, record.updated_at, record.model_dump_json()],
            'unit_id',
        )
        self.recount_project_units(record.project_id)
        return record

    def get_unit(self, unit_id: str) -> UnitAgentRecord | None:
        row = self._fetchone('SELECT payload_json FROM units WHERE unit_id = ?', (unit_id,))
        return self._parse(row['payload_json'], UnitAgentRecord) if row else None

    def list_units(self, project_id: str) -> list[UnitAgentRecord]:
        rows = self._fetchall('SELECT payload_json FROM units WHERE project_id = ? ORDER BY level ASC, created_at ASC', (project_id,))
        return [self._parse(row['payload_json'], UnitAgentRecord) for row in rows]

    def delete_units(self, unit_ids: list[str]) -> None:
        values = [str(item or '').strip() for item in unit_ids if str(item or '').strip()]
        if not values:
            return
        placeholders = ', '.join('?' for _ in values)
        with self._lock, self._conn:
            self._conn.execute(f'DELETE FROM units WHERE unit_id IN ({placeholders})', tuple(values))

    def upsert_stage(self, record: UnitStageRecord) -> UnitStageRecord:
        self._upsert(
            'stages',
            ['stage_id', 'project_id', 'unit_id', 'idx', 'status', 'payload_json'],
            [record.stage_id, record.project_id, record.unit_id, record.index, record.status, record.model_dump_json()],
            'stage_id',
        )
        return record

    def list_stages(self, project_id: str) -> list[UnitStageRecord]:
        rows = self._fetchall('SELECT payload_json FROM stages WHERE project_id = ? ORDER BY idx ASC', (project_id,))
        return [self._parse(row['payload_json'], UnitStageRecord) for row in rows]

    def delete_stages_for_units(self, unit_ids: list[str]) -> None:
        values = [str(item or '').strip() for item in unit_ids if str(item or '').strip()]
        if not values:
            return
        placeholders = ', '.join('?' for _ in values)
        with self._lock, self._conn:
            self._conn.execute(f'DELETE FROM stages WHERE unit_id IN ({placeholders})', tuple(values))

    def get_stage(self, stage_id: str) -> UnitStageRecord | None:
        row = self._fetchone('SELECT payload_json FROM stages WHERE stage_id = ?', (stage_id,))
        return self._parse(row['payload_json'], UnitStageRecord) if row else None

    def upsert_notice(self, record: PendingProjectNotice) -> PendingProjectNotice:
        self._upsert(
            'notices',
            ['notice_id', 'session_id', 'project_id', 'acknowledged', 'created_at', 'payload_json'],
            [record.notice_id, record.session_id, record.project_id, 1 if record.acknowledged else 0, record.created_at, record.model_dump_json()],
            'notice_id',
        )
        return record

    def list_notices(self, session_id: str, *, include_acknowledged: bool = False) -> list[PendingProjectNotice]:
        if include_acknowledged:
            rows = self._fetchall('SELECT payload_json FROM notices WHERE session_id = ? ORDER BY created_at DESC', (session_id,))
        else:
            rows = self._fetchall('SELECT payload_json FROM notices WHERE session_id = ? AND acknowledged = 0 ORDER BY created_at DESC', (session_id,))
        return [self._parse(row['payload_json'], PendingProjectNotice) for row in rows]

    def acknowledge_notice(self, notice_id: str) -> PendingProjectNotice | None:
        row = self._fetchone('SELECT payload_json FROM notices WHERE notice_id = ?', (notice_id,))
        if not row:
            return None
        notice = self._parse(row['payload_json'], PendingProjectNotice).model_copy(update={'acknowledged': True})
        self.upsert_notice(notice)
        return notice

    def upsert_artifact(self, record: ProjectArtifactRecord) -> ProjectArtifactRecord:
        self._upsert(
            'artifacts',
            ['artifact_id', 'project_id', 'unit_id', 'created_at', 'payload_json'],
            [record.artifact_id, record.project_id, record.unit_id, record.created_at, record.model_dump_json()],
            'artifact_id',
        )
        return record

    def list_artifacts(self, project_id: str) -> list[ProjectArtifactRecord]:
        rows = self._fetchall('SELECT payload_json FROM artifacts WHERE project_id = ? ORDER BY created_at ASC', (project_id,))
        return [self._parse(row['payload_json'], ProjectArtifactRecord) for row in rows]

    def delete_artifacts_for_units(self, unit_ids: list[str]) -> None:
        values = [str(item or '').strip() for item in unit_ids if str(item or '').strip()]
        if not values:
            return
        placeholders = ', '.join('?' for _ in values)
        with self._lock, self._conn:
            self._conn.execute(f'DELETE FROM artifacts WHERE unit_id IN ({placeholders})', tuple(values))

    def get_artifact(self, artifact_id: str) -> ProjectArtifactRecord | None:
        row = self._fetchone('SELECT payload_json FROM artifacts WHERE artifact_id = ?', (artifact_id,))
        return self._parse(row['payload_json'], ProjectArtifactRecord) if row else None

    def reset_project_runtime(self, project_id: str, *, keep_unit_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM units WHERE project_id = ? AND unit_id != ?', (project_id, keep_unit_id))
            self._conn.execute('DELETE FROM stages WHERE project_id = ?', (project_id,))

    def delete_project(self, project_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM notices WHERE project_id = ?', (project_id,))
            self._conn.execute('DELETE FROM artifacts WHERE project_id = ?', (project_id,))
            self._conn.execute('DELETE FROM stages WHERE project_id = ?', (project_id,))
            self._conn.execute('DELETE FROM units WHERE project_id = ?', (project_id,))
            self._conn.execute('DELETE FROM projects WHERE project_id = ?', (project_id,))

    def recount_project_units(self, project_id: str) -> None:
        project = self.get_project(project_id)
        if project is None:
            return
        units = self.list_units(project_id)
        active = sum(1 for unit in units if unit.status in {'pending', 'planning', 'ready', 'running', 'checking', 'blocked'})
        completed = sum(1 for unit in units if unit.status == 'completed')
        failed = sum(1 for unit in units if unit.status in {'failed', 'canceled'})
        self.upsert_project(project.model_copy(update={
            'active_unit_count': active,
            'completed_unit_count': completed,
            'failed_unit_count': failed,
        }))

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

