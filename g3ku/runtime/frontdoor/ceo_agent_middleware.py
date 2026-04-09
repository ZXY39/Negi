from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse, hook_config
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain.messages import AIMessage, SystemMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command, interrupt

from g3ku.json_schema_utils import get_attached_raw_parameters_schema
from g3ku.providers.fallback import PUBLIC_PROVIDER_FAILURE_MESSAGE


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


def _message_content(value: Any) -> Any:
    if isinstance(value, dict):
        content = value.get("content", "")
    else:
        content = getattr(value, "content", "")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text", item.get("content", ""))
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n\n".join(parts).strip()
    return content


def _tool_schema(tool: Any) -> dict[str, Any] | None:
    if isinstance(tool, dict):
        return dict(tool)
    name = str(getattr(tool, "name", "") or "").strip()
    if not name:
        return None
    parameters = get_attached_raw_parameters_schema(tool) or {}
    if not parameters:
        args_schema = getattr(tool, "args_schema", None)
        if args_schema is not None and hasattr(args_schema, "model_json_schema"):
            parameters = dict(args_schema.model_json_schema() or {})
    return {
        "name": name,
        "description": str(getattr(tool, "description", "") or ""),
        "parameters": parameters,
    }


class CeoPromptAssemblyMiddleware(AgentMiddleware):
    def __init__(self, *, runner: Any) -> None:
        super().__init__()
        self._runner = runner

    def _prepare_request_and_update(self, request: ModelRequest) -> tuple[ModelRequest, dict[str, Any]]:
        state = dict(request.state or {})
        prompt_context = self._runner.build_prompt_context(
            state=state,
            runtime=request.runtime,
            tools=request.tools,
        )
        overlay_text = str(prompt_context.get("system_overlay") or "").strip()
        tool_schemas = [
            schema
            for schema in (
                _tool_schema(tool)
                for tool in list(request.tools or [])
            )
            if schema is not None
        ]
        model_refs = list(self._runner._resolve_ceo_model_refs() or [])
        provider_model = str(model_refs[0] if model_refs else "").strip()
        session_key = str(getattr(getattr(request.runtime, "context", None), "session_key", "") or "").strip()
        contract = self._runner._frontdoor_prompt_contract(
            state=state,
            provider_model=provider_model,
            tool_schemas=tool_schemas,
            overlay_text=overlay_text,
            fallback_system_message=request.system_message,
            fallback_messages=list(request.messages or []),
            session_key=session_key,
            overlay_section_count=len([section for section in overlay_text.split("\n\n") if section.strip()]),
        )
        request_system_message, request_messages = self._runner._render_request_records(contract.request_messages)
        current_blocks = list((request_system_message or request.system_message or SystemMessage(content="")).content_blocks)
        blocks = list(current_blocks)
        if overlay_text:
            blocks = [*blocks, {"type": "text", "text": overlay_text}]
        system_message = SystemMessage(content=blocks) if blocks else None
        updated_model_settings = {
            **dict(request.model_settings or {}),
            "prompt_cache_key": contract.prompt_cache_key,
        }
        return request.override(
            system_message=system_message,
            messages=request_messages,
            model_settings=updated_model_settings,
        ), {
            "prompt_cache_key": contract.prompt_cache_key,
            "prompt_cache_diagnostics": dict(contract.diagnostics),
        }

    @staticmethod
    def _wrap_response(response: ModelResponse | ExtendedModelResponse, update: dict[str, Any]) -> ExtendedModelResponse:
        if isinstance(response, ExtendedModelResponse):
            existing_update = dict(getattr(getattr(response, "command", None), "update", {}) or {})
            return ExtendedModelResponse(
                model_response=response.model_response,
                command=Command(update={**existing_update, **update}),
            )
        return ExtendedModelResponse(
            model_response=response,
            command=Command(update=update),
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        updated_request, update = self._prepare_request_and_update(request)
        response = handler(updated_request)
        return self._wrap_response(response, update)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        updated_request, update = self._prepare_request_and_update(request)
        progress = getattr(getattr(request.runtime, "context", None), "on_progress", None)
        await self._runner._emit_progress(
            progress,
            "正在请求 CEO 模型生成下一步响应...",
            event_kind="analysis",
            event_data={"phase": "model_call"},
        )
        provider_retry_count = 0
        empty_response_retry_count = 0
        while True:
            try:
                response = await handler(updated_request)
            except Exception as exc:
                if PUBLIC_PROVIDER_FAILURE_MESSAGE not in str(exc or ""):
                    raise
                provider_retry_count += 1
                await self._runner._emit_progress(
                    progress,
                    f"模型调用失败，正在重试（第{provider_retry_count}次）...",
                    event_kind="analysis",
                    event_data={"phase": "provider_retry", "attempt": provider_retry_count},
                )
                await asyncio.sleep(float(min(10, max(1, provider_retry_count))))
                continue
            response_message = next(
                (item for item in reversed(list(response.result or [])) if isinstance(item, AIMessage)),
                None,
            )
            if response_message is None or not self._runner._is_empty_model_response(
                self._runner._model_response_view(response_message)
            ):
                break
            empty_response_retry_count += 1
            await self._runner._emit_progress(
                progress,
                f"模型返回空响应，正在重试（第{empty_response_retry_count}次）...",
                event_kind="analysis",
                event_data={"phase": "empty_response_retry", "attempt": empty_response_retry_count},
            )
            await asyncio.sleep(float(min(10, max(1, empty_response_retry_count))))
        return self._wrap_response(response, update)


class CeoToolExposureMiddleware(AgentMiddleware):
    def __init__(self, *, runner: Any) -> None:
        super().__init__()
        self._runner = runner

    def _resolved_tool(self, request: ToolCallRequest) -> Any:
        tool_name = str(
            getattr(getattr(request, "tool", None), "name", "")
            or getattr(getattr(request, "tool_call", {}), "get", lambda *_: "")("name")
            or ""
        ).strip()
        tools = self._runner.visible_langchain_tools(state=request.state, runtime=request.runtime)
        for tool in list(tools or []):
            if str(getattr(tool, "name", "") or "").strip() == tool_name:
                return tool
        return None

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        tools = self._runner.visible_langchain_tools(state=request.state, runtime=request.runtime)
        return handler(request.override(tools=tools))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        tools = self._runner.visible_langchain_tools(state=request.state, runtime=request.runtime)
        return await handler(request.override(tools=tools))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        resolved_tool = request.tool or self._resolved_tool(request)
        if resolved_tool is None:
            return handler(request)
        return handler(request.override(tool=resolved_tool))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        resolved_tool = request.tool or self._resolved_tool(request)
        if resolved_tool is None:
            return await handler(request)
        return await handler(request.override(tool=resolved_tool))


class CeoTurnLifecycleMiddleware(AgentMiddleware):
    def __init__(self, *, runner: Any) -> None:
        super().__init__()
        self._runner = runner

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(request)

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime) -> dict[str, Any] | None:
        current_state = dict(state or {})
        if str(current_state.get("final_output") or "").strip():
            return {"jump_to": "end"}
        update = await self._runner._postprocess_completed_tool_cycle(state=current_state)
        if update is not None:
            self._runner._sync_runtime_session_frontdoor_state(
                state={**current_state, **dict(update or {})},
                runtime=runtime,
            )
        return update

    async def aafter_agent(self, state, runtime) -> dict[str, Any] | None:
        finalized = await self._runner._graph_finalize_turn(
            {
                **dict(state or {}),
                "messages": self._runner._state_message_records(state),
            }
        )
        return {
            **self._runner._replace_messages_update(list(finalized.get("messages") or [])),
            "final_output": str(finalized.get("final_output") or ""),
            "route_kind": str(finalized.get("route_kind") or "direct_reply"),
            "verified_task_ids": list(state.get("verified_task_ids") or []),
            "frontdoor_stage_state": self._runner._frontdoor_stage_state_snapshot(state),
            "compression_state": dict(
                state.get("compression_state") or self._runner._default_compression_state()
            ),
        }


