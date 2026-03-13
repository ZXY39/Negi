from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from main.governance.models import RolePolicyMatrixRecord, SkillResourceRecord, ToolFamilyRecord
from main.governance.roles import to_public_actor_role, to_public_allowed_roles
from main.protocol import now_iso

T = TypeVar('T', bound=BaseModel)


class GovernanceStore:
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
            CREATE TABLE IF NOT EXISTS skill_resources (
                skill_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS tool_families (
                tool_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS role_policy_matrix (
                policy_id TEXT PRIMARY KEY,
                actor_role TEXT NOT NULL,
                resource_kind TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                action_id TEXT,
                effect TEXT NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS governance_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            ''',
        ]
        with self._lock, self._conn:
            for statement in statements:
                self._conn.execute(statement)

    def replace_skill_resources(self, records: list[SkillResourceRecord], *, updated_at: str | None = None) -> None:
        stamp = updated_at or now_iso()
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM skill_resources')
            for record in records:
                self._conn.execute(
                    'INSERT INTO skill_resources (skill_id, enabled, updated_at, payload_json) VALUES (?, ?, ?, ?)',
                    (record.skill_id, 1 if record.enabled else 0, stamp, record.model_dump_json()),
                )

    def upsert_skill_resource(self, record: SkillResourceRecord, *, updated_at: str) -> SkillResourceRecord:
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO skill_resources (skill_id, enabled, updated_at, payload_json) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(skill_id) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at, payload_json=excluded.payload_json',
                (record.skill_id, 1 if record.enabled else 0, updated_at, record.model_dump_json()),
            )
        return record

    def list_skill_resources(self) -> list[SkillResourceRecord]:
        rows = self._fetchall('SELECT payload_json FROM skill_resources ORDER BY skill_id ASC')
        return [self._parse(row['payload_json'], SkillResourceRecord) for row in rows]

    def get_skill_resource(self, skill_id: str) -> SkillResourceRecord | None:
        row = self._fetchone('SELECT payload_json FROM skill_resources WHERE skill_id = ?', (skill_id,))
        return self._parse(row['payload_json'], SkillResourceRecord) if row else None

    def replace_tool_families(self, records: list[ToolFamilyRecord], *, updated_at: str) -> None:
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM tool_families')
            for record in records:
                self._conn.execute(
                    'INSERT INTO tool_families (tool_id, enabled, updated_at, payload_json) VALUES (?, ?, ?, ?)',
                    (record.tool_id, 1 if record.enabled else 0, updated_at, record.model_dump_json()),
                )

    def upsert_tool_family(self, record: ToolFamilyRecord, *, updated_at: str) -> ToolFamilyRecord:
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO tool_families (tool_id, enabled, updated_at, payload_json) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(tool_id) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at, payload_json=excluded.payload_json',
                (record.tool_id, 1 if record.enabled else 0, updated_at, record.model_dump_json()),
            )
        return record

    def list_tool_families(self) -> list[ToolFamilyRecord]:
        rows = self._fetchall('SELECT payload_json FROM tool_families ORDER BY tool_id ASC')
        return [self._parse(row['payload_json'], ToolFamilyRecord) for row in rows]

    def get_tool_family(self, tool_id: str) -> ToolFamilyRecord | None:
        row = self._fetchone('SELECT payload_json FROM tool_families WHERE tool_id = ?', (tool_id,))
        return self._parse(row['payload_json'], ToolFamilyRecord) if row else None

    def replace_default_role_policies(self, records: list[RolePolicyMatrixRecord]) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM role_policy_matrix WHERE source = 'default'")
            for record in records:
                self._conn.execute(
                    'INSERT INTO role_policy_matrix (policy_id, actor_role, resource_kind, resource_id, action_id, effect, source, updated_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (record.policy_id, record.actor_role, record.resource_kind, record.resource_id, record.action_id, record.effect, record.source, record.updated_at, record.model_dump_json()),
                )

    def list_role_policies(self) -> list[RolePolicyMatrixRecord]:
        rows = self._fetchall('SELECT payload_json FROM role_policy_matrix ORDER BY actor_role, resource_kind, resource_id, action_id ASC')
        return [self._parse(row['payload_json'], RolePolicyMatrixRecord) for row in rows]

    def get_meta(self, key: str) -> str | None:
        row = self._fetchone('SELECT value FROM governance_meta WHERE key = ?', (str(key or ''),))
        return str(row['value']) if row else None

    def set_meta(self, key: str, value: str) -> None:
        stamp = now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO governance_meta (key, value, updated_at) VALUES (?, ?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
                (str(key or ''), str(value or ''), stamp),
            )

    def _fetchone(self, sql: str, params: tuple[object, ...]) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    @staticmethod
    def _parse(payload_json: str, model_cls: type[T]) -> T:
        payload = json.loads(payload_json)
        if model_cls is SkillResourceRecord and isinstance(payload, dict):
            payload['allowed_roles'] = to_public_allowed_roles(payload.get('allowed_roles') or [])
        elif model_cls is ToolFamilyRecord and isinstance(payload, dict):
            for action in payload.get('actions') or []:
                if isinstance(action, dict):
                    action['allowed_roles'] = to_public_allowed_roles(action.get('allowed_roles') or [])
        elif model_cls is RolePolicyMatrixRecord and isinstance(payload, dict):
            payload['actor_role'] = to_public_actor_role(payload.get('actor_role'))
        return model_cls.model_validate(payload)
