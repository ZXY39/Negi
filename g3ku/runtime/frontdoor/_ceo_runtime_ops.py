from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import hashlib
import json
import math
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from loguru import logger

from g3ku.agent.tools.base import Tool
from g3ku.config.live_runtime import get_runtime_config, peek_runtime_revision
from g3ku.core.messages import UserInputMessage
from g3ku.core.timefmt import render_arrival_stamp, strip_arrival_time_stamp
from g3ku.json_schema_utils import (
    normalize_runtime_tool_arguments_dict,
    sanitize_provider_parameters_schema,
)
from g3ku.providers.base import normalize_usage_payload
from g3ku.providers.fallback import (
    MODEL_RETRY_LIVE_STATES,
    PUBLIC_PROVIDER_FAILURE_MESSAGE,
    ModelProviderExhaustedError,
    ModelProviderResponseError,
)
from g3ku.providers.responses_protocol_helpers import (
    _convert_messages as _preview_responses_messages,
)
from g3ku.providers.responses_protocol_helpers import (
    _convert_tools as _preview_responses_tools,
)
from g3ku.providers.responses_protocol_helpers import (
    _prompt_cache_key as _preview_prompt_cache_key,
)
from g3ku.resources.retired_schemas import (
    index_schemas_by_name,
    lookup_tool_schema,
    placeholder_tool_schema,
    schema_name,
)
from g3ku.runtime import skill_roster_notice as _skill_roster_notice
from g3ku.runtime import tool_arg_stash
from g3ku.runtime.chunk_compression_plan import (
    CHUNK_MAX_CHUNKS,
    COMPRESSION_MAX_PASS_ROUNDS,
    COMPRESSION_MODE_CHUNKED,
    CompressionPass,
    plan_compression_pass,
)
from g3ku.runtime.config_refresh import refresh_loop_runtime_config
from g3ku.runtime.context.image_metrics import bmp_dimensions, webp_dimensions
from g3ku.runtime.context.summarizer import estimate_tokens
from g3ku.runtime.frontdoor import pinned_tool_bundle
from g3ku.runtime.frontdoor.message_builder import (
    MEMORY_SNAPSHOT_ADOPTED_AT_ATTR,
    MEMORY_SNAPSHOT_ADOPTION_FIRST_REQUEST,
    MEMORY_SNAPSHOT_ADOPTION_REBASE,
    MEMORY_WRITE_HINT_HEADER,
    RETRIEVED_MEMORY_HINT_HEADER,
    adopt_memory_snapshot,
    is_memory_snapshot_message,
    memory_snapshot_provenance,
)
from g3ku.runtime.frontdoor.token_preflight_compaction import (
    FrontdoorTokenPreflightResult,
)
from g3ku.runtime.message_token_estimation import estimate_message_tokens
from g3ku.runtime.project_environment import current_project_environment
from g3ku.runtime.session_agent import (
    FRONTDOOR_LAST_SEND_AT_ATTR,
    PENDING_PROVIDER_BUNDLE_RECOMMIT_ATTR,
)
from g3ku.runtime.stage_prompt_compaction import (
    ECHO_STRIP_ENABLED,
    STAGE_ARCHIVE_HEADING,
    STAGE_CLOSURE_INACTIVE_NOTES,
    STAGE_RAW_PREFIXES,
    STAGE_REF_SELECTION_RULE,
    TOKEN_COMPACT_V2_PREFIX,
    build_stage_archive_document,
    closing_stage_target,
    compact_stage_prompt_messages_in_place,
    is_stage_block_echo_text,
    render_stage_ref_candidate_block,
    render_stage_ref_index,
    split_stage_ref_selection,
    stage_archive_selector_from_request_messages,
    stage_block_indexes,
    stage_created_at_ceiling,
    stage_created_at_within_watermark,
    stage_is_swallowable,
    stage_ledger_may_have_moved,
    stage_message_call_ids,
    stage_ref_candidates,
    stage_transition_signature,
    strip_stage_block_echo,
    summarized_stage_ids,
)
from g3ku.runtime.tool_context_presence import (
    contract_index_from_revisions,
    contract_presence_index,
    contract_presence_reading,
    loader_contract_revisions,
)
from g3ku.runtime.tool_error_guidance import availability_hint
from g3ku.runtime.tool_history import (
    align_compaction_keep_recent,
    extract_call_id,
    iter_compaction_atomic_groups,
    truncate_group_head_tail,
)
from g3ku.runtime.tool_impulse import (
    CONTRACT_DELIVERY_FIELD,
    EXECUTION_PATH_CORRECTED,
    EXECUTION_PATH_ILLEGAL_REUSE,
    EXECUTION_PATH_NORMAL,
    EXECUTION_PATH_REUSED,
    IMPULSE_REJECT,
    IMPULSE_STAGED,
    REUSE_FLAG_IGNORED_FIELD,
    REUSE_PLAN_CONTRACT_ABSENT,
    REUSE_PLAN_EXECUTE,
    REUSE_PLAN_EXPLICIT_ARGS,
    REUSE_PLAN_NO_STASH,
    STAGED_REPLAY_FIELD,
    aggregate_reuse_results,
    plan_reuse_execution,
    reuse_refusal_text,
    split_reuse_arguments,
    staged_replay_kind,
)
from g3ku.runtime.tool_visibility import CEO_FIXED_BUILTIN_TOOL_NAMES, CEO_NEVER_CONTRACT_TOOL_NAMES
from g3ku.runtime.web_ceo_sessions import (
    WEB_CEO_IMAGE_UPLOAD_MAX_BYTES,
    actual_request_dir_for_session,
    fold_internal_prompt_history,
    is_prompt_visible_message,
    persist_frontdoor_actual_request,
    strip_multimodal_blocks_from_message_records,
    trailing_turn_record,
)
from g3ku.utils.helpers import tools_bundle_single_source_enabled
from main.governance.tool_context import apply_runtime_tool_context_projection
from main.models import normalize_execution_policy_metadata
from main.protocol import build_envelope, now_iso
from main.runtime.chat_backend import (
    build_actual_request_diagnostics,
    build_prompt_cache_diagnostics,
    resolve_send_model_context_window_info,
)
from main.runtime.internal_tools import (
    SilentTool,
    SubmitNextStageTool,
)
from main.runtime.send_token_preflight import (
    build_runtime_estimated_input_truth,
    build_runtime_hybrid_send_token_estimate,
    build_runtime_observed_input_truth,
    build_runtime_send_token_preflight_snapshot,
    compute_runtime_send_token_preflight_thresholds,
)
from main.runtime.stage_budget import (
    FRONTDOOR_STAGELESS_MEMORY_TOOL_NAMES,
    SILENT_TOOL_NAME,
    STAGE_BUDGET_EXHAUSTED_FREE_PASS_REMINDER,
    STAGE_BUDGET_EXHAUSTION_PREDICTED_REMINDER_TEMPLATE,
    STAGE_TOOL_NAME,
    STAGE_TOOL_ROUND_BUDGET_MAX,
    STAGE_TOOL_ROUND_BUDGET_MIN,
    STAGELESS_FREE_PASS_REMINDER,
    response_tool_calls_count_against_stage_budget,
    stage_free_pass_kind,
    stage_gate_error_for_tool,
    visible_tools_for_stage_iteration,
)
from main.runtime.stage_messages import (
    STAGE_REPLY_BOUNCE_LIMIT,
    build_ceo_stage_overlay,
    build_ceo_stage_reply_bounce_message,
    build_ceo_stage_result_block_message,
    is_turn_only_system_note_message,
    strip_turn_only_system_note_messages,
)
from main.runtime.tool_call_repair import (
    XML_REPAIR_ATTEMPT_LIMIT,
    build_xml_tool_repair_message,
    extract_tool_calls_from_xml_pseudo_content,
    recover_tool_calls_from_json_payload,
)
from main.service.create_async_task_contract import (
    normalize_create_async_task_file_targets,
    normalize_create_async_task_seed_tools,
)

from . import skill_roster as _skill_roster
from ._ceo_support import CeoFrontDoorSupport
from .canonical_context import (
    combine_canonical_context,
    default_frontdoor_canonical_context,
    merge_turn_stage_state_into_canonical_context,
    normalize_frontdoor_canonical_context,
)
from .message_builder import CeoMessageBuilder
from .prompt_cache_contract import DEFAULT_CACHE_FAMILY_REVISION, build_frontdoor_prompt_contract
from .session_temp_dir import ceo_session_temp_dir
from .state_models import (
    CeoFrontdoorInterrupted,
    CeoPendingInterrupt,
    CeoPersistentState,
    CeoRuntime,
)
from .tool_contract import (
    apply_pinned_contract_to_head,
    build_frontdoor_tool_contract,
    bump_skill_roster_epoch,
    frontdoor_pinned_contract_text,
    is_frontdoor_tool_contract_echo_text,
    is_frontdoor_tool_contract_message,
    normalize_frontdoor_candidate_tool_items,
    pinned_contract_is_carried_by_head,
    pinned_skill_ids_for,
    skill_roster_epoch_for,
    strip_frontdoor_tool_contract_echo,
    upsert_frontdoor_tool_contract_message,
)

CeoGraphState = CeoPersistentState




_TASK_ID_PATTERN = re.compile(r"task:[A-Za-z0-9][\w:-]*")
# 旧文案静默哨兵的字面值。P4 之后它不再是任何判据，只作为"该被清洗掉的噪声"保留一份：
# 转录里仍存有历史轮次的这类尾巴，模型有模仿上下文的倾向，不剥就会当正文发给用户。
# 实盘该形态出现 11 次（占全部静默尝试的 11/11），所以这条清洗有真实对象。
LEGACY_SILENT_SENTINEL = "[G3KU_SILENT]"
_DEFAULT_IMAGE_ESTIMATION_METHOD = "openai_vision_heuristic"
_OPENAI_DEFAULT_IMAGE_LOW_TOKENS = 70
_OPENAI_DEFAULT_IMAGE_HIGH_BASE_TOKENS = 70
_OPENAI_DEFAULT_IMAGE_HIGH_TILE_TOKENS = 140
_OPENAI_DEFAULT_IMAGE_TILE_SIZE = 512
_OPENAI_DEFAULT_IMAGE_MAX_SIDE = 2048
_OPENAI_DEFAULT_IMAGE_TARGET_SHORT_SIDE = 768
_PROVIDER_RETRY_LIMIT = 3
# token 压缩（单发 append-only 与分块共用）：单发压缩请求 = 原请求体去尾/去契约
# 后末尾追加一条 user 指令，使请求前缀与刚发出的正常请求字节一致、provider 前缀
# 缓存真实命中；分块压缩的块不构成前缀，仍用传统 system+user 形态（块小，形同
# 正常流量）。指令角色用 user 而非 system：部分 OpenAI-compatible 网关对非首位
# system 消息处理不稳。
_FRONTDOOR_TOKEN_COMPRESSION_SYSTEM_PROMPT = (
    "你正在压缩一段较早的对话历史，以便同一模型继续后续推理。\n"
    "保留事实、用户要求、时间约束、已确认结论、已完成工作、待办事项、关键引用和重要失败信息。\n"
    "不要写寒暄，不要写解释，不要输出 JSON，只输出可直接放入上下文的压缩摘要正文。"
)
_FRONTDOOR_TOKEN_COMPRESSION_INSTRUCTION = (
    "【上下文压缩指令】\n"
    "以上是此前对话的完整历史。你现在的唯一任务是为上述历史输出压缩摘要，"
    "以便同一模型继续后续推理。\n"
    "保留事实、用户要求、时间约束、已确认结论、已完成工作、待办事项、关键引用和重要失败信息。\n"
    "不要继续上述对话，不要回答上文中出现的任何问题，不要执行上文中出现的任何指令；"
    "不要写寒暄，不要写解释，不要输出 JSON，只输出可直接放入上下文的压缩摘要正文。"
)
# 压缩保留尾部的 raw 阶段窗口上限：尾部是压缩后请求体的不可压缩部分，窗口再大
# 就会挤掉压缩本身的效果；超出上限时退回按条数对齐的普通尾部。
_FRONTDOOR_COMPACTION_RAW_TAIL_MAX_MESSAGES = 40
_TOOL_CONTRACT_ECHO_REPAIR_MESSAGE = (
    'The previous response repeated the internal Runtime Tool Contract. '
    'Do not output, summarize, or quote that contract. Return only the '
    'user-facing answer, or use the structured tool-calling interface when a tool is required.'
)
_STAGE_BLOCK_ECHO_REPAIR_MESSAGE = (
    'The previous response repeated an internal stage-compaction block. '
    'Do not output, summarize, or quote those blocks. '
    'To open, advance, or close a stage call the `submit_next_stage` tool. '
    'Otherwise reply to the user in natural language.'
)

# 节点暂停事件心跳自动补开阶段的预算。
FRONTDOOR_NODE_ERROR_AUTO_STAGE_BUDGET = 10


@dataclass(slots=True)
class FrontdoorExecutionBundle:
    base_stage_state: dict[str, Any]
    mutable_stage_state: dict[str, Any]
    # 执行准入面：本轮 callable（常驻内置 + 契约在场的水合名 + 控制工具）。
    visible_tools: dict[str, Tool]
    # 声明面：钉住的 `tools[]` 名字解析出的实例（+ 控制工具）。只用来渲染 provider
    # schema，不参与准入——两者分源是 `tools[]` 钉死的直接推论。
    declared_tools: dict[str, Tool]
    runtime_context: dict[str, Any]
    on_progress: Any


class FrontdoorCompressionRuntimeError(RuntimeError):
    def __init__(self, *, code: str, message: str, recoverable: bool = True) -> None:
        super().__init__(str(message or "").strip())
        self.code = str(code or "").strip() or "runtime_error"
        self.message = str(message or "").strip()
        self.recoverable = bool(recoverable)


def _estimate_frontdoor_provider_request_tokens(
    *,
    provider_request_body: dict[str, Any] | None,
    request_messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
) -> int:
    breakdown = _estimate_frontdoor_provider_request_token_breakdown(
        provider_request_body=provider_request_body,
        request_messages=request_messages,
        tool_schemas=tool_schemas,
    )
    return int(breakdown.get("estimated_total_tokens") or 0)


def _data_url_media_bytes(url: str) -> tuple[str, bytes]:
    text = str(url or "").strip()
    if not text.startswith("data:"):
        return "", b""
    header, sep, data = text.partition(",")
    if not sep:
        return "", b""
    mime_type = str(header[5:].split(";", 1)[0] or "").strip().lower()
    if ";base64" not in header.lower():
        return mime_type, b""
    try:
        return mime_type, base64.b64decode(data.encode("ascii"), validate=False)
    except (binascii.Error, ValueError):
        return mime_type, b""


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xFF\xD8":
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            return None
        segment_length = int.from_bytes(data[index:index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            return None
        if marker in {
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        } and index + 7 < len(data):
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            if width > 0 and height > 0:
                return width, height
            return None
        index += segment_length
    return None


def _image_dimensions_from_bytes(data: bytes, *, mime_type: str = "") -> tuple[int, int] | None:
    normalized_mime = str(mime_type or "").strip().lower()
    if normalized_mime == "image/png" and len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return (width, height) if width > 0 and height > 0 else None
    if normalized_mime == "image/gif" and len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
        return (width, height) if width > 0 and height > 0 else None
    if normalized_mime == "image/webp":
        return webp_dimensions(data)
    if normalized_mime == "image/bmp":
        return bmp_dimensions(data)
    if normalized_mime in {"image/jpeg", "image/jpg"} or data[:2] == b"\xFF\xD8":
        return _jpeg_dimensions(data)
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return (width, height) if width > 0 and height > 0 else None
    if len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
        return (width, height) if width > 0 and height > 0 else None
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return webp_dimensions(data)
    if len(data) >= 26 and data[:2] == b"BM":
        return bmp_dimensions(data)
    return _jpeg_dimensions(data)


def _image_dimensions_from_data_url(url: str) -> tuple[int, int] | None:
    mime_type, data = _data_url_media_bytes(url)
    if not data:
        return None
    return _image_dimensions_from_bytes(data, mime_type=mime_type)


def _normalize_openai_default_image_dimensions(width: int, height: int) -> tuple[int, int]:
    normalized_width = max(1, int(width or 1))
    normalized_height = max(1, int(height or 1))
    largest_side = max(normalized_width, normalized_height)
    if largest_side > _OPENAI_DEFAULT_IMAGE_MAX_SIDE:
        scale = float(_OPENAI_DEFAULT_IMAGE_MAX_SIDE) / float(largest_side)
        normalized_width = max(1, int(math.ceil(normalized_width * scale)))
        normalized_height = max(1, int(math.ceil(normalized_height * scale)))
    smallest_side = min(normalized_width, normalized_height)
    if smallest_side > _OPENAI_DEFAULT_IMAGE_TARGET_SHORT_SIDE:
        scale = float(_OPENAI_DEFAULT_IMAGE_TARGET_SHORT_SIDE) / float(smallest_side)
        normalized_width = max(1, int(math.ceil(normalized_width * scale)))
        normalized_height = max(1, int(math.ceil(normalized_height * scale)))
    return normalized_width, normalized_height


def _estimate_openai_default_image_tokens(
    *,
    width: int | None,
    height: int | None,
    detail: str | None,
) -> int:
    normalized_detail = str(detail or "auto").strip().lower()
    if normalized_detail == "low":
        return _OPENAI_DEFAULT_IMAGE_LOW_TOKENS
    resolved_width = max(1, int(width or _OPENAI_DEFAULT_IMAGE_TILE_SIZE))
    resolved_height = max(1, int(height or _OPENAI_DEFAULT_IMAGE_TILE_SIZE))
    final_width, final_height = _normalize_openai_default_image_dimensions(resolved_width, resolved_height)
    tiles_wide = max(1, int(math.ceil(float(final_width) / float(_OPENAI_DEFAULT_IMAGE_TILE_SIZE))))
    tiles_high = max(1, int(math.ceil(float(final_height) / float(_OPENAI_DEFAULT_IMAGE_TILE_SIZE))))
    return _OPENAI_DEFAULT_IMAGE_HIGH_BASE_TOKENS + (
        tiles_wide * tiles_high * _OPENAI_DEFAULT_IMAGE_HIGH_TILE_TOKENS
    )


def _image_block_payload(url: str, *, detail: str | None) -> dict[str, Any]:
    dimensions = _image_dimensions_from_data_url(url)
    width, height = dimensions if dimensions else (None, None)
    return {
        "width": int(width or 0),
        "height": int(height or 0),
        "detail": str(detail or "auto").strip().lower() or "auto",
        "estimated_tokens": _estimate_openai_default_image_tokens(
            width=width,
            height=height,
            detail=detail,
        ),
    }


def _extract_image_blocks(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            found.extend(_extract_image_blocks(item))
        return found
    if not isinstance(value, dict):
        return found
    item_type = str(value.get("type") or "").strip().lower()
    if item_type in {"image_url", "input_image"}:
        image_value = value.get("image_url")
        if isinstance(image_value, dict):
            image_url = image_value.get("url")
            detail = image_value.get("detail", value.get("detail"))
        else:
            image_url = image_value or value.get("url")
            detail = value.get("detail")
        if isinstance(image_url, str) and image_url:
            found.append(_image_block_payload(image_url, detail=detail))
        return found
    for item in value.values():
        found.extend(_extract_image_blocks(item))
    return found


def _payload_without_inline_images(value: Any) -> Any:
    if isinstance(value, list):
        return [_payload_without_inline_images(item) for item in value]
    if not isinstance(value, dict):
        return value
    item_type = str(value.get("type") or "").strip().lower()
    if item_type in {"image_url", "input_image"}:
        image_value = value.get("image_url")
        detail = (
            image_value.get("detail", value.get("detail"))
            if isinstance(image_value, dict)
            else value.get("detail")
        )
        return {
            "type": item_type,
            "image_estimation": "inline_image_omitted",
            **({"detail": str(detail or "").strip()} if str(detail or "").strip() else {}),
        }
    return {
        str(key): _payload_without_inline_images(item)
        for key, item in value.items()
    }


def _estimate_frontdoor_provider_request_token_breakdown(
    *,
    provider_request_body: dict[str, Any] | None,
    request_messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = dict(provider_request_body or {})
    if not payload:
        payload = {
            "input": list(request_messages),
            "tools": list(tool_schemas or []),
        }
    image_blocks = _extract_image_blocks(payload)
    estimated_image_tokens = sum(int(item.get("estimated_tokens") or 0) for item in image_blocks)
    tools_payload = [
        dict(item)
        for item in list(payload.get("tools") or tool_schemas or [])
        if isinstance(item, dict)
    ]
    estimated_tool_schema_tokens = (
        estimate_tokens(json.dumps(tools_payload, ensure_ascii=False, separators=(",", ":"), default=str))
        if tools_payload
        else 0
    )
    text_payload = dict(_payload_without_inline_images(payload) or {})
    text_payload.pop("tools", None)
    estimated_text_tokens = estimate_tokens(
        json.dumps(text_payload, ensure_ascii=False, separators=(",", ":"), default=str)
    )
    return {
        "estimated_total_tokens": int(estimated_text_tokens + estimated_tool_schema_tokens + estimated_image_tokens),
        "estimated_text_tokens": int(estimated_text_tokens),
        "estimated_tool_schema_tokens": int(estimated_tool_schema_tokens),
        "estimated_image_tokens": int(estimated_image_tokens),
        "image_count": len(image_blocks),
        "image_estimation_method": _DEFAULT_IMAGE_ESTIMATION_METHOD if image_blocks else "",
    }


def _hidden_internal_prompt_message_metadata(
    *,
    source: str,
    internal_prompt_kind: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": str(source or "").strip().lower(),
        "prompt_visible": True,
        "ui_visible": False,
        "internal_prompt_kind": str(internal_prompt_kind or "").strip(),
    }
    for key, value in dict(extra or {}).items():
        if value in (None, "", [], {}):
            continue
        payload[str(key)] = value
    return payload


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


_NO_RESUME = object()


def _merged_interrupt_values(state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    values = _checkpoint_safe_value(dict(state or {}))
    if not isinstance(values, dict):
        values = {}
    interrupt_state = _checkpoint_safe_value(dict(payload or {}))
    if not isinstance(interrupt_state, dict):
        interrupt_state = {}
    interrupt_approval_request = interrupt_state.get("approval_request")
    if not isinstance(values.get("approval_request"), dict):
        if isinstance(interrupt_approval_request, dict):
            values["approval_request"] = dict(interrupt_approval_request)
        else:
            values["approval_request"] = dict(interrupt_state)
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
    if isinstance(interrupt_state.get("frontdoor_selection_debug"), dict):
        values["frontdoor_selection_debug"] = dict(interrupt_state.get("frontdoor_selection_debug") or {})
    return values


def raise_frontdoor_approval_interrupt(*, state: dict[str, Any], payload: dict[str, Any]) -> Any:
    resume_state = _checkpoint_safe_value(dict(state or {}))
    values = _merged_interrupt_values(resume_state, payload)
    interrupt_id = "approval:" + str((payload or {}).get("batch_id") or uuid.uuid4().hex)
    raise CeoFrontdoorInterrupted(
        interrupts=[
            CeoPendingInterrupt(
                interrupt_id=interrupt_id,
                value=_checkpoint_safe_value(payload),
            )
        ],
        values=values,
        resume_state=resume_state,
    )


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


def _provider_visible_tool_contract(tool: Tool) -> tuple[str, dict[str, Any] | None]:
    model_description, model_parameters = _model_visible_tool_contract(tool)
    stripped_parameters = sanitize_provider_parameters_schema(model_parameters)
    return (
        str(model_description or "").strip(),
        stripped_parameters if isinstance(stripped_parameters, dict) else model_parameters,
    )


def _provider_tool_schema(tool: Tool) -> dict[str, Any]:
    model_description, model_parameters = _provider_visible_tool_contract(tool)
    return {
        "type": "function",
        "function": {
            "name": str(tool.name or "").strip(),
            "description": model_description,
            "parameters": dict(model_parameters or {}),
        },
    }


def _provider_tool_schemas(tools: dict[str, Tool]) -> list[dict[str, Any]]:
    return [_provider_tool_schema(tool) for tool in dict(tools or {}).values()]


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
    raw_targets = normalized.get("file_targets")
    if raw_targets is None:
        normalized["file_targets"] = []
    elif isinstance(raw_targets, str):
        stripped_targets = str(raw_targets).strip()
        parsed_targets: Any = None
        if stripped_targets.startswith("[") and stripped_targets.endswith("]"):
            try:
                parsed_targets = json.loads(stripped_targets)
            except Exception:
                parsed_targets = None
        if isinstance(parsed_targets, list):
            normalized["file_targets"] = parsed_targets
        else:
            normalized["file_targets"] = normalize_create_async_task_file_targets(stripped_targets)
    if "seed_tools" in normalized:
        normalized["seed_tools"] = normalize_create_async_task_seed_tools(normalized.get("seed_tools"))
    return normalized


class CeoFrontDoorRuntimeOps(CeoFrontDoorSupport):
    _ALLOWED_FRONTDOOR_SHRINK_REASONS = frozenset({"", "token_compression", "stage_compaction", "user_edit_truncation"})
    _TOKEN_COMPRESSION_TRIGGER_RATIO = 0.80
    _TOKEN_COMPRESSION_ESTIMATE_SAFETY_RATIO = 0.95
    _CONTENT_OPEN_IMAGE_CONTEXT_TEXT = "图片已通过 content_open 打开，视觉内容已附带在本轮上下文中"
    # LLM token 压缩会无条件保留最近 N 条 body 消息（recent_tail）。若某条尾部
    # 消息本身是超大工具结果（例如 content_open 带出单行巨型 artifact），压缩后
    # 的估算依然超窗，压缩检查必然抛错、回合必然失败，且基线永远带着这条消息，
    # 形成"每轮都压缩、每轮都失败"的死循环。这里把尾部消息内容硬性截断到
    # 该上限，保证压缩重建后的请求体有界、压缩总能收敛。
    _FRONTDOOR_COMPACTION_TAIL_CONTENT_CHAR_LIMIT = 16_000

    @classmethod
    def _bound_frontdoor_compaction_tail_messages(
        cls,
        messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """把压缩保留的尾部工具结果截断到字符上限，确保压缩结果必然收敛。"""
        bounded: list[dict[str, Any]] = []
        for item in list(messages or []):
            if not isinstance(item, dict):
                bounded.append(item)
                continue
            if str(item.get("role") or "").strip().lower() != "tool":
                bounded.append(dict(item))
                continue
            content = item.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            if len(text) <= cls._FRONTDOOR_COMPACTION_TAIL_CONTENT_CHAR_LIMIT:
                bounded.append(dict(item))
                continue
            updated = dict(item)
            updated["content"] = (
                text[: cls._FRONTDOOR_COMPACTION_TAIL_CONTENT_CHAR_LIMIT].rstrip()
                + (
                    "\n\n[内容已截断]：该工具结果共 "
                    f"{len(text)} 字符，超出压缩保留上限"
                    f"（{cls._FRONTDOOR_COMPACTION_TAIL_CONTENT_CHAR_LIMIT} 字符）。"
                    "原文仍存储在对应 artifact 中，请按上文其 ref 用 content_open/content_search 检索。"
                )
            )
            bounded.append(updated)
        return bounded

    def _frontdoor_runtime_config(self) -> Any:
        try:
            config, _revision, _changed = get_runtime_config(force=False)
        except Exception:
            config = None
        return config if config is not None else getattr(self._loop, "app_config", None)

    @staticmethod
    def _is_frontdoor_tool_contract_record(record: dict[str, Any] | None) -> bool:
        return is_frontdoor_tool_contract_message(dict(record or {}))

    @classmethod
    def _strip_frontdoor_turn_only_artifacts(
        cls,
        messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        return [
            dict(item)
            for item in list(messages or [])
            if isinstance(item, dict)
            and not cls._is_frontdoor_tool_contract_record(item)
            and not is_turn_only_system_note_message(item)
        ]

    @staticmethod
    def _is_frontdoor_dynamic_overlay_record(record: dict[str, Any] | None) -> bool:
        """长期记忆写入提示 / 已检索记忆使用提示等动态 overlay（按块头常量识别）。"""
        content = str((record or {}).get("content") or "").strip()
        return content.startswith(MEMORY_WRITE_HINT_HEADER) or content.startswith(RETRIEVED_MEMORY_HINT_HEADER)

    @classmethod
    def _strip_frontdoor_dynamic_overlays(
        cls,
        messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        return [
            dict(item)
            for item in list(messages or [])
            if isinstance(item, dict) and not cls._is_frontdoor_dynamic_overlay_record(item)
        ]

    @classmethod
    def _frontdoor_comparable_request_records(
        cls,
        messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """跨轮可比性投影：两侧消息必须走同一投影，前缀相等判定才不失真。

        = durable 基线同管（工具契约 / turn-only note / 长期记忆快照剥离、多模态块
        剥离、内部提示历史折叠）+ 动态 overlay 剥离。下一轮请求由 durable 基线
        重新拼装并重新注入动态块，而 artifact 里存的是上一轮真实请求原貌——只剥
        契约/回合产物会让多模态、折叠与 overlay 差异把 usage-first 估算静默打成
        全量 preview（turn 边界 comparable 失效，事故：2026-09-16 QQ 渠道误触发压缩）。
        """
        return cls._strip_frontdoor_dynamic_overlays(cls._durable_frontdoor_request_body_messages(messages))

    @classmethod
    def _frontdoor_adoption_projection_record_kept(cls, record: dict[str, Any] | None) -> bool:
        """scaffold 采纳探针的逐条投影判定（只整条丢弃、不改写内容，保持原始下标可反映射）。"""
        if not isinstance(record, dict):
            return False
        if cls._is_frontdoor_tool_contract_record(record):
            return False
        if is_turn_only_system_note_message(record):
            return False
        if cls._is_frontdoor_memory_snapshot_record(record):
            return False
        if cls._is_frontdoor_dynamic_overlay_record(record):
            return False
        return True

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
    def _durable_frontdoor_request_body_messages(
        cls,
        request_messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        durable = strip_multimodal_blocks_from_message_records(
            cls._strip_frontdoor_turn_only_artifacts(
                cls._request_body_messages_without_tool_contracts(request_messages)
            )
        )
        # 不变量：durable 基线永不含 frontdoor 运行时工具契约块。
        # 若混入（旧版本遗留/回归），直接丢弃，避免污染跨回合基线。
        durable = [
            item
            for item in durable
            if not cls._is_frontdoor_tool_contract_record(item)
        ]
        # 折叠重复的内部提示词（心跳规则/事件束）。续跑基线每个请求都从 request_messages
        # 提交（含失败回合，见 _persist_frontdoor_actual_request），且基线只存 {role,content}
        # 无 metadata；warm 路径不经 prompt_history_messages，故 1.2 的折叠够不到这里。在基线
        # chokepoint 按内容标记折叠，避免 provider 长期不可用、同一事件每轮重投时 bundle 线性堆积。
        return fold_internal_prompt_history(durable)

    @classmethod
    def _trim_frontdoor_seed_stage_compaction(
        cls,
        seed: list[dict[str, Any]] | None,
        stage_state: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """把续跑 seed（上一份请求体基线）按阶段归属原位压缩：
        过期阶段只移除工具肉身、压缩块落回原位、阶段外对话逐条保留，
        使续跑路径真正收缩且不整体打碎前缀缓存。
        返回 (裁剪结果, 本次是否实际移除了消息)。"""
        records = [dict(item) for item in list(seed or []) if isinstance(item, dict)]
        if not records or not list((stage_state or {}).get("stages") or []):
            return records, False
        parts = compact_stage_prompt_messages_in_place(
            records,
            stage_state=stage_state,
            preserve_leading_system=True,
            preserve_leading_user=True,
        )
        trimmed = [
            *list(parts["prefix"]),
            *list(parts["rewritten"]),
        ]
        return trimmed, bool(parts.get("stage_compaction_applied"))

    @classmethod
    def _reconcile_paused_user_turns_into_seed(
        cls,
        seed_messages: list[dict[str, Any]] | None,
        persisted_session: Any,
        current_turn_user_content: Any = None,
    ) -> list[dict[str, Any]]:
        """手动暂停发生在模型请求发出之前时，基线只在请求完成后回写，
        被暂停回合的用户消息会永远缺席续跑种子（种子路径又不读转录）。
        这里按转录对账：prompt-visible 的暂停用户回合若缺席种子，
        按转录顺序补到种子尾部，使下一轮上下文默认继承它们。
        与种子已有用户消息或当前回合用户消息同文本的不重复补。"""
        records = [dict(item) for item in list(seed_messages or []) if isinstance(item, dict)]
        transcript = (
            list(getattr(persisted_session, "messages", []) or [])
            if persisted_session is not None
            else []
        )
        if not records or not transcript:
            return records
        # 种子里的用户消息可能带投影追加的送达时间戳装饰，转录原文没有；
        # 去重比较前统一剥离，补发时用记录 timestamp 重新装饰，保持
        # "请求体里的用户消息都带时间锚点"的不变量。
        known_user_texts = {
            strip_arrival_time_stamp(cls._content_text(record.get("content")).strip())
            for record in records
            if str(record.get("role") or "").strip().lower() == "user"
        }
        known_user_texts.discard("")
        current_text = strip_arrival_time_stamp(cls._content_text(current_turn_user_content).strip())
        if current_text:
            known_user_texts.add(current_text)
        for message in transcript:
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "").strip().lower() != "user":
                continue
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            if str(metadata.get("_transcript_state") or "").strip().lower() != "paused":
                continue
            if not is_prompt_visible_message(message):
                continue
            text = strip_arrival_time_stamp(cls._content_text(message.get("content")).strip())
            if not text or text in known_user_texts:
                continue
            known_user_texts.add(text)
            stamp = render_arrival_stamp(message.get("timestamp"))
            records.append({"role": "user", "content": f"{text}{stamp}" if stamp else text})
        return records

    def _quarantine_frontdoor_shrink(
        self,
        session: Any,
        *,
        new_seed: list[dict[str, Any]],
        previous_tokens: int,
        next_tokens: int,
    ) -> None:
        """检测到「无理由收缩」时不裸抛冻结会话，而是把拒绝后的新种子
        以受控原因写回基线，使后续回合对比一致、会话可自愈。"""
        consecutive = int(getattr(session, "_frontdoor_shrink_quarantine_count", 0) or 0) + 1
        setattr(session, "_frontdoor_shrink_quarantine_count", consecutive)
        durable_seed = self._durable_frontdoor_request_body_messages(new_seed)
        setattr(session, "_frontdoor_request_body_messages", durable_seed)
        setattr(session, "_frontdoor_history_shrink_reason", "context_shrink_quarantine")
        logger.warning(
            "frontdoor shrink quarantined (self-heal): prev={} next={} consecutive={} session={}",
            previous_tokens,
            next_tokens,
            consecutive,
            getattr(getattr(session, "state", None), "session_key", ""),
        )

    def _ceo_image_multimodal_enabled_for_model_refs(self, model_refs: list[str] | None) -> bool:
        """整条链的保守 AND：**只服务显示与写后回显**，不再门装配层。

        判据本体在 `g3ku/config/model_manager.image_multimodal_chain_decision`，与
        `model_config` 工具的写后回显同源。装配层的"这一发能不能看图"用
        `_ceo_image_multimodal_for_ref`（按当前那颗判），两者混用会让已勾多模态的链首
        因为链尾没收图而看不到图。
        """
        from g3ku.config.model_manager import image_multimodal_chain_decision

        return image_multimodal_chain_decision(self._frontdoor_runtime_config(), model_refs)[0]

    def _ceo_image_multimodal_for_ref(self, model_ref: Any) -> tuple[bool, str]:
        """本轮这一发落到的那颗模型收不收图，以及它的展示名（降级文案要点名它）。"""
        from g3ku.config.model_manager import image_multimodal_target_decision

        return image_multimodal_target_decision(self._frontdoor_runtime_config(), model_ref)

    def _ceo_image_multimodal_for_request(self, model_refs: list[str] | None) -> tuple[bool, str]:
        """按链首判：链前进到别的成员时，咽喉点那侧还有同一判据兜底。"""
        refs = [str(item or "").strip() for item in list(model_refs or []) if str(item or "").strip()]
        return self._ceo_image_multimodal_for_ref(refs[0] if refs else "")

    def _degrade_image_content_for_request(self, content: Any, *, model_refs: list[str] | None) -> Any:
        """本轮这一发落到的那颗不收图时，把 content 里的图片块换成"路径/占位 + 原因"文本。

        放在前门定稿正文之处而不是只放咽喉点，是为了让 artifact 与 `image_count` 读到
        **降级后的同一份正文**：否则复盘会看见 `image_count=1` 而实际发出去的是文字。
        外部／MCP 附件那道只把图片编成 data: URL，落盘路径在同一条消息的附件说明行里，
        所以按顺序把那些路径配回来；配不上（条数不等）才留 `(inline)`。
        """
        from g3ku.runtime.image_capability import (
            degrade_image_blocks_in_messages,
            image_path_lookup_from_content,
        )

        if not isinstance(content, list):
            return content
        enabled, display_name = self._ceo_image_multimodal_for_request(model_refs)
        if enabled:
            return content
        degraded, degraded_count = degrade_image_blocks_in_messages(
            [{"role": "user", "content": content}],
            display_name=display_name,
            path_lookup=image_path_lookup_from_content(content),
        )
        if not degraded_count:
            return content
        return degraded[0].get("content", content)

    @staticmethod
    def _web_ceo_uploaded_files_note(uploads: list[dict[str, Any]]) -> str:
        if not uploads:
            return ""
        lines = ["Uploaded attachments:"]
        for item in uploads:
            kind = str(item.get("kind") or "").strip().lower()
            name = str(item.get("name") or item.get("path") or "").strip()
            path = str(item.get("path") or "").strip()
            if kind == "audio":
                # 语音条的内容已经在正文的转写里；留一行路径只会让模型以为还有个
                # 没读的附件要去打开（实测它为此跑过 content_describe/ffmpeg）。
                # 音频仍留在 metadata 里供气泡回放，只是不进模型可见面。
                continue
            if kind == "image":
                lines.append(f"- image: {name} (local path: {path})")
            else:
                lines.append(f"- file: {name} (local path: {path})")
        if len(lines) == 1:
            return ""
        lines.append("You may inspect the local file paths above when helpful.")
        return "\n".join(lines)

    @classmethod
    def _web_ceo_multimodal_image_note(
        cls,
        *,
        text: str,
        uploads: list[dict[str, Any]],
    ) -> str:
        image_uploads = [
            dict(item)
            for item in list(uploads or [])
            if str(item.get("kind") or "").strip().lower() == "image"
        ]
        file_names = [
            str(item.get("name") or item.get("path") or "").strip()
            for item in list(uploads or [])
            if str(item.get("kind") or "").strip().lower() != "image"
            and str(item.get("name") or item.get("path") or "").strip()
        ]
        if not image_uploads:
            return cls._web_ceo_user_text_with_upload_note(text=text, uploads=uploads)
        text_value = str(text or "").strip()
        image_label = "image" if len(image_uploads) == 1 else "images"
        lines = [
            (
                f"For this turn, the uploaded {image_label} "
                f"are attached directly in this request. Use direct visual reasoning on the attached {image_label} first."
                if len(image_uploads) > 1
                else "For this turn, the uploaded image is attached directly in this request. "
                "Use direct visual reasoning on the attached image first."
            )
        ]
        if file_names:
            lines.append("Other uploaded files in this turn: " + ", ".join(file_names) + ".")
        note = "\n".join(lines).strip()
        if text_value and note:
            return f"{text_value}\n\n{note}"
        return note or text_value

    @classmethod
    def _web_ceo_user_text_with_upload_note(
        cls,
        *,
        text: str,
        uploads: list[dict[str, Any]],
    ) -> str:
        text_value = str(text or "").strip()
        note = cls._web_ceo_uploaded_files_note(uploads)
        if text_value and note:
            return f"{text_value}\n\n{note}"
        return note or text_value

    @staticmethod
    def _web_ceo_image_data_url(path: Path, mime_type: str) -> str:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _frontdoor_image_upload_too_large_error(*, name: str, size_bytes: int) -> FrontdoorCompressionRuntimeError:
        return FrontdoorCompressionRuntimeError(
            code="web_ceo_image_too_large",
            message=(
                f"Image attachment {str(name or '').strip() or 'image'} exceeds the 5 MiB limit "
                f"({int(size_bytes or 0)} bytes)."
            ),
            recoverable=True,
        )

    @classmethod
    def _web_ceo_image_capability_note(
        cls,
        *,
        text: str,
        uploads: list[dict[str, Any]],
        display_name: str,
    ) -> str:
        """当前模型不收图时，给每张图留一行"绝对路径 + 为什么看不到"。"""
        from g3ku.runtime.image_capability import build_image_degradation_text

        lines = []
        for item in list(uploads or []):
            if str(item.get("kind") or "").strip().lower() != "image":
                continue
            raw_path = str(item.get("path") or item.get("name") or "").strip()
            if raw_path:
                try:
                    raw_path = str(Path(raw_path).expanduser().resolve())
                except Exception:
                    pass
            lines.append(build_image_degradation_text(path=raw_path, display_name=display_name))
        if not lines:
            return str(text or "")
        base = str(text or "").strip()
        return "\n".join([part for part in [base, *lines] if part])

    def _expand_web_ceo_uploads_for_current_request_content(
        self,
        *,
        content: Any,
        metadata: dict[str, Any] | None,
        model_refs: list[str] | None,
    ) -> Any:
        payload = dict(metadata or {})
        uploads = [
            dict(item)
            for item in list(payload.get("web_ceo_uploads") or [])
            if isinstance(item, dict)
        ]
        if not uploads:
            return content

        raw_text = payload.get("web_ceo_raw_text")
        text_value = str(raw_text) if isinstance(raw_text, str) else self._content_text(content)
        multimodal_enabled, image_model_name = self._ceo_image_multimodal_for_request(model_refs)
        has_image_uploads = any(
            str(item.get("kind") or "").strip().lower() == "image"
            for item in uploads
        )
        merged_text = (
            self._web_ceo_multimodal_image_note(text=text_value, uploads=uploads)
            if multimodal_enabled and has_image_uploads
            else self._web_ceo_user_text_with_upload_note(text=text_value, uploads=uploads)
        )
        if not multimodal_enabled:
            # 图不进请求，但也不能凭空消失：留绝对路径 + 点名当前模型配置，模型自己决定
            # 用 content_open 取回还是提示用户换配置。链不因为图前进到下一位。
            return self._web_ceo_image_capability_note(
                text=merged_text,
                uploads=uploads,
                display_name=image_model_name,
            )

        content_blocks: list[dict[str, Any]] = []
        if merged_text:
            content_blocks.append({"type": "text", "text": merged_text})
        for item in uploads:
            if str(item.get("kind") or "").strip().lower() != "image":
                continue
            path, mime_type, _display_name = self._checked_web_ceo_image_upload(item)
            content_blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self._web_ceo_image_data_url(path, mime_type)},
                }
            )
        if len(content_blocks) == 1 and content_blocks[0].get("type") == "text":
            return merged_text
        return content_blocks or merged_text

    def _checked_web_ceo_image_upload(self, item: dict[str, Any]) -> tuple[Path, str, str]:
        """上传图的两条硬校验（缺图 / 超 5 MiB）与展示身份，两条道共用同一个口径。"""
        path = Path(str(item.get("path") or "")).expanduser()
        if not path.exists() or not path.is_file():
            raise FrontdoorCompressionRuntimeError(
                code="web_ceo_image_missing",
                message=f"Uploaded image is missing: {str(item.get('name') or path)}",
                recoverable=True,
            )
        size_bytes = int(item.get("size") or 0) or int(path.stat().st_size or 0)
        if size_bytes > WEB_CEO_IMAGE_UPLOAD_MAX_BYTES:
            raise self._frontdoor_image_upload_too_large_error(
                name=str(item.get("name") or path.name),
                size_bytes=size_bytes,
            )
        mime_type = str(item.get("mime_type") or item.get("mimeType") or "image/png").strip() or "image/png"
        display_name = str(item.get("name") or path.name).strip() or path.name
        return path, mime_type, display_name

    @staticmethod
    def _request_content_block_list(content: Any) -> list[Any]:
        """Normalize user-message content into a block list (str -> one text
        block) so batch sibling contents can be merged block-wise."""
        if content is None:
            return []
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content.strip() else []
        if isinstance(content, list):
            blocks: list[Any] = []
            for item in content:
                if isinstance(item, str):
                    if item.strip():
                        blocks.append({"type": "text", "text": item})
                elif isinstance(item, dict):
                    blocks.append(item)
            return blocks
        text = str(content or "")
        return [{"type": "text", "text": text}] if text.strip() else []

    def _merge_prompt_batch_sibling_contents(
        self,
        *,
        session: Any,
        current_turn_id: str,
        current_content: Any,
        model_refs: list[str] | None,
    ) -> Any:
        """把 prompt_batch 批次内其它输入的内容块并入当前回合请求。

        prompt_batch 只以批次最后一条输入驱动回合，frontdoor 只能展开这一条
        输入的内容：用户连续发送的消息被排队并合并成一个批次时，较早输入的
        文本与图片会彻底缺席模型请求（助手只看到最后一条）。这里按时间顺序
        把同批次其它输入的内容块（各自按其元数据展开）并入当前回合内容，
        相同块去重。转录不受影响——合并只发生在请求构建期，完成回写时各行
        仍按各自原文落盘。中途追加的 follow-up 消息在 prepare 之后才会进入
        `_active_user_batch_inputs`（见 `_consume_session_follow_up_messages_before_call_model`），
        不会与本合并重叠。"""
        batch_inputs = list(getattr(session, "_active_user_batch_inputs", None) or [])
        if len(batch_inputs) < 2 or not current_turn_id:
            return current_content
        sibling_inputs = [
            item
            for item in batch_inputs
            if str(((getattr(item, "metadata", None) or {}).get("_transcript_turn_id")) or "").strip()
            not in ("", current_turn_id)
        ]
        if not sibling_inputs:
            return current_content
        merged_blocks: list[Any] = []
        for item in sibling_inputs:
            item_metadata = dict(getattr(item, "metadata", None) or {})
            sibling_content = self._model_content(getattr(item, "content", ""))
            try:
                expanded = self._expand_web_ceo_uploads_for_current_request_content(
                    content=sibling_content,
                    metadata=item_metadata,
                    model_refs=model_refs,
                )
            except FrontdoorCompressionRuntimeError:
                # 较早输入的附件过期/缺失（临时目录回收、进程重启）不得拖垮
                # 整批回合：该输入降级为原文并入（图片缺席）。当前回合输入在
                # 调用点单独展开，保持严格失败语义。
                expanded = sibling_content
            for block in self._request_content_block_list(expanded):
                if block not in merged_blocks:
                    merged_blocks.append(block)
        if not merged_blocks:
            return current_content
        for block in self._request_content_block_list(current_content):
            if block not in merged_blocks:
                merged_blocks.append(block)
        return merged_blocks

    @staticmethod
    def _message_content_has_multimodal_blocks(value: Any) -> bool:
        if not isinstance(value, list):
            return False
        for item in value:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip().lower()
            if item_type in {"image_url", "input_image", "file", "input_file"}:
                return True
        return False

    @staticmethod
    def _content_open_image_overlay_payload(raw_result: Any) -> dict[str, Any] | None:
        payload = raw_result if isinstance(raw_result, dict) else None
        if payload is None and isinstance(raw_result, str):
            text = str(raw_result or "").strip()
            if text.startswith("{"):
                try:
                    parsed = json.loads(text)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    payload = parsed
        if not isinstance(payload, dict):
            return None
        if payload.get("ok") is not True:
            return None
        if str(payload.get("content_kind") or "").strip().lower() != "image":
            return None
        if payload.get("multimodal_open_pending") is not True:
            return None
        target = payload.get("runtime_image_target")
        return dict(payload) if isinstance(target, dict) and str(target.get("path") or "").strip() else None

    def _content_open_image_payloads_from_tool_results(
        self,
        tool_results: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for item in list(tool_results or []):
            if not isinstance(item, dict):
                continue
            payload = self._content_open_image_overlay_payload(item.get("raw_result"))
            if not payload:
                continue
            target = dict(payload.get("runtime_image_target") or {})
            path = str(target.get("path") or "").strip()
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            payloads.append(payload)
        return payloads

    def _compress_image_bytes_within_limit(self, path: Path) -> tuple[bytes, str] | None:
        """Best-effort compress an image so its bytes fit under the upload limit.

        Returns ``(bytes, mime_type)`` on success, or ``None`` when the image cannot
        be brought under ``WEB_CEO_IMAGE_UPLOAD_MAX_BYTES`` (or Pillow is unavailable).
        """
        try:
            import io

            from PIL import Image
        except Exception:
            return None
        try:
            with Image.open(path) as opened:
                opened.load()
                image = opened
                if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                    image = image.convert("RGBA")
                    background = Image.new("RGB", image.size, (255, 255, 255))
                    background.paste(image, mask=image.split()[-1])
                    image = background
                else:
                    image = image.convert("RGB")
                for quality in (85, 70, 55, 40, 30):
                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG", quality=quality, optimize=True)
                    if buffer.tell() <= WEB_CEO_IMAGE_UPLOAD_MAX_BYTES:
                        return buffer.getvalue(), "image/jpeg"
                scaled = image
                for _ in range(8):
                    width, height = scaled.size
                    scaled = scaled.resize(
                        (max(1, int(width * 0.8)), max(1, int(height * 0.8))),
                        Image.LANCZOS,
                    )
                    buffer = io.BytesIO()
                    scaled.save(buffer, format="JPEG", quality=60, optimize=True)
                    if buffer.tell() <= WEB_CEO_IMAGE_UPLOAD_MAX_BYTES:
                        return buffer.getvalue(), "image/jpeg"
            return None
        except Exception:
            return None

    @staticmethod
    def _frontdoor_active_stage_goal(state: dict[str, Any] | None) -> str:
        """取当前活动阶段目标，供 content_open 图片叠加层锚定当前任务。

        仅当阶段状态里存在真正的活动阶段（status=active 或 active_stage_id 命中）
        时返回其目标；否则返回空串，叠加层退回通用文案，避免锚到无关历史阶段。
        """
        stage_state = state.get("frontdoor_stage_state") if isinstance(state, dict) else None
        if not isinstance(stage_state, dict):
            return ""
        stages = list(stage_state.get("stages") or [])
        active_stage_id = str(stage_state.get("active_stage_id") or "").strip()
        fallback_goal = ""
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            stage_goal = str(stage.get("stage_goal") or "").strip()
            if not stage_goal:
                continue
            stage_id = str(stage.get("stage_id") or "").strip()
            if active_stage_id and stage_id == active_stage_id:
                return stage_goal
            if str(stage.get("status") or "").strip().lower() == "active":
                fallback_goal = stage_goal
        return fallback_goal

    def _content_open_image_overlay_context_text(
        self,
        *,
        active_stage_goal: str = "",
        base_text: str = "",
    ) -> str:
        """叠加层文案：带多模态图片的末尾用户消息只允许追加在请求尾部。

        有活动阶段目标时附上目标锚点并提醒继续完成，避免长会话里的历史内部
        事件与图片指令竞争注意力；无活动阶段时保持原有通用文案。`base_text` 让
        载体自己声明身份（入站上传 vs content_open 重开），两种来源的说法不能互抄。
        """
        base = str(base_text or "").strip() or self._CONTENT_OPEN_IMAGE_CONTEXT_TEXT
        goal = str(active_stage_goal or "").strip()
        if not goal:
            return base
        if len(goal) > 300:
            goal = goal[:300].rstrip() + "…"
        return (
            f"{base}\n"
            f"当前阶段目标：{goal}\n"
            "请继续完成该目标。这些图片服务于当前任务，不要切换到历史对话或历史定时任务。"
        )

    @staticmethod
    def _frontdoor_tail_image_block_count(request_messages: list[dict[str, Any]] | None) -> int:
        """最后一条消息里有多少个 image 块——"这一发上游到底收不收得到像素"的客观读数。"""
        records = [item for item in list(request_messages or []) if isinstance(item, dict)]
        if not records:
            return 0
        content = records[-1].get("content")
        if not isinstance(content, list):
            return 0
        return sum(
            1
            for block in content
            if isinstance(block, dict) and str(block.get("type") or "").strip().lower() in {"image_url", "input_image"}
        )

    def _content_open_image_overlay_message_blocks(
        self,
        payloads: list[dict[str, Any]] | None,
        *,
        model_refs: list[str] | None,
        active_stage_goal: str = "",
    ) -> list[dict[str, Any]]:
        image_enabled, image_model_name = self._ceo_image_multimodal_for_request(model_refs)
        if not image_enabled:
            raise FrontdoorCompressionRuntimeError(
                code="content_open_image_requires_multimodal",
                message=(
                    f"当前模型配置名称是({image_model_name})没有勾选多模态能力因此无法打开图片；"
                    f"需要看图请把**本会话**的模型选择切到已勾选多模态的配置；不要为此改全局模型链。"
                ),
                recoverable=True,
            )
        carrier_text = ""
        for raw in list(payloads or []):
            if isinstance(raw, dict) and str(raw.get("overlay_text") or "").strip():
                carrier_text = str(raw.get("overlay_text") or "").strip()
                break
        overlay_text = self._content_open_image_overlay_context_text(
            active_stage_goal=active_stage_goal,
            base_text=carrier_text,
        )
        blocks: list[dict[str, Any]] = [{"type": "text", "text": overlay_text}]
        seen_paths: set[str] = set()
        for raw in list(payloads or []):
            payload = self._content_open_image_overlay_payload(raw)
            if not payload:
                continue
            target = dict(payload.get("runtime_image_target") or {})
            path = Path(str(target.get("path") or "")).expanduser()
            path_key = str(path).strip()
            if not path_key or path_key in seen_paths:
                continue
            display_name = str(target.get("display_name") or path.name)
            if not path.exists() or not path.is_file():
                seen_paths.add(path_key)
                blocks.append({"type": "text", "text": f"[图片 {display_name} 文件不存在，未能附带]"})
                continue
            seen_paths.add(path_key)
            mime_type = str(target.get("mime_type") or payload.get("mime_type") or "image/png").strip() or "image/png"
            size_bytes = int(path.stat().st_size or 0)
            if size_bytes > WEB_CEO_IMAGE_UPLOAD_MAX_BYTES:
                compressed = self._compress_image_bytes_within_limit(path)
                if compressed is None:
                    blocks.append(
                        {
                            "type": "text",
                            "text": f"[图片 {display_name} 超过 5 MiB 上限（{size_bytes} 字节）且无法压缩到上限以内，未能附带]",
                        }
                    )
                    continue
                compressed_bytes, mime_type = compressed
                encoded = base64.b64encode(compressed_bytes).decode("ascii")
                blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                    }
                )
                continue
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self._web_ceo_image_data_url(path, mime_type)},
                }
            )
        return blocks if len(blocks) > 1 else []

    def _append_content_open_image_overlay_to_live_request_messages(
        self,
        *,
        request_messages: list[dict[str, Any]] | None,
        payloads: list[dict[str, Any]] | None,
        model_refs: list[str] | None,
        active_stage_goal: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        records = [dict(item) for item in list(request_messages or []) if isinstance(item, dict)]
        blocks = self._content_open_image_overlay_message_blocks(
            payloads,
            model_refs=model_refs,
            active_stage_goal=active_stage_goal,
        )
        if not blocks:
            durable = strip_multimodal_blocks_from_message_records(records)
            return records, durable
        last_role = str(records[-1].get("role") or "").strip().lower() if records else ""
        if last_role == "user":
            # 末位是当轮契约/阶段闸门块时，直接把 content 换掉会让这一跳收不到活状态行
            # （denied_tools、活动阶段），契约在场即能力的口径就断了。节点道（react_loop
            # 同名载体）一直是折叠口径：原文字并进首个 text 块，两条道按同一形状发。
            existing_text = self._content_text(records[-1].get("content")).strip()
            if existing_text and isinstance(blocks[0], dict) and blocks[0].get("type") == "text":
                blocks = [
                    {
                        "type": "text",
                        "text": f"{existing_text}\n\n{str(blocks[0].get('text') or '').strip()}",
                    },
                    *list(blocks[1:]),
                ]
            live_records = self._replace_last_user_message_content(messages=records, content=blocks)
        else:
            live_records = [*records, {"role": "user", "content": list(blocks)}]
        durable_records = strip_multimodal_blocks_from_message_records(live_records)
        return live_records, durable_records

    def _prefer_live_user_payload_over_text_history(
        self,
        *,
        messages: list[dict[str, Any]] | None,
        live_user_content: Any,
    ) -> list[dict[str, Any]]:
        records = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        if not records or not self._message_content_has_multimodal_blocks(live_user_content):
            return records
        last = dict(records[-1])
        if str(last.get("role") or "").strip().lower() != "user":
            return records
        if self._message_content_has_multimodal_blocks(last.get("content")):
            return records
        stripped_live_records = strip_multimodal_blocks_from_message_records(
            [{"role": "user", "content": live_user_content}]
        )
        stripped_live_content = (
            stripped_live_records[0].get("content", "")
            if stripped_live_records
            else live_user_content
        )
        if self._content_text(last.get("content")).strip() != self._content_text(stripped_live_content).strip():
            return records
        records[-1] = {**last, "content": live_user_content}
        return records

    @staticmethod
    def _replace_last_user_message_content(
        *,
        messages: list[dict[str, Any]] | None,
        content: Any,
    ) -> list[dict[str, Any]]:
        records = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        if not records:
            return records
        last = dict(records[-1])
        if str(last.get("role") or "").strip().lower() != "user":
            return records
        records[-1] = {**last, "content": content}
        return records

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
        config = self._frontdoor_runtime_config()
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
            if sep and normalized_prefix == "responses":
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
        if provider_id != "responses":
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
            "model": resolved_model,
            "store": False,
            "stream": True,
            "input": input_items,
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
            # 与「压缩后仍装不下」同一条口径：只陈述缺的是什么，不指挥去动模型记录——
            # 这句在模型看得见的位置，泛指"改链配置"会被当成改全局链的许可。
            message=(
                f"当前模型{display_name}的模型记录没有最大上下文TOKEN，本跳没有可用压缩窗口"
                f"{detail_suffix}"
            ),
            recoverable=True,
        )

    def _frontdoor_context_window_exceeded_error(
        self,
        *,
        model_info: dict[str, Any] | None,
        detail: str = '',
    ) -> FrontdoorCompressionRuntimeError:
        display_name = self._frontdoor_model_display_name(model_info)
        # 只陈述事实，不指挥改配置：超窗的原因可能是"这颗窗口就这么大"，也可能是
        # "本轮没有可压的历史了"，两者都不是操作者该去动模型记录的理由。窗口与压缩
        # 读数在帧与逐跳明细里，处置看那里。
        message = f"上下文大小超出当前模型{display_name}：按这颗模型的窗口压缩后仍装不下"
        normalized_detail = str(detail or '').strip()
        if normalized_detail:
            # 拒因原样带上：规模超限、单组超容量、容量非正是三种不同处置。
            message = f"{message}（{normalized_detail}）"
        return FrontdoorCompressionRuntimeError(
            code="frontdoor_context_window_exceeded",
            message=message,
            recoverable=True,
        )

    def _frontdoor_observed_request_span_tokens(self, state: CeoGraphState | None) -> int:
        """会话本回合已发出的最大 (输入+输出) 跨度：分块容量的实测下界。"""
        highest = 0
        for item in list((state or {}).get('frontdoor_actual_request_history') or []):
            if not isinstance(item, dict):
                continue
            usage = item.get('usage')
            if not isinstance(usage, dict):
                continue
            try:
                span = int(usage.get('input_tokens') or 0) + int(usage.get('output_tokens') or 0)
            except (TypeError, ValueError):
                continue
            if span > highest:
                highest = span
        return highest

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
        runtime: CeoRuntime,
        tool_schemas: list[Any],
    ) -> dict[str, Any]:
        state_for_request = dict(state or {})
        # 新增可读 skill 的一次性提示：检测与投递只在这一处（每跳的发送准备）发生，
        # 并且和落账同一步——追加进本轮历史，请求体与 durable 基线都从它派生。
        # 其它装配点（state 刷新、回合起点装配）一律不读台账，否则先跑的把差集领走、
        # 落请求体那处永远拿空（旧机制就是这么失效的）。
        request_messages, delivered_message = self._apply_frontdoor_skill_roster_notice(
            list(state_for_request.get("messages") or []),
            session_key=str(state_for_request.get("session_key") or "").strip(),
            session=getattr(getattr(runtime, "context", None), "session", None),
        )
        state_for_request["messages"] = list(request_messages)
        if delivered_message is not None and state_for_request.get("frontdoor_live_request_messages"):
            # 这条车道另有带多模态块的活历史：只能追加，不能拿纯文本历史覆盖它，
            # 否则上传图片的那一段结构就在这次赋值里被抹掉。
            state_for_request["frontdoor_live_request_messages"] = [
                *list(state_for_request.get("frontdoor_live_request_messages") or []),
                dict(delivered_message),
            ]
        durable_request_messages = [dict(item) for item in list(request_messages or []) if isinstance(item, dict)]
        prompt_cache_key = str(state_for_request.get("prompt_cache_key") or "")
        prompt_cache_diagnostics = dict(state_for_request.get("prompt_cache_diagnostics") or {})
        actual_tool_schemas: list[dict[str, Any]] = []
        # 记录口径随工件一起落盘：存量记录是 `proxy`（预检另渲的那份），跨口径那一跳的哈希不可比。
        actual_tools_record_basis = "wire"
        if hasattr(self, "_frontdoor_prompt_contract"):
            try:
                # 方案 C：估算面与记录面用的就是调用方传进来的那一份**线上**数组。这里原来无条件
                # 覆盖成 `_selected_tool_schemas(...)` 的另渲结果，于是工件 `tool_schemas`、
                # `actual_tool_schema_hash`、`prompt_cache_key` 的输入与 token 估算一直在读一份比
                # 线上少名单尾部两颗合成控制工具的数组——诊断读到的是代理，不是发送体。
                # cache key 本身对 `tool_schemas` 不敏感（`chat_backend.build_stable_prompt_cache_key`
                # 丢弃该参数），这条不敏感由 `test_prompt_cache_key_is_insensitive_to_tool_schemas` 钉住：
                # 它哪天红了，就说明有人让 key 变成 tool-aware，那时再拆记录用形参。
                tool_schemas = [dict(item) for item in list(tool_schemas or []) if isinstance(item, dict)]
                actual_tool_schemas = list(tool_schemas)
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
        durable_request_messages = [dict(item) for item in list(request_messages or []) if isinstance(item, dict)]
        pending_content_open_image_payloads = [
            dict(item)
            for item in list(state_for_request.get("pending_content_open_image_payloads") or [])
            if isinstance(item, dict)
        ]
        if pending_content_open_image_payloads:
            request_messages, durable_request_messages = self._append_content_open_image_overlay_to_live_request_messages(
                request_messages=request_messages,
                payloads=pending_content_open_image_payloads,
                model_refs=list(state_for_request.get("model_refs") or []),
                active_stage_goal=self._frontdoor_active_stage_goal(state_for_request),
            )
        model_info = self._resolve_frontdoor_send_model_context_window(
            model_refs=list(state_for_request.get("model_refs") or []),
        )
        provider_request_body = self._build_frontdoor_provider_request_body_preview(
            request_messages=request_messages,
            tool_schemas=actual_tool_schemas,
            model_info=model_info,
            prompt_cache_key=prompt_cache_key,
            parallel_tool_calls=(bool(state_for_request.get("parallel_enabled")) if list(tool_schemas or []) else None),
        )
        context_window_tokens = int(model_info.get("context_window_tokens") or 0)
        preview_estimate_tokens = self._estimate_frontdoor_send_total_tokens(
            provider_request_body=provider_request_body,
            request_messages=request_messages,
            tool_schemas=actual_tool_schemas,
        )
        estimate_breakdown = _estimate_frontdoor_provider_request_token_breakdown(
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
        anchor_projection_shrink_tokens = 0
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
            (
                delta_estimate_tokens,
                comparable_to_previous_request,
                anchor_projection_shrink_tokens,
            ) = self._frontdoor_append_only_delta_estimate_tokens(
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
                previous_tools_record_basis=str(latest_record.get("tools_record_basis") or "proxy").strip(),
                current_tools_record_basis=str(actual_tools_record_basis).strip(),
                stage_state=dict(state_for_request.get("frontdoor_stage_state") or {}),
            )
        # 锚点取的是上一跳的真实 usage，其中含这一跳已被阶段裁撤掉的工具肉身；
        # 不扣回就直接把裁撤前的规模当读数挂到下一跳，且压缩触发用的是同一个数。
        previous_effective_input_tokens = max(
            0,
            previous_effective_input_tokens - anchor_projection_shrink_tokens,
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
            "durable_request_messages": list(durable_request_messages),
            "tool_schemas": list(actual_tool_schemas),
            "provider_request_body": provider_request_body,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_diagnostics": dict(prompt_cache_diagnostics or {}),
            "model_info": dict(model_info or {}),
            "provider_model": provider_model,
            "resolved_model_key": str(model_info.get("model_key") or "").strip(),
            "context_window_tokens": context_window_tokens,
            "estimated_total_tokens": int(snapshot.estimated_total_tokens or 0),
            "preview_estimate_tokens": int(hybrid_estimate.preview_estimate_tokens or 0),
            "usage_based_estimate_tokens": int(hybrid_estimate.usage_based_estimate_tokens or 0),
            "delta_estimate_tokens": int(hybrid_estimate.delta_estimate_tokens or 0),
            "effective_input_tokens": int(previous_effective_input_tokens or 0),
            "anchor_projection_shrink_tokens": int(anchor_projection_shrink_tokens or 0),
            "estimate_source": str(hybrid_estimate.estimate_source or "preview_estimate"),
            "comparable_to_previous_request": bool(hybrid_estimate.comparable_to_previous_request),
            "final_estimate_tokens": int(hybrid_estimate.final_estimate_tokens or 0),
            "trigger_tokens": trigger_tokens,
            "effective_trigger_tokens": effective_trigger_tokens,
            "missing_context_window": missing_context_window,
            "would_exceed_context_window": bool(snapshot.would_exceed_context_window),
            "would_trigger_token_compression": bool(snapshot.would_trigger_token_compression),
            "ratio": float(snapshot.ratio or 0.0),
            "estimated_text_tokens": int(estimate_breakdown.get("estimated_text_tokens") or 0),
            "estimated_tool_schema_tokens": int(estimate_breakdown.get("estimated_tool_schema_tokens") or 0),
            "estimated_image_tokens": int(estimate_breakdown.get("estimated_image_tokens") or 0),
            "image_count": int(estimate_breakdown.get("image_count") or 0),
            # 位置读数：图块总数 vs 落在**最后一条消息**里的数量。只读末条的上游（实盘 hy4-preview）
            # 只有 at_tail 那部分真进视觉通道，光看 image_count 会把"发了图"读成"看见了图"。
            "image_blocks_at_tail": self._frontdoor_tail_image_block_count(request_messages),
            "image_estimation_method": str(estimate_breakdown.get("image_estimation_method") or ""),
        }

    async def _emit_frontdoor_runtime_snapshot(
        self,
        *,
        runtime: CeoRuntime,
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

    @staticmethod
    def _frontdoor_compaction_tail_count(body: list[dict[str, Any]]) -> int:
        """压缩保留尾部 = 最近 4 条（工具调用组对齐由调用方做）。

        这里曾额外覆盖"最近 3 个完成阶段 + 活动阶段"的 raw 窗口——收口把 compact 块
        摘掉之后，那是压缩后上下文里唯一还带工具正文的层，被摘要一起吞掉就再无第三层
        可退。窗口已随"阶段压缩只由模型点名"一起移除：未被点名的阶段一直留在正文里，
        真被压缩吞掉时按收口记账（`context_visible: false`）不再回渲，所以尾部只需要
        保住续写位本身，不再由账本反推。"""
        return min(len(body), 4)

    @staticmethod
    def _frontdoor_render_stage_state(*, session: Any, state: dict[str, Any] | None) -> dict[str, Any]:
        """界面阶段轨道读的那本账：本轮/会话的 `frontdoor_stage_state`，不掺 canonical 编号。

        取值顺序必须与 `_frontdoor_durable_stage_state` 里的 `turn_state` 完全一致，否则
        "压缩时看到的账"与"渲染出来的账"又不是同一本。
        """
        if isinstance(state, dict) and isinstance(state.get("frontdoor_stage_state"), dict):
            return dict(state.get("frontdoor_stage_state") or {})
        if session is not None and isinstance(getattr(session, "_frontdoor_stage_state", None), dict):
            return dict(getattr(session, "_frontdoor_stage_state", None) or {})
        return {}

    @staticmethod
    def _frontdoor_durable_stage_state(*, session: Any, state: dict[str, Any] | None) -> dict[str, Any]:
        """压缩侧读的账本：durable canonical 链 + 本轮 stage_state 的合并视图。"""
        canonical = getattr(session, "_frontdoor_canonical_context", None) if session is not None else None
        turn_state: dict[str, Any] = {}
        if isinstance(state, dict) and isinstance(state.get("frontdoor_stage_state"), dict):
            turn_state = dict(state.get("frontdoor_stage_state") or {})
        elif session is not None and isinstance(getattr(session, "_frontdoor_stage_state", None), dict):
            turn_state = dict(getattr(session, "_frontdoor_stage_state", None) or {})
        if not isinstance(canonical, dict) and not turn_state:
            return {}
        return combine_canonical_context(canonical or {}, turn_state)

    def _frontdoor_write_stage_archive_file(
        self,
        *,
        session_key: Any,
        stages: list[dict[str, Any]],
        kind: str = "frontdoor_stage_archive",
    ) -> tuple[str, int, int]:
        """把即将收口或被点名裁撤的阶段账本原样导出到会话临时目录，返回 (路径, stage 起, stage 止)。

        收口只让阶段不再进 provider 上下文；全量真相源必须仍可回读。落点选
        session_temp_dir 是因为它已在运行时工具契约里公示，模型 `content_open` 本来就
        能开该目录下的文件，不需要新增工具面。写失败返回空路径——摘要里就不出现指针，
        不阻塞压缩。`kind` 区分两条车道：收口归档与裁撤归档的成因不同，打开文件的人
        与维护者都要能一眼看出是哪条。"""
        records = [dict(item) for item in list(stages or []) if isinstance(item, dict)]
        if not records:
            return "", 0, 0
        indexes = [int(item.get("stage_index") or 0) for item in records if int(item.get("stage_index") or 0) > 0]
        try:
            directory = Path(self._ceo_session_temp_dir(session_key))
            directory.mkdir(parents=True, exist_ok=True)
            payload = build_stage_archive_document(
                kind=kind,
                owner=str(session_key or "").strip(),
                created_at=now_iso(),
                stages=records,
            )
            path = directory / f"g3ku_stage_archive_{len(records)}_{uuid.uuid4().hex[:8]}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return str(path), min(indexes or [0]), max(indexes or [0])
        except Exception:
            logger.debug("frontdoor stage archive export failed for {}", str(session_key or ""))
            return "", 0, 0

    def _frontdoor_archive_evicted_stage(
        self,
        *,
        session_key: str,
        stage_state: dict[str, Any],
        stage_id: str,
    ) -> str:
        """模型点名裁撤时把该阶段全量账本导成文件，路径写回账本的 archive_ref。

        读回靠 `content_open`（三角色可见、能开绝对路径），不新增工具面：工具 schema
        由 `_selected_tool_schemas` 从**全局** `loop.tools` 注册表解析，而阶段账本是
        **每会话**的，注册一个全局可执行实例会把 A 会话的账本暴露给 B 会话。
        一条阶段只在被裁撤时导一次，ref 落在账本上后续逐轮复用，不重复写盘。
        写失败就留空：块里不出现指针，也不谎称可读回。
        """
        wanted = str(stage_id or "").strip()
        if not wanted:
            return ""
        target = next(
            (
                stage
                for stage in list(stage_state.get("stages") or [])
                if isinstance(stage, dict) and str(stage.get("stage_id") or "").strip() == wanted
            ),
            None,
        )
        if not isinstance(target, dict) or target.get("context_evicted") is not True:
            return ""
        existing = str(target.get("archive_ref") or "").strip()
        if existing:
            return existing
        path, archive_start, archive_end = self._frontdoor_write_stage_archive_file(
            session_key=session_key,
            stages=[target],
            kind="frontdoor_stage_eviction",
        )
        if not path:
            return ""
        target["archive_ref"] = path
        index = int(target.get("stage_index") or 0)
        target["archive_stage_index_start"] = archive_start or index
        target["archive_stage_index_end"] = archive_end or index
        return path

    def _frontdoor_summarized_stage_ids(
        self,
        *,
        stage_state: dict[str, Any],
        recent_tail: list[dict[str, Any]],
    ) -> list[str]:
        """本次压缩真正吞掉的阶段（判定规则与节点车道共用 `summarized_stage_ids`）。"""
        return summarized_stage_ids(stage_state, body_messages=recent_tail)

    @classmethod
    def _frontdoor_compression_boundary_index(
        cls,
        render_stage_state: Any,
        durable_stage_state: Any,
        stage_ids: list[str],
    ) -> int:
        """折进摘要的最后一个阶段在**渲染账本**里的 `stage_index`；0 = 一条阶段都没折。

        区分线在阶段轨道里的落点由它决定：`stage_index` 小于等于它的阶段画在线上方。
        活动阶段永远不被吞（`stage_is_swallowable` 直接排除 `active_stage_id`），所以它
        连同之后的阶段一定留在线下方继续长——线因此永远落在两张阶段卡之间，不切开任何
        一张卡。

        边界必须换算到 `_frontdoor_render_stage_state` 那本账：被吞清单出自 canonical
        合并视图，那侧编号是全局单调的（实测一条会话里 canonical 17740..18445 对界面
        1..706，交集 0）。原样把 canonical 编号交给前端，判据会读成"这一回合的阶段全部
        在边界之下"，线就退回 feed 尾部。跨账靠 `_frontdoor_stage_content_identity`，
        与收口标记 `_frontdoor_mark_stages_archived` 用的是同一座桥。
        """
        wanted = {str(item or "").strip() for item in list(stage_ids or []) if str(item or "").strip()}
        if not wanted:
            return 0
        identities = {
            cls._frontdoor_stage_content_identity(stage)
            for stage in list((durable_stage_state or {}).get("stages") or [])
            if isinstance(stage, dict) and str(stage.get("stage_id") or "").strip() in wanted
        }
        identities.discard("")
        boundary = 0
        for stage in list((render_stage_state or {}).get("stages") or []):
            if not isinstance(stage, dict):
                continue
            if str(stage.get("stage_id") or "").strip() not in wanted:
                if cls._frontdoor_stage_content_identity(stage) not in identities:
                    continue
            try:
                boundary = max(boundary, int(stage.get("stage_index") or 0))
            except (TypeError, ValueError):
                continue
        return boundary

    @staticmethod
    def _frontdoor_stage_content_identity(stage: Any) -> str:
        """跨存储识别同一条逻辑阶段：canonical 链与本轮 stage_state 用的是两套
        stage_id 序号（实测同一条会话里 1..382 对 843..1226，交集 0），只按 id 匹配
        必然漏掉一半。字段口径对齐 `canonical_context._completed_stage_content_identity`。"""
        if not isinstance(stage, dict):
            return ""
        if str(stage.get("status") or "").strip().lower() == "active":
            return ""
        created_at = str(stage.get("created_at") or "").strip()
        if not created_at:
            return ""
        return "|".join(
            (
                str(stage.get("stage_kind") or "normal"),
                created_at,
                str(stage.get("finished_at") or "").strip(),
                str(stage.get("stage_goal") or "").strip(),
                str(stage.get("completed_stage_summary") or "").strip(),
            )
        )

    @classmethod
    def _frontdoor_mark_stages_archived(cls, stores: list[Any], selector: dict[str, Any], *, body_messages: Any) -> int:
        """把收口标记就地写进给定的账本结构，返回实际新标记的条数（幂等）。

        清单口径（存量压缩块的 `stage_ids`）走内容身份匹配：两份账本各维护一套 stage_id
        序号（实测交集 0），只标 canonical 等于没标（合并去重留下的是本轮 stage_state
        那份）；水位线直接命中，created_at 是同一条逻辑阶段在两边共享的同一个值。写入
        刻意不过 `normalize_frontdoor_canonical_context`，归一化会重排账本。活动阶段、
        已收口的、非普通阶段一律不收口；水位线口径额外保住肉身或 raw 块还在即将落定的
        请求体里的阶段——那批内容没进摘要，收了就是挖洞（未被模型点名的阶段一直留在正文
        里，所以这条守卫就是全部近场保护，没有额外的条数窗口）。"""
        wanted = {
            str(item or "").strip()
            for item in list((selector or {}).get("stage_ids") or [])
            if str(item or "").strip()
        }
        watermark = str((selector or {}).get("archived_through_created_at") or "").strip()
        if not wanted and not watermark:
            return 0
        usable = [store for store in list(stores or []) if isinstance(store, dict)]
        if not usable:
            return 0
        identities: set[str] = set()
        for store in usable:
            for stage in list(store.get("stages") or []):
                if not isinstance(stage, dict):
                    continue
                if str(stage.get("stage_id") or "").strip() in wanted:
                    identity = cls._frontdoor_stage_content_identity(stage)
                    if identity:
                        identities.add(identity)
        body_raw_stage_indexes = stage_block_indexes(body_messages, prefixes=STAGE_RAW_PREFIXES)
        body_call_ids = stage_message_call_ids(body_messages)
        hidden = 0
        for store in usable:
            active_stage_id = str(store.get("active_stage_id") or "").strip()
            for stage in list(store.get("stages") or []):
                if not isinstance(stage, dict):
                    continue
                if str(stage.get("status") or "").strip().lower() == "active":
                    continue
                if stage.get("context_visible") is False:
                    continue
                stage_id = str(stage.get("stage_id") or "").strip()
                matched = stage_id in wanted or (
                    bool(identities) and cls._frontdoor_stage_content_identity(stage) in identities
                )
                if not matched:
                    matched = bool(watermark) and stage_created_at_within_watermark(
                        stage.get("created_at"), watermark
                    )
                if not matched:
                    continue
                if watermark and not stage_is_swallowable(
                    stage,
                    active_stage_id=active_stage_id,
                    body_stage_indexes=body_raw_stage_indexes,
                    body_call_ids=body_call_ids,
                ):
                    continue
                stage["context_visible"] = False
                hidden += 1
        return hidden

    @classmethod
    def _frontdoor_apply_stage_archive(cls, result: dict[str, Any], request_messages: Any) -> int:
        """账本提交点应用收口：标记必须跟着"即将成为 durable"的那份账本副本一起产出。

        会话属性不是账本的权威写入者——`_graph_finalize_turn` 用轮初 state 快照重建
        canonical 再回灌，只标会话属性会在同一回合收尾时被整体覆盖（实盘表现为
        标记数归零、阶段块照旧逐轮渲染）。所以这里对 finalize 产出的两份 result
        账本动手，读取的请求体也正是同一份即将落定的基线请求体。"""
        if not isinstance(result, dict):
            return 0
        selector = stage_archive_selector_from_request_messages(request_messages)
        return cls._frontdoor_mark_stages_archived(
            [result.get("frontdoor_canonical_context"), result.get("frontdoor_stage_state")],
            selector,
            body_messages=request_messages,
        )

    @classmethod
    def _frontdoor_hide_summarized_stages(cls, session: Any, request_messages: Any) -> int:
        """会话属性版的收口标记（手动压缩车道：没有 finalize 回合，持久化点即提交点）。"""
        if session is None:
            return 0
        selector = stage_archive_selector_from_request_messages(request_messages)
        return cls._frontdoor_mark_stages_archived(
            [
                getattr(session, "_frontdoor_canonical_context", None),
                getattr(session, "_frontdoor_stage_state", None),
            ],
            selector,
            body_messages=request_messages,
        )

    @staticmethod
    def _estimate_frontdoor_compression_body_tokens(messages: list[dict[str, Any]] | None) -> int:
        """压缩侧的正文尺子：装箱用量、单发请求体用量必须走同一个估算器。

        两处 estimator 不同口径时，"装得下"的判据会随消息形状漂，边界会话随机切块。
        """
        try:
            return int(
                _estimate_frontdoor_provider_request_tokens(
                    provider_request_body=None,
                    request_messages=list(messages or []),
                    tool_schemas=[],
                )
                or 0
            )
        except Exception:
            return 0

    def _frontdoor_plan_token_compression_pass(
        self,
        *,
        grouped_history: list[tuple[list[dict[str, Any]], int]],
        single_shot_request_tokens: int,
        context_window_tokens: int,
        observed_span_tokens: int,
    ) -> CompressionPass:
        """完整压缩 / 分块压缩只由这一处判定：块数 1 就是完整压缩，>1 才切块。

        信封取**实测**的单发请求体非历史部分（system 前缀 + 指令尾），不用合成的
        "系统提示 + 2k"：append-only 压缩请求把整段前缀真的发了一遍，把它记成 2k 会
        让判据在窗口边缘偏乐观，正好造出一发发不出去的请求。
        """
        counts = [int(tokens or 0) for _group, tokens in list(grouped_history or [])]
        history_tokens = sum(counts)
        envelope_tokens = max(0, int(single_shot_request_tokens or 0) - history_tokens)
        return plan_compression_pass(
            counts,
            estimated_total_tokens=int(single_shot_request_tokens or 0),
            trigger_tokens=0,
            context_window_tokens=int(context_window_tokens or 0),
            envelope_tokens=envelope_tokens,
            observed_span_tokens=int(observed_span_tokens or 0),
            single_shot_tokens=int(single_shot_request_tokens or 0),
        )

    async def _run_frontdoor_llm_token_compression(
        self,
        *,
        state: CeoGraphState,
        runtime: CeoRuntime,
        request_messages: list[dict[str, Any]],
        model_refs: list[str],
        tool_schemas: list[dict[str, Any]],
        compression_source: str = "token_compression",
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
        session = getattr(getattr(runtime, "context", None), "session", None)
        durable_stage_state = self._frontdoor_durable_stage_state(session=session, state=state)
        recent_tail_count = self._frontdoor_compaction_tail_count(normalized_body)
        # 尾部边界不得落在工具调用组中间（与节点通道同一不变量）：尾部首条是
        # tool 结果时向前扩展边界，把声明它的 assistant 消息一并保留；最坏
        # 退化为整 body 尾部，落入下方无可压缩历史分支。
        recent_tail_count = align_compaction_keep_recent(normalized_body, recent_tail_count)
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
        # 静默痕迹先摘出待压缩区间，压缩完成后原样回插：这条行的全部意义在于
        # "后续轮次看得见上次选了沉默"，被摘要吞掉等于这条判据从未存在过。
        older_history_messages, preserved_silent_groups = self._lift_silent_trace_groups(
            older_history_messages
        )
        # 防御：可压缩历史末尾若残留「assistant 声明工具调用但结果缺失」的悬空组
        # （正常对齐后不应出现，续跑种子/异常状态可能带入），从压缩请求里丢弃，
        # 避免 provider 拒绝请求；计数仅入诊断。
        older_history_messages, dropped_dangling_tool_groups = self._drop_dangling_trailing_tool_call_groups(
            older_history_messages
        )
        # 尾部消息是压缩后请求体的最小不可压缩部分；超大工具结果必须先截断，
        # 否则压缩结果不收敛：压缩检查（final_request_tokens > 窗口）必然失败。
        recent_tail = self._bound_frontdoor_compaction_tail_messages(recent_tail)
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

        try:
            if not older_history_messages:
                await self._emit_frontdoor_runtime_snapshot(
                    runtime=runtime,
                    state={**dict(state or {}), "compression_state": self._default_compression_state()},
                )
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
            context_window_tokens = int(model_info.get("context_window_tokens") or 0)
            # 收口候选 = 本次真正会被摘要吞掉的阶段；它们的 key_refs 带编号交给摘要
            # 模型挑选，正文由结果侧逐字回填。候选清单并进指令消息本体，历史前缀
            # （system + older_history）保持与刚发出的正常请求字节一致。
            summarized_stage_ids = self._frontdoor_summarized_stage_ids(
                stage_state=durable_stage_state,
                recent_tail=recent_tail,
            )
            ref_candidates = stage_ref_candidates(durable_stage_state, stage_ids=set(summarized_stage_ids))
            instruction_text = _FRONTDOOR_TOKEN_COMPRESSION_INSTRUCTION
            candidate_block = render_stage_ref_candidate_block(ref_candidates)
            if candidate_block:
                instruction_text = (
                    f"{_FRONTDOOR_TOKEN_COMPRESSION_INSTRUCTION}\n"
                    f"{STAGE_REF_SELECTION_RULE}\n\n{candidate_block}"
                )
            # append-only 单发压缩请求：原请求体（去尾/去契约）+ 末尾一条 user 指令。
            # 前缀与刚发出的正常请求字节一致，provider 前缀缓存真实命中，压缩请求
            # 的新增 token 只有指令本身——与正常流量同形，快速成功或快速 429 重试，
            # 不再出现整段历史 JSON 重打包导致的缓存全失效 + 巨包静默挂起。
            single_shot_messages = [
                *system_prefix,
                *older_history_messages,
                {"role": "user", "content": instruction_text},
            ]
            single_shot_tokens = self._estimate_frontdoor_send_total_tokens(
                provider_request_body=self._build_frontdoor_provider_request_body_preview(
                    request_messages=single_shot_messages,
                    tool_schemas=[],
                    model_info=model_info,
                    prompt_cache_key="",
                    parallel_tool_calls=None,
                ),
                request_messages=single_shot_messages,
                tool_schemas=[],
            )
            grouped_history = [
                (list(group), self._estimate_frontdoor_compression_body_tokens(group))
                for group in iter_compaction_atomic_groups(older_history_messages)
            ]
            compression_pass = self._frontdoor_plan_token_compression_pass(
                grouped_history=grouped_history,
                single_shot_request_tokens=self._estimate_frontdoor_compression_body_tokens(single_shot_messages),
                context_window_tokens=context_window_tokens,
                observed_span_tokens=self._frontdoor_observed_request_span_tokens(state),
            )
            if compression_pass.refusal is not None:
                # 窗口装不下任何一块压缩请求的信封：这一 pass 放弃，原请求体照发。
                # 这不是"超窗错误"——压缩本来就只是让请求体变小的一条道，压不动时把
                # 决定权交回正常发送路径，由它按 provider 的实际回执处理。
                await self._emit_frontdoor_runtime_snapshot(
                    runtime=runtime,
                    state={**dict(state or {}), "compression_state": self._default_compression_state()},
                )
                return FrontdoorTokenPreflightResult(
                    request_messages=list(request_messages),
                    final_request_tokens=int(single_shot_tokens or 0),
                    history_shrink_reason="",
                    diagnostics={
                        "applied": False,
                        "compression_pass_failed": str(compression_pass.refusal.reason or ""),
                        "capacity_tokens": int(compression_pass.capacity_tokens or 0),
                        "capacity_source": compression_pass.capacity_source,
                        "final_request_tokens": int(single_shot_tokens or 0),
                        "estimated_total_tokens": int(single_shot_tokens or 0),
                    },
                )
            chunked_mode = bool(compression_pass.mode == COMPRESSION_MODE_CHUNKED)
            compression_state = {
                "status": "running",
                "text": "上下文压缩中（分块）" if chunked_mode else "上下文压缩中",
                "source": compression_source,
                "needs_recheck": False,
            }
            await self._emit_frontdoor_runtime_snapshot(
                runtime=runtime,
                state={**dict(state or {}), "compression_state": compression_state},
            )
            chunk_count = 1
            merge_pass_applied = False
            chunk_failed_count = 0
            chunk_committed_count = 0
            compression_pass_rounds = 1
            deferred_messages: list[dict[str, Any]] = []
            if chunked_mode:
                (
                    compressed_text,
                    chunk_count,
                    merge_pass_applied,
                    chunk_failed_count,
                    compression_pass_rounds,
                    deferred_messages,
                    chunk_committed_count,
                ) = await self._chunked_frontdoor_compression_summaries(
                    grouped_history=grouped_history,
                    compression_pass=compression_pass,
                    model_refs=list(model_refs or []),
                    state=state,
                    runtime=runtime,
                    is_cancelled=_compression_cancelled,
                    model_info=model_info,
                )
            else:
                compressed_text, _compressed_message = await self._run_frontdoor_compression_helper_request(
                    messages=single_shot_messages,
                    model_refs=list(model_refs or []),
                    state=state,
                    runtime=runtime,
                    is_cancelled=_compression_cancelled,
                    model_info=model_info,
                    progress_text="上下文压缩中",
                )
            if _compression_cancelled():
                raise asyncio.CancelledError()
            if not str(compressed_text or "").strip():
                # 没拿到摘要就放弃这一 pass：原请求体照发。空产物往下走会收口阶段并替换
                # 历史，那是静默丢内容，比超窗报错更坏。
                await self._emit_frontdoor_runtime_snapshot(
                    runtime=runtime,
                    state={**dict(state or {}), "compression_state": self._default_compression_state()},
                )
                return FrontdoorTokenPreflightResult(
                    request_messages=list(request_messages),
                    final_request_tokens=int(single_shot_tokens or 0),
                    history_shrink_reason="",
                    diagnostics={
                        "applied": False,
                        "mode": "llm_chunked" if chunked_mode else "llm",
                        "compression_pass_failed": "compression helper returned no summary",
                        "final_request_tokens": int(single_shot_tokens or 0),
                        "estimated_total_tokens": int(single_shot_tokens or 0),
                        "chunk_count": int(chunk_count or 0),
                        "chunk_failed_count": int(chunk_failed_count or 0),
                    },
                )
            # 收口三段：模型选的引用（逐字回填）、被吞阶段的落档指针、账本标记。
            # 分块车道不要求模型选号（每块看不到全量候选），只落档并留指针。
            selected_ref_ids: list[int] = []
            if not chunked_mode:
                compressed_text, selected_ref_ids = split_stage_ref_selection(compressed_text)
            index_text, selected_ref_count, dropped_dead_refs = render_stage_ref_index(
                ref_candidates,
                selected_ref_ids,
            )
            summarized_stage_records = [
                dict(stage)
                for stage in list(durable_stage_state.get("stages") or [])
                if isinstance(stage, dict) and str(stage.get("stage_id") or "").strip() in set(summarized_stage_ids)
            ]
            archive_path, archive_stage_start, archive_stage_end = self._frontdoor_write_stage_archive_file(
                session_key=state.get("session_key"),
                stages=summarized_stage_records,
            )
            archive_text = ""
            if archive_path:
                archive_text = "\n".join(
                    [
                        STAGE_ARCHIVE_HEADING,
                        f"- stage {archive_stage_start}-{archive_stage_end} 共 {len(summarized_stage_records)} "
                        f"个阶段的完整记录（含逐条 key_refs 与工具轮次）已收口，不再逐轮进入上下文："
                        f"{archive_path}",
                        "  需要回看这些阶段的细节时用 content_open 按上面的路径打开。",
                    ]
                )
            summary_sections = [item for item in (compressed_text.strip(), index_text, archive_text) if item]
            compressed_text = "\n\n".join(summary_sections).strip()
            compacted_payload = {
                "kind": "frontdoor_token_compaction_llm",
                "history_message_count": len(older_history_messages),
            }
            if archive_path:
                compacted_payload["stage_archive"] = {
                    "ref": archive_path,
                    "stage_index_start": archive_stage_start,
                    "stage_index_end": archive_stage_end,
                    "stage_count": len(summarized_stage_records),
                    # 收口水位线随摘要块一起 durable：基线推进到这份请求体的那一刻才被应用。
                    # 逐条 stage_id 清单换成一个 created_at 上限：375 条 id 要 8.4k 字符，
                    # 比它守护的摘要正文还长且每轮重发。缺时间戳的阶段收不到，只能多渲染
                    # 一轮——反方向（收了没摘要的阶段）才是丢内容。
                    "archived_through_created_at": stage_created_at_ceiling(summarized_stage_records),
                }
            compacted_block = {
                "role": "assistant",
                "content": (
                    f"{TOKEN_COMPACT_V2_PREFIX}\n"
                    f"{json.dumps(compacted_payload, ensure_ascii=False, sort_keys=True)}\n\n"
                    f"{compressed_text}"
                ).strip(),
            }
            # 摘出的静默痕迹回插在摘要块之后、recent_tail 之前：它们比尾部更早，
            # 放这个位置保持时间顺序单调，不会在摘要与近况之间造出时序空洞。
            rewritten_messages = [
                *system_prefix,
                compacted_block,
                *deferred_messages,
                *preserved_silent_groups,
                *recent_tail,
                *contract_messages,
            ]
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
                    "mode": "llm_chunked" if chunked_mode else "llm",
                    "compression_mode": "llm_chunked" if chunked_mode else "llm",
                    "chunk_count": int(chunk_count or 1),
                    "merge_pass_applied": bool(merge_pass_applied),
                    "chunk_failed_count": int(chunk_failed_count or 0),
                    "chunk_committed_count": int(chunk_committed_count or 0),
                    "compression_pass_rounds": int(compression_pass_rounds or 1),
                    "deferred_message_count": int(len(deferred_messages or [])),
                    "capacity_tokens": int(compression_pass.capacity_tokens or 0),
                    "capacity_source": compression_pass.capacity_source,
                    "truncation_group_count": len(list(compression_pass.truncation_group_indexes or [])),
                    "dropped_dangling_tool_groups": int(dropped_dangling_tool_groups or 0),
                    "retained_recent_tail_count": recent_tail_count,
                    "compressed_history_message_count": len(older_history_messages),
                    "stage_ref_candidate_count": len(ref_candidates),
                    "stage_ref_selected_count": int(selected_ref_count),
                    "stage_ref_dropped_dead": int(dropped_dead_refs),
                    "stage_archive_pending_count": len(summarized_stage_ids),
                    # 区分线在阶段轨道里的落点；收尾落行时原样带进 stats，实时帧同带。
                    "boundary_stage_index": self._frontdoor_compression_boundary_index(
                        self._frontdoor_render_stage_state(session=session, state=state),
                        durable_stage_state,
                        summarized_stage_ids,
                    ),
                    "stage_archive_ref": archive_path,
                    "final_request_tokens": rewritten_tokens,
                },
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
        finally:
            if generation_id is not None and callable(finish_generation):
                try:
                    finish_generation(generation_id)
                except Exception:
                    pass

    @classmethod
    def _lift_silent_trace_groups(
        cls,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """把 `silent` 痕迹组（assistant 行 + 它声明的 tool 结果行）从待压缩区间摘出来。

        token 压缩是位置型的 —— `older_history_messages` 整段换成一条摘要块，白名单
        对它无效，所以只能摘出再回插。组必须整体搬：只搬 assistant 行会留下没有声明方
        的 tool 结果，provider 直接拒；只搬 tool 行则反过来出现悬空声明。
        返回 (剩余消息, 摘出的组)，两侧都保持原相对顺序。
        """
        records = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        preserved_indexes: set[int] = set()
        preserved_call_ids: set[str] = set()
        for index, item in enumerate(records):
            if str(item.get("role") or "").strip().lower() != "assistant":
                continue
            call_ids = {
                extract_call_id((call or {}).get("id"))
                for call in list(item.get("tool_calls") or [])
                if str(
                    ((call or {}).get("function") or {}).get("name") or (call or {}).get("name") or ""
                ).strip()
                == SILENT_TOOL_NAME
            }
            call_ids.discard("")
            if not call_ids:
                continue
            preserved_indexes.add(index)
            preserved_call_ids |= call_ids
        for index, item in enumerate(records):
            if index in preserved_indexes or str(item.get("role") or "").strip().lower() != "tool":
                continue
            if extract_call_id(item.get("tool_call_id")) in preserved_call_ids:
                preserved_indexes.add(index)
        remaining = [item for index, item in enumerate(records) if index not in preserved_indexes]
        preserved = [item for index, item in enumerate(records) if index in preserved_indexes]
        return remaining, preserved

    @classmethod
    def _drop_dangling_trailing_tool_call_groups(
        cls,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        """丢弃可压缩历史末尾悬空的工具调用组（assistant 声明了工具调用但结果缺失）。

        正常的尾部对齐之后不应出现该形态；续跑种子或异常状态可能带入。带着它发送
        会被部分 provider 拒绝，防御性丢弃，只影响压缩请求、不改写基线。
        返回 (保留的消息, 丢弃的组数)。"""
        kept = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        dropped = 0
        while kept:
            last = kept[-1]
            role = str((last or {}).get("role") or "").strip().lower()
            if role != "assistant" or not list((last or {}).get("tool_calls") or []):
                break
            kept.pop()
            dropped += 1
        return kept, dropped

    async def _run_frontdoor_compression_helper_request(
        self,
        *,
        messages: list[dict[str, Any]],
        model_refs: list[str],
        state: CeoGraphState,
        runtime: CeoRuntime,
        is_cancelled: Any,
        model_info: dict[str, Any],
        progress_text: str = "上下文压缩中",
    ) -> tuple[str, Any]:
        """压缩 helper 的单次请求发送 + 空响应对齐普通路径的重试语义。

        与 `_graph_call_model` 普通发送一致：空响应（含 error_text）先尝试运行时
        配置失效重建模型链，否则退避后重试，不设上限（普通路径同样无上限）；
        每次尝试之间检查取消钩子，压缩中 pause/取消随时生效。返回 (摘要正文, 消息)。
        """
        empty_response_retry_count = 0
        current_model_refs = list(model_refs or [])
        while True:
            if callable(is_cancelled) and bool(is_cancelled()):
                raise asyncio.CancelledError()
            message = await self._call_model_with_tools(
                messages=list(messages),
                tool_schemas=[],
                model_refs=list(current_model_refs),
                parallel_tool_calls=None,
                prompt_cache_key="",
            )
            if callable(is_cancelled) and bool(is_cancelled()):
                raise asyncio.CancelledError()
            response_view = self._model_response_view(message)
            self._persist_frontdoor_internal_request_artifact(
                state=state,
                runtime=runtime,
                request_messages=list(messages),
                tool_schemas=[],
                prompt_cache_key="",
                prompt_cache_diagnostics=build_prompt_cache_diagnostics(
                    stable_messages=list(messages),
                    dynamic_appendix_messages=[],
                    tool_schemas=[],
                    provider_model=self._frontdoor_model_display_name(model_info),
                    scope="ceo_frontdoor_token_compression",
                    prompt_cache_key="",
                    actual_request_messages=list(messages),
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
                usage=self._model_response_usage(message),
                request_lane="token_compression",
                parent_request_id=str(state.get("frontdoor_actual_request_history", [{}])[-1].get("request_id") or "").strip()
                if list(state.get("frontdoor_actual_request_history") or [])
                else "",
            )
            compressed_text = self._content_text(response_view.content).strip()
            has_error_text = bool(str(response_view.error_text or "").strip())
            if callable(is_cancelled) and bool(is_cancelled()):
                raise asyncio.CancelledError()
            if compressed_text and not has_error_text:
                return compressed_text, message
            # 空摘要 / provider 错误文本：对齐普通路径的空响应语义——先试配置失效
            # 重建模型链，再退避重试。绝不把空结果或错误文本当成压缩产物，也绝不
            # 误报为「上下文超限」。
            if self._refresh_runtime_config_for_retry_invalidation():
                try:
                    refreshed = self._resolve_ceo_model_refs_for_session(str(state.get("session_key") or "").strip())
                except Exception:
                    refreshed = []
                if list(refreshed or []):
                    current_model_refs = list(refreshed)
            empty_response_retry_count += 1
            if empty_response_retry_count >= _PROVIDER_RETRY_LIMIT:
                # 与节点道同口径封顶（`_PROVIDER_RETRY_LIMIT`）。交回空串＝"这一 pass 没拿到
                # 摘要"，由调用方放弃压缩；绝不把空产物当压缩结果，那等于静默丢历史。
                return "", None
            await self._emit_frontdoor_runtime_snapshot(
                runtime=runtime,
                state={
                    **dict(state or {}),
                    "compression_state": {
                        "status": "running",
                        "text": f"{progress_text}（重试 {empty_response_retry_count}）",
                        "source": compression_source,
                        "needs_recheck": False,
                    },
                },
            )
            await asyncio.sleep(float(min(10, max(1, empty_response_retry_count))))

    def _fit_frontdoor_chunk_to_capacity(
        self,
        chunk: list[dict[str, Any]],
        *,
        capacity_tokens: int,
    ) -> list[dict[str, Any]]:
        """一颗原子组单独超过块容量时，把头尾各留一段直到装得下（最多收 3 次）。

        不可分的是"组"而不是"正文"：工具调用与它的结果必须同块，但结果正文可以截。
        收到 0.216 还装不下就照原样发——那时超的是 provider 的宽容度，不是算错的容量，
        宁多发一节也不在压缩里再套一层摘要。
        """
        records = list(chunk or [])
        if capacity_tokens <= 0:
            return records
        for keep_ratio in (0.6, 0.36, 0.216):
            if self._estimate_frontdoor_compression_body_tokens(records) <= capacity_tokens:
                break
            records, changed = truncate_group_head_tail(records, keep_ratio=keep_ratio)
            if not changed:
                break
        return records

    async def _chunked_frontdoor_compression_summaries(
        self,
        *,
        grouped_history: list[tuple[list[dict[str, Any]], int]],
        compression_pass: CompressionPass,
        model_refs: list[str],
        state: CeoGraphState,
        runtime: CeoRuntime,
        is_cancelled: Any,
        model_info: dict[str, Any],
    ) -> tuple[str, int, bool, int, int, list[dict[str, Any]], int]:
        """执行规划器给出的块，提交最长成功前缀，其余原始历史留在本跳请求里。

        块是 system+user 重打包形态（块不构成对话前缀，体量与正常流量同级）。一块
        没拿到摘要就停在这儿：压好的块照常交付，这一块及其之后的原始历史整体保留，
        由下一跳按那时的窗口重算。比"整次作废、下一跳重烧所有块"省，也比"在摘要里
        插一行缺口却声称历史已收口"诚实——那行缺口下面根本没跟内容。
        返回 (合并摘要, 计划块数, 是否归并, 未取得摘要的块数, 实跑轮数, 留在请求里的原始历史, 收下的块数)。
        """

        capacity = int(compression_pass.capacity_tokens or 0)
        planned_chunks = max(1, int(compression_pass.planned_chunks or 0))
        queue: list[tuple[int, int]] = [
            tuple(item) for item in list(compression_pass.spans or []) + list(compression_pass.deferred_spans or [])
        ]
        summaries: list[str] = []
        failed_chunks = 0
        committed_group_end = 0
        rounds = 0
        while queue:
            rounds += 1
            batch = queue[:CHUNK_MAX_CHUNKS]
            remaining = queue[CHUNK_MAX_CHUNKS:]
            interrupted = False
            for offset, (start, end) in enumerate(batch):
                if callable(is_cancelled) and bool(is_cancelled()):
                    raise asyncio.CancelledError()
                index = len(summaries) + 1
                progress = f"上下文压缩中（分块 {index}/{planned_chunks}）"
                await self._emit_frontdoor_runtime_snapshot(
                    runtime=runtime,
                    state={
                        **dict(state or {}),
                        "compression_state": {
                            "status": "running",
                            "text": progress,
                            "source": compression_source,
                            "needs_recheck": False,
                        },
                    },
                )
                chunk = self._fit_frontdoor_chunk_to_capacity(
                    [dict(item) for group, _tokens in grouped_history[start:end] for item in group],
                    capacity_tokens=capacity,
                )
                chunk_messages = [
                    {"role": "system", "content": _FRONTDOOR_TOKEN_COMPRESSION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "kind": "frontdoor_token_compression_chunk",
                                "model": self._frontdoor_model_display_name(model_info),
                                "chunk_index": index,
                                "chunk_count": planned_chunks,
                                "history_messages": list(chunk),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
                chunk_text, _chunk_message = await self._run_frontdoor_compression_helper_request(
                    messages=chunk_messages,
                    model_refs=list(model_refs or []),
                    state=state,
                    runtime=runtime,
                    is_cancelled=is_cancelled,
                    model_info=model_info,
                    progress_text=progress,
                )
                if not str(chunk_text or "").strip():
                    # 这一块耗尽重试仍没拿到摘要：停在这里，本块及其后的原始历史原样留在
                    # 请求里，由下一跳重算。压好的前缀照常交付，不整次作废重烧。
                    failed_chunks += 1
                    queue = batch[offset:] + remaining
                    interrupted = True
                    break
                summaries.append(f"[分块摘要 {index}/{planned_chunks}]\n{chunk_text}")
                committed_group_end = end
            if interrupted or rounds >= COMPRESSION_MAX_PASS_ROUNDS:
                break
            queue = remaining
        deferred_messages = [
            dict(item) for group, _tokens in grouped_history[committed_group_end:] for item in group
        ]
        combined = "\n\n".join(summaries).strip()
        deferred_note = (
            f"[本次分块只覆盖了较早的历史，仍有 {len(deferred_messages)} 条原始消息在本请求里，"
            "下一跳按那时的窗口继续压缩]"
            if deferred_messages
            else ""
        )
        merge_pass_applied = False
        if len(summaries) > 1 and self._estimate_frontdoor_compression_body_tokens(
            [{"role": "assistant", "content": combined}]
        ) > capacity:
            if callable(is_cancelled) and bool(is_cancelled()):
                raise asyncio.CancelledError()
            await self._emit_frontdoor_runtime_snapshot(
                runtime=runtime,
                state={
                    **dict(state or {}),
                    "compression_state": {
                        "status": "running",
                        "text": "上下文压缩中（归并分块摘要）",
                        "source": compression_source,
                        "needs_recheck": False,
                    },
                },
            )
            merge_messages = [
                {"role": "system", "content": _FRONTDOOR_TOKEN_COMPRESSION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "kind": "frontdoor_token_compression_merge",
                            "model": self._frontdoor_model_display_name(model_info),
                            "summaries": summaries,
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
            merged_text, _merged_message = await self._run_frontdoor_compression_helper_request(
                messages=merge_messages,
                model_refs=list(model_refs or []),
                state=state,
                runtime=runtime,
                is_cancelled=is_cancelled,
                model_info=model_info,
                progress_text="上下文压缩中（归并分块摘要）",
            )
            if str(merged_text or "").strip():
                # 归并失败时交回的是已经压好的那份多块摘要，不是空串：拿空串覆盖等于
                # 把整次压缩的成果抵给一次没成功的调用。
                combined = merged_text
            merge_pass_applied = True
        if deferred_note and summaries:
            combined = "\n\n".join(item for item in (combined.strip(), deferred_note) if item).strip()
        return (
            combined,
            planned_chunks,
            merge_pass_applied,
            failed_chunks,
            rounds,
            deferred_messages,
            len(summaries),
        )

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

    def _frontdoor_runtime_config_revision(self) -> int:
        """Current runtime config revision after an mtime-gated reload check."""
        try:
            _config, revision, _changed = get_runtime_config(force=False)
            return int(revision or 0)
        except Exception:
            try:
                return int(peek_runtime_revision() or 0)
            except Exception:
                return 0

    def _rotate_frontdoor_model_refs_if_stale(self, state: dict[str, Any]) -> dict[str, Any]:
        """Re-resolve ``model_refs`` when the runtime config revision moved on.

        Model-chain edits (``model_config`` tool, admin routes) rewrite the
        config and refresh the loop runtime, but the in-flight turn still
        carries the ``model_refs`` captured at ``prepare_turn``. Each
        ``call_model`` iteration builds a fresh provider request, so
        re-resolving at this boundary is a rebuild point, not a mid-request
        hot swap of an already-sent provider request.

        Rotation only runs when a ``model_refs_revision`` baseline exists and
        differs from the current revision. Legacy state without the baseline
        keeps its existing refs untouched.
        """
        recorded_revision = state.get("model_refs_revision")
        if recorded_revision is None:
            return state
        try:
            recorded = int(recorded_revision)
        except (TypeError, ValueError):
            return state
        current_revision = self._frontdoor_runtime_config_revision()
        if recorded == current_revision:
            return state
        try:
            new_refs = list(self._resolve_ceo_model_refs_for_session(state.get("session_key")))
        except Exception:
            logger.warning(
                "frontdoor model refs rotation skipped; resolve failed session={}",
                str(state.get("session_key") or "").strip(),
            )
            return state
        if not new_refs:
            return state
        rotated = dict(state)
        rotated["model_refs"] = list(new_refs)
        rotated["model_refs_revision"] = self._frontdoor_runtime_config_revision()
        logger.info(
            "frontdoor model refs rotated revision {} -> {} session={}",
            recorded,
            rotated["model_refs_revision"],
            str(state.get("session_key") or "").strip(),
        )
        return rotated

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
        if previous_path:
            record = cls._frontdoor_actual_request_record_from_path(previous_path)
            if record:
                return record
        # C1：previous 槽位只在同实例轮转时填充，会话重载/重启后为空——回退扫描
        # 会话 artifact 目录取最新一条可见请求，让 usage+delta 估算跨进程重启后
        # 的下一个真实用户轮仍然可用。
        return cls._frontdoor_latest_persisted_visible_request_record(session)

    @classmethod
    def _frontdoor_latest_persisted_visible_request_record(cls, session: Any | None) -> dict[str, Any]:
        session_key = str(getattr(getattr(session, "state", None), "session_key", "") or "").strip()
        if not session_key:
            return {}
        try:
            directory = actual_request_dir_for_session(session_key, create=False)
        except Exception:
            return {}
        try:
            if not directory.exists():
                return {}
            candidates = sorted(directory.glob("*.json"), key=lambda item: item.name, reverse=True)
        except Exception:
            return {}
        for candidate in candidates:
            record = cls._frontdoor_actual_request_record_from_path(str(candidate))
            if not record:
                continue
            lane = str(record.get("request_lane") or "").strip()
            if lane and lane != "visible_frontdoor":
                continue
            if not list(record.get("request_messages") or record.get("messages") or []):
                continue
            return record
        return {}

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

    @classmethod
    def _frontdoor_seed_actual_request_record(cls, *, session: Any | None) -> dict[str, Any]:
        """Seed record for reproducing the provider-facing request body prefix.

        空闲 composer 预估发生在任何回合开始之前,此时最新请求仍在实时轨迹里;
        用户回合开始会把实时轨迹搬进 previous 槽位(`_preserve_frontdoor_actual_request_trace_for_next_visible_turn`)。
        send preflight 的 append-only 比对始终以最新记录为基线,种子必须解析到同一条,
        否则 usage-first 估算静默退化成全量 preview。故这里取「最新记录」(无实时轨迹时
        自然回落到 previous 槽位,与回合开始时的语义一致)。
        """
        return cls._frontdoor_latest_actual_request_record(session=session, state=None)

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
        stage_state: dict[str, Any] | None = None,
        previous_tools_record_basis: str = "proxy",
        current_tools_record_basis: str = "proxy",
    ) -> tuple[int, bool, int]:
        """返回 (本跳相对上一跳的增量, 是否可比, 上一跳被阶段裁撤掉的投影体量)。

        第三项是 usage-first 锚点的修正量：锚点取的是上一跳的真实 provider usage，
        里面还留着这一跳已被裁掉的工具肉身，不扣回去就会把整段被裁体量算回读数。
        """
        # 两侧走同一跨轮可比性投影（契约 / turn-only / 记忆快照 / 多模态 / 动态
        # overlay 剥离 + 内部提示折叠）。长期记忆快照与工具契约块是每轮重新生成
        # 的动态块，只存在于已发出的真实请求里，而下一轮请求由 durable 基线重新
        # 拼装，按原始形态比对会让前缀恒不等，usage-first 估算静默退化成全量 preview。
        previous_projection = cls._frontdoor_comparable_request_records(previous_request_messages)
        current_records = cls._frontdoor_comparable_request_records(current_request_messages)
        # 阶段窗口重写是原位块重写：两侧在同一份阶段状态下做同一 trim（幂等）后才
        # 可能前缀相等，否则阶段压缩会让历史中段字节漂移、可比性失效。
        previous_records = previous_projection
        previous_stage_trimmed = False
        if isinstance(stage_state, dict) and list(stage_state.get("stages") or []):
            previous_records, previous_stage_trimmed = cls._trim_frontdoor_seed_stage_compaction(
                previous_projection,
                stage_state,
            )
            current_records, _current_trimmed = cls._trim_frontdoor_seed_stage_compaction(
                current_records,
                stage_state,
            )
        if not previous_records or len(current_records) < len(previous_records):
            return 0, False, 0
        if not cls._fresh_turn_seed_records_match(current_records[: len(previous_records)], previous_records):
            return 0, False, 0
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
        if previous_tool_schema_hash != current_tool_schema_hash and str(
            previous_tools_record_basis or "proxy"
        ) == str(current_tools_record_basis or "proxy"):
            return 0, False, 0
        # 两侧记录口径不同（切换记录面对齐的那一跳会遇到：旧记录是 proxy 那份 31 名数组，
        # 当跳是 wire 那份 33 名）⇒ 工具哈希不等不再单独构成"不可比"。代价是这一跳万一线上
        # 字节真的也变了，这一条比对放行了；线上一致性由 wire 三枚读数与
        # `tools[] bytes changed outside a reprint boundary` 那条 WARNING 负责，不放给估算面。
        normalized_previous_tool_schemas = [
            dict(item)
            for item in list(previous_tool_schemas or [])
            if isinstance(item, dict)
        ]
        previous_estimate_tokens = int(
            _estimate_frontdoor_provider_request_tokens(
                provider_request_body=None,
                request_messages=previous_records,
                tool_schemas=normalized_previous_tool_schemas,
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
        projection_shrink_tokens = 0
        if previous_stage_trimmed:
            # 同一投影下再量一次「未裁」版本：裁撤掉的肉身仍算在上一跳的真实 usage 里，
            # 差额不扣回锚点就会在每个过期点把整段被裁体量算回读数。
            untrimmed_previous_estimate_tokens = int(
                _estimate_frontdoor_provider_request_tokens(
                    provider_request_body=None,
                    request_messages=previous_projection,
                    tool_schemas=normalized_previous_tool_schemas,
                )
                or 0
            )
            projection_shrink_tokens = max(
                0,
                untrimmed_previous_estimate_tokens - previous_estimate_tokens,
            )
        return (
            max(0, current_estimate_tokens - previous_estimate_tokens),
            True,
            projection_shrink_tokens,
        )

    @staticmethod
    def _provider_tool_exposure_revision(tool_names: list[str] | None) -> str:
        normalized = [
            str(item or "").strip()
            for item in list(tool_names or [])
            if str(item or "").strip()
        ]
        if not normalized:
            return ""
        payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        return f"pte:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"

    @classmethod
    def _refresh_frontdoor_provider_tool_bundle(
        cls,
        *,
        prior_provider_tool_names: list[str] | None,
        desired_provider_tool_names: list[str] | None,
        prior_history_shrink_reason: str = "",
        recommit_boundary: bool = False,
    ) -> dict[str, Any]:
        prior = [
            str(item or "").strip()
            for item in list(prior_provider_tool_names or [])
            if str(item or "").strip()
        ]
        desired = [
            str(item or "").strip()
            for item in list(desired_provider_tool_names or [])
            if str(item or "").strip()
        ]
        prior_membership = set(prior)
        # 唯一的重新提交点由调用方以**消耗式**信号给进来（`recommit_boundary`：手动压缩与内联
        # 压缩挂的同一枚待重印标记、空闲窗口第一跳）。这一跳正文反正已被整段重写，可复用前缀
        # 断在这里，重印参数表不额外破缓存。
        # 这里不再读 `prior_history_shrink_reason` 当判据：那条值是会话级残留，一次压缩之后
        # 每轮都还在（实盘 10-19 18:21:28 / 18:22:08 / 18:24:21 / 18:24:29 连续四跳各自改
        # 名册），等于把"只有重印边界能改名册"稀释成"每个用户轮都能改"。该字段只留作取证读数。
        at_recommit_boundary = bool(recommit_boundary)
        if not prior:
            active = list(desired)
            provider_tool_bundle_mode = "pinned_seeded"
        elif at_recommit_boundary:
            active = list(desired)
            provider_tool_bundle_mode = "pinned_recommitted"
        else:
            # 钉住＝完全冻结：重印边界（token 压缩、手动压缩、空闲窗口第一跳）之前
            # `tools[]` 一个名字不增删，回合起点也不并入新增，换车道同样不重印。期间新工具靠尾块
            # `undeclared_candidates` 点名（清单到压缩那一跳才重印），可执行面走水合
            # 台账，模型调用的参数契约由 load 返回承载——见 `tool-and-skill-system.md`
            # 「清单滞后不是权限滞后」。
            active = list(prior)
            provider_tool_bundle_mode = "pinned_frozen"
        return {
            "provider_tool_names": list(active),
            "pending_provider_tool_names": [],
            "provider_tool_exposure_pending": False,
            "provider_tool_exposure_revision": cls._provider_tool_exposure_revision(active),
            "provider_tool_exposure_commit_reason": "",
            "provider_tool_bundle_seeded": bool(set(active) != prior_membership),
            "provider_tool_bundle_mode": provider_tool_bundle_mode,
            "desired_provider_tool_names": list(desired),
            "prior_history_shrink_reason": str(prior_history_shrink_reason or "").strip(),
            "provider_tool_membership_changed": bool(set(active) != prior_membership),
        }

    @classmethod
    def _resolve_frontdoor_provider_tool_exposure(
        cls,
        *,
        active_provider_tool_names: list[str] | None,
        pending_provider_tool_names: list[str] | None,
        desired_provider_tool_names: list[str] | None,
        commit_reason: str = "",
    ) -> dict[str, Any]:
        _ = pending_provider_tool_names
        # `commit_reason="token_compression"` 在这里代表"这一跳就是压缩跳"——一次性、由调用方
        # 明说，所以它可以直接充当重印判据。生产不走这条：它挂消耗式标记并由
        # `_frontdoor_bundle_recommit_boundary` 消费，避免跨轮残留把判据粘住。
        return cls._refresh_frontdoor_provider_tool_bundle(
            prior_provider_tool_names=active_provider_tool_names,
            desired_provider_tool_names=desired_provider_tool_names,
            prior_history_shrink_reason=commit_reason,
            recommit_boundary=str(commit_reason or "").strip() == "token_compression",
        )

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
            # 与 message_builder._request_body_seed_records 对称：两侧都全 strip，
            # 避免构建期 lstrip 与比较期仅 rstrip 造成的假性不等。
            return value.replace("\r\n", "\n").strip()
        return value

    @staticmethod
    def _seed_record_is_empty_non_structural(record: dict[str, Any] | None) -> bool:
        """与构建期一致：空内容的非结构记录（无工具调用、非工具结果）不参与比较。"""
        if not isinstance(record, dict):
            return True
        if list(record.get("tool_calls") or []):
            return False
        if str(record.get("role") or "").strip().lower() == "tool":
            return False
        return not str(record.get("content") or "").strip()

    @classmethod
    def _fresh_turn_seed_records_match(
        cls,
        first: list[dict[str, Any]] | None,
        second: list[dict[str, Any]] | None,
    ) -> bool:
        first_records = [
            dict(item)
            for item in list(first or [])
            if isinstance(item, dict) and not cls._seed_record_is_empty_non_structural(item)
        ]
        second_records = [
            dict(item)
            for item in list(second or [])
            if isinstance(item, dict) and not cls._seed_record_is_empty_non_structural(item)
        ]
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
        previous_record = cls._frontdoor_seed_actual_request_record(session=session)
        previous_request_messages = cls._prompt_message_records(previous_record.get("request_messages"))
        if not previous_request_messages:
            return cls._prompt_message_records(live_request_messages)
        stable_records = cls._prompt_message_records(stable_messages)
        live_records = cls._prompt_message_records(live_request_messages)
        # C5：上一份真实请求原貌带记忆快照/动态 overlay，本轮稳定段也带本轮重新
        # 注入的同形动态块——按原样逐条比对必然在 index 1 失配、采纳失败退回基线
        # 重组。两侧同一「逐条丢弃」投影后再比前缀，输出仍用原始记录拼接。
        keep_flags = [cls._frontdoor_adoption_projection_record_kept(item) for item in stable_records]
        projected_stable = [dict(item) for item, kept in zip(stable_records, keep_flags) if kept]
        projected_previous = [
            dict(item)
            for item in previous_request_messages
            if cls._frontdoor_adoption_projection_record_kept(item)
        ]
        body_len = len(projected_previous)
        stable_len = len(stable_records)
        if body_len <= 0 or len(projected_stable) < body_len or stable_len < body_len:
            return live_records
        if not cls._fresh_turn_seed_records_match(projected_stable[:body_len], projected_previous):
            return live_records
        # 把投影前缀长度反映射回原始下标：stable 侧被消费到第几条原始记录。
        raw_boundary = stable_len
        consumed = 0
        for index, kept in enumerate(keep_flags, start=1):
            if kept:
                consumed += 1
            if consumed >= body_len:
                raw_boundary = index
                break
        if len(live_records) < stable_len or not cls._fresh_turn_seed_records_match(
            live_records[:stable_len],
            stable_records,
        ):
            return live_records
        stable_tail = list(stable_records[raw_boundary:])
        live_tail = list(live_records[stable_len:])
        return [
            *list(cls._strip_frontdoor_turn_only_artifacts(previous_request_messages)),
            *stable_tail,
            *live_tail,
        ]

    @classmethod
    def _fresh_turn_tool_schema_seed_from_previous_actual_request(
        cls,
        *,
        session: Any | None,
        tool_schemas: list[dict[str, Any]] | None,
        expected_schema_names: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str] | None]:
        current_tool_schemas = [dict(item) for item in list(tool_schemas or []) if isinstance(item, dict)]
        previous_record = cls._frontdoor_seed_actual_request_record(session=session)
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
        if previous_names == current_names:
            return previous_tool_schemas, list(previous_names)
        return current_tool_schemas, None

    """Shared CEO runtime operations reused by the create_agent frontdoor path."""

    def __init__(self, *, loop) -> None:
        super().__init__(loop=loop)

    def _ceo_session_temp_dir(self, session_key: Any) -> str:
        """会话级临时目录 `<数据根>/temp/ceo/<safe_session_key>`。

        注入为工具 runtime 的 `task_temp_dir`：exec 未显式传 working_dir 时以它
        为默认 cwd，filesystem/exec 的路径策略也以它为临时内容的规范落点，
        避免临时文件散落到工作区根目录。目录惰性创建（exec/filesystem 写入时 mkdir）。
        锚是数据根而不是项目根，与 `temp/tasks/` 同根。
        """
        return ceo_session_temp_dir(session_key)

    def _frontdoor_exec_runtime_policy(self) -> dict[str, Any] | None:
        main_service = getattr(self._loop, "main_task_service", None)
        getter = getattr(main_service, "_current_exec_runtime_policy_payload", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    def _frontdoor_skill_roster_items(
        self,
        *,
        session_key: Any,
        skill_ids: list[Any] | None,
    ) -> list[dict[str, str]]:
        """把 state 里的 id 名单补全成渲染要用的 `id|名称|描述` 条目。

        state 只带 id（state 键过归一化白名单，塞记录进去会被静默丢掉），描述从治理
        实时可见集取——与 `message_builder` 回合起点渲染用的是同一份来源，同一批 id
        渲出的字节逐字相同。重印只发生在 epoch 跳变时，这里的实时取数成本可忽略。
        """
        normalized_ids = [
            str(item or "").strip()
            for item in list(skill_ids or [])
            if str(item or "").strip()
        ]
        if not normalized_ids:
            return []
        service = getattr(self._loop, "main_task_service", None)
        lister = getattr(service, "list_contract_visible_skill_resources", None)
        records_by_id: dict[str, Any] = {}
        if callable(lister):
            try:
                records = lister(
                    actor_role="ceo",
                    session_id=str(session_key or "").strip() or "web:shared",
                )
            except Exception:
                records = []
            for record in list(records or []):
                record_id = _skill_roster.normalize_skill_roster_item(record)
                if record_id is not None:
                    records_by_id[record_id["skill_id"]] = record
        items: list[dict[str, str]] = []
        for skill_id in normalized_ids:
            record = records_by_id.get(skill_id)
            if record is not None:
                item = _skill_roster.normalize_skill_roster_item(record)
                if item is not None:
                    items.append(item)
                    continue
            # 记录取不到（mid-turn 被删）也要占位：头部名单是"钉住时点的全量"，
            # 少一行会让重印结果与回合起点渲染的字节不一致。
            items.append({"skill_id": skill_id, "display_name": skill_id, "description": ""})
        return items

    def _frontdoor_pinned_contract(
        self,
        *,
        session_key: Any,
        skill_ids: list[Any] | None,
        session: Any = None,
    ) -> tuple[str, list[str]]:
        """钉进头部的静态声明原文 + 它钉住的那份名单；空串＝本轮不钉，尾块照旧带全量。

        前门一个回合内有四条装配路（`message_builder` 的两条、回合起点的 send-preflight、
        同回合每一跳的 prompt contract），它们拿到的 session 实例与 state 形状各不相同，靠
        `tool_contract` 里那张按 session_key 分桶的进程表对齐：只要刷新边界（roster epoch）
        没动，四处拿到的是同一串原文。头部一改就顶掉身后全部前缀，四处不一致比不钉严重。
        """
        normalized_session_key = str(session_key or "").strip()
        pinned_text = frontdoor_pinned_contract_text(
            session,
            skill_items=self._frontdoor_skill_roster_items(
                session_key=normalized_session_key,
                skill_ids=list(skill_ids or []),
            ),
            exec_runtime_policy=self._frontdoor_exec_runtime_policy(),
            session_temp_dir=self._ceo_session_temp_dir(normalized_session_key),
            skill_roster_epoch=skill_roster_epoch_for(session, session_key=normalized_session_key),
            session_key=normalized_session_key,
        )
        if not pinned_text:
            return "", []
        return pinned_text, pinned_skill_ids_for(session, session_key=normalized_session_key)

    def _ceo_tool_watchdog_runtime_config(self) -> dict[str, Any]:
        """CEO 侧统一工具 timeout 全局默认值入口（读主运行时配置，缺省走内置默认）。"""
        main_service = getattr(self._loop, "main_task_service", None)
        config = getattr(main_service, "_app_config", None)
        agents = getattr(config, "agents", None) if config is not None else None
        try:
            value = float(getattr(agents, "tool_default_timeout_seconds", 0) or 0)
        except (TypeError, ValueError):
            return {}
        return {"default_timeout_seconds": value} if value >= 1 else {}

    def _build_tool_runtime_context(
        self,
        *,
        state: CeoGraphState,
        runtime: CeoRuntime,
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
        turn_model_refs = list(
            state.get("model_refs") or self._resolve_ceo_model_refs_for_session(session.state.session_key) or []
        )
        image_enabled, image_model_name = self._ceo_image_multimodal_for_request(turn_model_refs)
        # 在场索引随 runtime context 交给执行道：参数错误指针与冲动投递都读这一份，
        # 每条消费路各自重扫历史就会在同一跳里长出两个答案。
        contract_presence = self._frontdoor_contract_presence_index(state)
        return {
            "on_progress": runtime.context.on_progress,
            "emit_lifecycle": True,
            "actor_role": "ceo",
            "tool_watchdog": self._ceo_tool_watchdog_runtime_config(),
            "session_key": session.state.session_key,
            "turn_id": turn_id,
            "model_refs": turn_model_refs,
            "image_multimodal_enabled": image_enabled,
            "image_model_display_name": image_model_name,
            "tool_contract_enforced": True,
            "callable_tool_names": list(state.get("tool_names") or []),
            "candidate_tool_names": self._frontdoor_candidate_tool_view(state),
            "candidate_skill_ids": list(state.get("candidate_skill_ids") or []),
            "tool_contract_presence": contract_presence,
            # 给拒绝文案用：撞进来路已收的工具时，要能说"是无权限"而不是"契约不在场"。
            "declared_denied_tool_names": self._frontdoor_declared_denied_tool_names_for_state(state),
            # 可加载面（钉住的 tools[]）：声明里有、callable 里没有的名字，拒绝文案要给 load 指引。
            "declared_tool_names": list(state.get("provider_tool_names") or []),
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
            "task_temp_dir": self._ceo_session_temp_dir(session.state.session_key),
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

    def _frontdoor_dispatch_tool_names(self, state: CeoGraphState) -> list[str]:
        """声明名单＝模型可见 `tools[]` 的名字源：钉住的声明 ∩ 当轮治理可见。

        这份名单是**可加载面**，不是准入面：`tools[]` 全程钉死（重印边界之前一个名字
        不增删，回合起点也不并入新增），所以它不并本轮新落的名字——mid-turn 注册的工具在
        声明吸收它之前，参数表不进 `tools[]`，靠尾块 `undeclared_candidates` 点名、load
        返回的参数契约供模型参考。真正放行调用的是 `_frontdoor_executable_tool_names`（本轮
        callable），两者分源。

        这里**不**按契约在场筛名字：在场是逐跳变化的（一次 load 就多一个名字、一次裁撤就少
        一个），拿它减声明名就等于让 `tools[]` 每跳改成员，钉住语义当场失效、前缀缓存按跳
        断。契约不在场的代价只付在准入面——调它会被拒并拿回契约，而不是从声明面消失。

        清单可以滞后（删名推迟到压缩重印），执行准入不行——声明滞后绝不能变成权限滞后。
        治理可见集取不到时按当轮 callable pool 收，不拿声明兜底（宁可窄不可宽）。
        """
        declared = self._normalized_tool_name_state_list(list(state.get("provider_tool_names") or []))
        granted = self._normalized_tool_name_state_list(list(state.get("rbac_visible_tool_names") or []))
        if granted:
            granted_set = set(granted)
            return [name for name in declared if name in granted_set]
        return self._normalized_tool_name_state_list(list(state.get("tool_names") or []))

    def _frontdoor_executable_tool_names(self, state: CeoGraphState) -> list[str]:
        """执行准入＝本轮 callable（常驻内置 + 契约在场的名字）；控制工具另行注入执行束。

        `tools[]` 是声明面、callable 集是准入面，两者不同义：声明里的名字在
        `load_tool_context` 把契约送进上下文（下一跳在场、进 callable）之前调用一律被拒，
        回执引导先 load。这是"先读契约再调"的唯一落点——只在尾块点名、执行侧照旧放行，
        规则就只剩文案效果（`perf_inspect` 那例）。`submit_next_stage` / `silent` 是合成实例，
        由 `_frontdoor_execution_bundle` 直接装进对象字典，不在这份名单里。

        RBAC 收回要当跳生效：声明名被收回时仍挂在钉住的 `tools[]` 上（清单滞后），所以
        声明名在 callable 之上再与当轮治理可见集求交，宁可窄不可宽。非声明名（mid-turn
        注册后 load 落账的）不受这道交约束——回合初快照本来就没有它，按快照误拒会把
        "load 成功却调不动"重新造出来。缺 callable 视图（`tool_names` 键缺失的旁路/老
        快照）才退回派发名单，避免整束塌成空、回合一个工具都调不动。
        """
        if not isinstance(state, dict):
            return []
        callable_names = [
            name
            for name in self._frontdoor_callable_tool_names_for_state(state)
            # 控制工具是执行束里另装的合成实例（`_frontdoor_execution_bundle`），不在
            # registry 里，留在这份名单只会让按名取实例空转、污染排障视图。
            if name not in {STAGE_TOOL_NAME, SILENT_TOOL_NAME}
        ]
        if not callable_names and "tool_names" not in state:
            return self._frontdoor_dispatch_tool_names(state)
        granted = self._normalized_tool_name_state_list(list(state.get("rbac_visible_tool_names") or []))
        if not granted:
            return callable_names
        declared = set(
            self._normalized_tool_name_state_list(list(state.get("provider_tool_names") or []))
        )
        granted_set = set(granted)
        return [
            name
            for name in callable_names
            if name not in declared or name in granted_set
        ]

    def _frontdoor_live_granted_tool_names(self, *, session_key: str) -> list[str]:
        """实时读一次"这个角色现在被允许哪些工具"。

        装配期的 capability_snapshot 是回合内缓存的，权限收回要过一会儿才反映进去；
        denied_tools 说的就是"现在调不动"，拿旧快照对照必然漏报。
        """
        service = getattr(self._loop, "main_task_service", None)
        lister = getattr(service, "list_effective_tool_names", None) if service is not None else None
        if not callable(lister):
            return []
        try:
            payload = lister(
                actor_role="ceo",
                session_id=str(session_key or "").strip() or "web:shared",
            )
        except Exception:
            return []
        return [str(item or "").strip() for item in list(payload or []) if str(item or "").strip()]

    @staticmethod
    def _frontdoor_declared_denied_tool_names(
        *,
        declared_tool_names: list[str] | None,
        granted_tool_names: list[str] | None,
    ) -> list[str]:
        """钉住的清单里带着、但当轮治理已不放行的名字：尾块要提前点名，别靠模型撞一次拒绝。

        治理可见集取不到时不产出这一行——那种情况下一律说"无权限"会误导模型。
        """
        declared = [str(item or "").strip() for item in list(declared_tool_names or []) if str(item or "").strip()]
        granted = {str(item or "").strip() for item in list(granted_tool_names or []) if str(item or "").strip()}
        if not declared or not granted:
            return []
        return [name for name in declared if name not in granted]

    def _frontdoor_declared_denied_tool_names_for_state(self, state: CeoGraphState) -> list[str]:
        """state 版差集：前门每一处要印 denied_tools 的地方都走这里，不再各算各的。

        发送前的 prompt contract 会用 state 重建尾块，重建处若自己另算一份（或干脆不传），
        装配层算出来的那一行就会在落请求体前被覆盖掉。

        名单源只有钉住声明：denied_tools 的语义是"`tools[]` 里带着参数表、但实时授权读
        不放行"。mid-turn 注册的名字不在 `tools[]` 里，被 RBAC 收回时不进这一行——
        它的出口是工具态收尾的实时卫生（`_frontdoor_tool_state_after_tool_results`）：
        下一跳起从 `tool_names` 摘除，callable 集不再含它；治理重新放行时由同一处
        的发现出口送回候选。

        单源化之后不再过可渲染性那道筛：参数表由整表供出，名单里的每个名字这一跳都带着
        一份字节（供不出的那份是占位），所以"在 `tools[]` 里"对钉住名单恒真。这与节点侧
        `declared_denied_tool_names` 的判据同形。
        """
        declared_tool_names = list(state.get("provider_tool_names") or [])
        if not tools_bundle_single_source_enabled():
            declared_tool_names = self._frontdoor_schema_renderable_tool_names(
                declared_tool_names,
                session_key=str(state.get("session_key") or "").strip(),
            )
        return self._frontdoor_declared_denied_tool_names(
            declared_tool_names=declared_tool_names,
            granted_tool_names=self._frontdoor_live_granted_tool_names(
                session_key=str(state.get("session_key") or ""),
            ),
        )

    def _frontdoor_schema_renderable_tool_names(
        self,
        tool_names: list[str] | None,
        *,
        session_key: str = "",
        session: Any = None,
    ) -> list[str]:
        """清单里这一跳**真能渲出参数表**的名字。

        `denied_tools` 的措辞是"`tools[]` 里带着参数表，但这一跳调不动"，所以它只装真的
        在 `tools[]` 里的名字。判据与渲染器同源：活实例、删除前抄下的那份 schema 快照，
        或钉住整表里供得出的字节（阶段 1 放行，`pinned_tool_bundle`）。两者都没有的名字
        （例如资源被卸载且没留过快照、整表里也没这份）渲不出参数表，写进这一行只会让模型
        拿着"参数表在场"的说明去撞一个不存在的名字。

        注意预检用的 `_selected_tool_schemas` 仍是"实例 ∪ 快照"两颗源、没有整表档：那份数组
        今天只服务估算与记录面，比 wire 少名字，等阶段 2 单源后一起收掉。

        判据用注册表里的存在性而不是真去渲一遍 schema：发送侧渲染器跳过一个名字的条件就是
        这三份来源都取不到，而这段每跳要跑好几处（装配、预检、发送前重建），按名字做一次
        字典查找就够了。
        """
        normalized = [
            str(item or "").strip()
            for item in list(tool_names or [])
            if str(item or "").strip()
        ]
        tools_registry = getattr(self._loop, "tools", None)
        tool_lookup = getattr(tools_registry, "get", None)
        if not callable(tool_lookup):
            # 注册表读不到时保持原口径：宁可多说，也不因为一次读失败把收回的名字静默掉。
            return normalized
        renderable: list[str] = []
        for name in normalized:
            try:
                tool = tool_lookup(name)
            except Exception:
                tool = None
            if tool is None:
                if lookup_tool_schema(name) is not None:
                    renderable.append(name)
                    continue
                # 阶段 1（Q-A）：钉住整表里供得出参数表的名字也算可渲染。今天这份判据比渲染器
                # 窄（渲染器还会读整表与占位），所以"既无实例也无快照"的名字在进渲染器之前
                # 就被摘掉——删一颗工具因此每跳少一条，前缀从那条的起始字节断一次。放行只决定
                # 名字留不留，不动渲染顺序。
                if pinned_tool_bundle.has_bundled_schema(
                    session_key=session_key,
                    session=session,
                    name=name,
                ):
                    renderable.append(name)
                continue
            renderable.append(str(getattr(tool, "name", "") or name).strip() or name)
        return renderable

    def _frontdoor_live_visible_skills(self, session_key: Any) -> list[Any]:
        """按轮检测的数据源：治理实时可见的 skill 记录（id+名称+描述）。

        取不到（服务缺失/异常）返回空列表——注入块本轮为空，头部名单照旧，宁可少说
        不可说错。
        """
        service = getattr(self._loop, "main_task_service", None)
        lister = getattr(service, "list_contract_visible_skill_resources", None)
        if not callable(lister):
            return []
        try:
            return list(
                lister(
                    actor_role="ceo",
                    session_id=str(session_key or "").strip() or "web:shared",
                )
                or []
            )
        except Exception:
            return []

    def _frontdoor_skill_roster_notice_scope(self, session_key: Any) -> str:
        """前门台账作用域：按会话一条，与节点道 `node:<task>:<node>` 同构。"""
        return _skill_roster_notice.session_scope_key(session_key)

    def _readable_frontdoor_skill_ids(self, session_key: Any) -> list[str]:
        readable: list[str] = []
        for record in list(
            _skill_roster_notice.readable_skill_items(self._frontdoor_live_visible_skills(session_key)) or []
        ):
            normalized = _skill_roster.normalize_skill_roster_item(record)
            if normalized is None or normalized["skill_id"] in readable:
                continue
            readable.append(normalized["skill_id"])
        return readable

    def _apply_frontdoor_skill_roster_notice(
        self,
        messages: list[dict[str, Any]] | None,
        *,
        session_key: Any,
        session: Any = None,
    ) -> list[dict[str, Any]]:
        """检测 + 一次性投递，只在这一处发生（真正落请求体的装配路径上）。

        旧机制一条回合内四个装配点共用一次消耗式读，先跑的把差集领走、落请求体那处
        永远拿空，提示因此从未上线。这里同时把"读者唯一"钉成结构：其它装配点不再读台账。

        基座迁移也在这里：存量请求体里的 `## 长期记忆` 块被剥离，并把该会话的记忆游标
        置为未初始化，下一次投递用整份文档重建基座（落在尾部，而不是头部）。
        """
        base_messages = [
            dict(item)
            for item in list(messages or [])
            if isinstance(item, dict)
        ]
        stripped_base = any(is_memory_snapshot_message(item) for item in base_messages)
        if stripped_base:
            base_messages = [
                item for item in base_messages if not is_memory_snapshot_message(item)
            ]
        scope = self._frontdoor_skill_roster_notice_scope(session_key)
        if not scope:
            return base_messages
        if stripped_base:
            _skill_roster_notice.reset_memory_cursor(scope)
        normalized_session_key = str(session_key or "").strip()
        memory_provider = None
        if session is not None:
            memory_provider = lambda **kwargs: self._frontdoor_memory_notice_provider(  # noqa: E731
                session=session, **kwargs
            )
        message, delivered = _skill_roster_notice.deliver_for_scope(
            scope,
            _skill_roster_notice.readable_skill_items(
                self._frontdoor_live_visible_skills(session_key)
            ),
            now=now_iso(),
            # 头部钉住块那份名单算"已宣布"：否则第一跳会把整份名单重播一遍。
            # 钉住表按裸 session_key 分桶（`session:` 前缀只是台账的作用域写法），
            # 拿前缀键去查会取回空名单，把全部技能当成新增。
            covered_skill_ids=pinned_skill_ids_for(None, session_key=normalized_session_key),
            memory_provider=memory_provider,
        )
        if message is None:
            return base_messages, None
        self._emit_frontdoor_skill_roster_notice_event(session_key=session_key, records=delivered)
        return [*base_messages, message], message

    def _frontdoor_memory_notice_provider(
        self,
        *,
        since_rev: int,
        base_rev: int,
        initialized: bool = False,
        session: Any = None,
    ) -> dict[str, Any] | None:
        """长期记忆增量：与 skill 名单同一条通知消息、同一个游标。

        首次投递（`initialized=False`）或压缩后的重投，把整份文档作为基座放进尾部；
        其余跳只投 `rev` 之后的增量。基座不再注入 message index 1。
        """
        manager = getattr(self._loop, "memory_manager", None)
        if manager is None or not callable(getattr(manager, "memory_notice_delta", None)):
            return None
        try:
            latest = int(manager.latest_memory_revision() or 0)
        except Exception:
            return None
        if not initialized:
            document_text = str(manager.snapshot_text() or "").strip()
            if not document_text:
                return {"rev": latest, "from_rev": 0, "text": "", "rebase": False, "align": True}
            self._record_memory_base_provenance(
                session,
                manager=manager,
                reason=(
                    MEMORY_SNAPSHOT_ADOPTION_REBASE
                    if str(getattr(session, MEMORY_SNAPSHOT_ADOPTED_AT_ATTR, "") or "").strip()
                    else MEMORY_SNAPSHOT_ADOPTION_FIRST_REQUEST
                ),
            )
            return {"rev": latest, "from_rev": 0, "text": document_text, "rebase": True}
        if latest <= int(since_rev or 0):
            return {"rev": latest, "from_rev": int(since_rev), "text": "", "rebase": False}
        try:
            delta = manager.memory_notice_delta(since_rev=int(since_rev))
        except Exception:
            return None
        if isinstance(delta, dict) and delta.get("rebase"):
            self._record_memory_base_provenance(
                session,
                manager=manager,
                reason=MEMORY_SNAPSHOT_ADOPTION_REBASE,
            )
        return delta

    def _record_memory_base_provenance(
        self, session: Any, *, manager: Any, reason: str
    ) -> None:
        """把线上那份基座写进 artifact 的 `memory_snapshot` 溯源；失败只影响读数。"""
        if session is None:
            return
        try:
            adopt_memory_snapshot(session, memory_manager=manager, reason=reason)
        except Exception:
            return

    def _emit_frontdoor_skill_roster_notice_event(self, *, session_key: str, records: list[dict[str, Any]]) -> None:
        """会话流里画一条内部行（与心跳 ack 同一族 system 行），不是用户气泡。

        事件是实时信号，不进持久转录行：刷新后这行会像 `ceo.internal.ack` 一样消失，
        名单本身在 Skill 管理页照旧可见。模型侧那份持久提示不依赖这条事件。
        """
        if not records:
            return
        service = getattr(self._loop, "main_task_service", None)
        registry = getattr(service, "registry", None)
        publisher = getattr(registry, "publish_ceo", None)
        seq_of = getattr(registry, "next_ceo_seq", None)
        if not callable(publisher) or not callable(seq_of):
            return
        # 信封在 try 外面装配：装配本身出错（缺字段、名字拼错）必须炸出来，
        # 只有往外发那一步允许失败——否则这条内部行会永远静默消失。
        envelope = build_envelope(
            channel="ceo",
            session_id=str(session_key or "").strip(),
            seq=int(seq_of(str(session_key or "").strip())),
            type="ceo.skill.roster_notice",
            data={
                "items": [
                    {
                        "skill_id": str(record.get("skill_id") or "").strip(),
                        "display_name": str(record.get("display_name") or "").strip(),
                        "description": str(record.get("description") or "").strip(),
                    }
                    for record in list(records or [])
                    if str(record.get("skill_id") or "").strip()
                ]
            },
        )
        try:
            publisher(str(session_key or "").strip(), envelope)
        except Exception:
            # 显示面失败不许影响这一跳能发出去：提示本体已经在历史里了。
            pass

    def _carry_frontdoor_skill_roster_notice_on_compaction(
        self,
        messages: list[dict[str, Any]] | None,
        *,
        session_key: Any,
        compression_stage_id: str,
    ) -> list[dict[str, Any]]:
        """压缩跳：把仍可读的名字带到压缩产物之后，不可读的在那一跳退场。"""
        base_messages = list(messages or [])
        scope = self._frontdoor_skill_roster_notice_scope(session_key)
        stage_id = str(compression_stage_id or "").strip()
        if not scope or not base_messages or not stage_id:
            return base_messages
        message = _skill_roster_notice.carry_for_scope(
            scope,
            compression_stage_id=stage_id,
            readable_skill_ids=self._readable_frontdoor_skill_ids(session_key),
            now=now_iso(),
        )
        if message is None:
            return base_messages
        return [*base_messages, message]

    def _carry_manual_compression_notice_into_baseline(self, *, session: Any, session_key: str) -> None:
        """手动压缩跑在回合外，够不到发送路径那一次搬运；在消费待重印标记的这一跳补。

        补的是 durable 基线本身——下一跳的请求体由它派生，提示因此重新进上下文。
        """
        if not session_key:
            return
        baseline = [
            dict(item)
            for item in list(getattr(session, "_frontdoor_request_body_messages", []) or [])
            if isinstance(item, dict)
        ]
        if not baseline:
            return
        carried = self._carry_frontdoor_skill_roster_notice_on_compaction(
            baseline,
            session_key=session_key,
            compression_stage_id=(
                "manual:" + str(getattr(session, "_active_turn_id", "") or now_iso()).strip()
            ),
        )
        if len(carried) == len(baseline):
            return
        setattr(session, "_frontdoor_request_body_messages", list(carried))

    def _frontdoor_bundle_recommit_boundary(self, *, session: Any, state: CeoGraphState) -> bool:
        """`tools[]` 与 skill 头部名单共用的唯一重新提交点：压缩。

        换车道**不**重印：心跳/定时内部轮与外部轮共用同一份冻结名单，一次车道切换
        不该把身后整段正文的缓存打掉（用户决策，保缓存命中）。内部轮的工具面收窄由
        执行准入承担（派发字典 ∩ 当轮治理可见集），声明面保持冻结；skill 名单同理，
        全量名单跨车道共享。

        返回 True 的同一处递增 skill roster epoch：skill 头部名单与 `tools[]` 共用一套
        "什么时候允许变"的口径，两份名单在同一个跳上一起重印。
        """
        if bool(getattr(session, PENDING_PROVIDER_BUNDLE_RECOMMIT_ATTR, False)):
            setattr(session, PENDING_PROVIDER_BUNDLE_RECOMMIT_ATTR, False)
            bump_skill_roster_epoch(session, session_key=str(state.get("session_key") or ""))
            return True
        return False

    def _registered_tools_for_state(self, state: CeoGraphState) -> dict[str, Tool]:
        """执行束的工具实例表：按**可执行名单**（本轮 callable）取名。

        `tools[]` 钉死后声明面与准入面分源——provider schema 由
        `_frontdoor_tool_schemas_for_state` 按钉住的派发名单单独取实例（`declared_tools`），
        这里只放本轮真能调的名字。声明里未 load 的工具不在这里，调用会得到
        `tool not available` 与先 `load_tool_context` 的指引。
        """
        return self._registered_tools(
            self._frontdoor_provider_visible_tool_names(self._frontdoor_executable_tool_names(state))
        )

    def _frontdoor_provider_visible_tool_names(
        self,
        tool_names: list[str] | None,
    ) -> list[str]:
        normalized = self._normalized_tool_name_state_list(tool_names)
        tools_registry = getattr(self._loop, "tools", None)
        tool_lookup = getattr(tools_registry, "get", None)
        if not tools_registry or not callable(tool_lookup):
            return normalized
        provider_visible: list[str] = []
        for name in normalized:
            tool = tool_lookup(str(name or "").strip())
            if tool is None:
                continue
            try:
                _provider_visible_tool_contract(tool)
            except Exception:
                continue
            provider_visible.append(str(tool.name or name).strip())
        return self._normalized_tool_name_state_list(provider_visible) or normalized

    def _frontdoor_has_valid_stage(self, state: CeoGraphState | dict[str, Any] | None) -> bool:
        normalized_state = (
            state
            if isinstance(state, dict) and "frontdoor_stage_state" in state
            else {"frontdoor_stage_state": dict(state or {}) if isinstance(state, dict) else {}}
        )
        snapshot = self._frontdoor_stage_state_snapshot(normalized_state)
        return bool(str(snapshot.get("active_stage_id") or "").strip()) and not bool(snapshot.get("transition_required"))

    def _frontdoor_contract_presence_index(
        self,
        state: CeoGraphState | dict[str, Any] | None,
    ) -> dict[str, str]:
        """这一跳在场的契约 ``tool_id -> fingerprint``。

        判据只读**本轮请求视图**（`state['messages']` 去掉已裁撤阶段的那些行）里的
        `[TOOL_CONTRACT:…]` 块，不是消息里曾经出现过的全部历史：阶段肉身被裁撤后块
        离开请求，工具就该立刻不可调用；下一跳重新拿契约才还得回来。阶段台账里保留的正文
        **不算在场**（D9）——正文不在模型眼前就不算，第二载体正是「看得见名字却调不动」
        那类漂移的来源。

        前门的 callable 在三个算点各自重算（装配路的 message_builder、
        `_refresh_prompt_cache_state`、发送预检），所以判据必须收在这一个方法里，三个算点
        各自调它——只在装配层过滤等于没写（`tool-and-skill-system.md` 里「合同分裂」那条教训）。

        不缓存：前门的 `state` 是 session 的持久状态、跨跳活着，把这份跳级字典挂在它上面
        下一跳就读到陈旧结果；200×6KB 的视图重算实测 ~0.17 ms，每条装配路各算一次。
        """
        view = self._frontdoor_view_without_evicted_stage_rows(
            state, list((state or {}).get("messages") or [])
        )
        return contract_presence_index(request_messages=view)

    def _frontdoor_contract_present_names(
        self,
        state: CeoGraphState | dict[str, Any] | None,
    ) -> list[str]:
        """在场 ``tool_id`` 的列表：前门每条装配路唯一的在场权威，callable 与 candidate 都只认它。"""
        return list(self._frontdoor_contract_presence_index(state))

    def _frontdoor_view_without_evicted_stage_rows(
        self,
        state: CeoGraphState | dict[str, Any] | None,
        messages: list[Any],
    ) -> list[Any]:
        """把"属于已裁撤阶段"的工具结果行从判据视图里剔除。

        实盘（web:ceo-09057e72cac8）暴露的是两份视图不同源：裁撤发生在**请求体重建**
        （`_trim_frontdoor_seed_stage_compaction`）时，而 `state["messages"]` 还是裁撤前那份，
        于是判据读得到正文 ⇒ 名字仍被当在场、继续留在 callable；渲染却用裁切后的视图 ⇒
        模型眼前已经没有契约，执行照旧放行。

        这里不依赖调用方交来哪一份视图：按阶段账本自己把裁撤阶段的 call id 集合摘出来，
        成对剔除这些行。剔除认的是**行**（`tool_call_id`）而不是 loader 名字——冲动调用
        投进的契约块同样属于那条阶段，裁走时一起走；裁走即离场，下一次调用重新拿契约
        （D9 之后没有"保留正文"这第二载体，撑住能力的只有眼前这份块）。
        """
        try:
            from g3ku.runtime.stage_prompt_compaction import extract_call_id, stage_round_call_ids
        except Exception:
            return list(messages or [])
        if not isinstance(state, dict):
            return list(messages or [])
        snapshot = self._frontdoor_stage_state_snapshot(state)
        evicted_call_ids: set[str] = set()
        for stage in list(snapshot.get("stages") or []):
            if not isinstance(stage, dict):
                continue
            if stage.get("context_evicted") is not True:
                continue
            evicted_call_ids |= {str(cid or "").strip() for cid in stage_round_call_ids(stage) if str(cid or "").strip()}
        if not evicted_call_ids:
            return list(messages or [])
        filtered: list[Any] = []
        for message in list(messages or []):
            if isinstance(message, dict) and str(message.get("role") or "").strip().lower() == "tool":
                call_id = str(extract_call_id(message.get("tool_call_id")) or "").strip()
                if call_id and call_id in evicted_call_ids:
                    continue
            filtered.append(message)
        return filtered

    @staticmethod
    def _frontdoor_repair_required_tool_name_set(
        state: CeoGraphState | dict[str, Any] | None,
    ) -> set[str]:
        """state 上带 `repair_required_tool_items` 时，这一档既不进 callable 也不进 candidate。

        这份键只有装配把回合级 context sources 挂进 state 时才有；取不到就是空集，那时
        待修复工具仍可能凭一个旧契约块进 callable——执行侧照样被 `RepairRequiredTool`
        拒掉并给出修复原因，所以这是"少一层提前收窄"，不是无契约放行。
        """
        if not isinstance(state, dict):
            return set()
        return {
            str(item.get("tool_id") or "").strip()
            for item in list(state.get("repair_required_tool_items") or [])
            if isinstance(item, dict) and str(item.get("tool_id") or "").strip()
        }

    def _frontdoor_candidate_tool_view(
        self,
        state: CeoGraphState | dict[str, Any] | None,
    ) -> list[str]:
        """本回合生效的候选视图 = 治理可见集 −（本轮 callable ∪ 待修复）。

        三个消费点（提升门禁 runtime context、尾块渲染、重复读守卫）必须读同一份，
        否则会出现"合同里看得见、门禁说不在候选"的分裂。

        契约缺席的名字不需要一条单独的"并回"规则：不在场就进不了 callable，同一个减法
        在这一刻把它留在候选里——「既不可调也不可读」的第四态在这份表达式下构造不出来。
        """
        if not isinstance(state, dict):
            return []
        granted = self._normalized_tool_name_state_list(
            list(state.get("rbac_visible_tool_names") or [])
            or [
                *list(state.get("tool_names") or []),
                *list(state.get("candidate_tool_names") or []),
            ]
        )
        excluded = self._frontdoor_callable_tool_names_for_state(state)
        excluded_set = set(excluded) | self._frontdoor_repair_required_tool_name_set(state)
        return [name for name in granted if name not in excluded_set]

    def _frontdoor_callable_tool_names_for_state(
        self,
        state: CeoGraphState | dict[str, Any] | None,
        *,
        tool_names: list[str] | None = None,
    ) -> list[str]:
        normalized_state = state if isinstance(state, dict) else {}
        pool = self._normalized_tool_name_state_list(
            tool_names
            if tool_names is not None
            else list(normalized_state.get("tool_names") or [])
        )
        # 治理可见集优先，取不到才退回本轮 callable 快照（沿用既有约定）。
        granted = self._normalized_tool_name_state_list(
            list(normalized_state.get("rbac_visible_tool_names") or [])
        ) or pool
        present_set = set(self._frontdoor_contract_present_names(normalized_state))
        repair_required_set = self._frontdoor_repair_required_tool_name_set(normalized_state)
        # callable = 常驻内置 ∪（治理可见 ∩ 契约在场 ∩ 非待修复）。常驻内置不交在场判据：
        # 它们没有 toolskill，契约块永远不会出现在请求里（exec / loader / 控制信号）。
        normalized: list[str] = []
        seen: set[str] = set()
        for name in [*pool, *granted]:
            if not name or name in seen:
                continue
            seen.add(name)
            if name in CEO_FIXED_BUILTIN_TOOL_NAMES:
                normalized.append(name)
                continue
            if name in present_set and name not in repair_required_set:
                normalized.append(name)
        # 静默收尾信号恒可调用：它是回合收尾合同的一部分，不随阶段态或曝光层而消失。
        # 缺了它模型只剩「把正文写短一点」这一种伪静默手段。
        if SILENT_TOOL_NAME not in normalized:
            normalized = [*normalized, SILENT_TOOL_NAME]
        if isinstance(state, dict) and (
            bool(state.get("cron_internal")) or bool(state.get("heartbeat_internal"))
        ):
            # Internal turns must keep the stage tool callable/visible even
            # without an active stage, otherwise the execution gate (which
            # only allows the stage tool before a stage exists) and the
            # exposure disagree and the turn cannot use any tool at all.
            if STAGE_TOOL_NAME not in normalized:
                return [*normalized, STAGE_TOOL_NAME]
            return normalized
        if self._frontdoor_has_valid_stage(state):
            return normalized
        # 新协议:无有效阶段时不再把 callable 收窄到只剩 sns —— 普通工具可调,
        # 但必须与 sns 同批提交;单独调用普通工具会被宽限执行一次,再次违规才硬拦。
        # 阶段规则仍在执行期由 stage_gate_error_for_tool / free-pass 判定兜底。
        if STAGE_TOOL_NAME not in normalized:
            return [STAGE_TOOL_NAME, *normalized]
        return normalized

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
        # 与 callable 侧同一份常驻合同：provider schema 里也要恒定出现，
        # 否则模型看见了名字却调不动（或反之，两种都造成静默失败）。
        if SILENT_TOOL_NAME not in normalized:
            normalized = [*normalized, SILENT_TOOL_NAME]
        if isinstance(state, dict) and (
            bool(state.get("cron_internal")) or bool(state.get("heartbeat_internal"))
        ):
            if STAGE_TOOL_NAME not in normalized:
                return [*normalized, STAGE_TOOL_NAME]
            return normalized
        if STAGE_TOOL_NAME not in normalized:
            return [*normalized, STAGE_TOOL_NAME]
        return normalized

    def _selected_tool_schemas(self, tool_names: list[str] | None) -> list[dict[str, Any]]:
        """按传入顺序渲染 provider `tools[]`：活实例优先，删掉的名字取删除前快照。

        清单钉住的是名字，`tools[]` 却是按实例现渲的。工具被删除后实例没了，如果这里
        直接跳过，这一跳的工具表就比上一跳少一条——可复用前缀从工具表那一截整段断开，
        等于让一次删除把身后正文的缓存全部重付。权限收回不会断是因为实例还在、参数表
        由声明侧继续供着；删除要享受同一条待遇，就只能用删除前抄下来的那份 schema。
        快照见 `g3ku/resources/retired_schemas.py`，重印点一过名字离开清单，它自然不再被取用。
        """
        schemas: list[dict[str, Any]] = []
        for name in self._normalized_tool_name_state_list(tool_names):
            tool = self._loop.tools.get(str(name or "").strip())
            if tool is None:
                retired = lookup_tool_schema(name)
                if not isinstance(retired, dict):
                    continue
                # 用同一个投影函数渲染，字节才与删除前那一跳完全一致。
                tool = SimpleNamespace(
                    name=name,
                    description="",
                    parameters={},
                    to_model_schema=lambda payload=retired: payload,
                )
            try:
                description, parameters = _provider_visible_tool_contract(tool)
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": getattr(tool, "name", "") or name,
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

    @staticmethod
    def _upsert_message_metadata(message: dict[str, Any], metadata: dict[str, Any] | None) -> dict[str, Any]:
        record = dict(message or {})
        payload = dict(record.get("metadata") or {}) if isinstance(record.get("metadata"), dict) else {}
        for key, value in dict(metadata or {}).items():
            if value in (None, "", [], {}):
                continue
            payload[str(key)] = value
        if payload:
            record["metadata"] = payload
        return record

    @classmethod
    def _tag_last_matching_user_message(
        cls,
        messages: list[dict[str, Any]] | None,
        *,
        content_text: str,
        metadata: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        tagged = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        needle = str(content_text or "").strip()
        if not tagged or not needle or not metadata:
            return tagged
        for index in range(len(tagged) - 1, -1, -1):
            message = dict(tagged[index] or {})
            if str(message.get("role") or "").strip().lower() != "user":
                continue
            if str(message.get("content") or "").strip() != needle:
                continue
            tagged[index] = cls._upsert_message_metadata(message, metadata)
            break
        return tagged

    @classmethod
    def _internal_prompt_seed_messages(
        cls,
        *,
        metadata: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None]:
        heartbeat_internal = bool(metadata.get("heartbeat_internal"))
        cron_internal = bool(metadata.get("cron_internal"))
        source = "cron" if cron_internal else "heartbeat" if heartbeat_internal else ""
        if not source:
            return [], "", None
        seed_messages: list[dict[str, Any]] = []
        event_bundle_text = ""
        event_metadata: dict[str, Any] | None = None
        if heartbeat_internal:
            stable_rules_text = str(metadata.get("heartbeat_stable_rules_text") or "").strip()
            if stable_rules_text:
                seed_messages.append(
                    {
                        "role": "user",
                        "content": stable_rules_text,
                        "metadata": _hidden_internal_prompt_message_metadata(
                            source=source,
                            internal_prompt_kind="heartbeat_rule",
                        ),
                    }
                )
            event_bundle_text = str(metadata.get("heartbeat_event_bundle_text") or "").strip()
            if event_bundle_text:
                event_metadata = _hidden_internal_prompt_message_metadata(
                    source=source,
                    internal_prompt_kind="heartbeat_event_bundle",
                )
        elif cron_internal:
            cron_job_id = str(metadata.get("cron_job_id") or "").strip()
            cron_system_message = CeoFrontDoorSupport._cron_internal_system_message(metadata)
            if isinstance(cron_system_message, dict) and str(cron_system_message.get("content") or "").strip():
                seed_messages.append(
                    {
                        "role": "user",
                        "content": str(cron_system_message.get("content") or "").strip(),
                        "metadata": _hidden_internal_prompt_message_metadata(
                            source=source,
                            internal_prompt_kind="cron_rule",
                            extra={"cron_job_id": cron_job_id},
                        ),
                    }
                )
            cron_event_message = CeoFrontDoorSupport._cron_internal_event_message(
                metadata,
                reminder_text=str(metadata.get("cron_reminder_text") or "").strip(),
            )
            if isinstance(cron_event_message, dict) and str(cron_event_message.get("content") or "").strip():
                seed_messages.append(
                    {
                        "role": "user",
                        "content": str(cron_event_message.get("content") or "").strip(),
                        "metadata": _hidden_internal_prompt_message_metadata(
                            source=source,
                            internal_prompt_kind="cron_event_bundle",
                            extra={"cron_job_id": cron_job_id},
                        ),
                    }
                )
        return seed_messages, event_bundle_text, event_metadata

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
            "pending_orphan_rounds": [],
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
    def _normalized_tool_name_state_list(raw: Any) -> list[str]:
        normalized: list[str] = []
        for item in list(raw or []):
            name = str(item or "").strip()
            if name and name not in normalized:
                normalized.append(name)
        return normalized

    @staticmethod
    def _normalized_candidate_tool_items(raw: Any, *, fallback_names: list[str] | None = None) -> list[dict[str, str]]:
        return normalize_frontdoor_candidate_tool_items(raw, fallback_names=fallback_names)

    def _current_load_tool_context_payload_for_frontdoor(
        self,
        *,
        requested_tool_id: str,
        runtime_context: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        main_service = getattr(self._loop, "main_task_service", None)
        if main_service is None:
            return None
        actor_role = str(dict(runtime_context or {}).get("actor_role") or "ceo").strip().lower() or "ceo"
        session_id = (
            str(dict(runtime_context or {}).get("session_key") or dict(runtime_context or {}).get("session_id") or "").strip()
            or "web:shared"
        )
        if hasattr(main_service, "load_tool_context_v2"):
            payload = main_service.load_tool_context_v2(
                actor_role=actor_role,
                session_id=session_id,
                tool_id=str(requested_tool_id or "").strip(),
            )
        else:
            payload = main_service.load_tool_context(
                actor_role=actor_role,
                session_id=session_id,
                tool_id=str(requested_tool_id or "").strip(),
            )
        if not isinstance(payload, dict) or not bool(payload.get("ok")):
            return None
        return apply_runtime_tool_context_projection(
            payload,
            requested_tool_id=str(requested_tool_id or "").strip(),
            runtime=runtime_context,
        )

    @staticmethod
    def _frontdoor_contract_fingerprint_index(
        messages: list[dict[str, Any]] | None,
    ) -> dict[str, str]:
        """在场契约的 ``tool_id -> fingerprint``，与 callable 收窄共用同一个判据函数。

        两处必须同判据，否则裁撤后重读被放行、同一份正文在上下文里出现两次。阶段台账里
        保留的正文不算在场（D9）——判据只认模型眼前那份块。
        """
        return contract_presence_index(request_messages=list(messages or []))

    @staticmethod
    def _load_tool_context_duplicate_repair_text() -> str:
        return (
            "Error: 上下文中已有该工具当前版本的未压缩 toolskill，禁止重复读取！"
            "请直接复用已有说明，或在工具状态变化/旧内容被压缩后再重试。"
        )

    async def _frontdoor_load_tool_context_duplicate_error(
        self,
        *,
        payload: dict[str, Any],
        state: dict[str, Any],
        runtime_context: dict[str, Any] | None,
    ) -> str:
        tool_name = str(payload.get("name") or "").strip()
        if tool_name not in {"load_tool_context", "load_tool_context_v2"}:
            return ""
        arguments = dict(payload.get("arguments") or {})
        requested_tool_id = str(arguments.get("tool_id") or "").strip()
        if not requested_tool_id or str(arguments.get("search_query") or "").strip():
            return ""
        candidate_tool_names = self._frontdoor_candidate_tool_view(state)
        if requested_tool_id in set(candidate_tool_names):
            return ""
        callable_tool_names = self._normalized_tool_name_state_list(state.get("tool_names"))
        contract_present_tool_names = self._frontdoor_contract_present_names(state)
        visible_tool_names = self._normalized_tool_name_state_list(state.get("rbac_visible_tool_names"))
        if requested_tool_id not in set(visible_tool_names):
            return ""
        if (
            requested_tool_id not in set(callable_tool_names)
            and requested_tool_id not in set(contract_present_tool_names)
        ):
            return ""
        effective_runtime_context = {
            **dict(runtime_context or {}),
            "candidate_tool_names": list(candidate_tool_names),
            "callable_tool_names": list(callable_tool_names),
            "contract_present_tool_names": list(contract_present_tool_names),
            "rbac_visible_tool_names": list(visible_tool_names),
        }
        current_payload = self._current_load_tool_context_payload_for_frontdoor(
            requested_tool_id=requested_tool_id,
            runtime_context=effective_runtime_context,
        )
        if not isinstance(current_payload, dict) or not bool(current_payload.get("ok")):
            return ""
        resolved_tool_id = str(current_payload.get("tool_id") or requested_tool_id).strip()
        current_fingerprint = str(current_payload.get("tool_context_fingerprint") or "").strip()
        if not current_fingerprint:
            return ""
        latest_fingerprint = self._frontdoor_contract_fingerprint_index(
            list(state.get("messages") or [])
        ).get(resolved_tool_id)
        if not latest_fingerprint:
            return ""
        if str(latest_fingerprint).strip() != current_fingerprint:
            return ""
        return self._load_tool_context_duplicate_repair_text()

    @classmethod
    def _runtime_session_frontdoor_state(
        cls,
        state: CeoGraphState | None,
        *,
        preview_pending_tool_round: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
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
        return (
            frontdoor_stage_state,
            frontdoor_canonical_context,
            compression_state,
            {},
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

    def _inherited_internal_turn_contract_state(
        self,
        *,
        state: CeoGraphState | None,
        session: Any | None,
    ) -> dict[str, Any]:
        if not isinstance(state, dict):
            return {}
        tool_names = self._normalized_tool_name_state_list(state.get("tool_names"))
        if not tool_names:
            return {}
        provider_tool_names = (
            self._normalized_tool_name_state_list(state.get("provider_tool_names"))
            or list(tool_names)
        )
        candidate_tool_names = self._frontdoor_candidate_tool_view(state)
        candidate_tool_items = self._normalized_candidate_tool_items(
            state.get("candidate_tool_items"),
            fallback_names=candidate_tool_names,
        )
        attachment_reopen_targets = [
            dict(item)
            for item in list(
                state.get("attachment_reopen_targets")
                or getattr(session, "_frontdoor_attachment_reopen_targets", [])
                or []
            )
            if isinstance(item, dict)
        ]
        visible_skill_ids = self._normalized_tool_name_state_list(
            state.get("visible_skill_ids")
            or getattr(session, "_frontdoor_visible_skill_ids", [])
        )
        candidate_skill_ids = (
            self._normalized_tool_name_state_list(state.get("candidate_skill_ids"))
            or list(visible_skill_ids)
        )
        rbac_visible_tool_names = (
            self._normalized_tool_name_state_list(
                state.get("rbac_visible_tool_names")
                or getattr(session, "_frontdoor_visible_tool_ids", [])
            )
            or list(provider_tool_names)
        )
        rbac_visible_skill_ids = (
            self._normalized_tool_name_state_list(
                state.get("rbac_visible_skill_ids")
                or getattr(session, "_frontdoor_visible_skill_ids", [])
            )
            or list(visible_skill_ids)
        )
        selection_debug = self._frontdoor_selection_debug_snapshot(state)
        if not selection_debug:
            raw_debug = getattr(session, "_frontdoor_selection_debug", None)
            selection_debug = dict(raw_debug) if isinstance(raw_debug, dict) else {}
        cache_family_revision = (
            str(state.get("cache_family_revision") or "").strip()
            or str(getattr(session, "_frontdoor_capability_snapshot_exposure_revision", "") or "").strip()
            or DEFAULT_CACHE_FAMILY_REVISION
        )
        provider_tool_exposure_revision = str(
            state.get("provider_tool_exposure_revision") or ""
        ).strip() or self._provider_tool_exposure_revision(provider_tool_names)
        repair_required_tool_items = [
            dict(item)
            for item in list(state.get("repair_required_tool_items") or [])
            if isinstance(item, dict)
        ]
        if not repair_required_tool_items:
            repair_required_tool_items = [
                dict(item)
                for item in list(getattr(session, "_frontdoor_repair_required_tool_items", []) or [])
                if isinstance(item, dict)
            ]
        repair_required_skill_items = [
            dict(item)
            for item in list(state.get("repair_required_skill_items") or [])
            if isinstance(item, dict)
        ]
        if not repair_required_skill_items:
            repair_required_skill_items = [
                dict(item)
                for item in list(getattr(session, "_frontdoor_repair_required_skill_items", []) or [])
                if isinstance(item, dict)
            ]
        return {
            "tool_names": list(tool_names),
            "provider_tool_names": list(provider_tool_names),
            "pending_provider_tool_names": [],
            "provider_tool_exposure_pending": False,
            "provider_tool_exposure_revision": provider_tool_exposure_revision,
            "provider_tool_exposure_commit_reason": "",
            "candidate_tool_names": list(candidate_tool_names),
            "candidate_tool_items": list(candidate_tool_items),
            "attachment_reopen_targets": list(attachment_reopen_targets),
            "visible_skill_ids": list(visible_skill_ids),
            "candidate_skill_ids": list(candidate_skill_ids),
            "rbac_visible_tool_names": list(rbac_visible_tool_names),
            "rbac_visible_skill_ids": list(rbac_visible_skill_ids),
            "cache_family_revision": cache_family_revision,
            "frontdoor_selection_debug": dict(selection_debug),
            "repair_required_tool_items": list(repair_required_tool_items),
            "repair_required_skill_items": list(repair_required_skill_items),
        }

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

    async def _emit_frontdoor_stage_sync_event(self, *, runtime: CeoRuntime) -> None:
        """Notify CEO websocket subscribers that the session's frontdoor stage
        state was just refreshed, so they can push a live ceo.turn.patch.

        Stage/canonical context on the session object only updates at graph-node
        boundaries; without this event the web UI learns about a newly started
        stage only on the next tool event, and with an empty delta it wipes the
        timeline instead.
        """
        session = getattr(getattr(runtime, "context", None), "session", None)
        emit = getattr(session, "_emit", None)
        if not callable(emit):
            return
        try:
            await emit("frontdoor_stage_synced")
        except Exception:
            logger.opt(exception=True).warning("Failed to emit frontdoor_stage_synced event")

    def _sync_runtime_session_frontdoor_state(
        self,
        *,
        state: CeoGraphState | None,
        runtime: CeoRuntime | None = None,
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
        ) = self._runtime_session_frontdoor_state(
            state,
            preview_pending_tool_round=preview_pending_tool_round,
        )
        setattr(target_session, "_frontdoor_stage_state", frontdoor_stage_state)
        setattr(target_session, "_frontdoor_canonical_context", frontdoor_canonical_context)
        # 会话自己判"这个 running 还有没有活的宿主"：被放弃的那一轮会按图状态把旧的
        # running 覆盖回来，不挡在这里就会一路传到快照与连续性文件，界面上那条线就没人收。
        adopt = getattr(target_session, "_adopt_compression_state", None)
        setattr(
            target_session,
            "_compression_state",
            adopt(compression_state) if callable(adopt) else compression_state,
        )
        setattr(target_session, "_semantic_context_state", {})
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
        setattr(
            target_session,
            "_frontdoor_repair_required_tool_items",
            [
                dict(item)
                for item in list(state.get("repair_required_tool_items") or [])
                if isinstance(item, dict)
            ],
        )
        setattr(
            target_session,
            "_frontdoor_repair_required_skill_items",
            [
                dict(item)
                for item in list(state.get("repair_required_skill_items") or [])
                if isinstance(item, dict)
            ],
        )
        setattr(
            target_session,
            "_frontdoor_attachment_reopen_targets",
            [
                dict(item)
                for item in list(state.get("attachment_reopen_targets") or [])
                if isinstance(item, dict)
            ],
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
            # 基线持久化统一走 durable 归一（剥工具契约/瞬时件/多模态），
            # 与守卫对比侧、新回合种子保持同一形态，避免契约块漏进基线。
            request_body_messages = self._durable_frontdoor_request_body_messages(
                [
                    dict(item)
                    for item in list(state.get("frontdoor_request_body_messages") or [])
                    if isinstance(item, dict)
                ]
            )
            if not request_body_messages:
                raw_messages = [
                    dict(item)
                    for item in list(state.get("messages") or [])
                    if isinstance(item, dict)
                ]
                if raw_messages:
                    request_body_messages = self._durable_frontdoor_request_body_messages(raw_messages)
            heartbeat_internal = bool(state.get("heartbeat_internal"))
            cron_internal = bool(state.get("cron_internal"))
            frontdoor_history_shrink_reason = str(state.get("frontdoor_history_shrink_reason") or "").strip()
            existing_request_body_messages = self._existing_frontdoor_request_body_reference(target_session)
            baseline_sync_decision = "allowed"
            should_apply_request_body_messages = has_authoritative_actual_request and (
                "frontdoor_request_body_messages" in state or request_body_messages
            )
            if should_apply_request_body_messages and self._looks_like_internal_only_heartbeat_regression(
                candidate_messages=request_body_messages,
                reference_messages=existing_request_body_messages,
                shrink_reason=frontdoor_history_shrink_reason,
                heartbeat_internal=heartbeat_internal,
                cron_internal=cron_internal,
            ):
                should_apply_request_body_messages = False
                baseline_sync_decision = "blocked_internal_only_regression"
            setattr(target_session, "_frontdoor_baseline_sync_decision", baseline_sync_decision)
            if should_apply_request_body_messages:
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
                if baseline_sync_decision == "blocked_internal_only_regression":
                    should_sync_continuity = False
                if should_sync_continuity:
                    sync_completed_continuity(
                        source_reason=(
                            "actual_request_sync" if incoming_has_authoritative_actual_request else "finalize"
                        ),
                        internal_turn=bool(heartbeat_internal or cron_internal),
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

    @staticmethod
    def _frontdoor_message_text_for_regression_guard(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str) and block.strip():
                    parts.append(block.strip())
                    continue
                if not isinstance(block, dict):
                    continue
                text_value = block.get("text", block.get("content", ""))
                if isinstance(text_value, str) and text_value.strip():
                    parts.append(text_value.strip())
            return "\n".join(parts).strip()
        return ""

    @classmethod
    def _frontdoor_messages_have_prefix(
        cls,
        candidate_messages: list[dict[str, Any]] | None,
        reference_messages: list[dict[str, Any]] | None,
    ) -> bool:
        candidate = [
            dict(item)
            for item in list(candidate_messages or [])
            if isinstance(item, dict)
        ]
        reference = [
            dict(item)
            for item in list(reference_messages or [])
            if isinstance(item, dict)
        ]
        if not candidate or not reference or len(candidate) < len(reference):
            return False
        return candidate[: len(reference)] == reference

    @classmethod
    def _frontdoor_internal_only_message_count(cls, messages: list[dict[str, Any]] | None) -> int:
        markers = (
            "This is a background heartbeat.",
            "## EVENT BUNDLE",
            "# Heartbeat Rules",
        )
        count = 0
        for item in list(messages or []):
            if not isinstance(item, dict):
                continue
            if str(item.get("role") or "").strip().lower() != "user":
                continue
            text = cls._frontdoor_message_text_for_regression_guard(item.get("content"))
            if not text:
                continue
            if any(marker in text for marker in markers):
                continue
            count += 1
        return count

    @classmethod
    def _frontdoor_message_weight_for_regression_guard(
        cls,
        messages: list[dict[str, Any]] | None,
    ) -> tuple[int, int, int]:
        items = [
            dict(item)
            for item in list(messages or [])
            if isinstance(item, dict)
        ]
        total_chars = sum(
            len(cls._frontdoor_message_text_for_regression_guard(item.get("content"))) for item in items
        )
        ordinary_user_messages = cls._frontdoor_internal_only_message_count(items)
        return len(items), ordinary_user_messages, total_chars

    @classmethod
    def _looks_like_internal_only_heartbeat_regression(
        cls,
        *,
        candidate_messages: list[dict[str, Any]] | None,
        reference_messages: list[dict[str, Any]] | None,
        shrink_reason: str,
        heartbeat_internal: bool,
        cron_internal: bool,
    ) -> bool:
        if not (heartbeat_internal or cron_internal):
            return False
        if shrink_reason in {"token_compression", "stage_compaction"}:
            return False
        candidate = [
            dict(item)
            for item in list(candidate_messages or [])
            if isinstance(item, dict)
        ]
        reference = [
            dict(item)
            for item in list(reference_messages or [])
            if isinstance(item, dict)
        ]
        if not candidate or not reference:
            return False
        if cls._frontdoor_messages_have_prefix(candidate, reference):
            return False
        first_text = cls._frontdoor_message_text_for_regression_guard(candidate[0].get("content"))
        first_user_text = ""
        for item in candidate:
            if str(item.get("role") or "").strip().lower() == "user":
                first_user_text = cls._frontdoor_message_text_for_regression_guard(item.get("content"))
                break
        markers = (
            "This is a background heartbeat.",
            "## EVENT BUNDLE",
            "# Heartbeat Rules",
        )
        if not any(marker in first_text or marker in first_user_text for marker in markers):
            return False
        candidate_weight = cls._frontdoor_message_weight_for_regression_guard(candidate)
        reference_weight = cls._frontdoor_message_weight_for_regression_guard(reference)
        return candidate_weight < reference_weight

    @classmethod
    def _frontdoor_request_body_messages_from_actual_request_record(
        cls,
        request_path: str,
    ) -> list[dict[str, Any]]:
        path = Path(str(request_path or "").strip())
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        raw_messages = [
            dict(item)
            for item in list(payload.get("request_messages") or payload.get("frontdoor_request_body_messages") or [])
            if isinstance(item, dict)
        ]
        if not raw_messages:
            return []
        return cls._durable_frontdoor_request_body_messages(raw_messages)

    @classmethod
    def _existing_frontdoor_request_body_reference(cls, session: Any) -> list[dict[str, Any]]:
        baseline = [
            dict(item)
            for item in list(getattr(session, "_frontdoor_request_body_messages", []) or [])
            if isinstance(item, dict)
        ]
        if baseline:
            return baseline
        actual_request_path = str(getattr(session, "_frontdoor_actual_request_path", "") or "").strip()
        if actual_request_path:
            artifact_messages = cls._frontdoor_request_body_messages_from_actual_request_record(actual_request_path)
            if artifact_messages:
                return artifact_messages
        actual_request_history = [
            dict(item)
            for item in list(getattr(session, "_frontdoor_actual_request_history", []) or [])
            if isinstance(item, dict)
        ]
        if actual_request_history:
            artifact_messages = cls._frontdoor_request_body_messages_from_actual_request_record(
                str(actual_request_history[-1].get("path") or "").strip()
            )
            if artifact_messages:
                return artifact_messages
        return []

    def _persist_frontdoor_actual_request(
        self,
        *,
        state: CeoGraphState,
        runtime: CeoRuntime,
        request_messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None,
        prompt_cache_key: str,
        prompt_cache_diagnostics: dict[str, Any],
        parallel_tool_calls: bool | None,
        provider_request_meta: dict[str, Any] | None = None,
        provider_request_body: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        provider_request_started_at: str = "",
        request_kind: str = "frontdoor_actual_request",
        request_lane: str = "visible_frontdoor",
    ) -> dict[str, Any]:
        session_key = str(state.get("session_key") or getattr(getattr(runtime, "context", None), "session_key", "") or "").strip()
        if not session_key:
            return {}
        target_session = getattr(getattr(runtime, "context", None), "session", None)
        # durable 基线唯一前进点也是"活跃"的唯一读数点：空闲刷新边界按它判"多久没发过"。
        if target_session is not None:
            setattr(
                target_session,
                FRONTDOOR_LAST_SEND_AT_ATTR,
                str(provider_request_started_at or "").strip() or now_iso(),
            )
        # 这里是 durable 基线唯一的前进点，阶段收口跟着它一起落地：请求体里那份摘要块
        # 自带收口水位线（`stage_archive.archived_through_created_at`）。压缩算完但没走到
        # 这一步的回合（发送失败、压缩后被暂停）不翻任何标记，下一轮照旧渲染块，不会丢历史。
        try:
            self._frontdoor_hide_summarized_stages(target_session, request_messages)
        except Exception:
            logger.debug("frontdoor stage archive apply failed for {}", session_key)
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
            request_kind=request_kind,
            request_lane=request_lane,
            provider_request_started_at=provider_request_started_at,
            memory_snapshot=memory_snapshot_provenance(target_session),
        )
        if target_session is not None:
            restore_source = str(getattr(target_session, "_frontdoor_restore_source", "none") or "none").strip() or "none"
            if restore_source != "none":
                payload["frontdoor_restore_source"] = restore_source
            baseline_sync_decision = str(
                getattr(target_session, "_frontdoor_baseline_sync_decision", "") or ""
            ).strip()
            if baseline_sync_decision:
                payload["frontdoor_baseline_sync_decision"] = baseline_sync_decision
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
        authoritative_request_body_messages = self._durable_frontdoor_request_body_messages(request_messages)
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
            # 基线每前进一次换一代：这是 durable 基线唯一的前进点，手动压缩据此判断
            # 自己读到的那条基线是否已被别的（可能在跑的）回合顶掉。
            setattr(
                target_session,
                "_frontdoor_baseline_revision",
                int(getattr(target_session, "_frontdoor_baseline_revision", 0) or 0) + 1,
            )
            setattr(target_session, "_frontdoor_prompt_cache_key_hash", str(record.get("prompt_cache_key_hash") or "").strip())
            setattr(target_session, "_frontdoor_actual_request_hash", str(record.get("actual_request_hash") or "").strip())
            setattr(target_session, "_frontdoor_actual_request_message_count", int(record.get("actual_request_message_count") or 0))
            setattr(target_session, "_frontdoor_actual_tool_schema_hash", str(record.get("actual_tool_schema_hash") or "").strip())
            usage_payload = dict(payload.get("usage") or {}) if isinstance(payload, dict) else {}
            reported_thinking = "thinking_tokens" in usage_payload
            if turn_id and (
                reported_thinking
                or any(
                    int(usage_payload.get(field) or 0)
                    for field in ("input_tokens", "output_tokens", "cache_hit_tokens")
                )
            ):
                turn_usage = getattr(target_session, "_frontdoor_turn_usage", None)
                if not isinstance(turn_usage, dict):
                    turn_usage = {}
                    setattr(target_session, "_frontdoor_turn_usage", turn_usage)
                entry = turn_usage.get(turn_id)
                if not isinstance(entry, dict):
                    entry = {"input_tokens": 0, "output_tokens": 0, "cache_hit_tokens": 0, "call_count": 0}
                entry["input_tokens"] = int(entry.get("input_tokens") or 0) + int(usage_payload.get("input_tokens") or 0)
                entry["output_tokens"] = int(entry.get("output_tokens") or 0) + int(usage_payload.get("output_tokens") or 0)
                entry["cache_hit_tokens"] = int(entry.get("cache_hit_tokens") or 0) + int(usage_payload.get("cache_hit_tokens") or 0)
                entry["call_count"] = int(entry.get("call_count") or 0) + 1
                if reported_thinking:
                    # 只在 provider 明确上报时累加：缺失时不写键，界面据此保留「~」估算标记。
                    entry["thinking_tokens"] = int(entry.get("thinking_tokens") or 0) + int(usage_payload.get("thinking_tokens") or 0)
                turn_usage[turn_id] = entry
                trim = getattr(target_session, "_bound_turn_usage_cache", None)
                if callable(trim):
                    trim()
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
            "provider_tool_exposure_revision": str(record.get("provider_tool_exposure_revision") or "").strip(),
            "provider_tool_exposure_commit_reason": str(
                record.get("provider_tool_exposure_commit_reason") or ""
            ).strip(),
            "frontdoor_token_preflight_diagnostics": copy.deepcopy(frontdoor_token_preflight_diagnostics),
        }

    @staticmethod
    def _frontdoor_observed_input_truth(
        *,
        usage: dict[str, Any] | None,
        provider_model: str,
        actual_request_hash: str,
        fallback_estimated_input_tokens: int = 0,
    ) -> dict[str, Any]:
        normalized_usage = normalize_usage_payload(usage)
        truth = None
        if normalized_usage:
            candidate_truth = build_runtime_observed_input_truth(
                usage=normalized_usage,
                provider_model=str(provider_model or "").strip(),
                actual_request_hash=str(actual_request_hash or "").strip(),
                source="provider_usage",
            )
            if int(candidate_truth.input_tokens or 0) > 0 or int(candidate_truth.cache_hit_tokens or 0) > 0:
                truth = candidate_truth
        if truth is None and int(fallback_estimated_input_tokens or 0) > 0:
            truth = build_runtime_estimated_input_truth(
                estimated_input_tokens=int(fallback_estimated_input_tokens or 0),
                provider_model=str(provider_model or "").strip(),
                actual_request_hash=str(actual_request_hash or "").strip(),
                source="preflight_estimate",
            )
        if truth is None:
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

    @staticmethod
    def _parse_frontdoor_timing_timestamp(value: Any) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.astimezone()
        return parsed

    def _frontdoor_turn_timing_fields(
        self,
        state: CeoGraphState,
        *,
        provider_request_started_at: str = "",
    ) -> dict[str, Any]:
        """Measure the pre-request window: bridge inbound → provider send.

        ``created_at`` on an artifact is written after the provider response
        returns, so it cannot expose dispatch, transcript persistence, prompt
        assembly, or preflight time. These fields make first-hop latency
        attributable instead of inferred.
        """
        metadata = _user_input_metadata(state.get("user_input"))
        inbound_received_at = str(metadata.get("turn_inbound_received_at") or "").strip()
        started_at = str(provider_request_started_at or "").strip()
        fields: dict[str, Any] = {
            "provider_request_started_at": started_at,
            "turn_inbound_received_at": inbound_received_at,
            "inbound_to_request_start_seconds": None,
        }
        inbound_dt = self._parse_frontdoor_timing_timestamp(inbound_received_at)
        started_dt = self._parse_frontdoor_timing_timestamp(started_at)
        if inbound_dt is not None and started_dt is not None:
            fields["inbound_to_request_start_seconds"] = round(
                (started_dt - inbound_dt).total_seconds(),
                3,
            )
        return fields

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
        provider_request_started_at: str = "",
        memory_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        diagnostics = dict(prompt_cache_diagnostics or {})
        provider_model = str((list(state.get("model_refs") or []) or [""])[0] or "").strip()
        resolved_provider_model = str(
            dict(state.get("frontdoor_token_preflight_diagnostics") or {}).get("provider_model")
            or provider_model
            or ""
        ).strip()
        preflight_diagnostics = dict(state.get("frontdoor_token_preflight_diagnostics") or {})
        observed_input_truth = self._frontdoor_observed_input_truth(
            usage=usage,
            provider_model=resolved_provider_model,
            actual_request_hash=str(diagnostics.get("actual_request_hash") or "").strip(),
            fallback_estimated_input_tokens=int(
                preflight_diagnostics.get("final_request_tokens")
                or preflight_diagnostics.get("estimated_total_tokens")
                or 0
            ),
        )
        frontdoor_token_preflight_diagnostics = self._frontdoor_diagnostics_with_observed_input_truth(
            preflight_diagnostics,
            observed_input_truth,
        )
        turn_timing = self._frontdoor_turn_timing_fields(
            state,
            provider_request_started_at=provider_request_started_at,
        )
        return {
            "type": str(request_kind or "").strip() or "frontdoor_actual_request",
            "request_kind": str(request_kind or "").strip() or "frontdoor_actual_request",
            "request_lane": str(request_lane or "").strip() or "visible_frontdoor",
            "session_key": str(session_key or "").strip(),
            "turn_id": str(turn_id or "").strip(),
            "parent_request_id": str(parent_request_id or "").strip(),
            "created_at": now_iso(),
            **turn_timing,
            "provider_model": resolved_provider_model,
            "model_refs": [
                str(item or "").strip()
                for item in list(state.get("model_refs") or [])
                if str(item or "").strip()
            ],
            "frontdoor_history_shrink_reason": str(state.get("frontdoor_history_shrink_reason") or "").strip(),
            "frontdoor_token_preflight_diagnostics": copy.deepcopy(frontdoor_token_preflight_diagnostics),
            # 长期记忆快照是会话级冻结值（采纳点=首请求/压缩轮末/手动压缩后），而本地 prompt
            # cache key 与 preflight 投影都不含这个块，只有这组字段能证明冻结生效。
            "memory_snapshot": dict(memory_snapshot or {}),
            "parallel_tool_calls": parallel_tool_calls,
            "prompt_cache_key": str(prompt_cache_key or "").strip(),
            "prompt_cache_key_hash": str(diagnostics.get("prompt_cache_key_hash") or "").strip(),
            "actual_request_hash": str(diagnostics.get("actual_request_hash") or "").strip(),
            "actual_request_message_count": int(diagnostics.get("actual_request_message_count") or 0),
            "actual_tool_schema_hash": str(diagnostics.get("actual_tool_schema_hash") or "").strip(),
            "tool_signature_hash": str(diagnostics.get("tool_signature_hash") or "").strip(),
            "provider_tool_exposure_revision": str(
                state.get("provider_tool_exposure_revision") or ""
            ).strip(),
            "provider_tool_exposure_commit_reason": str(
                state.get("provider_tool_exposure_commit_reason") or ""
            ).strip(),
            "bundle_placeholder_names": [
                str(item or "").strip()
                for item in list(state.get("bundle_placeholder_names") or [])
                if str(item or "").strip()
            ],
            # 单源化后这两组读数是同一本账：`tool_schemas` 就是发送体里那一份（预检不再另渲），
            # `wire_*` 三枚则是它的哈希/序列哈希/逐名哈希，外加整表重写的分类计数。
            "frontdoor_tool_schema_hits": dict(state.get("frontdoor_tool_schema_hits") or {}),
            "frontdoor_tool_schema_fallback_opportunities": int(
                state.get("frontdoor_tool_schema_fallback_opportunities") or 0
            ),
            "bundle_compat_rebuild": int(state.get("bundle_compat_rebuild") or 0),
            "bundle_fault": int(state.get("bundle_fault") or 0),
            "wire_tool_schema_hash": str(state.get("frontdoor_wire_tool_schema_hash") or "").strip(),
            # 与上一跳记录比对时要认得两侧口径（存量记录是 proxy，切换后是 wire）。
            "tools_record_basis": str(state.get("frontdoor_tools_record_basis") or "proxy").strip(),
            "wire_tool_name_sequence_hash": str(
                state.get("frontdoor_wire_tool_name_sequence_hash") or ""
            ).strip(),
            "wire_tool_schema_item_hashes": dict(state.get("frontdoor_wire_tool_schema_item_hashes") or {}),
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
        runtime: CeoRuntime,
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
                memory_snapshot=memory_snapshot_provenance(target_session),
            ),
        )

    def _frontdoor_tool_state_after_tool_results(
        self,
        *,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        tool_names = self._normalized_tool_name_state_list(state.get("tool_names"))
        candidate_tool_names = self._normalized_tool_name_state_list(state.get("candidate_tool_names"))
        candidate_tool_items = self._normalized_candidate_tool_items(
            state.get("candidate_tool_items"),
            fallback_names=candidate_tool_names,
        )
        visible_tool_names = self._normalized_tool_name_state_list(
            state.get("rbac_visible_tool_names")
            or [*tool_names, *candidate_tool_names]
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
        # callable 由这一跳的请求视图重算：load 送进的契约块就在同批工具结果里，它落进
        # `state["messages"]` 之后自然在场——没有台账需要在这段里喂。
        # 阶段工具是 callable 渲染端的注入项，不进 `tool_names` 这一池：那池的口径是
        # builder 的 callable，历来不含它。
        tool_names = [
            name
            for name in self._frontdoor_callable_tool_names_for_state(
                {**dict(state or {}), "tool_names": tool_names}
            )
            if name != STAGE_TOOL_NAME
        ]
        # candidate = 治理可见 −（callable ∪ 待修复）。契约缺席的名字在这一刻就回到候选：
        # 不在场进不了 callable，同一个减法把它留在候选里，回合内重新 load 的出口始终可达。
        # 待修复那一档不进候选——builder 侧本来就把它们摘出去，这里重新派生必须同口径。
        excluded_from_candidate = set(tool_names) | self._frontdoor_repair_required_tool_name_set(state)
        candidate_tool_names = [
            name
            for name in visible_tool_names
            if name not in excluded_from_candidate
        ]
        # 实时卫生与发现出口共用一次治理实时读；读不到就都跳过（宁可滞后再说，
        # 也不拿失败当收回）。声明名的收回走 denied_tools + 重印边界，不在这里摘。
        live_granted = self._frontdoor_live_granted_tool_names(
            session_key=str(state.get("session_key") or ""),
        )
        if live_granted:
            live_granted_set = set(live_granted)
            declared_set = set(
                self._normalized_tool_name_state_list(state.get("provider_tool_names"))
            )
            # 卫生：非声明的名字被 RBAC 收回时当跳出 callable——它不在
            # `tools[]` 里，denied_tools 点不到名，留在 callable 只会让模型拿着
            # "可调用"的预告去撞一次拒绝。RBAC 重新放行后由下面的发现出口重新
            # 送回候选，模型重新 load 即可。
            rbac_dropped = [
                name
                for name in tool_names
                if name not in declared_set
                and name not in live_granted_set
                and name not in CEO_FIXED_BUILTIN_TOOL_NAMES
            ]
            if rbac_dropped:
                rbac_dropped_set = set(rbac_dropped)
                tool_names = [name for name in tool_names if name not in rbac_dropped_set]
            # 发现：治理实时放行、但既不在 `tools[]`（钉住声明未吸收）、也不可调用的名字，
            # 并回候选——尾块把它渲成 `undeclared_candidates`
            # （"能 load 但参数表还没进本次 tools[]，清单到压缩那一跳才重印"），
            # 模型 load 拿到正文与参数契约后即可调用。
            live_undeclared = [
                name
                for name in live_granted
                if name
                and name not in declared_set
                and name not in set(tool_names)
                and name not in CEO_FIXED_BUILTIN_TOOL_NAMES
            ]
            for name in live_undeclared:
                if name not in candidate_tool_names:
                    candidate_tool_names.append(name)
        candidate_name_set = set(candidate_tool_names)
        candidate_tool_items = [
            dict(item)
            for item in list(candidate_tool_items or [])
            if str(item.get("tool_id") or "").strip() in candidate_name_set
        ]
        # 掉出 callable 的名字在回合初快照里没有条目（它当时在 callable 面上，条目被摘掉），
        # 只按 names 过滤的话尾块 `candidate_tools` 那行就永远少它一个——那一行读的是
        # items。补名字级条目即可：说明文字由 provider `tools[]` 的 description 承载，
        # 这里不抄第二份。
        item_names = {str(item.get("tool_id") or "").strip() for item in candidate_tool_items}
        for name in candidate_tool_names:
            if name and name not in item_names:
                candidate_tool_items.append({"tool_id": name, "description": ""})
                item_names.add(name)
        return {
            "tool_names": list(tool_names),
            "candidate_tool_names": list(candidate_tool_names),
            "candidate_tool_items": list(candidate_tool_items),
        }

    def _refresh_frontdoor_dynamic_contract_state(
        self,
        *,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        refreshed = dict(state or {})
        # 声明名单直接交给渲染器（活实例优先、删掉的名字取删除前快照），别先摘名字。
        provider_tool_schema_names = list(
            refreshed.get("provider_tool_names") or refreshed.get("tool_names") or []
        )
        for legacy_field in ("summary_text", "summary_payload", "summary_model_key", "summary_version"):
            refreshed.pop(legacy_field, None)
        if not hasattr(self, "_frontdoor_prompt_contract"):
            return refreshed
        try:
            model_refs = list(refreshed.get("model_refs") or [])
            provider_model = str(model_refs[0] if model_refs else "").strip()
            try:
                tool_schemas = self._selected_tool_schemas(provider_tool_schema_names)
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
            normalized_stage = {
                "stage_id": stage_id,
                "stage_index": int(raw_stage.get("stage_index") or index),
                "stage_goal": str(raw_stage.get("stage_goal") or "").strip(),
                "preamble_text": str(raw_stage.get("preamble_text") or "").strip(),
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
            # 收口标记必须穿过这份白名单：渲染读的是 stage_state，标记一旦在这里被
            # 丢掉，压缩落地的水位线就只活在 canonical 那份里，块照旧逐轮渲染。
            # 与 canonical 归一化器同一口径——只在 False 时写，缺失即视为可见。
            if raw_stage.get("context_visible") is False:
                normalized_stage["context_visible"] = False
            # 裁撤标记必须和收口标记一样穿过这份白名单，否则 stage_state 侧读到的一直是
            # "没裁过"，模型点了名也不会生效（与 canonical 归一化器同一口径）。
            if raw_stage.get("context_evicted") is True:
                normalized_stage["context_evicted"] = True
            normalized_stages.append(normalized_stage)
        if active_stage_id and not any(
            str(stage.get("stage_id") or "").strip() == active_stage_id
            and str(stage.get("status") or "").strip().lower() == "active"
            for stage in normalized_stages
        ):
            active_stage_id = ""
        transition_required = bool(raw.get("transition_required"))
        if not active_stage_id:
            transition_required = False
        pending_orphan_rounds = [
            dict(item)
            for item in list(raw.get("pending_orphan_rounds") or [])
            if isinstance(item, dict)
        ]
        return {
            "active_stage_id": active_stage_id,
            "transition_required": transition_required,
            "stages": normalized_stages,
            "pending_orphan_rounds": pending_orphan_rounds,
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

    @staticmethod
    def _frontdoor_closing_stage(stage_state: dict[str, Any] | None) -> dict[str, Any] | None:
        """`completed_stage_summary` / `key_refs` / 裁撤材料的归属对象，两车道共用 `closing_stage_target`。

        判据只允许有一份：前门与节点都会在轮末 / run 终局把活动阶段结清并清空
        `active_stage_id`，而模型可能到下一个回合（前门的渠道会话形态）或恢复后的下一次提交
        （节点的错误恢复与验收打回）才补发 `submit_next_stage`。只认活动位会让三样材料一起
        静默悬空，新阶段却照常追加，从外表看不出异常（实盘
        `ext:qq-official-1903529517:f8a8001865631301`：12/12 条阶段无总结、点名过的裁撤一次都没
        兑现、归档文件 0 个）。合同详见
        `docs/architecture/runtime-overview.md`「stage_compaction」。
        """
        target = closing_stage_target(stage_state)
        return target if isinstance(target, dict) else None

    @classmethod
    def _frontdoor_closing_stage_id(cls, stage_state: dict[str, Any] | None) -> str:
        return str((cls._frontdoor_closing_stage(stage_state) or {}).get("stage_id") or "").strip()

    def _frontdoor_submit_next_stage(
        self,
        stage_state: dict[str, Any],
        *,
        session_key: str,
        arguments: dict[str, Any],
        preamble_text: str = "",
        system_generated: bool = False,
        archive: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """一次 `submit_next_stage` 提交的完整落账：解析归属 → 提交 → 按需导档 → 回报。

        两条车道（图闭包的本轮工作副本、finalize 重建 durable 账本）共用这里，收尾判据只有
        一份；`archive=False` 给闭包用，因为写在会被 durable 返回值覆盖的工作副本上的
        `archive_ref` 只会多留一份无人引用的归档文件。`stage_closure` 回执只在模型给了收尾
        材料时附上——没有它，模型只能按提示词文案倒推自己有没有裁撤成功，于是向用户谎报。
        """
        closing_stage_id = self._frontdoor_closing_stage_id(stage_state)
        normalized_summary = str(arguments.get("completed_stage_summary") or "").strip()
        drop_detail = bool(arguments.get("drop_completed_stage_tool_detail"))
        next_state, next_stage = self._submit_frontdoor_next_stage_state(
            stage_state,
            stage_goal=str(arguments.get("stage_goal") or ""),
            tool_round_budget=int(arguments.get("tool_round_budget") or 0),
            completed_stage_summary=normalized_summary,
            key_refs=[
                dict(item)
                for item in list(arguments.get("key_refs") or [])
                if isinstance(item, dict)
            ],
            final=bool(arguments.get("final")),
            preamble_text=preamble_text,
            system_generated=system_generated,
            drop_completed_stage_tool_detail=drop_detail,
        )
        if drop_detail and archive:
            self._frontdoor_archive_evicted_stage(
                session_key=session_key,
                stage_state=next_state,
                stage_id=closing_stage_id,
            )
        payload = dict(next_stage)
        if normalized_summary or drop_detail:
            closed = next(
                (
                    stage
                    for stage in list(next_state.get("stages") or [])
                    if isinstance(stage, dict)
                    and str(stage.get("stage_id") or "").strip() == closing_stage_id
                ),
                None,
            )
            if not closing_stage_id:
                reason = "no_closing_target"
            elif drop_detail and not normalized_summary:
                reason = "summary_required"
            else:
                reason = "applied"
            payload["stage_closure"] = {
                "target_stage_id": closing_stage_id,
                "summary_attached": bool(
                    closed and str(closed.get("completed_stage_summary") or "").strip()
                ),
                "evicted": bool(closed and closed.get("context_evicted") is True),
                "reason": reason,
                # 落空时在结果里自带一句可读说明：schema 描述两车道共用，改它会整体失效
                # provider 前缀，而结果属动态尾部，模型不必再靠猜。
                **({} if reason == "applied" else {"note": STAGE_CLOSURE_INACTIVE_NOTES[reason]}),
            }
        return next_state, payload

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
        preamble_text: str = "",
        system_generated: bool = False,
        drop_completed_stage_tool_detail: bool = False,
        include_pending_in_budget: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        normalized_state = cls._frontdoor_stage_state_snapshot({"frontdoor_stage_state": stage_state})
        normalized_goal = str(stage_goal or "").strip()
        normalized_budget = max(STAGE_TOOL_ROUND_BUDGET_MIN, int(tool_round_budget or 0))
        normalized_summary = str(completed_stage_summary or "").strip()
        normalized_key_refs = [dict(item) for item in list(key_refs or []) if isinstance(item, dict)]
        if not normalized_goal:
            raise ValueError("stage_goal must not be empty")
        if normalized_budget > STAGE_TOOL_ROUND_BUDGET_MAX:
            raise ValueError(
                f"tool_round_budget must not exceed {STAGE_TOOL_ROUND_BUDGET_MAX}"
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

        # 归属对象只有一处判据：活动阶段优先，跨回合提交时回退到刚被轮末结清、总结还空着
        # 的最后一条阶段。少了回退这一支，渠道会话的 summary / key_refs / 裁撤会一起悬空，
        # 而新阶段照常追加，从外表看不出任何异常。
        closing_stage = cls._frontdoor_closing_stage(normalized_state)
        closing_stage_id = str((closing_stage or {}).get("stage_id") or "").strip()

        now = now_iso()
        stages: list[dict[str, Any]] = []
        for stage in list(normalized_state.get("stages") or []):
            current = dict(stage)
            if closing_stage_id and str(current.get("stage_id") or "").strip() == closing_stage_id:
                current.update(
                    {
                        "status": "completed",
                        # 轮末结清时已经落过 finished_at，跨回合补记不改动它：
                        # 收口水位线与阶段排序都按这个时间命中。
                        "finished_at": str(current.get("finished_at") or "").strip() or now,
                        "completed_stage_summary": normalized_summary,
                        "key_refs": normalized_key_refs,
                    }
                )
                # 与节点侧同一口径：只有同批带了非空总结才落裁撤标记，缺失即未裁撤，
                # 所以落盘不会给每条阶段添一个布尔键。
                if drop_completed_stage_tool_detail and normalized_summary:
                    current["context_evicted"] = True
            stages.append(current)

        next_stage_index = max((int(stage.get("stage_index") or 0) for stage in stages), default=0) + 1
        next_stage_id = f"frontdoor-stage-{next_stage_index}"
        pending_orphan_rounds = [
            dict(item)
            for item in list(normalized_state.get("pending_orphan_rounds") or [])
            if isinstance(item, dict)
        ]
        grafted_rounds: list[dict[str, Any]] = []
        for orphan_index, round_item in enumerate(pending_orphan_rounds, start=1):
            grafted = dict(round_item)
            grafted["round_id"] = f"{next_stage_id}:round-{orphan_index}"
            grafted["round_index"] = orphan_index
            grafted["orphan_grafted"] = True
            grafted_rounds.append(grafted)
        grafted_used = sum(
            1 for round_item in grafted_rounds if bool(round_item.get("budget_counted"))
        )
        stage_budget = (
            normalized_budget + grafted_used
            if include_pending_in_budget
            else normalized_budget
        )
        stage_rounds_used = (
            grafted_used
            if include_pending_in_budget
            else min(normalized_budget, grafted_used)
        )
        next_stage = {
            "stage_id": next_stage_id,
            "stage_index": next_stage_index,
            "stage_kind": "normal",
            "system_generated": bool(system_generated),
            "mode": "自主执行",
            "status": "active",
            "stage_goal": normalized_goal,
            "preamble_text": str(preamble_text or "").strip(),
            "completed_stage_summary": "",
            "final_stage": bool(final),
            "key_refs": [],
            "tool_round_budget": stage_budget,
            "tool_rounds_used": stage_rounds_used,
            "created_at": now,
            "finished_at": "",
            "rounds": grafted_rounds,
        }
        next_state = {
            "active_stage_id": next_stage_id,
            "transition_required": False,
            "stages": [*stages, next_stage],
            "pending_orphan_rounds": [],
        }
        return next_state, next_stage

    @classmethod
    def _record_frontdoor_stage_round(
        cls,
        stage_state: dict[str, Any],
        *,
        tool_call_payloads: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        text: str = "",
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
                        "text": str(text or "").strip(),
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
            "pending_orphan_rounds": [
                dict(item)
                for item in list(normalized_state.get("pending_orphan_rounds") or [])
                if isinstance(item, dict)
            ],
        }

    def _merged_frontdoor_canonical_context(
        self,
        *,
        state: CeoGraphState,
        frontdoor_stage_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return merge_turn_stage_state_into_canonical_context(
            self._frontdoor_canonical_context_snapshot(state),
            frontdoor_stage_state or self._default_frontdoor_stage_state(),
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
        # 只交契约、没执行的那一行：轨道据此把它画成"加载 tool"，而不是普通工具调用。
        contract_delivery = str(result.get(CONTRACT_DELIVERY_FIELD) or "").strip()
        if contract_delivery:
            item[CONTRACT_DELIVERY_FIELD] = contract_delivery
        # 这次调用实际走的道（D15）与"同一份入参重发"补格。阶段记录是前门唯一的耐久落点：
        # 节点那边有 `task_node_tool_result.payload.execution_path` 可按行查，前门没有那种
        # 行，不在这儿落就等于只能用回执文本反推——而文本反推已经被证明会数错。
        execution_path = str(result.get("execution_path") or "").strip()
        if execution_path:
            item["execution_path"] = execution_path
        staged_replay = str(result.get(STAGED_REPLAY_FIELD) or "").strip()
        if staged_replay:
            item[STAGED_REPLAY_FIELD] = staged_replay
        if bool(result.get(REUSE_FLAG_IGNORED_FIELD)):
            item[REUSE_FLAG_IGNORED_FIELD] = True
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
        cycle_narration_text = str(state.get("analysis_text") or "").strip()
        node_error_context = self._frontdoor_node_error_heartbeat_context(state)
        stage_created_this_cycle = False
        for payload, result in zip(list(tool_call_payloads or []), list(tool_results or []), strict=False):
            tool_name = str(payload.get("name") or "").strip()
            status = str(result.get("status") or "").strip().lower()
            if tool_name == STAGE_TOOL_NAME:
                if status != "error":
                    arguments = dict(payload.get("arguments") or {})
                    # 标记、导档、块里的指针必须由这条"回合后重建 durable 账本"的路径产出：
                    # 图节点里那份 mutable_stage_state 只是本轮工作副本，finalize 会用这里的
                    # 返回值覆盖状态。与收口标记同一教训——标记要落在 durable 基线推进的那一步。
                    stage_state, _ = self._frontdoor_submit_next_stage(
                        stage_state,
                        session_key=str(state.get("session_key") or "").strip(),
                        arguments=arguments,
                        preamble_text=cycle_narration_text,
                    )
                    stage_created_this_cycle = True
                continue
            ordinary_calls.append(dict(payload))
            ordinary_results.append(dict(result))
        # 节点暂停事件心跳：本轮没有显式 submit 且无活动阶段时，自动补开一个
        # system_generated 阶段（预算 10），让实质性工具落进一个可追溯的阶段。
        # 预算耗尽后 transition_required 置真，仍需 submit_next_stage 才能继续。
        if (
            not stage_created_this_cycle
            and node_error_context is not None
            and ordinary_calls
            and not str(stage_state.get("active_stage_id") or "").strip()
        ):
            stage_state, _ = self._frontdoor_auto_open_node_error_stage(
                stage_state,
                context=node_error_context,
            )
            stage_created_this_cycle = True
        round_entries = [
            self._frontdoor_round_tool_entry(payload=payload, result=result, source=source)
            for payload, result in zip(list(ordinary_calls or []), list(ordinary_results or []), strict=False)
        ]
        free_pass_kind = ""
        for result in ordinary_results:
            kind = str(result.get("free_pass_kind") or "").strip()
            if kind:
                free_pass_kind = kind
                break
        # 宽限执行的工具按归属记账：无阶段 → 待入账孤儿轮；预算耗尽 → 本阶段溢出轮。
        if free_pass_kind == "stageless" and ordinary_calls and not str(stage_state.get("active_stage_id") or "").strip():
            return self._record_frontdoor_orphan_rounds(
                stage_state,
                tool_call_payloads=ordinary_calls,
                tools=round_entries,
                text=cycle_narration_text,
            )
        if free_pass_kind == "exhausted" and ordinary_calls:
            return self._record_frontdoor_overflow_round(
                stage_state,
                tool_call_payloads=ordinary_calls,
                tools=round_entries,
                text=cycle_narration_text,
            )
        updated_state = self._record_frontdoor_stage_round(
            stage_state,
            tool_call_payloads=ordinary_calls,
            tools=round_entries,
            text="" if stage_created_this_cycle else cycle_narration_text,
        )
        return updated_state

    @classmethod
    def _record_frontdoor_orphan_rounds(
        cls,
        stage_state: dict[str, Any],
        *,
        tool_call_payloads: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        text: str = "",
    ) -> dict[str, Any]:
        normalized_state = cls._frontdoor_stage_state_snapshot({"frontdoor_stage_state": stage_state})
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
        pending = [
            dict(item)
            for item in list(normalized_state.get("pending_orphan_rounds") or [])
            if isinstance(item, dict)
        ]
        round_index = len(pending) + 1
        pending.append(
            {
                "round_id": f"orphan-round-{round_index}",
                "round_index": round_index,
                "created_at": now_iso(),
                "text": str(text or "").strip(),
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
                "orphan": True,
                "tools": [dict(item) for item in list(tools or []) if isinstance(item, dict)],
            }
        )
        return {
            "active_stage_id": str(normalized_state.get("active_stage_id") or "").strip(),
            "transition_required": bool(normalized_state.get("transition_required")),
            "stages": list(normalized_state.get("stages") or []),
            "pending_orphan_rounds": pending,
        }

    @classmethod
    def _record_frontdoor_overflow_round(
        cls,
        stage_state: dict[str, Any],
        *,
        tool_call_payloads: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        text: str = "",
    ) -> dict[str, Any]:
        normalized_state = cls._frontdoor_stage_state_snapshot({"frontdoor_stage_state": stage_state})
        active_stage_id = str(normalized_state.get("active_stage_id") or "").strip()
        visible_calls = [
            dict(item)
            for item in list(tool_call_payloads or [])
            if str(item.get("name") or "").strip() and str(item.get("name") or "").strip() != STAGE_TOOL_NAME
        ]
        if not visible_calls or not active_stage_id:
            return normalized_state
        stages: list[dict[str, Any]] = []
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
                        "text": str(text or "").strip(),
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
                        "budget_counted": False,
                        "overflow": True,
                        "tools": [dict(item) for item in list(tools or []) if isinstance(item, dict)],
                    }
                )
                current["rounds"] = rounds
            stages.append(current)
        return {
            "active_stage_id": active_stage_id,
            "transition_required": bool(normalized_state.get("transition_required")),
            "stages": stages,
            "pending_orphan_rounds": [
                dict(item)
                for item in list(normalized_state.get("pending_orphan_rounds") or [])
                if isinstance(item, dict)
            ],
        }

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
            "pending_orphan_rounds": [
                dict(item)
                for item in list(normalized_state.get("pending_orphan_rounds") or [])
                if isinstance(item, dict)
            ],
        }

    @classmethod
    def _frontdoor_stage_gate_error(
        cls,
        *,
        tool_name: str,
        stage_state: dict[str, Any],
        allow_stageless: bool = False,
    ) -> str:
        snapshot = cls._frontdoor_stage_state_snapshot({"frontdoor_stage_state": stage_state})
        has_active_stage = bool(str(snapshot.get("active_stage_id") or "").strip())
        transition_required = bool(snapshot.get("transition_required"))
        if allow_stageless and not has_active_stage and not transition_required:
            # 节点暂停事件心跳：无活动阶段时放行本轮首个实质性工具调用，阶段由
            # _frontdoor_stage_state_after_tool_cycle 自动补开，避免"必撞一次闸"。
            return ""
        return stage_gate_error_for_tool(
            tool_name,
            has_active_stage=has_active_stage,
            transition_required=transition_required,
            extra_allowed_tools={*cls._CONTROL_TOOL_NAMES, *FRONTDOOR_STAGELESS_MEMORY_TOOL_NAMES},
            stage_tool_name=STAGE_TOOL_NAME,
        )

    @classmethod
    def _frontdoor_stage_free_pass_kind(cls, stage_state: dict[str, Any] | None) -> str:
        """撞闸普通工具是否获得本轮宽限执行，及记账归属。

        返回 "stageless"(无活动阶段、宽限未用尽)、"exhausted"(预算耗尽、宽限未用尽)，
        或 ""(宽限已用尽，应硬拦)。节点暂停事件心跳的 allow_stageless 放行优先于本判定
        (在 _frontdoor_stage_gate_error 已返回空串，不会走到这里)。
        """
        snapshot = cls._frontdoor_stage_state_snapshot({"frontdoor_stage_state": stage_state or {}})
        has_active_stage = bool(str(snapshot.get("active_stage_id") or "").strip())
        transition_required = bool(snapshot.get("transition_required"))
        active_stage = next(
            (
                dict(stage)
                for stage in list(snapshot.get("stages") or [])
                if str(stage.get("stage_id") or "").strip() == str(snapshot.get("active_stage_id") or "").strip()
                and str(stage.get("status") or "").strip().lower() == "active"
            ),
            None,
        )
        return stage_free_pass_kind(
            has_active_stage=has_active_stage,
            transition_required=transition_required,
            active_stage_rounds=list(active_stage.get("rounds") or []) if active_stage is not None else [],
            pending_orphan_rounds=list(snapshot.get("pending_orphan_rounds") or []),
        )

    @classmethod
    def _frontdoor_predicted_exhaustion_reminder(
        cls,
        stage_state: dict[str, Any] | None,
        *,
        ordinary_payloads: list[dict[str, Any]],
    ) -> str:
        """在合法轮即将打满预算时,给本轮结果附上"下轮必须同批 sns"的预告提醒。

        仅当本轮不含 sns、且预算计数的普通调用会把 used 推到预算上限时触发;
        含 sns 的同批无需预告(模型已主动开下一阶段)。
        """
        if not ordinary_payloads:
            return ""
        if not response_tool_calls_count_against_stage_budget(
            ordinary_payloads,
            extra_non_budget_tools=cls._CONTROL_TOOL_NAMES,
        ):
            return ""
        snapshot = cls._frontdoor_stage_state_snapshot({"frontdoor_stage_state": stage_state or {}})
        if not str(snapshot.get("active_stage_id") or "").strip() or bool(snapshot.get("transition_required")):
            return ""
        active_stage = next(
            (
                dict(stage)
                for stage in list(snapshot.get("stages") or [])
                if str(stage.get("stage_id") or "").strip() == str(snapshot.get("active_stage_id") or "").strip()
                and str(stage.get("status") or "").strip().lower() == "active"
            ),
            None,
        )
        if active_stage is None or bool(active_stage.get("final_stage")):
            return ""
        budget = int(active_stage.get("tool_round_budget") or 0)
        used = int(active_stage.get("tool_rounds_used") or 0)
        if budget <= 0 or used + 1 < budget:
            return ""
        return STAGE_BUDGET_EXHAUSTION_PREDICTED_REMINDER_TEMPLATE.format(used=used, budget=budget)

    @classmethod
    def _frontdoor_node_error_heartbeat_context(cls, state: CeoGraphState) -> dict[str, Any] | None:
        """节点暂停事件心跳的上下文；非该事件返回 None。

        心跳轮里节点 `task_node_error` 事件要求模型用 manage_task_nodes 处置，
        但无活动阶段时这些工具会被闸门拦下。这里提取 task/node 信息，供闸门放行
        与自动补开阶段（标题体现"任务 ID xxx 中的节点出现自动暂停"）使用。
        """
        if not isinstance(state, dict):
            return None
        metadata = _user_input_metadata(state.get("user_input"))
        heartbeat_internal = bool(state.get("heartbeat_internal")) or bool(metadata.get("heartbeat_internal"))
        if not heartbeat_internal:
            return None
        if str(metadata.get("heartbeat_reason") or "").strip() != "task_node_error":
            return None
        task_ids = [
            str(item or "").strip()
            for item in list(metadata.get("heartbeat_task_ids") or [])
            if str(item or "").strip()
        ]
        node_ids = [
            str(item or "").strip()
            for item in list(metadata.get("heartbeat_node_ids") or [])
            if str(item or "").strip()
        ]
        return {"task_id": task_ids[0] if task_ids else "", "node_id": node_ids[0] if node_ids else ""}

    @staticmethod
    def _frontdoor_node_error_stage_goal(context: dict[str, Any]) -> str:
        task_id = str((context or {}).get("task_id") or "").strip()
        if task_id:
            return f"任务 ID {task_id} 中的节点出现自动暂停，检查原因并处理"
        return "任务中的节点出现自动暂停，检查原因并处理"

    @classmethod
    def _frontdoor_auto_open_node_error_stage(
        cls,
        stage_state: dict[str, Any],
        *,
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return cls._submit_frontdoor_next_stage_state(
            stage_state,
            stage_goal=cls._frontdoor_node_error_stage_goal(context),
            tool_round_budget=FRONTDOOR_NODE_ERROR_AUTO_STAGE_BUDGET,
            completed_stage_summary="",
            final=False,
            preamble_text="",
            system_generated=True,
        )

    @classmethod
    def _frontdoor_absorb_orphan_rounds(cls, stage_state: dict[str, Any]) -> dict[str, Any]:
        """轮末收尾前,把待入账孤儿轮收纳进一个 system_generated 阶段。

        只处理"无活动阶段且本轮直接输出文本、不再调用 sns"的边界情况:
        复用 _submit_frontdoor_next_stage_state 的嫁接逻辑,把孤儿轮作为该自动阶段的
        rounds(标记 orphan_grafted),阶段保持 kind="normal"(阶段压缩只认 normal),
        system_generated=True 以区分来源;随后由轮末 output 收尾以指针摘要关闭它。
        有活动阶段时孤儿轮留给下一次 sns 嫁接,这里不动。
        """
        snapshot = cls._frontdoor_stage_state_snapshot({"frontdoor_stage_state": stage_state})
        pending = [
            dict(item)
            for item in list(snapshot.get("pending_orphan_rounds") or [])
            if isinstance(item, dict)
        ]
        if not pending or str(snapshot.get("active_stage_id") or "").strip():
            return snapshot
        first_round_text = str(pending[0].get("text") or "").strip()
        stage_goal = (
            first_round_text[:60]
            if first_round_text
            else "自动补记：无阶段期间执行的工具调用"
        )
        budget = min(max(STAGE_TOOL_ROUND_BUDGET_MIN, len(pending)), STAGE_TOOL_ROUND_BUDGET_MAX)
        absorbed, _ = cls._submit_frontdoor_next_stage_state(
            snapshot,
            stage_goal=stage_goal,
            tool_round_budget=budget,
            completed_stage_summary="",
            key_refs=[],
            final=False,
            preamble_text=first_round_text,
            system_generated=True,
            include_pending_in_budget=False,
        )
        return absorbed

    @classmethod
    def _frontdoor_default_overlay_text(cls, state: CeoGraphState) -> str:
        stage_gate = cls._frontdoor_stage_gate(state)
        return _join_overlay_text(
            build_ceo_stage_overlay(stage_gate),
            build_ceo_stage_result_block_message(stage_gate),
        )

    @staticmethod
    def _normalize_review_risk_level(value: Any, *, default: str = "high") -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"low", "medium", "high"}:
            return normalized
        fallback = str("" if default is None else default).strip().lower()
        if fallback in {"low", "medium", "high"}:
            return fallback
        return ""

    def _legacy_reviewable_tool_risk_map(self) -> dict[str, str]:
        assembly_cfg = getattr(getattr(self._loop, "_memory_runtime_settings", None), "assembly", None)
        if not bool(getattr(assembly_cfg, "frontdoor_interrupt_approval_enabled", False)):
            return {}
        raw_names = list(
            getattr(assembly_cfg, "frontdoor_interrupt_tool_names", ["create_async_task"]) or []
        )
        reviewable: dict[str, str] = {}
        for raw_name in raw_names:
            name = str(raw_name or "").strip()
            if name:
                reviewable[name] = "high"
        return reviewable

    def _dynamic_reviewable_tool_risk_map(self, *, session_key: str) -> dict[str, str]:
        service = getattr(self._loop, "main_task_service", None)
        supplier = getattr(service, "frontdoor_reviewable_tool_risk_map", None) if service is not None else None
        if not callable(supplier):
            return {}
        try:
            payload = supplier(actor_role="ceo", session_id=str(session_key or "").strip() or "web:shared")
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        reviewable: dict[str, str] = {}
        for raw_name, raw_risk in payload.items():
            name = str(raw_name or "").strip()
            if not name:
                continue
            reviewable[name] = self._normalize_review_risk_level(raw_risk)
        return reviewable

    def _reviewable_tool_risk_map(self, *, session_key: str) -> dict[str, str]:
        reviewable = dict(self._legacy_reviewable_tool_risk_map())
        dynamic_reviewable = self._dynamic_reviewable_tool_risk_map(session_key=session_key)
        risk_rank = {"low": 0, "medium": 1, "high": 2}
        for tool_name, risk_level in dynamic_reviewable.items():
            current = reviewable.get(tool_name, "low")
            if risk_rank[risk_level] >= risk_rank.get(current, 0):
                reviewable[tool_name] = risk_level
        return reviewable

    def _approval_request_for_tool_calls(
        self,
        tool_call_payloads: list[dict[str, Any]],
        *,
        session_key: str = "",
    ) -> dict[str, Any] | None:
        tool_calls = [
            dict(item)
            for item in list(tool_call_payloads or [])
            if isinstance(item, dict)
        ]
        if not tool_calls:
            return None
        reviewable_risk_map = self._reviewable_tool_risk_map(session_key=session_key)
        review_items: list[dict[str, Any]] = []
        pass_through_tool_call_ids: list[str] = []
        for item in tool_calls:
            tool_call_id = str(item.get("id") or "").strip()
            tool_name = str(item.get("name") or "").strip()
            arguments = dict(item.get("arguments") or {})
            risk_level = self._normalize_review_risk_level(
                reviewable_risk_map.get(tool_name, ""),
                default="",
            )
            if risk_level:
                review_items.append(
                    {
                        "tool_call_id": tool_call_id,
                        "name": tool_name,
                        "risk_level": risk_level,
                        "arguments": arguments,
                    }
                )
                continue
            if tool_call_id:
                pass_through_tool_call_ids.append(tool_call_id)
        if not review_items:
            return None
        return {
            "kind": "frontdoor_tool_approval_batch",
            "batch_id": f"batch:{uuid.uuid4().hex[:12]}",
            "mode": "regulatory_review",
            "submission_mode": "batch_submit_only",
            "tool_calls": tool_calls,
            "review_items": review_items,
            "pass_through_tool_call_ids": pass_through_tool_call_ids,
        }

    def _normalize_approval_resume_value(
        self,
        *,
        decision: Any,
        original_payloads: list[dict[str, Any]],
        approval_request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_request = dict(approval_request or {})
        if str(normalized_request.get("kind") or "").strip() == "frontdoor_tool_approval_batch":
            if not isinstance(decision, dict):
                raise ValueError("batch review resume payload must be an object")
            if str(decision.get("type") or "").strip() != "submit_batch_review":
                raise ValueError("unsupported approval resume type")
            if str(decision.get("batch_id") or "").strip() != str(normalized_request.get("batch_id") or "").strip():
                raise ValueError("approval batch mismatch")
            review_items = [
                dict(item)
                for item in list(normalized_request.get("review_items") or [])
                if isinstance(item, dict)
            ]
            decisions = [
                dict(item)
                for item in list(decision.get("decisions") or [])
                if isinstance(item, dict)
            ]
            expected_ids = [
                str(item.get("tool_call_id") or "").strip()
                for item in review_items
                if str(item.get("tool_call_id") or "").strip()
            ]
            decision_map = {
                str(item.get("tool_call_id") or "").strip(): dict(item)
                for item in decisions
                if str(item.get("tool_call_id") or "").strip()
            }
            if sorted(decision_map.keys()) != sorted(expected_ids):
                raise ValueError("batch review decisions must cover every review item exactly once")
            normalized_decisions: list[dict[str, Any]] = []
            for tool_call_id in expected_ids:
                raw_decision = str(decision_map[tool_call_id].get("decision") or "").strip().lower()
                if raw_decision not in {"approve", "reject"}:
                    raise ValueError(f"unsupported batch review decision for {tool_call_id}")
                normalized_item = {
                    "tool_call_id": tool_call_id,
                    "decision": raw_decision,
                }
                note = str(decision_map[tool_call_id].get("note") or "").strip()
                if raw_decision == "reject" and note:
                    normalized_item["note"] = note
                normalized_decisions.append(normalized_item)
            # 逐条裁决必须逐条生效：`reject` 的那条从执行名单里摘掉并留下合成拒绝结果，
            # `approve` 与免审通过的条目照常执行。整批恒 approved 会把操作者拒掉的工具
            # 照样跑掉（审批门形同虚设）。
            rejected_by_id = {
                str(item.get("tool_call_id") or "").strip(): item
                for item in normalized_decisions
                if item.get("decision") == "reject"
            }
            executable_payloads: list[dict[str, Any]] = []
            synthetic_results: list[dict[str, Any]] = []
            for payload in list(original_payloads or []):
                tool_call_id = str(payload.get("id") or "").strip()
                rejected_item = rejected_by_id.get(tool_call_id)
                if rejected_item is None:
                    executable_payloads.append(dict(payload))
                    continue
                synthetic_results.append(
                    self._build_synthetic_rejection_result(
                        tool_call_id=tool_call_id,
                        tool_name=str(payload.get("name") or ""),
                        note=str(rejected_item.get("note") or ""),
                    )
                )
            return {
                "approved": True,
                "tool_call_payloads": list(original_payloads),
                "executable_tool_call_payloads": executable_payloads,
                "synthetic_tool_results": synthetic_results,
                "batch_review_decisions": normalized_decisions,
                "batch_id": str(normalized_request.get("batch_id") or "").strip(),
            }
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

    def _build_synthetic_rejection_result(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        note: str = "",
    ) -> dict[str, Any]:
        normalized_tool_call_id = str(tool_call_id or "").strip()
        normalized_tool_name = str(tool_name or "tool").strip() or "tool"
        result_text = "Error: 用户不允许运行此工具。"
        normalized_note = str(note or "").strip()
        if normalized_note:
            result_text = f"{result_text}\n补充说明：{normalized_note}"
        return {
            "tool_call_id": normalized_tool_call_id,
            "tool_name": normalized_tool_name,
            "status": "error",
            "raw_result": None,
            "result_text": result_text,
            "tool_message": self._tool_result_message(
                tool_call_id=normalized_tool_call_id,
                tool_name=normalized_tool_name,
                content=result_text,
                started_at="",
                finished_at="",
                elapsed_seconds=None,
            ),
            "started_at": "",
            "finished_at": "",
            "elapsed_seconds": None,
            "synthetic_rejection": True,
        }

    def _merge_ordered_tool_results(
        self,
        *,
        original_payloads: list[dict[str, Any]],
        real_results: list[dict[str, Any]],
        synthetic_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        real_by_id = {
            str(item.get("tool_call_id") or "").strip(): dict(item)
            for item in list(real_results or [])
            if str(item.get("tool_call_id") or "").strip()
        }
        synthetic_by_id = {
            str(item.get("tool_call_id") or "").strip(): dict(item)
            for item in list(synthetic_results or [])
            if str(item.get("tool_call_id") or "").strip()
        }
        merged: list[dict[str, Any]] = []
        for payload in list(original_payloads or []):
            tool_call_id = str(payload.get("id") or "").strip()
            if tool_call_id in synthetic_by_id:
                merged.append(dict(synthetic_by_id[tool_call_id]))
                continue
            if tool_call_id in real_by_id:
                merged.append(dict(real_by_id[tool_call_id]))
                continue
            raise RuntimeError(f"missing merged tool result for {tool_call_id or '<unknown>'}")
        return merged

    def _frontdoor_rebuild_pinned_tool_bundle(
        self,
        *,
        keep_names: list[str],
        declared_tools: dict[str, Any],
        registry_lookup: Any,
        prior_index: dict[str, Any] | None = None,
        prior_index_first: bool = False,
    ) -> dict[str, Any]:
        """按"这一跳拿得到的字节"重建整表：声明面实例 → 注册表活实例 → 删除前快照 → 旧整表 → 占位。

        重建只发生在重印边界或整表缺名那一跳，代价等于一次预算内的重印。前四种来源都取不到才
        占位，且占位跟着进整表定影：不写进去的话，下一跳又会缺名重建一次并再记一次 fault，
        "冻结跳零 IO"与 fault 的信噪比一起塌。

        `prior_index_first` 按重建的目的换次序：边界/播种要让编辑在这一跳被吸收，所以先读实例；
        冻结跳上的兼容重建只是补一个缺名，先原样复用旧整表，否则一次无关的增删会顺带把别人的
        字节换掉——那正是"在重印边界之外断前缀"。
        """
        placeholders: list[str] = []
        index: dict[str, Any] = {}
        fallback_index = dict(prior_index or {})
        for name in keep_names:
            normalized = str(name or "").strip()
            if not normalized or normalized in index:
                continue
            carried = fallback_index.get(normalized)
            if prior_index_first and isinstance(carried, dict):
                index[normalized] = dict(carried)
                continue
            payload: Any = None
            candidates = [declared_tools.get(normalized)]
            candidates.append(registry_lookup(normalized) if callable(registry_lookup) else None)
            for source in candidates:
                if not callable(getattr(source, "to_model_schema", None)):
                    continue
                try:
                    payload = _provider_tool_schema(source)
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    break
            if not isinstance(payload, dict):
                retired = lookup_tool_schema(normalized)
                if isinstance(retired, dict):
                    carrier = SimpleNamespace(
                        name=normalized,
                        description="",
                        parameters={},
                        to_model_schema=lambda value=retired: value,
                    )
                    try:
                        payload = _provider_tool_schema(carrier)
                    except Exception:
                        payload = None
            if not isinstance(payload, dict):
                payload = carried
            if not isinstance(payload, dict):
                payload = placeholder_tool_schema(normalized)
                placeholders.append(normalized)
            index[normalized] = dict(payload)
        return {"index": index, "placeholders": placeholders}

    def _persist_frontdoor_pinned_tool_bundle(
        self,
        *,
        session: Any,
        session_key: str,
        keep_names: list[str],
        rendered: list[dict[str, Any]],
        build_reason: str,
    ) -> dict[str, Any]:
        """把这一跳渲出的整表原字节写进会话级 singleton 文件，并换上新 revision。"""
        index = index_schemas_by_name(rendered)
        if session is not None:
            setattr(session, "_frontdoor_pinned_tool_schema_bundle", dict(index))
        written = pinned_tool_bundle.write_pinned_tool_bundle(
            session_key=str(session_key or "").strip(),
            provider_tool_names=list(keep_names or index.keys()),
            rendered_tool_schemas=list(rendered or []),
            build_reason=str(build_reason or "").strip(),
        )
        if session is not None:
            if written.get("path"):
                setattr(session, "_frontdoor_pinned_tool_bundle_path", str(written.get("path") or ""))
                setattr(session, "_frontdoor_pinned_tool_bundle_revision", str(written.get("revision") or ""))
                setattr(session, "_frontdoor_pinned_tool_bundle_fault", 0)
            else:
                setattr(session, "_frontdoor_pinned_tool_bundle_fault", 1)
                logger.warning(
                    "frontdoor pinned tools[] bundle could not be persisted: session_key={} names={}",
                    str(session_key or "").strip(),
                    len(list(keep_names or [])),
                )
        return written

    def _frontdoor_tool_schemas_for_state(
        self,
        *,
        state: CeoGraphState,
        runtime: CeoRuntime,
    ) -> list[dict[str, Any]]:
        # `tools[]` 的名字源就是**钉住的声明**本身：执行束的准入面按本轮 callable 组装，拿它
        # 当模型可见名单会让"收回一个工具"或"没 load 过"变成"这一跳少一条 schema"——清单排在
        # 正文最前面，等于一次权限动作把身后全部存活正文重铺。参数表按名字取实例：先声明面
        # （`declared_tools`，两颗合成控制工具也在里面），其次注册表活实例，再次删除前快照，
        # 三条都经同一个投影函数，字节与上一跳一致。可执行名单另有自己的判据
        # （`_frontdoor_executable_tool_names`＝本轮 callable），执行准入与声明自始分源。
        # state 带不出钉住声明（未播种的调用方）才退回当轮派发名单，那是播种一支。
        keep = self._normalized_tool_name_state_list(
            list(state.get("provider_tool_names") or [])
        ) or self._frontdoor_dispatch_tool_names(state)
        for forced in (STAGE_TOOL_NAME, SILENT_TOOL_NAME):
            if forced not in keep:
                keep.append(forced)
        keep_names = [str(name or "").strip() for name in keep if str(name or "").strip()]
        execution_bundle = self._frontdoor_execution_bundle(state=state, runtime=runtime)
        declared_tools = dict(execution_bundle.declared_tools or {})
        registry = getattr(self._loop, "tools", None)
        registry_lookup = getattr(registry, "get", None)
        retired_lookup = lookup_tool_schema
        session = getattr(getattr(runtime, "context", None), "session", None)
        # 整表引用：上一次名单真的变化时渲出来的原字节。实例与删除前快照都是进程内的，
        # 重启即空，而钉住的名单会从会话基线里活过来——没有这一档，重启后那一跳只能删名。
        bundled_schemas = dict(getattr(session, "_frontdoor_pinned_tool_schema_bundle", None) or {})
        # 单源化（开关 `G3KU_TOOLS_BUNDLE_SINGLE_SOURCE`，默认关）：参数表只有整表这一个来源，
        # 声明面/注册表的活实例只在"构建整表"的那一刻被读。于是同名工具被编辑不再于冻结跳
        # 改字节；整表缺名（含存量第一份还没有后来的成员）就地重建，重建也拿不到才占位。
        if tools_bundle_single_source_enabled():
            # 整表的载体按车道是两份：会话属性 + 按 session_key 分桶的进程表。同一回合内不同
            # 装配路拿到的 session 不是同一个实例，只认会话属性的话，换一个对象就读不到整表，
            # 于是回退去读活实例——单源就在那个装配点破了，所以这一份不是性能优化而是正确性。
            bundle_session_key = str(state.get("session_key") or "").strip()
            bundle_ref_path = str(getattr(session, "_frontdoor_pinned_tool_bundle_path", "") or "").strip()
            if not bundled_schemas:
                bundled_schemas = pinned_tool_bundle.index_for_session(bundle_session_key)
            # "从没写过"与"写过但读不出来"在索引上都表现为空表，处置却不同：后者是故障，必须
            # 当场可数（满盘写坏的那种载体就是 0 字节）。路径在、内容读不出 ⇒ 读不到。
            bundle_read_fault = bool(bundle_ref_path and not bundled_schemas)
            if bundle_read_fault:
                logger.warning(
                    "frontdoor pinned tools[] bundle could not be read, forcing a reprint: session_key={} path={}",
                    bundle_session_key,
                    bundle_ref_path,
                )
            bundle_mode = str(state.get("provider_tool_bundle_mode") or "").strip()
            at_boundary = (
                bundle_mode != "pinned_frozen"
                if bundle_mode
                else sorted(keep_names) != sorted(bundled_schemas.keys())
            )
            missing_names = [name for name in keep_names if not isinstance(bundled_schemas.get(name), dict)]
            rebuilt_placeholders: list[str] = []
            compat_rebuild = bool(bundled_schemas and missing_names)
            if at_boundary or missing_names or not bundled_schemas:
                rebuilt = self._frontdoor_rebuild_pinned_tool_bundle(
                    keep_names=keep_names,
                    declared_tools=declared_tools,
                    registry_lookup=registry_lookup,
                    prior_index=bundled_schemas,
                    prior_index_first=compat_rebuild,
                )
                if rebuilt.get("index"):
                    bundled_schemas = dict(rebuilt["index"])
                    rebuilt_placeholders = list(rebuilt.get("placeholders") or [])
                    self._persist_frontdoor_pinned_tool_bundle(
                        session=session,
                        session_key=bundle_session_key,
                        keep_names=keep_names,
                        rendered=[bundled_schemas[name] for name in keep_names if name in bundled_schemas],
                        build_reason=self._frontdoor_bundle_build_reason(
                            bundle_mode=bundle_mode,
                            seeded=not bundle_ref_path and not compat_rebuild,
                            compat_rebuild=compat_rebuild or bundle_read_fault,
                        ),
                    )
            single_source_rendered: list[dict[str, Any]] = []
            for name in keep_names:
                payload = bundled_schemas.get(name)
                if isinstance(payload, dict):
                    single_source_rendered.append(dict(payload))
                    continue
                # 连重建都没拿到（写盘也失败的分支）：名字留在清单里落占位，不静默摘名。
                single_source_rendered.append(dict(placeholder_tool_schema(name)))
                rebuilt_placeholders.append(str(name))
            state["bundle_placeholder_names"] = list(rebuilt_placeholders)
            # 两类重建分开计：`bundle_compat_rebuild` 是"整表里没有后来新增的成员/整表读不出"
            # 这种兼容重建（每个在飞会话一次），`bundle_fault` 是载体本身读不出或写不出去。
            # 读不出同时进这两栏是真的（既重建了、也是故障），但写失败只进 fault、边界重印
            # 只进 rebuild——混成一栏会把灰度期的正常收口读成故障风暴。
            state["bundle_compat_rebuild"] = int(compat_rebuild or bundle_read_fault)
            state["bundle_fault"] = int(
                bundle_read_fault or bool(getattr(session, "_frontdoor_pinned_tool_bundle_fault", 0))
            )
            state["frontdoor_tool_schema_hits"] = {
                "declared": 0,
                "registry": 0,
                "retired": 0,
                "bundle": len(keep_names) - len(rebuilt_placeholders),
                "placeholder": len(rebuilt_placeholders),
            }
            state["frontdoor_tool_schema_fallback_opportunities"] = len(rebuilt_placeholders)
            return single_source_rendered
        tools_by_name: dict[str, Any] = {}
        placeholder_names: list[str] = []
        schema_source_hits = {"declared": 0, "registry": 0, "retired": 0, "bundle": 0, "placeholder": 0}
        for name in keep:
            tool = declared_tools.get(name)
            if tool is not None:
                schema_source_hits["declared"] += 1
            elif callable(registry_lookup):
                try:
                    tool = registry_lookup(name)
                except Exception:
                    tool = None
                if tool is not None:
                    schema_source_hits["registry"] += 1
            if tool is None:
                retired = retired_lookup(name) if callable(retired_lookup) else None
                bundled = bundled_schemas.get(name)
                if isinstance(retired, dict):
                    payload = retired
                    schema_source_hits["retired"] += 1
                elif isinstance(bundled, dict):
                    payload = bundled
                    schema_source_hits["bundle"] += 1
                else:
                    # 三档全空：名字留住，参数表明示这一跳没带出来。删名与占位的断点位置
                    # 相同（都从这一条的起始字节起断），占位不省缓存，省的是语义。
                    payload = placeholder_tool_schema(name)
                    schema_source_hits["placeholder"] += 1
                    placeholder_names.append(name)
                tool = SimpleNamespace(
                    name=name,
                    description="",
                    parameters={},
                    to_model_schema=lambda payload=payload: payload,
                )
            tools_by_name[name] = tool
        rendered = _provider_tool_schemas(tools_by_name)
        # 占位是一次渲染期的事实，落进 state 让这一跳的工件把它记下来：没有可数落点的
        # 兜底等于没有兜底——"名字还在"与"名字是占位"在工具哈希上看不出区别。
        state["bundle_placeholder_names"] = list(placeholder_names)
        state["bundle_compat_rebuild"] = 0
        # 按档计数与机会分母：开关关闭这一支保持阶段 1 的形状（参数表仍按实例→快照→整表取），
        # 读数词汇与单源那支一致，灰度两侧能直接对着比。
        state["frontdoor_tool_schema_hits"] = dict(schema_source_hits)
        state["frontdoor_tool_schema_fallback_opportunities"] = int(
            schema_source_hits["retired"] + schema_source_hits["bundle"] + schema_source_hits["placeholder"]
        )
        # 整表在名单变化或出现过占位的那一跳重写（常态跳不写盘）。
        bundle_changed = placeholder_names or sorted(keep_names) != sorted(bundled_schemas.keys())
        if session is not None and bundle_changed:
            self._persist_frontdoor_pinned_tool_bundle(
                session=session,
                session_key=str(state.get("session_key") or "").strip(),
                keep_names=keep_names,
                rendered=rendered,
                build_reason="membership_changed",
            )
        state["bundle_fault"] = int(bool(getattr(session, "_frontdoor_pinned_tool_bundle_fault", 0)))
        return rendered

    @staticmethod
    def _frontdoor_bundle_build_reason(*, bundle_mode: str, seeded: bool, compat_rebuild: bool) -> str:
        """整表这次重写的原因，落在工件里可读的三档：首次播种 / 边界重印 / 兼容重建。

        兼容重建（整表缺后来新增的成员，或整表读不出来）每在飞会话只发生一次；把它和边界重印
        混成一档，灰度期就分不清"正常收口"与"载体坏了"。
        """
        if seeded:
            return "seeded"
        if compat_rebuild:
            return "forced_reprint"
        return str(bundle_mode or "membership_changed")

    @staticmethod
    def _frontdoor_wire_tool_schema_readings(tool_schemas: list[Any] | None) -> dict[str, Any]:
        """线上那份 `tools[]` 的三枚读数：内容哈希、**按位置**的名字序列哈希、逐名内容哈希。

        工件里的 `actual_tool_schema_hash` 走 `_tool_signature_hash`，签名是按名字排序之后才
        哈希的（`main/runtime/chat_backend.py:463-477`）⇒ 它对 tools[] 的顺序是盲的。钉住清单
        要守的恰恰是"成员没变而字节变"，所以顺序与逐名各另存一枚。都是新增字段，不动
        `_tool_signature_hash` 本体（它另有消费者）。
        """
        payload = [dict(item) for item in list(tool_schemas or []) if isinstance(item, dict)]
        names = [schema_name(item) for item in payload]
        item_hashes: dict[str, str] = {}
        for name, item in zip(names, payload):
            if not name or name in item_hashes:
                continue
            item_hashes[name] = hashlib.sha256(
                json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
        return {
            "content_hash": str(
                build_actual_request_diagnostics(
                    request_messages=[],
                    tool_schemas=payload,
                ).get("actual_tool_schema_hash")
                or ""
            ).strip(),
            "sequence_hash": hashlib.sha256(
                json.dumps(list(names), ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
            "item_hashes": item_hashes,
        }

    def _record_frontdoor_wire_tool_schema_readings(
        self,
        *,
        session: Any,
        state: dict[str, Any],
        tool_schemas: list[Any] | None,
    ) -> None:
        """把当跳线上 `tools[]` 的读数写进 state，并在"名单与顺序都没动、字节却变了"时点名。

        比对只在两侧都有读数、且序列哈希相同的那一跳成立：序列不同说明跨过了重印边界（名单
        本来就该变），那不是回归。首跳与存量会话（上一跳还没这份字段）直接跳过——**没有可比
        对象不等于通过**，判读时按"未比对"记。
        """
        readings = self._frontdoor_wire_tool_schema_readings(tool_schemas)
        state["frontdoor_wire_tool_schema_hash"] = readings["content_hash"]
        # 方案 C 之后工件里那份 `tool_schemas` 就是发送体那一份；存量记录没有这个键，读侧按
        # `proxy`（预检另渲的那份）处理，比对时不拿工具哈希相等当硬条件。
        state["frontdoor_tools_record_basis"] = "wire"
        state["frontdoor_wire_tool_name_sequence_hash"] = readings["sequence_hash"]
        state["frontdoor_wire_tool_schema_item_hashes"] = dict(readings["item_hashes"])
        previous = self._frontdoor_latest_actual_request_record(session=session, state=None) or {}
        previous_sequence = str(previous.get("wire_tool_name_sequence_hash") or "").strip()
        previous_content = str(previous.get("wire_tool_schema_hash") or "").strip()
        if not previous_sequence or not previous_content:
            return
        if previous_sequence != readings["sequence_hash"] or previous_content == readings["content_hash"]:
            return
        previous_items = dict(previous.get("wire_tool_schema_item_hashes") or {})
        changed_names = [
            name
            for name, digest in list(readings["item_hashes"].items())
            if str(previous_items.get(name) or "") and str(previous_items.get(name)) != digest
        ]
        logger.warning(
            "frontdoor tools[] bytes changed outside a reprint boundary: session_key={} previous={} current={} changed={}",
            str(getattr(getattr(session, "state", None), "session_key", "") or "").strip(),
            previous_content[:16],
            readings["content_hash"][:16],
            ",".join(changed_names) or "(unknown)",
        )

    def _advance_frontdoor_stash_hop(self, session: Any) -> tuple[dict[str, Any], list[str]]:
        """推进前门侧的暂存时钟（只在还留着条目时）。与节点道同一条规则、同一份清扫。

        过了严格一跳的条目一并清掉，并把被清的名字回给调用方（D15 的 `abandoned` 档）：
        不清的话它们整回合都留在会话载体里，而"这批为什么没跑"没有任何地方看得见。
        """
        carrier = getattr(session, '_frontdoor_tool_arg_stash', {}) or {}
        container = tool_arg_stash.carrier_container(carrier)
        if not container:
            return carrier, []
        next_hop = tool_arg_stash.read_hop(carrier) + 1
        swept_container, dropped_scopes = tool_arg_stash.sweep_expired(
            container,
            current_hop=next_hop,
            present_tool_ids=None,
        )
        next_carrier = tool_arg_stash.build_carrier(swept_container, hop=next_hop)
        setattr(session, '_frontdoor_tool_arg_stash', next_carrier)
        # 清扫发生在执行阶段，而 `frontdoor_selection_debug` 在下一跳的调用前写：把被清的
        # 名字暂存在会话上，由那一跳的读数带走（进程内交接，不落盘——落盘的读数只有那一份）。
        setattr(session, '_frontdoor_stash_swept', [
            tool_arg_stash.scope_tool_id(scope) for scope in dropped_scopes
        ])
        return next_carrier, [tool_arg_stash.scope_tool_id(scope) for scope in dropped_scopes]

    def _frontdoor_contract_presence_reading(
        self,
        *,
        state: CeoGraphState | dict[str, Any] | None,
        candidate_tool_names: list[str],
        session: Any,
    ) -> dict[str, Any]:
        """D15 的每跳在场读数，落在 `frontdoor_selection_debug`（这份已整份持久化）。

        视图与在场权威同源：走 `_frontdoor_contract_presence_index` 那一条（`state['messages']`
        去掉已裁撤阶段的行）。读数若另取一份视图，就会报出与准入不同的在场集合——那正是本次
        改造要消灭的形状。
        """
        view = self._frontdoor_view_without_evicted_stage_rows(
            state, list((state or {}).get("messages") or [])
        )
        revisions = loader_contract_revisions(view)
        index = contract_index_from_revisions(revisions)
        session_key = str(getattr(getattr(session, 'state', None), 'session_key', '') or '').strip()
        container = tool_arg_stash.read_container(
            getattr(session, '_frontdoor_tool_arg_stash', None) or {}
        )

        def _staged_count_of(tool_id: str) -> int:
            if not session_key:
                return 0
            return int(
                tool_arg_stash.staged_count(
                    container,
                    scope_key=tool_arg_stash.session_scope_key(session_key, tool_id),
                )
                or 0
            )

        return contract_presence_reading(
            [*list(index), *list(candidate_tool_names or [])],
            index=index,
            revisions=revisions,
            staged_count_of=_staged_count_of,
        )

    def _frontdoor_impulse_contract_content(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        state: CeoGraphState | dict[str, Any] | None,
        runtime_context: dict[str, Any],
    ) -> tuple[str, str] | None:
        """未水合工具被直接调用时的一跳：交契约 + 暂存入参，**不执行**（D3）。

        返回 `(kind, content)`，kind ∈ `staged` / `invalid` / `failed`；`None` 表示这
        不是"没水合"这一类（已在水合面里、常驻内置、或投递不可用），调用方按原路走。
        `failed` 只在校验投递本身抛异常时出现——那种情况下**不放行执行**，否则正好破掉
        "执行发生在契约可见之后"这条不变量。
        """
        name = str(tool_name or '').strip()
        if not name or name in CEO_NEVER_CONTRACT_TOOL_NAMES:
            return None
        session = (runtime_context or {}).get('runtime_session')
        if session is None:
            return None
        service = getattr(self._loop, 'main_task_service', None)
        deliver = getattr(service, 'deliver_impulse_contract', None) if service is not None else None
        if not callable(deliver):
            return None
        view = self._frontdoor_view_without_evicted_stage_rows(state, list((state or {}).get('messages') or []))
        presence_index = runtime_context.get('tool_contract_presence')
        if not isinstance(presence_index, dict):
            presence_index = contract_presence_index(request_messages=view)
        if name in presence_index:
            return None
        session_key = str(
            (runtime_context or {}).get('session_key') or getattr(session, 'session_key', '') or 'web:shared'
        ).strip() or 'web:shared'
        delivered = runtime_context.get('impulse_delivered_tools')
        if not isinstance(delivered, dict):
            delivered = {}
            runtime_context['impulse_delivered_tools'] = delivered
        position = int(delivered.get(name, 0) or 0) + 1
        totals = runtime_context.get('impulse_batch_totals')
        batch_total = int(totals.get(name, position) or position) if isinstance(totals, dict) else position
        carrier = runtime_context.get('tool_arg_stash_carrier') or getattr(session, '_frontdoor_tool_arg_stash', {}) or {}
        try:
            result = deliver(
                actor_role='ceo',
                session_id=session_key,
                tool_id=name,
                arguments=dict(arguments or {}),
                scope_key=tool_arg_stash.session_scope_key(session_key, name),
                carrier=carrier,
                batch_position=position,
                batch_total=max(position, batch_total),
                runtime_context=runtime_context,
            )
        except Exception as exc:
            return ('failed', f'could not deliver the contract for {name}: {exc}')
        if not isinstance(result, dict) or str(result.get('outcome') or '').strip() == IMPULSE_REJECT:
            return None
        setattr(session, '_frontdoor_tool_arg_stash', result.get('carrier') or {})
        runtime_context['tool_arg_stash_carrier'] = result.get('carrier') or {}
        delivered[name] = position
        return (
            str(result.get('outcome') or IMPULSE_STAGED),
            str(result.get('content') or ''),
        )

    async def _frontdoor_reuse_args_execution(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        state: CeoGraphState | dict[str, Any] | None,
        runtime_context: dict[str, Any],
        execute_one: Callable[[dict[str, Any]], Awaitable[tuple[str, str]]],
        call_id: str,
    ) -> str | None:
        """前门道的 `reuse_args=true`：把当轮全部暂存入参跑完，聚合成一个结果（D7）。

        与节点道同一套判定（`plan_reuse_execution`）、同一套回执，差别只在作用域键是会话
        级、以及执行走本车道已有的单次调用（每份结果先各自渲染与外置，再聚合）。逐项
        `ok` 取执行侧状态：外置后的失败信封不以 `Error` 开头，文本判据认不出来。
        返回 None 表示这不是复用调用，调用方按普通路径走。

        这一层是前门道上唯一知道"这次到底走了哪条道"的地方，所以 D15 的 `execution_path`
        与补格 `staged_replay` 在这里按 call id 记一次，交给执行结果行落盘——下游不许拿
        回执文本反推。
        """
        reuse_requested, explicit_arguments = split_reuse_arguments(arguments)
        name = str(tool_name or '').strip()
        if not name:
            return None
        session = (runtime_context or {}).get('runtime_session')
        session_key = str(
            (runtime_context or {}).get('session_key') or getattr(session, 'session_key', '') or 'web:shared'
        ).strip() or 'web:shared'
        carrier = (
            runtime_context.get('tool_arg_stash_carrier')
            or getattr(session, '_frontdoor_tool_arg_stash', {})
            or {}
        )
        container = tool_arg_stash.read_container(carrier)
        hop = tool_arg_stash.read_hop(carrier)
        scope_key = tool_arg_stash.session_scope_key(session_key, name)
        resolved: dict[str, Any] = {}

        def _staged_view() -> tuple[list[dict[str, Any]], str]:
            # 在场视图与权威同源：走 `_frontdoor_contract_presence_index` 那一条。
            # 只在容器里真有该名字时才摊这份视图——普通调用一律零成本返回。
            if 'entries' not in resolved:
                view = self._frontdoor_view_without_evicted_stage_rows(
                    state, list((state or {}).get('messages') or [])
                )
                presence_index = runtime_context.get('tool_contract_presence')
                if not isinstance(presence_index, dict):
                    presence_index = contract_presence_index(request_messages=view)
                fingerprint = str(presence_index.get(name) or '').strip()
                resolved['entries'] = tool_arg_stash.peek_entries(
                    container,
                    scope_key=scope_key,
                    current_hop=hop,
                    fingerprint=fingerprint,
                )
                resolved['fingerprint'] = fingerprint
            return list(resolved['entries']), str(resolved['fingerprint'] or '')

        if not reuse_requested:
            if container.get(scope_key):
                entries, _fingerprint = _staged_view()
                if entries:
                    self._record_frontdoor_execution_path(
                        runtime_context,
                        call_id,
                        EXECUTION_PATH_CORRECTED,
                        staged_replay=staged_replay_kind(
                            staged_entries=entries,
                            arguments=dict(arguments or {}),
                            digest_of=tool_arg_stash.args_digest,
                        ),
                    )
            return None
        staged_entries, fingerprint = _staged_view()
        plan = plan_reuse_execution(
            reuse_requested=True,
            explicit_arguments=explicit_arguments,
            contract_present=bool(fingerprint),
            staged_entries=staged_entries,
        )
        kind = str(plan.get('kind') or '').strip()
        if kind == REUSE_PLAN_EXPLICIT_ARGS:
            # 带 reuse 又给显式入参：字段误用。刚为它暂存过东西 ⇒ 模型主动改参数重调
            # （`corrected`）；纯误填算非法复用。两者在排障上是两回事（与节点道同判据）。
            self._record_frontdoor_execution_path(
                runtime_context,
                call_id,
                EXECUTION_PATH_CORRECTED if staged_entries else EXECUTION_PATH_ILLEGAL_REUSE,
                staged_replay=staged_replay_kind(
                    staged_entries=staged_entries,
                    arguments=explicit_arguments,
                    digest_of=tool_arg_stash.args_digest,
                ) if staged_entries else "",
                reuse_flag_ignored=True,
            )
            executed_text, _executed_status = await execute_one(dict(plan.get('arguments') or {}))
            self._persist_frontdoor_stash_container(
                container=tool_arg_stash.consume_entries(
                    container,
                    scope_key=scope_key,
                    current_hop=hop,
                    fingerprint=fingerprint,
                )[0],
                hop=hop,
                session=session,
                runtime_context=runtime_context,
            )
            return f"{reuse_refusal_text(kind, name)}\n\n{executed_text}"
        if kind in {REUSE_PLAN_NO_STASH, REUSE_PLAN_CONTRACT_ABSENT}:
            self._record_frontdoor_execution_path(runtime_context, call_id, EXECUTION_PATH_ILLEGAL_REUSE)
            if kind == REUSE_PLAN_CONTRACT_ABSENT and container.get(scope_key):
                self._persist_frontdoor_stash_container(
                    container=tool_arg_stash.drop_scope(container, scope_key=scope_key),
                    hop=hop,
                    session=session,
                    runtime_context=runtime_context,
                )
            return reuse_refusal_text(kind, name)
        if kind != REUSE_PLAN_EXECUTE:
            return None
        self._record_frontdoor_execution_path(runtime_context, call_id, EXECUTION_PATH_REUSED)
        entries = [dict(item) for item in list(plan.get('entries') or []) if isinstance(item, dict)]
        items: list[dict[str, Any]] = []
        for index, entry in enumerate(entries):
            entry_arguments = dict(entry.get(tool_arg_stash.ARGS_FIELD) or {})
            text, entry_status = await execute_one(entry_arguments)
            text = str(text or '')
            item: dict[str, Any] = {
                'index': index,
                'args_digest': str(entry.get(tool_arg_stash.DIGEST_FIELD) or tool_arg_stash.args_digest(entry_arguments)),
                'ok': str(entry_status or 'success') == 'success',
            }
            item['error' if item['ok'] is False else 'result'] = text
            items.append(item)
        self._persist_frontdoor_stash_container(
            container=tool_arg_stash.consume_entries(
                container,
                scope_key=scope_key,
                current_hop=hop,
                fingerprint=fingerprint,
            )[0],
            hop=hop,
            session=session,
            runtime_context=runtime_context,
        )
        return aggregate_reuse_results(tool_name=name, items=items)

    def _record_frontdoor_execution_path(
        self,
        runtime_context: dict[str, Any],
        call_id: str,
        path: str,
        *,
        staged_replay: str = "",
        reuse_flag_ignored: bool = False,
    ) -> None:
        """按 call id 记下这次调用实际走的道，供执行结果行落盘（D15 与 `staged_replay` 补格）。

        只有这一层知道"批量语义生效 / 字段误用 / 改参数重调 / 同一份入参重发"，而审计行在
        阶段记录那侧按 call 落，所以在这里交接一次——下游用回执文本反推会得到另一套数。
        `reuse_flag_ignored` 再分一刀：打了标记却被显式参数顶掉，和根本没打标记，在
        `corrected` 里是同一个词，可它们要修的地方完全不同（schema 形状 vs 模型的取舍）。
        """
        key = str(call_id or '').strip()
        if not key or not str(path or '').strip():
            return
        paths = runtime_context.get('tool_execution_paths')
        if not isinstance(paths, dict):
            paths = {}
            runtime_context['tool_execution_paths'] = paths
        paths[key] = str(path).strip()
        replay = str(staged_replay or '').strip()
        if replay:
            rows = runtime_context.get('tool_staged_replay')
            if not isinstance(rows, dict):
                rows = {}
                runtime_context['tool_staged_replay'] = rows
            rows[key] = replay
        if reuse_flag_ignored:
            ignored = runtime_context.get('tool_reuse_flag_ignored')
            if not isinstance(ignored, dict):
                ignored = {}
                runtime_context['tool_reuse_flag_ignored'] = ignored
            ignored[key] = True

    def _persist_frontdoor_stash_container(
        self,
        *,
        container: dict[str, Any],
        hop: int,
        session: Any,
        runtime_context: dict[str, Any],
    ) -> None:
        """消费后的载体同时写回会话对象与本轮 runtime_context，避免同跳两份读口径。"""
        next_carrier = tool_arg_stash.build_carrier(container, hop=hop)
        if session is not None:
            setattr(session, '_frontdoor_tool_arg_stash', next_carrier)
        runtime_context['tool_arg_stash_carrier'] = next_carrier

    def _frontdoor_execution_bundle(
        self,
        *,
        state: CeoGraphState,
        runtime: CeoRuntime,
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
            drop_completed_stage_tool_detail: bool = False,
        ) -> dict[str, Any]:
            # 与 durable 侧重建共用同一个入口，差别只在 `archive=False`：归档要落在
            # durable 账本那一步，写在一份会被覆盖掉的工作副本上只会多留一份没人引用的文件。
            next_stage_state, stage_payload = self._frontdoor_submit_next_stage(
                mutable_stage_state,
                session_key=str(state.get("session_key") or "").strip(),
                arguments={
                    "stage_goal": stage_goal,
                    "tool_round_budget": tool_round_budget,
                    "completed_stage_summary": completed_stage_summary,
                    "key_refs": key_refs or [],
                    "final": final,
                    "drop_completed_stage_tool_detail": drop_completed_stage_tool_detail,
                },
                preamble_text=str(state.get("analysis_text") or "").strip(),
                archive=False,
            )
            mutable_stage_state.clear()
            mutable_stage_state.update(next_stage_state)
            return stage_payload

        all_tools = {
            **registered_tools,
            STAGE_TOOL_NAME: SubmitNextStageTool(_submit_stage),
            SILENT_TOOL_NAME: SilentTool(),
        }
        # 声明面与准入面分源：schema 用的实例按钉住的派发名单单独解析（同一道
        # `_frontdoor_provider_visible_tool_names` 契约投影校验），控制工具复用同一份
        # 实例；两者分家后 `tools[]` 不随 callable 收窄。
        declared_tools = {
            **self._registered_tools(
                self._frontdoor_provider_visible_tool_names(
                    self._frontdoor_dispatch_tool_names(state)
                )
            ),
            STAGE_TOOL_NAME: all_tools[STAGE_TOOL_NAME],
            SILENT_TOOL_NAME: all_tools[SILENT_TOOL_NAME],
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
            declared_tools=declared_tools,
            runtime_context=runtime_context,
            on_progress=runtime_context.get("on_progress"),
        )

    @staticmethod
    def _model_response_view(message: dict[str, Any]) -> Any:
        payload = dict(message or {})
        return type(
            "ModelResponseView",
            (),
            {
                "content": payload.get("content", ""),
                "tool_calls": list(payload.get("tool_calls", None) or []),
                "finish_reason": str(payload.get("finish_reason", "stop") or "stop"),
                "error_text": str(payload.get("error_text", "") or ""),
                "error_kind": str(payload.get("error_kind", "") or ""),
                "error_code": str(payload.get("error_code", "") or ""),
                "error_status": payload.get("error_status"),
                "reasoning_content": payload.get("reasoning_content"),
                "thinking_blocks": payload.get("thinking_blocks"),
                "reasoning_items": payload.get("reasoning_items"),
                "reasoning_context_allowed": bool(payload.get("reasoning_context_allowed") or False),
                "stream_incomplete": bool(payload.get("stream_incomplete") or False),
                "provider_request_meta": payload.get("provider_request_meta"),
                "provider_request_body": payload.get("provider_request_body"),
            },
        )()

    @staticmethod
    def _model_response_usage(message: dict[str, Any]) -> dict[str, int]:
        return normalize_usage_payload((dict(message or {})).get("usage"))

    def _checkpoint_safe_provider_request_body(
        self,
        provider_request_body: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(provider_request_body, dict):
            return _checkpoint_safe_value(provider_request_body)
        payload = dict(provider_request_body or {})
        input_payload = payload.get("input")
        tool_payload = payload.get("tools")
        input_count = 0
        if isinstance(input_payload, list):
            input_count = len(input_payload)
        elif input_payload not in (None, "", [], {}):
            input_count = 1
        tools_count = 0
        if isinstance(tool_payload, list):
            tools_count = len(tool_payload)
        elif tool_payload not in (None, "", [], {}):
            tools_count = 1
        summary: dict[str, Any] = {
            "input_count": int(input_count),
            "tools_count": int(tools_count),
            "contains_multimodal": bool(_extract_image_blocks(payload)),
        }
        for key, value in payload.items():
            normalized_key = str(key or "").strip()
            if not normalized_key or normalized_key in {"input", "tools"}:
                continue
            if value is None:
                continue
            if isinstance(value, str | int | float | bool):
                summary[normalized_key] = value
                continue
            if normalized_key in {"text", "reasoning", "metadata"} and isinstance(value, dict):
                summary[normalized_key] = _checkpoint_safe_value(_payload_without_inline_images(value))
        return dict(_checkpoint_safe_value(summary) or {})

    def _checkpoint_safe_stable_messages(
        self,
        messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        return strip_multimodal_blocks_from_message_records(
            self._prompt_message_records(messages)
        )

    def _checkpoint_safe_model_response_payload(self, message: dict[str, Any]) -> dict[str, Any]:
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
            "reasoning_items": _checkpoint_safe_value(response_view.reasoning_items),
            "reasoning_context_allowed": bool(response_view.reasoning_context_allowed),
            "stream_incomplete": bool(response_view.stream_incomplete),
            "provider_request_meta": _checkpoint_safe_value(response_view.provider_request_meta),
            "provider_request_body": self._checkpoint_safe_provider_request_body(response_view.provider_request_body),
        }

    @staticmethod
    def _frontdoor_assistant_reasoning_field(response_payload: dict[str, Any]) -> dict[str, Any]:
        """该跳的思考要不要落进这条 assistant 行——只看发送侧链级闸门的判定结果。

        chat 协议落 `reasoning_content` 文本，Responses 协议落加密 `reasoning_items` 项；正文被
        截断或外置都不影响它——思考整段随行，随它所在那一跳的阶段过期点或摘要区间一起退出。
        """
        if not bool(response_payload.get("reasoning_context_allowed")):
            return {}
        field: dict[str, Any] = {}
        if str(response_payload.get("reasoning_content") or "").strip():
            field["reasoning_content"] = response_payload.get("reasoning_content")
        items = [
            dict(item)
            for item in list(response_payload.get("reasoning_items") or [])
            if isinstance(item, dict)
        ]
        if items:
            field["reasoning_items"] = items
        return field

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
    def _strip_legacy_silent_sentinel_line(text: str) -> str:
        """剥掉首行或末行上孤立存在的旧静默哨兵，只当噪声清洗，不作静默判据。"""
        lines = [line for line in str(text or "").splitlines()]
        trimmed = list(lines)
        changed = False
        while trimmed and trimmed[0].strip() == LEGACY_SILENT_SENTINEL:
            trimmed.pop(0)
            changed = True
        while trimmed and trimmed[-1].strip() == LEGACY_SILENT_SENTINEL:
            trimmed.pop(-1)
            changed = True
        if not changed:
            return str(text or "").strip()
        return "\n".join(trimmed).strip()

    @staticmethod
    def _silent_signal_from_tool_payloads(payloads: list[dict[str, Any]] | None) -> dict[str, Any]:
        """取本轮最后一次 `silent` 调用的判据；没有则返回 {}。

        只在这里解析一次：工具参数同时充当审计载荷与痕迹正文，finalize 与转录落盘
        都读解析结果，避免像旧的文案哨兵那样在多处重复匹配字符串。
        """
        latest: dict[str, Any] = {}
        for item in list(payloads or []):
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip() != SILENT_TOOL_NAME:
                continue
            arguments = dict(item.get("arguments") or {}) if isinstance(item.get("arguments"), dict) else {}
            latest = {
                "reason": str(arguments.get("reason") or "").strip(),
                "subject": str(arguments.get("subject") or "").strip(),
                "superseded_by": str(arguments.get("superseded_by") or "").strip(),
            }
        return latest

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
            # The duplicate-rejection text also mentions task_append_notice as
            # guidance, so only the dedicated append-notice phrasing (which
            # starts with "现有任务") counts as the append-notice decision.
            rejection_kind = "duplicate"
            if text.startswith("任务未创建：现有任务") and ("task_append_notice" in text or "追加通知" in text):
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

    @staticmethod
    def _model_response_payload_dict(response: Any) -> dict[str, Any]:
        """Flatten one provider response into the payload shape the frontdoor already reads."""
        tool_calls: list[dict[str, Any]] = []
        for call in list(getattr(response, "tool_calls", None) or []):
            arguments = getattr(call, "arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            tool_calls.append(
                {
                    "id": getattr(call, "id", None),
                    "name": str(getattr(call, "name", "") or ""),
                    "args": arguments,
                    "type": "tool_call",
                }
            )
        payload: dict[str, Any] = {
            "content": getattr(response, "content", None) or "",
            "tool_calls": tool_calls,
            "finish_reason": getattr(response, "finish_reason", "stop"),
            "error_text": getattr(response, "error_text", None) or "",
            "error_kind": getattr(response, "error_kind", None) or "",
            "usage": getattr(response, "usage", {}),
        }
        for key in ("reasoning_content", "thinking_blocks", "reasoning_items"):
            value = getattr(response, key, None)
            if value:
                payload[key] = value
        for key in ("error_code", "error_status"):
            value = getattr(response, key, None)
            if value not in (None, ""):
                payload[key] = value
        if getattr(response, "reasoning_context_allowed", False):
            payload["reasoning_context_allowed"] = True
        if getattr(response, "stream_incomplete", False):
            payload["stream_incomplete"] = True
        for key in ("provider_request_meta", "provider_request_body"):
            value = getattr(response, key, None)
            if isinstance(value, dict) and value:
                payload[key] = dict(value)
        return payload

    async def _call_model_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        tool_schemas: list[Any],
        model_refs: list[str],
        parallel_tool_calls: bool | None,
        prompt_cache_key: str,
        on_text_delta: Any = None,
        on_reasoning_delta: Any = None,
        on_reasoning_segment: Any = None,
        on_model_retry_status: Any = None,
        required_context_window_tokens: int = 0,
    ) -> dict[str, Any]:
        response = await self._resolve_chat_backend().chat(
            messages=[dict(item) for item in list(messages or []) if isinstance(item, dict)],
            tools=list(tool_schemas or []) or None,
            model_refs=list(model_refs or []),
            parallel_tool_calls=bool(parallel_tool_calls) if isinstance(parallel_tool_calls, bool) else None,
            prompt_cache_key=(str(prompt_cache_key).strip() or None) if prompt_cache_key is not None else None,
            on_text_delta=on_text_delta,
            on_reasoning_delta=on_reasoning_delta,
            on_reasoning_segment=on_reasoning_segment,
            on_model_retry_status=on_model_retry_status,
            required_context_window_tokens=int(required_context_window_tokens or 0),
        )
        return self._model_response_payload_dict(response)

    async def _graph_prepare_turn(
        self,
        state: CeoGraphState,
        *,
        runtime: CeoRuntime,
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
        # 手动压缩跑在回合外，够不到发送路径那一次压缩搬运；它挂的待重印标记在本跳
        # 的边界判定里被消费，所以要**先**把仍可读的名字补回 durable 基线，这一跳的
        # 种子/装配才读得到它。标记本身留给下面的边界判定消费（一处消费两次读会错位）。
        prepare_session_key = str(getattr(getattr(session, "state", None), "session_key", "") or "").strip()
        manual_recommit_pending = bool(getattr(session, PENDING_PROVIDER_BUNDLE_RECOMMIT_ATTR, False))
        if manual_recommit_pending:
            self._carry_manual_compression_notice_into_baseline(
                session=session,
                session_key=prepare_session_key,
            )
        metadata = _user_input_metadata(user_input)
        builder_user_metadata = dict(metadata or {})
        batch_query_text = str(metadata.get("web_ceo_batch_query_text") or "").strip()
        raw_query_text = str(metadata.get("web_ceo_raw_text") or "").strip()
        query_text = batch_query_text or raw_query_text or self._content_text(user_content)
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
        has_prior_request_body_seed = False
        internal_seed_messages, internal_event_bundle_text, internal_event_message_metadata = (
            self._internal_prompt_seed_messages(metadata=metadata)
        )
        model_refs = self._resolve_ceo_model_refs_for_session(getattr(session.state, "session_key", ""))
        model_refs_revision = self._frontdoor_runtime_config_revision()
        current_turn_user_content = (
            ""
            if cron_internal
            else internal_event_bundle_text
            if heartbeat_internal and internal_event_bundle_text
            else self._merge_prompt_batch_sibling_contents(
                session=session,
                current_turn_id=str(metadata.get("_transcript_turn_id") or "").strip(),
                current_content=self._expand_web_ceo_uploads_for_current_request_content(
                    content=self._model_content(user_content),
                    metadata=metadata,
                    model_refs=model_refs,
                ),
                model_refs=model_refs,
            )
        )
        # 收图与否按当前模型判：web 上传那道在 `_expand_web_ceo_uploads_for_current_request_content`
        # 里已经给过"路径 + 原因"，外部／MCP 附件那道在这里收口。剥块等于让图凭空消失，
        # 所以一律换成文本块；链前进到别的成员时还有咽喉点那侧的同一判据兜底。
        current_turn_user_content = self._degrade_image_content_for_request(
            current_turn_user_content,
            model_refs=model_refs,
        )
        current_turn_has_multimodal_uploads = self._message_content_has_multimodal_blocks(
            current_turn_user_content
        )
        seed_stage_compaction_applied = False
        if session_request_body_messages:
            # 接通 stage 数据源：优先 frontdoor_stage_state，为空时回退 canonical 的 stages，
            # 使续跑路径的 seed 裁剪能拿到完成阶段摘要（无损裁剪的前提）。
            seed_stage_state = dict(current_frontdoor_stage_state or {})
            if not list(seed_stage_state.get("stages") or []):
                seed_stage_state = dict(current_frontdoor_canonical_context or {})
            request_body_seed_messages, seed_stage_compaction_applied = self._trim_frontdoor_seed_stage_compaction(
                session_request_body_messages, seed_stage_state
            )
            # 手动暂停回合的请求从未发出，其用户消息不在基线里；按转录对账补回，
            # 避免“发送后立即暂停再补发”时暂停消息从模型上下文消失。
            request_body_seed_messages = self._reconcile_paused_user_turns_into_seed(
                request_body_seed_messages,
                runtime_session,
                current_turn_user_content=current_turn_user_content,
            )
            checkpoint_messages = []
            builder_user_metadata["_frontdoor_history_seed"] = "session_window"
            has_prior_request_body_seed = True
        # 仅当存在真实的续跑种子（非空基线）时才把内部事件并入其中。冷启动（无基线，
        # 如进程重启后首轮或全新会话首轮）时，这两条内部消息绝不能冒充"完整旧请求体"：
        # 那会让本轮被误判为续跑、走假定种子已含基础系统提示的续跑分支，从而静默
        # 丢掉基础提示。冷启动时内部事件单独传给 builder，由新建路径注入。
        if internal_seed_messages and request_body_seed_messages:
            request_body_seed_messages = [*list(request_body_seed_messages), *list(internal_seed_messages)]
        inherited_internal_contract_state = (
            self._inherited_internal_turn_contract_state(state=state, session=session)
            if (heartbeat_internal or cron_internal) and has_prior_request_body_seed
            else {}
        )
        attachment_reopen_targets: list[dict[str, Any]] = []
        repair_required_tool_items: list[dict[str, Any]] = []
        repair_required_skill_items: list[dict[str, Any]] = []
        compression_state_payload = dict(current_compression_state or self._default_compression_state())
        frontdoor_history_shrink_reason_from_prepare = ""
        turn_overlay_section_count = 0
        if inherited_internal_contract_state:
            attachment_reopen_targets = [
                dict(item)
                for item in list(inherited_internal_contract_state.get("attachment_reopen_targets") or [])
                if isinstance(item, dict)
            ]
            selected_skill_ids = list(inherited_internal_contract_state.get("visible_skill_ids") or [])
            candidate_tool_names = list(inherited_internal_contract_state.get("candidate_tool_names") or [])
            candidate_tool_items = [
                dict(item)
                for item in list(inherited_internal_contract_state.get("candidate_tool_items") or [])
                if isinstance(item, dict)
            ]
            rbac_visible_tool_names = list(inherited_internal_contract_state.get("rbac_visible_tool_names") or [])
            rbac_visible_skill_ids = list(inherited_internal_contract_state.get("rbac_visible_skill_ids") or [])
            repair_required_tool_items = [
                dict(item)
                for item in list(inherited_internal_contract_state.get("repair_required_tool_items") or [])
                if isinstance(item, dict)
            ]
            repair_required_skill_items = [
                dict(item)
                for item in list(inherited_internal_contract_state.get("repair_required_skill_items") or [])
                if isinstance(item, dict)
            ]
            frontdoor_selection_debug = dict(
                inherited_internal_contract_state.get("frontdoor_selection_debug") or {}
            )
            inherited_tool_names = list(inherited_internal_contract_state.get("tool_names") or [])
            # 内部轮（心跳 / cron）继承上一轮的 callable 与候选，继承的那份同样要过在场判据
            # （FIX_PLAN §2.3 末条）：压缩把正文摘掉的那一跳，内部轮不能照旧调用无契约的工具，
            # 也不能把不在场的名字留在 inherited 的 tool_names 行里。
            # 请求视图取 builder 真正会用的那一份：有续跑种子就用种子，没有才回到检查点消息。
            internal_presence_state = {
                "messages": list(request_body_seed_messages or checkpoint_messages or []),
                "frontdoor_stage_state": current_frontdoor_stage_state,
                "frontdoor_canonical_context": current_frontdoor_canonical_context,
                "rbac_visible_tool_names": rbac_visible_tool_names,
                "tool_names": inherited_tool_names,
                "repair_required_tool_items": repair_required_tool_items,
            }
            contract_present_tool_names = self._frontdoor_contract_present_names(internal_presence_state)
            present_set = set(contract_present_tool_names)
            tool_names = [
                name
                for name in inherited_tool_names
                if name in CEO_FIXED_BUILTIN_TOOL_NAMES or name in present_set
            ]
            callable_tool_names = self._frontdoor_callable_tool_names_for_state(
                {
                    **internal_presence_state,
                    "cron_internal": cron_internal,
                    "heartbeat_internal": heartbeat_internal,
                },
                tool_names=tool_names,
            )
            frontdoor_selection_debug["callable_tool_names"] = list(callable_tool_names)
            frontdoor_selection_debug["candidate_tool_names"] = list(candidate_tool_names)
            frontdoor_selection_debug["contract_present_tool_names"] = list(contract_present_tool_names)
            frontdoor_selection_debug["tool_contract_presence"] = self._frontdoor_contract_presence_reading(
                state=internal_presence_state,
                candidate_tool_names=candidate_tool_names,
                session=session,
            )
            frontdoor_selection_debug["staged_swept_tool_ids"] = list(
                getattr(session, '_frontdoor_stash_swept', []) or []
            )
            messages = self._prompt_message_records(request_body_seed_messages)
            has_current_turn_user_content = bool(self._content_text(current_turn_user_content).strip())
            # 末位是不是本轮用户回合：内部规则行（心跳规则/cron 提醒正文）现在落在 user
            # 角色上，必须先跳过它，否则本轮事件束被当成"已在历史里"、永不追加。
            _trailing_record = trailing_turn_record(messages)
            _trailing_is_user = bool(_trailing_record) and str(_trailing_record.get("role") or "").strip().lower() == "user"
            if has_current_turn_user_content and not _trailing_is_user:
                messages.append({"role": "user", "content": current_turn_user_content})
            provider_model = str(model_refs[0] if model_refs else "").strip()
            prior_provider_tool_names = self._normalized_tool_name_state_list(
                state.get("provider_tool_names")
                or getattr(session, "_frontdoor_provider_tool_schema_names", [])
            )
            desired_provider_tool_names = self._frontdoor_provider_visible_tool_names(
                list(
                    rbac_visible_tool_names
                    or inherited_internal_contract_state.get("provider_tool_names")
                    or []
                )
            )
            provider_tool_exposure = self._refresh_frontdoor_provider_tool_bundle(
                prior_provider_tool_names=prior_provider_tool_names,
                desired_provider_tool_names=desired_provider_tool_names,
                prior_history_shrink_reason=str(
                    state.get("frontdoor_history_shrink_reason")
                    or getattr(session, "_frontdoor_history_shrink_reason", "")
                    or ""
                ).strip(),
                recommit_boundary=self._frontdoor_bundle_recommit_boundary(session=session, state=state),
            )
            runtime_visible_tool_names = list(provider_tool_exposure.get("provider_tool_names") or [])
            tool_schemas = self._selected_tool_schemas(runtime_visible_tool_names)
            stable_messages = list(messages)
            dynamic_appendix_messages: list[dict[str, Any]] = []
            cache_family_revision = str(
                inherited_internal_contract_state.get("cache_family_revision") or ""
            ).strip()
            turn_overlay_text = ""
        else:
            exposure = await self._resolver.resolve_for_actor(
                actor_role="ceo",
                session_id=session.state.session_key,
            )
            # 装配路也过同一条在场判据：builder 用这份在场名单组 callable 与 candidate，
            # 少算一次就等于三个算点里有两个口径不同（合同分裂）。
            # 请求视图取 builder 真正会用的那一份：有续跑种子就用种子（走这条时
            # `checkpoint_messages` 已被置空），没有种子才回到状态里的那份消息。
            assembly_presence_state = {
                "messages": list(request_body_seed_messages or checkpoint_messages or []),
                "frontdoor_stage_state": current_frontdoor_stage_state,
                "frontdoor_canonical_context": current_frontdoor_canonical_context,
                "cron_internal": cron_internal,
                "heartbeat_internal": heartbeat_internal,
            }
            contract_present_tool_names = self._frontdoor_contract_present_names(assembly_presence_state)
            assembly = await self._builder.build_for_ceo(
                session=session,
                query_text=builder_query_text,
                exposure=exposure,
                persisted_session=runtime_session,
                checkpoint_messages=checkpoint_messages,
                request_body_seed_messages=request_body_seed_messages,
                internal_seed_messages=internal_seed_messages,
                user_content=current_turn_user_content,
                user_metadata=builder_user_metadata,
                frontdoor_stage_state=current_frontdoor_stage_state,
                frontdoor_canonical_context=current_frontdoor_canonical_context,
                semantic_context_state={},
                contract_present_tool_names=list(contract_present_tool_names),
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
            repair_required_tool_items = [
                dict(item)
                for item in list(getattr(assembly, "repair_required_tool_items", []) or [])
                if isinstance(item, dict)
            ]
            repair_required_skill_items = [
                dict(item)
                for item in list(getattr(assembly, "repair_required_skill_items", []) or [])
                if isinstance(item, dict)
            ]
            compression_state_payload = dict(
                getattr(assembly, "trace", {}).get("compression_state_payload")
                or current_compression_state
                or self._default_compression_state()
            )
            frontdoor_history_shrink_reason_from_prepare = str(
                getattr(assembly, "trace", {}).get("frontdoor_history_shrink_reason") or ""
            ).strip()
            turn_overlay_section_count = int(
                getattr(assembly, "trace", {}).get("turn_overlay_section_count", 0) or 0
            )
            attachment_reopen_targets = [
                dict(item)
                for item in list(getattr(assembly, "trace", {}).get("attachment_reopen_targets") or [])
                if isinstance(item, dict)
            ]
            frontdoor_selection_debug = {
                "query_text": str(builder_query_text or "").strip(),
                "raw_turn_query_text": str(query_text or "").strip(),
                "tool_selection": dict(getattr(assembly, "trace", {}).get("tool_selection") or {}),
                "selected_skills": list(getattr(assembly, "trace", {}).get("selected_skills") or []),
                "capability_snapshot": dict(getattr(assembly, "trace", {}).get("capability_snapshot") or {}),
                "callable_tool_names": [],
                "candidate_tool_names": list(candidate_tool_names),
                "contract_present_tool_names": list(contract_present_tool_names),
                "tool_contract_presence": self._frontdoor_contract_presence_reading(
                    state=assembly_presence_state,
                    candidate_tool_names=candidate_tool_names,
                    session=session,
                ),
                "staged_swept_tool_ids": list(getattr(session, '_frontdoor_stash_swept', []) or []),
            }
            tool_names = list(
                getattr(assembly, "tool_names", None)
                or getattr(assembly, "callable_tool_names", None)
                or []
            )
            callable_tool_names = self._frontdoor_callable_tool_names_for_state(
                {
                    **assembly_presence_state,
                    "rbac_visible_tool_names": rbac_visible_tool_names,                    "tool_names": tool_names,
                    "repair_required_tool_items": repair_required_tool_items,
                },
            )
            frontdoor_selection_debug["callable_tool_names"] = list(callable_tool_names)
            messages = list(assembly.model_messages or [])
            messages = self._prefer_live_user_payload_over_text_history(
                messages=messages,
                live_user_content=current_turn_user_content,
            )
            if current_turn_has_multimodal_uploads:
                messages = self._replace_last_user_message_content(
                    messages=messages,
                    content=current_turn_user_content,
                )
            has_current_turn_user_content = bool(self._content_text(current_turn_user_content).strip())
            # 末位是不是本轮用户回合：内部规则行（心跳规则/cron 提醒正文）现在落在 user
            # 角色上，必须先跳过它，否则本轮事件束被当成"已在历史里"、永不追加。
            _trailing_record = trailing_turn_record(messages)
            _trailing_is_user = bool(_trailing_record) and str(_trailing_record.get("role") or "").strip().lower() == "user"
            if has_current_turn_user_content and not _trailing_is_user:
                messages.append({"role": "user", "content": current_turn_user_content})

            provider_model = str(model_refs[0] if model_refs else "").strip()
            provider_tool_seed_names = list(
                rbac_visible_tool_names
                or [
                    str(item or "").strip()
                    for item in list(exposure.get("tool_names") or [])
                    if str(item or "").strip()
                ]
            )
            desired_provider_tool_names = self._frontdoor_provider_visible_tool_names(
                provider_tool_seed_names
            )
            provider_tool_exposure = self._refresh_frontdoor_provider_tool_bundle(
                prior_provider_tool_names=self._normalized_tool_name_state_list(
                    state.get("provider_tool_names")
                    or getattr(session, "_frontdoor_provider_tool_schema_names", [])
                ),
                desired_provider_tool_names=desired_provider_tool_names,
                prior_history_shrink_reason=str(
                    state.get("frontdoor_history_shrink_reason")
                    or getattr(session, "_frontdoor_history_shrink_reason", "")
                    or ""
                ).strip(),
                recommit_boundary=self._frontdoor_bundle_recommit_boundary(session=session, state=state),
            )
            runtime_visible_tool_names = list(provider_tool_exposure.get("provider_tool_names") or [])
            tool_schemas = self._selected_tool_schemas(runtime_visible_tool_names)
            stable_messages = self._prompt_message_records(getattr(assembly, "stable_messages", None)) or list(messages)
            if current_turn_has_multimodal_uploads:
                stable_messages = self._replace_last_user_message_content(
                    messages=stable_messages,
                    content=current_turn_user_content,
                )
            dynamic_appendix_messages = self._prompt_message_records(
                getattr(assembly, "dynamic_appendix_messages", None)
            )
            cache_family_revision = str(getattr(assembly, "cache_family_revision", "") or "").strip()
            turn_overlay_text = str(getattr(assembly, "turn_overlay_text", "") or "").strip()
        request_session_key = str(getattr(getattr(session, "state", None), "session_key", "") or "").strip()
        pinned_contract_text, pinned_skill_ids = self._frontdoor_pinned_contract(
            session=session,
            session_key=request_session_key,
            skill_ids=list(selected_skill_ids),
        )
        stable_messages = apply_pinned_contract_to_head(stable_messages, pinned_contract_text)
        messages = apply_pinned_contract_to_head(messages, pinned_contract_text)
        # 头部先落，尾块才允许省略这三段：判据取头部原文，不取"本轮算出来了"。
        if not pinned_contract_is_carried_by_head(stable_messages, pinned_contract_text):
            pinned_contract_text, pinned_skill_ids = "", []
        # 新增可读 skill 的提示不在这里检测：检测与投递只发生在真正落请求体的那一处
        # （`_frontdoor_prompt_contract`）。旧机制一条回合内四个装配点共用一次消耗式读，
        # 先跑的把差集领走，落请求体那处永远拿空——提示因此从未上线。
        dynamic_appendix_messages = upsert_frontdoor_tool_contract_message(
            dynamic_appendix_messages,
            build_frontdoor_tool_contract(
                pinned_contract_text=pinned_contract_text,
                pinned_skill_ids=pinned_skill_ids,
                callable_tool_names=list(callable_tool_names),
                candidate_tool_names=list(candidate_tool_names),
                candidate_tool_items=list(candidate_tool_items),
                frontdoor_stage_state=dict(current_frontdoor_stage_state or {}),
                visible_skill_ids=list(selected_skill_ids),
                candidate_skill_ids=list(selected_skill_ids),
                repair_required_tool_items=list(repair_required_tool_items),
                repair_required_skill_items=list(repair_required_skill_items),
                rbac_visible_tool_names=list(rbac_visible_tool_names),
                rbac_visible_skill_ids=list(rbac_visible_skill_ids),
                denied_tool_names=self._frontdoor_declared_denied_tool_names(
                    declared_tool_names=self._frontdoor_schema_renderable_tool_names(
                        list(runtime_visible_tool_names or []),
                        session_key=str(state.get("session_key") or "").strip(),
                    ),
                    granted_tool_names=self._frontdoor_live_granted_tool_names(
                        session_key=str(state.get("session_key") or ""),
                    ),
                ),
                # 本次请求真正带出去的声明名单：尾块据此只渲"能 load 但还没进 tools[]"的差集。
                declared_tool_names=list(runtime_visible_tool_names or []),
                contract_revision=cache_family_revision,
                exec_runtime_policy=(
                    self._loop.main_task_service._current_exec_runtime_policy_payload()
                    if callable(getattr(getattr(self._loop, "main_task_service", None), "_current_exec_runtime_policy_payload", None))
                    else None
                ),
                attachment_reopen_targets=list(attachment_reopen_targets),
                session_temp_dir=self._ceo_session_temp_dir(getattr(getattr(session, "state", None), "session_key", "")),
            ),
        )
        prompt_scope = "ceo_frontdoor"
        live_request_messages = self._prompt_message_records(messages)
        if session_request_body_messages:
            continuity_bridge = {"carried": False}
            consume_continuity_bridge = getattr(session, "_consume_completed_continuity_bridge", None)
            if callable(consume_continuity_bridge):
                continuity_bridge = dict(consume_continuity_bridge() or {})
            live_request_messages = self._fresh_turn_live_request_messages_from_previous_actual_request(
                session=session,
                stable_messages=stable_messages,
                live_request_messages=live_request_messages,
            )
            if bool(continuity_bridge.get("carried")):
                cache_family_revision = str(
                    continuity_bridge.get("exposure_revision") or cache_family_revision or ""
                ).strip()
            if not tools_bundle_single_source_enabled():
                # 上一跳原文整表复用是前门的第五档：单源化后它的位置由钉住整表顶掉，两份
                # 来源并存就会让"整表说的是什么"和"线上发的是什么"再次分家。
                tool_schemas, seeded_provider_tool_names = (
                    self._fresh_turn_tool_schema_seed_from_previous_actual_request(
                        session=session,
                        tool_schemas=tool_schemas,
                        expected_schema_names=(
                            list(continuity_bridge.get("provider_tool_schema_names") or [])
                            if bool(continuity_bridge.get("carried"))
                            else list(runtime_visible_tool_names)
                        ),
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
            overlay_section_count=turn_overlay_section_count,
        )
        messages = list(contract.request_messages)
        stable_messages = list(contract.stable_messages)
        dynamic_appendix_messages = list(contract.dynamic_appendix_messages)
        cache_family_revision = str(contract.cache_family_revision or "").strip()
        prompt_cache_key = contract.prompt_cache_key
        prompt_cache_diagnostics = dict(contract.diagnostics)
        if internal_event_message_metadata and current_turn_user_content:
            messages = self._tag_last_matching_user_message(
                messages,
                content_text=str(current_turn_user_content or ""),
                metadata=internal_event_message_metadata,
            )
            stable_messages = self._tag_last_matching_user_message(
                stable_messages,
                content_text=str(current_turn_user_content or ""),
                metadata=internal_event_message_metadata,
            )
        persisted_messages = list(stable_messages)
        persisted_dynamic_appendix_messages = list(dynamic_appendix_messages)
        if prompt_scope == "ceo_frontdoor":
            request_body_messages, tool_contract_messages = self._split_request_body_and_tool_contract_messages(messages)
            if request_body_messages:
                persisted_messages = self._durable_frontdoor_request_body_messages(request_body_messages)
            if tool_contract_messages:
                persisted_dynamic_appendix_messages = list(tool_contract_messages)
            else:
                persisted_dynamic_appendix_messages = []
        shrink_reason = str(
            frontdoor_history_shrink_reason_from_prepare
            or ("stage_compaction" if seed_stage_compaction_applied else "")
            or state.get("frontdoor_history_shrink_reason")
            or session_shrink_reason
            or ""
        ).strip()
        if session_request_body_messages:
            # Compare shrink on the same provider-facing shape. Session continuity
            # baselines may still carry runtime-only tool metadata such as status
            # or timing fields, but those fields are stripped when the next visible
            # turn rebuilds its request-body seed.
            # Both sides are note-neutral: the turn-only note is stripped from the
            # carried history before this turn and re-appended fresh at the tail,
            # so stripping it here must not count as an illegal shrink.
            # 两侧同形归一：基线与新种子都先剥工具契约/瞬时件/多模态，
            # 再比 token。历史上基线曾混入末位工具契约块，导致新回合（已剥契约）
            # 被误判为「无理由收缩」而永久冻结。
            previous_tokens = estimate_message_tokens(
                CeoMessageBuilder._request_body_seed_records(
                    strip_turn_only_system_note_messages(
                        self._request_body_messages_without_tool_contracts(session_request_body_messages)
                    )
                )
            )
            next_tokens = estimate_message_tokens(
                CeoMessageBuilder._request_body_seed_records(
                    strip_turn_only_system_note_messages(
                        self._request_body_messages_without_tool_contracts(persisted_messages)
                    )
                )
            )
            if next_tokens < previous_tokens and shrink_reason not in self._ALLOWED_FRONTDOOR_SHRINK_REASONS.difference({""}):
                self._quarantine_frontdoor_shrink(
                    session,
                    new_seed=persisted_messages,
                    previous_tokens=previous_tokens,
                    next_tokens=next_tokens,
                )
        if frontdoor_selection_debug:
            # 读数按**装配后**的请求视图重算。上一份基线在续跑前会被阶段压缩改写，本轮
            # 刚加载的工具行又是装配时才拼进请求的——只读种子会让读数与本跳真正发出去的
            # 视图分叉（实盘：上一跳加载的契约在下一跳被报成 absent，模型那边却看得见）。
            frontdoor_selection_debug["tool_contract_presence"] = self._frontdoor_contract_presence_reading(
                state={
                    "messages": messages,
                    "frontdoor_stage_state": current_frontdoor_stage_state,
                    "frontdoor_canonical_context": current_frontdoor_canonical_context,
                },
                candidate_tool_names=list(frontdoor_selection_debug.get("candidate_tool_names") or []),
                session=session,
            )
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
                compression_state_payload
                or current_compression_state
                or self._default_compression_state()
            ),
            "turn_overlay_text": turn_overlay_text or None,
            "frontdoor_selection_debug": frontdoor_selection_debug,
            "tool_names": list(tool_names),
            "provider_tool_names": list(runtime_visible_tool_names),
            # 渲染器用它判"这一跳是不是重印边界"（单源化后整表只在该写的时候写）。
            # 另一条装配分支不带这个键时，渲染器回退到"名单与整表键集是否不同"——成员只在
            # 边界变，两种写法同义；不回退成"取数失败也算边界"。
            "provider_tool_bundle_mode": str(provider_tool_exposure.get("provider_tool_bundle_mode") or "").strip(),
            "pending_provider_tool_names": list(
                provider_tool_exposure.get("pending_provider_tool_names") or []
            ),
            "provider_tool_exposure_pending": bool(
                provider_tool_exposure.get("provider_tool_exposure_pending")
            ),
            "provider_tool_exposure_revision": str(
                provider_tool_exposure.get("provider_tool_exposure_revision") or ""
            ),
            "provider_tool_exposure_commit_reason": str(
                provider_tool_exposure.get("provider_tool_exposure_commit_reason") or ""
            ),
            "candidate_tool_names": list(candidate_tool_names),
            "candidate_tool_items": list(candidate_tool_items),
            "attachment_reopen_targets": [
                dict(item)
                for item in list(attachment_reopen_targets or [])
                if isinstance(item, dict)
            ],
            "repair_required_tool_items": [
                dict(item)
                for item in list(repair_required_tool_items or [])
                if isinstance(item, dict)
            ],
            "repair_required_skill_items": [
                dict(item)
                for item in list(repair_required_skill_items or [])
                if isinstance(item, dict)
            ],
            "visible_skill_ids": list(selected_skill_ids),
            "candidate_skill_ids": list(selected_skill_ids),
            "rbac_visible_tool_names": list(rbac_visible_tool_names),
            "rbac_visible_skill_ids": list(rbac_visible_skill_ids),
            "used_tools": [],
            "route_kind": "direct_reply",
            "verified_task_ids": [],
            "dispatched_task_ids": [],
            "repair_overlay_text": None,
            "xml_repair_attempt_count": 0,
            "xml_repair_excerpt": "",
            "xml_repair_tool_names": [],
            "xml_repair_last_issue": "",
            "tool_contract_echo_attempt_count": 0,
            "stage_block_echo_attempt_count": 0,
            "stage_reply_bounce_count": 0,
            "empty_response_retry_count": 0,
            "heartbeat_internal": heartbeat_internal,
            "cron_internal": cron_internal,
            "model_refs": model_refs,
            "model_refs_revision": model_refs_revision,
            "stable_messages": self._checkpoint_safe_stable_messages(stable_messages),
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
        runtime: CeoRuntime,
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
        model_refs = list(
            state.get("model_refs")
            or self._resolve_ceo_model_refs_for_session(state.get("session_key"))
            or []
        )
        for item in queued_inputs:
            item_metadata = dict(getattr(item, "metadata", {}) or {})
            expanded_content = self._expand_web_ceo_uploads_for_current_request_content(
                content=self._model_content(getattr(item, "content", "")),
                metadata=item_metadata,
                model_refs=model_refs,
            )
            follow_up_messages.append({"role": "user", "content": expanded_content})
            raw_follow_up_text = str(item_metadata.get("web_ceo_raw_text") or "").strip()
            follow_up_text = raw_follow_up_text or self._content_text(getattr(item, "content", ""))
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
            "messages": self._durable_frontdoor_request_body_messages(updated_request_body_messages),
            "frontdoor_live_request_messages": list(updated_request_body_messages),
            "frontdoor_request_body_messages": self._durable_frontdoor_request_body_messages(updated_request_body_messages),
        }
        if merged_query_text:
            update["query_text"] = merged_query_text
        self._sync_runtime_session_frontdoor_state(
            state={**dict(state or {}), **update},
            runtime=runtime,
        )
        # 队列被这一跳接走的那一刻必须自己广播一次，与回合尾 drain 那条 (_run_user_turn)
        # 同形状：网页候选条的真相来自 ceo.state 的整表读数，不补这一帧，条目会一直挂到
        # 下一次恰好发出的状态帧（实测多画 5.8 秒，期间正文已经进气泡了）。
        emit_snapshot = getattr(session, "_emit_state_snapshot", None)
        if callable(emit_snapshot):
            result = emit_snapshot()
            if hasattr(result, "__await__"):
                await result
        return update

    async def _graph_call_model(
        self,
        state: CeoGraphState,
        *,
        runtime: CeoRuntime,
    ) -> dict[str, Any]:
        iteration = int(state.get("iteration", 0) or 0) + 1
        configured_limit = state.get("max_iterations")
        if configured_limit is not None and iteration > max(0, int(configured_limit)):
            raise RuntimeError("CEO frontdoor exceeded maximum iterations")

        state_for_request = dict(state or {})
        # Mid-turn model-chain switches (model_config tool / admin routes) only
        # rewrite config; the turn state still carries prepare_turn's model_refs.
        # Re-resolve at this iteration boundary so the next provider request and
        # the downstream tool runtime context (multimodal gate) follow the new chain.
        state_for_request = self._rotate_frontdoor_model_refs_if_stale(state_for_request)
        runtime_session = getattr(getattr(runtime, "context", None), "session", None)
        assistant_text_delta_handler = None
        assistant_reasoning_delta_handler = None
        assistant_reasoning_segment_handler = None
        assistant_reasoning_segment_close_handler = None
        model_retry_status_handler = None
        if runtime_session is not None:
            callback = getattr(runtime_session, "_handle_assistant_text_delta", None)
            if callable(callback):
                assistant_text_delta_handler = callback
            reasoning_callback = getattr(runtime_session, "_handle_assistant_reasoning_delta", None)
            if callable(reasoning_callback):
                assistant_reasoning_delta_handler = reasoning_callback
            reasoning_segment = getattr(runtime_session, "_begin_assistant_reasoning_segment", None)
            if callable(reasoning_segment):
                assistant_reasoning_segment_handler = reasoning_segment
            reasoning_segment_close = getattr(runtime_session, "_reasoning_close_segment", None)
            if callable(reasoning_segment_close):
                assistant_reasoning_segment_close_handler = reasoning_segment_close
            begin_segment = getattr(runtime_session, "_begin_assistant_text_segment", None)
            if callable(begin_segment):
                begin_segment()
            async def _handle_frontdoor_model_retry_status(status: dict[str, Any]) -> None:
                setattr(
                    runtime_session,
                    "_frontdoor_model_retry_status",
                    (
                        copy.deepcopy(dict(status))
                        if isinstance(status, dict)
                        and str(status.get("state") or "").strip() in MODEL_RETRY_LIVE_STATES
                        else None
                    ),
                )
                emit_snapshot = getattr(runtime_session, "_emit_state_snapshot", None)
                if callable(emit_snapshot):
                    await emit_snapshot()

            model_retry_status_handler = _handle_frontdoor_model_retry_status
        # 发请求前的最后一次收窄：只摘"渲不出参数表"的名字。判据必须与渲染器同源
        # （活实例或删除前快照），用"活实例"当判据会把刚删掉的名字从钉住清单里永久摘掉，
        # 于是这一跳断前缀、下一跳也回不来——删除就退化成了立即删名。
        normalized_provider_tool_names = self._frontdoor_schema_renderable_tool_names(
            list(state_for_request.get("provider_tool_names") or state_for_request.get("tool_names") or []),
            session_key=str(state_for_request.get("session_key") or "").strip(),
            session=getattr(getattr(runtime, "context", None), "session", None),
        )
        if normalized_provider_tool_names:
            state_for_request = {
                **state_for_request,
                "provider_tool_names": list(normalized_provider_tool_names),
                "pending_provider_tool_names": [],
                "provider_tool_exposure_pending": False,
                "provider_tool_exposure_revision": self._provider_tool_exposure_revision(
                    normalized_provider_tool_names
                ),
                "provider_tool_exposure_commit_reason": "",
            }
        follow_up_update = await self._consume_session_follow_up_messages_before_call_model(
            state=state_for_request,
            runtime=runtime,
        )
        if follow_up_update:
            state_for_request = {**state_for_request, **follow_up_update}
        tool_schemas = self._frontdoor_tool_schemas_for_state(state=state_for_request, runtime=runtime)
        # 阶段 0 观测先行：对**线上这一份**取读数并比对，不用预检那份（预检走
        # `_selected_tool_schemas`，名单比 wire 少两颗合成控制工具，拿它当断言对象会永远
        # 读不到"控制工具字节变了"这一类事实）。
        self._record_frontdoor_wire_tool_schema_readings(
            session=getattr(getattr(runtime, "context", None), "session", None),
            state=state_for_request,
            tool_schemas=tool_schemas,
        )
        while True:
            preflight_snapshot = self._frontdoor_send_preflight_snapshot(
                state=state_for_request,
                runtime=runtime,
                tool_schemas=tool_schemas,
            )
            request_messages = list(preflight_snapshot.get("request_messages") or [])
            durable_request_messages = list(preflight_snapshot.get("durable_request_messages") or request_messages)
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
                "anchor_projection_shrink_tokens": int(
                    preflight_snapshot.get("anchor_projection_shrink_tokens") or 0
                ),
                "estimate_source": str(preflight_snapshot.get("estimate_source") or "preview_estimate"),
                "comparable_to_previous_request": bool(preflight_snapshot.get("comparable_to_previous_request")),
                "final_estimate_tokens": int(preflight_snapshot.get("final_estimate_tokens") or estimated_total_tokens),
                "trigger_tokens": trigger_tokens,
                "effective_trigger_tokens": int(preflight_snapshot.get("effective_trigger_tokens") or 0),
                "max_context_tokens": context_window_tokens,
                "provider_model": str(preflight_snapshot.get("provider_model") or self._frontdoor_model_display_name(model_info)),
                "resolved_model_key": str(preflight_snapshot.get("resolved_model_key") or ""),
                "would_exceed_context_window": bool(preflight_snapshot.get("would_exceed_context_window")),
                "would_trigger_token_compression": bool(preflight_snapshot.get("would_trigger_token_compression")),
                "ratio": float(preflight_snapshot.get("ratio") or 0.0),
                "estimated_text_tokens": int(preflight_snapshot.get("estimated_text_tokens") or 0),
                "estimated_tool_schema_tokens": int(preflight_snapshot.get("estimated_tool_schema_tokens") or 0),
                "estimated_image_tokens": int(preflight_snapshot.get("estimated_image_tokens") or 0),
                "image_count": int(preflight_snapshot.get("image_count") or 0),
                # 落盘的是"这一发实际要发的体"的末位读数，与 image_count 同一份消息，
                # 否则 artifact 会说"发了图"而说不出这颗上游看不看得见。
                "image_blocks_at_tail": self._frontdoor_tail_image_block_count(request_messages),
                "image_estimation_method": str(preflight_snapshot.get("image_estimation_method") or ""),
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
                # 压缩把整份正文重写了，一次性提示会随之离开上下文。那一跳可复用前缀反正
                # 已经断在这里，把仍可读的名字带到压缩产物之后不额外付缓存；读不到的名字
                # 在这同一跳退场（不写"已下架"行，裁决归 load_skill_context 三态回执）。
                request_messages = self._carry_frontdoor_skill_roster_notice_on_compaction(
                    request_messages,
                    session_key=str(state_for_request.get("session_key") or "").strip(),
                    compression_stage_id=(
                        f"turn:{str(getattr(runtime_session, '_active_turn_id', '') or '').strip()}:comp"
                        if runtime_session is not None
                        else ""
                    ),
                )
                durable_request_messages = strip_multimodal_blocks_from_message_records(request_messages)
                if runtime_session is not None and dict(preflight.diagnostics or {}).get("applied"):
                    # 按 turn 记账，回合收尾时据此落「会话已压缩」区分线。用 turn_id 而不是
                    # 布尔标志：被打断的回合留下的标志不会被下一个回合误读成一次新压缩。
                    compressed_turn_id = str(getattr(runtime_session, "_active_turn_id", "") or "").strip()
                    setattr(runtime_session, "_frontdoor_compressed_turn_id", compressed_turn_id)
                    compression_boundary_index = int(
                        dict(preflight.diagnostics or {}).get("boundary_stage_index") or 0
                    )
                    setattr(
                        runtime_session,
                        "_frontdoor_compression_boundary_index",
                        compression_boundary_index,
                    )
                    # 摘要一落地就先把边界广播出去：这一回合之后还要跑很多跳，等收尾再画线
                    # 的话，压缩之后跑出来的阶段已经全在线上方了。转录行仍由收尾那次写盘落。
                    await runtime_session.announce_context_compression_boundary(
                        turn_id=compressed_turn_id,
                        boundary_stage_index=compression_boundary_index,
                    )
                post_compaction_tokens = int(preflight.final_request_tokens or 0)
                post_compaction_snapshot = build_runtime_send_token_preflight_snapshot(
                    context_window_tokens=context_window_tokens,
                    estimated_total_tokens=post_compaction_tokens,
                )
                preflight_diagnostics = {
                    **dict(preflight.diagnostics or {}),
                    # 内层放弃这一 pass 时（helper 没拿到摘要）不能在这里被改写成"已压缩"。
                    "applied": bool((preflight.diagnostics or {}).get("applied", True)),
                    "mode": "llm",
                    "final_request_tokens": post_compaction_tokens,
                    "estimated_total_tokens": int(post_compaction_snapshot.estimated_total_tokens or 0),
                    "preview_estimate_tokens": int(post_compaction_tokens or 0),
                    "usage_based_estimate_tokens": 0,
                    "delta_estimate_tokens": 0,
                    "effective_input_tokens": 0,
                    "anchor_projection_shrink_tokens": 0,
                    "estimate_source": "preview_estimate",
                    "comparable_to_previous_request": False,
                    "final_estimate_tokens": int(post_compaction_tokens or 0),
                    "trigger_tokens": trigger_tokens,
                    "effective_trigger_tokens": int(preflight_snapshot.get("effective_trigger_tokens") or 0),
                    "max_context_tokens": context_window_tokens,
                    "provider_model": str(
                        preflight_snapshot.get("provider_model") or self._frontdoor_model_display_name(model_info)
                    ),
                    "resolved_model_key": str(preflight_snapshot.get("resolved_model_key") or ""),
                    "ratio": float(post_compaction_snapshot.ratio or 0.0),
                    "would_exceed_context_window": bool(post_compaction_snapshot.would_exceed_context_window),
                    "would_trigger_token_compression": bool(post_compaction_snapshot.would_trigger_token_compression),
                    "estimated_text_tokens": int(preflight_snapshot.get("estimated_text_tokens") or 0),
                    "estimated_tool_schema_tokens": int(preflight_snapshot.get("estimated_tool_schema_tokens") or 0),
                    "estimated_image_tokens": int(preflight_snapshot.get("estimated_image_tokens") or 0),
                    "image_count": int(preflight_snapshot.get("image_count") or 0),
                    # 压缩后的请求体才是这一发真正发出去的那份：按它数末位，不按压缩前那份。
                    "image_blocks_at_tail": self._frontdoor_tail_image_block_count(request_messages),
                    "image_estimation_method": str(preflight_snapshot.get("image_estimation_method") or ""),
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
                if preflight_shrink_reason == "token_compression":
                    # 记录“本轮真实发生内联 token 压缩”，供轮末记忆复核冲刷判定；
                    # 与跨轮残留的 frontdoor_history_shrink_reason 是两个概念。
                    if runtime_session is not None:
                        setattr(runtime_session, "_frontdoor_token_compression_applied_turn", True)
                        # 正文整段被重写，可复用前缀反正断在这一跳：把"下一跳允许重印名单"挂成
                        # 一次性标记，交给 `_frontdoor_bundle_recommit_boundary` 消费。判据不再
                        # 读会话级残留的 `frontdoor_history_shrink_reason`——那条一次压缩之后
                        # 每一轮都还在，等于把"只有重印边界能改名册"稀释成"每轮都能改"。
                        setattr(runtime_session, PENDING_PROVIDER_BUNDLE_RECOMMIT_ATTR, True)
                    current_provider_tool_names = self._frontdoor_provider_visible_tool_names(
                        list(state_for_request.get("provider_tool_names") or [])
                    )
                    prior_provider_tool_names = self._normalized_tool_name_state_list(
                        getattr(runtime_session, "_frontdoor_provider_tool_schema_names", [])
                        or current_provider_tool_names
                    )
                    compression_provider_tool_names = self._frontdoor_provider_visible_tool_names(
                        list(prior_provider_tool_names or current_provider_tool_names)
                    ) or list(current_provider_tool_names)
                    compression_state = {
                        **dict(state_for_request or {}),
                        "provider_tool_names": list(compression_provider_tool_names),
                        "pending_provider_tool_names": [],
                        "provider_tool_exposure_pending": False,
                        "provider_tool_exposure_revision": self._provider_tool_exposure_revision(
                            compression_provider_tool_names
                        ),
                        "provider_tool_exposure_commit_reason": "",
                        "frontdoor_live_request_messages": list(request_messages),
                    }
                    if compression_provider_tool_names != current_provider_tool_names:
                        compression_tool_schemas = self._selected_tool_schemas(
                            list(compression_provider_tool_names)
                        )
                        compression_contract = self._frontdoor_prompt_contract(
                            state=compression_state,
                            provider_model=str((list(compression_state.get("model_refs") or []) or [""])[0] or "").strip(),
                            tool_schemas=compression_tool_schemas,
                            overlay_text=str(compression_state.get("turn_overlay_text") or "").strip(),
                            session_key=str(compression_state.get("session_key") or "").strip(),
                            overlay_section_count=len(list(compression_state.get("dynamic_appendix_messages") or [])),
                        )
                        compression_request_messages = list(compression_contract.request_messages)
                        compression_prompt_cache_key = str(
                            compression_contract.prompt_cache_key or prompt_cache_key
                        )
                        compression_prompt_cache_diagnostics = dict(
                            compression_contract.diagnostics or prompt_cache_diagnostics
                        )
                        compression_provider_request_body = self._build_frontdoor_provider_request_body_preview(
                            request_messages=compression_request_messages,
                            tool_schemas=compression_tool_schemas,
                            model_info=model_info,
                            prompt_cache_key=compression_prompt_cache_key,
                            parallel_tool_calls=(
                                bool(state_for_request.get("parallel_enabled")) if list(tool_schemas or []) else None
                            ),
                        )
                        compression_total_tokens = self._estimate_frontdoor_send_total_tokens(
                            provider_request_body=compression_provider_request_body,
                            request_messages=compression_request_messages,
                            tool_schemas=compression_tool_schemas,
                        )
                        request_messages = list(compression_request_messages)
                        prompt_cache_key = compression_prompt_cache_key
                        prompt_cache_diagnostics = compression_prompt_cache_diagnostics
                        actual_tool_schemas = list(compression_tool_schemas)
                        state_for_request = compression_state
                        post_compaction_snapshot = build_runtime_send_token_preflight_snapshot(
                            context_window_tokens=context_window_tokens,
                            estimated_total_tokens=int(compression_total_tokens or 0),
                        )
                        preflight_diagnostics = {
                            **dict(preflight_diagnostics or {}),
                            "estimated_total_tokens": int(compression_total_tokens or 0),
                            "preview_estimate_tokens": int(compression_total_tokens or 0),
                            "final_estimate_tokens": int(compression_total_tokens or 0),
                            "final_request_tokens": int(compression_total_tokens or 0),
                            "ratio": float(post_compaction_snapshot.ratio or 0.0),
                            "would_exceed_context_window": bool(post_compaction_snapshot.would_exceed_context_window),
                            "would_trigger_token_compression": bool(
                                post_compaction_snapshot.would_trigger_token_compression
                            ),
                            "provider_tool_refresh_deferred_due_to": "token_compression",
                        }
                    else:
                        state_for_request = compression_state
                if runtime_session is not None and preflight_shrink_reason:
                    setattr(runtime_session, "_frontdoor_pending_shrink_reason", "")
                if int(preflight_diagnostics.get("final_request_tokens") or preflight.final_request_tokens or 0) > context_window_tokens:
                    raise self._frontdoor_context_window_exceeded_error(
                        model_info=model_info,
                        # 区分两种"超窗"：压了还超，与压缩本身没拿到摘要。
                        detail=str(preflight_diagnostics.get("compression_pass_failed") or "").strip(),
                    )
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
            provider_request_started_at = ""
            while True:
                try:
                    if not provider_request_started_at:
                        provider_request_started_at = now_iso()
                    message = await self._call_model_with_tools(
                        messages=request_messages,
                        tool_schemas=tool_schemas,
                        model_refs=list(state_for_request.get("model_refs") or []),
                        parallel_tool_calls=(bool(state_for_request.get("parallel_enabled")) if tool_schemas else None),
                        prompt_cache_key=prompt_cache_key,
                        on_text_delta=assistant_text_delta_handler,
                        on_reasoning_delta=assistant_reasoning_delta_handler,
                        on_reasoning_segment=assistant_reasoning_segment_handler,
                        on_model_retry_status=model_retry_status_handler,
                        # 前门没有回合准入，链上"塞不下这一发"的成员过去照样会被发到；
                        # 这里把预检算出来的本轮体量交给发送层做同一判据。
                        required_context_window_tokens=int(preflight_diagnostics.get("estimated_total_tokens") or 0),
                    )
                    # 这一跳的模型流到这里就结束了，后面是工具执行。思考段必须在这儿收口，
                    # 否则表要一直走到下一次 attempt 起点（或回合收尾）才停，工具跑多久
                    # 就在思考行上多算多久。
                    if callable(assistant_reasoning_segment_close_handler):
                        assistant_reasoning_segment_close_handler()
                except Exception as exc:
                    if not isinstance(exc, ModelProviderExhaustedError) and PUBLIC_PROVIDER_FAILURE_MESSAGE not in str(exc or ""):
                        raise
                    if self._refresh_runtime_config_for_retry_invalidation():
                        state_for_request["model_refs"] = list(
                            self._resolve_ceo_model_refs_for_session(state_for_request.get("session_key"))
                        )
                        state_for_request["model_refs_revision"] = self._frontdoor_runtime_config_revision()
                        restart_with_refreshed_runtime = True
                        break
                    provider_retry_count += 1
                    if provider_retry_count >= _PROVIDER_RETRY_LIMIT:
                        # Re-raise the original exception so the raw provider
                        # error reaches the frontend unwrapped.
                        raise
                    await asyncio.sleep(float(min(10, max(1, provider_retry_count))))
                    continue
                response_view = self._model_response_view(message)
                # 未终止的流不再由这里判：provider 已经把它标成提供侧故障（带 error_text），
                # 模型链在同一次调用内换槽位；整条链都断时走下方 finish_reason=="error" 的
                # 结构化上抛。这里只留"正常终止但空"的重放——它不是传输故障。
                if self._is_empty_model_response(response_view):
                    if self._refresh_runtime_config_for_retry_invalidation():
                        state_for_request["model_refs"] = list(
                            self._resolve_ceo_model_refs_for_session(state_for_request.get("session_key"))
                        )
                        state_for_request["model_refs_revision"] = self._frontdoor_runtime_config_revision()
                        restart_with_refreshed_runtime = True
                        break
                    empty_response_retry_count += 1
                    if empty_response_retry_count >= _PROVIDER_RETRY_LIMIT:
                        # 与节点车道同构：耗尽后失败上抛，由 session_agent 的错误车道
                        # 落成「这一轮处理失败：…」，不把运行时内部文案当助手回复投递。
                        raise ModelProviderExhaustedError(
                            message="模型返回空响应（无正文、无工具调用）"
                            + f"，自动重试 {empty_response_retry_count} 次仍未取得可用回复。"
                        )
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
            parallel_tool_calls=(bool(state_for_request.get("parallel_enabled")) if tool_schemas else None),
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
            provider_request_started_at=provider_request_started_at,
        )
        message_state_update = (
            self._replace_messages_update(
                list(self._strip_frontdoor_turn_only_artifacts(durable_request_messages))
            )
            if callable(getattr(self, "_replace_messages_update", None))
            else {"messages": list(self._strip_frontdoor_turn_only_artifacts(durable_request_messages))}
        )
        return {
            "iteration": iteration,
            "repair_overlay_text": None,
            **message_state_update,
            "frontdoor_live_request_messages": [],
            "pending_content_open_image_payloads": [],
            "model_refs": list(state_for_request.get("model_refs") or []),
            "model_refs_revision": state_for_request.get("model_refs_revision"),
            "provider_tool_names": list(state_for_request.get("provider_tool_names") or []),
            "pending_provider_tool_names": list(
                state_for_request.get("pending_provider_tool_names") or []
            ),
            "provider_tool_exposure_pending": bool(
                state_for_request.get("provider_tool_exposure_pending")
            ),
            "provider_tool_exposure_revision": str(
                state_for_request.get("provider_tool_exposure_revision") or ""
            ),
            "provider_tool_exposure_commit_reason": str(
                state_for_request.get("provider_tool_exposure_commit_reason") or ""
            ),
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
        runtime: CeoRuntime,
    ) -> dict[str, Any]:
        response_payload = dict(state.get("response_payload") or {})
        response_view = self._model_response_view(response_payload)
        # 调用准入按可执行束（含水合名），不按钉住的 tools[]：mid-turn 注册的工具
        # load 落账后模型就能点名调用，参数表靠 load 返回的契约承载。
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
        # 只有「阶段刚创建且尚无实质工具轮」才允许打回纯文本收尾，且整回合限次
        # （stage_reply_bounce_count）；预算耗尽不再打回，文本收尾直接 finalize，
        # 由 _graph_finalize_turn 以指针摘要关闭活动阶段。
        stage_reply_bounce_message = ""
        if not bool(state.get("heartbeat_internal")) and not bool(state.get("cron_internal")):
            stage_reply_bounce_message = build_ceo_stage_reply_bounce_message(stage_gate)
        stage_reply_bounce_count = int(state.get("stage_reply_bounce_count", 0) or 0)

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
            approval_request = self._approval_request_for_tool_calls(
                tool_call_payloads,
                session_key=str(state.get("session_key") or "").strip(),
            )
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
                "tool_contract_echo_attempt_count": 0,
                "stage_block_echo_attempt_count": 0,
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
        if not response_tool_calls and is_frontdoor_tool_contract_echo_text(text):
            echo_attempt_count = int(state.get('tool_contract_echo_attempt_count', 0) or 0) + 1
            if echo_attempt_count == 1:
                return {
                    'repair_overlay_text': _TOOL_CONTRACT_ECHO_REPAIR_MESSAGE,
                    'tool_contract_echo_attempt_count': echo_attempt_count,
                    'final_output': '',
                    'next_step': 'call_model',
                }
            # Never promote a repeated internal contract summary to final
            # output.  The normal empty-response explanation is user-facing and
            # contains no provider/runtime contract material.
            return {
                'final_output': self._empty_response_explanation(
                    used_tools=used_tools,
                    verified_task_ids=list(state.get('verified_task_ids') or []),
                ),
                'route_kind': self._route_kind_for_turn(
                    used_tools=used_tools,
                    default=current_route_kind,
                    verified_task_ids=list(state.get('verified_task_ids') or []),
                ),
                'tool_contract_echo_attempt_count': echo_attempt_count,
                'next_step': 'finalize',
            }
        if not response_tool_calls and is_stage_block_echo_text(text):
            stage_echo_attempt_count = int(state.get('stage_block_echo_attempt_count', 0) or 0) + 1
            if stage_echo_attempt_count == 1:
                return {
                    'repair_overlay_text': _STAGE_BLOCK_ECHO_REPAIR_MESSAGE,
                    'stage_block_echo_attempt_count': stage_echo_attempt_count,
                    'final_output': '',
                    'next_step': 'call_model',
                }
            # Never promote a repeated internal stage-compaction block to
            # final output, and never persist or deliver the raw block text.
            # The normal empty-response explanation is user-facing and
            # contains no stage-protocol material.
            return {
                'final_output': self._empty_response_explanation(
                    used_tools=used_tools,
                    verified_task_ids=list(state.get('verified_task_ids') or []),
                ),
                'route_kind': self._route_kind_for_turn(
                    used_tools=used_tools,
                    default=current_route_kind,
                    verified_task_ids=list(state.get('verified_task_ids') or []),
                ),
                'stage_block_echo_attempt_count': stage_echo_attempt_count,
                'next_step': 'finalize',
            }
        if str(response_view.finish_reason or "").strip().lower() == "error":
            error_detail = str(
                response_view.error_text
                or response_view.content
                or "model response failed"
            ).strip()
            error_detail = error_detail if error_detail.lower() not in {"error", "error:"} else "model response failed"
            if str(response_view.error_kind or "").strip() == "StreamIncomplete":
                # 断流的取证串是英文内部文案，不能原样投给渠道：这里换成与空响应车道同口径的
                # 中文，原文进 raw_message（.g3ku/errors 与前端排障仍看得到）。
                raise ModelProviderExhaustedError(
                    raw_message=error_detail,
                    message="响应流未正常终止（未收到终止标记），整条模型链都没有拿到可用回复。",
                )
            if error_detail == PUBLIC_PROVIDER_FAILURE_MESSAGE:
                raise ModelProviderExhaustedError(raw_message=error_detail, message=error_detail)
            # 抛结构化异常而非裸 RuntimeError：携带 provider 的 code/status/kind，使上层
            # 分类器拿到真实 error_code（如 insufficient_quota）而不是 legacy_session_error，
            # 完整错误原文保留在 message。仍是 RuntimeError 子类，兼容既有 except。
            raise ModelProviderResponseError(
                message=error_detail,
                raw_message=error_detail,
                code=str(response_view.error_code or ""),
                status=response_view.error_status,
                kind=str(response_view.error_kind or ""),
            )

        if text.strip():
            if ECHO_STRIP_ENABLED:
                # 回显裁剪开关见 stage_prompt_compaction.ECHO_STRIP_ENABLED；关闭时
                # 尾部契约/阶段块片段随回复原样放行，避免合法内联引用被齐根截断。
                text = strip_frontdoor_tool_contract_echo(text)
                text = _skill_roster_notice.strip_skill_roster_notice_echo(text)
                text = strip_stage_block_echo(text)
            if not text:
                return {
                    'final_output': self._empty_response_explanation(
                        used_tools=used_tools,
                        verified_task_ids=list(state.get('verified_task_ids') or []),
                    ),
                    'route_kind': self._route_kind_for_turn(
                        used_tools=used_tools,
                        default=current_route_kind,
                        verified_task_ids=list(state.get('verified_task_ids') or []),
                    ),
                    'next_step': 'finalize',
                }
        if text.strip():
            if stage_reply_bounce_message and stage_reply_bounce_count < STAGE_REPLY_BOUNCE_LIMIT:
                return {
                    "repair_overlay_text": stage_reply_bounce_message,
                    "stage_reply_bounce_count": stage_reply_bounce_count + 1,
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

        if stage_reply_bounce_message and stage_reply_bounce_count < STAGE_REPLY_BOUNCE_LIMIT:
            return {
                "repair_overlay_text": stage_reply_bounce_message,
                "stage_reply_bounce_count": stage_reply_bounce_count + 1,
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
        runtime: CeoRuntime | None = None,
        resume_decision: Any = _NO_RESUME,
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
        ) = self._runtime_session_frontdoor_state(
            preview_state,
            preview_pending_tool_round=True,
        )
        interrupt_payload = {
            **approval_request,
            "frontdoor_stage_state": preview_frontdoor_stage_state,
            "frontdoor_canonical_context": preview_frontdoor_canonical_context,
            "compression_state": preview_compression_state,
            "tool_call_payloads": [
                dict(item)
                for item in list(state.get("tool_call_payloads") or [])
                if isinstance(item, dict)
            ],
            "frontdoor_selection_debug": self._frontdoor_selection_debug_snapshot(state),
        }
        _ = runtime
        if resume_decision is _NO_RESUME:
            decision = raise_frontdoor_approval_interrupt(state=state, payload=interrupt_payload)
        else:
            decision = resume_decision
        normalized = self._normalize_approval_resume_value(
            decision=decision,
            original_payloads=list(state.get("tool_call_payloads") or []),
            approval_request=approval_request,
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
            # 批审把条目分成了两半：这一半真的执行，另一半在 `_graph_execute_tools` 里
            # 与真实结果按原顺序合并。非批审的裁决没有这两个键，回落成"全部照原样执行"。
            "executable_tool_call_payloads": list(
                normalized.get("executable_tool_call_payloads") or normalized["tool_call_payloads"]
            ),
            "synthetic_tool_results": list(normalized.get("synthetic_tool_results") or []),
            "approval_batch_id": str(normalized.get("batch_id") or ""),
            "next_step": "execute_tools",
        }

    async def _graph_execute_tools(
        self,
        state: CeoGraphState,
        *,
        runtime: CeoRuntime,
    ) -> dict[str, Any]:
        original_tool_call_payloads = list(state.get("tool_call_payloads") or [])
        executable_tool_call_payloads = list(
            state.get("executable_tool_call_payloads") or original_tool_call_payloads
        )
        synthetic_tool_results = [
            dict(item)
            for item in list(state.get("synthetic_tool_results") or [])
            if isinstance(item, dict)
        ]
        if not original_tool_call_payloads:
            return {"next_step": "call_model"}

        execution_bundle = self._frontdoor_execution_bundle(state=state, runtime=runtime)
        runtime_context = execution_bundle.runtime_context
        # 冲动投递的同批记账：每个工具在这一批里被调了几次，回执要说"第 i / n 份"（D4）。
        impulse_batch_totals: dict[str, int] = {}
        for pending_payload in list(executable_tool_call_payloads or []):
            pending_name = str((pending_payload or {}).get('name') or '').strip()
            if pending_name:
                impulse_batch_totals[pending_name] = impulse_batch_totals.get(pending_name, 0) + 1
        runtime_context['impulse_batch_totals'] = impulse_batch_totals
        runtime_context['impulse_delivered_tools'] = {}
        # 在场索引每跳只算一次（D12），下传给这一批每一次投递/复用判定。state 在工具阶段不变，
        # 逐条重扫全历史（median 视图 0.08 ms、最大 578 条 0.6 ms）在 30 连发时是纯浪费。
        runtime_context['tool_contract_presence'] = self._frontdoor_contract_presence_index(state)
        # 暂存时钟在投递之前推进：本轮落的条目要按推进后的跳序记，下一跳才正好可消费。
        runtime_context['tool_arg_stash_carrier'], runtime_context['tool_arg_stash_swept'] = (
            self._advance_frontdoor_stash_hop((runtime_context or {}).get('runtime_session'))
        )
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
        node_error_context = self._frontdoor_node_error_heartbeat_context(state)
        semaphore = asyncio.Semaphore(
            self._parallel_slot_count(
                state.get("max_parallel_tool_calls"),
                len(executable_tool_call_payloads),
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
                "tool_call_id": str(payload.get("id") or "").strip(),
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

            def _impulse_ledger_fields() -> dict[str, str]:
                """这次调用实际走的道（D15）与"同一份入参重发"补格，按 call id 取。

                记号由 `_frontdoor_reuse_args_execution` 在判定那一刻写下；没记过就是普通
                执行。三个返回点都带上它，阶段记录才有一条完整可数的道。
                """
                call_key = str(payload.get("id") or "").strip()
                fields = {
                    "execution_path": str(
                        (runtime_context.get("tool_execution_paths") or {}).get(call_key)
                        or EXECUTION_PATH_NORMAL
                    )
                }
                replay = str((runtime_context.get("tool_staged_replay") or {}).get(call_key) or "").strip()
                if replay:
                    fields[STAGED_REPLAY_FIELD] = replay
                if (runtime_context.get("tool_reuse_flag_ignored") or {}).get(call_key):
                    fields[REUSE_FLAG_IGNORED_FIELD] = True
                return fields
            gate_error = self._frontdoor_stage_gate_error(
                tool_name=tool_name,
                stage_state=mutable_stage_state,
                allow_stageless=bool(node_error_context),
            )
            free_pass_kind = ""
            if gate_error:
                free_pass_kind = self._frontdoor_stage_free_pass_kind(mutable_stage_state)
                if not free_pass_kind:
                    return await _error_result(payload, gate_error)
            duplicate_load_error = await self._frontdoor_load_tool_context_duplicate_error(
                payload=payload,
                state=dict(state or {}),
                runtime_context=runtime_context,
            )
            if duplicate_load_error:
                return await _error_result(payload, duplicate_load_error)
            tool = visible_tools.get(tool_name)
            if tool is None:
                impulse = self._frontdoor_impulse_contract_content(
                    tool_name=tool_name,
                    arguments=dict(payload.get("arguments") or {}),
                    state=state,
                    runtime_context=runtime_context,
                )
                if impulse is not None:
                    kind, impulse_content = impulse
                    if kind == "failed":
                        return await _error_result(payload, impulse_content)
                    # 这一跳没有执行工具，只交回契约；实时帧要带上同一条标记，前端才能把
                    # 它画成"加载 tool"并从工具行里摘出去。
                    impulse_event_data = self._tool_result_progress_event_data(
                        tool_name=tool_name,
                        result_text=impulse_content,
                        tool_call_id=str(payload.get("id") or "").strip() or None,
                    )
                    impulse_event_data[CONTRACT_DELIVERY_FIELD] = kind
                    await self._emit_progress(
                        on_progress,
                        impulse_content,
                        event_kind="tool_result",
                        event_data=impulse_event_data,
                    )
                    return {
                        "tool_call_id": str(payload.get("id") or "").strip(),
                        "tool_name": tool_name,
                        "status": "success",
                        "raw_result": impulse_content,
                        "result_text": impulse_content,
                        "free_pass_kind": free_pass_kind,
                        CONTRACT_DELIVERY_FIELD: kind,
                        **_impulse_ledger_fields(),
                        "tool_message": self._tool_result_message(
                            tool_call_id=str(payload.get("id") or ""),
                            tool_name=tool_name or "tool",
                            content=impulse_content,
                            started_at="",
                            finished_at="",
                            elapsed_seconds=None,
                        ),
                    }
                hint = availability_hint(
                    requested=tool_name,
                    callable_names=list(visible_tools.keys()),
                    candidate_names=(runtime_context or {}).get("candidate_tool_names") or [],
                    denied_names=(runtime_context or {}).get("declared_denied_tool_names") or [],
                    declared_names=(runtime_context or {}).get("declared_tool_names") or [],
                )
                return await _error_result(payload, f"tool not available: {tool_name}" + (f"\n{hint}" if hint else ""))
            async def _reuse_execute_one(call_arguments: dict[str, Any]) -> tuple[str, str]:
                async with semaphore:
                    (
                        _raw_result,
                        reuse_text,
                        status,
                        _started_at,
                        _finished_at,
                        _elapsed,
                    ) = await self._execute_tool_call_with_raw_result(
                        tool=tool,
                        tool_name=tool_name,
                        arguments=_normalize_frontdoor_tool_arguments(tool_name, dict(call_arguments or {})),
                        runtime_context=runtime_context,
                        on_progress=on_progress,
                        tool_call_id=str(payload.get("id") or "").strip() or None,
                    )
                return str(reuse_text or ""), str(status or "success")

            reuse_content = await self._frontdoor_reuse_args_execution(
                tool_name=tool_name,
                arguments=dict(payload.get("arguments") or {}),
                state=state,
                runtime_context=runtime_context,
                execute_one=_reuse_execute_one,
                call_id=str(payload.get("id") or "").strip(),
            )
            if reuse_content is not None:
                # 复用调用与冲动投递同形：一条 call id 恰好一个结果块，宽限提醒不再叠加
                # （预算仍按本轮一次计，批次内部的 N 份不额外占轮）。
                return {
                    "tool_call_id": str(payload.get("id") or "").strip(),
                    "tool_name": tool_name,
                    "status": self._tool_status(reuse_content),
                    "raw_result": reuse_content,
                    "result_text": reuse_content,
                    "free_pass_kind": free_pass_kind,
                    **_impulse_ledger_fields(),
                    "tool_message": self._tool_result_message(
                        tool_call_id=str(payload.get("id") or ""),
                        tool_name=tool_name or "tool",
                        content=reuse_content,
                        started_at="",
                        finished_at="",
                        elapsed_seconds=None,
                    ),
                }
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
            if status == "success":
                if free_pass_kind:
                    reminder = (
                        STAGELESS_FREE_PASS_REMINDER
                        if free_pass_kind == "stageless"
                        else STAGE_BUDGET_EXHAUSTED_FREE_PASS_REMINDER
                    )
                    result_text = f"{result_text}\n\n{reminder}".strip()
                elif predicted_exhaustion_reminder:
                    result_text = f"{result_text}\n\n{predicted_exhaustion_reminder}".strip()
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
                "tool_call_id": str(payload.get("id") or "").strip(),
                "tool_name": tool_name,
                "status": status,
                "raw_result": result_payload.get("raw_result"),
                "result_text": result_text,
                "free_pass_kind": free_pass_kind,
                **_impulse_ledger_fields(),
                "tool_message": self._tool_result_message(
                    tool_call_id=str(payload.get("id") or ""),
                    tool_name=tool_name or "tool",
                    content=result_text,
                    started_at=str(result_payload.get("started_at") or ""),
                    finished_at=str(result_payload.get("finished_at") or ""),
                    elapsed_seconds=result_payload.get("elapsed_seconds"),
                ),
            }

        indexed_payloads = [
            (index, dict(payload))
            for index, payload in enumerate(list(executable_tool_call_payloads or []))
            if isinstance(payload, dict)
        ]
        stage_items = [
            (index, payload)
            for index, payload in indexed_payloads
            if str(payload.get("name") or "").strip() == STAGE_TOOL_NAME
        ]
        ordinary_items = [
            (index, payload)
            for index, payload in indexed_payloads
            if str(payload.get("name") or "").strip() != STAGE_TOOL_NAME
        ]
        # 同批含 submit_next_stage 时不预告:那批普通工具记到刚开的新阶段上,按旧阶段预算
        # 算出的预告既失配、又会贴到 sns 自己的返回值里,成为与账本相反的陈述。
        predicted_exhaustion_reminder = (
            ""
            if stage_items
            else self._frontdoor_predicted_exhaustion_reminder(
                mutable_stage_state,
                ordinary_payloads=[payload for _, payload in ordinary_items],
            )
        )
        ordered_results: dict[int, dict[str, Any]] = {}
        stage_failed = False
        for position, (index, payload) in enumerate(stage_items):
            if position > 0:
                ordered_results[index] = await _error_result(
                    payload,
                    f"{STAGE_TOOL_NAME} can be called at most once per batch; the extra call was ignored",
                )
                stage_failed = True
                continue
            stage_signature_before = stage_transition_signature(mutable_stage_state)
            result = await _run_single(payload)
            ordered_results[index] = result
            if str(result.get("status") or "").strip().lower() == "error":
                # 与节点道同一条判据：账本一字未动（提交前就被参数契约或闸门判拒）时，本批
                # 普通调用看到的阶段面还是模型上一跳看过的那份，作废整批只是白烧一轮。
                stage_failed = stage_ledger_may_have_moved(
                    before=stage_signature_before,
                    after=stage_transition_signature(mutable_stage_state),
                )
        if ordinary_items:
            if stage_items and stage_failed:
                for index, payload in ordinary_items:
                    ordered_results[index] = await _error_result(
                        payload,
                        f"{STAGE_TOOL_NAME} failed earlier in this batch; retry other tools after a successful stage transition",
                    )
            else:
                ordinary_results = await asyncio.gather(*[_run_single(payload) for _, payload in ordinary_items])
                for (index, _payload), result in zip(ordinary_items, ordinary_results, strict=False):
                    ordered_results[index] = result
        executed_tool_results = [ordered_results[index] for index, _payload in indexed_payloads if index in ordered_results]
        tool_results = self._merge_ordered_tool_results(
            original_payloads=original_tool_call_payloads,
            real_results=executed_tool_results,
            synthetic_results=synthetic_tool_results,
        )
        pending_content_open_image_payloads = [
            dict(item)
            for item in list(state.get("pending_content_open_image_payloads") or [])
            if isinstance(item, dict)
        ]
        pending_content_open_image_payloads.extend(self._content_open_image_payloads_from_tool_results(tool_results))
        frontdoor_stage_state = self._frontdoor_stage_state_after_tool_cycle(
            {
                **dict(state or {}),
                "frontdoor_stage_state": base_stage_state,
            },
            tool_call_payloads=original_tool_call_payloads,
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
            **self._frontdoor_assistant_reasoning_field(response_payload),
            "tool_calls": self._assistant_tool_calls_from_payloads(original_tool_call_payloads),
        }
        messages = list(state.get("messages") or [])
        if hasattr(self, "_state_message_records"):
            messages = list(getattr(self, "_state_message_records")(messages))
        messages = self._strip_frontdoor_turn_only_artifacts(messages)
        messages.append(assistant_message)
        messages.extend(tool_messages)
        # 回合内过期点：模型在 `submit_next_stage` 里点名裁撤后，前门过去要等到下一回合
        # 装配才兑现（`_trim_frontdoor_seed_stage_compaction` 只在 prepare_turn 调用），
        # 于是一个长回合的正文一路线性涨。这里与节点道 `_stage_expiry_hop` 同构：踩到
        # 过期点的这一跳把正文按阶段归属原位压缩，压缩结果成为下一跳的发送基线；裁完
        # 之后判据自然转假，下一跳回到 append-only 链，前缀失效面只有过期点那么多次。
        stage_compacted_messages, stage_compaction_applied = self._trim_frontdoor_seed_stage_compaction(
            messages,
            frontdoor_stage_state,
        )
        if stage_compaction_applied:
            messages = stage_compacted_messages
        authoritative_request_body_messages = self._durable_frontdoor_request_body_messages(messages)
        # 工具态写回读**这一跳之后**的视图：裁撤与写回同批时，写回早于阶段账本落地就会读成
        # "还没裁"，于是名字仍被当在场，而下一跳的渲染/派发读的是裁后的视图 ⇒ 同一份状态里
        # 出现 `already_callable` 回执与 `tool not available` 两种答案。这里把新的阶段态与
        # 裁后的消息一并交进去，在场判据、候选、回执、执行四者从此同源。
        updated_tool_contract_state = self._frontdoor_tool_state_after_tool_results(
            state={
                **dict(state or {}),
                "messages": messages,
                "frontdoor_stage_state": frontdoor_stage_state,
            },
        )

        used_tools = list(state.get("used_tools") or [])
        used_tools.extend(
            [
                str(payload.get("name") or "").strip()
                for payload in original_tool_call_payloads
                if str(payload.get("name") or "").strip()
                and str(payload.get("name") or "").strip() not in self._CONTROL_TOOL_NAMES
            ]
        )
        verified_task_ids: list[str] = []
        dispatched_task_ids: list[str] = []
        for tool_result in tool_results:
            tool_name = str(tool_result.get("tool_name") or "").strip()
            result_text = str(tool_result.get("result_text") or "").strip()
            if tool_name != "create_async_task":
                continue
            parsed = self._parse_create_async_task_result(result_text)
            if not bool(parsed.get("created")):
                continue
            for task_id in list(parsed.get("created_task_ids") or []):
                if not task_id or task_id in dispatched_task_ids:
                    continue
                # 任务号取自运行时自己写的工具结果（`创建任务成功task:x`），不是模型正文，
                # 所以"本回合派发了哪些任务"不需要存在性闸门；闸门留给模型可见的那句派发尾注
                # （verified）——只有那边需要防"回一个不存在的号"。实盘：加了闸门的 metadata
                # 在 3 次派发里只落了 1 次。
                dispatched_task_ids.append(task_id)
                if self._task_id_exists(task_id):
                    verified_task_ids.append(task_id)
        route_kind = self._route_kind_for_turn(
            used_tools=used_tools,
            default=str(state.get("route_kind") or "direct_reply"),
            verified_task_ids=verified_task_ids,
        )
        # 与 legacy 路径（_ceo_create_agent_impl.py）对齐：派发验证成功后给模型
        # "可直接自然回复"的一次性尾注（经 _apply_turn_overlay 注入下一次请求，
        # call_model 返回时自动清空），避免模型误以为还需要继续开阶段干活。
        dispatch_reply_overlay_text: str | None = None
        if verified_task_ids and route_kind == "task_dispatch":
            if len(verified_task_ids) == 1:
                dispatch_reply_overlay_text = (
                    f"Dispatch result is already available. Reply naturally based on the verified task id {verified_task_ids[0]}."
                )
            else:
                dispatch_reply_overlay_text = (
                    "Dispatch result is already available. Reply naturally based on the verified task ids "
                    + ", ".join(verified_task_ids)
                    + "."
                )
        result = {
            "messages": messages,
            "frontdoor_live_request_messages": list(messages),
            "frontdoor_request_body_messages": authoritative_request_body_messages,
            "used_tools": used_tools,
            "route_kind": route_kind,
            "analysis_text": "",
            "tool_call_payloads": [],
            "executable_tool_call_payloads": [],
            "synthetic_tool_results": [],
            "verified_task_ids": list(verified_task_ids),
            "dispatched_task_ids": list(dispatched_task_ids),
            "synthetic_tool_calls_used": False,
            "frontdoor_stage_state": frontdoor_stage_state,
            "pending_content_open_image_payloads": pending_content_open_image_payloads,
            "next_step": "call_model",
        }
        if stage_compaction_applied:
            # 基线因阶段裁撤收缩过就得带合法原因落库：下一次 prepare 的非法收缩守卫
            # 按 `frontdoor_history_shrink_reason` 放行 stage_compaction，否则会被隔离。
            result["frontdoor_history_shrink_reason"] = "stage_compaction"
        if dispatch_reply_overlay_text:
            result["repair_overlay_text"] = dispatch_reply_overlay_text
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
        result["attachment_reopen_targets"] = [
            dict(item)
            for item in list(state.get("attachment_reopen_targets") or [])
            if isinstance(item, dict)
        ]
        result["repair_required_tool_items"] = [
            dict(item)
            for item in list(state.get("repair_required_tool_items") or [])
            if isinstance(item, dict)
        ]
        result["repair_required_skill_items"] = [
            dict(item)
            for item in list(state.get("repair_required_skill_items") or [])
            if isinstance(item, dict)
        ]
        silent_signal = self._silent_signal_from_tool_payloads(original_tool_call_payloads)
        if silent_signal:
            # 批次里其余工具已经跑完，本轮到此以静默收尾，不再回模型索要一句收尾话
            # （要求模型"说话才能不说"正是要换掉的东西：文案哨兵实盘 0 次成功）。
            # 前门此前没有工具即终态的先例 —— submit_final_result 只存在于节点侧，
            # 所以这条 finalize 边是新增的，收尾文本取同一条助手消息里随工具一起
            # 给出的正文；模型只调工具不给正文时退回 reason。
            accompanying_text = str(self._model_content(assistant_message.get("content")) or "").strip()
            result["silent_reply"] = True
            result["silent_reason"] = str(silent_signal.get("reason") or "")
            result["silent_subject"] = str(silent_signal.get("subject") or "")
            result["silent_superseded_by"] = str(silent_signal.get("superseded_by") or "")
            result["final_output"] = accompanying_text or str(silent_signal.get("reason") or "")
            result["next_step"] = "finalize"
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
        raw_output = str(state.get("final_output") or "").strip()
        # 纯显示层清洗，不是静默触发器：实盘 11 次旧哨兵用法全部写成「正文 + 空行 +
        # [G3KU_SILENT]」，识别已在 P4 删除，若不剥掉这行尾巴就会把它当正文发给用户。
        # 只在首/末整行时剥离，绝不因句子中间出现该串就动正文。
        output = self._strip_legacy_silent_sentinel_line(raw_output)
        silent_reply = bool(state.get("silent_reply"))
        silent_reason = str(state.get("silent_reason") or "").strip()
        if not output and not silent_reply and not bool(state.get("heartbeat_internal")):
            # 本轮没有可见正文 = 模型选择不说，机器不得替它编一条推给用户：旧的英文兜底
            # 会在 QQ 侧留下一条内部文案（2026-09-23 23:25:51 `qq_official.bridge:deliver`
            # 实盘投递，会话 ext:qq-official:f8a8001865631301）。改成静默后，模型照旧契约
            # 输出纯 `[G3KU_SILENT]` 也只会落到"剥完即空、空即静默"，那条废弃写法不再泄漏。
            silent_reply = True
            silent_reason = (
                "旧静默哨兵剥除后无正文" if output != raw_output else "模型未给出可见正文"
            )
        route_kind = str(state.get("route_kind") or "direct_reply")
        # 静默回合不再在此处把 final_output 清零：清零会吞掉 silent_reply 信号，落盘侧
        # 就分不出"本轮静默"和"本轮真的没输出"，心跳修复循环还会把合法静默判成无效空
        # 回复、连撞上限后发出误导性的"连续失败"兜底文案。改为保留原文并把 silent_reply
        # 显式写回 result，由 session_agent 经回填通道读取；基线回填与阶段收尾统一按
        # visible_output 判断，确保正文不会同文两份。
        visible_output = "" if silent_reply else output
        # 工具静默时不回填正文不是漏改：随工具一起给出的那段文本已经躺在 execute_tools
        # 追加的 assistant tool_calls 行里进了基线，再 append 一遍就是同文两份。
        result = {
            "final_output": output,
            "silent_reply": silent_reply,
            "route_kind": route_kind,
        }
        if silent_reply:
            # 把判据一路带到转录落盘处，供痕迹行与审计读取；session_agent 侧拿不到
            # 本轮工具调用，只能靠这条回填通道。
            result["silent_reason"] = silent_reason
            result["silent_subject"] = str(state.get("silent_subject") or "").strip()
            result["silent_superseded_by"] = str(state.get("silent_superseded_by") or "").strip()
        messages = list(state.get("messages") or [])
        if hasattr(self, "_state_message_records"):
            messages = list(getattr(self, "_state_message_records")(messages))
        else:
            messages = [dict(message) for message in messages if isinstance(message, dict)]
        request_body_messages, _tool_contract_messages = self._split_request_body_and_tool_contract_messages(messages)
        if request_body_messages:
            messages = list(request_body_messages)
        authoritative_request_body_messages = strip_multimodal_blocks_from_message_records(
            [
                dict(item)
                for item in list(state.get("frontdoor_request_body_messages") or request_body_messages or messages)
                if isinstance(item, dict)
            ]
        )
        frontdoor_history_shrink_reason = str(state.get("frontdoor_history_shrink_reason") or "").strip()
        finalized_stage_state = self._frontdoor_stage_state_snapshot(state)
        finalized_stage_state = self._frontdoor_absorb_orphan_rounds(finalized_stage_state)
        is_internal_turn = bool(state.get("heartbeat_internal")) or bool(state.get("cron_internal"))
        # 内部回合的真实可见回复必须像普通回合一样进基线，才能被下一轮上下文看见；
        # 只排除空输出的内部 ACK，与 session_agent 转录持久化的判据一致。旧的
        # HEARTBEAT_OK 字面判据已随文案出口一并删除（实盘 0 次单独命中）。
        is_silent_internal_ack = is_internal_turn and not str(output or "").strip()
        should_append_visible_output = bool(visible_output) and not is_silent_internal_ack
        if should_append_visible_output:
            final_response_payload = dict(state.get("response_payload") or {})
            visible_row = {"role": "assistant", "content": visible_output}
            # 轮末行只在该跳正文与可见正文逐字相同时带思考：多跳攒出来的正文不与任何单一跳
            # 对齐，配错比不配更坏。
            hop_text = self._content_text(final_response_payload.get("content", "")).strip()
            if hop_text and hop_text == str(visible_output or "").strip():
                visible_row.update(self._frontdoor_assistant_reasoning_field(final_response_payload))
            messages.append(dict(visible_row))
            authoritative_request_body_messages = [
                *list(authoritative_request_body_messages),
                dict(visible_row),
            ]
        if visible_output and route_kind == "direct_reply":
            result["messages"] = list(messages)
            result["frontdoor_request_body_messages"] = list(authoritative_request_body_messages)
            result["frontdoor_history_shrink_reason"] = frontdoor_history_shrink_reason
            # 轮末不写摘要:纯文本收尾的回合里,该阶段的最终回复就紧邻在块之后,摘要写
            # 指针只会让块宣称"结论已交付"却指不到任何东西(它指向的助手回复会被上下文
            # 压缩吃掉)。留空即可——块两侧就是对话原文。模型经 submit_next_stage 自带
            # 摘要的阶段不受影响(_complete_active_frontdoor_stage_state 仅在为空时填充)。
            finalized_stage_state = self._complete_active_frontdoor_stage_state(finalized_stage_state)
            result["frontdoor_stage_state"] = finalized_stage_state
            result["frontdoor_canonical_context"] = self._merged_frontdoor_canonical_context(
                state=state,
                frontdoor_stage_state=finalized_stage_state,
            )
            self._frontdoor_apply_stage_archive(result, authoritative_request_body_messages)
            return result
        if visible_output or silent_reply:
            # 轮末不写摘要:纯文本收尾的回合里,该阶段的最终回复就紧邻在块之后,摘要写
            # 指针只会让块宣称"结论已交付"却指不到任何东西(它指向的助手回复会被上下文
            # 压缩吃掉)。留空即可——块两侧就是对话原文。模型经 submit_next_stage 自带
            # 摘要的阶段不受影响(_complete_active_frontdoor_stage_state 仅在为空时填充)。
            # 静默轮同样收口但不传摘要:不收口会把这条活动阶段留给下一轮继承,后面的可见
            # 回合会在同一条卡里长出静默轮的轮次;而把 reason 写进摘要槽会顶掉下一次
            # submit 的真实结论——理由已有去处,就是本轮 silent 调用的 arguments.reason。
            finalized_stage_state = self._complete_active_frontdoor_stage_state(finalized_stage_state)
        result["frontdoor_stage_state"] = finalized_stage_state
        result["frontdoor_canonical_context"] = self._merged_frontdoor_canonical_context(
            state=state,
            frontdoor_stage_state=finalized_stage_state,
        )
        result["messages"] = list(messages)
        result["frontdoor_request_body_messages"] = list(authoritative_request_body_messages)
        result["frontdoor_history_shrink_reason"] = frontdoor_history_shrink_reason
        self._frontdoor_apply_stage_archive(result, authoritative_request_body_messages)
        return result

    @staticmethod
    def _graph_next_step(state: CeoGraphState) -> str:
        next_step = str(state.get("next_step") or "finalize").strip()
        if next_step not in {"call_model", "review_tool_calls", "execute_tools", "finalize"}:
            return "finalize"
        return next_step


__all__ = ["CeoFrontDoorRuntimeOps"]
