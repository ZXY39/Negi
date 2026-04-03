from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict


class CeoGraphState(TypedDict, total=False):
    user_input: Any
    session: Any
    on_progress: Any
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
    runtime_context: dict[str, Any]
    final_output: str
    error_message: str
    model_refs: list[str]
    prompt_cache_key: str
    parallel_enabled: bool
    max_parallel_tool_calls: int | None
    max_iterations: int | None
    iteration: int
    visible_tools: dict[str, Any]
    langchain_tools: list[Any]
    langchain_tool_map: dict[str, Any]
    response_message: Any
    response_content: Any
    synthetic_tool_calls_used: bool
    analysis_text: str
    tool_call_payloads: list[dict[str, Any]]
    next_step: str
