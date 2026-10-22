from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.runtime.context.types import RetrievedContextBundle
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder


class _PromptBuilder:
    def build(self, *, skills: list) -> str:
        _ = skills
        return "BASE PROMPT"


class _MemoryManager:
    async def sync_catalog(self, service, *, skill_ids=None, tool_ids=None):
        _ = service, skill_ids, tool_ids
        return {"created": 0, "updated": 0, "removed": 0}

    async def retrieve_context_bundle(self, **kwargs):
        return RetrievedContextBundle(query=str(kwargs.get("query") or ""), records=[])


def _loop() -> SimpleNamespace:
    return SimpleNamespace(
        main_task_service=None,
        memory_manager=_MemoryManager(),
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                core_tools=[],
            )
        ),
    )


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
    )


@pytest.mark.asyncio
async def test_build_for_ceo_appends_ephemeral_tail_messages_only_to_model_messages() -> None:
    builder = CeoMessageBuilder(loop=_loop(), prompt_builder=_PromptBuilder())

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="check the running exec tool",
        exposure={
            "skills": [],
            "tool_families": [],
            "tool_names": ["stop_tool_execution"],
        },
        persisted_session=None,
        user_content="check the running exec tool",
        ephemeral_tail_messages=[
            {"role": "assistant", "content": "Ephemeral reminder header"},
            {"role": "user", "content": "The exec tool has already been reminded 2 times."},
        ],
    )

    assert [item["content"] for item in result.model_messages[-2:]] == [
        "Ephemeral reminder header",
        "The exec tool has already been reminded 2 times.",
    ]
    assert all(item.get("content") != "Ephemeral reminder header" for item in result.stable_messages)
    assert all(item.get("content") != "The exec tool has already been reminded 2 times." for item in result.stable_messages)
    assert all(item.get("content") != "Ephemeral reminder header" for item in result.dynamic_appendix_messages)


def test_builder_no_longer_injects_a_memory_block_into_the_head() -> None:
    """基座由通知车道的尾部消息承载；builder 不再产出 `## 长期记忆` 头部位。"""
    assert not hasattr(CeoMessageBuilder, "_memory_snapshot_stable_message")
