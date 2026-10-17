from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import TypeVar

from loguru import logger
from pydantic import BaseModel

from main.governance.models import RolePolicyMatrixRecord, SkillResourceRecord, ToolRecord
from main.governance.roles import (
    split_public_allowed_roles,
    to_public_actor_role,
)
from main.protocol import now_iso

T = TypeVar('T', bound=BaseModel)


def _report_dropped_roles(payload: dict[str, object], dropped: list[str]) -> None:
    """读侧丢掉的角色必须留下可数的落点：点名是哪一行、丢了哪些值。"""
    if not dropped:
        return
    row_id = str(payload.get('skill_id') or payload.get('tool_id') or payload.get('resource_id') or '?')
    logger.warning(
        'governance: row {} carries unsupported allowed_roles {} and is read without them',
        row_id,
        dropped,
    )


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
            CREATE TABLE IF NOT EXISTS tools (
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
            'CREATE INDEX IF NOT EXISTS idx_role_policy_lookup ON role_policy_matrix(actor_role, resource_kind, resource_id)',
        ]
        with self._lock, self._conn:
            for statement in statements:
                self._conn.execute(statement)
            self._migrate_legacy_tool_families()

    def _migrate_legacy_tool_families(self) -> None:
        """一次性把族粒度记录展开成工具粒度，然后删掉旧表。

        角色的权威一直在 payload_json 里，role_policy_matrix 每次启动都按重建的注册表重生成
        （实测存量非 default 行为 0），所以这里只搬 payload，旧 matrix 行直接删。
        """
        tables = {
            str(row['name'])
            for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if 'tool_families' not in tables:
            return
        stamp = now_iso()
        migrated = 0
        for row in self._conn.execute('SELECT payload_json FROM tool_families').fetchall():
            try:
                legacy = json.loads(str(row['payload_json'] or '{}'))
            except Exception:
                continue
            for payload in self._expand_legacy_family(legacy):
                try:
                    record = ToolRecord(**payload)
                except Exception:
                    continue
                self._conn.execute(
                    'INSERT OR REPLACE INTO tools (tool_id, enabled, updated_at, payload_json) VALUES (?, ?, ?, ?)',
                    (record.tool_id, 1 if record.enabled else 0, stamp, record.model_dump_json()),
                )
                migrated += 1
        self._conn.execute("DELETE FROM role_policy_matrix WHERE resource_kind IN ('tool_family', 'tool')")
        self._conn.execute('DROP TABLE tool_families')
        self._conn.execute(
            "INSERT INTO governance_meta (key, value, updated_at) VALUES (?, ?, ?) "
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
            ('tool_granularity_migration_v1_applied', str(migrated), stamp),
        )

    @staticmethod
    def _expand_legacy_family(legacy: dict) -> list[dict]:
        if not isinstance(legacy, dict):
            return []
        family_tool_id = str(legacy.get('tool_id') or '').strip()
        if not family_tool_id or 'actions' not in legacy:
            return []
        base = {
            'display_name': str(legacy.get('display_name') or family_tool_id),
            'description': str(legacy.get('description') or ''),
            'is_core': bool(legacy.get('is_core', False)),
            'enabled': bool(legacy.get('enabled', True)),
            'available': bool(legacy.get('available', True)),
            'tool_type': str(legacy.get('tool_type') or 'internal'),
            'install_dir': legacy.get('install_dir'),
            'callable': bool(legacy.get('callable', True)),
            'source_path': str(legacy.get('source_path') or ''),
            'metadata': {
                key: value
                for key, value in dict(legacy.get('metadata') or {}).items()
                if key != 'sources'
            },
        }
        items: list[dict] = []
        for action in list(legacy.get('actions') or []):
            if not isinstance(action, dict):
                continue
            executor_names = [
                str(name or '').strip()
                for name in list(action.get('executor_names') or [])
                if str(name or '').strip()
            ] or [family_tool_id]
            for executor_name in executor_names:
                payload = dict(base)
                payload.update(
                    {
                        'tool_id': executor_name,
                        'display_name': str(action.get('label') or legacy.get('display_name') or executor_name),
                        'risk_level': str(action.get('risk_level') or 'medium'),
                        'destructive': bool(action.get('destructive', False)),
                        'agent_visible': bool(action.get('agent_visible', True)),
                        'admin_mode': str(action.get('admin_mode') or 'editable'),
                        'allowed_roles': list(action.get('allowed_roles') or []),
                    }
                )
                items.append(payload)
        return items

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

    def replace_tools(self, records: list[ToolRecord], *, updated_at: str) -> None:
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM tools')
            for record in records:
                self._conn.execute(
                    'INSERT INTO tools (tool_id, enabled, updated_at, payload_json) VALUES (?, ?, ?, ?)',
                    (record.tool_id, 1 if record.enabled else 0, updated_at, record.model_dump_json()),
                )

    def upsert_tool(self, record: ToolRecord, *, updated_at: str) -> ToolRecord:
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO tools (tool_id, enabled, updated_at, payload_json) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(tool_id) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at, payload_json=excluded.payload_json',
                (record.tool_id, 1 if record.enabled else 0, updated_at, record.model_dump_json()),
            )
        return record

    def list_tools(self) -> list[ToolRecord]:
        rows = self._fetchall('SELECT payload_json FROM tools ORDER BY tool_id ASC')
        return [self._parse(row['payload_json'], ToolRecord) for row in rows]

    def get_tool(self, tool_id: str) -> ToolRecord | None:
        row = self._fetchone('SELECT payload_json FROM tools WHERE tool_id = ?', (tool_id,))
        return self._parse(row['payload_json'], ToolRecord) if row else None

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

    def find_role_policies(self, *, actor_role: str, resource_kind: str, resource_id: str, action_id: str | None = None) -> list[RolePolicyMatrixRecord]:
        """鉴权用的窄查询：只取这个主体对这个资源可能命中的行。

        列里的 actor_role 就是公开形态（与 payload 一致），所以按列过滤等价于
        解析后再过滤。排序 action_id DESC 让精确动作排在通配（NULL 动作）之前，
        工具记录没有动作层，存的就是 NULL。
        """
        rows = self._fetchall(
            'SELECT payload_json FROM role_policy_matrix '
            'WHERE actor_role = ? AND resource_kind = ? AND resource_id = ? '
            'AND (action_id IS NULL OR action_id = ?) '
            'ORDER BY action_id DESC',
            (str(actor_role or ''), str(resource_kind or ''), str(resource_id or ''), str(action_id or '')),
        )
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
            kept, dropped = split_public_allowed_roles(payload.get('allowed_roles') or [])
            _report_dropped_roles(payload, dropped)
            # 技能面这条空集回落是现行契约（工具面保留空集，见 tool-and-skill-system.md）
            payload['allowed_roles'] = kept or ['ceo', 'execution']
        elif model_cls is ToolRecord and isinstance(payload, dict):
            kept, dropped = split_public_allowed_roles(list(payload.get('allowed_roles') or []))
            _report_dropped_roles(payload, dropped)
            payload['allowed_roles'] = kept
        elif model_cls is RolePolicyMatrixRecord and isinstance(payload, dict):
            payload['actor_role'] = to_public_actor_role(payload.get('actor_role'))
        return model_cls.model_validate(payload)
