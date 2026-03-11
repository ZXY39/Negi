from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from g3ku.org_graph.protocol import now_iso


class CheckpointStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute(
                'CREATE TABLE IF NOT EXISTS checkpoints (unit_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL)'
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def touch(self, unit_id: str, payload: dict[str, Any] | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO checkpoints (unit_id, payload_json, updated_at) VALUES (?, ?, ?) ON CONFLICT(unit_id) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at',
                (unit_id, json.dumps(payload or {}, ensure_ascii=False), now_iso()),
            )

    def get(self, unit_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute('SELECT payload_json FROM checkpoints WHERE unit_id = ?', (unit_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    def update(self, unit_id: str, mutator) -> dict[str, Any]:
        payload = self.get(unit_id)
        updated = mutator(dict(payload or {}))
        self.touch(unit_id, updated)
        return updated

    def delete_many(self, unit_ids: list[str]) -> None:
        if not unit_ids:
            return
        placeholders = ', '.join('?' for _ in unit_ids)
        with self._lock, self._conn:
            self._conn.execute(f'DELETE FROM checkpoints WHERE unit_id IN ({placeholders})', tuple(unit_ids))

