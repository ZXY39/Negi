import pytest

from g3ku.runtime.frontdoor.ceo_summarizer import summarize_frontdoor_history


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
