from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from typing_extensions import TypedDict


class CeoPersistentState(TypedDict, total=False):
    user_input: dict[str, Any]
    approval_request: dict[str, Any] | None
    approval_status: str
    query_text: str
    messages: list[dict[str, Any]]
    turn_overlay_text: str | None
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
    prompt_cache_diagnostics: dict[str, Any]
    parallel_enabled: bool
    max_parallel_tool_calls: int | None
    max_iterations: int | None
    iteration: int
    response_payload: dict[str, Any]
    synthetic_tool_calls_used: bool
    analysis_text: str
    tool_call_payloads: list[dict[str, Any]]
    next_step: str
    summary_text: str
    summary_version: int


@dataclass(slots=True)
class CeoRuntimeContext:
    loop: Any
    session: Any
    session_key: str
    on_progress: Callable[..., Any] | None


@dataclass(slots=True)
class CeoPendingInterrupt:
    interrupt_id: str
    value: Any


class CeoFrontdoorInterrupted(RuntimeError):
    def __init__(self, *, interrupts: list[CeoPendingInterrupt], values: dict[str, Any]) -> None:
        super().__init__("ceo_frontdoor_interrupted")
        self.interrupts = list(interrupts or [])
        self.values = dict(values or {})


def initial_persistent_state(*, user_input: Any) -> dict[str, Any]:
    return {
        "user_input": user_input,
        "approval_request": None,
        "approval_status": "",
        "messages": [],
        "used_tools": [],
        "tool_names": [],
        "route_kind": "direct_reply",
        "summary_text": "",
        "summary_version": 0,
    }


__all__ = [
    "CeoFrontdoorInterrupted",
    "CeoPendingInterrupt",
    "CeoPersistentState",
    "CeoRuntimeContext",
    "initial_persistent_state",
]
