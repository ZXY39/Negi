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
    if not isinstance(payload, dict):
        return False
    if payload.get("ok") is False:
        return True
    # `status: "error"` 是 exec 自己声明成败的写法（`g3ku/agent/tools/shell.py` 按退出码写
    # 这个键，命令没跑起来时也是这个词）。只认这一个字面量：别的工具把 `status` 用作动作
    # 落点（`stop_tool_execution` 成功取消任务时回 "stopped"、"not_found"），按失败词表
    # 判会把一次成功的调用画成感叹号。
    return str(payload.get("status") or "").strip().lower() == "error"


__all__ = [
    "is_error_like_tool_result",
]