class CeoModelOutputMiddleware(AgentMiddleware):
    def __init__(self, *, runner: Any) -> None:
        super().__init__()
        self._runner = runner

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(request)

    @hook_config(can_jump_to=["end", "model"])
    async def aafter_model(self, state, runtime) -> dict[str, Any] | None:
        current_state = dict(state or {})
        raw_messages = list(current_state.get("messages") or [])
        last_ai_message = next((item for item in reversed(raw_messages) if isinstance(item, AIMessage)), None)
        if last_ai_message is None:
            self._runner._sync_runtime_session_frontdoor_state(state=current_state, runtime=runtime)
            return None

        response_payload = self._runner._checkpoint_safe_model_response_payload(last_ai_message)
        normalized = await self._runner._graph_normalize_model_output(
            {
                **current_state,
                "response_payload": response_payload,
            },
            runtime=runtime,
        )
        history_records = self._runner._state_message_records(raw_messages[:-1])
        keep_last_ai = bool(str(self._runner._content_text(response_payload.get("content", "")) or "").strip())

        if str(normalized.get("next_step") or "") == "review_tool_calls":
            tool_call_payloads = list(normalized.get("tool_call_payloads") or [])
            ai_record = self._runner._message_record(last_ai_message)
            ai_record["content"] = (
                None
                if normalized.get("synthetic_tool_calls_used")
                else self._runner._model_content(response_payload.get("content", ""))
            )
            ai_record["tool_calls"] = self._runner._assistant_tool_calls_from_payloads(tool_call_payloads)
            preview_state = {
                **current_state,
                "tool_call_payloads": tool_call_payloads,
            }
            preview_frontdoor_stage_state, preview_compression_state = self._runner._runtime_session_frontdoor_state(
                preview_state,
                preview_pending_tool_round=True,
            )
            update = {
                **self._runner._replace_messages_update([*history_records, ai_record]),
                "response_payload": response_payload,
                "analysis_text": str(normalized.get("analysis_text") or ""),
                "tool_call_payloads": tool_call_payloads,
                "approval_request": (
                    dict(normalized.get("approval_request") or {})
                    if isinstance(normalized.get("approval_request"), dict)
                    else None
                ),
                "approval_status": "",
                "synthetic_tool_calls_used": bool(normalized.get("synthetic_tool_calls_used")),
                "xml_repair_attempt_count": int(normalized.get("xml_repair_attempt_count") or 0),
                "xml_repair_excerpt": str(normalized.get("xml_repair_excerpt") or ""),
                "xml_repair_tool_names": list(normalized.get("xml_repair_tool_names") or []),
                "xml_repair_last_issue": str(normalized.get("xml_repair_last_issue") or ""),
                "repair_overlay_text": None,
                "final_output": "",
            }
            self._runner._sync_runtime_session_frontdoor_state(
                state={
                    **current_state,
                    **update,
                    "frontdoor_stage_state": preview_frontdoor_stage_state,
                    "compression_state": preview_compression_state,
                },
                runtime=runtime,
            )
            return update

        if str(normalized.get("next_step") or "") == "call_model":
            update = {
                **self._runner._replace_messages_update(history_records),
                "response_payload": response_payload,
                "repair_overlay_text": str(normalized.get("repair_overlay_text") or ""),
                "xml_repair_attempt_count": int(normalized.get("xml_repair_attempt_count") or 0),
                "xml_repair_excerpt": str(normalized.get("xml_repair_excerpt") or ""),
                "xml_repair_tool_names": list(normalized.get("xml_repair_tool_names") or []),
                "xml_repair_last_issue": str(normalized.get("xml_repair_last_issue") or ""),
                "jump_to": "model",
            }
            self._runner._sync_runtime_session_frontdoor_state(
                state={**current_state, **update},
                runtime=runtime,
            )
            return update

        if str(normalized.get("next_step") or "") == "finalize":
            final_messages = [*history_records, self._runner._message_record(last_ai_message)] if keep_last_ai else history_records
            update = {
                **self._runner._replace_messages_update(final_messages),
                "response_payload": response_payload,
                "final_output": str(normalized.get("final_output") or ""),
                "route_kind": str(normalized.get("route_kind") or current_state.get("route_kind") or "direct_reply"),
                "jump_to": "end",
            }
            self._runner._sync_runtime_session_frontdoor_state(
                state={**current_state, **update},
                runtime=runtime,
            )
            return update
        self._runner._sync_runtime_session_frontdoor_state(state=current_state, runtime=runtime)
        return None


