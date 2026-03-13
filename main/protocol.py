from __future__ import annotations

from datetime import datetime
from typing import Any


PROTOCOL_VERSION = 1


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def build_envelope(
    *,
    channel: str,
    session_id: str,
    type: str,
    data: dict[str, Any] | None = None,
    task_id: str | None = None,
    seq: int = 0,
    event_name: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'protocol_version': PROTOCOL_VERSION,
        'channel': channel,
        'session_id': session_id,
        'task_id': task_id,
        'seq': seq,
        'timestamp': now_iso(),
        'type': type,
        'data': data or {},
    }
    if event_name:
        payload['event_name'] = event_name
    return payload
