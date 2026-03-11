from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from g3ku.org_graph.models import ProjectEventRecord


class EventStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(project_id, seq)
                )
                '''
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def append(self, record: ProjectEventRecord) -> ProjectEventRecord:
        with self._lock, self._conn:
            seq = int(record.seq or 0)
            if seq <= 0:
                row = self._conn.execute(
                    'SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE project_id = ?',
                    (record.project_id,),
                ).fetchone()
                seq = int(row[0])
            stored = record.model_copy(update={'seq': seq})
            self._conn.execute(
                'INSERT INTO events (event_id, project_id, seq, created_at, payload_json) VALUES (?, ?, ?, ?, ?)',
                (stored.event_id, stored.project_id, stored.seq, stored.created_at, stored.model_dump_json()),
            )
        return stored

    def list_after(self, project_id: str, after_seq: int = 0, limit: int = 200) -> list[ProjectEventRecord]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT payload_json FROM events WHERE project_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?',
                (project_id, int(after_seq), int(limit)),
            ).fetchall()
        return [ProjectEventRecord.model_validate(json.loads(row['payload_json'])) for row in rows]

    def latest_seq(self, project_id: str) -> int:
        with self._lock:
            row = self._conn.execute('SELECT COALESCE(MAX(seq), 0) FROM events WHERE project_id = ?', (project_id,)).fetchone()
        return int(row[0]) if row else 0

    def delete_project(self, project_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM events WHERE project_id = ?', (project_id,))

