from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from main.governance.models import RolePolicyMatrixRecord, SkillResourceRecord, ToolFamilyRecord
from main.governance.roles import (
    normalize_public_allowed_roles,
    to_public_actor_role,
    to_public_allowed_roles,
)
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
            '''
            CREATE TABLE IF NOT EXISTS exec_command_approvals (
                approval_id TEXT PRIMARY KEY,
                command_norm TEXT NOT NULL,
                command_text TEXT NOT NULL,
                cwd TEXT NOT NULL DEFAULT '',
                guard_reason TEXT NOT NULL DEFAULT '',
                actor_role TEXT NOT NULL DEFAULT '',
                lane TEXT NOT NULL DEFAULT '',
                context_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                decision_scope TEXT NOT NULL DEFAULT '',
                decided_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                decided_at TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT ''
            )
            ''',
            'CREATE INDEX IF NOT EXISTS idx_exec_approvals_status ON exec_command_approvals(status, created_at)',
            'CREATE INDEX IF NOT EXISTS idx_exec_approvals_command ON exec_command_approvals(command_norm, status)',
            'CREATE INDEX IF NOT EXISTS idx_exec_approvals_context ON exec_command_approvals(context_id)',
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

    def delete_role_policies_for_resource(self, *, resource_kind: str, resource_id: str) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                'DELETE FROM role_policy_matrix WHERE resource_kind = ? AND resource_id = ?',
                (str(resource_kind or ''), str(resource_id or '')),
            )
            return int(getattr(cursor, 'rowcount', 0) or 0)

    def list_role_policies(self) -> list[RolePolicyMatrixRecord]:
        rows = self._fetchall('SELECT payload_json FROM role_policy_matrix ORDER BY actor_role, resource_kind, resource_id, action_id ASC')
        return [self._parse(row['payload_json'], RolePolicyMatrixRecord) for row in rows]

    def get_meta(self, key: str) -> str | None:
        row = self._fetchone('SELECT value FROM governance_meta WHERE key = ?', (str(key or ''),))
        return str(row['value']) if row else None

    def get_bool_meta(self, key: str, *, default: bool = False) -> bool:
        raw = self.get_meta(key)
        if raw is None:
            return bool(default)
        return str(raw).strip().lower() in {'1', 'true', 'yes', 'on'}

    def set_meta(self, key: str, value: str) -> None:
        stamp = now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO governance_meta (key, value, updated_at) VALUES (?, ?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
                (str(key or ''), str(value or ''), stamp),
            )

    def set_bool_meta(self, key: str, value: bool) -> None:
        self.set_meta(key, 'true' if bool(value) else 'false')

    # -- exec command approvals ------------------------------------------------

    def create_exec_approval(self, record: dict[str, object]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO exec_command_approvals (approval_id, command_norm, command_text, cwd, guard_reason, '
                'actor_role, lane, context_id, status, decision_scope, decided_by, created_at, decided_at, expires_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    str(record.get('approval_id') or ''),
                    str(record.get('command_norm') or ''),
                    str(record.get('command_text') or ''),
                    str(record.get('cwd') or ''),
                    str(record.get('guard_reason') or ''),
                    str(record.get('actor_role') or ''),
                    str(record.get('lane') or ''),
                    str(record.get('context_id') or ''),
                    str(record.get('status') or 'pending'),
                    str(record.get('decision_scope') or ''),
                    str(record.get('decided_by') or ''),
                    str(record.get('created_at') or now_iso()),
                    str(record.get('decided_at') or ''),
                    str(record.get('expires_at') or ''),
                ),
            )

    def get_exec_approval(self, approval_id: str) -> dict[str, object] | None:
        row = self._fetchone(
            'SELECT * FROM exec_command_approvals WHERE approval_id = ?', (str(approval_id or ''),)
        )
        return dict(row) if row else None

    def list_exec_approvals(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, object]]:
        sql = 'SELECT * FROM exec_command_approvals'
        params: list[object] = []
        normalized_status = str(status or '').strip()
        if normalized_status:
            sql += ' WHERE status = ?'
            params.append(normalized_status)
        sql += ' ORDER BY created_at DESC LIMIT ?'
        params.append(max(1, int(limit or 50)))
        return [dict(row) for row in self._fetchall(sql, tuple(params))]

    def decide_exec_approval(
        self,
        approval_id: str,
        *,
        status: str,
        decided_by: str = '',
        decision_scope: str = '',
    ) -> bool:
        """Decide a pending approval exactly once; returns False if it was
        already decided/expired (UPDATE guarded by status='pending')."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE exec_command_approvals SET status = ?, decided_by = ?, decision_scope = ?, decided_at = ? "
                "WHERE approval_id = ? AND status = 'pending'",
                (str(status), str(decided_by or ''), str(decision_scope or ''), now_iso(), str(approval_id or '')),
            )
            return int(getattr(cursor, 'rowcount', 0) or 0) == 1

    def expire_stale_exec_approvals(self, cutoff_iso: str) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE exec_command_approvals SET status = 'expired', decided_by = 'system', decided_at = ? "
                "WHERE status = 'pending' AND created_at < ?",
                (now_iso(), str(cutoff_iso or '')),
            )
            return int(getattr(cursor, 'rowcount', 0) or 0)

    def count_recent_exec_approval_outcomes(
        self, command_norm: str, *, since_iso: str, statuses: tuple[str, ...]
    ) -> int:
        placeholders = ', '.join('?' for _ in statuses)
        row = self._fetchone(
            f'SELECT COUNT(*) AS n FROM exec_command_approvals '
            f'WHERE command_norm = ? AND status IN ({placeholders}) AND created_at >= ?',
            (str(command_norm or ''), *statuses, str(since_iso or '')),
        )
        return int((row['n'] if row else 0) or 0)

    def prune_exec_approvals(self, *, keep_recent: int = 200) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                'DELETE FROM exec_command_approvals WHERE approval_id NOT IN '
                '(SELECT approval_id FROM exec_command_approvals ORDER BY created_at DESC LIMIT ?)',
                (max(10, int(keep_recent or 200)),),
            )
            return int(getattr(cursor, 'rowcount', 0) or 0)

    def delete_exec_approvals_for_context(self, context_id: str) -> int:
        """任务删除全量清除：按 context_id（=task_id）删审批行（含命令明文）。

        task_id 带 `task:` 前缀，与 session_key（`web:…` 等）命名空间不冲突，
        精确等值匹配不会误删会话通道的审批记录。
        """
        normalized = str(context_id or '').strip()
        if not normalized:
            return 0
        with self._lock, self._conn:
            cursor = self._conn.execute(
                'DELETE FROM exec_command_approvals WHERE context_id = ?',
                (normalized,),
            )
            return int(getattr(cursor, 'rowcount', 0) or 0)

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
                    action['allowed_roles'] = normalize_public_allowed_roles(action.get('allowed_roles') or [])
        elif model_cls is RolePolicyMatrixRecord and isinstance(payload, dict):
            payload['actor_role'] = to_public_actor_role(payload.get('actor_role'))
        return model_cls.model_validate(payload)
