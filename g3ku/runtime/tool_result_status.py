from __future__ import annotations

import json
from typing import Any


def _structured_tool_result_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return None
    text = str(value or "").strip()
    if text[:1] != "{":
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def is_error_like_tool_result(value: Any) -> bool:
    if isinstance(value, str) and str(value or "").strip().startswith("Error"):
        return True
    payload = _structured_tool_result_payload(value)
    return isinstance(payload, dict) and payload.get("ok") is False


__all__ = [
    "is_error_like_tool_result",
]
