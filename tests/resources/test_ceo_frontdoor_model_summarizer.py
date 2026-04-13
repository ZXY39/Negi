from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner


def test_obsolete_frontdoor_summary_modules_are_removed() -> None:
    summarizer_module = ".".join(["g3ku", "runtime", "frontdoor", "ceo" + "_summarizer"])
    compaction_module = ".".join(["g3ku", "runtime", "frontdoor", "history" + "_compaction"])

    assert importlib.util.find_spec(summarizer_module) is None
    assert importlib.util.find_spec(compaction_module) is None


@pytest.mark.asyncio
async def test_frontdoor_runtime_no_longer_exposes_legacy_history_summarizer_hook() -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    assert not hasattr(runner, "_summarize_messages")


def test_frontdoor_no_longer_uses_summary_text_overlay() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    context = runner.build_prompt_context(
        state={
            "summary_text": "obsolete summary overlay",
            "turn_overlay_text": "stage overlay",
            "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
        },
        runtime=None,
        tools=[],
    )

    assert "summary_text" not in context
    assert "obsolete summary overlay" not in context["system_overlay"]
    assert "stage overlay" in context["system_overlay"]
