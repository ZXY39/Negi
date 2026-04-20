from __future__ import annotations

from g3ku.runtime.frontdoor.tool_contract import build_frontdoor_tool_contract


def _field(value, name: str):
    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


def test_frontdoor_prompt_cache_key_ignores_dynamic_appendix_changes_when_stable_messages_match() -> None:
    from g3ku.runtime.frontdoor.prompt_cache_contract import build_frontdoor_prompt_contract

    stable_messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "原始用户问题"},
    ]
    tool_schemas = [
        {
            "name": "memory_write",
            "description": "",
            "parameters": {"type": "object"},
        }
    ]

    first = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[
            {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory A"}
        ],
        tool_schemas=tool_schemas,
        cache_family_revision="exp:rev-1",
    )
    second = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[
            {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory B"}
        ],
        tool_schemas=tool_schemas,
        cache_family_revision="exp:rev-1",
    )

    assert _field(first, "prompt_cache_key") == _field(second, "prompt_cache_key")
    assert _field(first, "stable_prefix_hash") == _field(second, "stable_prefix_hash")
    assert _field(first, "dynamic_appendix_hash") != _field(second, "dynamic_appendix_hash")
    assert _field(first, "request_messages") != _field(second, "request_messages")


def test_frontdoor_prompt_cache_key_changes_when_capability_revision_changes() -> None:
    from g3ku.runtime.frontdoor.prompt_cache_contract import build_frontdoor_prompt_contract

    stable_messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "原始用户问题"},
    ]
    tool_schemas = [
        {
            "name": "memory_write",
            "description": "",
            "parameters": {"type": "object"},
        }
    ]

    first = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[
            {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory A"}
        ],
        tool_schemas=tool_schemas,
        cache_family_revision="exp:rev-1",
    )
    second = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[
            {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory A"}
        ],
        tool_schemas=tool_schemas,
        cache_family_revision="exp:rev-2",
    )

    assert _field(first, "prompt_cache_key") != _field(second, "prompt_cache_key")


def test_frontdoor_prompt_cache_key_changes_when_exposure_revision_changes() -> None:
    from g3ku.runtime.frontdoor.prompt_cache_contract import build_frontdoor_prompt_contract

    stable_messages = [
        {"role": "system", "content": "stable system\n\n## Capability Exposure Snapshot\n- Exposure revision: `exp:rev-1`"},
        {"role": "user", "content": "鍘熷鐢ㄦ埛闂"},
    ]
    tool_schemas = [
        {
            "name": "memory_write",
            "description": "",
            "parameters": {"type": "object"},
        }
    ]

    first = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[
            {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
        ],
        tool_schemas=tool_schemas,
        cache_family_revision="exp:rev-1",
    )
    second = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=[
            {"role": "system", "content": "stable system\n\n## Capability Exposure Snapshot\n- Exposure revision: `exp:rev-2`"},
            {"role": "user", "content": "鍘熷鐢ㄦ埛闂"},
        ],
        dynamic_appendix_messages=[
            {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
        ],
        tool_schemas=tool_schemas,
        cache_family_revision="exp:rev-2",
    )

    assert _field(first, "prompt_cache_key") != _field(second, "prompt_cache_key")


def test_frontdoor_prompt_cache_key_ignores_dynamic_tool_contract_changes() -> None:
    from g3ku.runtime.frontdoor.prompt_cache_contract import build_frontdoor_prompt_contract

    stable_messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "stable bootstrap"},
    ]
    tool_schemas = [
        {
            "name": "exec",
            "description": "",
            "parameters": {"type": "object"},
        }
    ]

    first_contract = build_frontdoor_tool_contract(
        callable_tool_names=["exec"],
        candidate_tool_names=["filesystem_write"],
        candidate_tool_items=[{"tool_id": "filesystem_write", "description": "Write file content"}],
        hydrated_tool_names=[],
        frontdoor_stage_state={"active_stage_id": "stage:1", "transition_required": False, "stages": []},
        candidate_skill_ids=["memory"],
        contract_revision="frontdoor:v1",
    ).to_message()
    second_contract = build_frontdoor_tool_contract(
        callable_tool_names=["exec", "filesystem_write"],
        candidate_tool_names=[],
        candidate_tool_items=[],
        hydrated_tool_names=["filesystem_write"],
        frontdoor_stage_state={"active_stage_id": "stage:1", "transition_required": False, "stages": []},
        candidate_skill_ids=["memory"],
        contract_revision="frontdoor:v1",
    ).to_message()

    first = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[first_contract],
        tool_schemas=tool_schemas,
        cache_family_revision="frontdoor:v1",
    )
    second = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[second_contract],
        tool_schemas=tool_schemas,
        cache_family_revision="frontdoor:v1",
    )

    assert _field(first, "prompt_cache_key") == _field(second, "prompt_cache_key")
    assert _field(first, "request_messages") != _field(second, "request_messages")


