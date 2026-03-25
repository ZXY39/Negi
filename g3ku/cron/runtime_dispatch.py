from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from g3ku.cron.types import CronJob


def resolve_cron_session_key(job: CronJob, *, session_manager: Any | None = None) -> str:
    payload = getattr(job, "payload", None)
    raw_session_key = str(getattr(payload, "session_key", "") or "").strip()
    if raw_session_key:
        cache = getattr(session_manager, "_cache", None)
        if isinstance(cache, dict) and raw_session_key in cache:
            return raw_session_key
        get_path = getattr(session_manager, "get_path", None)
        if callable(get_path):
            try:
                path = Path(get_path(raw_session_key))
            except Exception:
                path = None
            if path is not None and path.exists():
                return raw_session_key
    return f"cron:{str(getattr(job, 'id', '') or '').strip()}"


async def dispatch_cron_job(
    job: CronJob,
    *,
    runtime_bridge: Any,
    session_manager: Any | None = None,
    register_task: Callable[[str, Any], None] | None = None,
) -> str | None:
    payload = getattr(job, "payload", None)
    channel = str(getattr(payload, "channel", "") or "").strip() or "cli"
    chat_id = str(getattr(payload, "to", "") or "").strip() or "direct"
    session_key = resolve_cron_session_key(job, session_manager=session_manager)
    result = await runtime_bridge.prompt(
        str(getattr(payload, "message", "") or ""),
        session_key=session_key,
        channel=channel,
        chat_id=chat_id,
        register_task=register_task,
    )
    return str(getattr(result, "output", "") or "")
