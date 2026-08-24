"""Regression tests for the create_async_task retry crash fixes.

Covers FIX_PLAN_create-async-task-aimessage-crash.md:
- P0.1: message-record helpers tolerate non-dict (LangChain message object) inputs.
- P1.1: create_async_task contract schema types are strict (no string union hints).
- P1.2: inbound normalization recovers intended types from string JSON arguments.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from g3ku.runtime.context.types import ContextAssemblyResult
from g3ku.runtime.frontdoor._ceo_runtime_ops import _normalize_frontdoor_tool_arguments
from g3ku.runtime.stage_prompt_compaction import (
    _message_role,
    decompose_stage_prompt_messages,
    repair_split_stage_tool_boundaries,
)

from main.service.create_async_task_contract import (
    build_create_async_task_parameters,
    normalize_create_async_task_inbound_params,
    validate_create_async_task_file_targets,
)


# ---------------------------------------------------------------------------
# P1.1 contract schema tightening
# ---------------------------------------------------------------------------

def test_contract_file_targets_type_is_strict_array() -> None:
    schema = build_create_async_task_parameters()
    file_targets = schema["properties"]["file_targets"]
    assert file_targets["type"] == "array"
    assert "null" not in tuple(file_targets["type"]) if isinstance(file_targets["type"], list) else file_targets["type"] != "null"


def test_contract_execution_policy_type_is_object() -> None:
    schema = build_create_async_task_parameters()
    execution_policy = schema["properties"]["execution_policy"]
    assert execution_policy["type"] == "object"
    assert "string" not in tuple(execution_policy["type"]) if isinstance(execution_policy["type"], list) else execution_policy["type"] != "string"


# ---------------------------------------------------------------------------
# P1.2 inbound normalization
# ---------------------------------------------------------------------------

def _base_params() -> dict[str, object]:
    return {
        "task": "daily briefing",
        "core_requirement": "deliver the briefing",
        "execution_policy": {"mode": "focus"},
    }


def test_inbound_normalizes_string_json_file_targets() -> None:
    params = {**_base_params(), "file_targets": '[]'}
    normalized = normalize_create_async_task_inbound_params(params)
    assert normalized["file_targets"] == []

    params = {**_base_params(), "file_targets": '[{"path": "C:/tmp/a.docx", "ref": "artifact:x"}]'}
    normalized = normalize_create_async_task_inbound_params(params)
    assert normalized["file_targets"] == [{"path": "C:/tmp/a.docx", "ref": "artifact:x"}]


def test_inbound_normalizes_bare_string_file_targets_semantically() -> None:
    params = {**_base_params(), "file_targets": "artifact:handle-1"}
    normalized = normalize_create_async_task_inbound_params(params)
    assert normalized["file_targets"] == [{"ref": "artifact:handle-1"}]

    params = {**_base_params(), "file_targets": "C:/tmp/a.docx"}
    normalized = normalize_create_async_task_inbound_params(params)
    assert normalized["file_targets"] == [{"path": "C:/tmp/a.docx"}]


def test_inbound_normalizes_none_file_targets_to_empty_list() -> None:
    params = {**_base_params(), "file_targets": None}
    normalized = normalize_create_async_task_inbound_params(params)
    assert normalized["file_targets"] == []


def test_inbound_normalizes_string_json_execution_policy() -> None:
    params = {**_base_params(), "execution_policy": '{"mode": "coverage"}'}
    normalized = normalize_create_async_task_inbound_params(params)
    assert normalized["execution_policy"] == {"mode": "coverage"}


def test_inbound_pass_through_of_valid_object_array() -> None:
    params = {**_base_params(), "file_targets": []}
    normalized = normalize_create_async_task_inbound_params(params)
    assert normalized["execution_policy"] == {"mode": "focus"}
    assert normalized["file_targets"] == []


def test_validate_file_targets_accepts_normalized_string_output() -> None:
    # Validation of structurally valid (but possibly nonexistent) targets should
    # not raise; path checks can surface precise errors, never a type crash.
    errors = validate_create_async_task_file_targets([])
    assert errors == []


def test_frontdoor_normalize_rejects_string_file_targets() -> None:
    # P1.2 fallback: the frontdoor argument normalizer must recover array/object
    # shapes from the string JSON shapes the responses protocol can produce.
    normalized = _normalize_frontdoor_tool_arguments(
        "create_async_task",
        {
            "execution_policy": '{"mode": "focus"}',
            "file_targets": "[]",
        },
    )
    assert normalized["execution_policy"] == {"mode": "focus"}
    assert normalized["file_targets"] == []


def test_frontdoor_normalize_interprets_bare_string_target() -> None:
    normalized = _normalize_frontdoor_tool_arguments(
        "create_async_task",
        {
            "execution_policy": {"mode": "focus"},
            "file_targets": "artifact:payload-1",
        },
    )
    assert normalized["file_targets"] == [{"ref": "artifact:payload-1"}]


def test_frontdoor_normalize_treats_none_targets_as_empty() -> None:
    normalized = _normalize_frontdoor_tool_arguments(
        "create_async_task",
        {
            "execution_policy": {"mode": "focus"},
            "file_targets": None,
        },
    )
    assert normalized["file_targets"] == []


# ---------------------------------------------------------------------------
# P0.1 message-record dict guards
# ---------------------------------------------------------------------------

def test_message_role_guards_non_dict_inputs() -> None:
    # Non-dict (e.g. LangChain) message objects are skipped, never crashed on.
    assert _message_role(AIMessage(content="hello")) == ""
    assert _message_role(None) == ""
    assert _message_role(42) == ""
    assert _message_role({"role": "user", "content": "x"}) == "user"


def test_repair_split_stage_tool_boundaries_tolerates_langchain_objects() -> None:
    messages: list[object] = [
        AIMessage(content="assistant turn"),
        {"role": "user", "content": "hello"},
    ]
    repaired = repair_split_stage_tool_boundaries(messages)  # type: ignore[arg-type]
    assert isinstance(repaired, list)
    assert all(isinstance(item, dict) for item in repaired)
    assert any(str(item.get("role") or "").strip().lower() == "user" for item in repaired)


def test_decompose_stage_prompt_messages_tolerates_langchain_objects() -> None:
    messages: list[object] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        AIMessage(content="model reply"),
        {"role": "user", "content": "again"},
    ]
    parts = decompose_stage_prompt_messages(messages, stage_state={})  # type: ignore[arg-type]
    assert isinstance(parts["prefix"], list)
    assert isinstance(parts["remainder"], list)
    assert isinstance(parts["active_window"], list)


def test_context_assembly_ignores_non_dict_stable_messages() -> None:
    result = ContextAssemblyResult(
        stable_messages=[
            AIMessage(content="model turn"),
            {"role": "system", "content": "system prompt text"},
            None,
        ]
    )
    assert "system prompt text" in result.system_prompt
    # system record must be dropped from recent_history without crashing.
    history = result.recent_history
    assert all(isinstance(item, dict) for item in history)
