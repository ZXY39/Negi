"""Queued tool for natural-language long-term memory requests."""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import Tool


class MemoryUpdateTool(Tool):
    """Queue a durable memory request for the CEO agent."""

    # 内部入队工具、瞬时完成：不向模型暴露统一 timeout_seconds 参数。
    hide_universal_timeout_parameter = True

    def __init__(self, *, manager: Any):
        self._manager = manager

    @property
    def name(self) -> str:
        return "memory_update"

    @property
    def description(self) -> str:
        return (
            "Queue a durable long-term memory request in natural language.\n"
            "Use this when the user explicitly asks the system to remember, revise, or forget a stable rule, "
            "preference, identity detail, default, constraint, or project fact.\n"
            "State what should change; a memory agent decides whether that becomes a new item, a rewrite of an "
            "existing one, or a deletion.\n"
            "所有关于记忆的处理都通过此工具交给记忆 agent，禁止自己改 `memory/` 下的文件。\n"
            "This call only queues the request: nothing has changed yet, so tell the user what you submitted "
            "and never say the memory is already saved, revised, or removed.\n"
            "任意数量的记忆处理需求可通过自然语言描述后，一次调用即可处理，禁止批量调用。\n"
            "Do not use this for temporary execution state or speculative guesses."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "Natural-language memory request to queue for the memory agent: "
                        "what to remember, revise, or forget."
                    ),
                }
            },
            "required": ["content"],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not str((params or {}).get("content") or "").strip():
            errors.append("content must not be empty")
        return errors

    async def execute(
        self,
        content: str,
        **kwargs: Any,
    ) -> str:
        runtime_raw = kwargs.pop("__g3ku_runtime", None)
        runtime = runtime_raw if isinstance(runtime_raw, dict) else {}
        session_key = str(runtime.get("session_key") or "")
        eligibility = getattr(self._manager, "review_eligibility", None)
        if callable(eligibility):
            allowed, reason = eligibility(session_key)
            if not allowed:
                # 控制面拒绝的第一道：群聊/第三方会话连入队都不许。工具面本身已经在
                # 受限档把它摘掉了，这里是"万一装配漏改"的硬校验。
                return json.dumps(
                    {
                        "ok": False,
                        "status": "rejected",
                        "reason": reason,
                        "message": (
                            "Long-term memory is only writable from the operator's own session. "
                            "This session is third-party, so nothing was queued. "
                            "Do not retry; explain to the user that memory changes need the operator."
                        ),
                    },
                    ensure_ascii=False,
                )
        result = await self._manager.enqueue_write_request(
            session_key=session_key,
            decision_source="user",
            payload_text=str(content or "").strip(),
            trigger_source="memory_update_tool",
        )
        return json.dumps(result, ensure_ascii=False)
