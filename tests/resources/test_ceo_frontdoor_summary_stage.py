import pytest

from types import SimpleNamespace

from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor.history_compaction import (
    FRONTDOOR_HISTORY_SUMMARY_MARKER,
    compact_frontdoor_history,
    is_frontdoor_history_summary_message,
)


def _stage_messages(count: int) -> list[dict[str, object]]:
    return [{"role": "user", "content": f"message {idx}"} for idx in range(count)]


def _build_no_config_runner() -> CreateAgentCeoFrontDoorRunner:
    return CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())


@pytest.mark.asyncio
async def test_compact_history_defaults_to_stage_budget() -> None:
    messages = _stage_messages(22)

    compacted = compact_frontdoor_history(
        messages,
        recent_message_count=20,
        summary_trigger_message_count=10,
    )

    assert len(compacted) == 21
    assert is_frontdoor_history_summary_message(compacted[0])
    assert FRONTDOOR_HISTORY_SUMMARY_MARKER in str(compacted[0].get("content") or "")


@pytest.mark.asyncio
async def test_compact_history_between_trigger_and_keep() -> None:
    messages = _stage_messages(15)

    compacted = compact_frontdoor_history(
        messages,
        recent_message_count=20,
        summary_trigger_message_count=10,
    )

    assert is_frontdoor_history_summary_message(compacted[0])


@pytest.mark.asyncio
async def test_summarize_messages_default_no_config() -> None:
    runner = _build_no_config_runner()
    runner._invoke_summary_model = None

    messages = _stage_messages(22)

    result = await runner._summarize_messages(messages=messages, state={})

    assert len(result["messages"]) == 21
    summary_meta = dict(result["messages"][0].get("metadata") or {})
    assert summary_meta.get("frontdoor_history_summary") is True
    assert FRONTDOOR_HISTORY_SUMMARY_MARKER in str(result["summary_text"])


@pytest.mark.asyncio
async def test_compact_history_does_not_isolate_current_user_turn() -> None:
    messages = [
        {"role": "assistant", "content": "answer 0"},
        {"role": "user", "content": "message 1"},
    ]

    compacted = compact_frontdoor_history(
        messages,
        recent_message_count=20,
        summary_trigger_message_count=1,
    )

    assert compacted == messages


@pytest.mark.asyncio
async def test_compact_history_keeps_trailing_tool_tail_intact() -> None:
    messages = [
        {"role": "user", "content": "message 0"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "name": "filesystem", "arguments": {"path": "."}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "directory listing"},
    ]

    compacted = compact_frontdoor_history(
        messages,
        recent_message_count=1,
        summary_trigger_message_count=1,
    )

    assert is_frontdoor_history_summary_message(compacted[0])
    assert compacted[-2:] == messages[-2:]
