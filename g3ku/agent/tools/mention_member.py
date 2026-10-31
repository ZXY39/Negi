"""Send a proactive group message that @-mentions one known member."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from g3ku.agent.tools.base import Tool

MentionPublisher = Callable[..., Awaitable[dict[str, Any]]]


class MentionMemberTool(Tool):
    """Only available in external group/guild sessions.

    The publisher is injected by the front door: it resolves the member from
    the session's sender roster and writes the message through the durable
    external outbound lane. The tool never accepts a raw openid.
    """

    hide_universal_timeout_parameter = True

    def __init__(self, publish: MentionPublisher):
        self._publish = publish

    @property
    def name(self) -> str:
        return "mention_member"

    @property
    def description(self) -> str:
        return (
            "Mention Master or a listed member number in this group; raw openids are rejected and delivery "
            "may be refused."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": "`Master` or a session-local member number such as `0002`.",
                },
                "message": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1000,
                    "description": "Message body sent to the group with the mention.",
                },
            },
            "required": ["target", "message"],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not str((params or {}).get("target") or "").strip():
            errors.append("target must not be empty")
        if not str((params or {}).get("message") or "").strip():
            errors.append("message must not be empty")
        if len(str((params or {}).get("message") or "")) > 1000:
            errors.append("message must be at most 1000 characters")
        return errors

    async def execute(
        self,
        target: str,
        message: str,
        **kwargs: Any,
    ) -> str:
        runtime = kwargs.get("__g3ku_runtime")
        result = await self._publish(
            target=str(target or "").strip(),
            message=str(message or "").strip(),
            runtime=runtime if isinstance(runtime, dict) else {},
        )
        return json.dumps(result, ensure_ascii=False)


__all__ = ["MentionMemberTool"]
