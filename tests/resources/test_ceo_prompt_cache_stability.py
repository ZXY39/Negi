from __future__ import annotations


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
            {"role": "assistant", "content": "## Visible Skills For This Turn\n- `focused-skill`"}
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
            {"role": "assistant", "content": "## Visible Skills For This Turn\n- `focused-skill`"}
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

    first = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[
            {
                "role": "user",
                "content": {
                    "message_type": "frontdoor_runtime_tool_contract",
                    "callable_tool_names": ["exec"],
                    "candidate_tool_names": ["filesystem_write"],
                    "hydrated_tool_names": [],
                    "visible_skill_ids": ["memory"],
                    "stage_summary": {"active_stage_id": "stage:1", "transition_required": False},
                    "contract_revision": "frontdoor:v1",
                },
            }
        ],
        tool_schemas=tool_schemas,
        cache_family_revision="frontdoor:v1",
    )
    second = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[
            {
                "role": "user",
                "content": {
                    "message_type": "frontdoor_runtime_tool_contract",
                    "callable_tool_names": ["exec", "filesystem_write"],
                    "candidate_tool_names": [],
                    "hydrated_tool_names": ["filesystem_write"],
                    "visible_skill_ids": ["memory"],
                    "stage_summary": {"active_stage_id": "stage:1", "transition_required": False},
                    "contract_revision": "frontdoor:v1",
                },
            }
        ],
        tool_schemas=tool_schemas,
        cache_family_revision="frontdoor:v1",
    )

    assert _field(first, "prompt_cache_key") == _field(second, "prompt_cache_key")
    assert _field(first, "request_messages") != _field(second, "request_messages")


def test_frontdoor_prompt_contract_keeps_dynamic_appendix_directly_after_system_for_main_lane() -> None:
    from g3ku.runtime.frontdoor.prompt_cache_contract import build_frontdoor_prompt_contract

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
            {"role": "user", "content": '{"message_type":"frontdoor_runtime_tool_contract"}'},
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
        {"role": "assistant", "content": "## Retrieved Context\n- memory"},
        {"role": "user", "content": '{"message_type":"frontdoor_runtime_tool_contract"}'},
        {"role": "user", "content": "bootstrap user"},
        {"role": "assistant", "content": "live assistant tool call"},
        {"role": "tool", "name": "load_skill_context", "tool_call_id": "call-skill-1", "content": '{"ok": true}'},
        {"role": "assistant", "content": "old request-only appendix"},
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
