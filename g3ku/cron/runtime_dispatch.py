from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from g3ku.core.messages import UserInputMessage
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
    delivered_runs = max(0, int(getattr(getattr(job, "state", None), "delivered_runs", 0) or 0))
    max_runs = max(1, int(getattr(payload, "max_runs", 1) or 1))
    user_message = UserInputMessage(
        content=str(getattr(payload, "message", "") or ""),
        metadata={
            "cron_internal": True,
            "cron_job_id": str(getattr(job, "id", "") or "").strip(),
            "cron_max_runs": max_runs,
            "cron_delivery_index": delivered_runs + 1,
            "cron_delivered_runs": delivered_runs,
            "cron_reminder_text": str(getattr(payload, "message", "") or ""),
            "cron_scheduled_run_at_ms": getattr(getattr(job, "state", None), "next_run_at_ms", None),
            "cron_last_delivered_at_ms": getattr(getattr(job, "state", None), "last_delivered_at_ms", None),
        },
    )
    result = await runtime_bridge.prompt(
        user_message,
        session_key=session_key,
        channel=channel,
        chat_id=chat_id,
        register_task=register_task,
    )
    return str(getattr(result, "output", "") or "")
