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
from g3ku.runtime.tool_visibility import CEO_FIXED_BUILTIN_TOOL_NAMES
from main.models import normalize_execution_policy_metadata
from main.protocol import now_iso
from main.runtime.chat_backend import build_actual_request_diagnostics
from main.runtime.internal_tools import SubmitNextStageTool
from main.runtime.stage_budget import (
    STAGE_TOOL_NAME,
    STAGE_TOOL_ROUND_BUDGET_MAX,
    STAGE_TOOL_ROUND_BUDGET_MIN,
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
from g3ku.runtime.web_ceo_sessions import frontdoor_stage_archive_task_id, persist_frontdoor_actual_request

from ._ceo_support import CeoFrontDoorSupport
from .canonical_context import (
    combine_canonical_context,
    default_frontdoor_canonical_context,
    merge_turn_stage_state_into_canonical_context,
    normalize_frontdoor_canonical_context,
)
from .prompt_cache_contract import build_frontdoor_prompt_contract
from .state_models import (
    CeoFrontdoorInterrupted,
    CeoPendingInterrupt,
    CeoPersistentState,
    CeoRuntimeContext,
)
from .tool_contract import (
    FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND,
    build_frontdoor_tool_contract,
    normalize_frontdoor_candidate_tool_items,
    upsert_frontdoor_tool_contract_message,
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


@dataclass(slots=True)
class FrontdoorExecutionBundle:
    base_stage_state: dict[str, Any]
    mutable_stage_state: dict[str, Any]
    visible_tools: dict[str, Tool]
    runtime_context: dict[str, Any]
    on_progress: Any


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


class CeoFrontDoorRuntimeOps(CeoFrontDoorSupport):
    @staticmethod
    def _is_frontdoor_tool_contract_record(record: dict[str, Any] | None) -> bool:
        if not isinstance(record, dict):
            return False
        if str(record.get("role") or "").strip().lower() != "user":
            return False
        content = record.get("content")
        payload: dict[str, Any] | None = None
        if isinstance(content, dict):
            payload = dict(content)
        elif isinstance(content, str):
            text = str(content or "").strip()
            if not text:
                return False
            try:
                parsed = json.loads(text)
            except Exception:
                return False
            if isinstance(parsed, dict):
                payload = dict(parsed)
        if not isinstance(payload, dict):
            return False
        return str(payload.get("message_type") or "").strip() == FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND

    @classmethod
    def _split_request_body_and_tool_contract_messages(
        cls,
        request_messages: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        body_messages: list[dict[str, Any]] = []
        contract_messages: list[dict[str, Any]] = []
        for item in list(request_messages or []):
            if not isinstance(item, dict):
                continue
            record = dict(item)
            if cls._is_frontdoor_tool_contract_record(record):
                contract_messages.append(record)
                continue
            body_messages.append(record)
        return body_messages, contract_messages

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
            if isinstance(interrupt_state.get("frontdoor_canonical_context"), dict):
                values["frontdoor_canonical_context"] = dict(interrupt_state.get("frontdoor_canonical_context") or {})
            if isinstance(interrupt_state.get("compression_state"), dict):
                values["compression_state"] = dict(interrupt_state.get("compression_state") or {})
            if isinstance(interrupt_state.get("semantic_context_state"), dict):
                values["semantic_context_state"] = dict(interrupt_state.get("semantic_context_state") or {})
            hydrated_tool_names = interrupt_state.get("hydrated_tool_names")
            if isinstance(hydrated_tool_names, list):
                values["hydrated_tool_names"] = [
                    str(item or "").strip()
                    for item in list(hydrated_tool_names or [])
                    if str(item or "").strip()
                ]
            if isinstance(interrupt_state.get("frontdoor_selection_debug"), dict):
                values["frontdoor_selection_debug"] = dict(interrupt_state.get("frontdoor_selection_debug") or {})
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
        turn_id_getter = getattr(session, "_current_turn_id", None)
        turn_id = ""
        if callable(turn_id_getter):
            try:
                turn_id = str(turn_id_getter() or "").strip()
            except Exception:
                turn_id = ""
        return {
            "on_progress": runtime.context.on_progress,
            "emit_lifecycle": True,
            "actor_role": "ceo",
            "session_key": session.state.session_key,
            "turn_id": turn_id,
            "tool_contract_enforced": True,
            "candidate_tool_names": list(state.get("candidate_tool_names") or []),
            "candidate_skill_ids": list(state.get("candidate_skill_ids") or []),
            "rbac_visible_tool_names": list(state.get("rbac_visible_tool_names") or []),
            "rbac_visible_skill_ids": list(state.get("rbac_visible_skill_ids") or []),
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
            "runtime_session": session,
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
        return self._registered_tools(
            self._frontdoor_runtime_visible_tool_names_for_state(
                state,
                tool_names=list(state.get("tool_names") or []),
            )
        )

    def _frontdoor_has_valid_stage(self, state: CeoGraphState | dict[str, Any] | None) -> bool:
        normalized_state = (
            state
            if isinstance(state, dict) and "frontdoor_stage_state" in state
            else {"frontdoor_stage_state": dict(state or {}) if isinstance(state, dict) else {}}
        )
        snapshot = self._frontdoor_stage_state_snapshot(normalized_state)
        return bool(str(snapshot.get("active_stage_id") or "").strip()) and not bool(snapshot.get("transition_required"))

    def _frontdoor_callable_tool_names_for_state(
        self,
        state: CeoGraphState | dict[str, Any] | None,
        *,
        tool_names: list[str] | None = None,
    ) -> list[str]:
        raw_names = tool_names
        if raw_names is None and isinstance(state, dict):
            raw_names = list(state.get("tool_names") or [])
        normalized = self._normalized_tool_name_state_list(raw_names)
        if isinstance(state, dict) and bool(state.get("cron_internal")):
            return normalized
        if self._frontdoor_has_valid_stage(state):
            return normalized
        return [STAGE_TOOL_NAME]

    def _frontdoor_runtime_visible_tool_names_for_state(
        self,
        state: CeoGraphState | dict[str, Any] | None,
        *,
        tool_names: list[str] | None = None,
    ) -> list[str]:
        raw_names = tool_names
        if raw_names is None and isinstance(state, dict):
            raw_names = list(state.get("tool_names") or [])
        normalized = self._normalized_tool_name_state_list(raw_names)
        if isinstance(state, dict) and bool(state.get("cron_internal")):
            return normalized
        if STAGE_TOOL_NAME not in normalized:
            return [*normalized, STAGE_TOOL_NAME]
        return normalized

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
    def _default_frontdoor_canonical_context() -> dict[str, Any]:
        return default_frontdoor_canonical_context()

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

    @staticmethod
    def _normalized_tool_name_state_list(raw: Any) -> list[str]:
        return CeoFrontDoorRuntimeOps._normalized_hydrated_tool_names(raw)

    @staticmethod
    def _normalized_candidate_tool_items(raw: Any, *, fallback_names: list[str] | None = None) -> list[dict[str, str]]:
        return normalize_frontdoor_candidate_tool_items(raw, fallback_names=fallback_names)

    @staticmethod
    def _tool_context_hydration_payload(raw_result: Any) -> dict[str, Any] | None:
        if isinstance(raw_result, dict):
            return dict(raw_result)
        if isinstance(raw_result, str):
            text = str(raw_result or "").strip()
            if not text or not text.startswith("{"):
                return None
            try:
                parsed = json.loads(text)
            except Exception:
                return None
            return dict(parsed) if isinstance(parsed, dict) else None
        return None

    def _frontdoor_hydrated_tool_limit_value(self) -> int:
        main_service = getattr(self._loop, "main_task_service", None)
        supplier = getattr(main_service, "_hydrated_tool_limit_value", None) if main_service is not None else None
        if callable(supplier):
            try:
                return max(1, int(supplier() or 16))
            except Exception:
                pass
        try:
            value = int(
                getattr(main_service, "_hydrated_tool_limit", getattr(self, "_hydrated_tool_limit", 16)) or 16
            )
        except Exception:
            value = 16
        return max(1, value)

    def _frontdoor_hydrated_tool_lru(
        self,
        *,
        existing_tool_names: Any,
        incoming_tool_names: Any,
        visible_tool_names: list[str] | None = None,
    ) -> list[str]:
        visible_name_set = {
            str(item or "").strip()
            for item in list(visible_tool_names or [])
            if str(item or "").strip()
        }
        existing = self._normalized_hydrated_tool_names(existing_tool_names)
        incoming = self._normalized_hydrated_tool_names(incoming_tool_names)
        if visible_name_set:
            existing = [name for name in existing if name in visible_name_set]
            incoming = [name for name in incoming if name in visible_name_set]
        if not incoming:
            limit = self._frontdoor_hydrated_tool_limit_value()
            return existing[-limit:]
        next_state = [name for name in existing if name not in incoming]
        next_state.extend(incoming)
        limit = self._frontdoor_hydrated_tool_limit_value()
        if len(next_state) > limit:
            next_state = next_state[-limit:]
        return next_state

    @classmethod
    def _runtime_session_frontdoor_state(
        cls,
        state: CeoGraphState | None,
        *,
        preview_pending_tool_round: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
        frontdoor_canonical_context = cls._frontdoor_canonical_context_snapshot(state)
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
        combined_canonical_context = combine_canonical_context(
            frontdoor_canonical_context,
            frontdoor_stage_state,
        )
        return (
            frontdoor_stage_state,
            combined_canonical_context,
            compression_state,
            semantic_context_state,
            hydrated_tool_names,
        )

    @classmethod
    def _frontdoor_canonical_context_snapshot(cls, state: CeoGraphState | None) -> dict[str, Any]:
        if not isinstance(state, dict):
            return cls._default_frontdoor_canonical_context()
        return normalize_frontdoor_canonical_context(
            state.get("frontdoor_canonical_context") or cls._default_frontdoor_canonical_context()
        )

    @classmethod
    def _frontdoor_selection_debug_snapshot(cls, state: CeoGraphState | None) -> dict[str, Any]:
        if not isinstance(state, dict):
            return {}
        raw_value = state.get("frontdoor_selection_debug")
        return dict(raw_value) if isinstance(raw_value, dict) else {}

    @staticmethod
    def _compression_state_has_material_content(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        return bool(
            str(value.get("status") or "").strip()
            or str(value.get("text") or "").strip()
            or str(value.get("source") or "").strip()
            or bool(value.get("needs_recheck"))
        )

    @staticmethod
    def _semantic_context_state_has_material_content(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        if str(value.get("summary_text") or "").strip():
            return True
        if bool(value.get("needs_refresh")):
            return True
        if str(value.get("updated_at") or "").strip():
            return True
        if str(value.get("coverage_history_source") or "").strip():
            return True
        try:
            coverage_message_index = int(value.get("coverage_message_index", -1) or -1)
        except (TypeError, ValueError):
            coverage_message_index = -1
        if coverage_message_index >= 0:
            return True
        try:
            coverage_stage_index = int(value.get("coverage_stage_index", 0) or 0)
        except (TypeError, ValueError):
            coverage_stage_index = 0
        if coverage_stage_index > 0:
            return True
        if str(value.get("failure_cooldown_until") or "").strip():
            return True
        return False

    @classmethod
    def _paused_manual_frontdoor_snapshot(cls, session: Any | None) -> dict[str, Any]:
        snapshot_supplier = getattr(session, "paused_execution_context_snapshot", None)
        if not callable(snapshot_supplier):
            return {}
        try:
            snapshot = snapshot_supplier()
        except Exception:
            return {}
        if not isinstance(snapshot, dict) or not snapshot:
            return {}
        if str(snapshot.get("status") or "").strip().lower() != "paused":
            return {}
        source = str(snapshot.get("source") or "").strip().lower()
        if source in {"approval", "heartbeat", "cron"}:
            return {}
        return dict(snapshot)

    @staticmethod
    def _persisted_session_has_paused_user_turn(persisted_session: Any | None) -> bool:
        for message in reversed(list(getattr(persisted_session, "messages", []) or [])):
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "").strip().lower() != "user":
                continue
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            if metadata.get("history_visible") is False:
                continue
            if str(metadata.get("_transcript_state") or "").strip().lower() == "paused":
                return True
        return False

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
        (
            frontdoor_stage_state,
            frontdoor_canonical_context,
            compression_state,
            semantic_context_state,
            hydrated_tool_names,
        ) = self._runtime_session_frontdoor_state(
            state,
            preview_pending_tool_round=preview_pending_tool_round,
        )
        setattr(target_session, "_frontdoor_stage_state", frontdoor_stage_state)
        setattr(target_session, "_frontdoor_canonical_context", frontdoor_canonical_context)
        setattr(target_session, "_compression_state", compression_state)
        setattr(target_session, "_semantic_context_state", semantic_context_state)
        setattr(target_session, "_frontdoor_hydrated_tool_names", list(hydrated_tool_names))
        setattr(
            target_session,
            "_frontdoor_selection_debug",
            self._frontdoor_selection_debug_snapshot(state),
        )
        if isinstance(state, dict):
            diagnostics = dict(state.get("prompt_cache_diagnostics") or {})
            actual_request_path = str(state.get("frontdoor_actual_request_path") or "").strip()
            if actual_request_path:
                setattr(target_session, "_frontdoor_actual_request_path", actual_request_path)
            actual_request_history = [
                dict(item)
                for item in list(state.get("frontdoor_actual_request_history") or [])
                if isinstance(item, dict)
            ]
            if actual_request_history:
                setattr(target_session, "_frontdoor_actual_request_history", actual_request_history)
            prompt_cache_key_hash = str(
                state.get("frontdoor_prompt_cache_key_hash")
                or diagnostics.get("prompt_cache_key_hash")
                or ""
            ).strip()
            if prompt_cache_key_hash:
                setattr(target_session, "_frontdoor_prompt_cache_key_hash", prompt_cache_key_hash)
            actual_request_hash = str(
                state.get("frontdoor_actual_request_hash")
                or diagnostics.get("actual_request_hash")
                or ""
            ).strip()
            if actual_request_hash:
                setattr(target_session, "_frontdoor_actual_request_hash", actual_request_hash)
            actual_request_message_count = int(
                state.get("frontdoor_actual_request_message_count")
                or diagnostics.get("actual_request_message_count")
                or 0
            )
            if actual_request_message_count:
                setattr(target_session, "_frontdoor_actual_request_message_count", actual_request_message_count)
            actual_tool_schema_hash = str(
                state.get("frontdoor_actual_tool_schema_hash")
                or diagnostics.get("actual_tool_schema_hash")
                or ""
            ).strip()
            if actual_tool_schema_hash:
                setattr(target_session, "_frontdoor_actual_tool_schema_hash", actual_tool_schema_hash)

    def _persist_frontdoor_actual_request(
        self,
        *,
        state: CeoGraphState,
        runtime: Runtime[CeoRuntimeContext],
        request_messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None,
        prompt_cache_key: str,
        prompt_cache_diagnostics: dict[str, Any],
        parallel_tool_calls: bool | None,
        provider_request_meta: dict[str, Any] | None = None,
        provider_request_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session_key = str(state.get("session_key") or getattr(getattr(runtime, "context", None), "session_key", "") or "").strip()
        if not session_key:
            return {}
        target_session = getattr(getattr(runtime, "context", None), "session", None)
        turn_id = ""
        turn_id_getter = getattr(target_session, "_current_turn_id", None) if target_session is not None else None
        if callable(turn_id_getter):
            try:
                turn_id = str(turn_id_getter()).strip()
            except Exception:
                turn_id = ""
        if not turn_id:
            turn_id = str(getattr(target_session, "_active_turn_id", "") or "").strip()
        diagnostics = dict(prompt_cache_diagnostics or {})
        provider_model = str((list(state.get("model_refs") or []) or [""])[0] or "").strip()
        record = persist_frontdoor_actual_request(
            session_key,
            payload={
                "type": "frontdoor_actual_request",
                "session_key": session_key,
                "turn_id": turn_id,
                "created_at": now_iso(),
                "provider_model": provider_model,
                "model_refs": [
                    str(item or "").strip()
                    for item in list(state.get("model_refs") or [])
                    if str(item or "").strip()
                ],
                "parallel_tool_calls": parallel_tool_calls,
                "prompt_cache_key": str(prompt_cache_key or "").strip(),
                "prompt_cache_key_hash": str(diagnostics.get("prompt_cache_key_hash") or "").strip(),
                "actual_request_hash": str(diagnostics.get("actual_request_hash") or "").strip(),
                "actual_request_message_count": int(diagnostics.get("actual_request_message_count") or 0),
                "actual_tool_schema_hash": str(diagnostics.get("actual_tool_schema_hash") or "").strip(),
                "tool_signature_hash": str(diagnostics.get("tool_signature_hash") or "").strip(),
                "stable_prefix_hash": str(diagnostics.get("stable_prefix_hash") or "").strip(),
                "dynamic_appendix_hash": str(diagnostics.get("dynamic_appendix_hash") or "").strip(),
                "messages": [dict(item) for item in list(request_messages or []) if isinstance(item, dict)],
                "request_messages": [dict(item) for item in list(request_messages or []) if isinstance(item, dict)],
                "tool_schemas": [dict(item) for item in list(tool_schemas or []) if isinstance(item, dict)],
                "provider_request_meta": (
                    dict(provider_request_meta or {})
                    if isinstance(provider_request_meta, dict)
                    else {}
                ),
                "provider_request_body": (
                    dict(provider_request_body or {})
                    if isinstance(provider_request_body, dict)
                    else {}
                ),
            },
        )
        if not record:
            return {}
        existing_history = [
            dict(item)
            for item in list(
                state.get("frontdoor_actual_request_history")
                or getattr(target_session, "_frontdoor_actual_request_history", [])
                or []
            )
            if isinstance(item, dict)
        ]
        existing_history.append(dict(record))
        existing_history = existing_history[-32:]
        if target_session is not None:
            setattr(target_session, "_frontdoor_actual_request_path", str(record.get("path") or "").strip())
            setattr(target_session, "_frontdoor_actual_request_history", list(existing_history))
            setattr(target_session, "_frontdoor_prompt_cache_key_hash", str(record.get("prompt_cache_key_hash") or "").strip())
            setattr(target_session, "_frontdoor_actual_request_hash", str(record.get("actual_request_hash") or "").strip())
            setattr(target_session, "_frontdoor_actual_request_message_count", int(record.get("actual_request_message_count") or 0))
            setattr(target_session, "_frontdoor_actual_tool_schema_hash", str(record.get("actual_tool_schema_hash") or "").strip())
        return {
            "frontdoor_actual_request_path": str(record.get("path") or "").strip(),
            "frontdoor_actual_request_history": list(existing_history),
            "frontdoor_prompt_cache_key_hash": str(record.get("prompt_cache_key_hash") or "").strip(),
            "frontdoor_actual_request_hash": str(record.get("actual_request_hash") or "").strip(),
            "frontdoor_actual_request_message_count": int(record.get("actual_request_message_count") or 0),
            "frontdoor_actual_tool_schema_hash": str(record.get("actual_tool_schema_hash") or "").strip(),
        }

    def _frontdoor_tool_state_after_tool_results(
        self,
        *,
        state: dict[str, Any],
        tool_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        tool_names = self._normalized_tool_name_state_list(state.get("tool_names"))
        candidate_tool_names = self._normalized_tool_name_state_list(state.get("candidate_tool_names"))
        candidate_tool_items = self._normalized_candidate_tool_items(
            state.get("candidate_tool_items"),
            fallback_names=candidate_tool_names,
        )
        hydrated_tool_names = self._normalized_tool_name_state_list(state.get("hydrated_tool_names"))
        visible_tool_names = self._normalized_tool_name_state_list(
            state.get("rbac_visible_tool_names")
            or [*tool_names, *candidate_tool_names]
        )
        promotion_targets: list[str] = []
        for tool_result in list(tool_results or []):
            tool_name = str(tool_result.get("tool_name") or "").strip()
            if tool_name not in {"load_tool_context", "load_tool_context_v2"}:
                continue
            raw_payload = self._tool_context_hydration_payload(tool_result.get("raw_result"))
            if not isinstance(raw_payload, dict) or not bool(raw_payload.get("ok")):
                continue
            targets = self._normalized_tool_name_state_list(raw_payload.get("hydration_targets"))
            for name in targets:
                if name in CEO_FIXED_BUILTIN_TOOL_NAMES:
                    continue
                if name not in promotion_targets:
                    promotion_targets.append(name)
        hydrated_tool_names = self._frontdoor_hydrated_tool_lru(
            existing_tool_names=hydrated_tool_names,
            incoming_tool_names=promotion_targets,
            visible_tool_names=visible_tool_names,
        )
        visible_name_set = set(visible_tool_names)
        if visible_name_set:
            tool_names = [name for name in tool_names if name in visible_name_set]
            candidate_tool_names = [name for name in candidate_tool_names if name in visible_name_set]
            candidate_tool_items = [
                dict(item)
                for item in list(candidate_tool_items or [])
                if str(item.get("tool_id") or "").strip() in visible_name_set
            ]
        for name in list(hydrated_tool_names or []):
            if name not in tool_names:
                tool_names.append(name)
        hydrated_set = set(hydrated_tool_names)
        candidate_tool_names = [
            name
            for name in candidate_tool_names
            if name not in hydrated_set
        ]
        candidate_name_set = set(candidate_tool_names)
        candidate_tool_items = [
            dict(item)
            for item in list(candidate_tool_items or [])
            if str(item.get("tool_id") or "").strip() in candidate_name_set
        ]
        return {
            "tool_names": list(tool_names),
            "candidate_tool_names": list(candidate_tool_names),
            "candidate_tool_items": list(candidate_tool_items),
            "hydrated_tool_names": list(hydrated_tool_names),
        }

    def _refresh_frontdoor_dynamic_contract_state(
        self,
        *,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        refreshed = dict(state or {})
        runtime_visible_tool_names = self._frontdoor_runtime_visible_tool_names_for_state(
            refreshed,
            tool_names=list(refreshed.get("tool_names") or []),
        )
        for legacy_field in ("summary_text", "summary_payload", "summary_model_key", "summary_version"):
            refreshed.pop(legacy_field, None)
        if not hasattr(self, "_frontdoor_prompt_contract"):
            return refreshed
        try:
            model_refs = list(refreshed.get("model_refs") or [])
            provider_model = str(model_refs[0] if model_refs else "").strip()
            try:
                tool_schemas = self._selected_tool_schemas(list(runtime_visible_tool_names))
            except Exception:
                tool_schemas = []
            contract = self._frontdoor_prompt_contract(
                state=refreshed,
                provider_model=provider_model,
                tool_schemas=tool_schemas,
                overlay_text=str(refreshed.get("turn_overlay_text") or "").strip(),
                session_key=str(refreshed.get("session_key") or "").strip(),
                overlay_section_count=len(list(refreshed.get("dynamic_appendix_messages") or [])),
            )
        except Exception:
            return refreshed
        refreshed["dynamic_appendix_messages"] = list(contract.dynamic_appendix_messages)
        refreshed["cache_family_revision"] = contract.cache_family_revision
        refreshed["prompt_cache_key"] = contract.prompt_cache_key
        refreshed["prompt_cache_diagnostics"] = dict(contract.diagnostics)
        return refreshed

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
        if normalized_budget < STAGE_TOOL_ROUND_BUDGET_MIN or normalized_budget > STAGE_TOOL_ROUND_BUDGET_MAX:
            raise ValueError(
                f"tool_round_budget must be between "
                f"{STAGE_TOOL_ROUND_BUDGET_MIN} and {STAGE_TOOL_ROUND_BUDGET_MAX}"
            )

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
                "mode": "自主执行",
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

    def _merged_frontdoor_canonical_context(
        self,
        *,
        state: CeoGraphState,
        frontdoor_stage_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        session_key = str(state.get("session_key") or "").strip()

        def _externalize_batch(
            stages: list[dict[str, Any]],
            stage_index_start: int,
            stage_index_end: int,
        ) -> tuple[str, str]:
            if not session_key:
                return "", ""
            return self._externalize_frontdoor_stage_archive(
                session_key=session_key,
                stage_index_start=stage_index_start,
                stage_index_end=stage_index_end,
                stages=stages,
            )

        return merge_turn_stage_state_into_canonical_context(
            self._frontdoor_canonical_context_snapshot(state),
            frontdoor_stage_state or self._default_frontdoor_stage_state(),
            externalize_batch=_externalize_batch if session_key else None,
        )

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
            "arguments": arguments,
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
        execution_bundle = self._frontdoor_execution_bundle(state=state, runtime=runtime)
        visible_tools = execution_bundle.visible_tools
        runtime_context = execution_bundle.runtime_context
        on_progress = execution_bundle.on_progress
        mutable_stage_state = execution_bundle.mutable_stage_state

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

    def _frontdoor_execution_bundle(
        self,
        *,
        state: CeoGraphState,
        runtime: Runtime[CeoRuntimeContext],
    ) -> FrontdoorExecutionBundle:
        registered_tools = self._registered_tools_for_state(state)
        base_stage_state = self._frontdoor_stage_state_snapshot(state)
        mutable_stage_state = copy.deepcopy(base_stage_state)

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
        return FrontdoorExecutionBundle(
            base_stage_state=base_stage_state,
            mutable_stage_state=mutable_stage_state,
            visible_tools=visible_tools,
            runtime_context=runtime_context,
            on_progress=runtime_context.get("on_progress"),
        )

    @staticmethod
    def _frontdoor_mixed_stage_tool_batch_error(tool_call_payloads: list[dict[str, Any]]) -> str:
        has_stage_tool = any(
            str(payload.get("name") or "").strip() == STAGE_TOOL_NAME
            for payload in list(tool_call_payloads or [])
            if isinstance(payload, dict)
        )
        has_non_stage_tool = any(
            str(payload.get("name") or "").strip()
            and str(payload.get("name") or "").strip() != STAGE_TOOL_NAME
            for payload in list(tool_call_payloads or [])
            if isinstance(payload, dict)
        )
        if has_stage_tool and has_non_stage_tool:
            return f"{STAGE_TOOL_NAME} must be called alone before using other tools"
        return ""

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
                    "provider_request_meta": payload.get("provider_request_meta"),
                    "provider_request_body": payload.get("provider_request_body"),
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
                "provider_request_meta": response_metadata.get("provider_request_meta"),
                "provider_request_body": response_metadata.get("provider_request_body"),
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
            "provider_request_meta": _checkpoint_safe_value(response_view.provider_request_meta),
            "provider_request_body": _checkpoint_safe_value(response_view.provider_request_body),
        }

    @staticmethod
    def _tool_call_payloads_from_calls(calls: list[Any]) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for call in list(calls or []):
            raw_arguments: Any = {}
            if isinstance(call, dict):
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(function.get("name") or call.get("name") or "").strip()
                call_id = str(call.get("id") or "")
                if "arguments" in function:
                    raw_arguments = function.get("arguments")
                elif "args" in function:
                    raw_arguments = function.get("args")
                elif "arguments" in call:
                    raw_arguments = call.get("arguments")
                elif "args" in call:
                    raw_arguments = call.get("args")
            else:
                function = getattr(call, "function", None)
                name = str(getattr(function, "name", "") or getattr(call, "name", "") or "").strip()
                call_id = str(getattr(call, "id", "") or "")
                if hasattr(function, "arguments"):
                    raw_arguments = getattr(function, "arguments")
                elif hasattr(function, "args"):
                    raw_arguments = getattr(function, "args")
                elif hasattr(call, "arguments"):
                    raw_arguments = getattr(call, "arguments")
                elif hasattr(call, "args"):
                    raw_arguments = getattr(call, "args")
            if isinstance(raw_arguments, str):
                try:
                    parsed_arguments = json.loads(raw_arguments)
                except Exception:
                    parsed_arguments = None
                arguments = dict(parsed_arguments) if isinstance(parsed_arguments, dict) else {}
            elif isinstance(raw_arguments, dict):
                arguments = dict(raw_arguments)
            else:
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
                "frontdoor_stage_state": self._default_frontdoor_stage_state(),
                "frontdoor_canonical_context": self._frontdoor_canonical_context_snapshot(state),
                "compression_state": dict(state.get("compression_state") or self._default_compression_state()),
                "frontdoor_selection_debug": self._frontdoor_selection_debug_snapshot(state),
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
        paused_manual_snapshot = (
            self._paused_manual_frontdoor_snapshot(session)
            if not heartbeat_internal and not cron_internal
            else {}
        )
        if paused_manual_snapshot and not self._persisted_session_has_paused_user_turn(runtime_session):
            paused_manual_snapshot = {}
        current_frontdoor_stage_state = self._frontdoor_stage_state_snapshot(state)
        if not list(current_frontdoor_stage_state.get("stages") or []) and paused_manual_snapshot:
            paused_stage_source = (
                paused_manual_snapshot.get("frontdoor_stage_state")
                or paused_manual_snapshot.get("visible_canonical_context")
                or paused_manual_snapshot.get("canonical_context")
                or {}
            )
            current_frontdoor_stage_state = self._frontdoor_stage_state_snapshot(
                {"frontdoor_stage_state": paused_stage_source}
            )
        current_frontdoor_canonical_context = self._frontdoor_canonical_context_snapshot(state)
        if not list(current_frontdoor_canonical_context.get("stages") or []) and paused_manual_snapshot:
            paused_canonical_source = paused_manual_snapshot.get("frontdoor_canonical_context") or {}
            current_frontdoor_canonical_context = normalize_frontdoor_canonical_context(paused_canonical_source)
            if not list(current_frontdoor_canonical_context.get("stages") or []):
                current_frontdoor_canonical_context = normalize_frontdoor_canonical_context(
                    paused_manual_snapshot.get("canonical_context")
                    or paused_manual_snapshot.get("visible_canonical_context")
                    or {}
                )
        current_compression_state = (
            dict(state.get("compression_state") or self._default_compression_state())
            if isinstance(state, dict)
            else self._default_compression_state()
        )
        if not self._compression_state_has_material_content(current_compression_state):
            paused_compression_state = (
                dict(paused_manual_snapshot.get("compression") or {})
                if paused_manual_snapshot
                else {}
            )
            if self._compression_state_has_material_content(paused_compression_state):
                current_compression_state = paused_compression_state
        current_semantic_context_state = (
            dict(state.get("semantic_context_state") or {})
            if isinstance(state, dict)
            else {}
        )
        if not self._semantic_context_state_has_material_content(current_semantic_context_state):
            paused_semantic_context_state = (
                dict(paused_manual_snapshot.get("semantic_context_state") or {})
                if paused_manual_snapshot
                else {}
            )
            session_semantic_context_state = dict(getattr(session, "_semantic_context_state", None) or {})
            if self._semantic_context_state_has_material_content(paused_semantic_context_state):
                current_semantic_context_state = paused_semantic_context_state
            elif self._semantic_context_state_has_material_content(session_semantic_context_state):
                current_semantic_context_state = session_semantic_context_state
        current_semantic_context_state = {
            **self._default_semantic_context_state(),
            **dict(current_semantic_context_state or {}),
        }
        seeded_hydrated_tool_names = (
            list(getattr(session, "_frontdoor_hydrated_tool_names", []) or [])
            or list(state.get("hydrated_tool_names") or [])
        )
        if not seeded_hydrated_tool_names and paused_manual_snapshot:
            seeded_hydrated_tool_names = [
                str(item or "").strip()
                for item in list(paused_manual_snapshot.get("hydrated_tool_names") or [])
                if str(item or "").strip()
            ]
        hydrated_tool_names = self._frontdoor_hydrated_tool_lru(
            existing_tool_names=seeded_hydrated_tool_names,
            incoming_tool_names=[],
            visible_tool_names=list(exposure.get("tool_names") or []),
        )
        assembly = await self._builder.build_for_ceo(
            session=session,
            query_text=builder_query_text,
            exposure=exposure,
            persisted_session=runtime_session,
            checkpoint_messages=list(state.get("messages") or []),
            user_content=self._model_content(user_content),
            user_metadata=metadata,
            frontdoor_stage_state=current_frontdoor_stage_state,
            frontdoor_canonical_context=current_frontdoor_canonical_context,
            semantic_context_state=dict(current_semantic_context_state or {}),
            hydrated_tool_names=list(hydrated_tool_names),
        )
        selected_skill_ids = [
            str(item.get("skill_id") or "").strip()
            for item in list(getattr(assembly, "trace", {}).get("selected_skills") or [])
            if isinstance(item, dict) and str(item.get("skill_id") or "").strip()
        ]
        candidate_tool_names = list(getattr(assembly, "candidate_tool_names", []) or [])
        candidate_tool_items = self._normalized_candidate_tool_items(
            getattr(assembly, "candidate_tool_items", None),
            fallback_names=candidate_tool_names,
        )
        rbac_visible_tool_names = [
            str(item or "").strip()
            for item in list(getattr(assembly, "trace", {}).get("capability_snapshot", {}).get("visible_tool_ids") or [])
            if str(item or "").strip()
        ]
        rbac_visible_skill_ids = [
            str(item or "").strip()
            for item in list(getattr(assembly, "trace", {}).get("capability_snapshot", {}).get("visible_skill_ids") or [])
            if str(item or "").strip()
        ]
        frontdoor_selection_debug = {
            "query_text": str(builder_query_text or "").strip(),
            "raw_turn_query_text": str(query_text or "").strip(),
            "semantic_frontdoor": dict(getattr(assembly, "trace", {}).get("semantic_frontdoor") or {}),
            "tool_selection": dict(getattr(assembly, "trace", {}).get("tool_selection") or {}),
            "selected_skills": list(getattr(assembly, "trace", {}).get("selected_skills") or []),
            "capability_snapshot": dict(getattr(assembly, "trace", {}).get("capability_snapshot") or {}),
            "callable_tool_names": [],
            "candidate_tool_names": list(candidate_tool_names),
            "hydrated_tool_names": list(hydrated_tool_names),
        }
        tool_names = list(
            getattr(assembly, "tool_names", None)
            or getattr(assembly, "callable_tool_names", None)
            or []
        )
        if cron_internal:
            tool_names = ["cron"]
        callable_tool_names = self._frontdoor_callable_tool_names_for_state(
            {
                "frontdoor_stage_state": current_frontdoor_stage_state,
                "cron_internal": cron_internal,
                "heartbeat_internal": heartbeat_internal,
            },
            tool_names=tool_names,
        )
        frontdoor_selection_debug["callable_tool_names"] = list(callable_tool_names)
        cron_system_message = self._cron_internal_system_message(metadata)
        messages: list[dict[str, Any]] = list(assembly.model_messages or [])
        if cron_system_message is not None:
            insert_at = 1 if messages and str(messages[0].get("role") or "").strip().lower() == "system" else 0
            messages = [*messages[:insert_at], cron_system_message, *messages[insert_at:]]
        if not messages or str(messages[-1].get("role") or "").strip().lower() != "user":
            messages.append({"role": "user", "content": self._model_content(user_content)})

        model_refs = self._resolve_ceo_model_refs()
        provider_model = str(model_refs[0] if model_refs else "").strip()
        runtime_visible_tool_names = self._frontdoor_runtime_visible_tool_names_for_state(
            {
                "frontdoor_stage_state": current_frontdoor_stage_state,
                "cron_internal": cron_internal,
                "heartbeat_internal": heartbeat_internal,
            },
            tool_names=tool_names,
        )
        tool_schemas = self._selected_tool_schemas(runtime_visible_tool_names)
        stable_messages = self._prompt_message_records(getattr(assembly, "stable_messages", None)) or list(messages)
        dynamic_appendix_messages = self._prompt_message_records(
            getattr(assembly, "dynamic_appendix_messages", None)
        )
        cache_family_revision = str(getattr(assembly, "cache_family_revision", "") or "").strip()
        turn_overlay_text = str(getattr(assembly, "turn_overlay_text", "") or "").strip()
        dynamic_appendix_messages = upsert_frontdoor_tool_contract_message(
            dynamic_appendix_messages,
            build_frontdoor_tool_contract(
                callable_tool_names=list(callable_tool_names),
                candidate_tool_names=list(candidate_tool_names),
                candidate_tool_items=list(candidate_tool_items),
                hydrated_tool_names=list(hydrated_tool_names),
                frontdoor_stage_state=dict(current_frontdoor_stage_state or {}),
                visible_skill_ids=list(selected_skill_ids),
                candidate_skill_ids=list(selected_skill_ids),
                rbac_visible_tool_names=list(rbac_visible_tool_names),
                rbac_visible_skill_ids=list(rbac_visible_skill_ids),
                contract_revision=cache_family_revision,
                exec_runtime_policy=(
                    self._loop.main_task_service._current_exec_runtime_policy_payload()
                    if callable(getattr(getattr(self._loop, "main_task_service", None), "_current_exec_runtime_policy_payload", None))
                    else None
                ),
            ),
        )
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
                overlay_text=turn_overlay_text,
                overlay_section_count=int(getattr(assembly, "trace", {}).get("turn_overlay_section_count", 0) or 0),
            )
            messages = list(contract.request_messages)
            stable_messages = list(contract.stable_messages)
            dynamic_appendix_messages = list(contract.dynamic_appendix_messages)
            cache_family_revision = str(contract.cache_family_revision or "").strip()
            prompt_cache_key = contract.prompt_cache_key
            prompt_cache_diagnostics = dict(contract.diagnostics)
        else:
            live_request_messages = self._prompt_message_records(messages)
            contract = build_frontdoor_prompt_contract(
                scope=prompt_scope,
                provider_model=provider_model,
                stable_messages=stable_messages,
                dynamic_appendix_messages=dynamic_appendix_messages,
                live_request_messages=live_request_messages,
                tool_schemas=tool_schemas,
                cache_family_revision=cache_family_revision,
                session_key=str(getattr(session.state, "session_key", "") or ""),
                overlay_text=turn_overlay_text,
                overlay_section_count=int(getattr(assembly, "trace", {}).get("turn_overlay_section_count", 0) or 0),
            )
            messages = list(contract.request_messages)
            stable_messages = list(contract.stable_messages)
            dynamic_appendix_messages = list(contract.dynamic_appendix_messages)
            cache_family_revision = str(contract.cache_family_revision or "").strip()
            prompt_cache_key = contract.prompt_cache_key
            prompt_cache_diagnostics = dict(contract.diagnostics)
        persisted_messages = list(stable_messages)
        persisted_dynamic_appendix_messages = list(dynamic_appendix_messages)
        if prompt_scope == "ceo_frontdoor":
            request_body_messages, tool_contract_messages = self._split_request_body_and_tool_contract_messages(messages)
            if request_body_messages:
                persisted_messages = list(request_body_messages)
            if tool_contract_messages:
                persisted_dynamic_appendix_messages = list(tool_contract_messages)
            else:
                persisted_dynamic_appendix_messages = []
        if cron_system_message is not None:
            insert_at = 1 if persisted_messages and str(persisted_messages[0].get("role") or "").strip().lower() == "system" else 0
            persisted_messages = [
                *persisted_messages[:insert_at],
                cron_system_message,
                *persisted_messages[insert_at:],
            ]
        parallel_enabled, max_parallel_tool_calls = self._parallel_tool_settings()
        return {
            "session_key": str(getattr(session.state, "session_key", "") or ""),
            "user_input": user_input,
            "approval_request": None,
            "approval_status": "",
            "query_text": query_text,
            "messages": persisted_messages,
            "frontdoor_stage_state": current_frontdoor_stage_state,
            "frontdoor_canonical_context": current_frontdoor_canonical_context,
            "compression_state": dict(
                getattr(assembly, "trace", {}).get("compression_state_payload")
                or current_compression_state
                or self._default_compression_state()
            ),
            "semantic_context_state": dict(
                getattr(assembly, "trace", {}).get("semantic_context_state")
                or current_semantic_context_state
                or {}
            ),
            "turn_overlay_text": turn_overlay_text or None,
            "frontdoor_selection_debug": frontdoor_selection_debug,
            "tool_names": list(tool_names),
            "candidate_tool_names": list(candidate_tool_names),
            "candidate_tool_items": list(candidate_tool_items),
            "hydrated_tool_names": list(hydrated_tool_names),
            "visible_skill_ids": list(selected_skill_ids),
            "candidate_skill_ids": list(selected_skill_ids),
            "rbac_visible_tool_names": list(rbac_visible_tool_names),
            "rbac_visible_skill_ids": list(rbac_visible_skill_ids),
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
            "dynamic_appendix_messages": persisted_dynamic_appendix_messages,
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
        request_messages = list(state.get("messages") or [])
        prompt_cache_key = str(state.get("prompt_cache_key") or "")
        prompt_cache_diagnostics = dict(state.get("prompt_cache_diagnostics") or {})
        actual_tool_schemas: list[dict[str, Any]] = []
        if hasattr(self, "_frontdoor_prompt_contract"):
            try:
                runtime_visible_tool_names = self._frontdoor_runtime_visible_tool_names_for_state(
                    state,
                    tool_names=list(state.get("tool_names") or []),
                )
                try:
                    tool_schemas = self._selected_tool_schemas(list(runtime_visible_tool_names))
                except Exception:
                    tool_schemas = []
                actual_tool_schemas = list(tool_schemas or [])
                request_contract = self._frontdoor_prompt_contract(
                    state=dict(state or {}),
                    provider_model=str((list(state.get("model_refs") or []) or [""])[0] or "").strip(),
                    tool_schemas=tool_schemas,
                    overlay_text=str(state.get("turn_overlay_text") or "").strip(),
                    session_key=str(state.get("session_key") or "").strip(),
                    overlay_section_count=len(list(state.get("dynamic_appendix_messages") or [])),
                )
                request_messages = list(request_contract.request_messages)
                prompt_cache_key = str(request_contract.prompt_cache_key or prompt_cache_key)
                prompt_cache_diagnostics = dict(request_contract.diagnostics or {})
            except Exception:
                if hasattr(self, "_state_message_records"):
                    request_messages = list(getattr(self, "_state_message_records")(request_messages))
        elif hasattr(self, "_state_message_records"):
            request_messages = list(getattr(self, "_state_message_records")(request_messages))
        request_messages = self._apply_turn_overlay(
            request_messages,
            overlay_text=str(state.get("repair_overlay_text") or "").strip(),
        )
        prompt_cache_diagnostics = {
            **prompt_cache_diagnostics,
            **build_actual_request_diagnostics(
                request_messages=request_messages,
                tool_schemas=actual_tool_schemas,
            ),
        }
        provider_retry_count = 0
        empty_response_retry_count = 0
        while True:
            try:
                message = await self._call_model_with_tools(
                    messages=request_messages,
                    langchain_tools=langchain_tools,
                    model_refs=list(state.get("model_refs") or []),
                    parallel_tool_calls=(bool(state.get("parallel_enabled")) if langchain_tools else None),
                    prompt_cache_key=prompt_cache_key,
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
        response_view = self._model_response_view(message)
        actual_request_trace = self._persist_frontdoor_actual_request(
            state=state,
            runtime=runtime,
            request_messages=request_messages,
            tool_schemas=actual_tool_schemas,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_diagnostics=prompt_cache_diagnostics,
            parallel_tool_calls=(bool(state.get("parallel_enabled")) if langchain_tools else None),
            provider_request_meta=(
                dict(response_view.provider_request_meta or {})
                if isinstance(response_view.provider_request_meta, dict)
                else {}
            ),
            provider_request_body=(
                dict(response_view.provider_request_body or {})
                if isinstance(response_view.provider_request_body, dict)
                else {}
            ),
        )
        message_state_update = (
            self._replace_messages_update(list(request_messages))
            if callable(getattr(self, "_replace_messages_update", None))
            else {"messages": list(request_messages)}
        )
        return {
            "iteration": iteration,
            "repair_overlay_text": None,
            **message_state_update,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_diagnostics": prompt_cache_diagnostics,
            **actual_request_trace,
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
        stage_protocol_message = ""
        if not bool(state.get("heartbeat_internal")) and not bool(state.get("cron_internal")):
            stage_protocol_message = build_ceo_stage_result_block_message(stage_gate)

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
            if stage_protocol_message and not str(state.get("repair_overlay_text") or "").strip():
                return {
                    "repair_overlay_text": (
                        stage_protocol_message
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

        if stage_protocol_message and not str(state.get("repair_overlay_text") or "").strip():
            return {
                "repair_overlay_text": (
                    stage_protocol_message
                    or build_ceo_stage_overlay(stage_gate)
                ),
                "final_output": "",
                "next_step": "call_model",
            }

        return {
            "final_output": self._empty_response_explanation(used_tools=used_tools),
            "route_kind": self._route_kind_for_turn(used_tools=used_tools, default=current_route_kind),
            "next_step": "finalize",
        }

    def _graph_review_tool_calls(
        self,
        state: CeoGraphState,
        *,
        runtime: Runtime[CeoRuntimeContext] | None = None,
    ) -> dict[str, Any]:
        approval_request = dict(state.get("approval_request") or {})
        if not approval_request:
            return {"next_step": "execute_tools"}

        preview_state = dict(state or {})
        (
            preview_frontdoor_stage_state,
            preview_frontdoor_canonical_context,
            preview_compression_state,
            preview_semantic_context_state,
            _preview_hydrated_tool_names,
        ) = self._runtime_session_frontdoor_state(
            preview_state,
            preview_pending_tool_round=True,
        )
        interrupt_payload = {
            **approval_request,
            "frontdoor_stage_state": preview_frontdoor_stage_state,
            "frontdoor_canonical_context": preview_frontdoor_canonical_context,
            "compression_state": preview_compression_state,
            "semantic_context_state": preview_semantic_context_state,
            "hydrated_tool_names": [
                str(item or "").strip()
                for item in list(state.get("hydrated_tool_names") or [])
                if str(item or "").strip()
            ],
            "tool_call_payloads": [
                dict(item)
                for item in list(state.get("tool_call_payloads") or [])
                if isinstance(item, dict)
            ],
            "frontdoor_selection_debug": self._frontdoor_selection_debug_snapshot(state),
        }
        _ = runtime
        decision = interrupt(interrupt_payload)
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

        execution_bundle = self._frontdoor_execution_bundle(state=state, runtime=runtime)
        runtime_context = execution_bundle.runtime_context
        on_progress = execution_bundle.on_progress
        analysis_text = str(state.get("analysis_text") or "").strip()
        if analysis_text:
            await self._emit_progress(
                on_progress,
                analysis_text,
                event_kind="analysis",
            )

        visible_tools = execution_bundle.visible_tools
        mutable_stage_state = execution_bundle.mutable_stage_state
        base_stage_state = execution_bundle.base_stage_state
        semaphore = asyncio.Semaphore(
            self._parallel_slot_count(
                state.get("max_parallel_tool_calls"),
                len(tool_call_payloads),
                enabled=bool(state.get("parallel_enabled")),
            )
        )

        async def _error_result(payload: dict[str, Any], error_text: str) -> dict[str, Any]:
            tool_name = str(payload.get("name") or "")
            normalized_error_text = str(error_text or "").strip()
            if not normalized_error_text.lower().startswith("error:"):
                normalized_error_text = f"Error: {normalized_error_text}"
            await self._emit_progress(
                on_progress,
                normalized_error_text,
                event_kind="tool_error",
                event_data=self._tool_result_progress_event_data(
                    tool_name=tool_name,
                    result_text=normalized_error_text,
                    tool_call_id=str(payload.get("id") or "").strip() or None,
                ),
            )
            return {
                "tool_name": tool_name,
                "status": "error",
                "raw_result": None,
                "result_text": normalized_error_text,
                "tool_message": self._tool_result_message(
                    tool_call_id=str(payload.get("id") or ""),
                    tool_name=tool_name or "tool",
                    content=normalized_error_text,
                    started_at="",
                    finished_at="",
                    elapsed_seconds=None,
                ),
                "started_at": "",
                "finished_at": "",
                "elapsed_seconds": None,
            }

        async def _run_single(payload: dict[str, Any]) -> dict[str, Any]:
            tool_name = str(payload.get("name") or "")
            gate_error = self._frontdoor_stage_gate_error(tool_name=tool_name, stage_state=mutable_stage_state)
            if gate_error:
                return await _error_result(payload, gate_error)
            tool = visible_tools.get(tool_name)
            if tool is None:
                return await _error_result(payload, f"tool not available: {tool_name}")
            async with semaphore:
                raw_result, result_text, status, started_at, finished_at, elapsed_seconds = await self._execute_tool_call_with_raw_result(
                    tool=tool,
                    tool_name=tool_name,
                    arguments=_normalize_frontdoor_tool_arguments(tool_name, dict(payload.get("arguments") or {})),
                    runtime_context=runtime_context,
                    on_progress=on_progress,
                    tool_call_id=str(payload.get("id") or "").strip() or None,
                )
            result_payload = {
                "raw_result": raw_result,
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
                "raw_result": result_payload.get("raw_result"),
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

        mixed_batch_error = self._frontdoor_mixed_stage_tool_batch_error(tool_call_payloads)
        if mixed_batch_error:
            tool_results = [await _error_result(payload, mixed_batch_error) for payload in tool_call_payloads]
        else:
            tool_results = await asyncio.gather(*[_run_single(payload) for payload in tool_call_payloads])
        updated_tool_contract_state = self._frontdoor_tool_state_after_tool_results(
            state=dict(state or {}),
            tool_results=tool_results,
        )
        frontdoor_stage_state = self._frontdoor_stage_state_after_tool_cycle(
            {
                **dict(state or {}),
                "frontdoor_stage_state": base_stage_state,
            },
            tool_call_payloads=tool_call_payloads,
            tool_results=tool_results,
        )
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
        if hasattr(self, "_state_message_records"):
            messages = list(getattr(self, "_state_message_records")(messages))
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
        result = {
            "messages": messages,
            "used_tools": used_tools,
            "route_kind": route_kind,
            "analysis_text": "",
            "tool_call_payloads": [],
            "verified_task_ids": [],
            "synthetic_tool_calls_used": False,
            "frontdoor_stage_state": frontdoor_stage_state,
            "next_step": "call_model",
        }
        result.update(updated_tool_contract_state)
        result["candidate_skill_ids"] = [
            str(item or "").strip()
            for item in list(state.get("candidate_skill_ids") or [])
            if str(item or "").strip()
        ]
        result["visible_skill_ids"] = [
            str(item or "").strip()
            for item in list(state.get("visible_skill_ids") or [])
            if str(item or "").strip()
        ]
        result["rbac_visible_tool_names"] = [
            str(item or "").strip()
            for item in list(state.get("rbac_visible_tool_names") or [])
            if str(item or "").strip()
        ]
        result["rbac_visible_skill_ids"] = [
            str(item or "").strip()
            for item in list(state.get("rbac_visible_skill_ids") or [])
            if str(item or "").strip()
        ]
        result.update(
            self._refresh_frontdoor_dynamic_contract_state(
                state={
                    **dict(state or {}),
                    **result,
                    "messages": messages,
                }
            )
        )
        return result

    async def _graph_finalize_turn(self, state: CeoGraphState) -> dict[str, Any]:
        output = str(state.get("final_output") or "").strip()
        if not output and not bool(state.get("heartbeat_internal")):
            output = self._empty_reply_fallback(str(state.get("query_text") or ""))
        route_kind = str(state.get("route_kind") or "direct_reply")
        result = {
            "final_output": output,
            "route_kind": route_kind,
        }
        messages = list(state.get("messages") or [])
        if hasattr(self, "_state_message_records"):
            messages = list(getattr(self, "_state_message_records")(messages))
        else:
            messages = [dict(message) for message in messages if isinstance(message, dict)]
        request_body_messages, _tool_contract_messages = self._split_request_body_and_tool_contract_messages(messages)
        if request_body_messages:
            messages = list(request_body_messages)
        finalized_stage_state = self._frontdoor_stage_state_snapshot(state)
        if output and route_kind == "direct_reply":
            messages.append({"role": "assistant", "content": output})
            result["messages"] = list(messages)
            finalized_stage_state = self._complete_active_frontdoor_stage_state(
                state.get("frontdoor_stage_state"),
                completed_stage_summary=output,
            )
            result["frontdoor_stage_state"] = finalized_stage_state
            result["frontdoor_canonical_context"] = self._merged_frontdoor_canonical_context(
                state=state,
                frontdoor_stage_state=finalized_stage_state,
            )
            return result
        if output:
            finalized_stage_state = self._complete_active_frontdoor_stage_state(
                state.get("frontdoor_stage_state"),
                completed_stage_summary=output,
            )
        result["frontdoor_stage_state"] = finalized_stage_state
        result["frontdoor_canonical_context"] = self._merged_frontdoor_canonical_context(
            state=state,
            frontdoor_stage_state=finalized_stage_state,
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
