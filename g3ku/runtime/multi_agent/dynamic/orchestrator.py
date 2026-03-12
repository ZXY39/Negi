from __future__ import annotations

import json
from typing import Any

from langchain.agents import create_agent
from langchain_core.tools import StructuredTool

from g3ku.core.messages import UserInputMessage
from g3ku.integrations.langchain_runtime import extract_final_response
from g3ku.runtime.multi_agent.dynamic.prompt_builder import DynamicPromptBuilder
from g3ku.runtime.multi_agent.dynamic.tracing import TraceContext, trace_payload
from g3ku.runtime.multi_agent.dynamic.types import DynamicSubagentRequest, DynamicSubagentResult


class OrchestratorRunner:
    def __init__(self, *, loop, controller=None, config=None, role_registry=None):
        self._loop = loop
        self._controller = controller or getattr(loop, "dynamic_subagent_controller", None)
        self._config = config or getattr(loop, "multi_agent_config", None)
        self._role_registry = role_registry
        self._prompt_builder = DynamicPromptBuilder(loop=loop)

    async def run_turn(self, *, user_input: UserInputMessage, session, on_progress=None) -> str:
        await self._loop._ensure_checkpointer_ready()
        system_prompt = self._prompt_builder.build_orchestrator_prompt()
        tools = self._build_tools(session=session, on_progress=on_progress)
        model_client = self._loop.model_client
        orchestrator_model_key = str(getattr(self._config, "orchestrator_model_key", "") or "").strip()
        default_model_key = str(getattr(self._loop, "_runtime_default_model_key", "") or "").strip()
        if orchestrator_model_key and orchestrator_model_key != default_model_key:
            model_client = self._controller.model_chain_executor._build_model_client(orchestrator_model_key)
        agent = create_agent(
            model=model_client,
            tools=tools,
            checkpointer=self._loop._checkpointer,
            store=self._loop._store,
            name="g3ku_orchestrator",
        )
        config: dict[str, Any] = {"recursion_limit": max(8, int(getattr(self._loop, "max_iterations", 12) or 12) * 2 + 4)}
        config["configurable"] = {"thread_id": session.state.session_key}
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        notices = self._drain_session_notices(session.state.session_key)
        if notices:
            messages.append({"role": "system", "content": self._format_session_notices(notices)})
        messages.append({"role": "user", "content": _content_text(user_input.content)})
        result = await agent.ainvoke({"messages": messages}, config=config)
        result_messages = list(result.get("messages") or [])
        final = extract_final_response(result_messages)
        return final.content if final and final.content else ""

    def _build_tools(self, *, session, on_progress=None):
        parent_session_id = session.state.session_key
        channel = getattr(session, "_channel", "cli")
        chat_id = getattr(session, "_chat_id", parent_session_id)

        async def delegate_task_sync(
            prompt: str,
            category: str = "",
            load_skills: list[str] | None = None,
            tools_allow: list[str] | None = None,
            context_constraints: list[str] | None = None,
            action_rules: list[str] | None = None,
            expected_schema: dict[str, Any] | None = None,
        ) -> str:
            trace = TraceContext(parent_session_id=parent_session_id, category=category or "dynamic", run_mode="sync")
            if on_progress is not None:
                await on_progress("Orchestrator delegating sync task", event_kind="tool_start", event_data=trace_payload(trace, tool_name="delegate_task_sync"))
            result = await self._controller.delegate_sync(
                request=DynamicSubagentRequest(
                    parent_session_id=parent_session_id,
                    category=category,
                    prompt=prompt,
                    load_skills=list(load_skills or []),
                    tools_allow=list(tools_allow or []),
                    output_schema=expected_schema,
                    run_mode="sync",
                    context_constraints=list(context_constraints or []),
                    action_rules=list(action_rules or []),
                ),
                channel=channel,
                chat_id=chat_id,
                on_progress=on_progress,
            )
            if on_progress is not None:
                await on_progress(
                    "Sync delegation completed" if result.ok else "Sync delegation failed",
                    event_kind="tool_result" if result.ok else "tool_error",
                    event_data=trace_payload(trace, tool_name="delegate_task_sync", current_session_id=result.session_id, lifecycle_status=result.status, error=result.error),
                )
            return result.model_dump_json(indent=2)

        async def delegate_task_background(
            prompt: str,
            category: str = "",
            load_skills: list[str] | None = None,
            tools_allow: list[str] | None = None,
            context_constraints: list[str] | None = None,
            action_rules: list[str] | None = None,
            expected_schema: dict[str, Any] | None = None,
        ) -> str:
            record = await self._controller.delegate_background(
                request=DynamicSubagentRequest(
                    parent_session_id=parent_session_id,
                    category=category,
                    prompt=prompt,
                    load_skills=list(load_skills or []),
                    tools_allow=list(tools_allow or []),
                    output_schema=expected_schema,
                    run_mode="background",
                    context_constraints=list(context_constraints or []),
                    action_rules=list(action_rules or []),
                    metadata={"origin_channel": channel, "origin_chat_id": chat_id},
                )
            )
            return record.model_dump_json(indent=2)

        async def continue_subagent_session(session_id: str, prompt: str, destroy_after_accept: bool = False) -> str:
            result = await self._controller.continue_session(
                parent_session_id=parent_session_id,
                session_id=session_id,
                prompt=prompt,
                channel=channel,
                chat_id=chat_id,
                on_progress=on_progress,
                destroy_after_accept=destroy_after_accept,
                background=False,
            )
            return result.model_dump_json(indent=2)

        async def subagent_task_status(task_id: str | None = None, session_id: str | None = None) -> str:
            pool = self._controller.background_pool
            if pool is None:
                return '{"ok": false, "status": "background_unavailable"}'
            return json.dumps(pool.status(task_id=task_id, session_id=session_id), ensure_ascii=False)

        async def pause_subagent_task(task_id: str | None = None, session_id: str | None = None) -> str:
            pool = self._controller.background_pool
            if pool is None:
                return '{"ok": false, "status": "background_unavailable"}'
            return json.dumps(await pool.pause(task_id=task_id, session_id=session_id), ensure_ascii=False)

        async def cancel_subagent_task(task_id: str | None = None, session_id: str | None = None) -> str:
            pool = self._controller.background_pool
            if pool is None:
                return '{"ok": false, "status": "background_unavailable"}'
            return json.dumps(await pool.cancel(task_id=task_id, session_id=session_id), ensure_ascii=False)

        return [
            StructuredTool.from_function(coroutine=delegate_task_sync, name="delegate_task_sync", description="Delegate a focused task to a new dynamic subagent, explicitly specifying the temporary role label plus required tools and skills, and wait for the result.", infer_schema=True),
            StructuredTool.from_function(coroutine=delegate_task_background, name="delegate_task_background", description="Delegate a long-running task to a background dynamic subagent, explicitly specifying the temporary role label plus required tools and skills, and return task/session identifiers.", infer_schema=True),
            StructuredTool.from_function(coroutine=continue_subagent_session, name="continue_subagent_session", description="Continue an existing frozen dynamic subagent session using its session_id.", infer_schema=True),
            StructuredTool.from_function(coroutine=subagent_task_status, name="subagent_task_status", description="Inspect a background dynamic subagent task by task_id or session_id.", infer_schema=True),
            StructuredTool.from_function(coroutine=pause_subagent_task, name="pause_subagent_task", description="Pause a background dynamic subagent task.", infer_schema=True),
            StructuredTool.from_function(coroutine=cancel_subagent_task, name="cancel_subagent_task", description="Cancel a background dynamic subagent task.", infer_schema=True),
        ]

    def _drain_session_notices(self, session_key: str) -> list[dict[str, Any]]:
        drain = getattr(self._loop, "drain_session_notices", None)
        if not callable(drain):
            return []
        return list(drain(session_key) or [])

    @staticmethod
    def _format_session_notices(notices: list[dict[str, Any]]) -> str:
        lines = [
            "Pending runtime notices from previous subagent executions:",
            "Use them as hard facts when deciding whether to retry, continue, or explain a failure to the user.",
        ]
        for index, notice in enumerate(notices, start=1):
            source = str(notice.get("source") or "runtime")
            level = str(notice.get("level") or "warn")
            text = str(notice.get("text") or "").strip()
            metadata = notice.get("metadata") if isinstance(notice.get("metadata"), dict) else {}
            extras = []
            for key in ("task_id", "session_id", "category", "status"):
                value = metadata.get(key)
                if value:
                    extras.append(f"{key}={value}")
            suffix = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"{index}. [{level}] {source}: {text}{suffix}")
        return "\n".join(lines)


MultiAgentRunner = OrchestratorRunner


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                raw = item.get("content") or item.get("text") or ""
                if raw:
                    parts.append(str(raw))
            elif item:
                parts.append(str(item))
        return "\n".join(parts)
    return str(value or "")