class CeoApprovalMiddleware(AgentMiddleware):
    def __init__(self, *, runner: Any) -> None:
        super().__init__()
        self._runner = runner

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(request)

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state, runtime) -> dict[str, Any] | None:
        current_state = dict(state or {})
        approval_request = dict(current_state.get("approval_request") or {})
        if not approval_request:
            return None

        preview_state = {
            **current_state,
            "tool_call_payloads": list(current_state.get("tool_call_payloads") or []),
        }
        preview_frontdoor_stage_state, preview_compression_state = self._runner._runtime_session_frontdoor_state(
            preview_state,
            preview_pending_tool_round=True,
        )
        interrupt_payload = {
            **approval_request,
            "frontdoor_stage_state": preview_frontdoor_stage_state,
            "compression_state": preview_compression_state,
            "tool_call_payloads": list(current_state.get("tool_call_payloads") or []),
        }

        decision = interrupt(interrupt_payload)
        normalized = self._runner._normalize_approval_resume_value(
            decision=decision,
            original_payloads=list(current_state.get("tool_call_payloads") or []),
        )
        if not normalized["approved"]:
            history_records = self._runner._state_message_records(current_state)
            if history_records and str(history_records[-1].get("role") or "").strip().lower() == "assistant":
                history_records = history_records[:-1]
            update = {
                **self._runner._replace_messages_update(history_records),
                "approval_request": None,
                "approval_status": "rejected",
                "tool_call_payloads": [],
                "final_output": "Cancelled the approval-gated action. No tool was executed.",
                "route_kind": "direct_reply",
                "jump_to": "end",
            }
            self._runner._sync_runtime_session_frontdoor_state(
                state={**current_state, **update},
                runtime=runtime,
            )
            return update

        raw_messages = list(current_state.get("messages") or [])
        last_ai_message = next((item for item in reversed(raw_messages) if isinstance(item, AIMessage)), None)
        if last_ai_message is None:
            update = {
                "approval_request": None,
                "approval_status": "approved",
                "tool_call_payloads": list(normalized["tool_call_payloads"]),
            }
            self._runner._sync_runtime_session_frontdoor_state(
                state={**current_state, **update},
                runtime=runtime,
            )
            return update

        history_records = self._runner._state_message_records(raw_messages[:-1])
        ai_record = self._runner._message_record(last_ai_message)
        ai_record["tool_calls"] = self._runner._assistant_tool_calls_from_payloads(
            list(normalized["tool_call_payloads"])
        )
        update = {
            **self._runner._replace_messages_update([*history_records, ai_record]),
            "approval_request": None,
            "approval_status": "approved",
            "tool_call_payloads": list(normalized["tool_call_payloads"]),
        }
        self._runner._sync_runtime_session_frontdoor_state(
            state={**current_state, **update},
            runtime=runtime,
        )
        return update