def test_frontdoor_prompt_cache_key_ignores_repair_required_contract_changes() -> None:
    from g3ku.runtime.frontdoor.prompt_cache_contract import build_frontdoor_prompt_contract

    stable_messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "stable bootstrap"},
    ]
    tool_schemas = [
        {
            "name": "exec",
            "description": "",
            "parameters": {"type": "object"},
        }
    ]

    first_contract = build_frontdoor_tool_contract(
        callable_tool_names=["exec"],
        candidate_tool_names=[],
        candidate_tool_items=[],
        repair_required_tool_items=[
            {"tool_id": "agent_browser", "description": "Browser automation", "reason": "missing required paths"}
        ],
        hydrated_tool_names=[],
        frontdoor_stage_state={"active_stage_id": "stage:1", "transition_required": False, "stages": []},
        candidate_skill_ids=[],
        repair_required_skill_items=[],
        contract_revision="frontdoor:v1",
    ).to_message()
    second_contract = build_frontdoor_tool_contract(
        callable_tool_names=["exec"],
        candidate_tool_names=[],
        candidate_tool_items=[],
        repair_required_tool_items=[
            {"tool_id": "agent_browser", "description": "Browser automation", "reason": "missing required paths"}
        ],
        hydrated_tool_names=[],
        frontdoor_stage_state={"active_stage_id": "stage:1", "transition_required": False, "stages": []},
        candidate_skill_ids=[],
        repair_required_skill_items=[
            {"skill_id": "writing-skills", "description": "Skill maintenance workflow", "reason": "missing required bins"}
        ],
        contract_revision="frontdoor:v1",
    ).to_message()

    first = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[first_contract],
        tool_schemas=tool_schemas,
        cache_family_revision="frontdoor:v1",
    )
    second = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[second_contract],
        tool_schemas=tool_schemas,
        cache_family_revision="frontdoor:v1",
    )

    assert _field(first, "prompt_cache_key") == _field(second, "prompt_cache_key")
    assert _field(first, "request_messages") != _field(second, "request_messages")


def test_frontdoor_prompt_cache_key_ignores_tool_schema_changes_when_stable_prefix_matches() -> None:
    from g3ku.runtime.frontdoor.prompt_cache_contract import build_frontdoor_prompt_contract

    stable_messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "stable bootstrap"},
    ]

    first = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[
            {"role": "assistant", "content": "## Retrieved Context\n- memory"}
        ],
        tool_schemas=[
            {
                "name": "web_fetch",
                "description": "",
                "parameters": {"type": "object"},
            }
        ],
        cache_family_revision="frontdoor:v1",
    )
    second = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[
            {"role": "assistant", "content": "## Retrieved Context\n- memory"}
        ],
        tool_schemas=[
            {
                "name": "filesystem_edit",
                "description": "",
                "parameters": {"type": "object"},
            }
        ],
        cache_family_revision="frontdoor:v1",
    )

    assert _field(first, "request_messages") == _field(second, "request_messages")
    assert _field(first, "prompt_cache_key") == _field(second, "prompt_cache_key")
    assert _field(first, "diagnostics")["actual_tool_schema_hash"] != _field(second, "diagnostics")["actual_tool_schema_hash"]


def test_frontdoor_prompt_contract_keeps_same_turn_contract_history_append_only() -> None:
    from g3ku.runtime.frontdoor.prompt_cache_contract import build_frontdoor_prompt_contract

    contract_message = build_frontdoor_tool_contract(
        callable_tool_names=["exec", "submit_next_stage"],
        candidate_tool_names=[],
        candidate_tool_items=[],
        hydrated_tool_names=[],
        frontdoor_stage_state={"active_stage_id": "stage:1", "transition_required": False, "stages": []},
        candidate_skill_ids=[],
        contract_revision="frontdoor:v1",
    ).to_message()
    contract_record = {"role": contract_message["role"], "content": contract_message["content"]}

    first = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=[
            {"role": "system", "content": "stable system"},
            {"role": "user", "content": "start"},
        ],
        dynamic_appendix_messages=[
            contract_message,
        ],
        live_request_messages=[
            {"role": "system", "content": "stable system"},
            {"role": "user", "content": "start"},
        ],
        tool_schemas=[],
        cache_family_revision="frontdoor:v1",
    )
    second = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=[
            {"role": "system", "content": "stable system"},
            {"role": "user", "content": "start"},
        ],
        dynamic_appendix_messages=[
            contract_message,
        ],
        live_request_messages=[
            {"role": "system", "content": "stable system"},
            {"role": "user", "content": "start"},
            contract_record,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "web_fetch", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "web_fetch",
                "tool_call_id": "call-1",
                "content": '{"status":"success"}',
            },
        ],
        tool_schemas=[],
        cache_family_revision="frontdoor:v1",
    )

    assert list(_field(first, "request_messages")) == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "start"},
        contract_record,
    ]
    assert list(_field(second, "request_messages")) == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "start"},
        contract_record,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "web_fetch", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "web_fetch",
            "tool_call_id": "call-1",
            "content": '{"status":"success"}',
        },
        contract_record,
    ]
    assert list(_field(second, "request_messages"))[: len(list(_field(first, "request_messages")))] == list(
        _field(first, "request_messages")
    )


