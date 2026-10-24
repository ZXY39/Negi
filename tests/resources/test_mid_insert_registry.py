from __future__ import annotations

from typing import Any

from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.frontdoor.prompt_cache_contract import build_frontdoor_prompt_contract
from main.runtime import mid_insert as mi
from main.runtime.react_loop import ReActToolLoop

CONTRACT = {
    "role": "user",
    "content": "## Runtime Tool Contract\nkind: frontdoor_runtime_tool_contract\ncallable_tools: `exec`",
}
NODE_CONTRACT = {
    "role": "user",
    "content": "## Runtime Tool Contract\nkind: node_runtime_tool_contract\ncallable_tools: `exec`",
}
TURN_ONLY = {"role": "user", "content": "System note for this turn only:\nstale overlay"}
NOTICE = {"role": "user", "content": "[RUNTIME_NOTICE]\n\n### Skills newly readable\n- `x`"}
APPEND = {
    "role": "user",
    "content": '[NOTICE_TAIL]\n{"kind":"raw_notice_window","notice_count":1,"notices":[{"message":"keep"}]}',
}
MEMORY = {"role": "assistant", "content": "## 长期记忆\n- preference"}
HINT = {"role": "assistant", "content": "## 长期记忆写入提示\n- write it"}
RELAY = {"role": "user", "content": "【任务收到用户定向通知】do it"}
USER = {"role": "user", "content": "real user text"}
ASSISTANT = {"role": "assistant", "content": "a"}
TOOL = {"role": "tool", "tool_call_id": "c1", "name": "filesystem_stat", "content": '{"ok":true}'}


def _family_fixtures() -> dict[str, dict[str, Any]]:
    return {
        mi.KIND_RUNTIME_NOTICE: dict(NOTICE),
        mi.KIND_TURN_ONLY_NOTE: dict(TURN_ONLY),
        mi.KIND_APPEND_NOTICE: dict(APPEND),
        mi.KIND_LEGACY_RUNTIME_CONTRACT: dict(CONTRACT),
        mi.KIND_SYSTEM_RELAY_NOTICE: dict(RELAY),
        mi.KIND_RETRIEVAL_MEMORY_SNAPSHOT: dict(MEMORY),
        mi.KIND_RETRIEVAL_OVERLAY: dict(HINT),
    }


def test_each_message_shape_is_claimed_by_exactly_one_family() -> None:
    for expected_kind, fixture in _family_fixtures().items():
        claims = [
            family.kind
            for family in mi.MID_INSERT_FAMILIES
            if family.level == mi.LEVEL_MESSAGE
            and family.classify is not None
            and family.classify(fixture)
        ]
        assert claims == [expected_kind]


def test_legacy_contract_frontdoor_and_node_share_one_family() -> None:
    assert mi.classify_message(CONTRACT) == mi.KIND_LEGACY_RUNTIME_CONTRACT
    assert mi.classify_message(NODE_CONTRACT) == mi.KIND_LEGACY_RUNTIME_CONTRACT


def test_hidden_event_metadata_wins_over_prefix() -> None:
    hidden = {
        "role": "user",
        "content": "ordinary looking text",
        "metadata": {"internal_prompt_kind": "heartbeat_rule", "ui_visible": False},
    }
    assert mi.classify_message(hidden) == mi.KIND_HIDDEN_INTERNAL_EVENT
    fallback = {"role": "user", "content": "# Heartbeat Rules\n- ping"}
    assert mi.classify_message(fallback) == mi.KIND_HIDDEN_INTERNAL_EVENT
    cron = {"role": "user", "content": "[CRON INTERNAL EVENT]\n{}"}
    assert mi.classify_message(cron) == mi.KIND_HIDDEN_INTERNAL_EVENT


def test_tool_contract_marker_is_not_a_message_family() -> None:
    block = (
        "[TOOL_CONTRACT:filesystem_edit:tcf:01234546789abcdef]\n"
        "body\n"
        "[/TOOL_CONTRACT]"
    )
    tool_message = {"role": "tool", "tool_call_id": "c1", "content": block}
    assert mi.classify_message(tool_message) is None
    markers = mi.scan_markers(tool_message)
    assert [(marker.kind, marker.tool_id, marker.fingerprint) for marker in markers] == [
        (mi.KIND_TOOL_CONTRACT_BLOCK, "filesystem_edit", "tcf:01234546789abcdef")
    ]


def test_fake_contract_header_inside_body_is_not_a_marker() -> None:
    body = (
        "here is an example:\n"
        "[TOOL_CONTRACT:other_tool:tcf:deadbeef]\n"
        "example\n"
        "[/TOOL_CONTRACT]"
    )
    assert mi.scan_markers({"role": "tool", "content": body}) == ()


