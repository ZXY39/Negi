from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from main.models import NodeRecord, TaskArtifactRecord, TaskRecord
from main.monitoring.models import (
    TaskProjectionMetaRecord,
    TaskProjectionNodeDetailRecord,
    TaskProjectionNodeRecord,
    TaskProjectionRoundRecord,
    TaskProjectionRuntimeFrameRecord,
)

T = TypeVar('T', bound=BaseModel)


class SQLiteTaskStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._read_lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute('PRAGMA journal_mode=WAL')
        self._setup()
        self._read_conn = self._open_read_conn()

    def close(self) -> None:
        with self._read_lock:
            self._read_conn.close()
        with self._lock:
            self._conn.close()

    def _open_read_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        with conn:
            conn.execute('PRAGMA query_only=ON')
        return conn

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
            '''
            CREATE TABLE IF NOT EXISTS task_runtime_meta (
                task_id TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS task_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS task_commands (
                command_id TEXT PRIMARY KEY,
                task_id TEXT,
                session_id TEXT NOT NULL,
                command_type TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                finished_at TEXT,
                worker_id TEXT,
                error_text TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS worker_status (
                worker_id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS task_projection_meta (
                task_id TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS task_nodes (
                node_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                parent_node_id TEXT,
                root_node_id TEXT NOT NULL,
                depth INTEGER NOT NULL,
                node_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                default_round_id TEXT NOT NULL,
                selected_round_id TEXT NOT NULL,
                round_options_count INTEGER NOT NULL,
                sort_key TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS task_node_details (
                node_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                input_text TEXT NOT NULL,
                input_ref TEXT NOT NULL,
                output_text TEXT NOT NULL,
                output_ref TEXT NOT NULL,
                check_result TEXT NOT NULL,
                check_result_ref TEXT NOT NULL,
                final_output TEXT NOT NULL,
                final_output_ref TEXT NOT NULL,
                failure_reason TEXT NOT NULL,
                prompt_summary TEXT NOT NULL,
                execution_trace_ref TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS task_runtime_frames (
                task_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                depth INTEGER NOT NULL,
                node_kind TEXT NOT NULL,
                phase TEXT NOT NULL,
                active INTEGER NOT NULL,
                runnable INTEGER NOT NULL,
                waiting INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (task_id, node_id)
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS task_node_rounds (
                task_id TEXT NOT NULL,
                parent_node_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                round_index INTEGER NOT NULL,
                label TEXT NOT NULL,
                is_latest INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                total_children INTEGER NOT NULL,
                completed_children INTEGER NOT NULL,
                running_children INTEGER NOT NULL,
                failed_children INTEGER NOT NULL,
                child_node_ids_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (task_id, parent_node_id, round_id)
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS task_model_calls (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS task_terminal_outbox (
                dedupe_key TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                delivery_state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                last_attempt_at TEXT NOT NULL,
                last_error TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS task_stall_outbox (
                dedupe_key TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                delivery_state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                last_attempt_at TEXT NOT NULL,
                last_error TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS task_worker_status_outbox (
                worker_id TEXT PRIMARY KEY,
                delivery_state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                last_attempt_at TEXT NOT NULL,
                last_error TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            ''',
            'CREATE INDEX IF NOT EXISTS idx_nodes_task_id ON nodes(task_id)',
            'CREATE INDEX IF NOT EXISTS idx_nodes_parent_node_id ON nodes(parent_node_id)',
            'CREATE INDEX IF NOT EXISTS idx_artifacts_task_id ON artifacts(task_id)',
            'CREATE INDEX IF NOT EXISTS idx_artifacts_node_id ON artifacts(node_id)',
            'CREATE INDEX IF NOT EXISTS idx_task_events_task_id_seq ON task_events(task_id, seq)',
            'CREATE INDEX IF NOT EXISTS idx_task_events_session_id_seq ON task_events(session_id, seq)',
            'CREATE INDEX IF NOT EXISTS idx_task_commands_status_created_at ON task_commands(status, created_at)',
            'CREATE INDEX IF NOT EXISTS idx_task_commands_task_id ON task_commands(task_id)',
            'CREATE INDEX IF NOT EXISTS idx_task_nodes_task_id_sort_key ON task_nodes(task_id, sort_key)',
            'CREATE INDEX IF NOT EXISTS idx_task_nodes_parent_node_id ON task_nodes(parent_node_id)',
            'CREATE INDEX IF NOT EXISTS idx_task_node_details_task_id ON task_node_details(task_id)',
            'CREATE INDEX IF NOT EXISTS idx_task_runtime_frames_task_id ON task_runtime_frames(task_id)',
            'CREATE INDEX IF NOT EXISTS idx_task_runtime_meta_updated_at ON task_runtime_meta(updated_at)',
            'CREATE INDEX IF NOT EXISTS idx_task_node_rounds_task_parent ON task_node_rounds(task_id, parent_node_id, round_index)',
            'CREATE INDEX IF NOT EXISTS idx_task_model_calls_task_id_seq ON task_model_calls(task_id, seq)',
            'CREATE INDEX IF NOT EXISTS idx_task_terminal_outbox_state_created_at ON task_terminal_outbox(delivery_state, created_at)',
            'CREATE INDEX IF NOT EXISTS idx_task_stall_outbox_state_created_at ON task_stall_outbox(delivery_state, created_at)',
            'CREATE INDEX IF NOT EXISTS idx_task_worker_status_outbox_state_updated_at ON task_worker_status_outbox(delivery_state, updated_at)',
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

    def delete_nodes(self, node_ids: list[str]) -> None:
        normalized_ids = [str(node_id or '').strip() for node_id in list(node_ids or []) if str(node_id or '').strip()]
        if not normalized_ids:
            return
        placeholders = ', '.join('?' for _ in normalized_ids)
        with self._lock, self._conn:
            self._conn.execute(f'DELETE FROM nodes WHERE node_id IN ({placeholders})', tuple(normalized_ids))

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
            self._conn.execute('DELETE FROM task_projection_meta WHERE task_id = ?', (task_id,))
            self._conn.execute('DELETE FROM task_node_rounds WHERE task_id = ?', (task_id,))
            self._conn.execute('DELETE FROM task_runtime_frames WHERE task_id = ?', (task_id,))
            self._conn.execute('DELETE FROM task_node_details WHERE task_id = ?', (task_id,))
            self._conn.execute('DELETE FROM task_nodes WHERE task_id = ?', (task_id,))
            self._conn.execute('DELETE FROM task_model_calls WHERE task_id = ?', (task_id,))
            self._conn.execute('DELETE FROM task_runtime_meta WHERE task_id = ?', (task_id,))
            self._conn.execute('DELETE FROM task_commands WHERE task_id = ?', (task_id,))
            self._conn.execute('DELETE FROM task_terminal_outbox WHERE task_id = ?', (task_id,))
            self._conn.execute('DELETE FROM task_stall_outbox WHERE task_id = ?', (task_id,))
            self._conn.execute('DELETE FROM artifacts WHERE task_id = ?', (task_id,))
            self._conn.execute('DELETE FROM nodes WHERE task_id = ?', (task_id,))
            self._conn.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))

    def upsert_task_runtime_meta(self, *, task_id: str, updated_at: str, payload: dict[str, object]) -> None:
        payload_json = json.dumps(payload)
        self._upsert(
            'task_runtime_meta',
            ['task_id', 'updated_at', 'payload_json'],
            [task_id, updated_at, payload_json],
            'task_id',
        )

    def get_task_runtime_meta(self, task_id: str) -> dict[str, object] | None:
        row = self._fetchone('SELECT payload_json FROM task_runtime_meta WHERE task_id = ?', (task_id,))
        if row is None:
            return None
        payload = json.loads(row['payload_json'])
        return payload if isinstance(payload, dict) else None

    def append_task_event(
        self,
        *,
        task_id: str | None,
        session_id: str,
        event_type: str,
        created_at: str,
        payload: dict[str, object],
    ) -> int:
        payload_json = json.dumps(payload)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                'INSERT INTO task_events (task_id, session_id, event_type, created_at, payload_json) VALUES (?, ?, ?, ?, ?)',
                (task_id, session_id, event_type, created_at, payload_json),
            )
            return int(cursor.lastrowid or 0)

    def list_task_events(
        self,
        *,
        after_seq: int = 0,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        predicates = ['seq > ?']
        params: list[object] = [max(0, int(after_seq or 0))]
        if task_id is not None:
            predicates.append('task_id = ?')
            params.append(str(task_id or ''))
        if session_id is not None:
            predicates.append('session_id = ?')
            params.append(str(session_id or ''))
        params.append(max(1, int(limit or 200)))
        sql = (
            'SELECT seq, task_id, session_id, event_type, created_at, payload_json '
            f'FROM task_events WHERE {" AND ".join(predicates)} ORDER BY seq ASC LIMIT ?'
        )
        rows = self._fetchall(sql, tuple(params))
        events: list[dict[str, object]] = []
        for row in rows:
            payload = json.loads(row['payload_json'])
            events.append(
                {
                    'seq': int(row['seq']),
                    'task_id': row['task_id'],
                    'session_id': row['session_id'],
                    'event_type': row['event_type'],
                    'created_at': row['created_at'],
                    'payload': payload if isinstance(payload, dict) else {},
                }
            )
        return events

    def latest_task_event_seq(
        self,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> int:
        predicates = ['1 = 1']
        params: list[object] = []
        if task_id is not None:
            predicates.append('task_id = ?')
            params.append(str(task_id or ''))
        if session_id is not None:
            predicates.append('session_id = ?')
            params.append(str(session_id or ''))
        row = self._fetchone(
            f'SELECT COALESCE(MAX(seq), 0) AS seq FROM task_events WHERE {" AND ".join(predicates)}',
            tuple(params),
        )
        return int((row['seq'] if row is not None else 0) or 0)

    def enqueue_task_command(
        self,
        *,
        command_id: str,
        task_id: str | None,
        session_id: str,
        command_type: str,
        created_at: str,
        payload: dict[str, object],
    ) -> None:
        payload_json = json.dumps(payload)
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO task_commands (command_id, task_id, session_id, command_type, status, created_at, payload_json) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (command_id, task_id, session_id, command_type, 'pending', created_at, payload_json),
            )

    def claim_pending_task_commands(
        self,
        *,
        worker_id: str,
        claimed_at: str,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        with self._lock, self._conn:
            rows = list(
                self._conn.execute(
                    'SELECT command_id, task_id, session_id, command_type, created_at, payload_json '
                    'FROM task_commands WHERE status = ? ORDER BY created_at ASC LIMIT ?',
                    ('pending', max(1, int(limit or 20))),
                ).fetchall()
            )
            if not rows:
                return []
            command_ids = [str(row['command_id']) for row in rows]
            for command_id in command_ids:
                self._conn.execute(
                    'UPDATE task_commands SET status = ?, worker_id = ?, claimed_at = ? WHERE command_id = ?',
                    ('claimed', worker_id, claimed_at, command_id),
                )
        commands: list[dict[str, object]] = []
        for row in rows:
            payload = json.loads(row['payload_json'])
            commands.append(
                {
                    'command_id': row['command_id'],
                    'task_id': row['task_id'],
                    'session_id': row['session_id'],
                    'command_type': row['command_type'],
                    'created_at': row['created_at'],
                    'payload': payload if isinstance(payload, dict) else {},
                }
            )
        return commands

    def finish_task_command(
        self,
        command_id: str,
        *,
        finished_at: str,
        success: bool,
        error_text: str = '',
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                'UPDATE task_commands SET status = ?, finished_at = ?, error_text = ? WHERE command_id = ?',
                ('completed' if success else 'failed', finished_at, str(error_text or ''), command_id),
            )

    def put_task_terminal_outbox(
        self,
        *,
        dedupe_key: str,
        task_id: str,
        session_id: str,
        created_at: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        key = str(dedupe_key or '').strip()
        if not key:
            raise ValueError('dedupe_key_required')
        with self._lock, self._conn:
            row = self._conn.execute(
                'SELECT dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
                'FROM task_terminal_outbox WHERE dedupe_key = ?',
                (key,),
            ).fetchone()
            if row is None:
                payload_json = json.dumps(payload, ensure_ascii=False)
                self._conn.execute(
                    'INSERT INTO task_terminal_outbox (dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (key, task_id, session_id, 'pending', created_at, created_at, '', 0, '', '', payload_json),
                )
                row = self._conn.execute(
                    'SELECT dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
                    'FROM task_terminal_outbox WHERE dedupe_key = ?',
                    (key,),
                ).fetchone()
        return self._task_terminal_outbox_row(row)

    def get_task_terminal_outbox(self, dedupe_key: str) -> dict[str, object] | None:
        row = self._fetchone(
            'SELECT dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
            'FROM task_terminal_outbox WHERE dedupe_key = ?',
            (str(dedupe_key or '').strip(),),
        )
        return self._task_terminal_outbox_row(row) if row else None

    def list_pending_task_terminal_outbox(self, *, limit: int = 200) -> list[dict[str, object]]:
        rows = self._fetchall(
            'SELECT dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
            'FROM task_terminal_outbox WHERE delivery_state != ? ORDER BY created_at ASC LIMIT ?',
            ('delivered', max(1, int(limit or 200))),
        )
        return [self._task_terminal_outbox_row(row) for row in rows]

    def mark_task_terminal_outbox_attempt(self, dedupe_key: str, *, attempted_at: str, error_text: str) -> None:
        key = str(dedupe_key or '').strip()
        if not key:
            return
        with self._lock, self._conn:
            row = self._conn.execute('SELECT attempts, delivery_state FROM task_terminal_outbox WHERE dedupe_key = ?', (key,)).fetchone()
            if row is None:
                return
            delivery_state = str(row['delivery_state'] or '').strip() or 'pending'
            if delivery_state == 'delivered':
                return
            attempts = int(row['attempts'] or 0) + 1
            self._conn.execute(
                'UPDATE task_terminal_outbox SET delivery_state = ?, updated_at = ?, attempts = ?, last_attempt_at = ?, last_error = ? WHERE dedupe_key = ?',
                ('pending', attempted_at, attempts, attempted_at, str(error_text or ''), key),
            )

    def mark_task_terminal_outbox_delivered(self, dedupe_key: str, *, delivered_at: str) -> None:
        key = str(dedupe_key or '').strip()
        if not key:
            return
        with self._lock, self._conn:
            self._conn.execute(
                'UPDATE task_terminal_outbox SET delivery_state = ?, updated_at = ?, delivered_at = ?, last_error = ? WHERE dedupe_key = ?',
                ('delivered', delivered_at, delivered_at, '', key),
            )

    def put_task_stall_outbox(
        self,
        *,
        dedupe_key: str,
        task_id: str,
        session_id: str,
        created_at: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        key = str(dedupe_key or '').strip()
        if not key:
            raise ValueError('dedupe_key_required')
        with self._lock, self._conn:
            row = self._conn.execute(
                'SELECT dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
                'FROM task_stall_outbox WHERE dedupe_key = ?',
                (key,),
            ).fetchone()
            if row is None:
                payload_json = json.dumps(payload, ensure_ascii=False)
                self._conn.execute(
                    'INSERT INTO task_stall_outbox (dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (key, task_id, session_id, 'pending', created_at, created_at, '', 0, '', '', payload_json),
                )
                row = self._conn.execute(
                    'SELECT dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
                    'FROM task_stall_outbox WHERE dedupe_key = ?',
                    (key,),
                ).fetchone()
        return self._task_stall_outbox_row(row)

    def get_task_stall_outbox(self, dedupe_key: str) -> dict[str, object] | None:
        row = self._fetchone(
            'SELECT dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
            'FROM task_stall_outbox WHERE dedupe_key = ?',
            (str(dedupe_key or '').strip(),),
        )
        return self._task_stall_outbox_row(row) if row else None

    def list_pending_task_stall_outbox(self, *, limit: int = 200) -> list[dict[str, object]]:
        rows = self._fetchall(
            'SELECT dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
            'FROM task_stall_outbox WHERE delivery_state != ? ORDER BY created_at ASC LIMIT ?',
            ('delivered', max(1, int(limit or 200))),
        )
        return [self._task_stall_outbox_row(row) for row in rows]

    def mark_task_stall_outbox_attempt(self, dedupe_key: str, *, attempted_at: str, error_text: str) -> None:
        key = str(dedupe_key or '').strip()
        if not key:
            return
        with self._lock, self._conn:
            row = self._conn.execute('SELECT attempts, delivery_state FROM task_stall_outbox WHERE dedupe_key = ?', (key,)).fetchone()
            if row is None:
                return
            delivery_state = str(row['delivery_state'] or '').strip() or 'pending'
            if delivery_state == 'delivered':
                return
            attempts = int(row['attempts'] or 0) + 1
            self._conn.execute(
                'UPDATE task_stall_outbox SET delivery_state = ?, updated_at = ?, attempts = ?, last_attempt_at = ?, last_error = ? WHERE dedupe_key = ?',
                ('pending', attempted_at, attempts, attempted_at, str(error_text or ''), key),
            )

    def mark_task_stall_outbox_delivered(self, dedupe_key: str, *, delivered_at: str) -> None:
        key = str(dedupe_key or '').strip()
        if not key:
            return
        with self._lock, self._conn:
            self._conn.execute(
                'UPDATE task_stall_outbox SET delivery_state = ?, updated_at = ?, delivered_at = ?, last_error = ? WHERE dedupe_key = ?',
                ('delivered', delivered_at, delivered_at, '', key),
            )

    def put_task_worker_status_outbox(
        self,
        *,
        worker_id: str,
        created_at: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        key = str(worker_id or '').strip()
        if not key:
            raise ValueError('worker_id_required')
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self._lock, self._conn:
            row = self._conn.execute(
                'SELECT worker_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
                'FROM task_worker_status_outbox WHERE worker_id = ?',
                (key,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    'INSERT INTO task_worker_status_outbox (worker_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (key, 'pending', created_at, created_at, '', 0, '', '', payload_json),
                )
            else:
                existing_created_at = str(row['created_at'] or created_at).strip() or created_at
                self._conn.execute(
                    'UPDATE task_worker_status_outbox SET delivery_state = ?, created_at = ?, updated_at = ?, delivered_at = ?, attempts = ?, last_attempt_at = ?, last_error = ?, payload_json = ? '
                    'WHERE worker_id = ?',
                    ('pending', existing_created_at, created_at, '', 0, '', '', payload_json, key),
                )
            row = self._conn.execute(
                'SELECT worker_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
                'FROM task_worker_status_outbox WHERE worker_id = ?',
                (key,),
            ).fetchone()
        return self._task_worker_status_outbox_row(row)

    def get_task_worker_status_outbox(self, worker_id: str) -> dict[str, object] | None:
        row = self._fetchone(
            'SELECT worker_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
            'FROM task_worker_status_outbox WHERE worker_id = ?',
            (str(worker_id or '').strip(),),
        )
        return self._task_worker_status_outbox_row(row) if row else None

    def list_pending_task_worker_status_outbox(self, *, limit: int = 200) -> list[dict[str, object]]:
        rows = self._fetchall(
            'SELECT worker_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
            'FROM task_worker_status_outbox WHERE delivery_state != ? ORDER BY updated_at ASC LIMIT ?',
            ('delivered', max(1, int(limit or 200))),
        )
        return [self._task_worker_status_outbox_row(row) for row in rows]

    def mark_task_worker_status_outbox_attempt(self, worker_id: str, *, attempted_at: str, error_text: str) -> None:
        key = str(worker_id or '').strip()
        if not key:
            return
        with self._lock, self._conn:
            row = self._conn.execute('SELECT attempts, delivery_state FROM task_worker_status_outbox WHERE worker_id = ?', (key,)).fetchone()
            if row is None:
                return
            delivery_state = str(row['delivery_state'] or '').strip() or 'pending'
            if delivery_state == 'delivered':
                return
            attempts = int(row['attempts'] or 0) + 1
            self._conn.execute(
                'UPDATE task_worker_status_outbox SET delivery_state = ?, attempts = ?, last_attempt_at = ?, last_error = ? WHERE worker_id = ?',
                ('pending', attempts, attempted_at, str(error_text or ''), key),
            )

    def mark_task_worker_status_outbox_delivered(self, worker_id: str, *, delivered_at: str) -> None:
        key = str(worker_id or '').strip()
        if not key:
            return
        with self._lock, self._conn:
            self._conn.execute(
                'UPDATE task_worker_status_outbox SET delivery_state = ?, delivered_at = ?, last_error = ? WHERE worker_id = ?',
                ('delivered', delivered_at, '', key),
            )

    def upsert_worker_status(
        self,
        *,
        worker_id: str,
        role: str,
        status: str,
        updated_at: str,
        payload: dict[str, object],
    ) -> None:
        self._upsert(
            'worker_status',
            ['worker_id', 'role', 'status', 'updated_at', 'payload_json'],
            [worker_id, role, status, updated_at, json.dumps(payload)],
            'worker_id',
        )

    def list_worker_status(self, *, role: str | None = None) -> list[dict[str, object]]:
        if role:
            rows = self._fetchall(
                'SELECT worker_id, role, status, updated_at, payload_json FROM worker_status WHERE role = ? ORDER BY updated_at DESC',
                (str(role or ''),),
            )
        else:
            rows = self._fetchall(
                'SELECT worker_id, role, status, updated_at, payload_json FROM worker_status ORDER BY updated_at DESC'
            )
        items: list[dict[str, object]] = []
        for row in rows:
            payload = json.loads(row['payload_json'])
            items.append(
                {
                    'worker_id': row['worker_id'],
                    'role': row['role'],
                    'status': row['status'],
                    'updated_at': row['updated_at'],
                    'payload': payload if isinstance(payload, dict) else {},
                }
            )
        return items

    def upsert_task_projection_meta(self, record: TaskProjectionMetaRecord) -> TaskProjectionMetaRecord:
        self._upsert(
            'task_projection_meta',
            ['task_id', 'version', 'updated_at', 'payload_json'],
            [record.task_id, int(record.version or 1), record.updated_at, record.model_dump_json()],
            'task_id',
        )
        return record

    def get_task_projection_meta(self, task_id: str) -> TaskProjectionMetaRecord | None:
        row = self._fetchone('SELECT payload_json FROM task_projection_meta WHERE task_id = ?', (task_id,))
        return self._parse(row['payload_json'], TaskProjectionMetaRecord) if row else None

    def replace_task_nodes(self, task_id: str, records: list[TaskProjectionNodeRecord]) -> None:
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM task_nodes WHERE task_id = ?', (task_id,))
            for record in records:
                self._conn.execute(
                    'INSERT INTO task_nodes (node_id, task_id, parent_node_id, root_node_id, depth, node_kind, status, title, updated_at, default_round_id, selected_round_id, round_options_count, sort_key, payload_json) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        record.node_id,
                        record.task_id,
                        record.parent_node_id,
                        record.root_node_id,
                        int(record.depth or 0),
                        record.node_kind,
                        record.status,
                        record.title,
                        record.updated_at,
                        record.default_round_id,
                        record.selected_round_id,
                        int(record.round_options_count or 0),
                        record.sort_key,
                        record.model_dump_json(),
                    ),
                )

    def upsert_task_node(self, record: TaskProjectionNodeRecord) -> TaskProjectionNodeRecord:
        self._upsert(
            'task_nodes',
            [
                'node_id',
                'task_id',
                'parent_node_id',
                'root_node_id',
                'depth',
                'node_kind',
                'status',
                'title',
                'updated_at',
                'default_round_id',
                'selected_round_id',
                'round_options_count',
                'sort_key',
                'payload_json',
            ],
            [
                record.node_id,
                record.task_id,
                record.parent_node_id,
                record.root_node_id,
                int(record.depth or 0),
                record.node_kind,
                record.status,
                record.title,
                record.updated_at,
                record.default_round_id,
                record.selected_round_id,
                int(record.round_options_count or 0),
                record.sort_key,
                record.model_dump_json(),
            ],
            'node_id',
        )
        return record

    def list_task_nodes(self, task_id: str) -> list[TaskProjectionNodeRecord]:
        rows = self._fetchall('SELECT payload_json FROM task_nodes WHERE task_id = ? ORDER BY sort_key ASC, node_id ASC', (task_id,))
        return [self._parse(row['payload_json'], TaskProjectionNodeRecord) for row in rows]

    def get_task_node(self, node_id: str) -> TaskProjectionNodeRecord | None:
        row = self._fetchone('SELECT payload_json FROM task_nodes WHERE node_id = ?', (node_id,))
        return self._parse(row['payload_json'], TaskProjectionNodeRecord) if row else None

    def replace_task_node_details(self, task_id: str, records: list[TaskProjectionNodeDetailRecord]) -> None:
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM task_node_details WHERE task_id = ?', (task_id,))
            for record in records:
                self._conn.execute(
                    'INSERT INTO task_node_details (node_id, task_id, updated_at, input_text, input_ref, output_text, output_ref, check_result, check_result_ref, final_output, final_output_ref, failure_reason, prompt_summary, execution_trace_ref, payload_json) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        record.node_id,
                        record.task_id,
                        record.updated_at,
                        record.input_text,
                        record.input_ref,
                        record.output_text,
                        record.output_ref,
                        record.check_result,
                        record.check_result_ref,
                        record.final_output,
                        record.final_output_ref,
                        record.failure_reason,
                        record.prompt_summary,
                        record.execution_trace_ref,
                        record.model_dump_json(),
                    ),
                )

    def upsert_task_node_detail(self, record: TaskProjectionNodeDetailRecord) -> TaskProjectionNodeDetailRecord:
        self._upsert(
            'task_node_details',
            [
                'node_id',
                'task_id',
                'updated_at',
                'input_text',
                'input_ref',
                'output_text',
                'output_ref',
                'check_result',
                'check_result_ref',
                'final_output',
                'final_output_ref',
                'failure_reason',
                'prompt_summary',
                'execution_trace_ref',
                'payload_json',
            ],
            [
                record.node_id,
                record.task_id,
                record.updated_at,
                record.input_text,
                record.input_ref,
                record.output_text,
                record.output_ref,
                record.check_result,
                record.check_result_ref,
                record.final_output,
                record.final_output_ref,
                record.failure_reason,
                record.prompt_summary,
                record.execution_trace_ref,
                record.model_dump_json(),
            ],
            'node_id',
        )
        return record

    def get_task_node_detail(self, node_id: str) -> TaskProjectionNodeDetailRecord | None:
        row = self._fetchone('SELECT payload_json FROM task_node_details WHERE node_id = ?', (node_id,))
        return self._parse(row['payload_json'], TaskProjectionNodeDetailRecord) if row else None

    def list_task_node_details(self, task_id: str) -> list[TaskProjectionNodeDetailRecord]:
        rows = self._fetchall('SELECT payload_json FROM task_node_details WHERE task_id = ? ORDER BY node_id ASC', (task_id,))
        return [self._parse(row['payload_json'], TaskProjectionNodeDetailRecord) for row in rows]

    def replace_task_runtime_frames(self, task_id: str, records: list[TaskProjectionRuntimeFrameRecord]) -> None:
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM task_runtime_frames WHERE task_id = ?', (task_id,))
            for record in records:
                self._conn.execute(
                    'INSERT INTO task_runtime_frames (task_id, node_id, depth, node_kind, phase, active, runnable, waiting, updated_at, payload_json) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        record.task_id,
                        record.node_id,
                        int(record.depth or 0),
                        record.node_kind,
                        record.phase,
                        1 if record.active else 0,
                        1 if record.runnable else 0,
                        1 if record.waiting else 0,
                        record.updated_at,
                        record.model_dump_json(),
                    ),
                )

    def upsert_task_runtime_frame(self, record: TaskProjectionRuntimeFrameRecord) -> TaskProjectionRuntimeFrameRecord:
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO task_runtime_frames (task_id, node_id, depth, node_kind, phase, active, runnable, waiting, updated_at, payload_json) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(task_id, node_id) DO UPDATE SET '
                'depth=excluded.depth, '
                'node_kind=excluded.node_kind, '
                'phase=excluded.phase, '
                'active=excluded.active, '
                'runnable=excluded.runnable, '
                'waiting=excluded.waiting, '
                'updated_at=excluded.updated_at, '
                'payload_json=excluded.payload_json',
                (
                    record.task_id,
                    record.node_id,
                    int(record.depth or 0),
                    record.node_kind,
                    record.phase,
                    1 if record.active else 0,
                    1 if record.runnable else 0,
                    1 if record.waiting else 0,
                    record.updated_at,
                    record.model_dump_json(),
                ),
            )
        return record

    def get_task_runtime_frame(self, task_id: str, node_id: str) -> TaskProjectionRuntimeFrameRecord | None:
        row = self._fetchone(
            'SELECT payload_json FROM task_runtime_frames WHERE task_id = ? AND node_id = ?',
            (task_id, node_id),
        )
        return self._parse(row['payload_json'], TaskProjectionRuntimeFrameRecord) if row else None

    def delete_task_runtime_frame(self, task_id: str, node_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                'DELETE FROM task_runtime_frames WHERE task_id = ? AND node_id = ?',
                (str(task_id or '').strip(), str(node_id or '').strip()),
            )

    def list_task_runtime_frames(self, task_id: str) -> list[TaskProjectionRuntimeFrameRecord]:
        rows = self._fetchall('SELECT payload_json FROM task_runtime_frames WHERE task_id = ? ORDER BY depth ASC, node_id ASC', (task_id,))
        return [self._parse(row['payload_json'], TaskProjectionRuntimeFrameRecord) for row in rows]

    def replace_task_node_rounds(self, task_id: str, records: list[TaskProjectionRoundRecord]) -> None:
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM task_node_rounds WHERE task_id = ?', (task_id,))
            for record in records:
                self._conn.execute(
                    'INSERT INTO task_node_rounds (task_id, parent_node_id, round_id, round_index, label, is_latest, created_at, source, total_children, completed_children, running_children, failed_children, child_node_ids_json, payload_json) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        record.task_id,
                        record.parent_node_id,
                        record.round_id,
                        int(record.round_index or 0),
                        record.label,
                        1 if record.is_latest else 0,
                        record.created_at,
                        record.source,
                        int(record.total_children or 0),
                        int(record.completed_children or 0),
                        int(record.running_children or 0),
                        int(record.failed_children or 0),
                        json.dumps(list(record.child_node_ids or []), ensure_ascii=False),
                        record.model_dump_json(),
                    ),
                )

    def replace_task_node_rounds_for_parent(
        self,
        task_id: str,
        parent_node_id: str,
        records: list[TaskProjectionRoundRecord],
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                'DELETE FROM task_node_rounds WHERE task_id = ? AND parent_node_id = ?',
                (task_id, parent_node_id),
            )
            for record in records:
                self._conn.execute(
                    'INSERT INTO task_node_rounds (task_id, parent_node_id, round_id, round_index, label, is_latest, created_at, source, total_children, completed_children, running_children, failed_children, child_node_ids_json, payload_json) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        record.task_id,
                        record.parent_node_id,
                        record.round_id,
                        int(record.round_index or 0),
                        record.label,
                        1 if record.is_latest else 0,
                        record.created_at,
                        record.source,
                        int(record.total_children or 0),
                        int(record.completed_children or 0),
                        int(record.running_children or 0),
                        int(record.failed_children or 0),
                        json.dumps(list(record.child_node_ids or []), ensure_ascii=False),
                        record.model_dump_json(),
                    ),
                )

    def list_task_node_rounds(self, task_id: str) -> list[TaskProjectionRoundRecord]:
        rows = self._fetchall(
            'SELECT payload_json FROM task_node_rounds WHERE task_id = ? ORDER BY parent_node_id ASC, round_index ASC, round_id ASC',
            (task_id,),
        )
        return [self._parse(row['payload_json'], TaskProjectionRoundRecord) for row in rows]

    def append_task_model_call(
        self,
        *,
        task_id: str,
        node_id: str,
        created_at: str,
        payload: dict[str, object],
    ) -> int:
        payload_json = json.dumps(payload)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                'INSERT INTO task_model_calls (task_id, node_id, created_at, payload_json) VALUES (?, ?, ?, ?)',
                (task_id, node_id, created_at, payload_json),
            )
            return int(cursor.lastrowid or 0)

    def list_task_model_calls(self, task_id: str, *, limit: int = 50) -> list[dict[str, object]]:
        rows = self._fetchall(
            'SELECT seq, task_id, node_id, created_at, payload_json FROM task_model_calls WHERE task_id = ? ORDER BY seq DESC LIMIT ?',
            (task_id, max(1, int(limit or 50))),
        )
        items: list[dict[str, object]] = []
        for row in rows:
            payload = json.loads(row['payload_json'])
            items.append(
                {
                    'seq': int(row['seq']),
                    'task_id': row['task_id'],
                    'node_id': row['node_id'],
                    'created_at': row['created_at'],
                    'payload': payload if isinstance(payload, dict) else {},
                }
            )
        items.reverse()
        return items

    def _upsert(self, table: str, columns: list[str], values: list[object], primary_key: str) -> None:
        with self._lock, self._conn:
            self._upsert_unlocked(table, columns, values, primary_key)

    def _upsert_unlocked(self, table: str, columns: list[str], values: list[object], primary_key: str) -> None:
        placeholders = ', '.join('?' for _ in columns)
        updates = ', '.join(f"{column}=excluded.{column}" for column in columns if column != primary_key)
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT({primary_key}) DO UPDATE SET {updates}"
        self._conn.execute(sql, values)

    def _fetchone(self, sql: str, params: tuple[object, ...]) -> sqlite3.Row | None:
        with self._read_lock:
            return self._read_conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._read_lock:
            return list(self._read_conn.execute(sql, params).fetchall())

    @staticmethod
    def _task_terminal_outbox_row(row: sqlite3.Row | None) -> dict[str, object]:
        if row is None:
            return {}
        payload = json.loads(row['payload_json'])
        return {
            'dedupe_key': row['dedupe_key'],
            'task_id': row['task_id'],
            'session_id': row['session_id'],
            'delivery_state': row['delivery_state'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
            'delivered_at': row['delivered_at'],
            'attempts': int(row['attempts'] or 0),
            'last_attempt_at': row['last_attempt_at'],
            'last_error': row['last_error'],
            'payload': payload if isinstance(payload, dict) else {},
        }

    @staticmethod
    def _task_stall_outbox_row(row: sqlite3.Row | None) -> dict[str, object]:
        if row is None:
            return {}
        payload = json.loads(row['payload_json'])
        return {
            'dedupe_key': row['dedupe_key'],
            'task_id': row['task_id'],
            'session_id': row['session_id'],
            'delivery_state': row['delivery_state'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
            'delivered_at': row['delivered_at'],
            'attempts': int(row['attempts'] or 0),
            'last_attempt_at': row['last_attempt_at'],
            'last_error': row['last_error'],
            'payload': payload if isinstance(payload, dict) else {},
        }

    @staticmethod
    def _task_worker_status_outbox_row(row: sqlite3.Row | None) -> dict[str, object]:
        if row is None:
            return {}
        payload = json.loads(row['payload_json'])
        return {
            'worker_id': row['worker_id'],
            'delivery_state': row['delivery_state'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
            'delivered_at': row['delivered_at'],
            'attempts': int(row['attempts'] or 0),
            'last_attempt_at': row['last_attempt_at'],
            'last_error': row['last_error'],
            'payload': payload if isinstance(payload, dict) else {},
        }

    @staticmethod
    def _parse(payload_json: str, model_cls: type[T]) -> T:
        return model_cls.model_validate(json.loads(payload_json))
