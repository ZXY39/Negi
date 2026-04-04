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


@pytest.mark.asyncio
async def test_summarize_frontdoor_history_model_success_preserves_leading_prefix_messages() -> None:
    messages = [
        {"role": "system", "content": "system guardrail"},
        {"role": "assistant", "content": "## Retrieved Context\nsource: CEO notes"},
        {"role": "user", "content": "message 0"},
        {"role": "assistant", "content": "message 1"},
        {"role": "user", "content": "message 2"},
        {"role": "assistant", "content": "message 3"},
        {"role": "user", "content": "message 4"},
        {"role": "assistant", "content": "message 5"},
    ]

    async def _fake_model(prompt: dict[str, object]) -> dict[str, object]:
        prompt_messages = prompt["messages"]
        assert isinstance(prompt_messages, list)
        assert [item["content"] for item in prompt_messages] == ["message 0", "message 1", "message 2", "message 3"]
        return {
            "stable_preferences": ["reply in Chinese"],
            "narrative": "Preserve the frontdoor context.",
        }

    result = await summarize_frontdoor_history(
        messages=messages,
        previous_summary_text="",
        previous_summary_payload={},
        keep_message_count=2,
        trigger_message_count=4,
        model_key="summary-model",
        model_invoke=_fake_model,
    )

    assert [item["content"] for item in result.messages[:2]] == [
        "system guardrail",
        "## Retrieved Context\nsource: CEO notes",
    ]
    assert "Preserve the frontdoor context." in result.messages[2]["content"]
    assert [item["content"] for item in result.messages[-2:]] == ["message 4", "message 5"]


@pytest.mark.asyncio
async def test_summarize_frontdoor_history_prefix_retention_matches_fallback_path() -> None:
    messages = [
        {"role": "system", "content": "system guardrail"},
        {"role": "assistant", "content": "## Retrieved Context\nsource: CEO notes"},
        {"role": "user", "content": "message 0"},
        {"role": "assistant", "content": "message 1"},
        {"role": "user", "content": "message 2"},
        {"role": "assistant", "content": "message 3"},
        {"role": "user", "content": "message 4"},
        {"role": "assistant", "content": "message 5"},
    ]
    heuristic_messages = compact_frontdoor_history(
        messages,
        recent_message_count=2,
        summary_trigger_message_count=4,
    )

    async def _boom(prompt: dict[str, object]) -> dict[str, object]:
        _ = prompt
        raise RuntimeError("summary model unavailable")

    async def _fake_model(prompt: dict[str, object]) -> dict[str, object]:
        _ = prompt
        return {
            "stable_preferences": ["reply in Chinese"],
            "narrative": "Preserve the frontdoor context.",
        }

    success_result = await summarize_frontdoor_history(
        messages=messages,
        previous_summary_text="",
        previous_summary_payload={},
        keep_message_count=2,
        trigger_message_count=4,
        model_key="summary-model",
        model_invoke=_fake_model,
    )
    fallback_result = await summarize_frontdoor_history(
        messages=messages,
        previous_summary_text="",
        previous_summary_payload={},
        keep_message_count=2,
        trigger_message_count=4,
        model_key="summary-model",
        model_invoke=_boom,
    )

    assert [item["content"] for item in success_result.messages[:2]] == [
        item["content"] for item in heuristic_messages[:2]
    ]
    assert [item["content"] for item in fallback_result.messages[:2]] == [
        item["content"] for item in heuristic_messages[:2]
    ]
    assert [item["content"] for item in success_result.messages[-2:]] == [
        item["content"] for item in heuristic_messages[-2:]
    ]
    assert [item["content"] for item in fallback_result.messages[-2:]] == [
        item["content"] for item in heuristic_messages[-2:]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_output",
    [
        {"stable_preferences": "reply in Chinese"},
        ["not", "a", "dict"],
    ],
)
async def test_summarize_frontdoor_history_malformed_model_output_falls_back_heuristically(
    model_output: object,
) -> None:
    messages = [{"role": "user", "content": f"message {idx}"} for idx in range(10)]
    compacted = compact_frontdoor_history(
        messages,
        recent_message_count=4,
        summary_trigger_message_count=6,
    )
    heuristic_state = frontdoor_summary_state(compacted)

    async def _bad_model(prompt: dict[str, object]) -> object:
        _ = prompt
        return model_output

    result = await summarize_frontdoor_history(
        messages=messages,
        previous_summary_text="",
        previous_summary_payload={},
        keep_message_count=4,
        trigger_message_count=6,
        model_key="summary-model",
        model_invoke=_bad_model,
    )

    assert result.summary_payload["fallback"] == "heuristic"
    assert [item["content"] for item in result.messages[-4:]] == [f"message {idx}" for idx in range(6, 10)]
    assert result.summary_model_key == ""
    assert result.summary_version == int(heuristic_state["summary_version"])


@pytest.mark.asyncio
async def test_summarize_frontdoor_history_model_summary_preserves_effective_compacted_count() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "## Existing Summary [frontdoor-history-summary]",
            "metadata": {
                "frontdoor_history_summary": True,
                "summary_version": 2,
                "summary_model_key": "existing-model",
                "compacted_message_count": 7,
            },
        },
        {"role": "user", "content": "message 1"},
        {"role": "assistant", "content": "message 2"},
        {"role": "user", "content": "message 3"},
        {"role": "assistant", "content": "message 4"},
        {"role": "user", "content": "message 5"},
        {"role": "assistant", "content": "message 6"},
        {"role": "user", "content": "message 7"},
    ]

    async def _fake_model(prompt: dict[str, object]) -> dict[str, object]:
        _ = prompt
        return {
            "stable_preferences": ["reply in Chinese"],
            "narrative": "Preserve existing compacted counts.",
        }

    result = await summarize_frontdoor_history(
        messages=messages,
        previous_summary_text="",
        previous_summary_payload={},
        keep_message_count=2,
        trigger_message_count=4,
        model_key="summary-model",
        model_invoke=_fake_model,
    )

    summary_message = result.messages[0]
    assert summary_message["metadata"]["compacted_message_count"] == 12
