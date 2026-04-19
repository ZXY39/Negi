from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from langchain_core.messages import AIMessage, convert_to_messages
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from g3ku.agent.tools.base import Tool
from g3ku.config.live_runtime import get_runtime_config
from g3ku.core.messages import UserInputMessage
from g3ku.json_schema_utils import (
    attach_raw_parameters_schema,
    build_args_schema_model,
    normalize_runtime_tool_arguments_dict,
    sanitize_provider_parameters_schema,
)
from g3ku.providers.base import normalize_usage_payload
from g3ku.providers.base_chat_model_adapter import G3kuChatModelAdapter
from g3ku.providers.fallback import PUBLIC_PROVIDER_FAILURE_MESSAGE
from g3ku.providers.openai_codex_provider import (
    _convert_messages as _preview_responses_messages,
    _convert_tools as _preview_responses_tools,
    _prompt_cache_key as _preview_prompt_cache_key,
    _strip_model_prefix as _preview_strip_model_prefix,
)
from g3ku.runtime.context.summarizer import estimate_tokens
from g3ku.runtime.config_refresh import refresh_loop_runtime_config
from g3ku.runtime.project_environment import current_project_environment
from g3ku.runtime.message_token_estimation import estimate_message_tokens
from g3ku.runtime.tool_visibility import CEO_FIXED_BUILTIN_TOOL_NAMES
from g3ku.runtime.frontdoor.token_preflight_compaction import (
    FRONTDOOR_COMPACTED_HISTORY_MAX_TOKENS,
    FrontdoorTokenPreflightResult,
    compact_frontdoor_history_zone,
)
from main.models import normalize_execution_policy_metadata
from main.protocol import now_iso
from main.runtime.chat_backend import (
    build_actual_request_diagnostics,
    build_prompt_cache_diagnostics,
    resolve_send_model_context_window_info,
)
from main.runtime.send_token_preflight import (
    build_runtime_hybrid_send_token_estimate,
    build_runtime_observed_input_truth,
    build_runtime_send_token_preflight_snapshot,
    compute_runtime_send_token_preflight_thresholds,
    should_trigger_runtime_token_compression,
)
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
    default_frontdoor_canonical_context,
    merge_turn_stage_state_into_canonical_context,
    normalize_frontdoor_canonical_context,
)
from .message_builder import CeoMessageBuilder
from .prompt_cache_contract import build_frontdoor_prompt_contract
from .state_models import (
    CeoFrontdoorInterrupted,
    CeoPendingInterrupt,
    CeoPersistentState,
    CeoRuntimeContext,
)
from .tool_contract import (
    build_frontdoor_tool_contract,
    is_frontdoor_tool_contract_message,
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


class FrontdoorCompressionRuntimeError(RuntimeError):
    def __init__(self, *, code: str, message: str, recoverable: bool = True) -> None:
        super().__init__(str(message or "").strip())
        self.code = str(code or "").strip() or "runtime_error"
        self.message = str(message or "").strip()
        self.recoverable = bool(recoverable)


@dataclass(frozen=True, slots=True)
class _FrontdoorTokenPreflightPolicy:
    max_context_tokens: int
    trigger_ratio: float
    trigger_tokens: int


def _build_frontdoor_token_preflight_policy(
    *,
    max_context_tokens: int,
    trigger_ratio: float,
) -> _FrontdoorTokenPreflightPolicy:
    normalized_max = max(0, int(max_context_tokens or 0))
    normalized_ratio = max(0.0, float(trigger_ratio or 0.0))
    return _FrontdoorTokenPreflightPolicy(
        max_context_tokens=normalized_max,
        trigger_ratio=normalized_ratio,
        trigger_tokens=int(normalized_max * normalized_ratio),
    )


def _estimate_frontdoor_provider_request_tokens(
    *,
    provider_request_body: dict[str, Any] | None,
    request_messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
) -> int:
    payload = dict(provider_request_body or {})
    if not payload:
        payload = {
            "input": list(request_messages),
            "tools": list(tool_schemas or []),
        }
    return estimate_tokens(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def _should_run_frontdoor_token_preflight(
    *,
    final_request_tokens: int,
    policy: _FrontdoorTokenPreflightPolicy,
) -> bool:
    return int(final_request_tokens or 0) >= int(policy.trigger_tokens)


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


def _provider_visible_tool_contract(tool: Tool) -> tuple[str, dict[str, Any] | None]:
    _model_description, model_parameters = _model_visible_tool_contract(tool)
    compatible_parameters = _ceo_model_compatible_parameters_schema(tool.name, model_parameters)
    stripped_parameters = sanitize_provider_parameters_schema(compatible_parameters)
    return "", stripped_parameters if isinstance(stripped_parameters, dict) else compatible_parameters


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

    model_description, compatible_model_parameters = _provider_visible_tool_contract(tool)
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
    _ALLOWED_FRONTDOOR_SHRINK_REASONS = frozenset({"", "token_compression", "stage_compaction"})
    _TOKEN_COMPRESSION_TRIGGER_RATIO = 0.80
    _TOKEN_COMPRESSION_ESTIMATE_SAFETY_RATIO = 0.95

    @staticmethod
    def _is_frontdoor_tool_contract_record(record: dict[str, Any] | None) -> bool:
        return is_frontdoor_tool_contract_message(dict(record or {}))

    @staticmethod
    def _is_frontdoor_memory_snapshot_record(record: dict[str, Any] | None) -> bool:
        if not isinstance(record, dict):
            return False
        if str(record.get("role") or "").strip().lower() != "assistant":
            return False
        return str(record.get("content") or "").strip().startswith("## 长期记忆\n")

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
            if cls._is_frontdoor_memory_snapshot_record(record):
                continue
            if cls._is_frontdoor_tool_contract_record(record):
                contract_messages.append(record)
                continue
            body_messages.append(record)
        return body_messages, contract_messages

    @classmethod
    def _request_body_messages_without_tool_contracts(
        cls,
        request_messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        body_messages, _contract_messages = cls._split_request_body_and_tool_contract_messages(request_messages)
        return [dict(item) for item in list(body_messages or []) if isinstance(item, dict)]

    @classmethod
    def _rewrite_frontdoor_request_messages_with_compacted_history(
        cls,
        *,
        request_messages: list[dict[str, Any]],
        compacted_history: Any,
    ) -> list[dict[str, Any]]:
        body_messages, contract_messages = cls._split_request_body_and_tool_contract_messages(request_messages)
        if not body_messages:
            return [dict(item) for item in list(request_messages or []) if isinstance(item, dict)]

        normalized_body = [dict(item) for item in body_messages if isinstance(item, dict)]
        system_prefix: list[dict[str, Any]] = []
        if normalized_body and str(normalized_body[0].get("role") or "").strip().lower() == "system":
            system_prefix = [dict(normalized_body[0])]
            normalized_body = normalized_body[1:]

        recent_tail_count = min(len(normalized_body), 4)
        if recent_tail_count <= 0 or len(normalized_body) <= recent_tail_count:
            return [*system_prefix, *normalized_body, *contract_messages]

        recent_tail = [dict(item) for item in normalized_body[-recent_tail_count:]]
        return [
            *system_prefix,
            dict(getattr(compacted_history, "compacted_block", {}) or {}),
            *recent_tail,
            *contract_messages,
        ]

    def _run_frontdoor_token_preflight_compaction(
        self,
        *,
        state: CeoGraphState,
        request_messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        provider_request_body: dict[str, Any] | None,
    ) -> FrontdoorTokenPreflightResult:
        assembly_cfg = getattr(getattr(self._loop, "_memory_runtime_settings", None), "assembly", None)
        policy = _build_frontdoor_token_preflight_policy(
            max_context_tokens=int(
                getattr(assembly_cfg, "frontdoor_compaction_max_context_tokens", 200000) or 200000
            ),
            trigger_ratio=float(
                getattr(assembly_cfg, "frontdoor_compaction_trigger_ratio", 0.10) or 0.10
            ),
        )
        final_request_tokens = _estimate_frontdoor_provider_request_tokens(
            provider_request_body=provider_request_body,
            request_messages=request_messages,
            tool_schemas=tool_schemas,
        )
        diagnostics: dict[str, Any] = {
            "applied": False,
            "final_request_tokens": final_request_tokens,
            "trigger_tokens": int(policy.trigger_tokens),
            "max_context_tokens": int(policy.max_context_tokens),
        }
        if not _should_run_frontdoor_token_preflight(
            final_request_tokens=final_request_tokens,
            policy=policy,
        ):
            return FrontdoorTokenPreflightResult(
                request_messages=list(request_messages),
                final_request_tokens=final_request_tokens,
                history_shrink_reason="",
                diagnostics=diagnostics,
            )

        compacted_history = compact_frontdoor_history_zone(
            raw_history_messages=self._request_body_messages_without_tool_contracts(request_messages),
            frontdoor_stage_state=dict(state.get("frontdoor_stage_state") or {}),
            max_compacted_tokens=FRONTDOOR_COMPACTED_HISTORY_MAX_TOKENS,
        )
        rewritten_messages = self._rewrite_frontdoor_request_messages_with_compacted_history(
            request_messages=request_messages,
            compacted_history=compacted_history,
        )
        rewritten_tokens = _estimate_frontdoor_provider_request_tokens(
            provider_request_body=None,
            request_messages=rewritten_messages,
            tool_schemas=tool_schemas,
        )
        diagnostics.update(
            {
                "applied": True,
                "compacted_block_tokens": int(getattr(compacted_history, "compacted_block_tokens", 0) or 0),
                "retained_completed_stage_ids": list(
                    getattr(compacted_history, "retained_completed_stage_ids", []) or []
                ),
                "final_request_tokens": rewritten_tokens,
            }
        )
        return FrontdoorTokenPreflightResult(
            request_messages=rewritten_messages,
            final_request_tokens=rewritten_tokens,
            history_shrink_reason="token_compression",
            diagnostics=diagnostics,
        )

    def _resolve_frontdoor_send_model_context_window(
        self,
        *,
        model_refs: list[str] | None,
    ) -> dict[str, Any]:
        model_key = str((list(model_refs or []) or [""])[0] or "").strip()
        if not model_key:
            return {
                "model_key": "",
                "provider_id": "",
                "provider_model": "",
                "resolved_model": "",
                "context_window_tokens": 0,
            }
        config = getattr(self._loop, "app_config", None)
        if config is None:
            try:
                config, _revision, _changed = get_runtime_config(force=False)
            except Exception:
                config = None
        if config is None:
            return {
                "model_key": model_key,
                "provider_id": "",
                "provider_model": model_key,
                "resolved_model": model_key,
                "context_window_tokens": 0,
                "resolution_error": "runtime_config_unavailable",
            }
        info = resolve_send_model_context_window_info(
            config=config,
            model_refs=model_refs,
        )
        return {
            "model_key": model_key,
            "provider_id": str(info.provider_id or "").strip(),
            "provider_model": str(info.provider_model or model_key).strip() or model_key,
            "resolved_model": str(info.resolved_model or info.provider_model or model_key).strip() or model_key,
            "context_window_tokens": int(info.context_window_tokens or 0),
            "resolution_error": str(info.resolution_error or "").strip(),
        }

    @staticmethod
    def _frontdoor_preview_provider_id(*, model_info: dict[str, Any] | None) -> str:
        payload = dict(model_info or {})
        provider_id = str(payload.get("provider_id") or "").strip().lower()
        if provider_id:
            return provider_id
        for candidate in (
            str(payload.get("model_key") or "").strip(),
            str(payload.get("provider_model") or "").strip(),
        ):
            prefix, sep, _rest = candidate.partition(":")
            normalized_prefix = prefix.strip().lower()
            if sep and normalized_prefix in {"responses", "openai_codex"}:
                return normalized_prefix
        return ""

    def _build_frontdoor_provider_request_body_preview(
        self,
        *,
        request_messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        model_info: dict[str, Any] | None,
        prompt_cache_key: str,
        parallel_tool_calls: bool | None,
    ) -> dict[str, Any]:
        provider_id = self._frontdoor_preview_provider_id(model_info=model_info)
        if provider_id not in {"responses", "openai_codex"}:
            return {
                "input": list(request_messages),
                "tools": list(tool_schemas or []),
                "parallel_tool_calls": parallel_tool_calls,
            }
        resolved_model = str(
            dict(model_info or {}).get("resolved_model")
            or dict(model_info or {}).get("provider_model")
            or dict(model_info or {}).get("model_key")
            or ""
        ).strip()
        system_prompt, input_items = _preview_responses_messages(list(request_messages or []))
        if system_prompt:
            input_items.insert(
                0,
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"[SYSTEM]\n{system_prompt}\n[END SYSTEM]"}],
                },
            )
        preview_body: dict[str, Any] = {
            "model": (
                _preview_strip_model_prefix(resolved_model)
                if provider_id == "openai_codex"
                else resolved_model
            ),
            "store": False,
            "stream": True,
            "instructions": system_prompt,
            "input": input_items,
            "text": {"verbosity": "medium"},
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": str(prompt_cache_key or _preview_prompt_cache_key(list(request_messages or []))),
        }
        if tool_schemas:
            preview_body["tools"] = _preview_responses_tools(list(tool_schemas or []))
            preview_body["tool_choice"] = "auto"
            preview_body["parallel_tool_calls"] = (
                bool(parallel_tool_calls) if parallel_tool_calls is not None else True
            )
        return preview_body

    @staticmethod
    def _frontdoor_model_display_name(model_info: dict[str, Any] | None) -> str:
        payload = dict(model_info or {})
        return str(payload.get("provider_model") or payload.get("model_key") or "当前模型").strip() or "当前模型"

    def _frontdoor_missing_context_window_error(self, *, model_info: dict[str, Any] | None) -> FrontdoorCompressionRuntimeError:
        display_name = self._frontdoor_model_display_name(model_info)
        resolution_error = str(dict(model_info or {}).get("resolution_error") or "").strip()
        detail_suffix = f" 原因: {resolution_error}" if resolution_error else ""
        return FrontdoorCompressionRuntimeError(
            code="model_context_window_missing",
            message=f"当前模型{display_name}未配置最大上下文TOKEN，请更改模型链配置后继续{detail_suffix}",
            recoverable=True,
        )

    def _frontdoor_context_window_exceeded_error(self, *, model_info: dict[str, Any] | None) -> FrontdoorCompressionRuntimeError:
        display_name = self._frontdoor_model_display_name(model_info)
        return FrontdoorCompressionRuntimeError(
            code="frontdoor_context_window_exceeded",
            message=f"上下文大小超出当前模型{display_name}，请更改模型链配置后继续",
            recoverable=True,
        )

    @staticmethod
    def _estimate_frontdoor_send_total_tokens(
        *,
        provider_request_body: dict[str, Any] | None,
        request_messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
    ) -> int:
        return _estimate_frontdoor_provider_request_tokens(
            provider_request_body=provider_request_body,
            request_messages=request_messages,
            tool_schemas=tool_schemas,
        )

    def _frontdoor_send_preflight_snapshot(
        self,
        *,
        state: CeoGraphState,
        runtime: Runtime[CeoRuntimeContext],
        langchain_tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        state_for_request = dict(state or {})
        request_messages = list(state_for_request.get("messages") or [])
        prompt_cache_key = str(state_for_request.get("prompt_cache_key") or "")
        prompt_cache_diagnostics = dict(state_for_request.get("prompt_cache_diagnostics") or {})
        actual_tool_schemas: list[dict[str, Any]] = []
        if hasattr(self, "_frontdoor_prompt_contract"):
            try:
                runtime_visible_tool_names = self._frontdoor_runtime_visible_tool_names_for_state(
                    state_for_request,
                    tool_names=list(state_for_request.get("provider_tool_names") or state_for_request.get("tool_names") or []),
                )
                try:
                    tool_schemas = self._selected_tool_schemas(list(runtime_visible_tool_names))
                except Exception:
                    tool_schemas = []
                actual_tool_schemas = list(tool_schemas or [])
                request_contract = self._frontdoor_prompt_contract(
                    state=dict(state_for_request or {}),
                    provider_model=str((list(state_for_request.get("model_refs") or []) or [""])[0] or "").strip(),
                    tool_schemas=tool_schemas,
                    overlay_text=str(state_for_request.get("turn_overlay_text") or "").strip(),
                    session_key=str(state_for_request.get("session_key") or "").strip(),
                    overlay_section_count=len(list(state_for_request.get("dynamic_appendix_messages") or [])),
                )
                request_messages = list(request_contract.request_messages)
                prompt_cache_key = str(request_contract.prompt_cache_key or prompt_cache_key)
                prompt_cache_diagnostics = dict(request_contract.diagnostics or prompt_cache_diagnostics)
            except Exception:
                if hasattr(self, "_state_message_records"):
                    request_messages = list(getattr(self, "_state_message_records")(request_messages))
        elif hasattr(self, "_state_message_records"):
            request_messages = list(getattr(self, "_state_message_records")(request_messages))
        request_messages = self._apply_turn_overlay(
            request_messages,
            overlay_text=str(state_for_request.get("repair_overlay_text") or "").strip(),
        )
        model_info = self._resolve_frontdoor_send_model_context_window(
            model_refs=list(state_for_request.get("model_refs") or []),
        )
        provider_request_body = self._build_frontdoor_provider_request_body_preview(
            request_messages=request_messages,
            tool_schemas=actual_tool_schemas,
            model_info=model_info,
            prompt_cache_key=prompt_cache_key,
            parallel_tool_calls=(bool(state_for_request.get("parallel_enabled")) if list(langchain_tools or []) else None),
        )
        context_window_tokens = int(model_info.get("context_window_tokens") or 0)
        preview_estimate_tokens = self._estimate_frontdoor_send_total_tokens(
            provider_request_body=provider_request_body,
            request_messages=request_messages,
            tool_schemas=actual_tool_schemas,
        )
        session = getattr(getattr(runtime, "context", None), "session", None)
        provider_model = self._frontdoor_model_display_name(model_info)
        latest_record = self._frontdoor_latest_actual_request_record(
            session=session,
            state=state_for_request,
        )
        previous_truth = self._frontdoor_previous_observed_input_truth(
            session=session,
            state=state_for_request,
            latest_record=latest_record,
        )
        previous_effective_input_tokens = int(previous_truth.get("effective_input_tokens") or 0)
        delta_estimate_tokens = 0
        comparable_to_previous_request = False
        previous_provider_model = str(previous_truth.get("provider_model") or "").strip()
        previous_truth_hash = str(previous_truth.get("actual_request_hash") or "").strip()
        latest_record_hash = str(latest_record.get("actual_request_hash") or "").strip()
        if (
            previous_effective_input_tokens > 0
            and previous_provider_model
            and self._frontdoor_provider_models_match(previous_provider_model, provider_model)
            and previous_truth_hash
            and latest_record_hash
            and previous_truth_hash == latest_record_hash
        ):
            delta_estimate_tokens, comparable_to_previous_request = self._frontdoor_append_only_delta_estimate_tokens(
                previous_request_messages=[
                    dict(item)
                    for item in list(latest_record.get("request_messages") or latest_record.get("messages") or [])
                    if isinstance(item, dict)
                ],
                current_request_messages=request_messages,
                previous_tool_schemas=[
                    dict(item)
                    for item in list(latest_record.get("tool_schemas") or [])
                    if isinstance(item, dict)
                ],
                current_tool_schemas=actual_tool_schemas,
            )
        hybrid_estimate = build_runtime_hybrid_send_token_estimate(
            preview_estimate_tokens=int(preview_estimate_tokens or 0),
            previous_effective_input_tokens=previous_effective_input_tokens,
            delta_estimate_tokens=delta_estimate_tokens,
            comparable_to_previous_request=comparable_to_previous_request,
        )
        thresholds = compute_runtime_send_token_preflight_thresholds(
            context_window_tokens=context_window_tokens,
        )
        trigger_tokens = int(thresholds.trigger_tokens or 0)
        effective_trigger_tokens = int(thresholds.effective_trigger_tokens or 0)
        missing_context_window = context_window_tokens <= 25_000
        snapshot = build_runtime_send_token_preflight_snapshot(
            context_window_tokens=context_window_tokens,
            estimated_total_tokens=int(hybrid_estimate.final_estimate_tokens or 0),
        )
        return {
            "request_messages": list(request_messages),
            "tool_schemas": list(actual_tool_schemas),
            "provider_request_body": provider_request_body,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_diagnostics": dict(prompt_cache_diagnostics or {}),
            "model_info": dict(model_info or {}),
            "provider_model": provider_model,
            "context_window_tokens": context_window_tokens,
            "estimated_total_tokens": int(snapshot.estimated_total_tokens or 0),
            "preview_estimate_tokens": int(hybrid_estimate.preview_estimate_tokens or 0),
            "usage_based_estimate_tokens": int(hybrid_estimate.usage_based_estimate_tokens or 0),
            "delta_estimate_tokens": int(hybrid_estimate.delta_estimate_tokens or 0),
            "effective_input_tokens": int(previous_effective_input_tokens or 0),
            "estimate_source": str(hybrid_estimate.estimate_source or "preview_estimate"),
            "comparable_to_previous_request": bool(hybrid_estimate.comparable_to_previous_request),
            "final_estimate_tokens": int(hybrid_estimate.final_estimate_tokens or 0),
            "trigger_tokens": trigger_tokens,
            "effective_trigger_tokens": effective_trigger_tokens,
            "missing_context_window": missing_context_window,
            "would_exceed_context_window": bool(snapshot.would_exceed_context_window),
            "would_trigger_token_compression": bool(snapshot.would_trigger_token_compression),
            "ratio": float(snapshot.ratio or 0.0),
        }

    async def _emit_frontdoor_runtime_snapshot(
        self,
        *,
        runtime: Runtime[CeoRuntimeContext],
        state: dict[str, Any],
    ) -> None:
        session = getattr(getattr(runtime, "context", None), "session", None)
        if session is None:
            return
        self._sync_runtime_session_frontdoor_state(state=state, runtime=runtime)
        emit_snapshot = getattr(session, "_emit_state_snapshot", None)
        if callable(emit_snapshot):
            result = emit_snapshot()
            if hasattr(result, "__await__"):
                await result

    async def _run_frontdoor_llm_token_compression(
        self,
        *,
        state: CeoGraphState,
        runtime: Runtime[CeoRuntimeContext],
        request_messages: list[dict[str, Any]],
        model_refs: list[str],
        tool_schemas: list[dict[str, Any]],
    ) -> FrontdoorTokenPreflightResult:
        body_messages, contract_messages = self._split_request_body_and_tool_contract_messages(request_messages)
        normalized_body = [dict(item) for item in body_messages if isinstance(item, dict)]
        system_prefix: list[dict[str, Any]] = []
        if normalized_body and str(normalized_body[0].get("role") or "").strip().lower() == "system":
            system_prefix = [dict(normalized_body[0])]
            normalized_body = normalized_body[1:]
        model_info = self._resolve_frontdoor_send_model_context_window(model_refs=model_refs)
        prompt_cache_key = str(state.get("prompt_cache_key") or "").strip()
        parallel_tool_calls = bool(state.get("parallel_enabled")) if list(tool_schemas or []) else None
        recent_tail_count = min(len(normalized_body), 4)
        if recent_tail_count <= 0 or len(normalized_body) <= recent_tail_count:
            return FrontdoorTokenPreflightResult(
                request_messages=list(request_messages),
                final_request_tokens=self._estimate_frontdoor_send_total_tokens(
                    provider_request_body=self._build_frontdoor_provider_request_body_preview(
                        request_messages=request_messages,
                        tool_schemas=tool_schemas,
                        model_info=model_info,
                        prompt_cache_key=prompt_cache_key,
                        parallel_tool_calls=parallel_tool_calls,
                    ),
                    request_messages=request_messages,
                    tool_schemas=tool_schemas,
                ),
                history_shrink_reason="",
                diagnostics={"applied": False, "reason": "no_compressible_history"},
            )
        older_history_messages = [dict(item) for item in normalized_body[:-recent_tail_count]]
        recent_tail = [dict(item) for item in normalized_body[-recent_tail_count:]]
        session = getattr(getattr(runtime, "context", None), "session", None)
        generation_id: int | None = None
        begin_generation = getattr(session, "_begin_frontdoor_compression_generation", None)
        finish_generation = getattr(session, "_finish_frontdoor_compression_generation", None)
        is_generation_cancelled = getattr(session, "_is_frontdoor_compression_generation_cancelled", None)
        cancel_token = getattr(session, "_active_cancel_token", None)
        if callable(begin_generation):
            try:
                generation_id = int(begin_generation() or 0)
            except Exception:
                generation_id = None

        def _compression_cancelled() -> bool:
            if cancel_token is not None and callable(getattr(cancel_token, "is_cancelled", None)):
                try:
                    if bool(cancel_token.is_cancelled()):
                        return True
                except Exception:
                    pass
            if generation_id is not None and callable(is_generation_cancelled):
                try:
                    if bool(is_generation_cancelled(generation_id)):
                        return True
                except Exception:
                    return False
            return False

        compression_prompt_messages = [
            {
                "role": "system",
                "content": (
                    "你正在压缩一段较早的对话历史，以便同一模型继续后续推理。\n"
                    "保留事实、用户要求、时间约束、已确认结论、已完成工作、待办事项、关键引用和重要失败信息。\n"
                    "不要写寒暄，不要写解释，不要输出 JSON，只输出可直接放入上下文的中文压缩摘要正文。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "kind": "frontdoor_token_compression",
                        "model": self._frontdoor_model_display_name(model_info),
                        "older_history_messages": older_history_messages,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        compression_state = {
            "status": "running",
            "text": "上下文压缩中",
            "source": "token_compression",
            "needs_recheck": False,
        }
        await self._emit_frontdoor_runtime_snapshot(
            runtime=runtime,
            state={**dict(state or {}), "compression_state": compression_state},
        )
        try:
            compressed_message = await self._call_model_with_tools(
                messages=compression_prompt_messages,
                langchain_tools=[],
                model_refs=list(model_refs or []),
                parallel_tool_calls=None,
                prompt_cache_key="",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if not _compression_cancelled():
                await self._emit_frontdoor_runtime_snapshot(
                    runtime=runtime,
                    state={**dict(state or {}), "compression_state": self._default_compression_state()},
                )
            raise
        try:
            response_view = self._model_response_view(compressed_message)
            self._persist_frontdoor_internal_request_artifact(
                state=state,
                runtime=runtime,
                request_messages=list(compression_prompt_messages),
                tool_schemas=[],
                prompt_cache_key="",
                prompt_cache_diagnostics=build_prompt_cache_diagnostics(
                    stable_messages=list(compression_prompt_messages),
                    dynamic_appendix_messages=[],
                    tool_schemas=[],
                    provider_model=self._frontdoor_model_display_name(model_info),
                    scope="ceo_frontdoor_token_compression",
                    prompt_cache_key="",
                    actual_request_messages=list(compression_prompt_messages),
                    actual_tool_schemas=[],
                ),
                parallel_tool_calls=None,
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
                usage=self._model_response_usage(compressed_message),
                request_lane="token_compression",
                parent_request_id=str(state.get("frontdoor_actual_request_history", [{}])[-1].get("request_id") or "").strip()
                if list(state.get("frontdoor_actual_request_history") or [])
                else "",
            )
            if _compression_cancelled():
                raise asyncio.CancelledError()
            compressed_text = self._content_text(response_view.content).strip()
            if _compression_cancelled():
                raise asyncio.CancelledError()
            if not compressed_text:
                await self._emit_frontdoor_runtime_snapshot(
                    runtime=runtime,
                    state={**dict(state or {}), "compression_state": self._default_compression_state()},
                )
                raise self._frontdoor_context_window_exceeded_error(model_info=model_info)
            compacted_payload = {
                "kind": "frontdoor_token_compaction_llm",
                "history_message_count": len(older_history_messages),
            }
            compacted_block = {
                "role": "assistant",
                "content": (
                    "[G3KU_TOKEN_COMPACT_V2]\n"
                    f"{json.dumps(compacted_payload, ensure_ascii=False, sort_keys=True)}\n\n"
                    f"{compressed_text}"
                ).strip(),
            }
            rewritten_messages = [*system_prefix, compacted_block, *recent_tail, *contract_messages]
            rewritten_tokens = self._estimate_frontdoor_send_total_tokens(
                provider_request_body=self._build_frontdoor_provider_request_body_preview(
                    request_messages=rewritten_messages,
                    tool_schemas=tool_schemas,
                    model_info=model_info,
                    prompt_cache_key=prompt_cache_key,
                    parallel_tool_calls=parallel_tool_calls,
                ),
                request_messages=rewritten_messages,
                tool_schemas=tool_schemas,
            )
            if _compression_cancelled():
                raise asyncio.CancelledError()
            await self._emit_frontdoor_runtime_snapshot(
                runtime=runtime,
                state={**dict(state or {}), "compression_state": self._default_compression_state()},
            )
            return FrontdoorTokenPreflightResult(
                request_messages=rewritten_messages,
                final_request_tokens=rewritten_tokens,
                history_shrink_reason="token_compression",
                diagnostics={
                    "applied": True,
                    "mode": "llm",
                    "retained_recent_tail_count": recent_tail_count,
                    "compressed_history_message_count": len(older_history_messages),
                    "final_request_tokens": rewritten_tokens,
                },
            )
        finally:
            if generation_id is not None and callable(finish_generation):
                try:
                    finish_generation(generation_id)
                except Exception:
                    pass

    def _refresh_runtime_config_for_retry_invalidation(self) -> bool:
        try:
            return bool(
                refresh_loop_runtime_config(
                    self._loop,
                    force=False,
                    reason="provider_retry_invalidation",
                )
            )
        except Exception:
            return False

    @staticmethod
    def _frontdoor_tool_schema_names(tool_schemas: list[dict[str, Any]] | None) -> list[str]:
        names: list[str] = []
        for item in list(tool_schemas or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("function", {}).get("name") or "").strip()
            if name:
                names.append(name)
        return names

    @staticmethod
    def _frontdoor_actual_request_record_from_path(path_text: str) -> dict[str, Any]:
        path = Path(str(path_text or "").strip())
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    @classmethod
    def _frontdoor_previous_actual_request_record(cls, session: Any | None) -> dict[str, Any]:
        if session is None:
            return {}
        previous_history = [
            dict(item)
            for item in list(getattr(session, "_frontdoor_previous_actual_request_history", []) or [])
            if isinstance(item, dict)
        ]
        previous_path = ""
        if previous_history:
            previous_path = str(previous_history[-1].get("path") or "").strip()
        if not previous_path:
            previous_path = str(getattr(session, "_frontdoor_previous_actual_request_path", "") or "").strip()
        if not previous_path:
            return {}
        return cls._frontdoor_actual_request_record_from_path(previous_path)

    @classmethod
    def _frontdoor_latest_actual_request_record(
        cls,
        *,
        session: Any | None,
        state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        candidates = (
            (
                [
                    dict(item)
                    for item in list((state or {}).get("frontdoor_actual_request_history") or [])
                    if isinstance(item, dict)
                ],
                str((state or {}).get("frontdoor_actual_request_path") or "").strip(),
            ),
            (
                [
                    dict(item)
                    for item in list(getattr(session, "_frontdoor_actual_request_history", []) or [])
                    if isinstance(item, dict)
                ],
                str(getattr(session, "_frontdoor_actual_request_path", "") or "").strip(),
            ),
        )
        for history, fallback_path in candidates:
            latest_path = str((history[-1].get("path") if history else "") or fallback_path or "").strip()
            if not latest_path:
                continue
            record = cls._frontdoor_actual_request_record_from_path(latest_path)
            if record:
                return record
        return cls._frontdoor_previous_actual_request_record(session)

    @staticmethod
    def _frontdoor_provider_models_match(previous_provider_model: str, current_provider_model: str) -> bool:
        previous_raw = str(previous_provider_model or "").strip()
        current_raw = str(current_provider_model or "").strip()
        if not previous_raw or not current_raw:
            return False
        if previous_raw == current_raw:
            return True
        previous_model = previous_raw.split(":", 1)[1].strip() if ":" in previous_raw else previous_raw
        current_model = current_raw.split(":", 1)[1].strip() if ":" in current_raw else current_raw
        return bool(previous_model and current_model and previous_model == current_model)

    @staticmethod
    def _frontdoor_previous_observed_input_truth(
        *,
        session: Any | None,
        state: dict[str, Any] | None,
        latest_record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = dict(latest_record or {})
        if isinstance(record.get("observed_input_truth"), dict):
            return dict(record.get("observed_input_truth") or {})
        diagnostics = dict((state or {}).get("frontdoor_token_preflight_diagnostics") or {})
        if isinstance(diagnostics.get("observed_input_truth"), dict):
            return dict(diagnostics.get("observed_input_truth") or {})
        session_diagnostics = dict(getattr(session, "_frontdoor_token_preflight_diagnostics", {}) or {})
        if isinstance(session_diagnostics.get("observed_input_truth"), dict):
            return dict(session_diagnostics.get("observed_input_truth") or {})
        return {}

    @classmethod
    def _frontdoor_append_only_delta_estimate_tokens(
        cls,
        *,
        previous_request_messages: list[dict[str, Any]] | None,
        current_request_messages: list[dict[str, Any]] | None,
        previous_tool_schemas: list[dict[str, Any]] | None,
        current_tool_schemas: list[dict[str, Any]] | None,
    ) -> tuple[int, bool]:
        previous_records = [dict(item) for item in list(previous_request_messages or []) if isinstance(item, dict)]
        current_records = [dict(item) for item in list(current_request_messages or []) if isinstance(item, dict)]
        if not previous_records or len(current_records) < len(previous_records):
            return 0, False
        if not cls._fresh_turn_seed_records_match(current_records[: len(previous_records)], previous_records):
            return 0, False
        previous_tool_schema_hash = str(
            build_actual_request_diagnostics(
                request_messages=[],
                tool_schemas=[
                    dict(item)
                    for item in list(previous_tool_schemas or [])
                    if isinstance(item, dict)
                ],
            ).get("actual_tool_schema_hash")
            or ""
        ).strip()
        current_tool_schema_hash = str(
            build_actual_request_diagnostics(
                request_messages=[],
                tool_schemas=[
                    dict(item)
                    for item in list(current_tool_schemas or [])
                    if isinstance(item, dict)
                ],
            ).get("actual_tool_schema_hash")
            or ""
        ).strip()
        if previous_tool_schema_hash != current_tool_schema_hash:
            return 0, False
        previous_estimate_tokens = int(
            _estimate_frontdoor_provider_request_tokens(
                provider_request_body=None,
                request_messages=previous_records,
                tool_schemas=[
                    dict(item)
                    for item in list(previous_tool_schemas or [])
                    if isinstance(item, dict)
                ],
            )
            or 0
        )
        current_estimate_tokens = int(
            _estimate_frontdoor_provider_request_tokens(
                provider_request_body=None,
                request_messages=current_records,
                tool_schemas=[
                    dict(item)
                    for item in list(current_tool_schemas or [])
                    if isinstance(item, dict)
                ],
            )
            or 0
        )
        return max(0, current_estimate_tokens - previous_estimate_tokens), True

    @classmethod
    def _fresh_turn_seed_normalized_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): cls._fresh_turn_seed_normalized_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._fresh_turn_seed_normalized_value(item) for item in value]
        if isinstance(value, str):
            return value.replace("\r\n", "\n").rstrip()
        return value

    @classmethod
    def _fresh_turn_seed_records_match(
        cls,
        first: list[dict[str, Any]] | None,
        second: list[dict[str, Any]] | None,
    ) -> bool:
        first_records = [dict(item) for item in list(first or []) if isinstance(item, dict)]
        second_records = [dict(item) for item in list(second or []) if isinstance(item, dict)]
        if len(first_records) != len(second_records):
            return False
        return all(
            cls._fresh_turn_seed_normalized_value(left)
            == cls._fresh_turn_seed_normalized_value(right)
            for left, right in zip(first_records, second_records)
        )

    @classmethod
    def _fresh_turn_live_request_messages_from_previous_actual_request(
        cls,
        *,
        session: Any | None,
        stable_messages: list[dict[str, Any]] | None,
        live_request_messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        previous_record = cls._frontdoor_previous_actual_request_record(session)
        previous_request_messages = cls._prompt_message_records(previous_record.get("request_messages"))
        if not previous_request_messages:
            return cls._prompt_message_records(live_request_messages)
        previous_request_body = cls._request_body_messages_without_tool_contracts(previous_request_messages)
        stable_records = cls._prompt_message_records(stable_messages)
        live_records = cls._prompt_message_records(live_request_messages)
        body_len = len(previous_request_body)
        stable_len = len(stable_records)
        if body_len <= 0 or stable_len < body_len:
            return live_records
        if not cls._fresh_turn_seed_records_match(stable_records[:body_len], previous_request_body):
            return live_records
        if len(live_records) < stable_len or not cls._fresh_turn_seed_records_match(
            live_records[:stable_len],
            stable_records,
        ):
            return live_records
        stable_tail = list(stable_records[body_len:])
        live_tail = list(live_records[stable_len:])
        return [*list(previous_request_messages), *stable_tail, *live_tail]

    @classmethod
    def _fresh_turn_tool_schema_seed_from_previous_actual_request(
        cls,
        *,
        session: Any | None,
        tool_schemas: list[dict[str, Any]] | None,
        expected_schema_names: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str] | None]:
        current_tool_schemas = [dict(item) for item in list(tool_schemas or []) if isinstance(item, dict)]
        previous_record = cls._frontdoor_previous_actual_request_record(session)
        previous_tool_schemas = [
            dict(item)
            for item in list(previous_record.get("tool_schemas") or [])
            if isinstance(item, dict)
        ]
        if not previous_tool_schemas or not current_tool_schemas:
            return current_tool_schemas, None
        current_names = cls._frontdoor_tool_schema_names(current_tool_schemas)
        previous_names = cls._frontdoor_tool_schema_names(previous_tool_schemas)
        if not previous_names:
            return current_tool_schemas, None
        normalized_expected_names = [
            str(item or "").strip()
            for item in list(expected_schema_names or [])
            if str(item or "").strip()
        ]
        if normalized_expected_names:
            if previous_names != normalized_expected_names:
                return current_tool_schemas, None
            return previous_tool_schemas, list(previous_names)
        if set(previous_names).issubset(set(current_names)):
            return previous_tool_schemas, list(previous_names)
        return current_tool_schemas, None

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
                tool_names=list(state.get("provider_tool_names") or state.get("tool_names") or []),
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
            raw_names = list(state.get("provider_tool_names") or state.get("tool_names") or [])
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
                description, parameters = _provider_visible_tool_contract(tool)
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": description,
                            "parameters": dict(parameters or {}),
                        },
                    }
                )
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
        return {}

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
        hydrated_tool_names = (
            cls._normalized_hydrated_tool_names(state.get("hydrated_tool_names"))
            if isinstance(state, dict)
            else []
        )
        return (
            frontdoor_stage_state,
            frontdoor_canonical_context,
            compression_state,
            {},
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
            _semantic_context_state,
            hydrated_tool_names,
        ) = self._runtime_session_frontdoor_state(
            state,
            preview_pending_tool_round=preview_pending_tool_round,
        )
        setattr(target_session, "_frontdoor_stage_state", frontdoor_stage_state)
        setattr(target_session, "_frontdoor_canonical_context", frontdoor_canonical_context)
        setattr(target_session, "_compression_state", compression_state)
        setattr(target_session, "_semantic_context_state", {})
        setattr(target_session, "_frontdoor_hydrated_tool_names", list(hydrated_tool_names))
        if isinstance(state, dict):
            setattr(
                target_session,
                "_frontdoor_capability_snapshot_exposure_revision",
                str(state.get("cache_family_revision") or "").strip(),
            )
            setattr(
                target_session,
                "_frontdoor_visible_tool_ids",
                self._normalized_tool_name_state_list(
                    state.get("rbac_visible_tool_names") or state.get("visible_tool_ids")
                ),
            )
            setattr(
                target_session,
                "_frontdoor_visible_skill_ids",
                self._normalized_tool_name_state_list(
                    state.get("rbac_visible_skill_ids") or state.get("visible_skill_ids")
                ),
            )
            setattr(
                target_session,
                "_frontdoor_provider_tool_schema_names",
                self._normalized_tool_name_state_list(
                    state.get("provider_tool_names") or state.get("tool_names")
                ),
            )
        setattr(
            target_session,
            "_frontdoor_selection_debug",
            self._frontdoor_selection_debug_snapshot(state),
        )
        if isinstance(state, dict):
            if "frontdoor_token_preflight_diagnostics" in state:
                setattr(
                    target_session,
                    "_frontdoor_token_preflight_diagnostics",
                    copy.deepcopy(dict(state.get("frontdoor_token_preflight_diagnostics") or {})),
                )
            diagnostics = dict(state.get("prompt_cache_diagnostics") or {})
            actual_request_path = str(state.get("frontdoor_actual_request_path") or "").strip()
            actual_request_history = [
                dict(item)
                for item in list(state.get("frontdoor_actual_request_history") or [])
                if isinstance(item, dict)
            ]
            incoming_has_authoritative_actual_request = bool(actual_request_path) or bool(actual_request_history)
            existing_actual_request_path = str(
                getattr(target_session, "_frontdoor_actual_request_path", "") or ""
            ).strip()
            existing_actual_request_history = [
                dict(item)
                for item in list(getattr(target_session, "_frontdoor_actual_request_history", []) or [])
                if isinstance(item, dict)
            ]
            session_has_authoritative_actual_request = bool(existing_actual_request_path) or bool(
                existing_actual_request_history
            )
            has_authoritative_actual_request = (
                incoming_has_authoritative_actual_request or session_has_authoritative_actual_request
            )
            if actual_request_path:
                setattr(target_session, "_frontdoor_actual_request_path", actual_request_path)
            if actual_request_history:
                setattr(target_session, "_frontdoor_actual_request_history", actual_request_history)
            prompt_cache_key_hash = str(
                state.get("frontdoor_prompt_cache_key_hash")
                or diagnostics.get("prompt_cache_key_hash")
                or ""
            ).strip()
            if prompt_cache_key_hash:
                setattr(target_session, "_frontdoor_prompt_cache_key_hash", prompt_cache_key_hash)
            if incoming_has_authoritative_actual_request:
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
            request_body_messages = [
                dict(item)
                for item in list(state.get("frontdoor_request_body_messages") or [])
                if isinstance(item, dict)
            ]
            if not request_body_messages:
                raw_messages = [
                    dict(item)
                    for item in list(state.get("messages") or [])
                    if isinstance(item, dict)
                ]
                if raw_messages:
                    request_body_messages, _contract_messages = self._split_request_body_and_tool_contract_messages(
                        raw_messages
                    )
            if has_authoritative_actual_request and (
                "frontdoor_request_body_messages" in state or request_body_messages
            ):
                setattr(target_session, "_frontdoor_request_body_messages", request_body_messages)
            if "frontdoor_history_shrink_reason" in state:
                setattr(
                    target_session,
                    "_frontdoor_history_shrink_reason",
                    str(state.get("frontdoor_history_shrink_reason") or "").strip(),
                )
            sync_completed_continuity = getattr(target_session, "_sync_completed_continuity_snapshot", None)
            if callable(sync_completed_continuity):
                should_sync_continuity = incoming_has_authoritative_actual_request or (
                    has_authoritative_actual_request
                    and (
                        "frontdoor_request_body_messages" in state
                        or bool(request_body_messages)
                        or "frontdoor_history_shrink_reason" in state
                    )
                )
                if should_sync_continuity:
                    sync_completed_continuity(
                        source_reason=(
                            "actual_request_sync" if incoming_has_authoritative_actual_request else "finalize"
                        )
                    )

    @staticmethod
    def _session_followup_token_compression_shrink_reason(session: Any) -> str:
        history_candidates = (
            (
                list(getattr(session, "_frontdoor_actual_request_history", []) or []),
                str(getattr(session, "_frontdoor_actual_request_path", "") or "").strip(),
            ),
            (
                list(getattr(session, "_frontdoor_previous_actual_request_history", []) or []),
                str(getattr(session, "_frontdoor_previous_actual_request_path", "") or "").strip(),
            ),
        )
        for raw_history, fallback_path in history_candidates:
            actual_request_history = [
                dict(item)
                for item in list(raw_history or [])
                if isinstance(item, dict)
            ]
            latest_record = dict(actual_request_history[-1]) if actual_request_history else {}
            parent_request_id = str(latest_record.get("request_id") or "").strip()
            latest_request_path = str(
                latest_record.get("path")
                or fallback_path
                or ""
            ).strip()
            latest_turn_id = str(latest_record.get("turn_id") or "").strip()
            if not parent_request_id or not latest_request_path:
                continue
            request_dir = Path(latest_request_path).parent
            if not request_dir.exists():
                continue
            try:
                artifact_paths = sorted(request_dir.glob("*.json"), reverse=True)
            except Exception:
                continue
            for artifact_path in artifact_paths:
                try:
                    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if str(payload.get("request_lane") or "").strip() != "token_compression":
                    continue
                if str(payload.get("parent_request_id") or "").strip() != parent_request_id:
                    continue
                artifact_turn_id = str(payload.get("turn_id") or "").strip()
                if latest_turn_id and artifact_turn_id and artifact_turn_id != latest_turn_id:
                    continue
                return "token_compression"
        return ""

    @staticmethod
    def _session_frontdoor_context_window_snapshot(session: Any) -> tuple[list[dict[str, Any]], str]:
        baseline = [
            dict(item)
            for item in list(getattr(session, "_frontdoor_request_body_messages", []) or [])
            if isinstance(item, dict)
        ]
        shrink_reason = str(getattr(session, "_frontdoor_history_shrink_reason", "") or "").strip()
        if not shrink_reason:
            shrink_reason = str(getattr(session, "_frontdoor_pending_shrink_reason", "") or "").strip()
        if not shrink_reason:
            shrink_reason = CeoFrontDoorRuntimeOps._session_followup_token_compression_shrink_reason(session)
        if shrink_reason:
            setattr(session, "_frontdoor_history_shrink_reason", shrink_reason)
            if str(getattr(session, "_frontdoor_pending_shrink_reason", "") or "").strip():
                setattr(session, "_frontdoor_pending_shrink_reason", "")
        if baseline:
            return baseline, shrink_reason
        paused_snapshot_supplier = getattr(session, "paused_execution_context_snapshot", None)
        paused_snapshot = paused_snapshot_supplier() if callable(paused_snapshot_supplier) else None
        if not isinstance(paused_snapshot, dict):
            return baseline, shrink_reason
        paused_baseline = [
            dict(item)
            for item in list(paused_snapshot.get("frontdoor_request_body_messages") or [])
            if isinstance(item, dict)
        ]
        paused_shrink_reason = str(paused_snapshot.get("frontdoor_history_shrink_reason") or "").strip()
        if paused_baseline:
            setattr(session, "_frontdoor_request_body_messages", list(paused_baseline))
        if paused_shrink_reason:
            setattr(session, "_frontdoor_history_shrink_reason", paused_shrink_reason)
        elif paused_baseline:
            resumed_shrink_reason = str(getattr(session, "_frontdoor_pending_shrink_reason", "") or "").strip()
            if resumed_shrink_reason:
                paused_shrink_reason = resumed_shrink_reason
                setattr(session, "_frontdoor_pending_shrink_reason", "")
                setattr(session, "_frontdoor_history_shrink_reason", resumed_shrink_reason)
        if not paused_shrink_reason and paused_baseline:
            resumed_shrink_reason = CeoFrontDoorRuntimeOps._session_followup_token_compression_shrink_reason(session)
            if resumed_shrink_reason:
                paused_shrink_reason = resumed_shrink_reason
                setattr(session, "_frontdoor_history_shrink_reason", resumed_shrink_reason)
        if paused_baseline or paused_shrink_reason:
            return paused_baseline, paused_shrink_reason
        return baseline, shrink_reason

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
        usage: dict[str, Any] | None = None,
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
        payload = self._build_frontdoor_request_artifact_payload(
            state=state,
            session_key=session_key,
            turn_id=turn_id,
            request_messages=request_messages,
            tool_schemas=tool_schemas,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_diagnostics=prompt_cache_diagnostics,
            parallel_tool_calls=parallel_tool_calls,
            provider_request_meta=provider_request_meta,
            provider_request_body=provider_request_body,
            usage=usage,
            request_kind="frontdoor_actual_request",
            request_lane="visible_frontdoor",
        )
        record = persist_frontdoor_actual_request(
            session_key,
            payload=payload,
        )
        if not record:
            return {}
        observed_input_truth = (
            copy.deepcopy(dict(payload.get("observed_input_truth") or {}))
            if isinstance(payload, dict)
            else {}
        )
        frontdoor_token_preflight_diagnostics = (
            copy.deepcopy(dict(payload.get("frontdoor_token_preflight_diagnostics") or {}))
            if isinstance(payload, dict)
            else {}
        )
        if observed_input_truth:
            record["observed_input_truth"] = copy.deepcopy(observed_input_truth)
        authoritative_request_body_messages = self._request_body_messages_without_tool_contracts(request_messages)
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
            setattr(target_session, "_frontdoor_request_body_messages", list(authoritative_request_body_messages))
            setattr(target_session, "_frontdoor_prompt_cache_key_hash", str(record.get("prompt_cache_key_hash") or "").strip())
            setattr(target_session, "_frontdoor_actual_request_hash", str(record.get("actual_request_hash") or "").strip())
            setattr(target_session, "_frontdoor_actual_request_message_count", int(record.get("actual_request_message_count") or 0))
            setattr(target_session, "_frontdoor_actual_tool_schema_hash", str(record.get("actual_tool_schema_hash") or "").strip())
            if frontdoor_token_preflight_diagnostics:
                setattr(
                    target_session,
                    "_frontdoor_token_preflight_diagnostics",
                    copy.deepcopy(frontdoor_token_preflight_diagnostics),
                )
        return {
            "frontdoor_actual_request_path": str(record.get("path") or "").strip(),
            "frontdoor_actual_request_history": list(existing_history),
            "frontdoor_request_body_messages": list(authoritative_request_body_messages),
            "frontdoor_prompt_cache_key_hash": str(record.get("prompt_cache_key_hash") or "").strip(),
            "frontdoor_actual_request_hash": str(record.get("actual_request_hash") or "").strip(),
            "frontdoor_actual_request_message_count": int(record.get("actual_request_message_count") or 0),
            "frontdoor_actual_tool_schema_hash": str(record.get("actual_tool_schema_hash") or "").strip(),
            "frontdoor_token_preflight_diagnostics": copy.deepcopy(frontdoor_token_preflight_diagnostics),
        }

    @staticmethod
    def _frontdoor_observed_input_truth(
        *,
        usage: dict[str, Any] | None,
        provider_model: str,
        actual_request_hash: str,
    ) -> dict[str, Any]:
        normalized_usage = normalize_usage_payload(usage)
        if not normalized_usage:
            return {}
        truth = build_runtime_observed_input_truth(
            usage=normalized_usage,
            provider_model=str(provider_model or "").strip(),
            actual_request_hash=str(actual_request_hash or "").strip(),
            source="provider_usage",
        )
        if int(truth.input_tokens or 0) <= 0 and int(truth.cache_hit_tokens or 0) <= 0:
            return {}
        return {
            "effective_input_tokens": int(truth.effective_input_tokens or 0),
            "input_tokens": int(truth.input_tokens or 0),
            "cache_hit_tokens": int(truth.cache_hit_tokens or 0),
            "provider_model": str(truth.provider_model or "").strip(),
            "actual_request_hash": str(truth.actual_request_hash or "").strip(),
            "source": str(truth.source or "").strip(),
        }

    @staticmethod
    def _frontdoor_diagnostics_with_observed_input_truth(
        diagnostics: dict[str, Any] | None,
        observed_input_truth: dict[str, Any] | None,
    ) -> dict[str, Any]:
        merged = copy.deepcopy(dict(diagnostics or {}))
        truth = dict(observed_input_truth or {})
        if not truth:
            return merged
        merged["observed_input_truth"] = copy.deepcopy(truth)
        merged["effective_input_tokens"] = int(truth.get("effective_input_tokens") or 0)
        merged["input_tokens"] = int(truth.get("input_tokens") or 0)
        merged["cache_hit_tokens"] = int(truth.get("cache_hit_tokens") or 0)
        merged["effective_input_tokens_source"] = str(truth.get("source") or "provider_usage")
        return merged

    def _build_frontdoor_request_artifact_payload(
        self,
        *,
        state: CeoGraphState,
        session_key: str,
        turn_id: str,
        request_messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None,
        prompt_cache_key: str,
        prompt_cache_diagnostics: dict[str, Any] | None,
        parallel_tool_calls: bool | None,
        provider_request_meta: dict[str, Any] | None = None,
        provider_request_body: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        request_kind: str,
        request_lane: str,
        parent_request_id: str = "",
    ) -> dict[str, Any]:
        diagnostics = dict(prompt_cache_diagnostics or {})
        provider_model = str((list(state.get("model_refs") or []) or [""])[0] or "").strip()
        resolved_provider_model = str(
            dict(state.get("frontdoor_token_preflight_diagnostics") or {}).get("provider_model")
            or provider_model
            or ""
        ).strip()
        observed_input_truth = self._frontdoor_observed_input_truth(
            usage=usage,
            provider_model=resolved_provider_model,
            actual_request_hash=str(diagnostics.get("actual_request_hash") or "").strip(),
        )
        frontdoor_token_preflight_diagnostics = self._frontdoor_diagnostics_with_observed_input_truth(
            dict(state.get("frontdoor_token_preflight_diagnostics") or {}),
            observed_input_truth,
        )
        return {
            "type": str(request_kind or "").strip() or "frontdoor_actual_request",
            "request_kind": str(request_kind or "").strip() or "frontdoor_actual_request",
            "request_lane": str(request_lane or "").strip() or "visible_frontdoor",
            "session_key": str(session_key or "").strip(),
            "turn_id": str(turn_id or "").strip(),
            "parent_request_id": str(parent_request_id or "").strip(),
            "created_at": now_iso(),
            "provider_model": resolved_provider_model,
            "model_refs": [
                str(item or "").strip()
                for item in list(state.get("model_refs") or [])
                if str(item or "").strip()
            ],
            "frontdoor_history_shrink_reason": str(state.get("frontdoor_history_shrink_reason") or "").strip(),
            "frontdoor_token_preflight_diagnostics": copy.deepcopy(frontdoor_token_preflight_diagnostics),
            "parallel_tool_calls": parallel_tool_calls,
            "prompt_cache_key": str(prompt_cache_key or "").strip(),
            "prompt_cache_key_hash": str(diagnostics.get("prompt_cache_key_hash") or "").strip(),
            "actual_request_hash": str(diagnostics.get("actual_request_hash") or "").strip(),
            "actual_request_message_count": int(diagnostics.get("actual_request_message_count") or 0),
            "actual_tool_schema_hash": str(diagnostics.get("actual_tool_schema_hash") or "").strip(),
            "tool_signature_hash": str(diagnostics.get("tool_signature_hash") or "").strip(),
            "stable_prefix_hash": str(diagnostics.get("stable_prefix_hash") or "").strip(),
            "dynamic_appendix_hash": str(diagnostics.get("dynamic_appendix_hash") or "").strip(),
            "observed_input_truth": copy.deepcopy(observed_input_truth),
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
            "usage": normalize_usage_payload(usage),
        }

    def _persist_frontdoor_internal_request_artifact(
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
        usage: dict[str, Any] | None = None,
        request_lane: str,
        parent_request_id: str = "",
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
        return persist_frontdoor_actual_request(
            session_key,
            payload=self._build_frontdoor_request_artifact_payload(
                state=state,
                session_key=session_key,
                turn_id=turn_id,
                request_messages=request_messages,
                tool_schemas=tool_schemas,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_diagnostics=prompt_cache_diagnostics,
                parallel_tool_calls=parallel_tool_calls,
                provider_request_meta=provider_request_meta,
                provider_request_body=provider_request_body,
                usage=usage,
                request_kind="frontdoor_internal_request",
                request_lane=request_lane,
                parent_request_id=parent_request_id,
            ),
        )

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
            tool_names=list(refreshed.get("provider_tool_names") or refreshed.get("tool_names") or []),
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
            getattr(assembly_cfg, "frontdoor_interrupt_tool_names", ["create_async_task"]) or []
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

    @staticmethod
    def _model_response_usage(message: AIMessage | dict[str, Any]) -> dict[str, int]:
        if isinstance(message, dict):
            payload = dict(message or {})
            return normalize_usage_payload(payload.get("usage"))
        response_metadata = dict(getattr(message, "response_metadata", {}) or {})
        return normalize_usage_payload(response_metadata.get("usage") or getattr(message, "usage", None))

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

    @classmethod
    def _parse_create_async_task_result(cls, result_text: str) -> dict[str, Any]:
        text = str(result_text or "").strip()
        if text.startswith("创建任务成功"):
            return {
                "created": True,
                "created_task_ids": cls._normalize_task_ids(_TASK_ID_PATTERN.findall(text)),
                "rejection_kind": "",
            }
        if text.startswith("任务未创建："):
            rejection_kind = "duplicate"
            if "task_append_notice" in text or "追加通知" in text:
                rejection_kind = "append_notice"
            return {
                "created": False,
                "created_task_ids": [],
                "rejection_kind": rejection_kind,
            }
        return {
            "created": False,
            "created_task_ids": [],
            "rejection_kind": "",
        }

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
        builder_user_metadata = dict(metadata or {})
        batch_query_text = str(metadata.get("web_ceo_batch_query_text") or "").strip()
        query_text = batch_query_text or self._content_text(user_content)
        heartbeat_internal = bool(metadata.get("heartbeat_internal"))
        cron_internal = bool(metadata.get("cron_internal"))
        retrieval_query = str(metadata.get("heartbeat_retrieval_query") or "").strip()
        builder_query_text = retrieval_query if heartbeat_internal and retrieval_query else query_text
        runtime_session = self._loop.sessions.get_or_create(session.state.session_key)
        session_request_body_messages, session_shrink_reason = self._session_frontdoor_context_window_snapshot(session)
        main_service = getattr(self._loop, "main_task_service", None)
        if main_service is not None:
            await main_service.startup()

        for name in ("cron",):
            tool = self._loop.tools.get(name)
            if tool is not None and hasattr(tool, "set_context"):
                tool.set_context(
                    getattr(session, "_channel", "cli"),
                    getattr(session, "_chat_id", session.state.session_key),
                )

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
        if not list(current_frontdoor_stage_state.get("stages") or []):
            current_frontdoor_stage_state = self._frontdoor_stage_state_snapshot(
                {"frontdoor_stage_state": getattr(session, "_frontdoor_stage_state", {})}
            )
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
        if not list(current_frontdoor_canonical_context.get("stages") or []):
            current_frontdoor_canonical_context = normalize_frontdoor_canonical_context(
                getattr(session, "_frontdoor_canonical_context", {}) or {}
            )
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
            current_compression_state = dict(getattr(session, "_compression_state", {}) or {})
        if not self._compression_state_has_material_content(current_compression_state):
            paused_compression_state = (
                dict(paused_manual_snapshot.get("compression") or {})
                if paused_manual_snapshot
                else {}
            )
            if self._compression_state_has_material_content(paused_compression_state):
                current_compression_state = paused_compression_state
        checkpoint_messages = list(state.get("messages") or [])
        request_body_seed_messages: list[dict[str, Any]] = []
        if session_request_body_messages:
            if not heartbeat_internal and not cron_internal:
                request_body_seed_messages = list(session_request_body_messages)
                checkpoint_messages = []
                builder_user_metadata["_frontdoor_history_seed"] = "session_window"
            elif not checkpoint_messages:
                checkpoint_messages = list(session_request_body_messages)
                builder_user_metadata["_frontdoor_history_seed"] = "session_window"
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
            checkpoint_messages=checkpoint_messages,
            request_body_seed_messages=request_body_seed_messages,
            user_content=self._model_content(user_content),
            user_metadata=builder_user_metadata,
            frontdoor_stage_state=current_frontdoor_stage_state,
            frontdoor_canonical_context=current_frontdoor_canonical_context,
            semantic_context_state={},
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
        provider_tool_seed_names = (
            list(tool_names)
            if cron_internal
            else [
                str(item or "").strip()
                for item in list(exposure.get("tool_names") or [])
                if str(item or "").strip()
            ]
        )
        runtime_visible_tool_names = self._frontdoor_runtime_visible_tool_names_for_state(
            {
                "frontdoor_stage_state": current_frontdoor_stage_state,
                "cron_internal": cron_internal,
                "heartbeat_internal": heartbeat_internal,
            },
            tool_names=provider_tool_seed_names,
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
            if session_request_body_messages:
                continuity_bridge = {"pending": False, "enabled": False}
                consume_continuity_bridge = getattr(session, "_consume_completed_continuity_bridge", None)
                if callable(consume_continuity_bridge):
                    continuity_bridge = dict(
                        consume_continuity_bridge(
                            current_visible_tool_ids=rbac_visible_tool_names,
                            current_visible_skill_ids=rbac_visible_skill_ids,
                        )
                        or {}
                    )
                live_request_messages = self._fresh_turn_live_request_messages_from_previous_actual_request(
                    session=session,
                    stable_messages=stable_messages,
                    live_request_messages=live_request_messages,
                )
                if bool(continuity_bridge.get("enabled")):
                    cache_family_revision = str(
                        continuity_bridge.get("exposure_revision") or cache_family_revision or ""
                    ).strip()
                seeded_provider_tool_names: list[str] | None = None
                if not (bool(continuity_bridge.get("pending")) and not bool(continuity_bridge.get("enabled"))):
                    tool_schemas, seeded_provider_tool_names = (
                        self._fresh_turn_tool_schema_seed_from_previous_actual_request(
                            session=session,
                            tool_schemas=tool_schemas,
                            expected_schema_names=list(
                                continuity_bridge.get("provider_tool_schema_names") or []
                            )
                            if bool(continuity_bridge.get("enabled"))
                            else None,
                        )
                    )
                if seeded_provider_tool_names:
                    runtime_visible_tool_names = list(seeded_provider_tool_names)
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
        shrink_reason = str(
            getattr(assembly, "trace", {}).get("frontdoor_history_shrink_reason")
            or state.get("frontdoor_history_shrink_reason")
            or session_shrink_reason
            or ""
        ).strip()
        if session_request_body_messages and not heartbeat_internal:
            # Compare shrink on the same provider-facing shape. Session continuity
            # baselines may still carry runtime-only tool metadata such as status
            # or timing fields, but those fields are stripped when the next visible
            # turn rebuilds its request-body seed.
            previous_tokens = estimate_message_tokens(
                CeoMessageBuilder._request_body_seed_records(session_request_body_messages)
            )
            next_tokens = estimate_message_tokens(
                CeoMessageBuilder._request_body_seed_records(persisted_messages)
            )
            if next_tokens < previous_tokens and shrink_reason not in self._ALLOWED_FRONTDOOR_SHRINK_REASONS.difference({""}):
                raise RuntimeError("frontdoor context shrank without an allowed reason")
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
            "turn_overlay_text": turn_overlay_text or None,
            "frontdoor_selection_debug": frontdoor_selection_debug,
            "tool_names": list(tool_names),
            "provider_tool_names": list(runtime_visible_tool_names),
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
            "frontdoor_live_request_messages": list(live_request_messages),
            "frontdoor_request_body_messages": persisted_messages,
            "frontdoor_history_shrink_reason": shrink_reason,
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

    async def _consume_session_follow_up_messages_before_call_model(
        self,
        *,
        state: CeoGraphState,
        runtime: Runtime[CeoRuntimeContext],
    ) -> dict[str, Any]:
        session = getattr(getattr(runtime, "context", None), "session", None)
        if session is None:
            return {}
        take_follow_ups = getattr(session, "take_follow_up_batch_for_call_model", None)
        if not callable(take_follow_ups):
            return {}
        drained = take_follow_ups()
        if hasattr(drained, "__await__"):
            drained = await drained
        queued_inputs = [
            item
            for item in list(drained or [])
            if isinstance(item, UserInputMessage)
        ]
        if not queued_inputs:
            return {}
        request_body_messages = [
            dict(item)
            for item in list(
                state.get("frontdoor_request_body_messages")
                or state.get("messages")
                or getattr(session, "_frontdoor_request_body_messages", [])
                or []
            )
            if isinstance(item, dict)
        ]
        if request_body_messages:
            request_body_messages = self._request_body_messages_without_tool_contracts(request_body_messages)
        follow_up_messages: list[dict[str, Any]] = []
        follow_up_texts: list[str] = []
        for item in queued_inputs:
            follow_up_messages.append({"role": "user", "content": self._model_content(getattr(item, "content", ""))})
            follow_up_text = self._content_text(getattr(item, "content", ""))
            if follow_up_text.strip():
                follow_up_texts.append(follow_up_text)
        if not follow_up_messages:
            return {}
        updated_request_body_messages = [*request_body_messages, *follow_up_messages]
        current_query_text = str(state.get("query_text") or "").strip()
        appended_query_text = "\n\n".join(follow_up_texts).strip()
        merged_query_text = "\n\n".join(
            part
            for part in (current_query_text, appended_query_text)
            if str(part or "").strip()
        ).strip()
        update = {
            "messages": list(updated_request_body_messages),
            "frontdoor_request_body_messages": list(updated_request_body_messages),
        }
        if merged_query_text:
            update["query_text"] = merged_query_text
        self._sync_runtime_session_frontdoor_state(
            state={**dict(state or {}), **update},
            runtime=runtime,
        )
        return update

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

        state_for_request = dict(state or {})
        follow_up_update = await self._consume_session_follow_up_messages_before_call_model(
            state=state_for_request,
            runtime=runtime,
        )
        if follow_up_update:
            state_for_request = {**state_for_request, **follow_up_update}
        langchain_tools = self._build_langchain_tools_for_state(state=state_for_request, runtime=runtime)
        while True:
            preflight_snapshot = self._frontdoor_send_preflight_snapshot(
                state=state_for_request,
                runtime=runtime,
                langchain_tools=langchain_tools,
            )
            request_messages = list(preflight_snapshot.get("request_messages") or [])
            prompt_cache_key = str(preflight_snapshot.get("prompt_cache_key") or "")
            prompt_cache_diagnostics = dict(preflight_snapshot.get("prompt_cache_diagnostics") or {})
            actual_tool_schemas = list(preflight_snapshot.get("tool_schemas") or [])
            model_info = dict(preflight_snapshot.get("model_info") or {})
            context_window_tokens = int(preflight_snapshot.get("context_window_tokens") or 0)
            if context_window_tokens <= 25_000:
                raise self._frontdoor_missing_context_window_error(model_info=model_info)
            estimated_total_tokens = int(preflight_snapshot.get("estimated_total_tokens") or 0)
            trigger_tokens = int(preflight_snapshot.get("trigger_tokens") or 0)
            preflight_diagnostics = {
                "applied": False,
                "mode": "llm",
                "final_request_tokens": estimated_total_tokens,
                "estimated_total_tokens": estimated_total_tokens,
                "preview_estimate_tokens": int(preflight_snapshot.get("preview_estimate_tokens") or 0),
                "usage_based_estimate_tokens": int(preflight_snapshot.get("usage_based_estimate_tokens") or 0),
                "delta_estimate_tokens": int(preflight_snapshot.get("delta_estimate_tokens") or 0),
                "effective_input_tokens": int(preflight_snapshot.get("effective_input_tokens") or 0),
                "estimate_source": str(preflight_snapshot.get("estimate_source") or "preview_estimate"),
                "comparable_to_previous_request": bool(preflight_snapshot.get("comparable_to_previous_request")),
                "final_estimate_tokens": int(preflight_snapshot.get("final_estimate_tokens") or estimated_total_tokens),
                "trigger_tokens": trigger_tokens,
                "effective_trigger_tokens": int(preflight_snapshot.get("effective_trigger_tokens") or 0),
                "max_context_tokens": context_window_tokens,
                "provider_model": str(preflight_snapshot.get("provider_model") or self._frontdoor_model_display_name(model_info)),
                "would_exceed_context_window": bool(preflight_snapshot.get("would_exceed_context_window")),
                "would_trigger_token_compression": bool(preflight_snapshot.get("would_trigger_token_compression")),
                "ratio": float(preflight_snapshot.get("ratio") or 0.0),
            }
            preflight_shrink_reason = ""
            should_attempt_token_compression = bool(
                preflight_snapshot.get("would_trigger_token_compression")
                or preflight_snapshot.get("would_exceed_context_window")
            )
            if should_attempt_token_compression:
                runtime_session = getattr(getattr(runtime, "context", None), "session", None)
                if runtime_session is not None:
                    setattr(runtime_session, "_frontdoor_pending_shrink_reason", "token_compression")
                pre_compaction_diagnostics = dict(preflight_diagnostics)
                preflight = await self._run_frontdoor_llm_token_compression(
                    state=state_for_request,
                    runtime=runtime,
                    request_messages=request_messages,
                    model_refs=list(state_for_request.get("model_refs") or []),
                    tool_schemas=actual_tool_schemas,
                )
                request_messages = list(preflight.request_messages)
                post_compaction_tokens = int(preflight.final_request_tokens or 0)
                post_compaction_snapshot = build_runtime_send_token_preflight_snapshot(
                    context_window_tokens=context_window_tokens,
                    estimated_total_tokens=post_compaction_tokens,
                )
                preflight_diagnostics = {
                    **dict(preflight.diagnostics or {}),
                    "applied": True,
                    "mode": "llm",
                    "final_request_tokens": post_compaction_tokens,
                    "estimated_total_tokens": int(post_compaction_snapshot.estimated_total_tokens or 0),
                    "preview_estimate_tokens": int(post_compaction_tokens or 0),
                    "usage_based_estimate_tokens": 0,
                    "delta_estimate_tokens": 0,
                    "effective_input_tokens": 0,
                    "estimate_source": "preview_estimate",
                    "comparable_to_previous_request": False,
                    "final_estimate_tokens": int(post_compaction_tokens or 0),
                    "trigger_tokens": trigger_tokens,
                    "effective_trigger_tokens": int(preflight_snapshot.get("effective_trigger_tokens") or 0),
                    "max_context_tokens": context_window_tokens,
                    "provider_model": str(
                        preflight_snapshot.get("provider_model") or self._frontdoor_model_display_name(model_info)
                    ),
                    "ratio": float(post_compaction_snapshot.ratio or 0.0),
                    "would_exceed_context_window": bool(post_compaction_snapshot.would_exceed_context_window),
                    "would_trigger_token_compression": bool(post_compaction_snapshot.would_trigger_token_compression),
                    "pre_compaction_estimated_total_tokens": int(
                        pre_compaction_diagnostics.get("estimated_total_tokens") or 0
                    ),
                    "pre_compaction_preview_estimate_tokens": int(
                        pre_compaction_diagnostics.get("preview_estimate_tokens") or 0
                    ),
                    "pre_compaction_usage_based_estimate_tokens": int(
                        pre_compaction_diagnostics.get("usage_based_estimate_tokens") or 0
                    ),
                    "pre_compaction_delta_estimate_tokens": int(
                        pre_compaction_diagnostics.get("delta_estimate_tokens") or 0
                    ),
                    "pre_compaction_effective_input_tokens": int(
                        pre_compaction_diagnostics.get("effective_input_tokens") or 0
                    ),
                    "pre_compaction_estimate_source": str(
                        pre_compaction_diagnostics.get("estimate_source") or "preview_estimate"
                    ),
                    "pre_compaction_comparable_to_previous_request": bool(
                        pre_compaction_diagnostics.get("comparable_to_previous_request")
                    ),
                    "pre_compaction_final_estimate_tokens": int(
                        pre_compaction_diagnostics.get("final_estimate_tokens") or 0
                    ),
                    "pre_compaction_ratio": float(pre_compaction_diagnostics.get("ratio") or 0.0),
                    "pre_compaction_would_exceed_context_window": bool(
                        pre_compaction_diagnostics.get("would_exceed_context_window")
                    ),
                    "pre_compaction_would_trigger_token_compression": bool(
                        pre_compaction_diagnostics.get("would_trigger_token_compression")
                    ),
                    **dict(preflight.diagnostics or {}),
                }
                preflight_shrink_reason = str(preflight.history_shrink_reason or "").strip()
                if runtime_session is not None and preflight_shrink_reason:
                    setattr(runtime_session, "_frontdoor_pending_shrink_reason", "")
                if int(preflight.final_request_tokens or 0) > context_window_tokens:
                    raise self._frontdoor_context_window_exceeded_error(model_info=model_info)
            state_for_request = {
                **dict(state_for_request or {}),
                "frontdoor_live_request_messages": list(request_messages),
                "frontdoor_token_preflight_diagnostics": preflight_diagnostics,
                "frontdoor_history_shrink_reason": str(
                    preflight_shrink_reason
                    or state_for_request.get("frontdoor_history_shrink_reason")
                    or ""
                ).strip(),
            }
            prompt_cache_diagnostics = {
                **prompt_cache_diagnostics,
                **build_actual_request_diagnostics(
                    request_messages=request_messages,
                    tool_schemas=actual_tool_schemas,
                ),
            }
            provider_retry_count = 0
            empty_response_retry_count = 0
            restart_with_refreshed_runtime = False
            while True:
                try:
                    message = await self._call_model_with_tools(
                        messages=request_messages,
                        langchain_tools=langchain_tools,
                        model_refs=list(state_for_request.get("model_refs") or []),
                        parallel_tool_calls=(bool(state_for_request.get("parallel_enabled")) if langchain_tools else None),
                        prompt_cache_key=prompt_cache_key,
                    )
                except Exception as exc:
                    if PUBLIC_PROVIDER_FAILURE_MESSAGE not in str(exc or ""):
                        raise
                    if self._refresh_runtime_config_for_retry_invalidation():
                        state_for_request["model_refs"] = list(self._resolve_ceo_model_refs())
                        restart_with_refreshed_runtime = True
                        break
                    provider_retry_count += 1
                    await asyncio.sleep(float(min(10, max(1, provider_retry_count))))
                    continue
                response_view = self._model_response_view(message)
                if self._is_empty_model_response(response_view):
                    if self._refresh_runtime_config_for_retry_invalidation():
                        state_for_request["model_refs"] = list(self._resolve_ceo_model_refs())
                        restart_with_refreshed_runtime = True
                        break
                    empty_response_retry_count += 1
                    await asyncio.sleep(float(min(10, max(1, empty_response_retry_count))))
                    continue
                break
            if restart_with_refreshed_runtime:
                continue
            break
        response_view = self._model_response_view(message)
        actual_request_trace = self._persist_frontdoor_actual_request(
            state=state_for_request,
            runtime=runtime,
            request_messages=request_messages,
            tool_schemas=actual_tool_schemas,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_diagnostics=prompt_cache_diagnostics,
            parallel_tool_calls=(bool(state_for_request.get("parallel_enabled")) if langchain_tools else None),
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
            usage=self._model_response_usage(message),
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
            "frontdoor_live_request_messages": [],
            "model_refs": list(state_for_request.get("model_refs") or []),
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_diagnostics": prompt_cache_diagnostics,
            "frontdoor_token_preflight_diagnostics": dict(
                state_for_request.get("frontdoor_token_preflight_diagnostics") or {}
            ),
            "frontdoor_history_shrink_reason": str(
                state_for_request.get("frontdoor_history_shrink_reason") or ""
            ).strip(),
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
                    "route_kind": self._route_kind_for_turn(
                        used_tools=used_tools,
                        default=current_route_kind,
                        verified_task_ids=list(state.get("verified_task_ids") or []),
                    ),
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
                    "route_kind": self._route_kind_for_turn(
                        used_tools=used_tools,
                        default=current_route_kind,
                        verified_task_ids=list(state.get("verified_task_ids") or []),
                    ),
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
                "route_kind": self._route_kind_for_turn(
                    used_tools=used_tools,
                    default=current_route_kind,
                    verified_task_ids=list(state.get("verified_task_ids") or []),
                ),
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
            "final_output": self._empty_response_explanation(
                used_tools=used_tools,
                verified_task_ids=list(state.get("verified_task_ids") or []),
            ),
            "route_kind": self._route_kind_for_turn(
                used_tools=used_tools,
                default=current_route_kind,
                verified_task_ids=list(state.get("verified_task_ids") or []),
            ),
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
            _preview_semantic_context_state,
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
        authoritative_request_body_messages = self._request_body_messages_without_tool_contracts(messages)

        used_tools = list(state.get("used_tools") or [])
        used_tools.extend(
            [
                str(payload.get("name") or "").strip()
                for payload in tool_call_payloads
                if str(payload.get("name") or "").strip()
                and str(payload.get("name") or "").strip() not in self._CONTROL_TOOL_NAMES
            ]
        )
        verified_task_ids: list[str] = []
        for tool_result in tool_results:
            tool_name = str(tool_result.get("tool_name") or "").strip()
            result_text = str(tool_result.get("result_text") or "").strip()
            if tool_name != "create_async_task":
                continue
            parsed = self._parse_create_async_task_result(result_text)
            if not bool(parsed.get("created")):
                continue
            for task_id in list(parsed.get("created_task_ids") or []):
                if not task_id or not self._task_id_exists(task_id) or task_id in verified_task_ids:
                    continue
                verified_task_ids.append(task_id)
        route_kind = self._route_kind_for_turn(
            used_tools=used_tools,
            default=str(state.get("route_kind") or "direct_reply"),
            verified_task_ids=verified_task_ids,
        )
        substantive_tool_names = [
            str(payload.get("name") or "").strip()
            for payload in tool_call_payloads
            if str(payload.get("name") or "").strip()
            and str(payload.get("name") or "").strip() not in self._CONTROL_TOOL_NAMES
        ]
        result = {
            "messages": messages,
            "frontdoor_request_body_messages": authoritative_request_body_messages,
            "used_tools": used_tools,
            "route_kind": route_kind,
            "analysis_text": "",
            "tool_call_payloads": [],
            "verified_task_ids": list(verified_task_ids),
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
        authoritative_request_body_messages = [
            dict(item)
            for item in list(state.get("frontdoor_request_body_messages") or request_body_messages or messages)
            if isinstance(item, dict)
        ]
        frontdoor_history_shrink_reason = str(state.get("frontdoor_history_shrink_reason") or "").strip()
        finalized_stage_state = self._frontdoor_stage_state_snapshot(state)
        should_append_visible_output = bool(output) and not bool(state.get("heartbeat_internal")) and not bool(
            state.get("cron_internal")
        )
        if should_append_visible_output:
            messages.append({"role": "assistant", "content": output})
            authoritative_request_body_messages = [
                *list(authoritative_request_body_messages),
                {"role": "assistant", "content": output},
            ]
        if output and route_kind == "direct_reply":
            result["messages"] = list(messages)
            result["frontdoor_request_body_messages"] = list(authoritative_request_body_messages)
            result["frontdoor_history_shrink_reason"] = frontdoor_history_shrink_reason
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
        result["frontdoor_request_body_messages"] = list(authoritative_request_body_messages)
        result["frontdoor_history_shrink_reason"] = frontdoor_history_shrink_reason
        return result

    @staticmethod
    def _graph_next_step(state: CeoGraphState) -> str:
        next_step = str(state.get("next_step") or "finalize").strip()
        if next_step not in {"call_model", "review_tool_calls", "execute_tools", "finalize"}:
            return "finalize"
        return next_step


__all__ = ["CeoFrontDoorRuntimeOps"]
