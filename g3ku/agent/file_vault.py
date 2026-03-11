from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from g3ku.utils.helpers import ensure_dir, resolve_path_in_workspace, safe_filename

_PLACEHOLDER_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_PLACEHOLDER_RANDOM_LEN = 16
PLACEHOLDER_PATTERN = re.compile(r"\b\d{6}-[A-Za-z0-9]{16}-[^\s\]]+\b")
_NATIVE_INPUT_MAX_BYTES = 8 * 1024 * 1024
_NATIVE_FILE_MIMES = {"application/pdf"}


class FileVault:
    """Workspace-scoped uploaded file vault with placeholder indexing and cleanup."""

    def __init__(
        self,
        *,
        workspace: Path,
        enabled: bool = True,
        root_dir: str = "memory/uploads",
        index_db_path: str = "memory/file_vault.db",
        max_storage_bytes: int = 4 * 1024 * 1024 * 1024,
        threshold_pct: int = 70,
        cleanup_target_pct: int = 55,
        recent_protect_hours: int = 24,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.enabled = bool(enabled)
        self.root_dir = resolve_path_in_workspace(root_dir, self.workspace)
        self.index_db_path = resolve_path_in_workspace(index_db_path, self.workspace)
        self.default_max_storage_bytes = max(1, int(max_storage_bytes or 1))
        self.default_threshold_pct = int(threshold_pct or 70)
        self.cleanup_target_pct = int(cleanup_target_pct or 55)
        self.recent_protect_hours = max(0, int(recent_protect_hours or 0))

        ensure_dir(self.root_dir)
        ensure_dir(self.index_db_path.parent)
        self._init_db()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().isoformat()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.index_db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS file_vault_items (
                    placeholder TEXT PRIMARY KEY,
                    rel_path TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    yyyymm TEXT NOT NULL,
                    mime TEXT,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    channel TEXT,
                    chat_id TEXT,
                    session_key TEXT,
                    context_snippet TEXT,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_accessed_at TEXT NOT NULL,
                    deleted_at TEXT,
                    pinned INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS file_vault_policy (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    max_storage_bytes INTEGER NOT NULL,
                    threshold_pct INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO file_vault_policy (id, max_storage_bytes, threshold_pct, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (self.default_max_storage_bytes, self.default_threshold_pct, self._now_iso()),
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fv_session ON file_vault_items(session_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fv_usage ON file_vault_items(use_count, last_accessed_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fv_deleted ON file_vault_items(deleted_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fv_name ON file_vault_items(original_name)"
            )
            conn.commit()

    def _policy(self, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        if conn is None:
            with self._connect() as own:
                return self._policy(conn=own)

        row = conn.execute(
            "SELECT max_storage_bytes, threshold_pct, updated_at FROM file_vault_policy WHERE id=1"
        ).fetchone()
        if row is None:
            return {
                "max_storage_bytes": self.default_max_storage_bytes,
                "threshold_pct": self.default_threshold_pct,
                "updated_at": self._now_iso(),
            }
        return {
            "max_storage_bytes": int(row["max_storage_bytes"]),
            "threshold_pct": int(row["threshold_pct"]),
            "updated_at": str(row["updated_at"]),
        }

    def _to_rel_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.workspace))
        except Exception:
            return str(path.resolve())

    def _to_abs_path(self, rel_path: str) -> Path:
        p = Path(rel_path)
        if p.is_absolute():
            return p
        return (self.workspace / p).resolve()

    def _normalize_name(self, original_name: str | None, *, default_suffix: str = ".bin") -> str:
        base = Path(str(original_name or "file")).name
        safe = safe_filename(base) or f"file{default_suffix}"
        if "." not in safe and default_suffix:
            safe = f"{safe}{default_suffix}"
        return safe

    @staticmethod
    def _random_token(length: int = _PLACEHOLDER_RANDOM_LEN) -> str:
        return "".join(secrets.choice(_PLACEHOLDER_CHARS) for _ in range(length))

    @staticmethod
    def _sha256(content: bytes) -> str:
        h = hashlib.sha256()
        h.update(content)
        return h.hexdigest()

    @staticmethod
    def _normalize_context_snippet(snippet: str | None, *, limit: int = 800) -> str:
        text = " ".join((snippet or "").split())
        return text[:limit]

    def ingest_bytes(
        self,
        *,
        content: bytes,
        original_name: str,
        mime: str | None,
        channel: str | None,
        chat_id: str | None,
        session_key: str | None,
        context_snippet: str | None = None,
    ) -> dict[str, Any]:
        payload = bytes(content or b"")
        now = self._now_iso()
        yyyymm = datetime.now().strftime("%Y%m")
        safe_name = self._normalize_name(original_name, default_suffix=Path(original_name or "file").suffix or ".bin")
        month_dir = ensure_dir(self.root_dir / yyyymm)

        while True:
            token = self._random_token()
            stored_name = f"{token}-{safe_name}"
            placeholder = f"{yyyymm}-{stored_name}"
            abs_path = (month_dir / stored_name).resolve()
            if not abs_path.exists():
                break

        abs_path.write_bytes(payload)
        rel_path = self._to_rel_path(abs_path)
        sha256 = self._sha256(payload)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO file_vault_items (
                    placeholder, rel_path, original_name, stored_name, yyyymm, mime, size_bytes, sha256,
                    channel, chat_id, session_key, context_snippet, use_count,
                    created_at, last_accessed_at, deleted_at, pinned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
                """,
                (
                    placeholder,
                    rel_path,
                    original_name,
                    stored_name,
                    yyyymm,
                    str(mime or ""),
                    len(payload),
                    sha256,
                    str(channel or ""),
                    str(chat_id or ""),
                    str(session_key or ""),
                    self._normalize_context_snippet(context_snippet),
                    0,
                    now,
                    now,
                ),
            )
            conn.commit()

        self.cleanup(dry_run=False)

        return {
            "placeholder": placeholder,
            "rel_path": rel_path,
            "abs_path": str(abs_path),
            "original_name": original_name,
            "stored_name": stored_name,
            "yyyymm": yyyymm,
            "mime": str(mime or ""),
            "size_bytes": len(payload),
            "sha256": sha256,
            "session_key": str(session_key or ""),
            "context_snippet": self._normalize_context_snippet(context_snippet),
            "created_at": now,
        }

    def resolve_placeholder(self, placeholder: str, *, touch: bool = True) -> dict[str, Any] | None:
        key = str(placeholder or "").strip()
        if not key:
            return None

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM file_vault_items
                WHERE placeholder=? AND deleted_at IS NULL
                """,
                (key,),
            ).fetchone()
            if row is None:
                return None

            abs_path = self._to_abs_path(str(row["rel_path"]))
            if not abs_path.exists():
                conn.execute(
                    "UPDATE file_vault_items SET deleted_at=? WHERE placeholder=?",
                    (self._now_iso(), key),
                )
                conn.commit()
                return None

            if touch:
                conn.execute(
                    """
                    UPDATE file_vault_items
                    SET use_count = use_count + 1,
                        last_accessed_at = ?
                    WHERE placeholder = ?
                    """,
                    (self._now_iso(), key),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM file_vault_items WHERE placeholder=?", (key,)).fetchone()

            return {
                "placeholder": str(row["placeholder"]),
                "rel_path": str(row["rel_path"]),
                "abs_path": str(abs_path),
                "original_name": str(row["original_name"]),
                "stored_name": str(row["stored_name"]),
                "yyyymm": str(row["yyyymm"]),
                "mime": str(row["mime"] or ""),
                "size_bytes": int(row["size_bytes"]),
                "sha256": str(row["sha256"]),
                "channel": str(row["channel"] or ""),
                "chat_id": str(row["chat_id"] or ""),
                "session_key": str(row["session_key"] or ""),
                "context_snippet": str(row["context_snippet"] or ""),
                "use_count": int(row["use_count"]),
                "created_at": str(row["created_at"]),
                "last_accessed_at": str(row["last_accessed_at"]),
                "deleted_at": str(row["deleted_at"] or ""),
                "pinned": int(row["pinned"] or 0),
            }

    @staticmethod
    def _parse_ts(value: str | None) -> datetime:
        raw = str(value or "").strip()
        if not raw:
            return datetime.fromtimestamp(0)
        try:
            return datetime.fromisoformat(raw)
        except Exception:
            return datetime.fromtimestamp(0)

    def lookup(self, *, query: str, session_key: str | None, limit: int = 5) -> list[dict[str, Any]]:
        raw_query = str(query or "").strip()
        if not raw_query:
            return []
        q_lower = raw_query.lower()
        placeholder_hits = set(PLACEHOLDER_PATTERN.findall(raw_query))
        limit = max(1, min(int(limit or 5), 20))

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT placeholder, original_name, mime, size_bytes, session_key,
                       context_snippet, use_count, last_accessed_at, created_at
                FROM file_vault_items
                WHERE deleted_at IS NULL
                """
            ).fetchall()

        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            item = {
                "placeholder": str(row["placeholder"]),
                "original_name": str(row["original_name"]),
                "mime": str(row["mime"] or ""),
                "size_bytes": int(row["size_bytes"]),
                "session_key": str(row["session_key"] or ""),
                "context_snippet": str(row["context_snippet"] or ""),
                "use_count": int(row["use_count"]),
                "last_accessed_at": str(row["last_accessed_at"]),
                "created_at": str(row["created_at"]),
            }
            score = 0.0
            if placeholder_hits and item["placeholder"] in placeholder_hits:
                score += 120.0
            if q_lower in item["placeholder"].lower():
                score += 60.0
            if q_lower in item["original_name"].lower():
                score += 45.0
            if q_lower in item["context_snippet"].lower():
                score += 30.0
            if session_key and item["session_key"] == session_key:
                score += 25.0
            score += min(item["use_count"], 50) * 0.2
            age_bonus = max(0.0, 8.0 - (datetime.now() - self._parse_ts(item["last_accessed_at"])) .days * 0.2)
            score += age_bonus
            if score > 0:
                scored.append((score, item))

        scored.sort(
            key=lambda pair: (
                pair[0],
                pair[1]["use_count"],
                self._parse_ts(pair[1]["last_accessed_at"]),
            ),
            reverse=True,
        )
        return [{**item, "score": round(score, 3)} for score, item in scored[:limit]]

    @staticmethod
    def _looks_text(data: bytes) -> bool:
        if not data:
            return True
        if b"\x00" in data[:4096]:
            return False
        sample = data[:4096]
        printable = sum(1 for b in sample if 32 <= b <= 126 or b in (9, 10, 13) or b >= 0x80)
        return printable / max(len(sample), 1) >= 0.9

    @staticmethod
    def _native_intro_block(*, placeholder: str, original_name: str, mime: str, size_bytes: int) -> dict[str, Any]:
        return {
            "type": "text",
            "text": (
                f"[file_vault_read] placeholder={placeholder} original_name={original_name} "
                f"mime={mime or 'unknown'} size_bytes={size_bytes}"
            ),
        }

    def _native_content_result(
        self,
        *,
        record: dict[str, Any],
        placeholder: str,
        data: bytes,
    ) -> dict[str, Any] | None:
        mime = str(record.get("mime") or "").strip().lower()
        original_name = str(record.get("original_name") or placeholder)
        size_bytes = int(record.get("size_bytes") or len(data))

        if size_bytes > _NATIVE_INPUT_MAX_BYTES:
            return {
                "status": "ok",
                "mode": "binary_meta",
                "placeholder": placeholder,
                "message": "file too large for native multimodal pass-through",
                "meta": {
                    "mime": mime,
                    "size_bytes": size_bytes,
                    "sha256": record.get("sha256"),
                    "original_name": original_name,
                },
            }

        intro = self._native_intro_block(
            placeholder=placeholder,
            original_name=original_name,
            mime=mime,
            size_bytes=size_bytes,
        )
        encoded = base64.b64encode(data).decode("ascii")

        if mime.startswith("image/"):
            return {
                "status": "ok",
                "mode": "native",
                "placeholder": placeholder,
                "original_name": original_name,
                "content": [
                    intro,
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ],
                "size_bytes": size_bytes,
            }

        if mime in _NATIVE_FILE_MIMES:
            return {
                "status": "ok",
                "mode": "native",
                "placeholder": placeholder,
                "original_name": original_name,
                "content": [
                    intro,
                    {
                        "type": "file",
                        "source_type": "base64",
                        "mime_type": mime,
                        "data": encoded,
                        "filename": original_name,
                    },
                ],
                "size_bytes": size_bytes,
            }

        return None

    def read(self, *, placeholder: str, mode: str = "auto", max_chars: int = 12000) -> dict[str, Any]:
        key = str(placeholder or "").strip()
        if not key:
            return {
                "status": "error",
                "reason": "invalid_placeholder",
                "message": "placeholder is required",
            }

        record = self.resolve_placeholder(key, touch=True)
        if record is None:
            return {
                "status": "evicted",
                "placeholder": key,
                "message": "file has been cleaned up, please ask user to re-upload",
            }

        path = Path(record["abs_path"])
        data = path.read_bytes()
        max_chars = max(256, min(int(max_chars or 12000), 200000))
        norm_mode = str(mode or "auto").strip().lower()

        if norm_mode not in {"auto", "text", "binary_meta"}:
            norm_mode = "auto"

        if norm_mode == "binary_meta":
            return {
                "status": "ok",
                "mode": "binary_meta",
                "placeholder": key,
                "meta": {
                    "mime": record.get("mime") or "",
                    "size_bytes": record.get("size_bytes"),
                    "sha256": record.get("sha256"),
                    "original_name": record.get("original_name"),
                },
            }

        if norm_mode == "auto":
            native = self._native_content_result(record=record, placeholder=key, data=data)
            if native is not None:
                return native

        text_content: str | None = None
        if norm_mode == "text" or self._looks_text(data):
            try:
                text_content = data.decode("utf-8", errors="replace")
            except Exception:
                text_content = None

        if text_content is None:
            return {
                "status": "ok",
                "mode": "binary_meta",
                "placeholder": key,
                "meta": {
                    "mime": record.get("mime") or "",
                    "size_bytes": record.get("size_bytes"),
                    "sha256": record.get("sha256"),
                    "original_name": record.get("original_name"),
                },
            }

        truncated = len(text_content) > max_chars
        text_out = text_content[:max_chars]
        return {
            "status": "ok",
            "mode": "text",
            "placeholder": key,
            "original_name": record.get("original_name"),
            "content": text_out,
            "truncated": truncated,
            "size_bytes": record.get("size_bytes"),
        }

    def stats(self, *, top_n: int = 10) -> dict[str, Any]:
        top_n = max(1, min(int(top_n or 10), 50))
        with self._connect() as conn:
            policy = self._policy(conn=conn)
            totals = conn.execute(
                """
                SELECT COUNT(*) AS total_files,
                       COALESCE(SUM(size_bytes), 0) AS total_bytes
                FROM file_vault_items
                WHERE deleted_at IS NULL
                """
            ).fetchone()
            rows = conn.execute(
                """
                SELECT placeholder, original_name, size_bytes, use_count, last_accessed_at
                FROM file_vault_items
                WHERE deleted_at IS NULL
                ORDER BY use_count DESC, last_accessed_at DESC
                LIMIT ?
                """,
                (top_n,),
            ).fetchall()

        total_bytes = int(totals["total_bytes"] if totals else 0)
        max_storage_bytes = int(policy["max_storage_bytes"])
        usage_pct = (float(total_bytes) / float(max_storage_bytes) * 100.0) if max_storage_bytes > 0 else 0.0
        return {
            "total_files": int(totals["total_files"] if totals else 0),
            "total_bytes": total_bytes,
            "max_storage_bytes": max_storage_bytes,
            "threshold_pct": int(policy["threshold_pct"]),
            "cleanup_target_pct": int(self.cleanup_target_pct),
            "recent_protect_hours": int(self.recent_protect_hours),
            "usage_pct": round(usage_pct, 3),
            "top_used": [
                {
                    "placeholder": str(r["placeholder"]),
                    "original_name": str(r["original_name"]),
                    "size_bytes": int(r["size_bytes"]),
                    "use_count": int(r["use_count"]),
                    "last_accessed_at": str(r["last_accessed_at"]),
                }
                for r in rows
            ],
        }

    def set_policy(
        self,
        *,
        max_storage_bytes: int | None = None,
        threshold_pct: int | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            current = self._policy(conn=conn)
            max_value = int(max_storage_bytes) if max_storage_bytes is not None else int(current["max_storage_bytes"])
            threshold_value = int(threshold_pct) if threshold_pct is not None else int(current["threshold_pct"])
            max_value = max(1, max_value)
            threshold_value = min(95, max(10, threshold_value))

            now = self._now_iso()
            conn.execute(
                """
                INSERT INTO file_vault_policy (id, max_storage_bytes, threshold_pct, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    max_storage_bytes=excluded.max_storage_bytes,
                    threshold_pct=excluded.threshold_pct,
                    updated_at=excluded.updated_at
                """,
                (max_value, threshold_value, now),
            )
            conn.commit()

        return {
            "max_storage_bytes": max_value,
            "threshold_pct": threshold_value,
            "updated_at": now,
        }

    def cleanup(self, *, dry_run: bool = False, target_pct: int | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            policy = self._policy(conn=conn)
            max_storage_bytes = int(policy["max_storage_bytes"])
            threshold_pct = int(policy["threshold_pct"])
            threshold_bytes = int(max_storage_bytes * threshold_pct / 100)
            target = int(max_storage_bytes * (target_pct if target_pct is not None else self.cleanup_target_pct) / 100)
            target = max(0, min(target, threshold_bytes))

            row = conn.execute(
                """
                SELECT COALESCE(SUM(size_bytes), 0) AS total_bytes,
                       COUNT(*) AS total_files
                FROM file_vault_items
                WHERE deleted_at IS NULL
                """
            ).fetchone()
            current_bytes = int(row["total_bytes"] if row else 0)
            current_files = int(row["total_files"] if row else 0)

            if current_bytes <= threshold_bytes:
                return {
                    "dry_run": bool(dry_run),
                    "triggered": False,
                    "current_bytes": current_bytes,
                    "threshold_bytes": threshold_bytes,
                    "target_bytes": target,
                    "deleted_count": 0,
                    "deleted_bytes": 0,
                    "candidates": [],
                    "remaining_files": current_files,
                }

            rows = conn.execute(
                """
                SELECT placeholder, rel_path, size_bytes, use_count, last_accessed_at, created_at
                FROM file_vault_items
                WHERE deleted_at IS NULL AND pinned = 0
                ORDER BY use_count ASC, last_accessed_at ASC, created_at ASC, size_bytes DESC
                """
            ).fetchall()

            protect_before = datetime.now() - timedelta(hours=self.recent_protect_hours)
            deleted: list[dict[str, Any]] = []
            deleted_bytes = 0
            remaining = current_bytes
            for row_item in rows:
                if remaining <= target:
                    break
                last_access = self._parse_ts(str(row_item["last_accessed_at"]))
                if last_access >= protect_before:
                    continue
                candidate = {
                    "placeholder": str(row_item["placeholder"]),
                    "rel_path": str(row_item["rel_path"]),
                    "size_bytes": int(row_item["size_bytes"]),
                }
                deleted.append(candidate)
                deleted_bytes += candidate["size_bytes"]
                remaining -= candidate["size_bytes"]

            if not dry_run and deleted:
                now = self._now_iso()
                for item in deleted:
                    path = self._to_abs_path(item["rel_path"])
                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    conn.execute(
                        "UPDATE file_vault_items SET deleted_at=? WHERE placeholder=?",
                        (now, item["placeholder"]),
                    )
                conn.commit()

            return {
                "dry_run": bool(dry_run),
                "triggered": True,
                "current_bytes": current_bytes,
                "threshold_bytes": threshold_bytes,
                "target_bytes": target,
                "deleted_count": len(deleted),
                "deleted_bytes": deleted_bytes,
                "candidates": deleted,
                "remaining_bytes": remaining,
                "remaining_files": max(0, current_files - (0 if dry_run else len(deleted))),
            }

    def serialize(self) -> str:
        return json.dumps(self.stats(), ensure_ascii=False)

