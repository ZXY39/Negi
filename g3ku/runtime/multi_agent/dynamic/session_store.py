from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from g3ku.runtime.multi_agent.dynamic.types import DynamicSubagentSessionRecord


class DynamicSubagentSessionStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @staticmethod
    def utcnow() -> str:
        return datetime.now(UTC).isoformat()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dynamic_subagent_sessions (
                    session_id TEXT PRIMARY KEY,
                    parent_session_id TEXT NOT NULL,
                    task_id TEXT,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_mode TEXT NOT NULL,
                    model_chain TEXT NOT NULL,
                    granted_tools TEXT NOT NULL,
                    injected_skills TEXT NOT NULL,
                    system_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_anchor_index INTEGER NOT NULL,
                    last_result_summary TEXT NOT NULL,
                    freeze_expires_at TEXT,
                    destroy_after_accept INTEGER NOT NULL,
                    metadata TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dynamic_subagent_parent ON dynamic_subagent_sessions(parent_session_id)"
            )

    def save(self, record: DynamicSubagentSessionRecord) -> DynamicSubagentSessionRecord:
        payload = record.model_dump(mode="json")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO dynamic_subagent_sessions (
                    session_id, parent_session_id, task_id, category, status, run_mode,
                    model_chain, granted_tools, injected_skills, system_fingerprint,
                    created_at, updated_at, last_anchor_index, last_result_summary,
                    freeze_expires_at, destroy_after_accept, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["session_id"],
                    payload["parent_session_id"],
                    payload.get("task_id"),
                    payload["category"],
                    payload["status"],
                    payload["run_mode"],
                    json.dumps(payload.get("model_chain") or [], ensure_ascii=False),
                    json.dumps(payload.get("granted_tools") or [], ensure_ascii=False),
                    json.dumps(payload.get("injected_skills") or [], ensure_ascii=False),
                    payload["system_fingerprint"],
                    payload["created_at"],
                    payload["updated_at"],
                    int(payload.get("last_anchor_index") or 0),
                    payload.get("last_result_summary") or "",
                    payload.get("freeze_expires_at"),
                    1 if payload.get("destroy_after_accept", True) else 0,
                    json.dumps(payload.get("metadata") or {}, ensure_ascii=False),
                ),
            )
        return record

    def get(self, session_id: str) -> DynamicSubagentSessionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM dynamic_subagent_sessions WHERE session_id = ?",
                (str(session_id or ""),),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_by_parent(self, parent_session_id: str) -> list[DynamicSubagentSessionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM dynamic_subagent_sessions WHERE parent_session_id = ? ORDER BY created_at DESC",
                (str(parent_session_id or ""),),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def update(self, session_id: str, **changes) -> DynamicSubagentSessionRecord | None:
        record = self.get(session_id)
        if record is None:
            return None
        payload = record.model_dump()
        payload.update({key: value for key, value in changes.items() if value is not None})
        payload["updated_at"] = self.utcnow()
        updated = DynamicSubagentSessionRecord.model_validate(payload)
        return self.save(updated)

    def expire_frozen(self, *, ttl_seconds: int) -> int:
        count = 0
        now = datetime.now(UTC)
        for record in self.list_all():
            if record.status != "frozen":
                continue
            if not record.freeze_expires_at:
                continue
            try:
                expires = datetime.fromisoformat(record.freeze_expires_at)
            except Exception:
                continue
            if expires <= now:
                self.update(record.session_id, status="destroyed")
                count += 1
        return count

    def mark_frozen(self, session_id: str, *, ttl_seconds: int) -> DynamicSubagentSessionRecord | None:
        expires_at = (datetime.now(UTC) + timedelta(seconds=max(1, int(ttl_seconds or 1)))).isoformat()
        return self.update(session_id, status="frozen", freeze_expires_at=expires_at)

    def list_all(self) -> list[DynamicSubagentSessionRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM dynamic_subagent_sessions ORDER BY created_at DESC").fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DynamicSubagentSessionRecord:
        return DynamicSubagentSessionRecord.model_validate(
            {
                "session_id": row["session_id"],
                "parent_session_id": row["parent_session_id"],
                "task_id": row["task_id"],
                "category": row["category"],
                "status": row["status"],
                "run_mode": row["run_mode"],
                "model_chain": json.loads(row["model_chain"] or "[]"),
                "granted_tools": json.loads(row["granted_tools"] or "[]"),
                "injected_skills": json.loads(row["injected_skills"] or "[]"),
                "system_fingerprint": row["system_fingerprint"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "last_anchor_index": int(row["last_anchor_index"] or 0),
                "last_result_summary": row["last_result_summary"] or "",
                "freeze_expires_at": row["freeze_expires_at"],
                "destroy_after_accept": bool(row["destroy_after_accept"]),
                "metadata": json.loads(row["metadata"] or "{}"),
            }
        )

