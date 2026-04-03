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
from pydantic import ConfigDict, Field, create_model
from typing_extensions import TypedDict

from g3ku.agent.tools.base import Tool
from g3ku.providers.base_chat_model_adapter import G3kuChatModelAdapter
from g3ku.providers.fallback import PUBLIC_PROVIDER_FAILURE_MESSAGE
from g3ku.runtime.project_environment import current_project_environment
from main.runtime.chat_backend import build_session_prompt_cache_key
from main.runtime.tool_call_repair import (
    XML_REPAIR_ATTEMPT_LIMIT,
    build_xml_tool_repair_message,
    extract_tool_calls_from_xml_pseudo_content,
    recover_tool_calls_from_json_payload,
)

from ._ceo_support import CeoFrontDoorSupport
from .state_models import CeoRuntimeContext, initial_persistent_state


class CeoGraphState(TypedDict, total=False):
    user_input: Any
    query_text: str
    messages: list[dict[str, Any]]
    tool_names: list[str]
    used_tools: list[str]
    route_kind: str
    repair_overlay_text: str | None
    xml_repair_attempt_count: int
    xml_repair_excerpt: str
    xml_repair_tool_names: list[str]
    xml_repair_last_issue: str
    empty_response_retry_count: int
    heartbeat_internal: bool
    cron_internal: bool
    final_output: str
    error_message: str
    model_refs: list[str]
    prompt_cache_key: str
    parallel_enabled: bool
    max_parallel_tool_calls: int | None
    max_iterations: int | None
    iteration: int
    response_message: Any
    response_content: Any
    synthetic_tool_calls_used: bool
    analysis_text: str
    tool_call_payloads: list[dict[str, Any]]
    next_step: str


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]

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


def _json_schema_annotation(schema: dict[str, Any] | None) -> Any:
    if not isinstance(schema, dict):
        return Any
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        non_null_types = [item for item in schema_type if item != "null"]
        if len(non_null_types) == 1:
            schema_type = non_null_types[0]
        else:
            return Any
    if schema_type == "string":
        return str
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "array":
        item_schema = schema.get("items")
        item_annotation = _json_schema_annotation(item_schema if isinstance(item_schema, dict) else None)
        if item_annotation is Any:
            return list[Any]
        return list[item_annotation]
    if schema_type == "object":
        return dict[str, Any]
    return Any


def _build_args_schema(tool: Tool):
    schema = tool.parameters or {}
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", [])) if isinstance(schema, dict) else set()

    fields: dict[str, tuple[Any, Any]] = {}
    for key, prop in props.items():
        description = prop.get("description") if isinstance(prop, dict) else None
        annotation = _json_schema_annotation(prop if isinstance(prop, dict) else None)
        default = ... if key in required else None
        if description:
            fields[key] = (annotation, Field(default=default, description=description))
        else:
            fields[key] = (annotation, default)

    model_name = "".join(part.capitalize() for part in tool.name.split("_")) + "Args"
    return create_model(model_name, __config__=ConfigDict(extra="allow"), **fields)


