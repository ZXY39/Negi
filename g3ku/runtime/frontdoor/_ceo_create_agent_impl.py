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

from ._ceo_langgraph_impl import CeoFrontDoorRuntimeOps
from .ceo_agent_middleware import (
    CeoApprovalMiddleware,
    CeoModelOutputMiddleware,
    CeoPromptAssemblyMiddleware,
    CeoToolExposureMiddleware,
    CeoTurnLifecycleMiddleware,
)
from .state_models import (
    CeoFrontdoorInterrupted,
    CeoPendingInterrupt,
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
        if overlay_text:
            return {"system_overlay": overlay_text}
        summary_text = str(state.get("summary_text") or "").strip()
        system_overlay = "Use the existing CEO layered context rules."
        if summary_text:
            system_overlay = f"{system_overlay}\n\n{summary_text}"
        return {"system_overlay": system_overlay}

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
            "summary_text": str(compacted_state.get("summary_text") or ""),
            "summary_payload": dict(compacted_state.get("summary_payload") or {}),
            "summary_version": int(compacted_state.get("summary_version") or 0),
            "summary_model_key": str(compacted_state.get("summary_model_key") or ""),
            "used_tools": used_tools,
            "route_kind": route_kind,
            "analysis_text": "",
            "tool_call_payloads": [],
            "synthetic_tool_calls_used": False,
        }
        if successful_dispatch is not None and set(substantive_tool_names) == {"create_async_task"}:
            result["final_output"] = self._task_dispatch_reply(
                result_text=str(successful_dispatch.get("result_text") or "")
            )
            result["jump_to"] = "end"
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

    def _get_compiled_graph(self):
        return self._compiled_graph if self._compiled_graph is not None else self._get_agent()

    async def _ensure_ready(self) -> None:
        ensure_ready = getattr(self._loop, "_ensure_checkpointer_ready", None)
        if callable(ensure_ready):
            result = ensure_ready()
            if hasattr(result, "__await__"):
                await result

    @staticmethod
    def _thread_config(session_key: str) -> dict[str, object]:
        return {"configurable": {"thread_id": str(session_key or "").strip()}}

    @staticmethod
    def _checkpoint_safe_value(value: Any) -> Any:
        if value is None or isinstance(value, str | int | float | bool):
            return value
        if isinstance(value, dict):
            return {
                str(key): CreateAgentCeoFrontDoorRunner._checkpoint_safe_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list | tuple | set):
            return [CreateAgentCeoFrontDoorRunner._checkpoint_safe_value(item) for item in value]
        return str(value)

    @classmethod
    def _unwrap_graph_output(cls, graph_output: Any) -> dict[str, Any]:
        interrupts = [
            CeoPendingInterrupt(
                interrupt_id=str(getattr(item, "id", "") or ""),
                value=cls._checkpoint_safe_value(getattr(item, "value", None)),
            )
            for item in list(getattr(graph_output, "interrupts", ()) or ())
        ]
        values = cls._checkpoint_safe_value(dict(getattr(graph_output, "value", graph_output) or {}))
        if not isinstance(values, dict):
            values = {}
        if interrupts:
            first_interrupt_value = interrupts[0].value if interrupts else None
            interrupt_state = first_interrupt_value if isinstance(first_interrupt_value, dict) else {}
            interrupt_approval_request = interrupt_state.get("approval_request")
            if not isinstance(values.get("approval_request"), dict) and isinstance(first_interrupt_value, dict):
                if isinstance(interrupt_approval_request, dict):
                    values["approval_request"] = dict(interrupt_approval_request)
                else:
                    values["approval_request"] = dict(first_interrupt_value)
            if not list(values.get("tool_call_payloads") or []):
                interrupt_payloads = list(interrupt_state.get("tool_call_payloads") or [])
                if interrupt_payloads:
                    values["tool_call_payloads"] = interrupt_payloads
                if not list(values.get("tool_call_payloads") or []):
                    if isinstance(interrupt_approval_request, dict):
                        interrupt_tool_calls = list(interrupt_approval_request.get("tool_calls") or [])
                        if interrupt_tool_calls:
                            values["tool_call_payloads"] = interrupt_tool_calls
                if not list(values.get("tool_call_payloads") or []):
                    approval_request = values.get("approval_request")
                    if isinstance(approval_request, dict):
                        tool_call_payloads = list(approval_request.get("tool_calls") or [])
                        if tool_call_payloads:
                            values["tool_call_payloads"] = tool_call_payloads
            raise CeoFrontdoorInterrupted(interrupts=interrupts, values=values)
        return values

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
        return str(values.get("final_output") or "")