def test_silent_trace_marker_scan() -> None:
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call-silent",
                "type": "function",
                "function": {
                    "name": "silent",
                    "arguments": '{"subject":"task:t1","reason":"done"}',
                },
            }
        ],
    }
    assert mi.classify_message(assistant) is None
    markers = mi.scan_markers(assistant)
    assert [(marker.kind, marker.call_ids) for marker in markers] == [
        (mi.KIND_SILENT_TRACE, ("call-silent",))
    ]


def test_marker_scan_ignores_plain_assistant_text() -> None:
    assert mi.scan_markers({"role": "assistant", "content": "no markers here"}) == ()


def test_strip_matrix_keeps_append_notice_and_system_relay_everywhere() -> None:
    fixture = [
        {"role": "system", "content": "system"},
        MEMORY,
        HINT,
        CONTRACT,
        TURN_ONLY,
        NOTICE,
        APPEND,
        RELAY,
        USER,
    ]
    for lane in mi.LANES:
        for stage in mi.STAGES:
            kept = mi.strip_for_stage(fixture, stage=stage, lane=lane)
            kinds = [mi.classify_message(item) for item in kept]
            assert mi.KIND_APPEND_NOTICE in kinds
            assert mi.KIND_SYSTEM_RELAY_NOTICE in kinds
            assert mi.KIND_HIDDEN_INTERNAL_EVENT not in kinds


def test_strip_matrix_family_presence_per_stage() -> None:
    fixture = [
        {"role": "system", "content": "system"},
        MEMORY,
        HINT,
        CONTRACT,
        TURN_ONLY,
        NOTICE,
        APPEND,
        RELAY,
    ]

    def kept_kinds(stage: str, lane: str) -> set[str]:
        return {
            kind
            for kind in (mi.classify_message(item) for item in mi.strip_for_stage(fixture, stage=stage, lane=lane))
            if kind
        }

    assert kept_kinds(mi.STAGE_DURABLE, mi.LANE_FRONTDOOR) == {
        mi.KIND_RETRIEVAL_OVERLAY,
        mi.KIND_RUNTIME_NOTICE,
        mi.KIND_APPEND_NOTICE,
        mi.KIND_SYSTEM_RELAY_NOTICE,
    }
    assert kept_kinds(mi.STAGE_PROMPT_TAIL, mi.LANE_FRONTDOOR) == {
        mi.KIND_RETRIEVAL_MEMORY_SNAPSHOT,
        mi.KIND_RETRIEVAL_OVERLAY,
        mi.KIND_RUNTIME_NOTICE,
        mi.KIND_APPEND_NOTICE,
        mi.KIND_SYSTEM_RELAY_NOTICE,
    }
    assert kept_kinds(mi.STAGE_COMPACTION, mi.LANE_FRONTDOOR) == {
        mi.KIND_RETRIEVAL_OVERLAY,
        mi.KIND_TURN_ONLY_NOTE,
        mi.KIND_RUNTIME_NOTICE,
        mi.KIND_APPEND_NOTICE,
        mi.KIND_SYSTEM_RELAY_NOTICE,
    }
    assert kept_kinds(mi.STAGE_POST_COMPACTION, mi.LANE_FRONTDOOR) == {
        mi.KIND_RETRIEVAL_MEMORY_SNAPSHOT,
        mi.KIND_RETRIEVAL_OVERLAY,
        mi.KIND_LEGACY_RUNTIME_CONTRACT,
        mi.KIND_TURN_ONLY_NOTE,
        mi.KIND_APPEND_NOTICE,
        mi.KIND_SYSTEM_RELAY_NOTICE,
    }
    assert kept_kinds(mi.STAGE_PROJECTION, mi.LANE_FRONTDOOR) == {
        mi.KIND_RUNTIME_NOTICE,
        mi.KIND_APPEND_NOTICE,
        mi.KIND_SYSTEM_RELAY_NOTICE,
    }
    assert kept_kinds(mi.STAGE_PROMPT_TAIL, mi.LANE_NODE) == {
        mi.KIND_RETRIEVAL_MEMORY_SNAPSHOT,
        mi.KIND_RETRIEVAL_OVERLAY,
        mi.KIND_RUNTIME_NOTICE,
        mi.KIND_APPEND_NOTICE,
        mi.KIND_SYSTEM_RELAY_NOTICE,
    }


def test_keeps_record_matches_strip_for_projection() -> None:
    for item in [MEMORY, HINT, CONTRACT, TURN_ONLY, NOTICE, APPEND, RELAY, USER]:
        kept = mi.keeps_record_for_stage(item, stage=mi.STAGE_PROJECTION, lane=mi.LANE_FRONTDOOR)
        stripped = mi.strip_for_stage([item], stage=mi.STAGE_PROJECTION, lane=mi.LANE_FRONTDOOR)
        assert kept == (stripped != [])


def test_user_prose_mentioning_marker_is_not_stripped() -> None:
    prose = {"role": "user", "content": "我在日志里看到 [RUNTIME_NOTICE] 这一行，是什么意思？"}
    assert mi.classify_message(prose) is None
    assert mi.strip_for_stage(
        [prose], stage=mi.STAGE_DURABLE, lane=mi.LANE_FRONTDOOR
    ) == [prose]


