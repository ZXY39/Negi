"""Cross-subsystem audit event sink for the web admin's 日志审计 (log audit) page.

Append-only, best-effort structured event ledger. Events land in
``<workspace>/.g3ku/audit.jsonl`` as one JSON object per line.

Writer semantics: :func:`emit_audit_event` is fire-and-forget — it never
raises, never blocks the host flow, and returns ``False`` when the sink is
unconfigured or the write fails. A sink is configured at runtime bootstrap
(``configure_audit_sink`` in ``g3ku/runtime/bootstrap_factory.py``), which
covers the web process, the CLI, and the managed worker.

Read semantics: ``list_audit_events`` / ``audit_summary`` resolve the
workspace root from the configured sink, falling back to the live runtime
config, and raise ``RuntimeError('audit_sink_unconfigured')`` when neither
resolves. There is deliberately no ``Path.cwd()`` fallback: provider-factory
unit tests run from the repo root and must never touch a real workspace.

Memory errors do NOT enter this sink; they stay in the memory view
(``memory/failed.jsonl``).
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

AUDIT_DIR_NAME = ".g3ku"
AUDIT_FILE_NAME = "audit.jsonl"

# 保留期：文件超过 ~2MB 触发修剪，保留最新 2000 条；读取硬上限 4000 条。
AUDIT_MAX_FILE_BYTES = 2 * 1024 * 1024
AUDIT_TRIM_KEEP_EVENTS = 2000
AUDIT_MAX_EVENT_COUNT = 4000
AUDIT_SUMMARY_WINDOW_HOURS = 24

KNOWN_SUBSYSTEM_LABELS = {
    "provider": "模型调用",
    "task": "任务执行",
    "web_api": "Web 接口",
}
AUDIT_LEVELS = ("info", "warning", "error")

_SUMMARY_TEXT_LIMIT = 240
_DETAIL_JSON_LIMIT = 4096
_DETAIL_FALLBACK_TEXT_LIMIT = 1000

_lock = threading.Lock()
_configured_root: Path | None = None


def configure_audit_sink(workspace: Path | str | None) -> Path | None:
    """Set the workspace root used for audit storage; never raises.

    Creates ``<workspace>/.g3ku`` best-effort and returns the resolved
    ``audit.jsonl`` path, or ``None`` when the input is empty/unusable.
    Passing ``None`` resets the sink to unconfigured.
    """
    global _configured_root
    try:
        if not workspace:
            _configured_root = None
            return None
        root = Path(workspace)
        audit_path = root / AUDIT_DIR_NAME / AUDIT_FILE_NAME
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        _configured_root = root
        return audit_path
    except Exception:
        return None


def audit_file_path() -> Path | None:
    """Configured audit file path (write side); ``None`` when unconfigured."""
    root = _configured_root
    if root is None:
        return None
    return root / AUDIT_DIR_NAME / AUDIT_FILE_NAME


def emit_audit_event(
    subsystem: str,
    level: str,
    event_type: str,
    summary: str,
    *,
    detail: dict[str, object] | None = None,
) -> bool:
    """Append one audit event; best-effort, never raises, returns success.

    Inputs are normalized: subsystem/level/event_type are stripped and
    lowercased, unknown levels collapse to ``'info'``, an empty event_type
    becomes ``'unspecified'``, the summary is truncated to 240 chars, and an
    oversized detail dict is replaced by a ``detail_truncated`` marker.
    """
    try:
        path = audit_file_path()
        if path is None:
            return False
        normalized_level = str(level or "").strip().lower()
        if normalized_level not in AUDIT_LEVELS:
            normalized_level = "info"
        normalized_type = str(event_type or "").strip().lower() or "unspecified"
        normalized_subsystem = str(subsystem or "").strip().lower() or "unknown"
        normalized_summary = str(summary or "").strip()[:_SUMMARY_TEXT_LIMIT]

        record_detail: dict[str, object] | None = None
        if isinstance(detail, dict):
            serialized = json.dumps(detail, ensure_ascii=False, default=str)
            if len(serialized) > _DETAIL_JSON_LIMIT:
                record_detail = {
                    "detail_truncated": True,
                    "detail_text": str(detail)[:_DETAIL_FALLBACK_TEXT_LIMIT],
                }
            else:
                record_detail = detail

        record = {
            "event_id": "evt_" + secrets.token_hex(6),
            "timestamp": _now_iso(),
            "subsystem": normalized_subsystem,
            "level": normalized_level,
            "event_type": normalized_type,
            "summary": normalized_summary,
            "detail": record_detail or {},
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            try:
                if os.stat(path).st_size > AUDIT_MAX_FILE_BYTES:
                    _trim_if_needed_locked(path)
            except OSError:
                pass
        return True
    except Exception:
        return False


def list_audit_events(
    *,
    limit: int = 50,
    offset: int = 0,
    level: str | None = None,
    subsystem: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Slice the audit ledger, newest first.

    ``level``/``subsystem`` filter by exact (case-insensitive) match; ``since``
    keeps only records whose ISO timestamp is strictly greater than the given
    string. Returns ``{'items', 'total', 'has_more'}``.
    """
    root = _resolve_read_root()
    events = _read_events(root)
    wanted_level = str(level or "").strip().lower() or None
    wanted_subsystem = str(subsystem or "").strip().lower() or None
    since_text = str(since or "").strip() or None
    filtered: list[dict[str, Any]] = []
    for record in events:
        if wanted_level and str(record.get("level") or "").lower() != wanted_level:
            continue
        if wanted_subsystem and str(record.get("subsystem") or "").lower() != wanted_subsystem:
            continue
        if since_text:
            record_ts = str(record.get("timestamp") or "")
            if not record_ts or record_ts <= since_text:
                continue
        filtered.append(record)
    total = len(filtered)
    offset = max(0, int(offset or 0))
    limit = max(1, int(limit or 50))
    items = filtered[offset : offset + limit]
    return {"items": items, "total": total, "has_more": offset + limit < total}


