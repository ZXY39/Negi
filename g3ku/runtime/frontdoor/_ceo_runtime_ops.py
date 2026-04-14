from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from langchain_core.messages import AIMessage, convert_to_messages
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from g3ku.agent.tools.base import Tool
from g3ku.json_schema_utils import (
    attach_raw_parameters_schema,
    build_args_schema_model,
    normalize_runtime_tool_arguments_dict,
)
from g3ku.providers.base_chat_model_adapter import G3kuChatModelAdapter
from g3ku.providers.fallback import PUBLIC_PROVIDER_FAILURE_MESSAGE
from g3ku.runtime.project_environment import current_project_environment
from g3ku.runtime.semantic_context_summary import default_semantic_context_state
from main.models import normalize_execution_policy_metadata
from main.protocol import now_iso
from main.runtime.chat_backend import build_prompt_cache_diagnostics, build_session_prompt_cache_key
from main.runtime.internal_tools import SubmitNextStageTool
from main.runtime.stage_budget import (
    STAGE_TOOL_NAME,
    response_tool_calls_count_against_stage_budget,
    stage_gate_error_for_tool,
    visible_tools_for_stage_iteration,
)
from main.runtime.stage_messages import build_ceo_stage_overlay, build_ceo_stage_result_block_message
from main.runtime.tool_call_repair import (
    XML_REPAIR_ATTEMPT_LIMIT,
    build_xml_tool_repair_message,
    extract_tool_calls_from_xml_pseudo_content,
    recover_tool_calls_from_json_payload,
)
from g3ku.runtime.web_ceo_sessions import frontdoor_stage_archive_task_id

from ._ceo_support import CeoFrontDoorSupport
from .prompt_cache_contract import build_frontdoor_prompt_contract
from .state_models import (
    CeoFrontdoorInterrupted,
    CeoPendingInterrupt,
    CeoPersistentState,
    CeoRuntimeContext,
)

ToolExecutor = Callable[..., Awaitable[Any]]
CeoGraphState = CeoPersistentState

_TASK_ID_PATTERN = re.compile(r"task:[A-Za-z0-9][\w:-]*")
_FRONTDOOR_STAGE_ARCHIVE_RETAIN_COMPLETED = 20
_FRONTDOOR_STAGE_ARCHIVE_BATCH_SIZE = 10
_FRONTDOOR_STAGE_ARCHIVE_SOURCE_KIND = "stage_history_archive"


@dataclass(slots=True)
class VisibleToolBundle:
    native_tools: dict[str, Tool]
    langchain_tools: list[BaseTool]
    langchain_tool_map: dict[str, BaseTool]


class _CeoStructuredTool(StructuredTool):
    def _to_args_and_kwargs(self, tool_input: str | dict, tool_call_id: str | None) -> tuple[tuple, dict]:
        args, kwargs = super()._to_args_and_kwargs(tool_input, tool_call_id)
        if tool_call_id is not None:
            kwargs["tool_call_id"] = tool_call_id
        return args, kwargs


