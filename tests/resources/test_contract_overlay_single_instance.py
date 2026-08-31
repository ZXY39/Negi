"""Regression tests for the contract / turn-only-note de-accumulation fix.

Covers the "single production-site contract, no stale note/contract in carried
history" invariants:
- (a) each request carries exactly one newest contract at the tail; zero stale
      contracts in the carried prefix (node + frontdoor).
- (b) at most one turn-only note (the current one); zero stale notes in the
      carried prefix.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from main.runtime.react_loop import ReActToolLoop
from main.runtime.stage_messages import (
    build_turn_only_system_note_message,
    is_turn_only_system_note_message,
    strip_turn_only_system_note_messages,
)


def _note(text: str = "overlay") -> dict[str, str]:
    return {"role": "user", "content": f"System note for this turn only:\n{text}"}


def _contract(payload: str = "payload-1") -> dict[str, str]:
    return {"role": "assistant", "content": f"## Runtime Tool Contract\n{payload}"}


def test_tool_contract_echo_helpers_require_canonical_kind_marker() -> None:
    from g3ku.runtime.frontdoor.tool_contract import (
        is_frontdoor_tool_contract_echo_text,
        strip_frontdoor_tool_contract_echo,
    )

    contract = "## Runtime Tool Contract\nkind: frontdoor_runtime_tool_contract\ncallable_tools: `exec`"
    assert is_frontdoor_tool_contract_echo_text(contract) is True
    assert strip_frontdoor_tool_contract_echo(contract) == ""
    assert strip_frontdoor_tool_contract_echo("answer\n\n" + contract) == "answer"
    assert is_frontdoor_tool_contract_echo_text("## Runtime Tool Contract\nordinary prose") is False


# ---------------------------------------------------------------------------
# shared note helpers
# ---------------------------------------------------------------------------

def test_build_turn_only_note_message_returns_none_for_blank() -> None:
    assert build_turn_only_system_note_message(None) is None
    assert build_turn_only_system_note_message("   ") is None


def test_build_turn_only_note_message_uses_shared_prefix() -> None:
    message = build_turn_only_system_note_message("hello")
    assert message == {"role": "user", "content": "System note for this turn only:\nhello"}


def test_is_turn_only_note_detects_only_user_prefix_records() -> None:
    assert is_turn_only_system_note_message(_note()) is True
    assert is_turn_only_system_note_message({"role": "user", "content": "System note for this turn only:"}) is True
    # Multimodal content list must never be treated as a note.
    assert is_turn_only_system_note_message({"role": "user", "content": [{"type": "image"}]}) is False
    assert is_turn_only_system_note_message({"role": "assistant", "content": "System note for this turn only:\nx"}) is False
    assert is_turn_only_system_note_message({"role": "user", "content": "plain"}) is False
    assert is_turn_only_system_note_message(None) is False
    assert is_turn_only_system_note_message("not a dict") is False


def test_strip_turn_only_notes_keeps_everything_else() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        _note("stale-1"),
        _note("stale-2"),
        {"role": "assistant", "content": "reply"},
    ]
    stripped = strip_turn_only_system_note_messages(messages)
    assert stripped == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "reply"},
    ]
    assert strip_turn_only_system_note_messages(None) == []


# ---------------------------------------------------------------------------
# node same-turn append-only merge
# ---------------------------------------------------------------------------

def test_node_same_turn_merge_drops_stale_note_and_contract_from_carried_prefix() -> None:
    previous_request_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "prompt"},
        {"role": "assistant", "content": "assistant-1"},
        {"role": "tool", "tool_call_id": "call-1", "name": "content_open", "content": "ok"},
        _note("stale-note"),
        _contract("stale-contract"),
    ]
    current_model_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "prompt"},
    ]
    pending_delta_messages = [
        {"role": "assistant", "content": "assistant-2"},
        {"role": "tool", "tool_call_id": "call-2", "name": "content_open", "content": "ok"},
    ]
    request_tail_messages = [
        _note("current-note"),
        _contract("current-contract"),
    ]
    merged = ReActToolLoop._same_turn_append_only_request_messages(
        previous_request_messages=previous_request_messages,
        current_model_messages=current_model_messages,
        pending_delta_messages=pending_delta_messages,
        request_tail_messages=request_tail_messages,
    )
    carved_prefix = merged[: len(current_model_messages) + len(pending_delta_messages)]
    assert all(not is_turn_only_system_note_message(item) for item in carved_prefix)
    assert all(str(item.get("content") or "").startswith("## Runtime Tool Contract") is False for item in carved_prefix)
    tail_contents = [str(item.get("content") or "") for item in merged]
    assert sum(1 for c in tail_contents if c.startswith("System note for this turn only:")) == 1
    assert sum(1 for c in tail_contents if c.startswith("## Runtime Tool Contract")) == 1
    assert tail_contents[-2:] == ["System note for this turn only:\ncurrent-note", "## Runtime Tool Contract\ncurrent-contract"]


def test_node_same_turn_merge_multiple_rounds_only_keep_latest_tail() -> None:
    base = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "prompt"},
    ]
    built = list(base)
    running_previous: list[dict[str, str]] | None = None
    for index in range(1, 4):
        delta = [
            {"role": "assistant", "content": f"assistant-{index}"},
            {"role": "tool", "tool_call_id": f"call-{index}", "name": "content_open", "content": "ok"},
        ]
        tail = [_note(f"note-{index}"), _contract(f"contract-{index}")]
        merged = ReActToolLoop._same_turn_append_only_request_messages(
            previous_request_messages=running_previous,
            current_model_messages=built,
            pending_delta_messages=delta,
            request_tail_messages=tail,
        )
        contents = [str(item.get("content") or "") for item in merged]
        assert sum(1 for c in contents if c.startswith("System note for this turn only:")) == 1
        assert sum(1 for c in contents if c.startswith("## Runtime Tool Contract")) == 1
        assert contents[-1] == f"## Runtime Tool Contract\ncontract-{index}"
        running_previous = [dict(item) for item in merged]
        built = list(merged)[: (len(built) + 2)]


# ---------------------------------------------------------------------------
# node fresh-turn seed path
# ---------------------------------------------------------------------------

def test_node_fresh_turn_seed_drops_stale_note_and_contract() -> None:
    seed_records = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "prompt"},
        {"role": "assistant", "content": "assistant-1"},
        {"role": "tool", "tool_call_id": "call-1", "name": "content_open", "content": "ok"},
        _note("stale-seed-note"),
        _contract("stale-seed-contract"),
    ]
    stable_records = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "prompt"},
        {"role": "assistant", "content": "assistant-1"},
        {"role": "tool", "tool_call_id": "call-1", "name": "content_open", "content": "ok"},
    ]
    live_records = [
        *stable_records,
        _note("current-note"),
        _contract("current-contract"),
    ]
    merged = ReActToolLoop._fresh_turn_live_request_messages_from_seed_request(
        seed_request_messages=seed_records,
        stable_messages=stable_records,
        live_request_messages=live_records,
    )
    contents = [str(item.get("content") or "") for item in merged]
    assert sum(1 for c in contents if c.startswith("System note for this turn only:")) == 1
    assert sum(1 for c in contents if c.startswith("## Runtime Tool Contract")) == 1
    assert contents[-2:] == ["System note for this turn only:\ncurrent-note", "## Runtime Tool Contract\ncurrent-contract"]


# ---------------------------------------------------------------------------
# frontdoor prompt contract tail
# ---------------------------------------------------------------------------

def _frontdoor_prompt_contract_module():
    from g3ku.runtime.frontdoor import prompt_cache_contract

    return prompt_cache_contract


def _contract_record() -> dict[str, str]:
    return {"role": "user", "content": json.dumps({"message_type": "frontdoor_runtime_tool_contract"}, ensure_ascii=False)}


def test_frontdoor_append_tail_cleans_stale_contract_from_body() -> None:
    from g3ku.runtime.frontdoor import prompt_cache_contract

    contract = _contract_record()
    result = prompt_cache_contract._with_dynamic_appendix_at_tail(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            dict(contract),
            _note("stale-note"),
            dict(contract),
        ],
        dynamic_appendix_messages=[dict(contract)],
    )
    assert result == [
        {"role": "system", "content": "sys"},
        dict(contract),
        {"role": "user", "content": "hello"},
    ]
    assert result[1] == contract
    assert result[-1] == {"role": "user", "content": "hello"}


def test_frontdoor_prompt_contract_key_stable_with_dirty_live_base() -> None:
    from g3ku.runtime.frontdoor.prompt_cache_contract import build_frontdoor_prompt_contract

    contract = _contract_record()
    clean_backend = _frontdoor_prompt_contract_module()
    clean = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-test",
        stable_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ],
        dynamic_appendix_messages=[dict(contract)],
        live_request_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ],
        tool_schemas=[],
        cache_family_revision="frontdoor:v1",
    )
    dirty = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-test",
        stable_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ],
        dynamic_appendix_messages=[dict(contract)],
        live_request_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            dict(contract),
            _note("stale-note"),
            dict(contract),
        ],
        tool_schemas=[],
        cache_family_revision="frontdoor:v1",
    )
    assert clean.prompt_cache_key == dirty.prompt_cache_key
    assert list(clean.request_messages) == list(dirty.request_messages) == [
        {"role": "system", "content": "sys"},
        dict(contract),
        {"role": "user", "content": "hello"},
    ]

# ---------------------------------------------------------------------------
# frontdoor fresh-turn seed: previous actual request that carried a note
# ---------------------------------------------------------------------------

def test_frontdoor_fresh_turn_seed_with_stale_note_keeps_real_prefix() -> None:
    from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner

    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    previous_record_path = None  # session carries inline history, not a file

    attempt = SimpleNamespace(
        _frontdoor_previous_actual_request_path="",
        _frontdoor_previous_actual_request_history=[
            {"path": "artifact:irrelevant", "turn_id": "turn-1"}
        ],
        _load_frontdoor_actual_request_payload=lambda **_: {
            "request_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old retrieved"},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }]},
                {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-1", "content": '{"status":"success"}'},
                _note("stale-seed-note"),
                _contract("stale-seed-contract"),
            ]
        },
    )
    previous_record = SimpleNamespace(
        get=lambda key, default=None: {
            "request_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old retrieved"},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }]},
                {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-1", "content": '{"status":"success"}'},
                _note("stale-seed-note"),
                _contract("stale-seed-contract"),
            ]
        }.get(key, default)
    )
    import g3ku.runtime.frontdoor._ceo_runtime_ops as ceo_runtime_ops

    def _fake_previous_actual_request_record(session):
        _ = session
        return previous_record

    original = ceo_runtime_ops.CeoFrontDoorRuntimeOps._frontdoor_previous_actual_request_record
    ceo_runtime_ops.CeoFrontDoorRuntimeOps._frontdoor_previous_actual_request_record = (
        staticmethod(lambda session: previous_record)
    )
    try:
        scaffold = runner._fresh_turn_live_request_messages_from_previous_actual_request(
            session=attempt,
            stable_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old retrieved"},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }]},
                {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-1", "content": '{"status":"success"}'},
                {"role": "assistant", "content": "final answer"},
            ],
            live_request_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old retrieved"},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }]},
                {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-1", "content": '{"status":"success"}'},
                {"role": "assistant", "content": "final answer"},
                {"role": "user", "content": "next user"},
            ],
        )
    finally:
        ceo_runtime_ops.CeoFrontDoorRuntimeOps._frontdoor_previous_actual_request_record = original

    contents = [str(item.get("content") or "") for item in scaffold]
    assert any(c == "old retrieved" for c in contents)
    assert any(c == '{"status":"success"}' for c in contents)
    assert any(c == "next user" for c in contents)
    # stale note/contract must not survive the seed
    assert sum(1 for c in contents if c.startswith("System note for this turn only:")) == 0
    assert sum(1 for c in contents if c.startswith("## Runtime Tool Contract")) == 0
