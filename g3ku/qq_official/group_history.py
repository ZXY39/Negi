"""Per-group inbound history for the official QQ adapter.

Non-@ group messages do not create turns. They are appended here as compact
JSONL plus media files; a digest heartbeat later points the model at the
artifact. The store is deliberately boring: one JSONL file, one media dir, one
state file, and an atomic rewrite for retention/size cleanup.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

HISTORY_RETENTION_DAYS = 30
CONSUMED_RETENTION_DAYS = 7
DEFAULT_HISTORY_MAX_GB = 2.0
DEFAULT_SCAN_MIN_MINUTES = 2
DEFAULT_SCAN_MAX_MINUTES = 5
_MEDIA_DIR = "media"
_MESSAGES_FILE = "messages.jsonl"
_STATE_FILE = "state.json"
# 同一群的 bridge 写入与 heartbeat 注入消费是不同实例；共享一把进程内锁，
# 避免 state.json 的读改写互相覆盖。
_STORE_LOCK = threading.RLock()


def group_history_root(session_key: str, *, create: bool = False) -> Path:
    from g3ku.runtime.frontdoor.session_workspace import ceo_session_workspace_dir

    root = Path(ceo_session_workspace_dir(session_key, create=create)) / "group-inbox"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def digest_artifact_task_id(session_key: str) -> str:
    return f"channel-digest:{str(session_key or '').strip()}"


def _safe_name(value: Any, fallback: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in str(value or "")).strip()
    text = text.strip(" .")[:120]
    return text or fallback


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@dataclass(slots=True)
class DigestStats:
    message_count: int = 0
    file_count: int = 0
    participant_count: int = 0
    has_master: bool = False
    window_start: str = ""
    window_end: str = ""
    n_start: int = 0
    n_end: int = 0


class GroupHistoryStore:
    def __init__(
        self,
        *,
        root: Path,
        app_id: str = "",
        external_key: str = "",
        session_key: str = "",
        max_bytes: int = int(DEFAULT_HISTORY_MAX_GB * 1024**3),
        retention_days: int = HISTORY_RETENTION_DAYS,
        consumed_retention_days: int = CONSUMED_RETENTION_DAYS,
    ) -> None:
        self.root = Path(root)
        self.app_id = str(app_id or "")
        self.external_key = str(external_key or "")
        self.session_key = str(session_key or "")
        self.max_bytes = max(1, int(max_bytes))
        self.retention_days = max(1, int(retention_days))
        self.consumed_retention_days = max(1, int(consumed_retention_days))
        self._lock = _STORE_LOCK

    @property
    def messages_path(self) -> Path:
        return self.root / _MESSAGES_FILE

    @property
    def media_dir(self) -> Path:
        return self.root / _MEDIA_DIR

    @property
    def state_path(self) -> Path:
        return self.root / _STATE_FILE

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self._write_state(self._default_state())

    def _default_state(self) -> dict[str, Any]:
        return {
            "next_n": 1,
            "last_consumed_n": 0,
            "usage_bytes": 0,
            "consumptions": [],
            "app_id": self.app_id,
            "external_key": self.external_key,
            "session_key": self.session_key,
        }

    def _read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        state = self._default_state()
        if isinstance(payload, dict):
            state.update(payload)
        try:
            state["next_n"] = max(1, int(state.get("next_n") or 1))
            state["last_consumed_n"] = max(0, int(state.get("last_consumed_n") or 0))
        except (TypeError, ValueError):
            state["next_n"] = 1
            state["last_consumed_n"] = 0
        if not isinstance(state.get("consumptions"), list):
            state["consumptions"] = []
        try:
            state["usage_bytes"] = max(0, int(state.get("usage_bytes") or 0))
        except (TypeError, ValueError):
            state["usage_bytes"] = 0
        state["app_id"] = self.app_id or str(state.get("app_id") or "")
        state["external_key"] = self.external_key or str(state.get("external_key") or "")
        state["session_key"] = self.session_key or str(state.get("session_key") or "")
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def append(
        self,
        *,
        text: str,
        sender_id: str,
        sender_name: str,
        role: str,
        is_master: bool,
        files: Iterable[dict[str, Any]] | None = None,
        message_id: str = "",
    ) -> int | None:
        payloads: list[dict[str, Any]] = []
        for index, item in enumerate(files or []):
            data = item.get("data") if isinstance(item, dict) else None
            if not isinstance(data, (bytes, bytearray)) or not data:
                continue
            name = _safe_name(item.get("name"), f"file-{index + 1}")
            descriptor = {
                "kind": str(item.get("kind") or "file"),
                "name": name,
                "mime": str(item.get("mime") or "application/octet-stream"),
                "size": len(data),
            }
            payloads.append({**descriptor, "_data": bytes(data)})

        with self._lock:
            self.ensure()
            state = self._read_state()
            n = int(state["next_n"])
            stored_files: list[dict[str, Any]] = []
            for item in payloads:
                safe = _safe_name(item["name"], "file")
                path = self.media_dir / f"{n}_{safe}"
                try:
                    path.write_bytes(item["_data"])
                except OSError as exc:
                    logger.warning("qq-official group history media write failed: {}", exc)
                    continue
                stored_files.append(
                    {
                        "kind": item["kind"],
                        "name": item["name"],
                        "mime": item["mime"],
                        "size": item["size"],
                        "path": str(path.resolve()),
                    }
                )
            if not text.strip() and not stored_files:
                return None
            line = {
                "n": n,
                "ts": _iso_now(),
                "sender_id": str(sender_id or ""),
                "sender_name": str(sender_name or ""),
                "role": str(role or ""),
                "master": bool(is_master),
                "text": str(text or ""),
                "files": stored_files,
            }
            if message_id:
                line["mid"] = str(message_id)
            try:
                with self.messages_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
            except OSError as exc:
                logger.warning("qq-official group history append failed: {}", exc)
                return None
            state["next_n"] = n + 1
            line_bytes = len(
                json.dumps(line, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ) + 1
            state["usage_bytes"] = int(state.get("usage_bytes") or 0) + line_bytes + sum(
                int(item.get("size") or 0) for item in stored_files
            )
            self._write_state(state)
            if int(state["usage_bytes"]) > self.max_bytes:
                self.enforce_limits()
            return n

    def read_messages(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            with self.messages_path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict) and int(item.get("n") or 0) > 0:
                        rows.append(item)
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("qq-official group history read failed: {}", exc)
        rows.sort(key=lambda item: int(item.get("n") or 0))
        return rows

    def build_digest(self) -> tuple[list[dict[str, Any]], DigestStats]:
        state = self._read_state()
        last = int(state.get("last_consumed_n") or 0)
        rows = [item for item in self.read_messages() if int(item.get("n") or 0) > last]
        stats = self._stats(rows)
        return rows, stats

    def last_consumed_n(self) -> int:
        return int(self._read_state().get("last_consumed_n") or 0)

    def _stats(self, rows: list[dict[str, Any]]) -> DigestStats:
        if not rows:
            return DigestStats()
        senders: set[str] = set()
        files = 0
        has_master = False
        for item in rows:
            senders.add(str(item.get("sender_id") or ""))
            files += len(item.get("files") or [])
            has_master = has_master or bool(item.get("master"))
        times = [str(item.get("ts") or "") for item in rows if item.get("ts")]
        return DigestStats(
            message_count=len(rows),
            file_count=files,
            participant_count=len([item for item in senders if item]),
            has_master=has_master,
            window_start=min(times) if times else "",
            window_end=max(times) if times else "",
            n_start=int(rows[0].get("n") or 0),
            n_end=int(rows[-1].get("n") or 0),
        )

    def mark_consumed(self, n_end: int) -> None:
        if int(n_end or 0) <= 0:
            return
        with self._lock:
            state = self._read_state()
            state["last_consumed_n"] = max(int(state.get("last_consumed_n") or 0), int(n_end))
            marks = list(state.get("consumptions") or [])
            marks.append({"n_end": int(n_end), "at": _iso_now()})
            state["consumptions"] = marks[-200:]
            self._write_state(state)

    def usage_bytes(self) -> int:
        total = 0
        for path in self.root.rglob("*") if self.root.exists() else []:
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def _expiry_for(self, item: dict[str, Any], consumptions: list[dict[str, Any]]) -> datetime | None:
        arrival = _parse_dt(item.get("ts"))
        if arrival is None:
            arrival = datetime.now().astimezone()
        consumed_at = None
        n = int(item.get("n") or 0)
        for mark in consumptions:
            try:
                if int(mark.get("n_end") or 0) >= n:
                    consumed_at = _parse_dt(mark.get("at")) or consumed_at
            except (TypeError, ValueError):
                continue
        base = consumed_at if consumed_at is not None else arrival
        days = self.consumed_retention_days if consumed_at is not None else self.retention_days
        return base + timedelta(days=days)

    def enforce_limits(self) -> None:
        with self._lock:
            state = self._read_state()
            rows = self.read_messages()
            if not rows:
                return
            consumptions = [item for item in state.get("consumptions") or [] if isinstance(item, dict)]
            now = datetime.now().astimezone()
            kept: list[dict[str, Any]] = []
            removed: list[dict[str, Any]] = []
            for item in rows:
                expiry = self._expiry_for(item, consumptions)
                if expiry is not None and expiry <= now:
                    removed.append(item)
                else:
                    kept.append(item)
            if not removed:
                total = self.usage_bytes()
                state["usage_bytes"] = total
                if total <= self.max_bytes:
                    self._write_state(state)
                    return
                # 容量淘汰：从最旧开始，直到估算可用空间回落到上限内。
                freed = 0
                while kept and total - freed > self.max_bytes:
                    item = kept.pop(0)
                    removed.append(item)
                    freed += self._message_bytes(item)
            self._rewrite(kept, removed, state)

    @staticmethod
    def _message_bytes(item: dict[str, Any]) -> int:
        size = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 1
        for file_item in item.get("files") or []:
            try:
                size += int(file_item.get("size") or 0)
            except (TypeError, ValueError):
                pass
        return size

    def _rewrite(self, kept: list[dict[str, Any]], removed: list[dict[str, Any]], state: dict[str, Any]) -> None:
        for item in removed:
            for file_item in item.get("files") or []:
                raw = str(file_item.get("path") or "").strip()
                if not raw:
                    continue
                try:
                    Path(raw).unlink(missing_ok=True)
                except OSError:
                    pass
        tmp = self.messages_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for item in kept:
                handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(tmp, self.messages_path)
        state["last_consumed_n"] = max(
            int(state.get("last_consumed_n") or 0),
            max((int(item.get("n") or 0) for item in removed), default=0),
        )
        state["usage_bytes"] = self.usage_bytes()
        self._write_state(state)
        if removed:
            logger.info(
                "qq-official group history cleanup: removed {} message(s), {} kept, {} bytes max",
                len(removed), len(kept), self.max_bytes,
            )

    def clear(self) -> int:
        with self._lock:
            removed_bytes = self.usage_bytes()
            state = self._read_state()
            for path in list(self.root.rglob("*")):
                if path.is_file() and path.name not in {_STATE_FILE}:
                    try:
                        path.unlink()
                    except OSError:
                        pass
            for path in sorted(self.root.rglob("*"), reverse=True):
                if path.is_dir() and path != self.root:
                    try:
                        path.rmdir()
                    except OSError:
                        pass
            state["last_consumed_n"] = max(0, int(state.get("next_n") or 1) - 1)
            state["consumptions"] = []
            state["usage_bytes"] = self.usage_bytes()
            self._write_state(state)
            self.write_meta()
            return removed_bytes

    def write_meta(self) -> None:
        self.ensure()
        state = self._read_state()
        meta = self.root / "meta.json"
        payload = {
            "app_id": self.app_id or str(state.get("app_id") or ""),
            "external_key": self.external_key or str(state.get("external_key") or ""),
            "session_key": self.session_key or str(state.get("session_key") or ""),
        }
        try:
            meta.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


def list_group_histories(app_id: str) -> list[tuple[str, Path]]:
    """Return ``(session_key, group_inbox_root)`` for one account's persisted groups."""
    app = str(app_id or "").strip()
    if not app:
        return []
    try:
        from g3ku.deployment.data_root import data_work_path

        root = data_work_path("session-workspaces")
    except Exception:
        return []
    if not root.exists():
        return []
    found: list[tuple[str, Path]] = []
    for meta_path in root.glob("*/group-inbox/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(meta.get("app_id") or "").strip() != app:
            continue
        session_key = str(meta.get("session_key") or "").strip()
        if session_key:
            found.append((session_key, meta_path.parent))
    return found


def account_history_usage(app_id: str) -> int:
    """Sum group-inbox bytes for one QQ account (best effort, UI read path)."""
    app = str(app_id or "").strip()
    if not app:
        return 0
    try:
        from g3ku.deployment.data_root import data_work_path

        root = data_work_path("session-workspaces")
    except Exception:
        return 0
    if not root.exists():
        return 0
    total = 0
    for meta_path in root.glob("*/group-inbox/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(meta.get("app_id") or "").strip() != app:
            continue
        for path in meta_path.parent.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    return total


__all__ = [
    "CONSUMED_RETENTION_DAYS",
    "DEFAULT_HISTORY_MAX_GB",
    "DEFAULT_SCAN_MAX_MINUTES",
    "DEFAULT_SCAN_MIN_MINUTES",
    "HISTORY_RETENTION_DAYS",
    "DigestStats",
    "GroupHistoryStore",
    "account_history_usage",
    "digest_artifact_task_id",
    "group_history_root",
    "list_group_histories",
]