def _positive_int(value: Any, default: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    return normalized if normalized > 0 else default


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


def _model_visible_tool_contract(tool: Tool) -> tuple[str, dict[str, Any] | None]:
    to_model_schema = getattr(tool, "to_model_schema", None)
    if callable(to_model_schema):
        model_schema = to_model_schema()
        if isinstance(model_schema, dict):
            function_payload = model_schema.get("function")
            function_schema = function_payload if isinstance(function_payload, dict) else model_schema
            description = str(
                function_schema.get("description")
                or getattr(tool, "model_description", "")
                or tool.description
            )
            parameters = function_schema.get("parameters")
            if isinstance(parameters, dict):
                return description, parameters
    model_description = str(getattr(tool, "model_description", "") or tool.description)
    model_parameters = getattr(tool, "model_parameters", None)
    return model_description, model_parameters if isinstance(model_parameters, dict) else tool.parameters


def _ceo_model_compatible_parameters_schema(tool_name: str, schema: dict[str, Any] | None) -> dict[str, Any] | None:
    normalized = copy.deepcopy(schema) if isinstance(schema, dict) else schema
    if str(tool_name or "").strip() != "memory_write" or not isinstance(normalized, dict):
        return normalized
    facts_schema = dict((normalized.get("properties") or {}).get("facts") or {})
    items_schema = dict(facts_schema.get("items") or {})
    fact_properties = items_schema.get("properties")
    if not isinstance(fact_properties, dict):
        return normalized
    value_schema = fact_properties.get("value")
    if not isinstance(value_schema, dict):
        return normalized
    raw_type = value_schema.get("type")
    if not isinstance(raw_type, list) or not any(item in {"object", "array"} for item in raw_type):
        return normalized
    value_schema["type"] = "string"
    description = str(value_schema.get("description") or "").strip()
    compatibility_note = (
        "For CEO frontdoor model compatibility, pass structured values as JSON-serialized strings."
    )
    if compatibility_note not in description:
        value_schema["description"] = f"{description} {compatibility_note}".strip() if description else compatibility_note
    fact_properties["value"] = value_schema
    items_schema["properties"] = fact_properties
    facts_schema["items"] = items_schema
    normalized["properties"] = {
        **dict(normalized.get("properties") or {}),
        "facts": facts_schema,
    }
    return normalized


def _build_langchain_tool(tool: Tool, executor: ToolExecutor) -> BaseTool:
    executor_params = inspect.signature(executor).parameters
    executor_accepts_tool_call_id = "tool_call_id" in executor_params or any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in executor_params.values()
    )

    async def _invoke(*, tool_call_id: str | None = None, **kwargs: Any) -> Any:
        filtered_kwargs = {
            str(key): value
            for key, value in dict(kwargs or {}).items()
            if value is not None
        }
        normalized_kwargs = normalize_runtime_tool_arguments_dict(filtered_kwargs)
        if executor_accepts_tool_call_id:
            return await executor(
                tool.name,
                normalized_kwargs,
                tool_call_id=str(tool_call_id or "").strip() or None,
            )
        return await executor(tool.name, normalized_kwargs)

    model_description, model_parameters = _model_visible_tool_contract(tool)
    compatible_model_parameters = _ceo_model_compatible_parameters_schema(tool.name, model_parameters)
    return attach_raw_parameters_schema(
        _CeoStructuredTool.from_function(
            coroutine=_invoke,
            name=tool.name,
            description=model_description,
            args_schema=build_args_schema_model(tool.name, compatible_model_parameters),
            infer_schema=False,
        ),
        compatible_model_parameters,
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


async def _invoke_execute_tool_call_compat(execute_tool_call: ToolExecutor, /, **kwargs: Any) -> Any:
    try:
        parameters = inspect.signature(execute_tool_call).parameters
    except (TypeError, ValueError):
        return await execute_tool_call(**kwargs)
    if "tool_call_id" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return await execute_tool_call(**kwargs)
    legacy_kwargs = dict(kwargs)
    legacy_kwargs.pop("tool_call_id", None)
    return await execute_tool_call(**legacy_kwargs)


def _normalize_frontdoor_tool_arguments(tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    normalized = normalize_runtime_tool_arguments_dict(arguments)
    if str(tool_name or "").strip() not in {"create_async_task", "continue_task"}:
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


class CeoFrontDoorRuntimeOps(CeoFrontDoorSupport):
    """Shared CEO runtime operations reused by the create_agent frontdoor path."""

    def __init__(self, *, loop) -> None:
        super().__init__(loop=loop)
        self._compiled_graph = None

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
        return _checkpoint_safe_value(value)

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
            if isinstance(interrupt_state.get("frontdoor_stage_state"), dict):
                values["frontdoor_stage_state"] = dict(interrupt_state.get("frontdoor_stage_state") or {})
            if isinstance(interrupt_state.get("compression_state"), dict):
                values["compression_state"] = dict(interrupt_state.get("compression_state") or {})
            raise CeoFrontdoorInterrupted(interrupts=interrupts, values=values)
        return values

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
    def _prompt_message_records(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in list(messages or []):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in {"system", "user", "assistant", "tool"}:
                continue
            normalized.append(dict(item))
        return normalized

    @classmethod
    def _heartbeat_stable_prefix_messages(
        cls,
        *,
        assembly: Any,
        metadata: dict[str, Any],
        live_request_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        heartbeat_stable_rules_text = str(metadata.get("heartbeat_stable_rules_text") or "").strip()
        if heartbeat_stable_rules_text:
            metadata_stable_messages: list[dict[str, Any]] = [
                {"role": "system", "content": heartbeat_stable_rules_text}
            ]
            heartbeat_task_ledger_summary = str(metadata.get("heartbeat_task_ledger_summary") or "").strip()
            if heartbeat_task_ledger_summary:
                metadata_stable_messages.append(
                    {"role": "assistant", "content": heartbeat_task_ledger_summary}
                )
            return metadata_stable_messages
        explicit_stable_messages = cls._prompt_message_records(getattr(assembly, "stable_messages", None))
        if explicit_stable_messages:
            return explicit_stable_messages
        if not live_request_messages:
            return []
        if str(live_request_messages[-1].get("role") or "").strip().lower() == "user":
            fallback_prefix = live_request_messages[:-1]
            if fallback_prefix:
                return fallback_prefix
        if str(live_request_messages[0].get("role") or "").strip().lower() == "system":
            return [live_request_messages[0]]
        return []

    @staticmethod
    def _effective_turn_overlay_text(state: CeoGraphState) -> str:
        return _join_overlay_text(
            state.get("turn_overlay_text"),
            state.get("repair_overlay_text"),
        )

    @staticmethod
    def _default_frontdoor_stage_state() -> dict[str, Any]:
        return {
            "active_stage_id": "",
            "transition_required": False,
            "stages": [],
        }

    @staticmethod
    def _default_compression_state() -> dict[str, Any]:
        return {
            "status": "",
            "text": "",
            "source": "",
            "needs_recheck": False,
        }

    @staticmethod
    def _default_semantic_context_state() -> dict[str, Any]:
        return default_semantic_context_state()

    @staticmethod
    def _normalized_hydrated_tool_names(raw: Any) -> list[str]:
        normalized: list[str] = []
        for item in list(raw or []):
            name = str(item or "").strip()
            if name and name not in normalized:
                normalized.append(name)
        return normalized

    @classmethod
    def _runtime_session_frontdoor_state(
        cls,
        state: CeoGraphState | None,
        *,
        preview_pending_tool_round: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
        frontdoor_stage_state = cls._frontdoor_stage_state_snapshot(state)
        if preview_pending_tool_round and isinstance(state, dict):
            frontdoor_stage_state = cls._record_frontdoor_stage_round(
                frontdoor_stage_state,
                tool_call_payloads=list(state.get("tool_call_payloads") or []),
            )
        compression_state = (
            dict(state.get("compression_state") or cls._default_compression_state())
            if isinstance(state, dict)
            else cls._default_compression_state()
        )
        semantic_context_state = (
            dict(state.get("semantic_context_state") or cls._default_semantic_context_state())
            if isinstance(state, dict)
            else cls._default_semantic_context_state()
        )
        hydrated_tool_names = (
            cls._normalized_hydrated_tool_names(state.get("hydrated_tool_names"))
            if isinstance(state, dict)
            else []
        )
        return frontdoor_stage_state, compression_state, semantic_context_state, hydrated_tool_names

    def _sync_runtime_session_frontdoor_state(
        self,
        *,
        state: CeoGraphState | None,
        runtime: Runtime[CeoRuntimeContext] | None = None,
        session: Any | None = None,
        preview_pending_tool_round: bool = False,
    ) -> None:
        target_session = session or getattr(getattr(runtime, "context", None), "session", None)
        if target_session is None:
            return
        frontdoor_stage_state, compression_state, semantic_context_state, hydrated_tool_names = self._runtime_session_frontdoor_state(
            state,
            preview_pending_tool_round=preview_pending_tool_round,
        )
        setattr(target_session, "_frontdoor_stage_state", frontdoor_stage_state)
        setattr(target_session, "_compression_state", compression_state)
        setattr(target_session, "_semantic_context_state", semantic_context_state)
        setattr(target_session, "_frontdoor_hydrated_tool_names", list(hydrated_tool_names))

    @classmethod
    def _frontdoor_stage_state_snapshot(cls, state: CeoGraphState | None) -> dict[str, Any]:
        raw = {}
        if isinstance(state, dict):
            raw_value = state.get("frontdoor_stage_state")
            raw = dict(raw_value) if isinstance(raw_value, dict) else {}
        active_stage_id = str(raw.get("active_stage_id") or "").strip()
        normalized_stages: list[dict[str, Any]] = []
        for index, raw_stage in enumerate(list(raw.get("stages") or []), start=1):
            if not isinstance(raw_stage, dict):
                continue
            stage_id = str(raw_stage.get("stage_id") or f"frontdoor-stage-{index}").strip()
            stage_status = str(raw_stage.get("status") or "").strip() or (
                "active" if stage_id and stage_id == active_stage_id else "completed"
            )
            normalized_stages.append(
                {
                    "stage_id": stage_id,
                    "stage_index": int(raw_stage.get("stage_index") or index),
                    "stage_goal": str(raw_stage.get("stage_goal") or "").strip(),
                    "tool_round_budget": max(0, int(raw_stage.get("tool_round_budget") or 0)),
                    "tool_rounds_used": max(0, int(raw_stage.get("tool_rounds_used") or 0)),
                    "status": stage_status,
                    "mode": str(raw_stage.get("mode") or "自主执行").strip() or "自主执行",
                    "stage_kind": str(raw_stage.get("stage_kind") or "normal").strip() or "normal",
                    "system_generated": bool(raw_stage.get("system_generated", False)),
                    "completed_stage_summary": str(raw_stage.get("completed_stage_summary") or "").strip(),
                    "final_stage": bool(raw_stage.get("final_stage", False)),
                    "key_refs": [
                        dict(item)
                        for item in list(raw_stage.get("key_refs") or [])
                        if isinstance(item, dict)
                    ],
                    "archive_ref": str(raw_stage.get("archive_ref") or "").strip(),
                    "archive_stage_index_start": max(0, int(raw_stage.get("archive_stage_index_start") or 0)),
                    "archive_stage_index_end": max(0, int(raw_stage.get("archive_stage_index_end") or 0)),
                    "rounds": [
                        dict(item)
                        for item in list(raw_stage.get("rounds") or [])
                        if isinstance(item, dict)
                    ],
                    "created_at": str(raw_stage.get("created_at") or ""),
                    "finished_at": str(raw_stage.get("finished_at") or ""),
                }
            )
        if active_stage_id and not any(
            str(stage.get("stage_id") or "").strip() == active_stage_id
            and str(stage.get("status") or "").strip().lower() == "active"
            for stage in normalized_stages
        ):
            active_stage_id = ""
        transition_required = bool(raw.get("transition_required"))
        if not active_stage_id:
            transition_required = False
        return {
            "active_stage_id": active_stage_id,
            "transition_required": transition_required,
            "stages": normalized_stages,
        }

    @classmethod
    def _frontdoor_stage_gate(cls, state: CeoGraphState | None) -> dict[str, Any]:
        stage_state = cls._frontdoor_stage_state_snapshot(state)
        active_stage_id = str(stage_state.get("active_stage_id") or "").strip()
        active_stage = next(
            (
                dict(stage)
                for stage in list(stage_state.get("stages") or [])
                if str(stage.get("stage_id") or "").strip() == active_stage_id
                and str(stage.get("status") or "").strip().lower() == "active"
            ),
            None,
        )
        completed_stages = [
            dict(stage)
            for stage in list(stage_state.get("stages") or [])
            if active_stage is None or str(stage.get("stage_id") or "").strip() != str(active_stage.get("stage_id") or "").strip()
        ]
        return {
            "enabled": True,
            "has_active_stage": active_stage is not None,
            "transition_required": bool(stage_state.get("transition_required")),
            "active_stage": active_stage,
            "completed_stages": completed_stages,
        }

    @staticmethod
    def _frontdoor_stage_has_substantive_progress(active_stage: dict[str, Any] | None) -> bool:
        if not isinstance(active_stage, dict):
            return False
        non_substantive = {STAGE_TOOL_NAME, *CeoFrontDoorSupport._CONTROL_TOOL_NAMES}
        for round_item in list(active_stage.get("rounds") or []):
            if not isinstance(round_item, dict):
                continue
            tools = [dict(item) for item in list(round_item.get("tools") or []) if isinstance(item, dict)]
            tool_names = [
                str(item.get("tool_name") or "").strip()
                for item in tools
                if str(item.get("tool_name") or "").strip()
            ]
            if not tool_names:
                tool_names = [
                    str(name or "").strip()
                    for name in list(round_item.get("tool_names") or [])
                    if str(name or "").strip()
                ]
            if any(name not in non_substantive for name in tool_names):
                return True
        return False

    @classmethod
    def _submit_frontdoor_next_stage_state(
        cls,
        stage_state: dict[str, Any],
        *,
        stage_goal: str,
        tool_round_budget: int,
        completed_stage_summary: str = "",
        key_refs: list[dict[str, Any]] | None = None,
        final: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        normalized_state = cls._frontdoor_stage_state_snapshot({"frontdoor_stage_state": stage_state})
        normalized_goal = str(stage_goal or "").strip()
        normalized_budget = int(tool_round_budget or 0)
        normalized_summary = str(completed_stage_summary or "").strip()
        normalized_key_refs = [dict(item) for item in list(key_refs or []) if isinstance(item, dict)]
        if not normalized_goal:
            raise ValueError("stage_goal must not be empty")
        if normalized_budget < 1 or normalized_budget > 10:
            raise ValueError("tool_round_budget must be between 1 and 10")

        active_stage_id = str(normalized_state.get("active_stage_id") or "").strip()
        active_stage = next(
            (
                dict(stage)
                for stage in list(normalized_state.get("stages") or [])
                if str(stage.get("stage_id") or "").strip() == active_stage_id
                and str(stage.get("status") or "").strip().lower() == "active"
            ),
            None,
        )
        if active_stage is not None and not cls._frontdoor_stage_has_substantive_progress(active_stage):
            raise ValueError(
                "current active stage has no substantive progress yet; "
                "do not call submit_next_stage again before using a non-control tool "
                "in this stage"
            )

        now = now_iso()
        stages: list[dict[str, Any]] = []
        for stage in list(normalized_state.get("stages") or []):
            current = dict(stage)
            if (
                active_stage is not None
                and str(current.get("stage_id") or "").strip() == str(active_stage.get("stage_id") or "").strip()
                and str(current.get("status") or "").strip().lower() == "active"
            ):
                current.update(
                    {
                        "status": "completed",
                        "finished_at": now,
                        "completed_stage_summary": normalized_summary,
                        "key_refs": normalized_key_refs,
                    }
                )
            stages.append(current)

        next_stage_index = max((int(stage.get("stage_index") or 0) for stage in stages), default=0) + 1
        next_stage = {
            "stage_id": f"frontdoor-stage-{next_stage_index}",
            "stage_index": next_stage_index,
            "stage_kind": "normal",
            "system_generated": False,
            "mode": "自主执行",
            "status": "active",
            "stage_goal": normalized_goal,
            "completed_stage_summary": "",
            "final_stage": bool(final),
            "key_refs": [],
            "tool_round_budget": normalized_budget,
            "tool_rounds_used": 0,
            "created_at": now,
            "finished_at": "",
            "rounds": [],
        }
        next_state = {
            "active_stage_id": str(next_stage.get("stage_id") or ""),
            "transition_required": False,
            "stages": [*stages, next_stage],
        }
        return next_state, next_stage

    @classmethod
    def _record_frontdoor_stage_round(
        cls,
        stage_state: dict[str, Any],
        *,
        tool_call_payloads: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        normalized_state = cls._frontdoor_stage_state_snapshot({"frontdoor_stage_state": stage_state})
        active_stage_id = str(normalized_state.get("active_stage_id") or "").strip()
        if not active_stage_id or bool(normalized_state.get("transition_required")):
            return normalized_state
        visible_calls = [
            dict(item)
            for item in list(tool_call_payloads or [])
            if str(item.get("name") or "").strip() and str(item.get("name") or "").strip() != STAGE_TOOL_NAME
        ]
        if not visible_calls:
            return normalized_state
        counts_budget = response_tool_calls_count_against_stage_budget(
            visible_calls,
            extra_non_budget_tools=CeoFrontDoorSupport._CONTROL_TOOL_NAMES,
        )
        stages: list[dict[str, Any]] = []
        latest_active: dict[str, Any] | None = None
        for stage in list(normalized_state.get("stages") or []):
            current = dict(stage)
            if (
                str(current.get("stage_id") or "").strip() == active_stage_id
                and str(current.get("status") or "").strip().lower() == "active"
            ):
                rounds = [dict(item) for item in list(current.get("rounds") or []) if isinstance(item, dict)]
                round_index = len(rounds) + 1
                rounds.append(
                    {
                        "round_id": f"{active_stage_id}:round-{round_index}",
                        "round_index": round_index,
                        "created_at": now_iso(),
                        "tool_names": [
                            str(item.get("name") or "").strip()
                            for item in visible_calls
                            if str(item.get("name") or "").strip()
                        ],
                        "tool_call_ids": [
                            str(item.get("id") or "").strip()
                            for item in visible_calls
                            if str(item.get("id") or "").strip()
                        ],
                        "budget_counted": counts_budget,
                        "tools": [
                            dict(item)
                            for item in list(tools or [])
                            if isinstance(item, dict)
                        ],
                    }
                )
                next_used = int(current.get("tool_rounds_used") or 0) + (1 if counts_budget else 0)
                budget = int(current.get("tool_round_budget") or 0)
                if budget > 0:
                    next_used = min(next_used, budget)
                current.update(
                    {
                        "tool_rounds_used": next_used,
                        "rounds": rounds,
                    }
                )
                latest_active = current
            stages.append(current)
        return {
            "active_stage_id": active_stage_id,
            "transition_required": bool(
                latest_active is not None
                and not bool(latest_active.get("final_stage"))
                and int(latest_active.get("tool_round_budget") or 0) > 0
                and int(latest_active.get("tool_rounds_used") or 0) >= int(latest_active.get("tool_round_budget") or 0)
            ),
            "stages": stages,
        }

    def _externalize_frontdoor_stage_archive(
        self,
        *,
        session_key: str,
        stage_index_start: int,
        stage_index_end: int,
        stages: list[dict[str, Any]],
    ) -> tuple[str, str]:
        normalized_session_key = str(session_key or "").strip()
        if not normalized_session_key:
            return "", ""
        service = getattr(self._loop, "main_task_service", None)
        content_store = getattr(service, "content_store", None) if service is not None else None
        summarize = getattr(content_store, "summarize_for_storage", None) if content_store is not None else None
        if not callable(summarize):
            return "", ""
        archive_payload = {
            "session_id": normalized_session_key,
            "stage_index_start": stage_index_start,
            "stage_index_end": stage_index_end,
            "stages": [dict(stage) for stage in list(stages or []) if isinstance(stage, dict)],
        }
        summary, ref = summarize(
            json.dumps(archive_payload, ensure_ascii=False, indent=2),
            runtime={
                "task_id": frontdoor_stage_archive_task_id(normalized_session_key),
                "session_key": normalized_session_key,
            },
            display_name=f"stage-history:frontdoor:{stage_index_start}-{stage_index_end}",
            source_kind=_FRONTDOOR_STAGE_ARCHIVE_SOURCE_KIND,
            force=True,
        )
        return str(summary or "").strip(), str(ref or "").strip()

    def _externalize_completed_frontdoor_stage_batches(
        self,
        *,
        session_key: str,
        stage_state: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_state = self._frontdoor_stage_state_snapshot({"frontdoor_stage_state": stage_state})
        stages = [dict(stage) for stage in list(normalized_state.get("stages") or []) if isinstance(stage, dict)]
        if not stages:
            return normalized_state
        while True:
            completed_normal = [
                (index, stage)
                for index, stage in enumerate(stages)
                if str(stage.get("stage_kind") or "normal").strip() == "normal"
                and str(stage.get("status") or "").strip().lower() != "active"
            ]
            if len(completed_normal) <= _FRONTDOOR_STAGE_ARCHIVE_RETAIN_COMPLETED:
                break
            batch = completed_normal[:_FRONTDOOR_STAGE_ARCHIVE_BATCH_SIZE]
            archive_stages = [dict(stage) for _, stage in batch]
            if not archive_stages:
                break
            stage_index_start = int(batch[0][1].get("stage_index") or 0)
            stage_index_end = int(batch[-1][1].get("stage_index") or 0)
            archive_summary, archive_ref = self._externalize_frontdoor_stage_archive(
                session_key=session_key,
                stage_index_start=stage_index_start,
                stage_index_end=stage_index_end,
                stages=archive_stages,
            )
            if not archive_ref:
                break
            compression_stage = {
                "stage_id": f"frontdoor-compression-{stage_index_start}-{stage_index_end}",
                "stage_index": stage_index_end,
                "stage_kind": "compression",
                "system_generated": True,
                "mode": "鑷富鎵ц",
                "status": "completed",
                "stage_goal": f"Archive completed stage history {stage_index_start}-{stage_index_end}",
                "completed_stage_summary": (
                    archive_summary
                    or f"Archived completed stages {stage_index_start}-{stage_index_end} into stage history archive."
                ),
                "key_refs": [],
                "archive_ref": archive_ref,
                "archive_stage_index_start": stage_index_start,
                "archive_stage_index_end": stage_index_end,
                "tool_round_budget": 0,
                "tool_rounds_used": 0,
                "created_at": now_iso(),
                "finished_at": now_iso(),
                "rounds": [],
            }
            batch_indexes = {index for index, _stage in batch}
            insert_at = min(batch_indexes)
            next_stages: list[dict[str, Any]] = []
            for index, stage in enumerate(stages):
                if index == insert_at:
                    next_stages.append(compression_stage)
                if index in batch_indexes:
                    continue
                next_stages.append(dict(stage))
            stages = next_stages
        return {
            "active_stage_id": str(normalized_state.get("active_stage_id") or "").strip(),
            "transition_required": bool(normalized_state.get("transition_required")),
            "stages": stages,
        }

    def _frontdoor_round_tool_entry(
        self,
        *,
        payload: dict[str, Any],
        result: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        tool_name = str(payload.get("name") or result.get("tool_name") or "tool").strip() or "tool"
        tool_call_id = str(payload.get("id") or result.get("tool_call_id") or "").strip()
        arguments = dict(payload.get("arguments") or {}) if isinstance(payload.get("arguments"), dict) else {}
        result_text = str(result.get("result_text") or "")
        status = str(result.get("status") or self._tool_status(result_text)).strip().lower() or "success"
        tool_message = dict(result.get("tool_message") or {}) if isinstance(result.get("tool_message"), dict) else {}
        progress_payload = self._tool_result_progress_event_data(
            tool_name=tool_name,
            result_text=result_text,
            tool_call_id=tool_call_id or None,
        )
        timestamp = str(
            tool_message.get("finished_at")
            or result.get("finished_at")
            or tool_message.get("started_at")
            or result.get("started_at")
            or ""
        ).strip()
        item: dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "arguments_text": self._tool_invocation_hint(tool_name, arguments),
            "output_text": result_text,
            "output_ref": str(progress_payload.get("output_ref") or "").strip(),
            "status": status,
            "started_at": str(tool_message.get("started_at") or result.get("started_at") or "").strip(),
            "finished_at": str(tool_message.get("finished_at") or result.get("finished_at") or "").strip(),
            "timestamp": timestamp,
            "kind": "tool_result" if status == "success" else "tool_error",
            "source": str(source or "user").strip().lower() or "user",
        }
        output_preview_text = str(progress_payload.get("output_preview_text") or "").strip()
        if output_preview_text:
            item["output_preview_text"] = output_preview_text
        elapsed_seconds = result.get("elapsed_seconds", tool_message.get("elapsed_seconds"))
        if isinstance(elapsed_seconds, (int, float)):
            item["elapsed_seconds"] = float(elapsed_seconds)
        return item

    def _frontdoor_stage_state_after_tool_cycle(
        self,
        state: CeoGraphState,
        *,
        tool_call_payloads: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        stage_state = self._frontdoor_stage_state_snapshot(state)
        ordinary_calls: list[dict[str, Any]] = []
        ordinary_results: list[dict[str, Any]] = []
        source = "cron" if bool(state.get("cron_internal")) else "heartbeat" if bool(state.get("heartbeat_internal")) else "user"
        for payload, result in zip(list(tool_call_payloads or []), list(tool_results or []), strict=False):
            tool_name = str(payload.get("name") or "").strip()
            status = str(result.get("status") or "").strip().lower()
            if tool_name == STAGE_TOOL_NAME:
                if status != "error":
                    stage_state, _ = self._submit_frontdoor_next_stage_state(
                        stage_state,
                        stage_goal=str(dict(payload.get("arguments") or {}).get("stage_goal") or ""),
                        tool_round_budget=int(dict(payload.get("arguments") or {}).get("tool_round_budget") or 0),
                        completed_stage_summary=str(
                            dict(payload.get("arguments") or {}).get("completed_stage_summary") or ""
                        ),
                        key_refs=[
                            dict(item)
                            for item in list(dict(payload.get("arguments") or {}).get("key_refs") or [])
                            if isinstance(item, dict)
                        ],
                        final=bool(dict(payload.get("arguments") or {}).get("final")),
                    )
                continue
            ordinary_calls.append(dict(payload))
            ordinary_results.append(dict(result))
        updated_state = self._record_frontdoor_stage_round(
            stage_state,
            tool_call_payloads=ordinary_calls,
            tools=[
                self._frontdoor_round_tool_entry(payload=payload, result=result, source=source)
                for payload, result in zip(list(ordinary_calls or []), list(ordinary_results or []), strict=False)
            ],
        )
        return self._externalize_completed_frontdoor_stage_batches(
            session_key=str(state.get("session_key") or "").strip(),
            stage_state=updated_state,
        )

    @classmethod
    def _complete_active_frontdoor_stage_state(
        cls,
        stage_state: dict[str, Any] | None,
        *,
        completed_stage_summary: str = "",
    ) -> dict[str, Any]:
        normalized_state = cls._frontdoor_stage_state_snapshot({"frontdoor_stage_state": stage_state or {}})
        active_stage_id = str(normalized_state.get("active_stage_id") or "").strip()
        if not active_stage_id:
            return normalized_state

        now = now_iso()
        normalized_summary = str(completed_stage_summary or "").strip()
        stages: list[dict[str, Any]] = []
        completed_any = False
        for stage in list(normalized_state.get("stages") or []):
            current = dict(stage)
            if (
                str(current.get("stage_id") or "").strip() == active_stage_id
                and str(current.get("status") or "").strip().lower() == "active"
            ):
                current["status"] = "completed"
                current["finished_at"] = str(current.get("finished_at") or "").strip() or now
                if normalized_summary and not str(current.get("completed_stage_summary") or "").strip():
                    current["completed_stage_summary"] = normalized_summary
                completed_any = True
            stages.append(current)

        return {
            "active_stage_id": "" if completed_any else active_stage_id,
            "transition_required": False if completed_any else bool(normalized_state.get("transition_required")),
            "stages": stages,
        }

    @classmethod
    def _frontdoor_stage_gate_error(cls, *, tool_name: str, stage_state: dict[str, Any]) -> str:
        snapshot = cls._frontdoor_stage_state_snapshot({"frontdoor_stage_state": stage_state})
        return stage_gate_error_for_tool(
            tool_name,
            has_active_stage=bool(str(snapshot.get("active_stage_id") or "").strip()),
            transition_required=bool(snapshot.get("transition_required")),
            extra_allowed_tools=cls._CONTROL_TOOL_NAMES,
            stage_tool_name=STAGE_TOOL_NAME,
        )

    @classmethod
    def _frontdoor_default_overlay_text(cls, state: CeoGraphState) -> str:
        stage_gate = cls._frontdoor_stage_gate(state)
        return _join_overlay_text(
            build_ceo_stage_overlay(stage_gate),
            build_ceo_stage_result_block_message(stage_gate),
        )

    def _reviewable_tool_names(self) -> set[str]:
        assembly_cfg = getattr(getattr(self._loop, "_memory_runtime_settings", None), "assembly", None)
        if not bool(getattr(assembly_cfg, "frontdoor_interrupt_approval_enabled", False)):
            return set()
        raw_names = list(
            getattr(assembly_cfg, "frontdoor_interrupt_tool_names", ["message", "create_async_task", "continue_task"]) or []
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
        mutable_stage_state = self._frontdoor_stage_state_snapshot(state)

        async def _submit_stage(
            stage_goal: str,
            tool_round_budget: int,
            completed_stage_summary: str = "",
            key_refs: list[dict[str, Any]] | None = None,
            final: bool = False,
        ) -> dict[str, Any]:
            next_stage_state, stage_payload = self._submit_frontdoor_next_stage_state(
                mutable_stage_state,
                stage_goal=stage_goal,
                tool_round_budget=tool_round_budget,
                completed_stage_summary=completed_stage_summary,
                key_refs=key_refs,
                final=final,
            )
            mutable_stage_state.clear()
            mutable_stage_state.update(next_stage_state)
            return stage_payload

        all_tools = {
            **registered_tools,
            STAGE_TOOL_NAME: SubmitNextStageTool(_submit_stage),
        }
        stage_gate = self._frontdoor_stage_gate({"frontdoor_stage_state": mutable_stage_state})
        visible_tools = visible_tools_for_stage_iteration(
            all_tools,
            has_active_stage=bool(stage_gate.get("has_active_stage")),
            transition_required=bool(stage_gate.get("transition_required")),
            stage_tool_name=STAGE_TOOL_NAME,
        )
        runtime_context = self._build_tool_runtime_context(state=state, runtime=runtime)
        on_progress = runtime_context.get("on_progress")

        async def _tool_executor(
            tool_name: str,
            arguments: dict[str, Any],
            tool_call_id: str | None = None,
        ) -> dict[str, Any]:
            tool = visible_tools.get(tool_name)
            if tool is None:
                return {
                    "result_text": f"Error: tool not available: {tool_name}",
                    "status": "error",
                    "started_at": "",
                    "finished_at": "",
                    "elapsed_seconds": None,
                }
            gate_error = self._frontdoor_stage_gate_error(tool_name=tool_name, stage_state=mutable_stage_state)
            if gate_error:
                return {
                    "result_text": f"Error: {gate_error}",
                    "status": "error",
                    "started_at": "",
                    "finished_at": "",
                    "elapsed_seconds": None,
                }
            normalized_arguments = _normalize_frontdoor_tool_arguments(tool_name, arguments)
            result_text, status, started_at, finished_at, elapsed_seconds = await _invoke_execute_tool_call_compat(
                self._execute_tool_call,
                tool=tool,
                tool_name=tool_name,
                arguments=normalized_arguments,
                runtime_context=runtime_context,
                on_progress=on_progress,
                tool_call_id=tool_call_id,
            )
            await self._emit_progress(
                on_progress,
                result_text,
                event_kind="tool_result" if status == "success" else "tool_error",
                event_data=self._tool_result_progress_event_data(
                    tool_name=tool_name,
                    result_text=result_text,
                    tool_call_id=tool_call_id,
                ),
            )
            return {
                "result_text": result_text,
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "elapsed_seconds": elapsed_seconds,
            }

        tool_bundle = _build_visible_tool_bundle(
            tools=visible_tools,
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
    def _normalize_task_id_value(value: Any) -> str:
        normalized = CeoFrontDoorRuntimeOps._normalize_task_ids(value)
        return normalized[0] if normalized else ""

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

    @staticmethod
    def _json_object_payload(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        text = str(value or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

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
        if getattr(getattr(runtime, "context", None), "session", None) is None:
            return {
                "session_key": str(state.get("session_key") or "").strip(),
                "messages": list(state.get("messages") or []),
                "frontdoor_stage_state": self._frontdoor_stage_state_snapshot(state),
                "compression_state": dict(state.get("compression_state") or self._default_compression_state()),
            }

        user_input = _persistent_user_input_payload(state.get("user_input"))
        user_content = _user_input_content(user_input)
        session = runtime.context.session
        metadata = _user_input_metadata(user_input)
        batch_query_text = str(metadata.get("web_ceo_batch_query_text") or "").strip()
        query_text = batch_query_text or self._content_text(user_content)
        heartbeat_internal = bool(metadata.get("heartbeat_internal"))
        cron_internal = bool(metadata.get("cron_internal"))
        retrieval_query = str(metadata.get("heartbeat_retrieval_query") or "").strip()
        builder_query_text = retrieval_query if heartbeat_internal and retrieval_query else query_text
        hydrated_tool_names: list[str] = []
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
            query_text=builder_query_text,
            exposure=exposure,
            persisted_session=runtime_session,
            checkpoint_messages=list(state.get("messages") or []),
            user_content=self._model_content(user_content),
            user_metadata=metadata,
            frontdoor_stage_state=self._frontdoor_stage_state_snapshot(state),
            semantic_context_state=dict(state.get("semantic_context_state") or {}),
            hydrated_tool_names=list(hydrated_tool_names),
        )
        tool_names = list(
            getattr(assembly, "tool_names", None)
            or getattr(assembly, "callable_tool_names", None)
            or []
        )
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
        tool_schemas = self._selected_tool_schemas(tool_names)
        stable_messages = list(messages)
        dynamic_appendix_messages: list[dict[str, Any]] = []
        cache_family_revision = str(getattr(assembly, "cache_family_revision", "") or "").strip()
        prompt_scope = "ceo_frontdoor"
        if heartbeat_internal:
            live_request_messages = self._prompt_message_records(messages)
            stable_messages = self._heartbeat_stable_prefix_messages(
                assembly=assembly,
                metadata=metadata,
                live_request_messages=live_request_messages,
            )
            if str(metadata.get("heartbeat_stable_rules_text") or "").strip():
                dynamic_appendix_messages = [
                    {"role": "user", "content": self._model_content(user_content)}
                ]
                live_request_messages = [*list(stable_messages), *list(dynamic_appendix_messages)]
            else:
                dynamic_appendix_messages = self._prompt_message_records(
                    getattr(assembly, "dynamic_appendix_messages", None)
                )
            prompt_scope = str(metadata.get("heartbeat_prompt_lane") or "ceo_heartbeat").strip() or "ceo_heartbeat"
            contract = build_frontdoor_prompt_contract(
                scope=prompt_scope,
                provider_model=provider_model,
                stable_messages=stable_messages,
                dynamic_appendix_messages=dynamic_appendix_messages,
                live_request_messages=live_request_messages,
                tool_schemas=tool_schemas,
                cache_family_revision=cache_family_revision,
                session_key=str(getattr(session.state, "session_key", "") or ""),
                overlay_text=str(getattr(assembly, "turn_overlay_text", "") or ""),
                overlay_section_count=int(getattr(assembly, "trace", {}).get("turn_overlay_section_count", 0) or 0),
            )
            messages = list(contract.request_messages)
            stable_messages = list(contract.stable_messages)
            dynamic_appendix_messages = list(contract.dynamic_appendix_messages)
            cache_family_revision = str(contract.cache_family_revision or "").strip()
            prompt_cache_key = contract.prompt_cache_key
            prompt_cache_diagnostics = dict(contract.diagnostics)
        else:
            prompt_cache_key = build_session_prompt_cache_key(
                session_key=str(getattr(session.state, "session_key", "") or ""),
                provider_model=provider_model,
                scope=prompt_scope,
                stable_messages=messages,
                tool_schemas=tool_schemas,
            )
            prompt_cache_diagnostics = build_prompt_cache_diagnostics(
                stable_messages=messages,
                tool_schemas=tool_schemas,
                provider_model=provider_model,
                scope=prompt_scope,
                prompt_cache_key=prompt_cache_key,
                overlay_text=str(getattr(assembly, "turn_overlay_text", "") or ""),
                overlay_section_count=int(getattr(assembly, "trace", {}).get("turn_overlay_section_count", 0) or 0),
            )
        parallel_enabled, max_parallel_tool_calls = self._parallel_tool_settings()
        return {
            "session_key": str(getattr(session.state, "session_key", "") or ""),
            "user_input": user_input,
            "approval_request": None,
            "approval_status": "",
            "query_text": query_text,
            "messages": messages,
            "frontdoor_stage_state": self._frontdoor_stage_state_snapshot(state),
            "compression_state": dict(
                getattr(assembly, "trace", {}).get("compression_state_payload")
                or state.get("compression_state")
                or self._default_compression_state()
            ),
            "semantic_context_state": dict(
                getattr(assembly, "trace", {}).get("semantic_context_state")
                or state.get("semantic_context_state")
                or {}
            ),
            "turn_overlay_text": str(getattr(assembly, "turn_overlay_text", "") or "").strip() or None,
            "tool_names": list(tool_names),
            "candidate_tool_names": list(getattr(assembly, "candidate_tool_names", []) or []),
            "hydrated_tool_names": list(hydrated_tool_names),
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
            "stable_messages": stable_messages,
            "dynamic_appendix_messages": dynamic_appendix_messages,
            "cache_family_revision": cache_family_revision,
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
        stage_gate = self._frontdoor_stage_gate(state)

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
                bool(stage_gate.get("transition_required"))
                and str(current_route_kind or "direct_reply") == "direct_reply"
                and not str(state.get("repair_overlay_text") or "").strip()
            ):
                return {
                    "repair_overlay_text": (
                        build_ceo_stage_result_block_message(stage_gate)
                        or build_ceo_stage_overlay(stage_gate)
                    ),
                    "final_output": "",
                    "next_step": "call_model",
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
                    result_text, status, started_at, finished_at, elapsed_seconds = await _invoke_execute_tool_call_compat(
                        self._execute_tool_call,
                        tool=tool,
                        tool_name=tool_name,
                        arguments=dict(payload.get("arguments") or {}),
                        runtime_context=runtime_context,
                        on_progress=on_progress,
                        tool_call_id=str(payload.get("id") or "").strip() or None,
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
                event_data=self._tool_result_progress_event_data(
                    tool_name=tool_name,
                    result_text=result_text,
                    tool_call_id=str(payload.get("id") or "").strip() or None,
                ),
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
        return {
            "messages": messages,
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
            result["messages"] = list(messages)
            result["frontdoor_stage_state"] = self._complete_active_frontdoor_stage_state(
                state.get("frontdoor_stage_state"),
                completed_stage_summary=output,
            )
            return result
        if output:
            result["frontdoor_stage_state"] = self._complete_active_frontdoor_stage_state(
                state.get("frontdoor_stage_state"),
                completed_stage_summary=output,
            )
        result["messages"] = list(messages)
        return result

    @staticmethod
    def _graph_next_step(state: CeoGraphState) -> str:
        next_step = str(state.get("next_step") or "finalize").strip()
        if next_step not in {"call_model", "review_tool_calls", "execute_tools", "finalize"}:
            return "finalize"
        return next_step


__all__ = ["CeoFrontDoorRuntimeOps"]
