"""Agent-facing control surface for external event subscriptions."""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.runtime.event_subscriptions import EventSubscriptionManager


class EventSubscriptionTool(Tool):
    def __init__(self, manager: EventSubscriptionManager):
        self._manager = manager

    @property
    def name(self) -> str:
        return "event_subscription"

    @property
    def description(self) -> str:
        return (
            "Create or manage authenticated external-event subscriptions that wake the current session or task node."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "create",
                        "list",
                        "events",
                        "set_enabled",
                        "rotate_credential",
                        "remove",
                    ],
                    "description": "Action to execute.",
                },
                "name": {
                    "type": "string",
                    "description": "Short subscription name for later identification.",
                },
                "instruction": {
                    "type": "string",
                    "description": (
                        "Instruction for your future self when this subscription wakes the target: "
                        "what condition occurred and what to do next."
                    ),
                },
                "condition_note": {
                    "type": "string",
                    "description": "Optional human-readable description of the external trigger condition.",
                },
                "target_kind": {
                    "type": "string",
                    "enum": ["session", "node"],
                    "description": "Wake the current session (default) or one execution node in the current scope.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task id for a node target; may be inferred from node_id.",
                },
                "node_id": {
                    "type": "string",
                    "description": "Execution node id for a node target.",
                },
                "credential_file": {
                    "type": "string",
                    "description": (
                        "Absolute path inside the current session workspace or task temp directory "
                        "where the generated connector credential JSON is written."
                    ),
                },
                "subscription_id": {
                    "type": "string",
                    "description": "Subscription id for events, set_enabled, rotate_credential or remove.",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Desired enabled state for set_enabled.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum recent events to return for events; default 20, max 50.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs: Any) -> str:
        runtime_context = kwargs.get("__g3ku_runtime")
        context = dict(runtime_context) if isinstance(runtime_context, dict) else {}
        try:
            if action == "create":
                result = self._manager.create(
                    runtime_context=context,
                    name=str(kwargs.get("name") or ""),
                    instruction=str(kwargs.get("instruction") or ""),
                    condition_note=str(kwargs.get("condition_note") or ""),
                    target_kind=str(kwargs.get("target_kind") or "session"),
                    task_id=str(kwargs.get("task_id") or ""),
                    node_id=str(kwargs.get("node_id") or ""),
                    credential_file=str(kwargs.get("credential_file") or ""),
                )
                return _render("Created external event subscription.", result)
            if action == "list":
                items = self._manager.list(runtime_context=context)
                return _render("External event subscriptions.", {"items": items})
            if action == "events":
                events = self._manager.events(
                    runtime_context=context,
                    subscription_id=str(kwargs.get("subscription_id") or ""),
                    limit=int(kwargs.get("limit") or 20),
                )
                return _render("Recent external events.", {"items": events})
            if action == "set_enabled":
                result = self._manager.set_enabled(
                    runtime_context=context,
                    subscription_id=str(kwargs.get("subscription_id") or ""),
                    enabled=bool(kwargs.get("enabled")),
                )
                return _render("Subscription state updated.", result)
            if action == "rotate_credential":
                result = self._manager.rotate_credential(
                    runtime_context=context,
                    subscription_id=str(kwargs.get("subscription_id") or ""),
                )
                return _render("Subscription credential rotated.", result)
            if action == "remove":
                result = self._manager.remove(
                    runtime_context=context,
                    subscription_id=str(kwargs.get("subscription_id") or ""),
                )
                return _render("Subscription removed.", result)
            return f"Error: unknown action: {action}"
        except (TypeError, ValueError) as exc:
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001 - tool errors return to the model
            return f"Error: event_subscription failed: {exc}"


def _render(title: str, payload: dict[str, Any] | None) -> str:
    return title + "\n" + json.dumps(payload or {}, ensure_ascii=False, indent=2, sort_keys=True)


__all__ = ["EventSubscriptionTool"]
