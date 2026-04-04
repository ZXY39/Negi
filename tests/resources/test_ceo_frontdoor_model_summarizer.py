import pytest

from g3ku.runtime.frontdoor.ceo_summarizer import summarize_frontdoor_history
from g3ku.runtime.frontdoor.history_compaction import compact_frontdoor_history, frontdoor_summary_state


@pytest.mark.asyncio
async def test_summarize_frontdoor_history_returns_structured_payload_and_preserves_tail() -> None:
    messages = [{"role": "user", "content": f"message {idx}"} for idx in range(10)]

    async def _fake_model(prompt: dict[str, object]) -> dict[str, object]:
        _ = prompt
        return {
            "stable_preferences": ["reply in Chinese"],
            "stable_facts": ["project root is D:/NewProjects/G3KU"],
            "open_loops": ["finish migration"],
            "recent_actions": ["reviewed ceo frontdoor"],
            "narrative": "The user asked to migrate the CEO runtime and keep behavior compatible.",
        }

    result = await summarize_frontdoor_history(
        messages=messages,
        previous_summary_text="",
        previous_summary_payload={},
        keep_message_count=4,
        trigger_message_count=6,
        model_key="summary-model",
        model_invoke=_fake_model,
    )

    assert result.summary_model_key == "summary-model"
    assert result.summary_payload["stable_preferences"] == ["reply in Chinese"]
    assert "The user asked to migrate the CEO runtime" in result.summary_text
    assert [item["content"] for item in result.messages[-4:]] == [f"message {idx}" for idx in range(6, 10)]


@pytest.mark.asyncio
async def test_summarize_frontdoor_history_falls_back_to_heuristic_compaction() -> None:
    messages = [{"role": "user", "content": f"message {idx}"} for idx in range(10)]
    compacted = compact_frontdoor_history(
        messages,
        recent_message_count=4,
        summary_trigger_message_count=6,
    )
    heuristic_state = frontdoor_summary_state(compacted)

    async def _boom(prompt: dict[str, object]) -> dict[str, object]:
        _ = prompt
        raise RuntimeError("summary model unavailable")

    result = await summarize_frontdoor_history(
        messages=messages,
        previous_summary_text="",
        previous_summary_payload={},
        keep_message_count=4,
        trigger_message_count=6,
        model_key="summary-model",
        model_invoke=_boom,
    )

    assert result.summary_payload["fallback"] == "heuristic"
    assert "frontdoor-history-summary" in str(result.summary_text)
    assert [item["content"] for item in result.messages[-4:]] == [f"message {idx}" for idx in range(6, 10)]
    assert result.summary_model_key == ""
    assert result.summary_version == int(heuristic_state["summary_version"])


@pytest.mark.asyncio
async def test_summarize_frontdoor_history_preserves_existing_summary_model_key_without_new_compaction() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "## Existing Summary [frontdoor-history-summary]",
            "metadata": {
                "frontdoor_history_summary": True,
                "summary_version": 2,
                "summary_model_key": "existing-model",
            },
        },
        {"role": "user", "content": "message 1"},
        {"role": "assistant", "content": "message 2"},
    ]

    async def _unused_model(prompt: dict[str, object]) -> dict[str, object]:
        _ = prompt
        return {"narrative": "should not be used"}

    result = await summarize_frontdoor_history(
        messages=messages,
        previous_summary_text="",
        previous_summary_payload={},
        keep_message_count=4,
        trigger_message_count=6,
        model_key="new-model",
        model_invoke=_unused_model,
    )

    assert result.summary_text == "## Existing Summary [frontdoor-history-summary]"
    assert result.summary_version == 2
    assert result.summary_model_key == "existing-model"
