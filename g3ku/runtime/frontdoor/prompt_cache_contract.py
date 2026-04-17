from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from g3ku.runtime.semantic_context_summary import LONG_CONTEXT_SUMMARY_PREFIX
from g3ku.runtime.frontdoor.tool_contract import FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND
from main.runtime.chat_backend import (
    build_prompt_cache_diagnostics,
    build_session_prompt_cache_key,
    sanitize_provider_messages,
)

DEFAULT_CACHE_FAMILY_REVISION = "ceo_frontdoor:stable-prefix:v1"


def _is_frontdoor_runtime_tool_contract_record(record: dict[str, Any]) -> bool:
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


def _dynamic_appendix_overlap_records(dynamic_appendix_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in list(dynamic_appendix_messages or [])
        if isinstance(item, dict) and not _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]


def _with_dynamic_appendix_after_system(
    stable_messages: list[dict[str, Any]],
    dynamic_appendix_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized_stable_messages = [dict(item) for item in list(stable_messages or []) if isinstance(item, dict)]
    normalized_dynamic_messages = [dict(item) for item in list(dynamic_appendix_messages or []) if isinstance(item, dict)]
    if not normalized_dynamic_messages:
        return normalized_stable_messages
    if (
        normalized_stable_messages
        and str(normalized_stable_messages[0].get("role") or "").strip().lower() == "system"
    ):
        return [
            normalized_stable_messages[0],
            *normalized_dynamic_messages,
            *normalized_stable_messages[1:],
        ]
    return [*normalized_dynamic_messages, *normalized_stable_messages]


def _with_dynamic_appendix_at_tail(
    request_messages: list[dict[str, Any]],
    dynamic_appendix_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized_request_messages = [dict(item) for item in list(request_messages or []) if isinstance(item, dict)]
    normalized_dynamic_messages = [dict(item) for item in list(dynamic_appendix_messages or []) if isinstance(item, dict)]
    if not normalized_dynamic_messages:
        return normalized_request_messages
    contract_messages = [
        dict(item)
        for item in normalized_dynamic_messages
        if _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    non_contract_messages = [
        dict(item)
        for item in normalized_dynamic_messages
        if not _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    merged_request_messages = list(normalized_request_messages)
    if non_contract_messages:
        merged_request_messages = _strip_first_slice(merged_request_messages, non_contract_messages)
        if merged_request_messages == normalized_request_messages:
            merged_request_messages = _strip_first_slice(
                merged_request_messages,
                _dynamic_appendix_overlap_records(non_contract_messages),
            )
        if not _records_contain_slice(merged_request_messages, non_contract_messages):
            merged_request_messages = [*merged_request_messages, *non_contract_messages]
    if contract_messages:
        contract_len = len(contract_messages)
        if not (
            contract_len > 0
            and len(merged_request_messages) >= contract_len
            and merged_request_messages[-contract_len:] == contract_messages
        ):
            merged_request_messages = [*merged_request_messages, *contract_messages]
    return merged_request_messages


def _records_contain_slice(records: list[dict[str, Any]], target: list[dict[str, Any]]) -> bool:
    if not target:
        return True
    target_len = len(target)
    if target_len > len(records):
        return False
    for start in range(len(records) - target_len + 1):
        if records[start : start + target_len] == target:
            return True
    return False


def _strip_first_slice(records: list[dict[str, Any]], target: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not target:
        return list(records)
    target_len = len(target)
    if target_len > len(records):
        return list(records)
    for start in range(len(records) - target_len + 1):
        if records[start : start + target_len] == target:
            return [*records[:start], *records[start + target_len :]]
    return list(records)


def _shared_prefix_length(first: list[dict[str, Any]], second: list[dict[str, Any]]) -> int:
    limit = min(len(first), len(second))
    index = 0
    while index < limit and first[index] == second[index]:
        index += 1
    return index


def _shortest_common_supersequence(
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = len(first)
    cols = len(second)
    dp: list[list[list[dict[str, Any]]]] = [
        [[] for _ in range(cols + 1)]
        for _ in range(rows + 1)
    ]
    for row in range(rows, -1, -1):
        for col in range(cols, -1, -1):
            if row == rows and col == cols:
                continue
            if row == rows:
                dp[row][col] = list(second[col:])
                continue
            if col == cols:
                dp[row][col] = list(first[row:])
                continue
            if first[row] == second[col]:
                dp[row][col] = [dict(first[row]), *dp[row + 1][col + 1]]
                continue
            take_first = [dict(first[row]), *dp[row + 1][col]]
            take_second = [dict(second[col]), *dp[row][col + 1]]
            if len(take_first) <= len(take_second):
                dp[row][col] = take_first
            else:
                dp[row][col] = take_second
    return dp[0][0]


def _dynamic_diagnostic_messages(
    *,
    dynamic_appendix_messages: list[dict[str, Any]],
    overlay_text: str,
) -> list[dict[str, Any]]:
    diagnostics = list(dynamic_appendix_messages)
    normalized_overlay_text = str(overlay_text or "").strip()
    if normalized_overlay_text:
        diagnostics.append({"role": "assistant", "content": normalized_overlay_text})
    return diagnostics


def _contains_long_context_summary(records: list[dict[str, Any]]) -> bool:
    items = list(records or [])
    for record in items:
        role = str(record.get("role") or "").strip().lower()
        content = str(record.get("content") or "").strip()
        if role == "assistant" and content.startswith(LONG_CONTEXT_SUMMARY_PREFIX):
            return True
    return False


def _effective_stable_messages(
    *,
    stable_messages: list[dict[str, Any]],
    live_request_messages: list[dict[str, Any]],
    dynamic_appendix_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    appendix_for_overlap = _dynamic_appendix_overlap_records(dynamic_appendix_messages)
    live_without_appendix = _strip_first_slice(live_request_messages, appendix_for_overlap)
    if live_without_appendix and _contains_long_context_summary(live_without_appendix):
        return live_without_appendix
    return list(stable_messages)


def _build_request_messages(
    *,
    stable_messages: list[dict[str, Any]],
    live_request_messages: list[dict[str, Any]],
    dynamic_appendix_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    appendix_for_overlap = _dynamic_appendix_overlap_records(dynamic_appendix_messages)
    if not live_request_messages:
        request_messages = list(stable_messages)
    else:
        live_without_appendix = _strip_first_slice(live_request_messages, appendix_for_overlap)
        prefix_length = _shared_prefix_length(stable_messages, live_without_appendix)
        request_messages = [
            *list(stable_messages[:prefix_length]),
            *_shortest_common_supersequence(
                list(stable_messages[prefix_length:]),
                list(live_without_appendix[prefix_length:]),
            ),
        ]
    if dynamic_appendix_messages and not _records_contain_slice(request_messages, dynamic_appendix_messages):
        request_messages = [*request_messages, *list(dynamic_appendix_messages)]
    return request_messages


@dataclass(slots=True)
class FrontdoorPromptContract:
    request_messages: list[dict[str, Any]]
    prompt_cache_key: str
    diagnostics: dict[str, Any]
    stable_prefix_hash: str
    dynamic_appendix_hash: str
    stable_messages: list[dict[str, Any]] = field(default_factory=list)
    dynamic_appendix_messages: list[dict[str, Any]] = field(default_factory=list)
    diagnostic_dynamic_messages: list[dict[str, Any]] = field(default_factory=list)
    cache_family_revision: str = DEFAULT_CACHE_FAMILY_REVISION


def build_frontdoor_prompt_contract(
    *,
    scope: str,
    provider_model: str,
    stable_messages: list[dict[str, Any]] | None,
    dynamic_appendix_messages: list[dict[str, Any]] | None,
    tool_schemas: list[dict[str, Any]] | None,
    cache_family_revision: str | None,
    session_key: str | None = None,
    live_request_messages: list[dict[str, Any]] | None = None,
    overlay_text: str | None = None,
    overlay_section_count: int | None = None,
) -> FrontdoorPromptContract:
    normalized_stable_messages = sanitize_provider_messages(stable_messages)
    normalized_dynamic_appendix_messages = sanitize_provider_messages(dynamic_appendix_messages)
    normalized_live_request_messages = sanitize_provider_messages(live_request_messages)
    normalized_scope = str(scope or "").strip()
    normalized_provider_model = str(provider_model or "").strip()
    normalized_cache_family_revision = (
        str(cache_family_revision or "").strip() or DEFAULT_CACHE_FAMILY_REVISION
    )
    normalized_overlay_text = str(overlay_text or "").strip()
    base_stable_messages = list(normalized_stable_messages or normalized_live_request_messages)
    if normalized_scope == "ceo_frontdoor":
        normalized_effective_stable_messages = list(base_stable_messages)
        request_base_messages = list(normalized_live_request_messages or normalized_effective_stable_messages)
        normalized_request_messages = _with_dynamic_appendix_at_tail(
            request_base_messages,
            normalized_dynamic_appendix_messages,
        )
    else:
        normalized_effective_stable_messages = _effective_stable_messages(
            stable_messages=base_stable_messages,
            live_request_messages=normalized_live_request_messages,
            dynamic_appendix_messages=normalized_dynamic_appendix_messages,
        )
        normalized_request_messages = _build_request_messages(
            stable_messages=normalized_effective_stable_messages,
            live_request_messages=normalized_live_request_messages,
            dynamic_appendix_messages=normalized_dynamic_appendix_messages,
        )
    normalized_diagnostic_dynamic_messages = _dynamic_diagnostic_messages(
        dynamic_appendix_messages=normalized_dynamic_appendix_messages,
        overlay_text=normalized_overlay_text,
    )
    prompt_cache_key = build_session_prompt_cache_key(
        session_key=str(session_key or "").strip(),
        provider_model=normalized_provider_model,
        scope=normalized_scope,
        stable_messages=normalized_effective_stable_messages,
        tool_schemas=list(tool_schemas or []),
        cache_family_revision=normalized_cache_family_revision,
    )
    diagnostics = build_prompt_cache_diagnostics(
        stable_messages=normalized_effective_stable_messages,
        dynamic_appendix_messages=normalized_diagnostic_dynamic_messages,
        tool_schemas=list(tool_schemas or []),
        provider_model=normalized_provider_model,
        scope=normalized_scope,
        prompt_cache_key=prompt_cache_key,
        overlay_text=normalized_overlay_text,
        overlay_section_count=overlay_section_count,
        cache_family_revision=normalized_cache_family_revision,
        actual_request_messages=normalized_request_messages,
        actual_tool_schemas=list(tool_schemas or []),
    )
    return FrontdoorPromptContract(
        request_messages=normalized_request_messages,
        prompt_cache_key=prompt_cache_key,
        diagnostics=diagnostics,
        stable_prefix_hash=str(diagnostics.get("stable_prefix_hash") or ""),
        dynamic_appendix_hash=str(diagnostics.get("dynamic_appendix_hash") or ""),
        stable_messages=normalized_effective_stable_messages,
        dynamic_appendix_messages=normalized_dynamic_appendix_messages,
        diagnostic_dynamic_messages=normalized_diagnostic_dynamic_messages,
        cache_family_revision=normalized_cache_family_revision,
    )


__all__ = [
    "DEFAULT_CACHE_FAMILY_REVISION",
    "FrontdoorPromptContract",
    "build_frontdoor_prompt_contract",
]
