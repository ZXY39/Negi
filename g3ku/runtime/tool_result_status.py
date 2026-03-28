from __future__ import annotations

import json
from typing import Any

_RUNNING_STATUSES = {"background_running", "running", "in_progress", "queued"}
_SUCCESS_STATUSES = {"success", "ok", "completed", "complete"}
_ERROR_STATUSES = {
    "error",
    "failed",
    "not_found",
    "unavailable",
    "stopped",
    "cancelled",
    "canceled",
    "timed_out",
    "timeout",
}


def infer_tool_result_status(value: Any, *, default: str = "success") -> str:
    payload = _coerce_payload(value)
    inferred = _payload_status(payload)
    if inferred:
        return inferred
    if isinstance(value, str) and str(value or "").strip().startswith("Error"):
        return "error"
    return str(default or "success")


def _coerce_payload(value: Any) -> Any:
    if isinstance(value, str):
        text = str(value or "").strip()
        if not text or text[:1] not in "{[":
            return value
        try:
            return json.loads(text)
        except Exception:
            return value
    return value


def _payload_status(payload: Any) -> str | None:
    if isinstance(payload, dict):
        status = str(payload.get("status") or "").strip().lower()
        if status in _RUNNING_STATUSES:
            return "running"
        if status in _SUCCESS_STATUSES:
            return "success"
        if status in _ERROR_STATUSES:
            return "error"

        ok_value = payload.get("ok")
        if isinstance(ok_value, bool):
            return "success" if ok_value else "error"

        success_value = payload.get("success")
        if isinstance(success_value, bool):
            return "success" if success_value else "error"

        exit_code = payload.get("exit_code")
        if isinstance(exit_code, int):
            return "success" if exit_code == 0 else "error"

        if str(payload.get("error") or "").strip():
            return "error"
        return None

    if isinstance(payload, list):
        saw_running = False
        for item in payload:
            inferred = _payload_status(item)
            if inferred == "error":
                return "error"
            if inferred == "running":
                saw_running = True
        return "running" if saw_running else None

    return None


__all__ = ["infer_tool_result_status"]