def audit_summary() -> dict[str, Any]:
    """Per-subsystem rollup over the last 24 hours.

    The fixed known subsystems are always present (zero-filled, in
    ``KNOWN_SUBSYSTEM_LABELS`` order); subsystems seen only in records follow
    in first-discovery order. Each entry carries 24h error/warning/event
    counts, the latest event's timestamp/level/summary, and a status of
    ``'error'`` when any error landed in the window, else ``'ok'``.
    """
    root = _resolve_read_root()
    window_start = datetime.now().astimezone() - timedelta(hours=AUDIT_SUMMARY_WINDOW_HOURS)
    entries: dict[str, dict[str, Any]] = {}
    for key, label in KNOWN_SUBSYSTEM_LABELS.items():
        entries[key] = _empty_summary_entry(key, label)
    for record in _read_events(root):
        ts_text = str(record.get("timestamp") or "")
        try:
            ts = datetime.fromisoformat(ts_text)
        except ValueError:
            continue
        if ts < window_start:
            continue
        key = str(record.get("subsystem") or "unknown").strip().lower() or "unknown"
        entry = entries.get(key)
        if entry is None:
            entry = _empty_summary_entry(key, key)
            entries[key] = entry
        level = str(record.get("level") or "info").strip().lower()
        entry["event_count"] = int(entry["event_count"]) + 1
        if level == "error":
            entry["error_count"] = int(entry["error_count"]) + 1
        elif level == "warning":
            entry["warning_count"] = int(entry["warning_count"]) + 1
        if not entry["latest_event_at"]:
            entry["latest_event_at"] = ts_text
            entry["latest_event_level"] = level
            entry["latest_event_summary"] = str(record.get("summary") or "")
    for entry in entries.values():
        entry["status"] = "error" if int(entry["error_count"]) > 0 else "ok"
    return {"subsystems": list(entries.values()), "generated_at": _now_iso()}


def _now_iso() -> str:
    """Local-aware ISO timestamp with second precision (matches main.protocol)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _lazy_workspace_root() -> Path | None:
    """Resolve the workspace root from the live runtime config; best-effort."""
    try:
        from g3ku.config.live_runtime import get_runtime_config

        config = get_runtime_config(force=False)[0]
        workspace = getattr(config, "workspace_path", None)
        return Path(workspace) if workspace else None
    except Exception:
        return None


def _resolve_read_root() -> Path:
    root = _configured_root
    if root is None:
        root = _lazy_workspace_root()
    if root is None:
        raise RuntimeError("audit_sink_unconfigured")
    return root


def _read_events(root: Path) -> list[dict[str, Any]]:
    """Read the ledger newest-first; malformed lines are skipped silently."""
    path = root / AUDIT_DIR_NAME / AUDIT_FILE_NAME
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict) or "timestamp" not in record:
            continue
        events.append(record)
    events.reverse()
    return events[:AUDIT_MAX_EVENT_COUNT]


def _trim_if_needed_locked(path: Path) -> None:
    """Keep only the newest ``AUDIT_TRIM_KEEP_EVENTS`` lines (caller holds ``_lock``).

    Rewrites via a pid-suffixed temp file + ``os.replace`` so concurrent
    append-only writers see either the old or the new file, never a partial
    one. Any failure abandons the trim and leaves the ledger untouched.
    """
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(3)}")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return
    lines = [line for line in content.splitlines() if line.strip()]
    if len(lines) <= AUDIT_TRIM_KEEP_EVENTS:
        return
    keep = lines[-AUDIT_TRIM_KEEP_EVENTS:]
    try:
        tmp_path.write_text("\n".join(keep) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _empty_summary_entry(key: str, label: str) -> dict[str, Any]:
    return {
        "subsystem": key,
        "label": label,
        "status": "ok",
        "event_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "latest_event_at": "",
        "latest_event_level": "",
        "latest_event_summary": "",
    }
