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
            "Scope: in the operator's own sessions this updates local long-term memory; in a group "
            "session it updates only that group's memory block, never the operator's local memory.\n"
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
        manager = self._manager
        view = runtime.get("memory_view")
        if view is None and session_key:
            # 节点道/未携带视图的调用点：按会话键问进程内活动路由，别掉回本地域。
            try:
                from g3ku.runtime.memory_scope_router import active_memory_scope_router

                router = active_memory_scope_router()
                if router is not None:
                    view = router.view_for_session(session_key)
            except Exception:
                view = None
        if view is not None:
            # 分域路由：群聊写本群专属块；本地写本地；第三方私聊没有可写管理器。
            manager = getattr(view, "writable_manager", None)
            if manager is None:
                return json.dumps(
                    {
                        "ok": False,
                        "status": "rejected",
                        "reason": "memory_read_only",
                        "message": (
                            "Long-term memory is writable only from the operator's own session or "
                            "from a group session (which writes that group's own block). "
                            "This session is read-only, so nothing was queued. "
                            "Do not retry; explain to the user that memory changes need the operator."
                        ),
                    },
                    ensure_ascii=False,
                )
        result = await manager.enqueue_write_request(
            session_key=session_key,
            decision_source="user",
            payload_text=str(content or "").strip(),
            trigger_source="memory_update_tool",
        )
        return json.dumps(result, ensure_ascii=False)
