from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from langchain_core.messages import AIMessage, convert_to_messages
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from g3ku.agent.tools.base import Tool
from g3ku.json_schema_utils import attach_raw_parameters_schema, build_args_schema_model
from g3ku.providers.base_chat_model_adapter import G3kuChatModelAdapter
from g3ku.providers.fallback import PUBLIC_PROVIDER_FAILURE_MESSAGE
from g3ku.runtime.project_environment import current_project_environment
from main.models import normalize_execution_policy_metadata
from main.runtime.chat_backend import build_prompt_cache_diagnostics, build_session_prompt_cache_key
from main.runtime.tool_call_repair import (
    XML_REPAIR_ATTEMPT_LIMIT,
    build_xml_tool_repair_message,
    extract_tool_calls_from_xml_pseudo_content,
    recover_tool_calls_from_json_payload,
)

from ._ceo_support import CeoFrontDoorSupport
from .ceo_summarizer import summarize_frontdoor_history
from .history_compaction import compact_frontdoor_history, frontdoor_summary_state
from .state_models import (
    CeoPersistentState,
    CeoRuntimeContext,
)

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]
CeoGraphState = CeoPersistentState

_TASK_ID_PATTERN = re.compile(r"task:[A-Za-z0-9][\w:-]*")


@dataclass(slots=True)
class VisibleToolBundle:
    native_tools: dict[str, Tool]
    langchain_tools: list[BaseTool]
    langchain_tool_map: dict[str, BaseTool]

