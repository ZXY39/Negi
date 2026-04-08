from __future__ import annotations

import json
from typing import Any

from langchain.agents import create_agent
from langchain.messages import SystemMessage
from langchain_core.messages import BaseMessage, RemoveMessage, convert_to_messages
from langchain_core.messages import SystemMessage as CoreSystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command

from g3ku.providers.base_chat_model_adapter import G3kuChatModelAdapter

from ._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from .ceo_agent_middleware import (
    CeoApprovalMiddleware,
    CeoModelOutputMiddleware,
    CeoPromptAssemblyMiddleware,
    CeoToolExposureMiddleware,
    CeoTurnLifecycleMiddleware,
)
from .state_models import (
    CeoPersistentState,
    CeoRuntimeContext,
    initial_persistent_state,
)


class CreateAgentCeoFrontDoorRunner(CeoFrontDoorRuntimeOps):
    def __init__(self, *, loop: Any) -> None:
        super().__init__(loop=loop)
        self._agent = None

    def build_prompt_context(self, *, state, runtime, tools) -> dict[str, str]:
        _ = runtime, tools
        overlay_text = self._effective_turn_overlay_text(state)
        default_overlay_text = self._frontdoor_default_overlay_text(state)
        if overlay_text:
            combined_overlay = "\n\n".join(
                part
                for part in (overlay_text, default_overlay_text)
                if str(part or "").strip()
            ).strip()
            return {"system_overlay": combined_overlay}
        return {"system_overlay": default_overlay_text}

    def visible_langchain_tools(self, *, state, runtime) -> list[Any]:
        return list(self._build_langchain_tools_for_state(state=state, runtime=runtime))

    @staticmethod
    def _message_role(value: Any) -> str:
        if isinstance(value, dict):
            role = str(value.get("role") or "").strip().lower()
        else:
            role = str(getattr(value, "type", "") or getattr(value, "role", "")).strip().lower()
        if role == "human":
            return "user"
        if role == "ai":
            return "assistant"
        return role

    def _message_record(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        role = self._message_role(value)
        record: dict[str, Any] = {
            "role": role,
            "content": getattr(value, "content", ""),
        }
        if role == "assistant":
            tool_calls = list(getattr(value, "tool_calls", None) or [])
            if tool_calls:
                record["tool_calls"] = self._assistant_tool_calls_from_payloads(
                    self._tool_call_payloads_from_calls(tool_calls)
                )
        if role == "tool":
            record["tool_call_id"] = str(getattr(value, "tool_call_id", "") or "")
            record["name"] = str(getattr(value, "name", "") or "")
            status = getattr(value, "status", None)
            if status is not None:
                record["status"] = str(status or "")
        return record

    def _state_message_records(self, state_or_messages: Any) -> list[dict[str, Any]]:
        if isinstance(state_or_messages, dict):
            raw_messages = list(state_or_messages.get("messages") or [])
        else:
            raw_messages = list(state_or_messages or [])
        return [self._message_record(item) for item in raw_messages]

    def _replace_messages_update(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *list(messages or [])]}

    def _request_messages_for_state(
        self,
        *,
        state: dict[str, Any],
    ) -> tuple[CoreSystemMessage | None, list[BaseMessage]]:
        records = self._state_message_records(state)
        system_message = None
        if records and str(records[0].get("role") or "").strip().lower() == "system":
            system_message = CoreSystemMessage(content=records[0].get("content", ""))
            records = records[1:]
        return system_message, list(convert_to_messages(records))

    def _tool_result_payload(self, tool_message: dict[str, Any]) -> dict[str, Any]:
        content = tool_message.get("content", "")
        if isinstance(content, dict):
            payload = dict(content)
        else:
            text = str(content or "")
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            payload = dict(parsed) if isinstance(parsed, dict) else {}
            if not payload:
                payload = {"result_text": text}
        result_text = str(payload.get("result_text") or content or "")
        payload["result_text"] = result_text
        payload["status"] = str(payload.get("status") or self._tool_status(result_text))
        return payload

    async def _prepare_turn_state(
        self,
        *,
        user_input: dict[str, Any],
        runtime_context: CeoRuntimeContext,
    ) -> dict[str, Any]:
        if (
            not hasattr(self._loop, "sessions")
            or getattr(runtime_context, "session", None) is None
            or not hasattr(getattr(runtime_context, "session", None), "state")
        ):
            payload = initial_persistent_state(user_input=user_input)
            payload["agent_runtime"] = "create_agent"
            return payload
        prepared = await self._graph_prepare_turn(
            initial_persistent_state(user_input=user_input),
            runtime=type("RuntimeShim", (), {"context": runtime_context})(),
        )
        prepared["agent_runtime"] = "create_agent"
        return dict(prepared)

    async def _postprocess_completed_tool_cycle(
        self,
        *,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        tool_call_payloads = list(state.get("tool_call_payloads") or [])
        if not tool_call_payloads:
            return None

        messages = self._state_message_records(state)
        if not messages:
            return None

        trailing_tools: list[dict[str, Any]] = []
        index = len(messages) - 1
        while index >= 0 and str(messages[index].get("role") or "").strip().lower() == "tool":
            trailing_tools.append(messages[index])
            index -= 1
        trailing_tools.reverse()
        if len(trailing_tools) < len(tool_call_payloads):
            return None

        compacted_state = await self._summarize_messages(messages=messages, state=state)
        messages = list(compacted_state.get("messages") or [])
        used_tools = list(state.get("used_tools") or [])
        used_tools.extend(
            [
                str(payload.get("name") or "").strip()
                for payload in tool_call_payloads
                if str(payload.get("name") or "").strip()
                and str(payload.get("name") or "").strip() not in self._CONTROL_TOOL_NAMES
            ]
        )
        route_kind = self._route_kind_for_turn(
            used_tools=used_tools,
            default=str(state.get("route_kind") or "direct_reply"),
        )

        tool_results = [
            {
                "tool_name": str(message.get("name") or payload.get("name") or ""),
                **self._tool_result_payload(message),
            }
            for message, payload in zip(
                trailing_tools[-len(tool_call_payloads):],
                tool_call_payloads,
                strict=False,
            )
        ]
        frontdoor_stage_state = self._frontdoor_stage_state_after_tool_cycle(
            state,
            tool_call_payloads=tool_call_payloads,
            tool_results=tool_results,
        )
        substantive_tool_names = [
            str(payload.get("name") or "").strip()
            for payload in tool_call_payloads
            if str(payload.get("name") or "").strip()
            and str(payload.get("name") or "").strip() not in self._CONTROL_TOOL_NAMES
        ]
        successful_dispatch = next(
            (
                item
                for item in tool_results
                if str(item.get("tool_name") or "").strip() == "create_async_task"
                and str(item.get("status") or "").strip().lower() == "success"
            ),
            None,
        )

        result = {
            **self._replace_messages_update(messages),
            "frontdoor_stage_state": frontdoor_stage_state,
            "used_tools": used_tools,
            "route_kind": route_kind,
            "analysis_text": "",
            "tool_call_payloads": [],
            "verified_task_ids": [],
            "synthetic_tool_calls_used": False,
        }
        if successful_dispatch is not None and set(substantive_tool_names) == {"create_async_task"}:
            verified_task_id = self._verified_dispatch_task_id(
                str(successful_dispatch.get("result_text") or "")
            )
            if not verified_task_id:
                result["final_output"] = self._unverified_task_dispatch_reply(
                    task_id=self._extract_task_id(
                        str(successful_dispatch.get("result_text") or "")
                    )
                )
                result["route_kind"] = "direct_reply"
                result["jump_to"] = "end"
                return result
            result["verified_task_ids"] = [verified_task_id]
            result["route_kind"] = "task_dispatch"
            result["tool_names"] = []
            result["repair_overlay_text"] = self._verified_dispatch_reply_overlay(task_id=verified_task_id)
        return result

    def _middleware(self) -> list[Any]:
        return [
            CeoTurnLifecycleMiddleware(runner=self),
            CeoToolExposureMiddleware(runner=self),
            CeoPromptAssemblyMiddleware(runner=self),
            CeoModelOutputMiddleware(runner=self),
            CeoApprovalMiddleware(runner=self),
        ]

    def _get_agent(self):
        if self._compiled_graph is not None:
            return self._compiled_graph
        if self._agent is None:
            model_refs = self._resolve_ceo_model_refs()
            self._agent = create_agent(
                model=G3kuChatModelAdapter(
                    chat_backend=self._resolve_chat_backend(),
                    model_refs=list(model_refs or []),
                ),
                tools=[],
                name="ceo_frontdoor",
                system_prompt=SystemMessage(content="You are the CEO frontdoor agent."),
                checkpointer=getattr(self._loop, "_checkpointer", None),
                store=getattr(self._loop, "_store", None),
                state_schema=CeoPersistentState,
                context_schema=CeoRuntimeContext,
                middleware=self._middleware(),
            )
        return self._agent

    async def run_turn(self, *, user_input, session, on_progress=None) -> str:
        await self._ensure_ready()
        setattr(session, "_last_route_kind", "direct_reply")
        session_key = str(getattr(getattr(session, "state", None), "session_key", "") or "").strip()
        runtime_context = CeoRuntimeContext(
            loop=self._loop,
            session=session,
            session_key=session_key,
            on_progress=on_progress,
        )
        payload = await self._prepare_turn_state(
            user_input={
                "content": getattr(user_input, "content", ""),
                "metadata": dict(getattr(user_input, "metadata", {}) or {}),
            },
            runtime_context=runtime_context,
        )
        graph_output = await self._get_agent().ainvoke(
            payload,
            config=self._thread_config(session_key),
            context=runtime_context,
            version="v2",
        )
        values = self._unwrap_graph_output(graph_output)
        setattr(session, "_last_route_kind", str(values.get("route_kind") or "direct_reply"))
        setattr(session, "_last_verified_task_ids", list(values.get("verified_task_ids") or []))
        self._sync_runtime_session_frontdoor_state(state=values, session=session)
        return str(values.get("final_output") or "")

    async def resume_turn(self, *, session, resume_value, on_progress=None) -> str:
        await self._ensure_ready()
        session_key = str(getattr(getattr(session, "state", None), "session_key", "") or "").strip()
        graph_output = await self._get_agent().ainvoke(
            Command(resume=resume_value),
            config=self._thread_config(session_key),
            context=CeoRuntimeContext(
                loop=self._loop,
                session=session,
                session_key=session_key,
                on_progress=on_progress,
            ),
            version="v2",
        )
        values = self._unwrap_graph_output(graph_output)
        setattr(session, "_last_route_kind", str(values.get("route_kind") or "direct_reply"))
        setattr(session, "_last_verified_task_ids", list(values.get("verified_task_ids") or []))
        self._sync_runtime_session_frontdoor_state(state=values, session=session)
        return str(values.get("final_output") or "")
