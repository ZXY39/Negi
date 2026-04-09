from __future__ import annotations


def _field(value, name: str):
    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


def test_build_heartbeat_prompt_lane_keeps_artifact_refs_out_of_retrieval_query_in_heartbeat_lane() -> None:
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
