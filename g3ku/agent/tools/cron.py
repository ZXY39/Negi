"""Cron tool for scheduling reminders and tasks."""

from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.cron.service import CronService
from g3ku.cron.timezones import validate_timezone_name
from g3ku.cron.types import CronSchedule


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(self, cron_service: CronService):
        self._cron = cron_service
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Schedule reminders and recurring tasks. Actions: add, list, remove. "
            "Reminder messages must be written as internal instructions to your future self. "
            "Recurring reminders stop after `max_runs` successful deliveries; omitted counts default to one-shot."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove"],
                    "description": "Action to perform",
                },
                "message": {
                    "type": "string",
                    "description": (
                        "Internal reminder content for your future self. "
                        "Describe the action to take, not the final user-facing reply."
                    ),
                },
                "max_runs": {
                    "type": "integer",
                    "description": (
                        "Maximum number of successful reminder deliveries. "
                        "If omitted, the reminder defaults to one-shot. `at` reminders always use 1."
                    ),
                },
                "stop_condition": {
                    "type": "string",
                    "description": (
                        "Deprecated compatibility field. Reminder stopping is controlled by `max_runs`."
                    ),
                },
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds (for recurring tasks)",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *' (for scheduled tasks)",
                },
                "tz": {
                    "type": "string",
                    "description": "IANA timezone for cron expressions (e.g. 'America/Vancouver')",
                },
                "at": {
                    "type": "string",
                    "description": "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00')",
                },
                "job_id": {"type": "string", "description": "Job ID (for remove)"},
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        message: str = "",
        stop_condition: str | None = None,
        max_runs: int | None = None,
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        runtime_context = kwargs.get("__g3ku_runtime") if isinstance(kwargs.get("__g3ku_runtime"), dict) else {}
        if bool(runtime_context.get("cron_internal")):
            current_job_id = str(runtime_context.get("cron_job_id") or "").strip()
            if action != "remove":
                return "Error: cron-internal runs may only remove the current job when the stop condition is met"
            if not current_job_id:
                return "Error: cron-internal remove is unavailable because the current job_id is missing"
            if str(job_id or "").strip() != current_job_id:
                return f"Error: cron-internal runs may only remove the current job_id '{current_job_id}'"
        if action == "add":
            return self._add_job(
                message,
                stop_condition,
                max_runs,
                every_seconds,
                cron_expr,
                tz,
                at,
                runtime_context=runtime_context,
            )
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        message: str,
        stop_condition: str | None,
        max_runs: int | None,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
        *,
        runtime_context: dict[str, Any] | None = None,
    ) -> str:
        if not message:
            return "Error: message is required for add"
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"
        if tz and not cron_expr:
            return "Error: tz can only be used with cron_expr"
        if tz:
            try:
                validate_timezone_name(tz)
            except ValueError as exc:
                return f"Error: {exc}"

        # Build schedule
        delete_after = False
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
        elif at:
            from datetime import datetime

            dt = datetime.fromisoformat(at)
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return "Error: either every_seconds, cron_expr, or at is required"

        try:
            job = self._cron.add_job(
                name=message[:30],
                schedule=schedule,
                message=message,
                deliver=True,
                channel=self._channel,
                to=self._chat_id,
                session_key=str((runtime_context or {}).get("session_key") or "").strip() or None,
                stop_condition=stop_condition,
                max_runs=max_runs,
                delete_after_run=delete_after,
            )
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Created job '{job.name}' (id: {job.id})"

    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = [f"- {j.name} (id: {j.id}, {j.schedule.kind})" for j in jobs]
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        if self._cron.remove_job(job_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"