def test_runtime_notice_family_carries_echo_guard() -> None:
    family = mi.family_for(mi.KIND_RUNTIME_NOTICE)
    assert family is not None
    assert family.echo_guard is not None


def test_frontdoor_durable_baseline_unchanged() -> None:
    fixture = [
        {"role": "system", "content": "system"},
        MEMORY,
        CONTRACT,
        TURN_ONLY,
        NOTICE,
        APPEND,
        RELAY,
        USER,
        ASSISTANT,
        TOOL,
    ]
    assert CeoFrontDoorRuntimeOps._durable_frontdoor_request_body_messages(fixture) == [
        {"role": "system", "content": "system"},
        NOTICE,
        APPEND,
        RELAY,
        USER,
        ASSISTANT,
        TOOL,
    ]
    assert [
        CeoFrontDoorRuntimeOps._frontdoor_adoption_projection_record_kept(item)
        for item in fixture
    ] == [True, False, False, False, True, True, True, True, True, True]
    assert CeoFrontDoorRuntimeOps._strip_frontdoor_dynamic_overlays(fixture) == [
        item for item in fixture if item != HINT
    ]


def test_frontdoor_prompt_tail_keeps_contract_after_overlay() -> None:
    live = [
        {"role": "system", "content": "system"},
        CONTRACT,
        TURN_ONLY,
        {"role": "user", "content": "current user"},
    ]
    appendix = [CONTRACT, {"role": "assistant", "content": "## overlay part"}]
    contract = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="test-model",
        stable_messages=live,
        dynamic_appendix_messages=appendix,
        tool_schemas=[],
        cache_family_revision=None,
        session_key="test-session",
        live_request_messages=live,
        overlay_text="",
    )
    assert contract.request_messages == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "current user"},
        {"role": "assistant", "content": "## overlay part"},
        CONTRACT,
    ]


def test_node_compaction_split_unchanged() -> None:
    parts = ReActToolLoop._split_request_messages_for_token_compaction(
        request_messages=[
            {"role": "system", "content": "node system"},
            {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
            dict(APPEND),
            dict(NODE_CONTRACT),
            dict(TURN_ONLY),
            dict(ASSISTANT),
            dict(TOOL),
        ],
        recent_tail_count=2,
    )
    assert len(parts["system_prefix"]) == 1
    assert len(parts["bootstrap_user"]) == 1
    assert parts["append_notice_tail"] == [APPEND]
    assert parts["trailing_note"] == []
    assert len(parts["compressible_history"]) == 2
    assert len(parts["recent_tail"]) == 2

    trailing = ReActToolLoop._split_request_messages_for_token_compaction(
        request_messages=[
            {"role": "system", "content": "node system"},
            {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
            dict(APPEND),
            dict(ASSISTANT),
            dict(TOOL),
            dict(TURN_ONLY),
        ],
        recent_tail_count=2,
    )
    assert trailing["trailing_note"] == [TURN_ONLY]


def test_insert_after_bootstrap_user_lands_behind_bootstrap() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1"}'},
        dict(ASSISTANT),
        dict(TOOL),
    ]
    inserted = mi.insert_mid_messages(
        messages,
        items=[dict(APPEND)],
        position=mi.POSITION_AFTER_BOOTSTRAP_USER,
        lane=mi.LANE_NODE,
    )
    assert inserted == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1"}'},
        APPEND,
        ASSISTANT,
        TOOL,
    ]


def test_insert_dedupes_same_identity_and_singleton_turn_only() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1"}'},
        dict(TURN_ONLY),
    ]
    inserted = mi.insert_mid_messages(
        messages,
        items=[dict(APPEND), dict(APPEND), dict(TURN_ONLY)],
        position=mi.POSITION_AFTER_BOOTSTRAP_USER,
        lane=mi.LANE_NODE,
    )
    assert inserted == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1"}'},
        APPEND,
        TURN_ONLY,
    ]


def test_insert_refuses_between_tool_call_and_results() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
        },
    ]
    try:
        mi.insert_mid_messages(
            messages,
            items=[dict(APPEND)],
            position=mi.POSITION_TAIL,
            lane=mi.LANE_NODE,
        )
    except ValueError as exc:
        assert "tool call" in str(exc)
    else:
        raise AssertionError("insert into a tool group must be refused")


def test_node_prepare_strips_contract_and_turn_only() -> None:
    loop = object.__new__(ReActToolLoop)
    loop._append_notice_tail_messages = lambda **kwargs: []
    prepared, _parts = loop._prepare_messages_with_parts(
        [
            {"role": "system", "content": "node system"},
            {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
            dict(NODE_CONTRACT),
            dict(TURN_ONLY),
            dict(ASSISTANT),
            dict(TOOL),
        ],
        runtime_context={},
    )
    assert prepared == [
        {"role": "system", "content": "node system"},
        {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
        ASSISTANT,
        TOOL,
    ]
