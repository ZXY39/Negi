from __future__ import annotations

from typing import Any

PENDING_NOTICE_STATE_KEY = "pending_notice_state"
RESUME_MODE_ORDINARY = "ordinary"
RESUME_MODE_WAIT_FOR_CHILDREN = "wait_for_children"
_KNOWN_RESUME_MODES = {RESUME_MODE_ORDINARY, RESUME_MODE_WAIT_FOR_CHILDREN}


def normalize_pending_notice_state(payload: Any) -> dict[str, str]:
    current = dict(payload or {}) if isinstance(payload, dict) else {}
    resume_mode = str(current.get("resume_mode") or "").strip()
    if resume_mode not in _KNOWN_RESUME_MODES:
        resume_mode = RESUME_MODE_ORDINARY
    return {
        "resume_mode": resume_mode,
        "epoch_id": str(current.get("epoch_id") or "").strip(),
        "holding_round_id": str(current.get("holding_round_id") or "").strip(),
        "updated_at": str(current.get("updated_at") or "").strip(),
    }


def set_pending_notice_state(
    payload: Any,
    *,
    resume_mode: str,
    epoch_id: str,
    holding_round_id: str,
    updated_at: str,
) -> dict[str, str]:
    current = normalize_pending_notice_state(payload)
    normalized_mode = str(resume_mode or "").strip()
    if normalized_mode not in _KNOWN_RESUME_MODES:
        normalized_mode = RESUME_MODE_ORDINARY
    current["resume_mode"] = normalized_mode
    current["epoch_id"] = str(epoch_id or "").strip()
    current["holding_round_id"] = str(holding_round_id or "").strip()
    current["updated_at"] = str(updated_at or "").strip()
    return current


def clear_pending_notice_state(payload: Any) -> dict[str, str]:
    return {
        "resume_mode": RESUME_MODE_ORDINARY,
        "epoch_id": "",
        "holding_round_id": "",
        "updated_at": "",
    }