def _build_langchain_tool(tool: Tool, executor: ToolExecutor) -> BaseTool:
    async def _invoke(**kwargs: Any) -> Any:
        return await executor(tool.name, kwargs)

    return StructuredTool.from_function(
        coroutine=_invoke,
        name=tool.name,
        description=tool.description,
        args_schema=_build_args_schema(tool),
        infer_schema=False,
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


def _build_langgraph_ceo_graph(runner):
    graph = StateGraph(CeoGraphState, context_schema=CeoRuntimeContext)
    graph.add_node("prepare_turn", runner._graph_prepare_turn)
    graph.add_node("call_model", runner._graph_call_model)
    graph.add_node("normalize_model_output", runner._graph_normalize_model_output)
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


class CeoFrontDoorRunner(CeoFrontDoorSupport):
    """Canonical LangGraph-based CEO frontdoor runner."""

    def __init__(self, *, loop) -> None:
        super().__init__(loop=loop)
        self._compiled_graph = None

    def _get_compiled_graph(self):
        if self._compiled_graph is None:
            self._compiled_graph = _build_langgraph_ceo_graph(self)
        return self._compiled_graph

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
            result_text, status, started_at, finished_at, elapsed_seconds = await self._execute_tool_call(
                tool=tool,
                tool_name=tool_name,
                arguments=arguments,
                runtime_context=runtime_context,
                on_progress=on_progress,
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
    def _model_response_view(message: AIMessage) -> Any:
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

    async def _graph_prepare_turn(
        self,
        state: CeoGraphState,
        *,
        runtime: Runtime[CeoRuntimeContext],
    ) -> dict[str, Any]:
        user_input = _persistent_user_input_payload(state.get("user_input"))
        user_content = _user_input_content(user_input)
        session = runtime.context.session
        on_progress = runtime.context.on_progress
        query_text = self._content_text(user_content)
        metadata = _user_input_metadata(user_input)
        heartbeat_internal = bool(metadata.get("heartbeat_internal"))
        cron_internal = bool(metadata.get("cron_internal"))
        runtime_session = self._loop.sessions.get_or_create(session.state.session_key)
        main_service = getattr(self._loop, "main_task_service", None)
        if main_service is not None:
            await main_service.startup()

        memory_channel = getattr(session, "_memory_channel", getattr(session, "_channel", "cli"))
        memory_chat_id = getattr(session, "_memory_chat_id", getattr(session, "_chat_id", session.state.session_key))
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

        model_refs = self._resolve_ceo_model_refs()
        provider_model = str(model_refs[0] if model_refs else "").strip()
        prompt_cache_key = build_session_prompt_cache_key(
            session_key=str(getattr(session.state, "session_key", "") or ""),
            provider_model=provider_model,
            scope="ceo_frontdoor",
        )
        parallel_enabled, max_parallel_tool_calls = self._parallel_tool_settings()
        return {
            "user_input": user_input,
            "query_text": query_text,
            "messages": messages,
            "tool_names": list(tool_names),
            "used_tools": [],
            "route_kind": "direct_reply",
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
            overlay_text=state.get("repair_overlay_text"),
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
            "response_message": message,
            "response_content": getattr(message, "content", ""),
            "empty_response_retry_count": empty_response_retry_count,
        }

    async def _graph_normalize_model_output(
        self,
        state: CeoGraphState,
        *,
        runtime: Runtime[CeoRuntimeContext],
    ) -> dict[str, Any]:
        response_message = state["response_message"]
        response_view = self._model_response_view(response_message)
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
            return {
                "analysis_text": analysis_text.strip(),
                "tool_call_payloads": tool_call_payloads,
                "synthetic_tool_calls_used": synthetic_tool_calls_used,
                "xml_repair_attempt_count": 0,
                "xml_repair_excerpt": "",
                "xml_repair_tool_names": [],
                "xml_repair_last_issue": "",
                "next_step": "execute_tools",
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
        assistant_message = {
            "role": "assistant",
            "content": None if state.get("synthetic_tool_calls_used") else self._model_content(state.get("response_content", "")),
            "tool_calls": self._assistant_tool_calls_from_payloads(tool_call_payloads),
        }
        messages = list(state.get("messages") or [])
        messages.append(assistant_message)
        messages.extend(tool_messages)

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
            return {
                "messages": messages,
                "used_tools": used_tools,
                "route_kind": route_kind,
                "analysis_text": "",
                "tool_call_payloads": [],
                "synthetic_tool_calls_used": False,
                "final_output": self._task_dispatch_reply(
                    result_text=str(successful_dispatch.get("result_text") or "")
                ),
                "next_step": "finalize",
            }
        return {
            "messages": messages,
            "used_tools": used_tools,
            "route_kind": route_kind,
            "analysis_text": "",
            "tool_call_payloads": [],
            "synthetic_tool_calls_used": False,
            "next_step": "call_model",
        }

    async def _graph_finalize_turn(self, state: CeoGraphState) -> dict[str, Any]:
        output = str(state.get("final_output") or "").strip()
        if not output and not bool(state.get("heartbeat_internal")):
            output = self._empty_reply_fallback(str(state.get("query_text") or ""))
        return {
            "final_output": output,
            "route_kind": str(state.get("route_kind") or "direct_reply"),
        }

    @staticmethod
    def _graph_next_step(state: CeoGraphState) -> str:
        next_step = str(state.get("next_step") or "finalize").strip()
        if next_step not in {"call_model", "execute_tools", "finalize"}:
            return "finalize"
        return next_step

    async def run_turn(self, *, user_input, session, on_progress=None) -> str:
        await self._loop._ensure_checkpointer_ready()
        setattr(session, "_last_route_kind", "direct_reply")
        result = await self._get_compiled_graph().ainvoke(
            initial_persistent_state(user_input=_persistent_user_input_payload(user_input)),
            config={"configurable": {"thread_id": session.state.session_key}},
            context=CeoRuntimeContext(
                loop=self._loop,
                session=session,
                session_key=session.state.session_key,
                on_progress=on_progress,
            ),
        )
        output = str(result.get("final_output") or "").strip()
        setattr(session, "_last_route_kind", str(result.get("route_kind") or "direct_reply"))
        return output


__all__ = ["CeoFrontDoorRunner"]
