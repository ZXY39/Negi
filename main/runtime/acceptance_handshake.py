from __future__ import annotations

from typing import Any

ACCEPTANCE_HANDSHAKE_KEY = "acceptance_handshake"
ACCEPTANCE_STATE_IDLE = "idle"
ACCEPTANCE_STATE_WAITING_ACCEPTANCE = "waiting_acceptance"
ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY = "waiting_execution_retry"
ACCEPTANCE_STATE_ACCEPTED = "accepted"
ACCEPTANCE_STATE_REJECTED_TERMINAL = "rejected_terminal"
ACCEPTANCE_STATE_CANCELED_BY_EXECUTION_FAILURE = "canceled_by_execution_failure"
FINAL_ACCEPTANCE_STATUS_PENDING = "pending"
FINAL_ACCEPTANCE_STATUS_RUNNING = "running"
FINAL_ACCEPTANCE_STATUS_PASSED = "passed"
FINAL_ACCEPTANCE_STATUS_FAILED = "failed"
FINAL_ACCEPTANCE_STATUS_VALUES = {
    FINAL_ACCEPTANCE_STATUS_PENDING,
    FINAL_ACCEPTANCE_STATUS_RUNNING,
    ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
    FINAL_ACCEPTANCE_STATUS_PASSED,
    FINAL_ACCEPTANCE_STATUS_FAILED,
    ACCEPTANCE_STATE_ACCEPTED,
    ACCEPTANCE_STATE_REJECTED_TERMINAL,
    ACCEPTANCE_STATE_CANCELED_BY_EXECUTION_FAILURE,
}

_KNOWN_ACCEPTANCE_STATES = {
    ACCEPTANCE_STATE_IDLE,
    ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
    ACCEPTANCE_STATE_ACCEPTED,
    ACCEPTANCE_STATE_REJECTED_TERMINAL,
    ACCEPTANCE_STATE_CANCELED_BY_EXECUTION_FAILURE,
}


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def normalize_acceptance_handshake(payload: Any) -> dict[str, Any]:
    current = dict(payload or {}) if isinstance(payload, dict) else {}
    state = str(current.get("state") or "").strip()
    if state not in _KNOWN_ACCEPTANCE_STATES:
        state = ACCEPTANCE_STATE_IDLE
    rejection_count = max(0, _coerce_int(current.get("rejection_count"), default=0))
    max_rejections = _coerce_int(current.get("max_rejections"), default=2)
    if rejection_count < 0:
        rejection_count = 0
    if max_rejections <= 0:
        max_rejections = 2
    return {
        "state": state,
        "acceptance_node_id": str(current.get("acceptance_node_id") or "").strip(),
        "rejection_count": rejection_count,
        "max_rejections": max_rejections,
        "latest_execution_result_ref": str(current.get("latest_execution_result_ref") or "").strip(),
        "latest_execution_result_summary": str(current.get("latest_execution_result_summary") or "").strip(),
        "latest_rejection_feedback_ref": str(current.get("latest_rejection_feedback_ref") or "").strip(),
        "latest_rejection_feedback_summary": str(current.get("latest_rejection_feedback_summary") or "").strip(),
        "updated_at": str(current.get("updated_at") or "").strip(),
    }


def set_acceptance_handshake_state(
    payload: Any,
    *,
    state: str,
    acceptance_node_id: str,
    rejection_count: int,
    max_rejections: int,
    latest_execution_result_ref: str,
    latest_execution_result_summary: str,
    latest_rejection_feedback_ref: str,
    latest_rejection_feedback_summary: str,
    updated_at: str,
) -> dict[str, Any]:
    current = normalize_acceptance_handshake(payload)
    current["state"] = state if state in _KNOWN_ACCEPTANCE_STATES else ACCEPTANCE_STATE_IDLE
    current["acceptance_node_id"] = str(acceptance_node_id or "").strip()
    current["rejection_count"] = max(0, _coerce_int(rejection_count, default=0))
    current["max_rejections"] = _coerce_int(max_rejections, default=2)
    if current["max_rejections"] <= 0:
        current["max_rejections"] = 2
    current["latest_execution_result_ref"] = str(latest_execution_result_ref or "").strip()
    current["latest_execution_result_summary"] = str(latest_execution_result_summary or "").strip()
    current["latest_rejection_feedback_ref"] = str(latest_rejection_feedback_ref or "").strip()
    current["latest_rejection_feedback_summary"] = str(latest_rejection_feedback_summary or "").strip()
    current["updated_at"] = str(updated_at or "").strip()
    return current
