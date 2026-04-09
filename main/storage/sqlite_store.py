from __future__ import annotations

import gzip
import hashlib
import json
import queue
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

from main.models import NodeRecord, TaskArtifactRecord, TaskRecord
from main.monitoring.models import (
    TaskProjectionMetaRecord,
    TaskProjectionNodeDetailRecord,
    TaskProjectionNodeRecord,
    TaskProjectionRoundRecord,
    TaskProjectionRuntimeFrameRecord,
    TaskProjectionToolResultRecord,
)

T = TypeVar('T', bound=BaseModel)
R = TypeVar('R')


class SQLiteTaskStore:
    def __init__(
        self,
        path: Path | str,
        debug_recorder=None,
        *,
        event_history_dir: Path | str | None = None,
        event_history_enabled: bool = True,
        event_history_archive_encoding: str = 'gzip',
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._event_history_dir = Path(event_history_dir or (self.path.parent / 'event-history'))
        self._event_history_enabled = bool(event_history_enabled)
        self._event_history_archive_encoding = str(event_history_archive_encoding or 'gzip').strip().lower() or 'gzip'
        if self._event_history_archive_encoding not in {'gzip', 'plain'}:
            self._event_history_archive_encoding = 'gzip'
        if self._event_history_enabled:
            self._event_history_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._read_lock = threading.RLock()
        self._closed = False
        self._debug_recorder = debug_recorder
        self._writer_queue: queue.Queue[tuple[Callable[[sqlite3.Connection], Any] | None, threading.Event | None, dict[str, Any] | None]] = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._metrics_lock = threading.RLock()
        self._runtime_metrics: dict[str, float] = {
            'sqlite_write_wait_ms': 0.0,
            'sqlite_write_exec_ms': 0.0,
            'sqlite_query_latency_ms': 0.0,
            'runtime_metrics_updated_mono': 0.0,
        }
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute('PRAGMA journal_mode=WAL')
        self._setup()
        self._read_conn = self._open_read_conn()
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name=f'sqlite-task-store-writer:{self.path.name}',
            daemon=True,
        )
        self._writer_thread.start()

    def close(self) -> None:
        writer_thread: threading.Thread | None = None
        with self._lock:
            if self._closed:
                return
            self._closed = True
            writer_thread = self._writer_thread
            self._writer_queue.put((None, None, None))
        if writer_thread is not None:
            writer_thread.join(timeout=5.0)
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
                payload_json TEXT NOT NULL,
                payload_is_external INTEGER NOT NULL DEFAULT 0,
                payload_archive_path TEXT NOT NULL DEFAULT '',
                payload_archive_encoding TEXT NOT NULL DEFAULT '',
                payload_hash TEXT NOT NULL DEFAULT ''
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
                payload_json TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT ''
            )
            ''',
            '''
            CREATE TABLE IF NOT EXISTS worker_leases (
                role TEXT PRIMARY KEY,
                worker_id TEXT NOT NULL,
                holder_pid INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
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
            CREATE TABLE IF NOT EXISTS task_node_tool_results (
                task_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                order_index INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_text TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                elapsed_seconds REAL,
                output_preview_text TEXT NOT NULL,
                output_ref TEXT NOT NULL,
                ephemeral INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (task_id, node_id, tool_call_id)
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
                accepted INTEGER,
                rejected_reason TEXT NOT NULL DEFAULT '',
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
            '''
            CREATE TABLE IF NOT EXISTS task_summary_outbox (
                task_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                delivery_state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                last_attempt_at TEXT NOT NULL,
                last_error TEXT NOT NULL,
                version INTEGER NOT NULL,
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
            'CREATE INDEX IF NOT EXISTS idx_task_node_tool_results_task_node_order ON task_node_tool_results(task_id, node_id, order_index)',
            'CREATE INDEX IF NOT EXISTS idx_task_runtime_meta_updated_at ON task_runtime_meta(updated_at)',
            'CREATE INDEX IF NOT EXISTS idx_task_node_rounds_task_parent ON task_node_rounds(task_id, parent_node_id, round_index)',
            'CREATE INDEX IF NOT EXISTS idx_task_model_calls_task_id_seq ON task_model_calls(task_id, seq)',
            'CREATE INDEX IF NOT EXISTS idx_task_terminal_outbox_state_created_at ON task_terminal_outbox(delivery_state, created_at)',
            'CREATE INDEX IF NOT EXISTS idx_task_stall_outbox_state_created_at ON task_stall_outbox(delivery_state, created_at)',
            'CREATE INDEX IF NOT EXISTS idx_task_worker_status_outbox_state_updated_at ON task_worker_status_outbox(delivery_state, updated_at)',
            'CREATE INDEX IF NOT EXISTS idx_task_summary_outbox_state_updated_at ON task_summary_outbox(delivery_state, updated_at)',
        ]
        with self._conn:
            for statement in statements:
                self._conn.execute(statement)
            self._ensure_column(self._conn, 'task_events', 'payload_is_external', "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(self._conn, 'task_events', 'payload_archive_path', "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(self._conn, 'task_events', 'payload_archive_encoding', "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(self._conn, 'task_events', 'payload_hash', "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(self._conn, 'task_commands', 'result_json', "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(self._conn, 'task_terminal_outbox', 'accepted', "INTEGER")
            self._ensure_column(self._conn, 'task_terminal_outbox', 'rejected_reason', "TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row[1]) for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}
        if str(column or '') in columns:
            return
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')

    def _writer_loop(self) -> None:
        while True:
            operation, done, outcome = self._writer_queue.get()
            started_mono = 0.0
            finished_mono = 0.0
            try:
                if operation is None:
                    break
                started_mono = time.perf_counter()
                with self._conn:
                    value = operation(self._conn)
                finished_mono = time.perf_counter()
                if outcome is not None:
                    outcome['value'] = value
            except Exception as exc:
                finished_mono = time.perf_counter()
                if outcome is not None:
                    outcome['error'] = exc
            finally:
                if outcome is not None:
                    if started_mono > 0.0:
                        outcome['started_mono'] = started_mono
                    if finished_mono > 0.0:
                        outcome['finished_mono'] = finished_mono
                if done is not None:
                    done.set()
                self._writer_queue.task_done()

    def _run_write(self, operation: Callable[[sqlite3.Connection], R]) -> R:
        with self._lock:
            if self._closed:
                raise RuntimeError('sqlite_task_store_closed')
        outcome: dict[str, Any] = {}
        done = threading.Event()
        started_at = datetime.now().astimezone().isoformat(timespec='seconds')
        queued_mono = time.perf_counter()
        self._writer_queue.put((operation, done, outcome))
        done.wait()
        completed_mono = time.perf_counter()
        started_mono = float(outcome.get('started_mono') or completed_mono)
        finished_mono = float(outcome.get('finished_mono') or completed_mono)
        total_wait_ms = max(0.0, (finished_mono - queued_mono) * 1000.0)
        self._update_runtime_metrics(
            sqlite_write_wait_ms=total_wait_ms,
            sqlite_write_exec_ms=max(0.0, (finished_mono - started_mono) * 1000.0),
        )
        recorder = self._debug_recorder
        if recorder is not None and hasattr(recorder, 'record'):
            try:
                recorder.record(section='sqlite.write', elapsed_ms=total_wait_ms, started_at=started_at)
            except Exception:
                pass
        error = outcome.get('error')
        if error is not None:
            raise error
        return outcome.get('value')  # type: ignore[return-value]

    def _execute_write(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self._run_write(lambda conn: conn.execute(sql, params))

    def writer_queue_depth(self) -> int:
        try:
            return max(0, int(self._writer_queue.qsize()))
        except Exception:
            return 0

    def runtime_metrics_snapshot(self) -> dict[str, float]:
        with self._metrics_lock:
            snapshot = dict(self._runtime_metrics)
        snapshot['writer_queue_depth'] = float(self.writer_queue_depth())
        return snapshot

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
        def operation(conn: sqlite3.Connection) -> TaskRecord | None:
            row = conn.execute('SELECT payload_json FROM tasks WHERE task_id = ?', (task_id,)).fetchone()
            if row is None:
                return None
            record = self._parse(row['payload_json'], TaskRecord)
            updated = mutator(record)
            self._upsert_conn(
                conn,
                'tasks',
                ['task_id', 'session_id', 'status', 'updated_at', 'payload_json'],
                [updated.task_id, updated.session_id, updated.status, updated.updated_at, updated.model_dump_json()],
                'task_id',
            )
            return updated
        return self._run_write(operation)

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
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(f'DELETE FROM task_runtime_frames WHERE node_id IN ({placeholders})', tuple(normalized_ids))
            conn.execute(f'DELETE FROM task_model_calls WHERE node_id IN ({placeholders})', tuple(normalized_ids))
            conn.execute(f'DELETE FROM task_node_tool_results WHERE node_id IN ({placeholders})', tuple(normalized_ids))
            conn.execute(f'DELETE FROM task_node_details WHERE node_id IN ({placeholders})', tuple(normalized_ids))
            conn.execute(f'DELETE FROM task_node_rounds WHERE parent_node_id IN ({placeholders})', tuple(normalized_ids))
            conn.execute(f'DELETE FROM task_nodes WHERE node_id IN ({placeholders})', tuple(normalized_ids))
            conn.execute(f'DELETE FROM nodes WHERE node_id IN ({placeholders})', tuple(normalized_ids))

        self._run_write(operation)

    def update_node(self, node_id: str, mutator) -> NodeRecord | None:
        def operation(conn: sqlite3.Connection) -> NodeRecord | None:
            row = conn.execute('SELECT payload_json FROM nodes WHERE node_id = ?', (node_id,)).fetchone()
            if row is None:
                return None
            record = self._parse(row['payload_json'], NodeRecord)
            updated = mutator(record)
            self._upsert_conn(
                conn,
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
        return self._run_write(operation)

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
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute('DELETE FROM task_projection_meta WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM task_node_rounds WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM task_runtime_frames WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM task_node_tool_results WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM task_node_details WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM task_nodes WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM task_model_calls WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM task_runtime_meta WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM task_commands WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM task_terminal_outbox WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM task_stall_outbox WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM task_summary_outbox WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM task_events WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM artifacts WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM nodes WHERE task_id = ?', (task_id,))
            conn.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))
        self._run_write(operation)

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
        payload_json = json.dumps(payload, ensure_ascii=False)
        payload_hash = hashlib.sha256(payload_json.encode('utf-8')).hexdigest()
        should_externalize = self._should_externalize_task_event(
            task_id=task_id,
            event_type=event_type,
            payload=payload,
        )
        stored_payload = self._task_event_storage_payload(
            task_id=task_id,
            event_type=event_type,
            payload=payload,
        ) if should_externalize else payload
        stored_payload_json = json.dumps(stored_payload, ensure_ascii=False)
        archive_encoding = self._event_history_archive_encoding if should_externalize else ''

        def operation(conn: sqlite3.Connection) -> int:
            if should_externalize:
                previous = conn.execute(
                    'SELECT seq, payload_hash FROM task_events WHERE task_id = ? AND event_type = ? ORDER BY seq DESC LIMIT 1',
                    (task_id, event_type),
                ).fetchone()
                if previous is not None and str(previous['payload_hash'] or '') == payload_hash:
                    return int(previous['seq'] or 0)
            cursor = conn.execute(
                'INSERT INTO task_events (task_id, session_id, event_type, created_at, payload_json, payload_is_external, payload_archive_path, payload_archive_encoding, payload_hash) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    task_id,
                    session_id,
                    event_type,
                    created_at,
                    stored_payload_json,
                    1 if should_externalize else 0,
                    '',
                    archive_encoding,
                    payload_hash,
                ),
            )
            seq = int(cursor.lastrowid or 0)
            if should_externalize and seq > 0:
                archive_path = self._write_task_event_archive(task_id=task_id, seq=seq, payload_json=payload_json)
                conn.execute(
                    'UPDATE task_events SET payload_archive_path = ? WHERE seq = ?',
                    (archive_path, seq),
                )
            return seq
        return self._run_write(operation)

    def list_task_events(
        self,
        *,
        after_seq: int = 0,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 200,
        hydrate_external: bool = True,
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
            'SELECT seq, task_id, session_id, event_type, created_at, payload_json, payload_is_external, payload_archive_path, payload_archive_encoding, payload_hash '
            f'FROM task_events WHERE {" AND ".join(predicates)} ORDER BY seq ASC LIMIT ?'
        )
        rows = self._fetchall(sql, tuple(params))
        events: list[dict[str, object]] = []
        for row in rows:
            payload = json.loads(row['payload_json'])
            if bool(row['payload_is_external']) and bool(hydrate_external):
                hydrated = self._read_task_event_archive(
                    path=str(row['payload_archive_path'] or '').strip(),
                    encoding=str(row['payload_archive_encoding'] or '').strip(),
                )
                if isinstance(hydrated, dict):
                    payload = hydrated
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

    def _should_externalize_task_event(
        self,
        *,
        task_id: str | None,
        event_type: str,
        payload: dict[str, object],
    ) -> bool:
        _ = payload
        if not self._event_history_enabled:
            return False
        return bool(str(task_id or '').strip()) and str(event_type or '').strip() == 'task.live.patch'

    @staticmethod
    def _task_event_storage_payload(
        *,
        task_id: str | None,
        event_type: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        if str(event_type or '').strip() != 'task.live.patch':
            return dict(payload or {})
        runtime_summary = dict(payload.get('runtime_summary') or {}) if isinstance(payload.get('runtime_summary'), dict) else {}
        frame = dict(payload.get('frame') or {}) if isinstance(payload.get('frame'), dict) else {}
        active_node_ids = [str(item) for item in list(runtime_summary.get('active_node_ids') or []) if str(item or '').strip()]
        runnable_node_ids = [str(item) for item in list(runtime_summary.get('runnable_node_ids') or []) if str(item or '').strip()]
        waiting_node_ids = [str(item) for item in list(runtime_summary.get('waiting_node_ids') or []) if str(item or '').strip()]
        return {
            'task_id': str(payload.get('task_id') or task_id or '').strip(),
            'runtime_summary_preview': {
                'active_node_count': len(active_node_ids),
                'runnable_node_count': len(runnable_node_ids),
                'waiting_node_count': len(waiting_node_ids),
                'active_node_ids_preview': active_node_ids[:5],
                'runnable_node_ids_preview': runnable_node_ids[:5],
                'waiting_node_ids_preview': waiting_node_ids[:5],
            },
            'frame_preview': {
                'node_id': str(frame.get('node_id') or '').strip(),
                'phase': str(frame.get('phase') or '').strip(),
                'stage_goal': SQLiteTaskStore._clip_text(frame.get('stage_goal')),
                'tool_call_count': len(list(frame.get('tool_calls') or [])),
                'child_pipeline_count': len(list(frame.get('child_pipelines') or [])),
            },
            'removed_node_id': str(payload.get('removed_node_id') or '').strip(),
            'payload_externalized': True,
        }

    def _write_task_event_archive(self, *, task_id: str | None, seq: int, payload_json: str) -> str:
        task_component = self._safe_path_component(task_id or 'global')
        suffix = '.json.gz' if self._event_history_archive_encoding == 'gzip' else '.json'
        relative_path = Path(task_component) / f'{int(seq)}{suffix}'
        archive_path = self._event_history_dir / relative_path
        temp_path = archive_path.with_name(f'{archive_path.name}.tmp')
        last_error: OSError | None = None
        for _attempt in range(3):
            try:
                self._event_history_dir.mkdir(parents=True, exist_ok=True)
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                if self._event_history_archive_encoding == 'gzip':
                    with gzip.open(temp_path, 'wt', encoding='utf-8') as handle:
                        handle.write(payload_json)
                else:
                    temp_path.write_text(payload_json, encoding='utf-8')
                temp_path.replace(archive_path)
                break
            except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
                last_error = exc
                time.sleep(0.01)
                continue
            finally:
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except FileNotFoundError:
                        pass
        else:
            if last_error is not None:
                raise last_error
        return relative_path.as_posix()

    def _read_task_event_archive(self, *, path: str, encoding: str) -> dict[str, object] | None:
        relative_path = Path(str(path or '').strip())
        if not str(relative_path).strip():
            return None
        archive_path = self._event_history_dir / relative_path
        if not archive_path.exists():
            return None
        normalized_encoding = str(encoding or '').strip().lower() or self._event_history_archive_encoding
        try:
            if normalized_encoding == 'gzip':
                with gzip.open(archive_path, 'rt', encoding='utf-8') as handle:
                    payload = json.loads(handle.read())
            else:
                payload = json.loads(archive_path.read_text(encoding='utf-8'))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _safe_path_component(value: str) -> str:
        text = str(value or '').strip() or 'global'
        safe = ''.join(ch if ch.isalnum() or ch in {'-', '_', '.'} else '_' for ch in text)
        return safe or 'global'

    @staticmethod
    def _clip_text(value: Any, *, limit: int = 240) -> str:
        text = ' '.join(str(value or '').split())
        if len(text) <= limit:
            return text
        return f'{text[: max(0, limit - 3)].rstrip()}...'

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
        self._execute_write(
            'INSERT INTO task_commands (command_id, task_id, session_id, command_type, status, created_at, payload_json, result_json) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (command_id, task_id, session_id, command_type, 'pending', created_at, payload_json, ''),
        )

    def claim_pending_task_commands(
        self,
        *,
        worker_id: str,
        claimed_at: str,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        def operation(conn: sqlite3.Connection) -> list[dict[str, object]]:
            rows = list(
                conn.execute(
                    'SELECT command_id, task_id, session_id, command_type, created_at, payload_json '
                    'FROM task_commands WHERE status = ? ORDER BY created_at ASC LIMIT ?',
                    ('pending', max(1, int(limit or 20))),
                ).fetchall()
            )
            if not rows:
                return []
            command_ids = [str(row['command_id']) for row in rows]
            for command_id in command_ids:
                conn.execute(
                    'UPDATE task_commands SET status = ?, worker_id = ?, claimed_at = ?, result_json = ? WHERE command_id = ?',
                    ('claimed', worker_id, claimed_at, '', command_id),
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
        return self._run_write(operation)

    def get_task_command(self, command_id: str) -> dict[str, object] | None:
        row = self._fetchone(
            'SELECT command_id, task_id, session_id, command_type, status, created_at, claimed_at, finished_at, worker_id, error_text, payload_json, result_json '
            'FROM task_commands WHERE command_id = ?',
            (str(command_id or '').strip(),),
        )
        return self._task_command_row(row) if row else None

    def finish_task_command(
        self,
        command_id: str,
        *,
        finished_at: str,
        success: bool,
        error_text: str = '',
        result: dict[str, object] | None = None,
    ) -> None:
        self._execute_write(
            'UPDATE task_commands SET status = ?, finished_at = ?, error_text = ?, result_json = ? WHERE command_id = ?',
            (
                'completed' if success else 'failed',
                finished_at,
                str(error_text or ''),
                json.dumps(result or {}, ensure_ascii=False) if result is not None else '',
                command_id,
            ),
        )

    @staticmethod
    def _parse_iso_datetime(value: Any) -> datetime | None:
        text = str(value or '').strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None

    def acquire_worker_lease(
        self,
        *,
        role: str,
        worker_id: str,
        holder_pid: int,
        acquired_at: str,
        heartbeat_at: str,
        expires_at: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        normalized_role = str(role or '').strip()
        if not normalized_role:
            raise ValueError('worker_lease_role_required')
        normalized_worker_id = str(worker_id or '').strip()
        if not normalized_worker_id:
            raise ValueError('worker_lease_worker_id_required')
        normalized_payload = dict(payload or {})
        normalized_pid = max(0, int(holder_pid or 0))

        def operation(conn: sqlite3.Connection) -> dict[str, object]:
            row = conn.execute(
                'SELECT role, worker_id, holder_pid, acquired_at, heartbeat_at, expires_at, payload_json FROM worker_leases WHERE role = ?',
                (normalized_role,),
            ).fetchone()
            if row is None:
                conn.execute(
                    'INSERT INTO worker_leases (role, worker_id, holder_pid, acquired_at, heartbeat_at, expires_at, payload_json) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (
                        normalized_role,
                        normalized_worker_id,
                        normalized_pid,
                        acquired_at,
                        heartbeat_at,
                        expires_at,
                        json.dumps(normalized_payload, ensure_ascii=False),
                    ),
                )
                return {
                    'acquired': True,
                    'takeover': False,
                    'role': normalized_role,
                    'worker_id': normalized_worker_id,
                    'holder_pid': normalized_pid,
                    'acquired_at': acquired_at,
                    'heartbeat_at': heartbeat_at,
                    'expires_at': expires_at,
                    'payload': normalized_payload,
                }

            current = self._worker_lease_row(row)
            current_expires_at = self._parse_iso_datetime(current.get('expires_at'))
            requested_at = self._parse_iso_datetime(acquired_at)
            current_worker_id = str(current.get('worker_id') or '').strip()
            can_take = current_worker_id == normalized_worker_id
            if not can_take and current_expires_at is not None and requested_at is not None:
                can_take = current_expires_at <= requested_at
            if not can_take:
                return {
                    'acquired': False,
                    'takeover': False,
                    **current,
                }

            conn.execute(
                'UPDATE worker_leases SET worker_id = ?, holder_pid = ?, acquired_at = ?, heartbeat_at = ?, expires_at = ?, payload_json = ? '
                'WHERE role = ?',
                (
                    normalized_worker_id,
                    normalized_pid,
                    acquired_at,
                    heartbeat_at,
                    expires_at,
                    json.dumps(normalized_payload, ensure_ascii=False),
                    normalized_role,
                ),
            )
            return {
                'acquired': True,
                'takeover': current_worker_id not in {'', normalized_worker_id},
                'previous_worker_id': current_worker_id,
                'role': normalized_role,
                'worker_id': normalized_worker_id,
                'holder_pid': normalized_pid,
                'acquired_at': acquired_at,
                'heartbeat_at': heartbeat_at,
                'expires_at': expires_at,
                'payload': normalized_payload,
            }

        return self._run_write(operation)

    def renew_worker_lease(
        self,
        *,
        role: str,
        worker_id: str,
        heartbeat_at: str,
        expires_at: str,
        payload: dict[str, object],
    ) -> bool:
        normalized_role = str(role or '').strip()
        normalized_worker_id = str(worker_id or '').strip()
        if not normalized_role or not normalized_worker_id:
            return False

        def operation(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                'SELECT worker_id FROM worker_leases WHERE role = ?',
                (normalized_role,),
            ).fetchone()
            if row is None or str(row['worker_id'] or '').strip() != normalized_worker_id:
                return False
            conn.execute(
                'UPDATE worker_leases SET heartbeat_at = ?, expires_at = ?, payload_json = ? WHERE role = ? AND worker_id = ?',
                (
                    heartbeat_at,
                    expires_at,
                    json.dumps(dict(payload or {}), ensure_ascii=False),
                    normalized_role,
                    normalized_worker_id,
                ),
            )
            return True

        return bool(self._run_write(operation))

    def get_worker_lease(self, role: str) -> dict[str, object] | None:
        row = self._fetchone(
            'SELECT role, worker_id, holder_pid, acquired_at, heartbeat_at, expires_at, payload_json FROM worker_leases WHERE role = ?',
            (str(role or '').strip(),),
        )
        return self._worker_lease_row(row) if row else None

    def release_worker_lease(self, *, role: str, worker_id: str) -> None:
        normalized_role = str(role or '').strip()
        normalized_worker_id = str(worker_id or '').strip()
        if not normalized_role or not normalized_worker_id:
            return
        self._execute_write(
            'DELETE FROM worker_leases WHERE role = ? AND worker_id = ?',
            (normalized_role, normalized_worker_id),
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
        def operation(conn: sqlite3.Connection) -> dict[str, object]:
            row = conn.execute(
                'SELECT dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, accepted, rejected_reason, payload_json '
                'FROM task_terminal_outbox WHERE dedupe_key = ?',
                (key,),
            ).fetchone()
            if row is None:
                payload_json = json.dumps(payload, ensure_ascii=False)
                conn.execute(
                    'INSERT INTO task_terminal_outbox (dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, accepted, rejected_reason, payload_json) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (key, task_id, session_id, 'pending', created_at, created_at, '', 0, '', '', None, '', payload_json),
                )
                row = conn.execute(
                    'SELECT dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, accepted, rejected_reason, payload_json '
                    'FROM task_terminal_outbox WHERE dedupe_key = ?',
                    (key,),
                ).fetchone()
            return self._task_terminal_outbox_row(row)
        return self._run_write(operation)

    def get_task_terminal_outbox(self, dedupe_key: str) -> dict[str, object] | None:
        row = self._fetchone(
            'SELECT dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, accepted, rejected_reason, payload_json '
            'FROM task_terminal_outbox WHERE dedupe_key = ?',
            (str(dedupe_key or '').strip(),),
        )
        return self._task_terminal_outbox_row(row) if row else None

    def list_pending_task_terminal_outbox(self, *, limit: int = 200) -> list[dict[str, object]]:
        rows = self._fetchall(
            'SELECT dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, accepted, rejected_reason, payload_json '
            'FROM task_terminal_outbox WHERE delivery_state != ? ORDER BY created_at ASC LIMIT ?',
            ('delivered', max(1, int(limit or 200))),
        )
        return [self._task_terminal_outbox_row(row) for row in rows]

    def mark_task_terminal_outbox_enqueue_result(
        self,
        dedupe_key: str,
        *,
        accepted: bool | None,
        rejected_reason: str,
        updated_at: str,
    ) -> None:
        key = str(dedupe_key or '').strip()
        if not key:
            return

        def operation(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                'SELECT accepted FROM task_terminal_outbox WHERE dedupe_key = ?',
                (key,),
            ).fetchone()
            if row is None:
                return
            current_accepted_raw = row['accepted']
            current_accepted = None if current_accepted_raw is None else bool(int(current_accepted_raw))
            next_accepted = None if accepted is None else bool(accepted)
            if current_accepted is True and next_accepted is False:
                return
            conn.execute(
                'UPDATE task_terminal_outbox SET updated_at = ?, accepted = ?, rejected_reason = ? WHERE dedupe_key = ?',
                (
                    str(updated_at or ''),
                    None if next_accepted is None else (1 if next_accepted else 0),
                    '' if next_accepted is True else str(rejected_reason or ''),
                    key,
                ),
            )

        self._run_write(operation)

    def mark_task_terminal_outbox_attempt(self, dedupe_key: str, *, attempted_at: str, error_text: str) -> None:
        key = str(dedupe_key or '').strip()
        if not key:
            return
        def operation(conn: sqlite3.Connection) -> None:
            row = conn.execute('SELECT attempts, delivery_state FROM task_terminal_outbox WHERE dedupe_key = ?', (key,)).fetchone()
            if row is None:
                return
            delivery_state = str(row['delivery_state'] or '').strip() or 'pending'
            if delivery_state == 'delivered':
                return
            attempts = int(row['attempts'] or 0) + 1
            conn.execute(
                'UPDATE task_terminal_outbox SET delivery_state = ?, updated_at = ?, attempts = ?, last_attempt_at = ?, last_error = ? WHERE dedupe_key = ?',
                ('pending', attempted_at, attempts, attempted_at, str(error_text or ''), key),
            )
        self._run_write(operation)

    def mark_task_terminal_outbox_delivered(self, dedupe_key: str, *, delivered_at: str) -> None:
        key = str(dedupe_key or '').strip()
        if not key:
            return
        self._execute_write(
            'UPDATE task_terminal_outbox SET delivery_state = ?, updated_at = ?, delivered_at = ?, last_error = ?, accepted = ?, rejected_reason = ? WHERE dedupe_key = ?',
            ('delivered', delivered_at, delivered_at, '', 1, '', key),
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
        def operation(conn: sqlite3.Connection) -> dict[str, object]:
            row = conn.execute(
                'SELECT dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
                'FROM task_stall_outbox WHERE dedupe_key = ?',
                (key,),
            ).fetchone()
            if row is None:
                payload_json = json.dumps(payload, ensure_ascii=False)
                conn.execute(
                    'INSERT INTO task_stall_outbox (dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (key, task_id, session_id, 'pending', created_at, created_at, '', 0, '', '', payload_json),
                )
                row = conn.execute(
                    'SELECT dedupe_key, task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
                    'FROM task_stall_outbox WHERE dedupe_key = ?',
                    (key,),
                ).fetchone()
            return self._task_stall_outbox_row(row)
        return self._run_write(operation)

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
        def operation(conn: sqlite3.Connection) -> None:
            row = conn.execute('SELECT attempts, delivery_state FROM task_stall_outbox WHERE dedupe_key = ?', (key,)).fetchone()
            if row is None:
                return
            delivery_state = str(row['delivery_state'] or '').strip() or 'pending'
            if delivery_state == 'delivered':
                return
            attempts = int(row['attempts'] or 0) + 1
            conn.execute(
                'UPDATE task_stall_outbox SET delivery_state = ?, updated_at = ?, attempts = ?, last_attempt_at = ?, last_error = ? WHERE dedupe_key = ?',
                ('pending', attempted_at, attempts, attempted_at, str(error_text or ''), key),
            )
        self._run_write(operation)

    def mark_task_stall_outbox_delivered(self, dedupe_key: str, *, delivered_at: str) -> None:
        key = str(dedupe_key or '').strip()
        if not key:
            return
        self._execute_write(
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
        def operation(conn: sqlite3.Connection) -> dict[str, object]:
            row = conn.execute(
                'SELECT worker_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
                'FROM task_worker_status_outbox WHERE worker_id = ?',
                (key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    'INSERT INTO task_worker_status_outbox (worker_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (key, 'pending', created_at, created_at, '', 0, '', '', payload_json),
                )
            else:
                existing_created_at = str(row['created_at'] or created_at).strip() or created_at
                conn.execute(
                    'UPDATE task_worker_status_outbox SET delivery_state = ?, created_at = ?, updated_at = ?, delivered_at = ?, attempts = ?, last_attempt_at = ?, last_error = ?, payload_json = ? '
                    'WHERE worker_id = ?',
                    ('pending', existing_created_at, created_at, '', 0, '', '', payload_json, key),
                )
            row = conn.execute(
                'SELECT worker_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, payload_json '
                'FROM task_worker_status_outbox WHERE worker_id = ?',
                (key,),
            ).fetchone()
            return self._task_worker_status_outbox_row(row)
        return self._run_write(operation)

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
        def operation(conn: sqlite3.Connection) -> None:
            row = conn.execute('SELECT attempts, delivery_state FROM task_worker_status_outbox WHERE worker_id = ?', (key,)).fetchone()
            if row is None:
                return
            delivery_state = str(row['delivery_state'] or '').strip() or 'pending'
            if delivery_state == 'delivered':
                return
            attempts = int(row['attempts'] or 0) + 1
            conn.execute(
                'UPDATE task_worker_status_outbox SET delivery_state = ?, attempts = ?, last_attempt_at = ?, last_error = ? WHERE worker_id = ?',
                ('pending', attempts, attempted_at, str(error_text or ''), key),
            )
        self._run_write(operation)

    def mark_task_worker_status_outbox_delivered(self, worker_id: str, *, delivered_at: str) -> None:
        key = str(worker_id or '').strip()
        if not key:
            return
        self._execute_write(
            'UPDATE task_worker_status_outbox SET delivery_state = ?, delivered_at = ?, last_error = ? WHERE worker_id = ?',
            ('delivered', delivered_at, '', key),
        )

    def put_task_summary_outbox(
        self,
        *,
        task_id: str,
        session_id: str,
        created_at: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        key = str(task_id or '').strip()
        if not key:
            raise ValueError('task_id_required')
        normalized_session_id = str(session_id or 'web:shared').strip() or 'web:shared'
        payload_json = json.dumps(payload, ensure_ascii=False)

        def operation(conn: sqlite3.Connection) -> dict[str, object]:
            row = conn.execute(
                'SELECT task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, version, payload_json '
                'FROM task_summary_outbox WHERE task_id = ?',
                (key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    'INSERT INTO task_summary_outbox (task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, version, payload_json) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (key, normalized_session_id, 'pending', created_at, created_at, '', 0, '', '', 1, payload_json),
                )
            else:
                existing_created_at = str(row['created_at'] or created_at).strip() or created_at
                next_version = max(1, int(row['version'] or 0) + 1)
                conn.execute(
                    'UPDATE task_summary_outbox SET session_id = ?, delivery_state = ?, created_at = ?, updated_at = ?, delivered_at = ?, attempts = ?, last_attempt_at = ?, last_error = ?, version = ?, payload_json = ? '
                    'WHERE task_id = ?',
                    (normalized_session_id, 'pending', existing_created_at, created_at, '', 0, '', '', next_version, payload_json, key),
                )
            row = conn.execute(
                'SELECT task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, version, payload_json '
                'FROM task_summary_outbox WHERE task_id = ?',
                (key,),
            ).fetchone()
            return self._task_summary_outbox_row(row)

        return self._run_write(operation)

    def get_task_summary_outbox(self, task_id: str) -> dict[str, object] | None:
        row = self._fetchone(
            'SELECT task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, version, payload_json '
            'FROM task_summary_outbox WHERE task_id = ?',
            (str(task_id or '').strip(),),
        )
        return self._task_summary_outbox_row(row) if row else None

    def list_pending_task_summary_outbox(self, *, limit: int = 200) -> list[dict[str, object]]:
        rows = self._fetchall(
            'SELECT task_id, session_id, delivery_state, created_at, updated_at, delivered_at, attempts, last_attempt_at, last_error, version, payload_json '
            'FROM task_summary_outbox WHERE delivery_state != ? ORDER BY updated_at ASC LIMIT ?',
            ('delivered', max(1, int(limit or 200))),
        )
        return [self._task_summary_outbox_row(row) for row in rows]

    def mark_task_summary_outbox_attempt(
        self,
        task_id: str,
        *,
        attempted_at: str,
        error_text: str,
        expected_version: int | None = None,
    ) -> bool:
        key = str(task_id or '').strip()
        if not key:
            return False

        def operation(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                'SELECT attempts, delivery_state, version FROM task_summary_outbox WHERE task_id = ?',
                (key,),
            ).fetchone()
            if row is None:
                return False
            delivery_state = str(row['delivery_state'] or '').strip() or 'pending'
            if delivery_state == 'delivered':
                return False
            current_version = max(1, int(row['version'] or 0))
            if expected_version is not None and current_version != int(expected_version):
                return False
            attempts = int(row['attempts'] or 0) + 1
            conn.execute(
                'UPDATE task_summary_outbox SET delivery_state = ?, updated_at = ?, attempts = ?, last_attempt_at = ?, last_error = ? WHERE task_id = ? AND version = ?',
                ('pending', attempted_at, attempts, attempted_at, str(error_text or ''), key, current_version),
            )
            return True

        return bool(self._run_write(operation))

    def mark_task_summary_outbox_delivered(
        self,
        task_id: str,
        *,
        delivered_at: str,
        expected_version: int | None = None,
    ) -> bool:
        key = str(task_id or '').strip()
        if not key:
            return False

        def operation(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                'SELECT delivery_state, version FROM task_summary_outbox WHERE task_id = ?',
                (key,),
            ).fetchone()
            if row is None:
                return False
            current_version = max(1, int(row['version'] or 0))
            if expected_version is not None and current_version != int(expected_version):
                return False
            conn.execute(
                'UPDATE task_summary_outbox SET delivery_state = ?, updated_at = ?, delivered_at = ?, last_error = ? WHERE task_id = ? AND version = ?',
                ('delivered', delivered_at, delivered_at, '', key, current_version),
            )
            return True

        return bool(self._run_write(operation))

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
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute('DELETE FROM task_nodes WHERE task_id = ?', (task_id,))
            for record in records:
                conn.execute(
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
        self._run_write(operation)

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
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute('DELETE FROM task_node_details WHERE task_id = ?', (task_id,))
            for record in records:
                conn.execute(
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
        self._run_write(operation)

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
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute('DELETE FROM task_runtime_frames WHERE task_id = ?', (task_id,))
            for record in records:
                conn.execute(
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
        self._run_write(operation)

    def upsert_task_runtime_frame(self, record: TaskProjectionRuntimeFrameRecord) -> TaskProjectionRuntimeFrameRecord:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
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
        self._run_write(operation)
        return record

    def upsert_task_node_tool_result(self, record: TaskProjectionToolResultRecord) -> TaskProjectionToolResultRecord:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                'INSERT INTO task_node_tool_results (task_id, node_id, tool_call_id, order_index, tool_name, arguments_text, status, started_at, finished_at, elapsed_seconds, output_preview_text, output_ref, ephemeral, payload_json) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(task_id, node_id, tool_call_id) DO UPDATE SET '
                'order_index=excluded.order_index, '
                'tool_name=excluded.tool_name, '
                'arguments_text=excluded.arguments_text, '
                'status=excluded.status, '
                'started_at=excluded.started_at, '
                'finished_at=excluded.finished_at, '
                'elapsed_seconds=excluded.elapsed_seconds, '
                'output_preview_text=excluded.output_preview_text, '
                'output_ref=excluded.output_ref, '
                'ephemeral=excluded.ephemeral, '
                'payload_json=excluded.payload_json',
                (
                    record.task_id,
                    record.node_id,
                    record.tool_call_id,
                    int(record.order_index or 0),
                    record.tool_name,
                    record.arguments_text,
                    record.status,
                    record.started_at,
                    record.finished_at,
                    record.elapsed_seconds,
                    record.output_preview_text,
                    record.output_ref,
                    1 if record.ephemeral else 0,
                    record.model_dump_json(),
                ),
            )

        self._run_write(operation)
        return record

    def list_task_node_tool_results(self, task_id: str, node_id: str) -> list[TaskProjectionToolResultRecord]:
        rows = self._fetchall(
            'SELECT payload_json FROM task_node_tool_results WHERE task_id = ? AND node_id = ? ORDER BY order_index ASC, tool_call_id ASC',
            (task_id, node_id),
        )
        return [self._parse(row['payload_json'], TaskProjectionToolResultRecord) for row in rows]

    def get_task_runtime_frame(self, task_id: str, node_id: str) -> TaskProjectionRuntimeFrameRecord | None:
        row = self._fetchone(
            'SELECT payload_json FROM task_runtime_frames WHERE task_id = ? AND node_id = ?',
            (task_id, node_id),
        )
        return self._parse(row['payload_json'], TaskProjectionRuntimeFrameRecord) if row else None

    def delete_task_runtime_frame(self, task_id: str, node_id: str) -> None:
        self._execute_write(
            'DELETE FROM task_runtime_frames WHERE task_id = ? AND node_id = ?',
            (str(task_id or '').strip(), str(node_id or '').strip()),
        )

    def list_task_runtime_frames(self, task_id: str) -> list[TaskProjectionRuntimeFrameRecord]:
        rows = self._fetchall('SELECT payload_json FROM task_runtime_frames WHERE task_id = ? ORDER BY depth ASC, node_id ASC', (task_id,))
        return [self._parse(row['payload_json'], TaskProjectionRuntimeFrameRecord) for row in rows]

    def replace_task_node_rounds(self, task_id: str, records: list[TaskProjectionRoundRecord]) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute('DELETE FROM task_node_rounds WHERE task_id = ?', (task_id,))
            for record in records:
                conn.execute(
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
        self._run_write(operation)

    def replace_task_node_rounds_for_parent(
        self,
        task_id: str,
        parent_node_id: str,
        records: list[TaskProjectionRoundRecord],
    ) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                'DELETE FROM task_node_rounds WHERE task_id = ? AND parent_node_id = ?',
                (task_id, parent_node_id),
            )
            for record in records:
                conn.execute(
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
        self._run_write(operation)

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
        def operation(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                'INSERT INTO task_model_calls (task_id, node_id, created_at, payload_json) VALUES (?, ?, ?, ?)',
                (task_id, node_id, created_at, payload_json),
            )
            return int(cursor.lastrowid or 0)
        return self._run_write(operation)

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
        self._run_write(lambda conn: self._upsert_conn(conn, table, columns, values, primary_key))

    @staticmethod
    def _upsert_conn(
        conn: sqlite3.Connection,
        table: str,
        columns: list[str],
        values: list[object],
        primary_key: str,
    ) -> None:
        placeholders = ', '.join('?' for _ in columns)
        updates = ', '.join(f"{column}=excluded.{column}" for column in columns if column != primary_key)
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT({primary_key}) DO UPDATE SET {updates}"
        conn.execute(sql, values)

    def _fetchone(self, sql: str, params: tuple[object, ...]) -> sqlite3.Row | None:
        started_mono = time.perf_counter()
        started_at = datetime.now().astimezone().isoformat(timespec='seconds')
        with self._read_lock:
            row = self._read_conn.execute(sql, params).fetchone()
        elapsed_ms = max(0.0, (time.perf_counter() - started_mono) * 1000.0)
        self._update_runtime_metrics(sqlite_query_latency_ms=elapsed_ms)
        recorder = self._debug_recorder
        if recorder is not None and hasattr(recorder, 'record'):
            try:
                recorder.record(section='sqlite.query.fetchone', elapsed_ms=elapsed_ms, started_at=started_at)
            except Exception:
                pass
        return row

    def _fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        started_mono = time.perf_counter()
        started_at = datetime.now().astimezone().isoformat(timespec='seconds')
        with self._read_lock:
            rows = list(self._read_conn.execute(sql, params).fetchall())
        elapsed_ms = max(0.0, (time.perf_counter() - started_mono) * 1000.0)
        self._update_runtime_metrics(sqlite_query_latency_ms=elapsed_ms)
        recorder = self._debug_recorder
        if recorder is not None and hasattr(recorder, 'record'):
            try:
                recorder.record(section='sqlite.query.fetchall', elapsed_ms=elapsed_ms, started_at=started_at)
            except Exception:
                pass
        return rows

    def _update_runtime_metrics(self, **values: float) -> None:
        updated_mono = time.perf_counter()
        with self._metrics_lock:
            for key, value in values.items():
                self._runtime_metrics[str(key)] = max(0.0, float(value or 0.0))
            self._runtime_metrics['runtime_metrics_updated_mono'] = updated_mono

    @staticmethod
    def _task_terminal_outbox_row(row: sqlite3.Row | None) -> dict[str, object]:
        if row is None:
            return {}
        payload = json.loads(row['payload_json'])
        accepted_raw = row['accepted']
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
            'accepted': None if accepted_raw is None else bool(int(accepted_raw)),
            'rejected_reason': row['rejected_reason'],
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
    def _task_command_row(row: sqlite3.Row | None) -> dict[str, object]:
        if row is None:
            return {}
        payload = json.loads(row['payload_json'])
        raw_result = str(row['result_json'] or '').strip()
        result = json.loads(raw_result) if raw_result else {}
        return {
            'command_id': row['command_id'],
            'task_id': row['task_id'],
            'session_id': row['session_id'],
            'command_type': row['command_type'],
            'status': row['status'],
            'created_at': row['created_at'],
            'claimed_at': row['claimed_at'],
            'finished_at': row['finished_at'],
            'worker_id': row['worker_id'],
            'error_text': row['error_text'],
            'payload': payload if isinstance(payload, dict) else {},
            'result': result if isinstance(result, dict) else {},
        }

    @staticmethod
    def _worker_lease_row(row: sqlite3.Row | None) -> dict[str, object]:
        if row is None:
            return {}
        payload = json.loads(row['payload_json'])
        return {
            'role': row['role'],
            'worker_id': row['worker_id'],
            'holder_pid': int(row['holder_pid'] or 0),
            'acquired_at': row['acquired_at'],
            'heartbeat_at': row['heartbeat_at'],
            'expires_at': row['expires_at'],
            'payload': payload if isinstance(payload, dict) else {},
        }

    @staticmethod
    def _task_summary_outbox_row(row: sqlite3.Row | None) -> dict[str, object]:
        if row is None:
            return {}
        payload = json.loads(row['payload_json'])
        return {
            'task_id': row['task_id'],
            'session_id': row['session_id'],
            'delivery_state': row['delivery_state'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
            'delivered_at': row['delivered_at'],
            'attempts': int(row['attempts'] or 0),
            'last_attempt_at': row['last_attempt_at'],
            'last_error': row['last_error'],
            'version': max(1, int(row['version'] or 0)),
            'payload': payload if isinstance(payload, dict) else {},
        }

    @staticmethod
    def _parse(payload_json: str, model_cls: type[T]) -> T:
        return model_cls.model_validate(json.loads(payload_json))
