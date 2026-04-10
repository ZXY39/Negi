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
from .prompt_cache_contract import DEFAULT_CACHE_FAMILY_REVISION, build_frontdoor_prompt_contract


class CreateAgentCeoFrontDoorRunner(CeoFrontDoorRuntimeOps):
    def __init__(self, *, loop: Any) -> None:
        super().__init__(loop=loop)
        self._agent = None

    def build_prompt_context(self, *, state, runtime, tools) -> dict[str, str]:
        _ = runtime, tools
        if list(state.get("dynamic_appendix_messages") or []):
            overlay_text = str(state.get("repair_overlay_text") or "").strip()
        else:
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
        additional_kwargs = getattr(value, "additional_kwargs", None)
        if isinstance(additional_kwargs, dict):
            metadata = additional_kwargs.get("metadata")
            if isinstance(metadata, dict) and metadata:
                record["metadata"] = dict(metadata)
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

    @staticmethod
    def _overlay_text_from_messages(messages: list[dict[str, Any]] | None) -> str:
        return "\n\n".join(
            str(message.get("content") or "").strip()
            for message in list(messages or [])
            if str(message.get("content") or "").strip()
        ).strip()

    @staticmethod
    def _prompt_cache_family_revision(state: dict[str, Any]) -> str:
        return str(state.get("cache_family_revision") or "").strip() or DEFAULT_CACHE_FAMILY_REVISION

    def _fallback_request_records(
        self,
        *,
        fallback_system_message: Any = None,
        fallback_messages: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if fallback_system_message is not None:
            records.append(self._message_record(fallback_system_message))
        records.extend(self._message_record(item) for item in list(fallback_messages or []))
        return records

    @staticmethod
    def _leading_system_record(records: list[dict[str, Any]] | None) -> dict[str, Any] | None:
        if not records:
            return None
        first = dict(records[0])
        if str(first.get("role") or "").strip().lower() != "system":
            return None
        return first

    def _effective_system_record(
        self,
        *,
        state: dict[str, Any],
        fallback_system_message: Any = None,
        fallback_messages: list[Any] | None = None,
    ) -> dict[str, Any] | None:
        candidates = (
            [self._message_record(item) for item in list(state.get("stable_messages") or [])],
            self._state_message_records(state),
            self._fallback_request_records(
                fallback_system_message=fallback_system_message,
                fallback_messages=fallback_messages,
            ),
        )
        for records in candidates:
            system_record = self._leading_system_record(records)
            if system_record is not None:
                return system_record
        return None

    def _with_effective_system_prefix(
        self,
        *,
        records: list[dict[str, Any]],
        effective_system_record: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        normalized_records = [self._message_record(item) for item in list(records or [])]
        if effective_system_record is None:
            return normalized_records
        if self._leading_system_record(normalized_records) == effective_system_record:
            return normalized_records
        if self._leading_system_record(normalized_records) is not None:
            normalized_records = normalized_records[1:]
        return [dict(effective_system_record), *normalized_records]

    def _stable_message_records_for_state(
        self,
        *,
        state: dict[str, Any],
        fallback_system_message: Any = None,
        fallback_messages: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        raw_stable_messages = list(state.get("stable_messages") or [])
        effective_system_record = self._effective_system_record(
            state=state,
            fallback_system_message=fallback_system_message,
            fallback_messages=fallback_messages,
        )
        if raw_stable_messages:
            return self._with_effective_system_prefix(
                records=[self._message_record(item) for item in raw_stable_messages],
                effective_system_record=effective_system_record,
            )
        live_records = self._state_message_records(state)
        if live_records:
            return self._with_effective_system_prefix(
                records=live_records,
                effective_system_record=effective_system_record,
            )
        return self._with_effective_system_prefix(
            records=self._fallback_request_records(
                fallback_system_message=fallback_system_message,
                fallback_messages=fallback_messages,
            ),
            effective_system_record=effective_system_record,
        )

    def _live_request_message_records_for_state(
        self,
        *,
        state: dict[str, Any],
        fallback_system_message: Any = None,
        fallback_messages: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        effective_system_record = self._effective_system_record(
            state=state,
            fallback_system_message=fallback_system_message,
            fallback_messages=fallback_messages,
        )
        live_records = self._state_message_records(state)
        if live_records:
            return self._with_effective_system_prefix(
                records=live_records,
                effective_system_record=effective_system_record,
            )
        return self._with_effective_system_prefix(
            records=self._fallback_request_records(
                fallback_system_message=fallback_system_message,
                fallback_messages=fallback_messages,
            ),
            effective_system_record=effective_system_record,
        )

    def _dynamic_appendix_message_records_for_state(
        self,
        *,
        state: dict[str, Any],
        overlay_text: str = "",
    ) -> list[dict[str, Any]]:
        raw_dynamic_messages = list(state.get("dynamic_appendix_messages") or [])
        if raw_dynamic_messages:
            return [self._message_record(item) for item in raw_dynamic_messages]
        normalized_overlay_text = str(overlay_text or "").strip()
        if not normalized_overlay_text:
            return []
        return [{"role": "assistant", "content": normalized_overlay_text}]

    def _render_request_records(
        self,
        records: list[dict[str, Any]] | None,
    ) -> tuple[CoreSystemMessage | None, list[BaseMessage]]:
        normalized_records = [self._message_record(item) for item in list(records or [])]
        system_message = None
        if normalized_records and str(normalized_records[0].get("role") or "").strip().lower() == "system":
            system_message = CoreSystemMessage(content=normalized_records[0].get("content", ""))
            normalized_records = normalized_records[1:]
        return system_message, list(convert_to_messages(normalized_records))

    def _replace_messages_update(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *list(messages or [])]}

    def _request_messages_for_state(
        self,
        *,
        state: dict[str, Any],
        fallback_system_message: Any = None,
        fallback_messages: list[Any] | None = None,
        session_key: str | None = None,
        provider_model: str = "",
        tool_schemas: list[dict[str, Any]] | None = None,
        overlay_text: str = "",
        overlay_section_count: int | None = None,
    ) -> tuple[CoreSystemMessage | None, list[BaseMessage]]:
        contract = self._frontdoor_prompt_contract(
            state=state,
            provider_model=provider_model,
            tool_schemas=tool_schemas,
            overlay_text=overlay_text,
            fallback_system_message=fallback_system_message,
            fallback_messages=fallback_messages,
            session_key=session_key,
            overlay_section_count=overlay_section_count,
        )
        return self._render_request_records(contract.request_messages)

    def _frontdoor_prompt_contract(
        self,
        *,
        state: dict[str, Any],
        provider_model: str,
        tool_schemas: list[dict[str, Any]] | None,
        overlay_text: str = "",
        fallback_system_message: Any = None,
        fallback_messages: list[Any] | None = None,
        session_key: str | None = None,
        overlay_section_count: int | None = None,
    ):
        return build_frontdoor_prompt_contract(
            scope="ceo_frontdoor",
            provider_model=provider_model,
            stable_messages=self._stable_message_records_for_state(
                state=state,
                fallback_system_message=fallback_system_message,
                fallback_messages=fallback_messages,
            ),
            dynamic_appendix_messages=self._dynamic_appendix_message_records_for_state(
                state=state,
                overlay_text=overlay_text,
            ),
            live_request_messages=self._live_request_message_records_for_state(
                state=state,
                fallback_system_message=fallback_system_message,
                fallback_messages=fallback_messages,
            ),
            tool_schemas=list(tool_schemas or []),
            cache_family_revision=self._prompt_cache_family_revision(state),
            session_key=str(session_key or state.get("session_key") or "").strip(),
            overlay_text=overlay_text,
            overlay_section_count=overlay_section_count,
        )

    def _refresh_prompt_cache_state(self, *, state: dict[str, Any]) -> dict[str, Any]:
        updated_state = dict(state or {})
        model_refs = list(updated_state.get("model_refs") or self._resolve_ceo_model_refs() or [])
        provider_model = str(model_refs[0] if model_refs else "").strip()
        tool_schemas = []
        try:
            tool_schemas = self._selected_tool_schemas(list(updated_state.get("tool_names") or []))
        except Exception:
            tool_schemas = []
        contract = self._frontdoor_prompt_contract(
            state=updated_state,
            provider_model=provider_model,
            tool_schemas=tool_schemas,
            overlay_text=str(updated_state.get("turn_overlay_text") or "").strip(),
            session_key=str(updated_state.get("session_key") or "").strip(),
            overlay_section_count=len(list(updated_state.get("dynamic_appendix_messages") or [])),
        )
        return {
            **updated_state,
            "messages": list(contract.request_messages),
            "stable_messages": list(contract.stable_messages),
            "dynamic_appendix_messages": list(contract.dynamic_appendix_messages),
            "cache_family_revision": contract.cache_family_revision,
            "prompt_cache_key": contract.prompt_cache_key,
            "prompt_cache_diagnostics": dict(contract.diagnostics),
        }

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
        refreshed = self._refresh_prompt_cache_state(state=dict(prepared))
        refreshed["agent_runtime"] = "create_agent"
        return refreshed

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
        successful_continuation = next(
            (
                item
                for item in tool_results
                if str(item.get("tool_name") or "").strip() == "continue_task"
                and str(item.get("status") or "").strip().lower() in {"success", "completed"}
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
        if successful_continuation is not None and set(substantive_tool_names) == {"continue_task"}:
            verified = self._verified_continuation_result(successful_continuation)
            if not verified:
                payload = self._continue_task_result_payload(successful_continuation)
                result["final_output"] = self._unverified_task_continuation_reply(
                    mode=str(payload.get("mode") or ""),
                    target_task_id=self._normalize_task_id_value(payload.get("target_task_id")),
                    continuation_task_id=self._normalize_task_id_value(
                        self._json_object_payload(payload.get("continuation_task") or {}).get("task_id")
                    ),
                    resumed_task_id=self._normalize_task_id_value(
                        self._json_object_payload(payload.get("resumed_task") or {}).get("task_id")
                    ),
                )
                result["route_kind"] = "direct_reply"
                result["jump_to"] = "end"
                return result
            result["route_kind"] = "task_continuation"
            result["tool_names"] = []
            result["repair_overlay_text"] = self._verified_continuation_reply_overlay(
                mode=verified["mode"],
                target_task_id=verified["target_task_id"],
                continuation_task_id=verified["continuation_task_id"],
                resumed_task_id=verified["resumed_task_id"],
            )
        return result

    def _middleware(self) -> list[Any]:
        return [
            CeoTurnLifecycleMiddleware(runner=self),
            CeoToolExposureMiddleware(runner=self),
            CeoPromptAssemblyMiddleware(runner=self),
            CeoApprovalMiddleware(runner=self),
            CeoModelOutputMiddleware(runner=self),
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
