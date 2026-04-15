from __future__ import annotations

import inspect
import json
from typing import Any

from langchain_core.messages import BaseMessage, RemoveMessage, convert_to_messages
from langchain_core.messages import SystemMessage as CoreSystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command

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
from .tool_contract import build_frontdoor_tool_contract, upsert_frontdoor_tool_contract_message


class CreateAgentCeoFrontDoorRunner(CeoFrontDoorRuntimeOps):
    def __init__(self, *, loop: Any) -> None:
        super().__init__(loop=loop)
        self._agent = None
        self._agent_checkpointer_ref = None

    def _invalidate_cached_runtime_bindings_if_stale(self) -> bool:
        current_checkpointer = getattr(self._loop, "_checkpointer", None)
        cached_checkpointer = getattr(self, "_agent_checkpointer_ref", None)
        if self._agent is None and self._compiled_graph is None:
            self._agent_checkpointer_ref = current_checkpointer
            return False
        if cached_checkpointer is current_checkpointer:
            return False
        self._agent = None
        self._compiled_graph = None
        self._agent_checkpointer_ref = current_checkpointer
        return True

    async def _ensure_runtime_bindings_ready(self) -> bool:
        ensure_ready = getattr(self._loop, "_ensure_checkpointer_ready", None)
        if callable(ensure_ready):
            result = ensure_ready()
            if inspect.isawaitable(result):
                await result
        changed = self._invalidate_cached_runtime_bindings_if_stale()
        checkpointer = getattr(self._loop, "_checkpointer", None)
        is_active = getattr(self._loop, "_sqlite_checkpointer_is_active", None)
        if callable(is_active) and checkpointer is not None and not is_active(checkpointer):
            if not changed:
                self._agent = None
                self._compiled_graph = None
                self._agent_checkpointer_ref = checkpointer
                changed = True
            if callable(ensure_ready):
                result = ensure_ready()
                if inspect.isawaitable(result):
                    await result
            self._agent_checkpointer_ref = getattr(self._loop, "_checkpointer", None)
        return changed

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
            record = dict(value)
            role = self._message_role(record)
            if role == "assistant":
                tool_calls = list(record.get("tool_calls") or [])
                if tool_calls:
                    record["tool_calls"] = self._assistant_tool_calls_from_payloads(
                        self._tool_call_payloads_from_calls(tool_calls)
                    )
            return record
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
        required_list_fields = (
            "tool_names",
            "candidate_tool_names",
            "hydrated_tool_names",
            "visible_skill_ids",
            "candidate_skill_ids",
            "rbac_visible_tool_names",
            "rbac_visible_skill_ids",
        )
        missing_fields = [
            field
            for field in required_list_fields
            if field not in state or not isinstance(state.get(field), list)
        ]
        if missing_fields:
            raise RuntimeError(
                "运行时工具合同损坏/缺失：前门状态缺少 canonical 合同字段 "
                + ", ".join(missing_fields)
            )
        raw_dynamic_messages = list(state.get("dynamic_appendix_messages") or [])
        normalized_dynamic_messages = [self._message_record(item) for item in raw_dynamic_messages]
        normalized_overlay_text = str(overlay_text or "").strip()
        if not normalized_dynamic_messages and normalized_overlay_text:
            normalized_dynamic_messages = [{"role": "assistant", "content": normalized_overlay_text}]
        callable_tool_names = self._frontdoor_callable_tool_names_for_state(
            state,
            tool_names=list(state.get("tool_names") or []),
        )
        frontdoor_tool_contract = build_frontdoor_tool_contract(
            callable_tool_names=list(callable_tool_names),
            candidate_tool_names=list(state.get("candidate_tool_names") or []),
            hydrated_tool_names=list(state.get("hydrated_tool_names") or []),
            frontdoor_stage_state=dict(state.get("frontdoor_stage_state") or {}),
            visible_skill_ids=list(state.get("visible_skill_ids") or []),
            candidate_skill_ids=list(state.get("candidate_skill_ids") or []),
            rbac_visible_tool_names=list(state.get("rbac_visible_tool_names") or []),
            rbac_visible_skill_ids=list(state.get("rbac_visible_skill_ids") or []),
            contract_revision=self._prompt_cache_family_revision(state),
        )
        return upsert_frontdoor_tool_contract_message(normalized_dynamic_messages, frontdoor_tool_contract)

    def _render_request_records(
        self,
        records: list[dict[str, Any]] | None,
    ) -> tuple[CoreSystemMessage | None, list[BaseMessage]]:
        normalized_records: list[dict[str, Any]] = []
        for item in list(records or []):
            record = self._message_record(item)
            content = record.get("content", "")
            if isinstance(content, dict):
                record["content"] = json.dumps(content, ensure_ascii=False, indent=2)
            normalized_records.append(record)
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
        callable_tool_names = self._frontdoor_callable_tool_names_for_state(
            updated_state,
            tool_names=list(updated_state.get("tool_names") or []),
        )
        model_refs = list(updated_state.get("model_refs") or self._resolve_ceo_model_refs() or [])
        provider_model = str(model_refs[0] if model_refs else "").strip()
        tool_schemas = []
        try:
            tool_schemas = self._selected_tool_schemas(list(callable_tool_names))
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

    @staticmethod
    def _normalized_tool_name_state_list(raw: Any) -> list[str]:
        normalized: list[str] = []
        for item in list(raw or []):
            name = str(item or "").strip()
            if name and name not in normalized:
                normalized.append(name)
        return normalized

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

        messages = list(messages or [])
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
        verified_task_ids: list[str] = []
        repair_overlay_text: str | None = None
        for tool_result in tool_results:
            tool_name = str(tool_result.get("tool_name") or "").strip()
            result_text = str(tool_result.get("result_text") or "").strip()
            if tool_name != "create_async_task":
                continue
            task_id = self._extract_task_id(result_text)
            if not task_id or not self._task_id_exists(task_id):
                continue
            verified_task_ids = [task_id]
            if route_kind == "task_dispatch":
                repair_overlay_text = (
                    f"Dispatch result is already available. Reply naturally based on the verified task id {task_id}."
                )
            break
        substantive_tool_names = [
            str(payload.get("name") or "").strip()
            for payload in tool_call_payloads
            if str(payload.get("name") or "").strip()
            and str(payload.get("name") or "").strip() not in self._CONTROL_TOOL_NAMES
        ]
        result = {
            **self._replace_messages_update(messages),
            "frontdoor_stage_state": frontdoor_stage_state,
            "used_tools": used_tools,
            "route_kind": route_kind,
            "analysis_text": "",
            "tool_call_payloads": [],
            "verified_task_ids": verified_task_ids,
            "synthetic_tool_calls_used": False,
        }
        should_update_frontdoor_tool_state = (
            "candidate_tool_names" in state
            or "hydrated_tool_names" in state
        )
        if should_update_frontdoor_tool_state:
            result["tool_names"] = self._normalized_tool_name_state_list(state.get("tool_names"))
            result["candidate_tool_names"] = self._normalized_tool_name_state_list(state.get("candidate_tool_names"))
            result["hydrated_tool_names"] = self._normalized_tool_name_state_list(state.get("hydrated_tool_names"))
            result["visible_skill_ids"] = self._normalized_tool_name_state_list(state.get("visible_skill_ids"))
            result["candidate_skill_ids"] = self._normalized_tool_name_state_list(state.get("candidate_skill_ids"))
            result["rbac_visible_tool_names"] = self._normalized_tool_name_state_list(state.get("rbac_visible_tool_names"))
            result["rbac_visible_skill_ids"] = self._normalized_tool_name_state_list(state.get("rbac_visible_skill_ids"))
        if repair_overlay_text:
            result["repair_overlay_text"] = repair_overlay_text
        return result

    def _message_replace_update(self, messages: list[Any] | None) -> dict[str, Any]:
        return self._replace_messages_update(self._state_message_records(messages))

    async def _node_prepare_turn(self, state, runtime) -> dict[str, Any]:
        prepared = await self._graph_prepare_turn(state, runtime=runtime)
        self._sync_runtime_session_frontdoor_state(
            state={**dict(state or {}), **dict(prepared or {})},
            runtime=runtime,
        )
        return prepared

    async def _node_call_model(self, state, runtime) -> dict[str, Any]:
        return await self._graph_call_model(state, runtime=runtime)

    async def _node_normalize_model_output(self, state, runtime) -> dict[str, Any]:
        current_state = dict(state or {})
        normalized = await self._graph_normalize_model_output(current_state, runtime=runtime)
        next_step = str(normalized.get("next_step") or "").strip()
        if next_step == "review_tool_calls":
            preview_state = {**current_state, **dict(normalized or {})}
            preview_frontdoor_stage_state, preview_compression_state, preview_semantic_context_state, _preview_hydrated_tool_names = self._runtime_session_frontdoor_state(
                preview_state,
                preview_pending_tool_round=True,
            )
            self._sync_runtime_session_frontdoor_state(
                state={
                    **preview_state,
                    "frontdoor_stage_state": preview_frontdoor_stage_state,
                    "compression_state": preview_compression_state,
                    "semantic_context_state": preview_semantic_context_state,
                },
                runtime=runtime,
            )
        elif next_step in {"call_model", "finalize"}:
            self._sync_runtime_session_frontdoor_state(
                state={**current_state, **dict(normalized or {})},
                runtime=runtime,
            )
        return normalized

    def _node_review_tool_calls(self, state, runtime) -> dict[str, Any]:
        reviewed = self._graph_review_tool_calls(state, runtime=runtime)
        self._sync_runtime_session_frontdoor_state(
            state={**dict(state or {}), **dict(reviewed or {})},
            runtime=runtime,
        )
        return reviewed

    async def _node_execute_tools(self, state, runtime) -> dict[str, Any]:
        executed = await self._graph_execute_tools(state, runtime=runtime)
        messages = self._state_message_records(executed.get("messages") or [])
        update = dict(executed or {})
        if "messages" in update:
            update.pop("messages", None)
            update.update(self._message_replace_update(messages))
        self._sync_runtime_session_frontdoor_state(
            state={**dict(state or {}), **dict(executed or {}), "messages": messages},
            runtime=runtime,
        )
        return update

    async def _node_finalize_turn(self, state, runtime) -> dict[str, Any]:
        finalized = await self._graph_finalize_turn(state)
        messages = self._state_message_records(finalized.get("messages") or [])
        update = dict(finalized or {})
        if "messages" in update:
            update.pop("messages", None)
            update.update(self._message_replace_update(messages))
        self._sync_runtime_session_frontdoor_state(
            state={**dict(state or {}), **dict(finalized or {}), "messages": messages},
            runtime=runtime,
        )
        return update

    def _middleware(self) -> list[Any]:
        # Retained only for compatibility tests around prompt assembly and cache diagnostics.
        # The production CEO/frontdoor path now runs exclusively on the explicit StateGraph.
        return [
            CeoTurnLifecycleMiddleware(runner=self),
            CeoToolExposureMiddleware(runner=self),
            CeoPromptAssemblyMiddleware(runner=self),
            CeoApprovalMiddleware(runner=self),
            CeoModelOutputMiddleware(runner=self),
        ]

    def _build_compiled_graph(self):
        builder = StateGraph(CeoPersistentState, context_schema=CeoRuntimeContext)
        builder.add_node("prepare_turn", self._node_prepare_turn)
        builder.add_node("call_model", self._node_call_model)
        builder.add_node("normalize_model_output", self._node_normalize_model_output)
        builder.add_node("review_tool_calls", self._node_review_tool_calls)
        builder.add_node("execute_tools", self._node_execute_tools)
        builder.add_node("finalize", self._node_finalize_turn)
        builder.add_edge(START, "prepare_turn")
        builder.add_edge("prepare_turn", "call_model")
        builder.add_edge("call_model", "normalize_model_output")
        builder.add_conditional_edges(
            "normalize_model_output",
            self._graph_next_step,
            {
                "call_model": "call_model",
                "review_tool_calls": "review_tool_calls",
                "execute_tools": "execute_tools",
                "finalize": "finalize",
            },
        )
        builder.add_conditional_edges(
            "review_tool_calls",
            self._graph_next_step,
            {
                "call_model": "call_model",
                "review_tool_calls": "review_tool_calls",
                "execute_tools": "execute_tools",
                "finalize": "finalize",
            },
        )
        builder.add_conditional_edges(
            "execute_tools",
            self._graph_next_step,
            {
                "call_model": "call_model",
                "review_tool_calls": "review_tool_calls",
                "execute_tools": "execute_tools",
                "finalize": "finalize",
            },
        )
        builder.add_edge("finalize", END)
        return builder.compile(
            checkpointer=getattr(self._loop, "_checkpointer", None),
            store=getattr(self._loop, "_store", None),
            name="ceo_frontdoor",
        )

    def _get_agent(self):
        if self._compiled_graph is not None:
            return self._compiled_graph
        if self._agent is not None:
            return self._agent
        if self._compiled_graph is None:
            self._compiled_graph = self._build_compiled_graph()
            self._agent_checkpointer_ref = getattr(self._loop, "_checkpointer", None)
        return self._compiled_graph

    async def run_turn(self, *, user_input, session, on_progress=None) -> str:
        await self._ensure_runtime_bindings_ready()
        setattr(session, "_last_route_kind", "direct_reply")
        session_key = str(getattr(getattr(session, "state", None), "session_key", "") or "").strip()
        runtime_context = CeoRuntimeContext(
            loop=self._loop,
            session=session,
            session_key=session_key,
            on_progress=on_progress,
        )
        payload = initial_persistent_state(
            user_input={
                "content": getattr(user_input, "content", ""),
                "metadata": dict(getattr(user_input, "metadata", {}) or {}),
            },
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
        await self._ensure_runtime_bindings_ready()
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