def _checkpoint_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {
            str(key): _checkpoint_safe_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [_checkpoint_safe_value(item) for item in value]
    return str(value)


def _persistent_user_input_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        content = value.get("content", "")
        metadata = value.get("metadata", {})
    else:
        content = getattr(value, "content", "")
        metadata = getattr(value, "metadata", {})
    return {
        "content": _checkpoint_safe_value(content),
        "metadata": (
            _checkpoint_safe_value(metadata)
            if isinstance(metadata, dict)
            else {}
        ),
    }


def _user_input_content(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("content", "")
    return getattr(value, "content", "")


def _user_input_metadata(value: Any) -> dict[str, Any]:
    metadata = value.get("metadata", {}) if isinstance(value, dict) else getattr(value, "metadata", {})
    return dict(metadata) if isinstance(metadata, dict) else {}


def _join_overlay_text(*parts: Any) -> str:
    sections = [str(part or "").strip() for part in parts if str(part or "").strip()]
    return "\n\n".join(sections).strip()


def _build_args_schema(tool: Tool):
    return build_args_schema_model(tool.name, tool.parameters)


def _build_langchain_tool(tool: Tool, executor: ToolExecutor) -> BaseTool:
    async def _invoke(**kwargs: Any) -> Any:
        filtered_kwargs = {
            str(key): value
            for key, value in dict(kwargs or {}).items()
            if value is not None
        }
        return await executor(tool.name, filtered_kwargs)

    return attach_raw_parameters_schema(
        StructuredTool.from_function(
            coroutine=_invoke,
            name=tool.name,
            description=tool.description,
            args_schema=_build_args_schema(tool),
            infer_schema=False,
        ),
        tool.parameters,
    )


def _build_visible_tool_bundle(*, tools: dict[str, Tool], executor: ToolExecutor) -> VisibleToolBundle:
    native_tools = dict(tools or {})
    langchain_tool_map = {
        name: _build_langchain_tool(tool, executor)
        for name, tool in native_tools.items()
    }
    return VisibleToolBundle(
        native_tools=native_tools,
        langchain_tools=list(langchain_tool_map.values()),
        langchain_tool_map=dict(langchain_tool_map),
    )


def _normalize_frontdoor_tool_arguments(tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    normalized = dict(arguments or {})
    if str(tool_name or "").strip() != "create_async_task":
        return normalized
    raw_policy = normalized.get("execution_policy")
    policy_payload: dict[str, Any]
    if isinstance(raw_policy, dict):
        policy_payload = dict(raw_policy)
    elif isinstance(raw_policy, str):
        stripped = str(raw_policy).strip()
        parsed: Any = None
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except Exception:
                parsed = None
        if isinstance(parsed, dict):
            policy_payload = dict(parsed)
        elif stripped:
            policy_payload = {"mode": stripped}
        else:
            policy_payload = {}
    else:
        policy_payload = {}
    normalized["execution_policy"] = normalize_execution_policy_metadata(policy_payload).model_dump(mode="json")
    return normalized


def _build_langgraph_ceo_graph(runner):
    graph = StateGraph(CeoPersistentState, context_schema=CeoRuntimeContext)
    graph.add_node("prepare_turn", runner._graph_prepare_turn)
    graph.add_node("call_model", runner._graph_call_model)
    graph.add_node("normalize_model_output", runner._graph_normalize_model_output)
    graph.add_node("review_tool_calls", runner._graph_review_tool_calls)
    graph.add_node("execute_tools", runner._graph_execute_tools)
    graph.add_node("finalize_turn", runner._graph_finalize_turn)
    graph.add_edge(START, "prepare_turn")
    graph.add_edge("prepare_turn", "call_model")
    graph.add_edge("call_model", "normalize_model_output")
    graph.add_conditional_edges(
        "normalize_model_output",
        runner._graph_next_step,
        {
            "call_model": "call_model",
            "review_tool_calls": "review_tool_calls",
            "execute_tools": "execute_tools",
            "finalize": "finalize_turn",
        },
    )
    graph.add_conditional_edges(
        "review_tool_calls",
        runner._graph_next_step,
        {
            "execute_tools": "execute_tools",
            "finalize": "finalize_turn",
        },
    )
    graph.add_conditional_edges(
        "execute_tools",
        runner._graph_next_step,
        {
            "call_model": "call_model",
            "finalize": "finalize_turn",
        },
    )
    graph.add_edge("finalize_turn", END)
    return graph.compile(
        name="ceo-frontdoor",
        checkpointer=getattr(runner._loop, "_checkpointer", None),
        store=getattr(runner._loop, "_store", None),
    )


class CeoFrontDoorRuntimeOps(CeoFrontDoorSupport):
    """Shared CEO runtime operations reused by the create_agent frontdoor path."""

    def __init__(self, *, loop) -> None:
        super().__init__(loop=loop)
        self._compiled_graph = None

    def _build_tool_runtime_context(
        self,
        *,
        state: CeoGraphState,
        runtime: Runtime[CeoRuntimeContext],
    ) -> dict[str, Any]:
        session = runtime.context.session
        runtime_session = self._loop.sessions.get_or_create(session.state.session_key)
        project_environment = current_project_environment(workspace_root=getattr(self._loop, "workspace", None))
        metadata = _user_input_metadata(state.get("user_input"))
        heartbeat_internal = bool(state.get("heartbeat_internal", metadata.get("heartbeat_internal")))
        cron_internal = bool(state.get("cron_internal", metadata.get("cron_internal")))
        return {
            "on_progress": runtime.context.on_progress,
            "emit_lifecycle": True,
            "actor_role": "ceo",
            "session_key": session.state.session_key,
            "channel": getattr(session, "_channel", "cli"),
            "chat_id": getattr(session, "_chat_id", session.state.session_key),
            "memory_channel": getattr(session, "_memory_channel", getattr(session, "_channel", "cli")),
            "memory_chat_id": getattr(
                session,
                "_memory_chat_id",
                getattr(session, "_chat_id", session.state.session_key),
            ),
            "cancel_token": getattr(session, "_active_cancel_token", None),
            "tool_snapshot_supplier": getattr(session, "inflight_turn_snapshot", None),
            "temp_dir": str(getattr(self._loop, "temp_dir", "") or ""),
            "loop": self._loop,
            "task_defaults": self._session_task_defaults(runtime_session),
            "project_python": str(project_environment.get("project_python") or ""),
            "project_python_dir": str(project_environment.get("project_python_dir") or ""),
            "project_scripts_dir": str(project_environment.get("project_scripts_dir") or ""),
            "project_path_entries": list(project_environment.get("project_path_entries") or []),
            "project_virtual_env": str(project_environment.get("project_virtual_env") or ""),
            "project_python_hint": str(project_environment.get("project_python_hint") or ""),
            "heartbeat_internal": heartbeat_internal,
            "cron_internal": cron_internal,
            "cron_job_id": str(metadata.get("cron_job_id") or "").strip(),
            "cron_stop_condition": str(metadata.get("cron_stop_condition") or "").strip(),
        }

    def _registered_tools_for_state(self, state: CeoGraphState) -> dict[str, Tool]:
        return self._registered_tools(list(state.get("tool_names") or []))

    def _selected_tool_schemas(self, tool_names: list[str] | None) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for name in list(tool_names or []):
            tool = self._loop.tools.get(str(name or "").strip())
            if tool is None:
                continue
            try:
                schemas.append(tool.to_schema())
            except Exception:
                continue
        return schemas

    @staticmethod
    def _effective_turn_overlay_text(state: CeoGraphState) -> str:
        return _join_overlay_text(
            state.get("turn_overlay_text"),
            state.get("repair_overlay_text"),
        )

    def _frontdoor_compaction_settings(self) -> tuple[int, int]:
        assembly_cfg = getattr(getattr(self._loop, "_memory_runtime_settings", None), "assembly", None)
        recent_count = max(1, int(getattr(assembly_cfg, "frontdoor_recent_message_count", 8) or 8))
        trigger_count = max(
            recent_count + 1,
            int(getattr(assembly_cfg, "frontdoor_summary_trigger_message_count", 24) or 24),
        )
        return recent_count, trigger_count

    def _compact_frontdoor_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        recent_count, trigger_count = self._frontdoor_compaction_settings()
        return compact_frontdoor_history(
            messages,
            recent_message_count=recent_count,
            summary_trigger_message_count=trigger_count,
        )

    def _compacted_frontdoor_state(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        compacted = self._compact_frontdoor_messages(messages)
        return {
            "messages": compacted,
            **frontdoor_summary_state(compacted),
        }

    def _summarizer_settings(self) -> tuple[bool, str | None, int, int]:
        assembly_cfg = getattr(getattr(self._loop, "_memory_runtime_settings", None), "assembly", None)
        enabled = bool(getattr(assembly_cfg, "frontdoor_summarizer_enabled", True))
        model_key = getattr(assembly_cfg, "frontdoor_summarizer_model_key", None)
        keep_count = int(
            getattr(
                assembly_cfg,
                "frontdoor_summarizer_keep_message_count",
                getattr(assembly_cfg, "frontdoor_recent_message_count", 8),
            )
            or 8
        )
        trigger_count = int(
            getattr(
                assembly_cfg,
                "frontdoor_summarizer_trigger_message_count",
                getattr(assembly_cfg, "frontdoor_summary_trigger_message_count", 24),
            )
            or 24
        )
        keep_count = max(1, keep_count)
        trigger_count = max(keep_count + 1, trigger_count)
        return enabled, model_key, trigger_count, keep_count

    async def _summarize_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        state: CeoGraphState,
    ) -> dict[str, Any]:
        enabled, model_key, trigger_count, keep_count = self._summarizer_settings()
        model_invoke = getattr(self, "_invoke_summary_model", None)
        if enabled and not callable(model_invoke):
            enabled = False
        if not enabled:
            compacted = compact_frontdoor_history(
                list(messages or []),
                recent_message_count=keep_count,
                summary_trigger_message_count=trigger_count,
            )
            summary_state = frontdoor_summary_state(compacted)
            return {
                "messages": compacted,
                "summary_text": str(summary_state.get("summary_text") or ""),
                "summary_payload": {},
                "summary_version": int(summary_state.get("summary_version") or 0),
                "summary_model_key": str(summary_state.get("summary_model_key") or ""),
            }

        result = await summarize_frontdoor_history(
            messages=list(messages or []),
            previous_summary_text=str(state.get("summary_text") or ""),
            previous_summary_payload=dict(state.get("summary_payload") or {}),
            keep_message_count=keep_count,
            trigger_message_count=trigger_count,
            model_key=model_key,
            model_invoke=model_invoke,
        )
        return {
            "messages": list(result.messages),
            "summary_text": str(result.summary_text or ""),
            "summary_payload": dict(result.summary_payload or {}),
            "summary_version": int(result.summary_version or 0),
            "summary_model_key": str(result.summary_model_key or ""),
        }

    def _reviewable_tool_names(self) -> set[str]:
        assembly_cfg = getattr(getattr(self._loop, "_memory_runtime_settings", None), "assembly", None)
        if not bool(getattr(assembly_cfg, "frontdoor_interrupt_approval_enabled", False)):
            return set()
        raw_names = list(
            getattr(assembly_cfg, "frontdoor_interrupt_tool_names", ["message", "create_async_task"]) or []
        )
        return {str(name).strip() for name in raw_names if str(name).strip()}

    def _approval_request_for_tool_calls(
        self,
        tool_call_payloads: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        risky = [
            dict(item)
            for item in list(tool_call_payloads or [])
            if str(item.get("name") or "").strip() in self._reviewable_tool_names()
        ]
        if not risky:
            return None
        return {
            "kind": "frontdoor_tool_approval",
            "question": "Approve the CEO frontdoor tool execution?",
            "tool_calls": risky,
        }

    def _normalize_approval_resume_value(
        self,
        *,
        decision: Any,
        original_payloads: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if decision is True:
            return {"approved": True, "tool_call_payloads": list(original_payloads)}
        if decision is False or decision in (None, ""):
            return {"approved": False, "tool_call_payloads": []}
        if isinstance(decision, dict):
            approved = bool(decision.get("approved", decision.get("action") == "approve"))
            return {
                "approved": approved,
                "tool_call_payloads": list(original_payloads) if approved else [],
            }
        return {"approved": False, "tool_call_payloads": []}

    def _build_langchain_tools_for_state(
        self,
        *,
        state: CeoGraphState,
        runtime: Runtime[CeoRuntimeContext],
    ) -> list[BaseTool]:
        registered_tools = self._registered_tools_for_state(state)
        runtime_context = self._build_tool_runtime_context(state=state, runtime=runtime)
        on_progress = runtime_context.get("on_progress")

        async def _tool_executor(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            tool = registered_tools.get(tool_name)
            if tool is None:
                return {
                    "result_text": f"Error: tool not available: {tool_name}",
                    "status": "error",
                    "started_at": "",
                    "finished_at": "",
                    "elapsed_seconds": None,
                }
            normalized_arguments = _normalize_frontdoor_tool_arguments(tool_name, arguments)
            result_text, status, started_at, finished_at, elapsed_seconds = await self._execute_tool_call(
                tool=tool,
                tool_name=tool_name,
                arguments=normalized_arguments,
                runtime_context=runtime_context,
                on_progress=on_progress,
            )
            await self._emit_progress(
                on_progress,
                result_text,
                event_kind="tool_result" if status == "success" else "tool_error",
                event_data={"tool_name": tool_name},
            )
            return {
                "result_text": result_text,
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "elapsed_seconds": elapsed_seconds,
            }

        tool_bundle = _build_visible_tool_bundle(
            tools=registered_tools,
            executor=_tool_executor,
        )
        return list(tool_bundle.langchain_tools)

    @staticmethod
    def _model_response_view(message: AIMessage | dict[str, Any]) -> Any:
        if isinstance(message, dict):
            payload = dict(message or {})
            return type(
                "ModelResponseView",
                (),
                {
                    "content": payload.get("content", ""),
                    "tool_calls": list(payload.get("tool_calls", None) or []),
                    "finish_reason": str(payload.get("finish_reason", "stop") or "stop"),
                    "error_text": str(payload.get("error_text", "") or ""),
                    "reasoning_content": payload.get("reasoning_content"),
                    "thinking_blocks": payload.get("thinking_blocks"),
                },
            )()
        response_metadata = dict(getattr(message, "response_metadata", {}) or {})
        additional_kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
        return type(
            "ModelResponseView",
            (),
            {
                "content": getattr(message, "content", ""),
                "tool_calls": list(getattr(message, "tool_calls", None) or []),
                "finish_reason": str(response_metadata.get("finish_reason", "stop") or "stop"),
                "error_text": str(response_metadata.get("error_text", "") or ""),
                "reasoning_content": additional_kwargs.get("reasoning_content"),
                "thinking_blocks": additional_kwargs.get("thinking_blocks"),
            },
        )()

    def _checkpoint_safe_model_response_payload(self, message: AIMessage) -> dict[str, Any]:
        response_view = self._model_response_view(message)
        return {
            "content": _checkpoint_safe_value(response_view.content),
            "tool_calls": _checkpoint_safe_value(
                self._tool_call_payloads_from_calls(list(response_view.tool_calls or []))
            ),
            "finish_reason": str(response_view.finish_reason or "stop"),
            "error_text": str(response_view.error_text or ""),
            "reasoning_content": _checkpoint_safe_value(response_view.reasoning_content),
            "thinking_blocks": _checkpoint_safe_value(response_view.thinking_blocks),
        }

    @staticmethod
    def _tool_call_payloads_from_calls(calls: list[Any]) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for call in list(calls or []):
            if isinstance(call, dict):
                arguments = call.get("args", call.get("arguments", {}))
                name = str(call.get("name") or "").strip()
                call_id = str(call.get("id") or "")
            else:
                arguments = getattr(call, "arguments", getattr(call, "args", {}))
                name = str(getattr(call, "name", "") or "").strip()
                call_id = str(getattr(call, "id", "") or "")
            if not isinstance(arguments, dict):
                arguments = {}
            payloads.append(
                {
                    "id": call_id,
                    "name": name,
                    "arguments": dict(arguments),
                }
            )
        return payloads

    @staticmethod
    def _assistant_tool_calls_from_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": str(item.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(item.get("name") or "").strip(),
                    "arguments": json.dumps(dict(item.get("arguments") or {}), ensure_ascii=False),
                },
            }
            for item in list(payloads or [])
        ]

    @staticmethod
    def _extract_task_id(text: str) -> str:
        match = _TASK_ID_PATTERN.search(str(text or ""))
        return str(match.group(0) if match else "").strip()

    @staticmethod
    def _normalize_task_ids(values: Any) -> list[str]:
        items = list(values) if isinstance(values, (list, tuple, set)) else [values]
        normalized: list[str] = []
        for raw in items:
            task_id = str(raw or "").strip()
            if not task_id.startswith("task:") or task_id in normalized:
                continue
            normalized.append(task_id)
        return normalized

    @staticmethod
    def _looks_like_task_dispatch_claim(text: str) -> bool:
        normalized = str(text or "").strip().lower()
        if not normalized or "task:" not in normalized:
            return False
        markers = (
            "后台",
            "异步任务",
            "续跑",
            "成功续跑",
            "已在后台",
            "新任务 id",
            "任务 id",
            "重新为您创建",
            "创建任务",
            "re-run in background",
            "background",
            "async task",
            "new task id",
            "created task",
        )
        return any(marker in normalized for marker in markers)

    def _task_id_exists(self, task_id: str) -> bool:
        normalized = str(task_id or "").strip()
        if not normalized:
            return False
        service = getattr(self._loop, "main_task_service", None)
        getter = getattr(service, "get_task", None) if service is not None else None
        if not callable(getter):
            return False
        try:
            return getter(normalized) is not None
        except Exception:
            return False

    def _verified_task_ids_from_text(self, text: str) -> list[str]:
        return [
            task_id
            for task_id in self._normalize_task_ids(_TASK_ID_PATTERN.findall(str(text or "")))
            if self._task_id_exists(task_id)
        ]

    def _verified_dispatch_task_id(self, text: str) -> str:
        if not self._looks_like_task_dispatch_claim(text):
            return ""
        task_ids = self._verified_task_ids_from_text(text)
        return task_ids[0] if task_ids else ""

    @staticmethod
    def _verified_task_ids_from_state(state: CeoGraphState) -> list[str]:
        return CeoFrontDoorRuntimeOps._normalize_task_ids(state.get("verified_task_ids"))

    @staticmethod
    def _heartbeat_task_ids_from_state(state: CeoGraphState) -> list[str]:
        metadata = _user_input_metadata(state.get("user_input"))
        return CeoFrontDoorRuntimeOps._normalize_task_ids(metadata.get("heartbeat_task_ids"))

    def _allowed_dispatch_task_ids_from_state(self, state: CeoGraphState) -> list[str]:
        allowed: list[str] = []
        for task_id in self._verified_task_ids_from_state(state):
            if task_id not in allowed:
                allowed.append(task_id)
        metadata = _user_input_metadata(state.get("user_input"))
        heartbeat_internal = bool(state.get("heartbeat_internal", metadata.get("heartbeat_internal")))
        if heartbeat_internal:
            for task_id in self._heartbeat_task_ids_from_state(state):
                if task_id not in allowed:
                    allowed.append(task_id)
        return allowed

    def _dispatch_claim_uses_allowed_task_ids(
        self,
        *,
        text: str,
        state: CeoGraphState,
    ) -> bool:
        referenced_task_ids = self._normalize_task_ids(_TASK_ID_PATTERN.findall(str(text or "")))
        if not referenced_task_ids:
            return False
        allowed_task_ids = set(self._allowed_dispatch_task_ids_from_state(state))
        if not allowed_task_ids:
            return False
        return set(referenced_task_ids).issubset(allowed_task_ids)

    def _unverified_task_dispatch_reply(self, *, task_id: str = "") -> str:
        detail = f"：`{task_id}`" if str(task_id or "").strip() else ""
        return f"未确认成功创建后台任务{detail}。当前回合没有可验证的真实任务派发结果。"

    def _task_dispatch_reply(self, *, result_text: str) -> str:
        text = str(result_text or "").strip()
        task_id = self._extract_task_id(text)
        if "复用" in text:
            if task_id:
                return (
                    "已复用正在进行中的异步任务：\n"
                    f"- 任务 ID: `{task_id}`\n\n"
                    "我会继续在后台推进。"
                )
            return text
        if task_id:
            return (
                "已开始处理，这个需求已转为异步任务：\n"
                f"- 任务 ID: `{task_id}`\n\n"
                "我会继续在后台推进，并在有结果后回复你。"
            )
        return text

    @staticmethod
    def _verified_dispatch_reply_overlay(*, task_id: str) -> str:
        normalized_task_id = str(task_id or "").strip()
        lines = [
            "A real async task has already been created and verified.",
            "Do not call any tools.",
            "Do not create or reuse another async task.",
            "Reply with the exact user-facing text to show now.",
            "Use natural language rather than a rigid template.",
        ]
        if normalized_task_id:
            lines.insert(1, f"The verified task id is `{normalized_task_id}`.")
            lines.append("Include the verified task id in the reply.")
        return "\n".join(lines).strip()

    async def _call_model_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        langchain_tools: list[Any],
        model_refs: list[str],
        parallel_tool_calls: bool | None,
        prompt_cache_key: str,
    ) -> AIMessage:
        chat_model = G3kuChatModelAdapter(
            chat_backend=self._resolve_chat_backend(),
            model_refs=list(model_refs or []),
        )
        runnable = chat_model.bind_tools(langchain_tools) if langchain_tools else chat_model
        return await runnable.ainvoke(
            convert_to_messages(messages),
            parallel_tool_calls=parallel_tool_calls,
            prompt_cache_key=prompt_cache_key,
        )

    async def _invoke_summary_model(
        self,
        prompt: dict[str, Any],
        *,
        explicit_model_key: str | None = None,
    ) -> dict[str, Any]:
        from g3ku.config.live_runtime import get_runtime_config
        from g3ku.providers.chatmodels import build_chat_model

        config, _revision, _changed = get_runtime_config(force=False)
        assembly_cfg = getattr(getattr(self._loop, "_memory_runtime_settings", None), "assembly", None)
        model_key = str(explicit_model_key or getattr(assembly_cfg, "frontdoor_summarizer_model_key", None) or "").strip()
        if model_key:
            model = build_chat_model(config, model_key=model_key)
        else:
            model = build_chat_model(config, role="ceo")

        system_prompt = (
            "You summarize CEO frontdoor conversation history.\n"
            "Return strict JSON only.\n"
            "Required keys: stable_preferences, stable_facts, open_loops, recent_actions, narrative.\n"
            "The first four keys must be arrays of strings. narrative must be a string.\n"
            "Be concise and preserve durable user context and unresolved work."
        )
        user_payload = json.dumps(
            {
                "previous_summary_text": str(prompt.get("previous_summary_text") or ""),
                "previous_summary_payload": dict(prompt.get("previous_summary_payload") or {}),
                "messages": [dict(item) for item in list(prompt.get("messages") or []) if isinstance(item, dict)],
            },
            ensure_ascii=False,
        )
        response = await model.ainvoke(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ]
        )
        raw = getattr(response, "content", response)
        if isinstance(raw, list):
            parts: list[str] = []
            for item in raw:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if isinstance(item, dict):
                    text_part = item.get("text") or item.get("content") or ""
                    if isinstance(text_part, str):
                        parts.append(text_part)
            raw = "\n".join(parts)
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, count=1)
            text = re.sub(r"\s*```$", "", text, count=1)
            text = text.strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match is None:
                raise
            parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("summary model response must be a JSON object")
        return dict(parsed)

    async def _graph_prepare_turn(
        self,
        state: CeoGraphState,
        *,
        runtime: Runtime[CeoRuntimeContext],
    ) -> dict[str, Any]:
        if getattr(getattr(runtime, "context", None), "session", None) is None:
            compacted_state = await self._summarize_messages(
                messages=list(state.get("messages") or []),
                state=state,
            )
            get_value = (
                compacted_state.get
                if isinstance(compacted_state, dict)
                else lambda key, default=None: getattr(compacted_state, key, default)
            )
            return {
                "messages": list(get_value("messages") or []),
                "summary_text": str(get_value("summary_text") or ""),
                "summary_payload": dict(get_value("summary_payload") or {}),
                "summary_version": int(get_value("summary_version") or 0),
                "summary_model_key": str(get_value("summary_model_key") or ""),
            }

        user_input = _persistent_user_input_payload(state.get("user_input"))
        user_content = _user_input_content(user_input)
        session = runtime.context.session
        query_text = self._content_text(user_content)
        metadata = _user_input_metadata(user_input)
        heartbeat_internal = bool(metadata.get("heartbeat_internal"))
        cron_internal = bool(metadata.get("cron_internal"))
        runtime_session = self._loop.sessions.get_or_create(session.state.session_key)
        main_service = getattr(self._loop, "main_task_service", None)
        if main_service is not None:
            await main_service.startup()

        for name in ("message", "cron"):
            tool = self._loop.tools.get(name)
            if tool is not None and hasattr(tool, "set_context"):
                if name == "message":
                    tool.set_context(
                        getattr(session, "_channel", "cli"),
                        getattr(session, "_chat_id", session.state.session_key),
                        None,
                    )
                else:
                    tool.set_context(
                        getattr(session, "_channel", "cli"),
                        getattr(session, "_chat_id", session.state.session_key),
                    )
        message_tool = self._loop.tools.get("message")
        if message_tool is not None and hasattr(message_tool, "start_turn"):
            message_tool.start_turn()

        exposure = await self._resolver.resolve_for_actor(
            actor_role="ceo",
            session_id=session.state.session_key,
        )
        assembly = await self._builder.build_for_ceo(
            session=session,
            query_text=query_text,
            exposure=exposure,
            persisted_session=runtime_session,
            checkpoint_messages=list(state.get("messages") or []),
            user_content=self._model_content(user_content),
            user_metadata=metadata,
        )
        tool_names = list(assembly.tool_names or list(exposure.get("tool_names") or []))
        if cron_internal:
            tool_names = ["cron"]
        cron_system_message = self._cron_internal_system_message(metadata)
        messages: list[dict[str, Any]] = list(assembly.model_messages or [])
        if cron_system_message is not None:
            insert_at = 1 if messages and str(messages[0].get("role") or "").strip().lower() == "system" else 0
            messages = [*messages[:insert_at], cron_system_message, *messages[insert_at:]]
        if not messages or str(messages[-1].get("role") or "").strip().lower() != "user":
            messages.append({"role": "user", "content": self._model_content(user_content)})
        summarizer_enabled, _model_key, summarizer_trigger_count, _keep_count = self._summarizer_settings()
        if summarizer_enabled and len(messages) > summarizer_trigger_count:
            await self._emit_progress(
                getattr(getattr(runtime, "context", None), "on_progress", None),
                "正在压缩较长会话历史以减少前门上下文负担...",
                event_kind="analysis",
                event_data={"phase": "history_compaction"},
            )
        compacted_state = await self._summarize_messages(messages=messages, state=state)
        messages = list(compacted_state.get("messages") or [])

        model_refs = self._resolve_ceo_model_refs()
        provider_model = str(model_refs[0] if model_refs else "").strip()
        tool_schemas = self._selected_tool_schemas(tool_names)
        prompt_cache_key = build_session_prompt_cache_key(
            session_key=str(getattr(session.state, "session_key", "") or ""),
            provider_model=provider_model,
            scope="ceo_frontdoor",
            stable_messages=messages,
            tool_schemas=tool_schemas,
        )
        prompt_cache_diagnostics = build_prompt_cache_diagnostics(
            stable_messages=messages,
            tool_schemas=tool_schemas,
            provider_model=provider_model,
            scope="ceo_frontdoor",
            prompt_cache_key=prompt_cache_key,
            overlay_text=str(getattr(assembly, "turn_overlay_text", "") or ""),
            overlay_section_count=int(getattr(assembly, "trace", {}).get("turn_overlay_section_count", 0) or 0),
        )
        parallel_enabled, max_parallel_tool_calls = self._parallel_tool_settings()
        return {
            "user_input": user_input,
            "approval_request": None,
            "approval_status": "",
            "query_text": query_text,
            "messages": messages,
            "summary_text": str(compacted_state.get("summary_text") or ""),
            "summary_payload": dict(compacted_state.get("summary_payload") or {}),
            "summary_version": int(compacted_state.get("summary_version") or 0),
            "summary_model_key": str(compacted_state.get("summary_model_key") or ""),
            "turn_overlay_text": str(getattr(assembly, "turn_overlay_text", "") or "").strip() or None,
            "tool_names": list(tool_names),
            "used_tools": [],
            "route_kind": "direct_reply",
            "verified_task_ids": [],
            "repair_overlay_text": None,
            "xml_repair_attempt_count": 0,
            "xml_repair_excerpt": "",
            "xml_repair_tool_names": [],
            "xml_repair_last_issue": "",
            "empty_response_retry_count": 0,
            "heartbeat_internal": heartbeat_internal,
            "cron_internal": cron_internal,
            "model_refs": model_refs,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_diagnostics": prompt_cache_diagnostics,
            "parallel_enabled": parallel_enabled,
            "max_parallel_tool_calls": max_parallel_tool_calls,
            "max_iterations": getattr(self._loop, "max_iterations", 12),
            "iteration": 0,
            "final_output": "",
            "error_message": "",
            "next_step": "call_model",
        }

    async def _graph_call_model(
        self,
        state: CeoGraphState,
        *,
        runtime: Runtime[CeoRuntimeContext],
    ) -> dict[str, Any]:
        iteration = int(state.get("iteration", 0) or 0) + 1
        configured_limit = state.get("max_iterations")
        if configured_limit is not None and iteration > max(0, int(configured_limit)):
            raise RuntimeError("CEO frontdoor exceeded maximum iterations")

        langchain_tools = self._build_langchain_tools_for_state(state=state, runtime=runtime)
        request_messages = self._apply_turn_overlay(
            list(state.get("messages") or []),
            overlay_text=self._effective_turn_overlay_text(state),
        )
        provider_retry_count = 0
        empty_response_retry_count = 0
        while True:
            try:
                message = await self._call_model_with_tools(
                    messages=request_messages,
                    langchain_tools=langchain_tools,
                    model_refs=list(state.get("model_refs") or []),
                    parallel_tool_calls=(bool(state.get("parallel_enabled")) if langchain_tools else None),
                    prompt_cache_key=str(state.get("prompt_cache_key") or ""),
                )
            except Exception as exc:
                if PUBLIC_PROVIDER_FAILURE_MESSAGE not in str(exc or ""):
                    raise
                provider_retry_count += 1
                await asyncio.sleep(float(min(10, max(1, provider_retry_count))))
                continue
            response_view = self._model_response_view(message)
            if self._is_empty_model_response(response_view):
                empty_response_retry_count += 1
                await asyncio.sleep(float(min(10, max(1, empty_response_retry_count))))
                continue
            break
        return {
            "iteration": iteration,
            "repair_overlay_text": None,
            "response_payload": self._checkpoint_safe_model_response_payload(message),
            "empty_response_retry_count": empty_response_retry_count,
        }

    async def _graph_normalize_model_output(
        self,
        state: CeoGraphState,
        *,
        runtime: Runtime[CeoRuntimeContext],
    ) -> dict[str, Any]:
        response_payload = dict(state.get("response_payload") or {})
        response_view = self._model_response_view(response_payload)
        visible_tools = self._registered_tools_for_state(state)
        visible_tool_names = {
            str(name or "").strip()
            for name in visible_tools.keys()
            if str(name or "").strip()
        }
        response_tool_calls = list(response_view.tool_calls or [])
        synthetic_tool_calls_used = False
        xml_pseudo_call = None
        current_route_kind = str(state.get("route_kind") or "direct_reply")
        used_tools = list(state.get("used_tools") or [])
        xml_repair_attempt_count = int(state.get("xml_repair_attempt_count", 0) or 0)

        if not response_tool_calls and visible_tool_names:
            xml_extraction = extract_tool_calls_from_xml_pseudo_content(
                response_view.content,
                visible_tools=visible_tools,
                id_prefix="call:ceo-xml-direct",
            )
            if xml_extraction.tool_calls:
                response_tool_calls = xml_extraction.tool_calls
                synthetic_tool_calls_used = True
            if not response_tool_calls and xml_repair_attempt_count > 0:
                repaired_tool_calls = recover_tool_calls_from_json_payload(
                    response_view.content,
                    allowed_tool_names=visible_tool_names,
                    id_prefix="call:ceo-xml-repair",
                )
                if repaired_tool_calls:
                    response_tool_calls = repaired_tool_calls
                    synthetic_tool_calls_used = True
            if not response_tool_calls and xml_extraction.matched:
                xml_pseudo_call = {
                    "excerpt": xml_extraction.excerpt,
                    "tool_names": list(xml_extraction.tool_names or []),
                    "issue": str(xml_extraction.issue or "").strip(),
                }

        tool_call_payloads = self._tool_call_payloads_from_calls(response_tool_calls)
        if tool_call_payloads:
            analysis_text = "" if synthetic_tool_calls_used else self._content_text(response_view.content)
            approval_request = self._approval_request_for_tool_calls(tool_call_payloads)
            return {
                "analysis_text": analysis_text.strip(),
                "tool_call_payloads": tool_call_payloads,
                "approval_request": approval_request,
                "approval_status": "",
                "synthetic_tool_calls_used": synthetic_tool_calls_used,
                "xml_repair_attempt_count": 0,
                "xml_repair_excerpt": "",
                "xml_repair_tool_names": [],
                "xml_repair_last_issue": "",
                "next_step": "review_tool_calls",
            }

        if xml_pseudo_call is not None:
            xml_repair_attempt_count += 1
            xml_repair_excerpt = str(xml_pseudo_call.get("excerpt") or "").strip()
            xml_repair_tool_names = list(xml_pseudo_call.get("tool_names") or [])
            xml_repair_last_issue = (
                str(xml_pseudo_call.get("issue") or "").strip()
                or "reply used XML-like pseudo tool syntax instead of a valid tool call"
            )
            if xml_repair_attempt_count >= XML_REPAIR_ATTEMPT_LIMIT:
                return {
                    "final_output": self._xml_repair_explanation(
                        count=xml_repair_attempt_count,
                        tool_names=xml_repair_tool_names,
                        content_excerpt=xml_repair_excerpt,
                    ),
                    "route_kind": self._route_kind_for_turn(used_tools=used_tools, default=current_route_kind),
                    "xml_repair_attempt_count": xml_repair_attempt_count,
                    "xml_repair_excerpt": xml_repair_excerpt,
                    "xml_repair_tool_names": xml_repair_tool_names,
                    "xml_repair_last_issue": xml_repair_last_issue,
                    "next_step": "finalize",
                }
            return {
                "repair_overlay_text": build_xml_tool_repair_message(
                    xml_excerpt=xml_repair_excerpt,
                    tool_names=xml_repair_tool_names,
                    attempt_count=xml_repair_attempt_count,
                    attempt_limit=XML_REPAIR_ATTEMPT_LIMIT,
                    latest_issue=xml_repair_last_issue,
                ),
                "xml_repair_attempt_count": xml_repair_attempt_count,
                "xml_repair_excerpt": xml_repair_excerpt,
                "xml_repair_tool_names": xml_repair_tool_names,
                "xml_repair_last_issue": xml_repair_last_issue,
                "next_step": "call_model",
            }

        if xml_repair_attempt_count > 0:
            xml_repair_attempt_count += 1
            xml_repair_last_issue = "reply still did not contain valid structured tool_calls or a valid JSON repair payload"
            if xml_repair_attempt_count >= XML_REPAIR_ATTEMPT_LIMIT:
                return {
                    "final_output": self._xml_repair_explanation(
                        count=xml_repair_attempt_count,
                        tool_names=list(state.get("xml_repair_tool_names") or []),
                        content_excerpt=str(response_view.content or ""),
                    ),
                    "route_kind": self._route_kind_for_turn(used_tools=used_tools, default=current_route_kind),
                    "xml_repair_attempt_count": xml_repair_attempt_count,
                    "xml_repair_last_issue": xml_repair_last_issue,
                    "next_step": "finalize",
                }
            return {
                "repair_overlay_text": build_xml_tool_repair_message(
                    xml_excerpt=str(state.get("xml_repair_excerpt") or ""),
                    tool_names=list(state.get("xml_repair_tool_names") or []),
                    attempt_count=xml_repair_attempt_count,
                    attempt_limit=XML_REPAIR_ATTEMPT_LIMIT,
                    latest_issue=xml_repair_last_issue,
                ),
                "xml_repair_attempt_count": xml_repair_attempt_count,
                "xml_repair_last_issue": xml_repair_last_issue,
                "next_step": "call_model",
            }

        text = self._content_text(response_view.content)
        if text.strip():
            if (
                self._looks_like_task_dispatch_claim(text)
                and not self._dispatch_claim_uses_allowed_task_ids(text=text, state=state)
            ):
                return {
                    "final_output": self._unverified_task_dispatch_reply(
                        task_id=self._extract_task_id(text)
                    ),
                    "route_kind": "direct_reply",
                    "verified_task_ids": [],
                    "next_step": "finalize",
                }
            return {
                "final_output": text.strip(),
                "route_kind": self._route_kind_for_turn(used_tools=used_tools, default=current_route_kind),
                "next_step": "finalize",
            }

        if str(response_view.finish_reason or "").strip().lower() == "error":
            raise RuntimeError(str(response_view.error_text or response_view.content or "model response failed"))

        return {
            "final_output": self._empty_response_explanation(used_tools=used_tools),
            "route_kind": self._route_kind_for_turn(used_tools=used_tools, default=current_route_kind),
            "next_step": "finalize",
        }

    def _graph_review_tool_calls(self, state: CeoGraphState) -> dict[str, Any]:
        approval_request = dict(state.get("approval_request") or {})
        if not approval_request:
            return {"next_step": "execute_tools"}

        decision = interrupt(approval_request)
        normalized = self._normalize_approval_resume_value(
            decision=decision,
            original_payloads=list(state.get("tool_call_payloads") or []),
        )
        if not normalized["approved"]:
            return {
                "approval_request": None,
                "approval_status": "rejected",
                "tool_call_payloads": [],
                "final_output": "Cancelled the approval-gated action. No tool was executed.",
                "route_kind": "direct_reply",
                "next_step": "finalize",
            }
        return {
            "approval_request": None,
            "approval_status": "approved",
            "tool_call_payloads": list(normalized["tool_call_payloads"]),
            "next_step": "execute_tools",
        }

    async def _graph_execute_tools(
        self,
        state: CeoGraphState,
        *,
        runtime: Runtime[CeoRuntimeContext],
    ) -> dict[str, Any]:
        tool_call_payloads = list(state.get("tool_call_payloads") or [])
        if not tool_call_payloads:
            return {"next_step": "call_model"}

        runtime_context = self._build_tool_runtime_context(state=state, runtime=runtime)
        on_progress = runtime_context.get("on_progress")
        analysis_text = str(state.get("analysis_text") or "").strip()
        if analysis_text:
            await self._emit_progress(
                on_progress,
                analysis_text,
                event_kind="analysis",
            )

        visible_tools = self._registered_tools_for_state(state)
        semaphore = asyncio.Semaphore(
            self._parallel_slot_count(
                state.get("max_parallel_tool_calls"),
                len(tool_call_payloads),
                enabled=bool(state.get("parallel_enabled")),
            )
        )

        async def _run_single(payload: dict[str, Any]) -> dict[str, Any]:
            tool_name = str(payload.get("name") or "")
            tool = visible_tools.get(tool_name)
            if tool is None:
                result_text = f"Error: tool not available: {tool_name}"
                result_payload = {
                    "result_text": result_text,
                    "status": "error",
                    "started_at": "",
                    "finished_at": "",
                    "elapsed_seconds": None,
                }
            else:
                async with semaphore:
                    result_text, status, started_at, finished_at, elapsed_seconds = await self._execute_tool_call(
                        tool=tool,
                        tool_name=tool_name,
                        arguments=dict(payload.get("arguments") or {}),
                        runtime_context=runtime_context,
                        on_progress=on_progress,
                    )
                result_payload = {
                    "result_text": result_text,
                    "status": status,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "elapsed_seconds": elapsed_seconds,
                }
            result_text = str(result_payload.get("result_text") or "")
            status = str(result_payload.get("status") or self._tool_status(result_text))
            await self._emit_progress(
                on_progress,
                result_text,
                event_kind="tool_result" if status == "success" else "tool_error",
                event_data={"tool_name": tool_name},
            )
            return {
                "tool_name": tool_name,
                "status": status,
                "result_text": result_text,
                "tool_message": self._tool_result_message(
                    tool_call_id=str(payload.get("id") or ""),
                    tool_name=tool_name or "tool",
                    content=result_text,
                    started_at=str(result_payload.get("started_at") or ""),
                    finished_at=str(result_payload.get("finished_at") or ""),
                    elapsed_seconds=result_payload.get("elapsed_seconds"),
                ),
            }

        tool_results = await asyncio.gather(*[_run_single(payload) for payload in tool_call_payloads])
        tool_messages = [dict(item.get("tool_message") or {}) for item in tool_results]
        response_payload = dict(state.get("response_payload") or {})
        assistant_message = {
            "role": "assistant",
            "content": (
                None
                if state.get("synthetic_tool_calls_used")
                else self._model_content(response_payload.get("content", ""))
            ),
            "tool_calls": self._assistant_tool_calls_from_payloads(tool_call_payloads),
        }
        messages = list(state.get("messages") or [])
        messages.append(assistant_message)
        messages.extend(tool_messages)
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
        if successful_dispatch is not None and set(substantive_tool_names) == {"create_async_task"}:
            verified_task_id = self._verified_dispatch_task_id(
                str(successful_dispatch.get("result_text") or "")
            )
            if not verified_task_id:
                return {
                    "messages": messages,
                    "summary_text": str(compacted_state.get("summary_text") or ""),
                    "summary_payload": dict(compacted_state.get("summary_payload") or {}),
                    "summary_version": int(compacted_state.get("summary_version") or 0),
                    "summary_model_key": str(compacted_state.get("summary_model_key") or ""),
                    "used_tools": used_tools,
                    "route_kind": "direct_reply",
                    "analysis_text": "",
                    "tool_call_payloads": [],
                    "verified_task_ids": [],
                    "synthetic_tool_calls_used": False,
                    "final_output": self._unverified_task_dispatch_reply(
                        task_id=self._extract_task_id(
                            str(successful_dispatch.get("result_text") or "")
                        )
                    ),
                    "next_step": "finalize",
                }
            return {
                "messages": messages,
                "summary_text": str(compacted_state.get("summary_text") or ""),
                "summary_payload": dict(compacted_state.get("summary_payload") or {}),
                "summary_version": int(compacted_state.get("summary_version") or 0),
                "summary_model_key": str(compacted_state.get("summary_model_key") or ""),
                "used_tools": used_tools,
                "route_kind": route_kind,
                "analysis_text": "",
                "tool_names": [],
                "tool_call_payloads": [],
                "verified_task_ids": [verified_task_id],
                "repair_overlay_text": self._verified_dispatch_reply_overlay(task_id=verified_task_id),
                "synthetic_tool_calls_used": False,
                "next_step": "call_model",
            }
        return {
            "messages": messages,
            "summary_text": str(compacted_state.get("summary_text") or ""),
            "summary_payload": dict(compacted_state.get("summary_payload") or {}),
            "summary_version": int(compacted_state.get("summary_version") or 0),
            "summary_model_key": str(compacted_state.get("summary_model_key") or ""),
            "used_tools": used_tools,
            "route_kind": route_kind,
            "analysis_text": "",
            "tool_call_payloads": [],
            "verified_task_ids": [],
            "synthetic_tool_calls_used": False,
            "next_step": "call_model",
        }

    async def _graph_finalize_turn(self, state: CeoGraphState) -> dict[str, Any]:
        output = str(state.get("final_output") or "").strip()
        if not output and not bool(state.get("heartbeat_internal")):
            output = self._empty_reply_fallback(str(state.get("query_text") or ""))
        route_kind = str(state.get("route_kind") or "direct_reply")
        result = {
            "final_output": output,
            "route_kind": route_kind,
        }
        messages = [dict(message) for message in list(state.get("messages") or []) if isinstance(message, dict)]
        if output and route_kind == "direct_reply":
            last_role = str(messages[-1].get("role") or "").strip().lower() if messages else ""
            if last_role != "assistant":
                messages.append({"role": "assistant", "content": output})
            compacted_state = await self._summarize_messages(messages=messages, state=state)
            result["messages"] = list(compacted_state.get("messages") or [])
            result["summary_text"] = str(compacted_state.get("summary_text") or "")
            result["summary_payload"] = dict(compacted_state.get("summary_payload") or {})
            result["summary_version"] = int(compacted_state.get("summary_version") or 0)
            result["summary_model_key"] = str(compacted_state.get("summary_model_key") or "")
            return result
        compacted_state = await self._summarize_messages(messages=messages, state=state)
        result["messages"] = list(compacted_state.get("messages") or [])
        result["summary_text"] = str(compacted_state.get("summary_text") or "")
        result["summary_payload"] = dict(compacted_state.get("summary_payload") or {})
        result["summary_version"] = int(compacted_state.get("summary_version") or 0)
        result["summary_model_key"] = str(compacted_state.get("summary_model_key") or "")
        return result

    @staticmethod
    def _graph_next_step(state: CeoGraphState) -> str:
        next_step = str(state.get("next_step") or "finalize").strip()
        if next_step not in {"call_model", "review_tool_calls", "execute_tools", "finalize"}:
            return "finalize"
        return next_step


__all__ = ["CeoFrontDoorRuntimeOps"]
