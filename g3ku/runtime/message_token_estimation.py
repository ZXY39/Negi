from __future__ import annotations

import json
from typing import Any

from g3ku.runtime.context.summarizer import estimate_tokens


def estimate_message_tokens(messages: list[dict[str, Any]] | None) -> int:
    payload = [
        dict(item)
        for item in list(messages or [])
        if isinstance(item, dict)
    ]
    return estimate_tokens(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    )


__all__ = ["estimate_message_tokens"]