def test_frontdoor_prompt_contract_appends_dynamic_appendix_at_tail_for_main_lane() -> None:
    from g3ku.runtime.frontdoor.prompt_cache_contract import build_frontdoor_prompt_contract

    contract_message = build_frontdoor_tool_contract(
        callable_tool_names=["submit_next_stage"],
        candidate_tool_names=["web_fetch"],
        candidate_tool_items=[{"tool_id": "web_fetch", "description": "HTTP fetch helper"}],
        hydrated_tool_names=[],
        frontdoor_stage_state={},
        contract_revision="frontdoor:v1",
    ).to_message()
    contract_record = {"role": contract_message["role"], "content": contract_message["content"]}

    contract = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=[
            {"role": "system", "content": "stable system"},
            {"role": "user", "content": "bootstrap user"},
            {"role": "assistant", "content": "stable history"},
        ],
        dynamic_appendix_messages=[
            {"role": "assistant", "content": "## Retrieved Context\n- memory"},
            contract_message,
        ],
        live_request_messages=[
            {"role": "system", "content": "stable system"},
            {"role": "user", "content": "bootstrap user"},
            {"role": "assistant", "content": "live assistant tool call"},
            {"role": "tool", "name": "load_skill_context", "tool_call_id": "call-skill-1", "content": '{"ok": true}'},
            {"role": "assistant", "content": "old request-only appendix"},
        ],
        tool_schemas=[],
        cache_family_revision="frontdoor:v1",
    )

    request_messages = list(_field(contract, "request_messages"))
    assert request_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "bootstrap user"},
        {"role": "assistant", "content": "live assistant tool call"},
        {"role": "tool", "name": "load_skill_context", "tool_call_id": "call-skill-1", "content": '{"ok": true}'},
        {"role": "assistant", "content": "old request-only appendix"},
        {"role": "assistant", "content": "## Retrieved Context\n- memory"},
        contract_record,
    ]
    assert list(_field(contract, "stable_messages")) == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "bootstrap user"},
        {"role": "assistant", "content": "stable history"},
    ]


def test_frontdoor_tool_contract_stage_summary_omits_round_counters_and_trace_fields() -> None:
    from g3ku.runtime.frontdoor.tool_contract import build_frontdoor_tool_contract

    payload = build_frontdoor_tool_contract(
        callable_tool_names=["exec"],
        candidate_tool_names=["filesystem_write"],
        hydrated_tool_names=["exec"],
        frontdoor_stage_state={
            "active_stage_id": "frontdoor-stage-2",
            "transition_required": False,
            "stages": [
                {
                    "stage_id": "frontdoor-stage-2",
                    "stage_goal": "inspect repo",
                    "tool_round_budget": 6,
                    "tool_rounds_used": 3,
                    "stage_kind": "normal",
                    "final_stage": False,
                    "completed_stage_summary": "should not leak",
                    "key_refs": [{"ref": "file:a"}],
                    "rounds": [{"round_id": "round-1"}],
                    "created_at": "2026-04-16T02:49:26+08:00",
                }
            ],
        },
        contract_revision="frontdoor:v1",
    ).to_message_payload()

    assert payload["stage_summary"] == {
        "active_stage_id": "frontdoor-stage-2",
        "transition_required": False,
        "active_stage": {
            "stage_id": "frontdoor-stage-2",
            "stage_goal": "inspect repo",
            "tool_round_budget": 6,
            "stage_kind": "normal",
            "final_stage": False,
        },
    }


def test_frontdoor_turn_overlay_appends_ephemeral_tail_message_without_mutating_existing_prefix() -> None:
    from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport

    base_messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "stable bootstrap"},
    ]

    request_messages = CeoFrontDoorSupport._apply_turn_overlay(
        base_messages,
        overlay_text="repair only",
    )

    assert request_messages[:2] == base_messages
    assert len(request_messages) == 3
    assert request_messages[-1] == {
        "role": "user",
        "content": "System note for this turn only:\nrepair only",
    }
