from __future__ import annotations



from dataclasses import dataclass

from typing import Annotated, Any, Callable



from langgraph.graph.message import add_messages

from typing_extensions import TypedDict

from .canonical_context import default_frontdoor_canonical_context
from .prompt_cache_contract import DEFAULT_CACHE_FAMILY_REVISION




class CeoPersistentState(TypedDict, total=False):

    session_key: str

    user_input: dict[str, Any]

    approval_request: dict[str, Any] | None

    approval_status: str

    query_text: str

    messages: Annotated[list[Any], add_messages]

    stable_messages: list[dict[str, Any]]

    dynamic_appendix_messages: list[dict[str, Any]]
    frontdoor_live_request_messages: list[dict[str, Any]]
    frontdoor_request_body_messages: list[dict[str, Any]]
    frontdoor_history_shrink_reason: str
    frontdoor_token_preflight_diagnostics: dict[str, Any]

    turn_overlay_text: str | None

    tool_names: list[str]
    provider_tool_names: list[str]
    pending_provider_tool_names: list[str]
    provider_tool_exposure_pending: bool
    provider_tool_exposure_revision: str
    provider_tool_exposure_commit_reason: str

    candidate_tool_names: list[str]

    candidate_tool_items: list[dict[str, Any]]

    hydrated_tool_names: list[str]

    visible_skill_ids: list[str]

    candidate_skill_ids: list[str]

    rbac_visible_tool_names: list[str]

    rbac_visible_skill_ids: list[str]

    attachment_reopen_targets: list[dict[str, Any]]

    cache_family_revision: str

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

    frontdoor_actual_request_path: str

    frontdoor_actual_request_history: list[dict[str, Any]]

    frontdoor_selection_debug: dict[str, Any]

    parallel_enabled: bool

    max_parallel_tool_calls: int | None

    max_iterations: int | None

    iteration: int

    response_payload: dict[str, Any]

    synthetic_tool_calls_used: bool

    analysis_text: str

    tool_call_payloads: list[dict[str, Any]]

    verified_task_ids: list[str]

    next_step: str

    frontdoor_stage_state: dict[str, Any]
    frontdoor_canonical_context: dict[str, Any]

    compression_state: dict[str, Any]

    agent_runtime: str





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





class CeoFrontdoorInterrupted(RuntimeError):  # noqa: N818

    def __init__(self, *, interrupts: list[CeoPendingInterrupt], values: dict[str, Any]) -> None:

        super().__init__("ceo_frontdoor_interrupted")

        self.interrupts = list(interrupts or [])

        self.values = dict(values or {})





def initial_persistent_state(*, user_input: Any) -> dict[str, Any]:

    return {

        "session_key": "",

        "user_input": user_input,

        "approval_request": None,

        "approval_status": "",

        "messages": [],

        "stable_messages": [],

        "dynamic_appendix_messages": [],
        "frontdoor_live_request_messages": [],
        "frontdoor_request_body_messages": [],
        "frontdoor_history_shrink_reason": "",
        "frontdoor_token_preflight_diagnostics": {},

        "used_tools": [],

        "tool_names": [],
        "provider_tool_names": [],
        "pending_provider_tool_names": [],
        "provider_tool_exposure_pending": False,
        "provider_tool_exposure_revision": "",
        "provider_tool_exposure_commit_reason": "",

        "candidate_tool_names": [],

        "candidate_tool_items": [],

        "hydrated_tool_names": [],

        "visible_skill_ids": [],

        "candidate_skill_ids": [],

        "rbac_visible_tool_names": [],

        "rbac_visible_skill_ids": [],

        "attachment_reopen_targets": [],

        "cache_family_revision": DEFAULT_CACHE_FAMILY_REVISION,

        "frontdoor_actual_request_path": "",

        "frontdoor_actual_request_history": [],

        "frontdoor_selection_debug": {},

        "route_kind": "direct_reply",

        "verified_task_ids": [],

        "frontdoor_stage_state": {

            "active_stage_id": "",

            "transition_required": False,

            "stages": [],

        },

        "frontdoor_canonical_context": default_frontdoor_canonical_context(),

        "compression_state": {

            "status": "",

            "text": "",

            "source": "",

            "needs_recheck": False,

        },

        "agent_runtime": "create_agent",

    }





__all__ = [

    "CeoFrontdoorInterrupted",

    "CeoPendingInterrupt",

    "CeoPersistentState",

    "CeoRuntimeContext",

    "initial_persistent_state",

]
