from __future__ import annotations


def _field(value, name: str):
    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


def test_build_heartbeat_lane_keeps_artifact_refs_out_of_retrieval_query_in_heartbeat_lane() -> None:
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="openai:gpt-4.1",
        stable_rules_text="Keep the user informed without exposing internal mechanics.",
        task_ledger_summary="task:521ee9055b4a failed after provider retries.",
        events=[
            {
                "reason": "task_terminal",
                "task_id": "task:521ee9055b4a",
                "status": "failed",
                "brief_text": "provider chain exhausted",
                "terminal_failure_reason": "provider chain exhausted",
                "terminal_output_ref": "artifact:artifact:task-terminal-521ee9055b4a",
            }
        ],
    )

    dynamic_appendix_messages = list(_field(lane, "dynamic_appendix_messages"))
    request_messages = list(_field(lane, "request_messages"))
    retrieval_query = str(_field(lane, "retrieval_query") or "")
    matching_dynamic_user_messages = [
        message
        for message in dynamic_appendix_messages
        if str(message.get("role") or "").strip().lower() == "user"
        and "task:521ee9055b4a" in str(message.get("content") or "")
        and "provider chain exhausted" in str(message.get("content") or "")
    ]

    assert _field(lane, "scope") == "ceo_heartbeat"
    assert "artifact:artifact:task-terminal-521ee9055b4a" not in retrieval_query
    assert "task_terminal" in retrieval_query
    assert "failed" in retrieval_query
    assert "task:521ee9055b4a" in retrieval_query
    assert "provider chain exhausted" in retrieval_query
    assert matching_dynamic_user_messages
    assert any(message in request_messages for message in matching_dynamic_user_messages)


def test_build_heartbeat_lane_reuses_stable_prefix_when_only_event_payload_changes() -> None:
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    base_kwargs = {
        "provider_model": "openai:gpt-4.1",
        "stable_rules_text": "Keep the user informed without exposing internal mechanics.",
        "task_ledger_summary": "task:demo-1 was already dispatched and is still running.",
    }
    first = build_heartbeat_prompt_lane(
        **base_kwargs,
        events=[
            {
                "reason": "tool_background",
                "tool_name": "skill-installer",
                "execution_id": "tool-exec:1",
                "status": "background_running",
                "elapsed_seconds": 30.0,
                "recommended_wait_seconds": 45.0,
                "runtime_snapshot": {"summary_text": "still fetching remote repository"},
            }
        ],
    )
    second = build_heartbeat_prompt_lane(
        **base_kwargs,
        events=[
            {
                "reason": "tool_background",
                "tool_name": "skill-installer",
                "execution_id": "tool-exec:1",
                "status": "background_running",
                "elapsed_seconds": 90.0,
                "recommended_wait_seconds": 45.0,
                "runtime_snapshot": {"summary_text": "now installing dependencies"},
            }
        ],
    )

    assert _field(first, "scope") == "ceo_heartbeat"
    assert _field(second, "scope") == "ceo_heartbeat"
    assert list(_field(first, "stable_messages")) == list(_field(second, "stable_messages"))
    assert list(_field(first, "dynamic_appendix_messages")) != list(_field(second, "dynamic_appendix_messages"))
    assert list(_field(first, "request_messages")) != list(_field(second, "request_messages"))
    assert str(_field(first, "retrieval_query") or "") != str(_field(second, "retrieval_query") or "")
