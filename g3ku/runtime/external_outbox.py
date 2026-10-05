"""Durable ledger for external-channel outbound: proactive pushes and turn replies.

The outbound delivery chain (bus -> drain -> per-session event hub -> bridge
SSE pump -> channel API) is entirely in-memory: a dead bridge pump or a
process restart silently strands heartbeat/cron/task-terminal pushes with no
replay path. This module is the durable ledger for that lane:

- The web shell drain registers every ``outbound.created`` publish here
  before it reaches the hub (``record_outbound_message``); the hub event
  carries the returned ``outbox_id``.
- The session event relay registers ``reply.final`` the same way
  (``event="reply.final"``). Turn replies need it for the same reason as
  pushes, and a pump is not always there to consume them: the only pump
  rebuild triggers are a channel inbound message and a pending ledger record,
  so a reply for a web-initiated channel turn has no consumer until this
  record makes the bridge build one.
- The bridge acks an id after the channel API confirms delivery
  (``POST /sessions/{id}/outbox/{outbox_id}/ack`` -> ``ack_outbound_message``).
  In-process reply waiters (OpenAI-compatible endpoint, MCP gateway) ack too:
  with no age-based cleanup they would otherwise hold their records forever.
- On startup the web shell republishes pending (unacked) records onto the
  outbound bus, and bridges warm pumps for sessions with pending records
  (``GET /outbox/pending``).

Age is deliberately not a discard reason: ``PENDING_MAX_AGE_SECONDS`` used to
retire everything unacked for 24h, which threw away answers the requester was
still waiting for. A record leaves the ledger only through an ack, and the
non-time exits are ``delivered`` (channel confirmed), ``undeliverable`` (bridge
gave up after its attempt budget) and ``unreachable`` (no enabled bridge token
can ever see it again — see ``g3ku.shells.web``).

Storage is a single append-only jsonl (``.g3ku/external-outbox/outbox.jsonl``)
of ``{"kind":"msg",...}`` records plus ``{"kind":"ack","id":...}`` tombstones.
Append-only keeps writes small and crash-safe under disk pressure: an ENOSPC
append failure degrades to live-only delivery with an error log and never
blocks the hub publish. ``compact_outbox`` rewrites the file down to pending
records and runs at startup after the replay load.

Delivery semantics are at-least-once: a lost ack (bridge crash between
channel delivery and ack) replays the message once after the next restart.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

OUTBOX_DIRNAME = Path(".g3ku") / "external-outbox"
OUTBOX_FILENAME = "outbox.jsonl"

# 记录对应哪种出站事件。主动推送是默认值；回合回复由 relay 显式标
# ``reply.final``，这样「为什么我收到两条同样的回答」事后能从账本里分得清。
PROACTIVE_EVENT = "outbound.created"
REPLY_EVENT = "reply.final"

_LOCK = threading.RLock()
_ROOT_OVERRIDE: Path | None = None


def configure_external_outbox_root(root: Path | str | None) -> None:
    """Test hook: redirect the outbox storage root (``None`` resets)."""
    global _ROOT_OVERRIDE
    _ROOT_OVERRIDE = Path(root) if root is not None else None


def _workspace_root() -> Path:
    # 与 external_sessions._registry_workspace 同一解析口径。
    try:
        from g3ku.config.live_runtime import get_runtime_config

        config = get_runtime_config(force=False)[0]
        workspace = getattr(config, "workspace_path", None)
        if workspace:
            return Path(workspace).resolve()
    except Exception:
        pass
    return Path.cwd().resolve()


def _outbox_path() -> Path:
    root = _ROOT_OVERRIDE if _ROOT_OVERRIDE is not None else _workspace_root()
    return Path(root) / OUTBOX_DIRNAME / OUTBOX_FILENAME


def _append_record(record: dict[str, Any]) -> bool:
    try:
        path = _outbox_path()
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        # 登记失败不阻断投递：消息仍走内存链路（live-only），但必须留下可
        # grep 的告警——磁盘满期间这正是"重启后无法重放"的边界。
        logger.error(
            "external outbox append failed ({}); message stays live-only: kind={} id={} session={}",
            exc,
            record.get("kind"),
            record.get("id"),
            record.get("session_key", "-"),
        )
        return False


def _read_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        path = _outbox_path()
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    records.append(parsed)
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.error("external outbox read failed: {}", exc)
        return []
    return records


def record_outbound_message(
    *,
    session_key: str,
    external_key: str,
    text: str,
    reply_to: str = "",
    dedupe_key: str = "",
    attachments: list[dict[str, Any]] | None = None,
    event: str = PROACTIVE_EVENT,
) -> str:
    """Register one pending outbound record; returns its outbox id.

    Empty string means registration failed (disk pressure): callers must still
    publish to the hub so live delivery gets its chance. ``attachments`` (the
    structured file descriptors extracted from the outbound text) are persisted
    so startup replay can re-publish them alongside the cleaned text.
    """
    outbox_id = f"obx-{uuid.uuid4().hex[:16]}"
    record: dict[str, Any] = {
        "kind": "msg",
        "id": outbox_id,
        "ts": datetime.now().isoformat(),
        "event": str(event or PROACTIVE_EVENT),
        "session_key": str(session_key or ""),
        "external_key": str(external_key or ""),
        "text": str(text or ""),
    }
    if str(reply_to or "").strip():
        record["reply_to"] = str(reply_to).strip()
    if str(dedupe_key or "").strip():
        record["dedupe_key"] = str(dedupe_key).strip()
    if attachments:
        record["attachments"] = [dict(item) for item in attachments if isinstance(item, dict)]
    if not _append_record(record):
        return ""
    return outbox_id


def ack_outbound_message(outbox_id: str, *, session_key: str = "", status: str = "delivered") -> bool:
    """Append an ack tombstone. With ``session_key`` the ack is refused unless
    the msg record exists and belongs to that session (cross-session guard for
    the REST surface). Idempotent: repeated acks are harmless tombstones."""
    clean = str(outbox_id or "").strip()
    if not clean:
        return False
    expected_session = str(session_key or "").strip()
    if expected_session:
        target = next(
            (
                record
                for record in _read_records()
                if str(record.get("kind") or "") == "msg" and str(record.get("id") or "") == clean
            ),
            None,
        )
        if target is None or str(target.get("session_key") or "") != expected_session:
            return False
    return _append_record(
        {
            "kind": "ack",
            "id": clean,
            "ts": datetime.now().isoformat(),
            "status": str(status or "delivered"),
        }
    )


def record_age_seconds(record: dict[str, Any], *, now: datetime | None = None) -> float | None:
    """Age of one ledger record in seconds; None when ts is missing/unparseable.

    周期对账用它做「足够老才重放」过滤。ts 畸形的记录重放不出去（对账侧直接跳过），
    但年龄不再是删除理由，所以它由 ``g3ku.shells.web`` 的不可达判定一并销账。
    """
    try:
        ts = datetime.fromisoformat(str(record.get("ts") or ""))
    except ValueError:
        return None
    return ((now or datetime.now()) - ts).total_seconds()


def load_pending_outbound() -> list[dict[str, Any]]:
    """Pending = msg records without an ack tombstone, oldest first."""
    records = _read_records()
    acked = {str(record.get("id") or "") for record in records if str(record.get("kind") or "") == "ack"}
    pending = [
        record
        for record in records
        if str(record.get("kind") or "") == "msg" and str(record.get("id") or "") not in acked
    ]
    pending.sort(key=lambda record: str(record.get("ts") or ""))
    return pending


def compact_outbox() -> None:
    """Best-effort: rewrite the append-only file down to pending msg records.

    Runs at startup after the replay load; ack tombstones (including fresh
    ``expired`` ones) are dropped together with their msg records.
    """
    with _LOCK:
        pending = load_pending_outbound()
        tmp: Path | None = None
        try:
            path = _outbox_path()
            if not path.exists():
                return
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                for record in pending:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("external outbox compaction skipped: {}", exc)
            try:
                if tmp is not None and tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
