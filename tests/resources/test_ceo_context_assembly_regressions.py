from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.runtime.context.types import RetrievedContextBundle
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.prompt_builder import CeoPromptBuilder
from g3ku.session.manager import Session


class _PromptBuilder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def build(self, *, skills: list) -> str:
        ids = [
            str(getattr(item, "skill_id", "") or "").strip()
            for item in list(skills or [])
            if str(getattr(item, "skill_id", "") or "").strip()
        ]
        self.calls.append(ids)
        return "BASE PROMPT"


class _MemoryManager:
    def __init__(self, response: str = "") -> None:
        self.calls: list[dict[str, object]] = []
        self.response = response

    async def sync_catalog(self, service, *, skill_ids=None, tool_ids=None):
        _ = service, skill_ids, tool_ids
        return {"created": 0, "updated": 0, "removed": 0}

    async def retrieve_context_bundle(self, **kwargs):
        self.calls.append(dict(kwargs))
        records = []
        if self.response:
            records.append(
                {
                    "record_id": "memory-1",
                    "context_type": "memory",
                    "l0": "remembered fact",
                    "l1": self.response,
                    "l2_preview": "",
                    "source": "test",
                    "confidence": 1.0,
                }
            )
        return RetrievedContextBundle(
            query=str(kwargs.get("query") or ""),
            records=records,
        )


class _SemanticMemoryManager(_MemoryManager):
    def __init__(
        self,
        *,
        response: str = "",
        skill_record_ids: list[str] | None = None,
        tool_record_ids: list[str] | None = None,
    ) -> None:
        super().__init__(response=response)
        self.skill_record_ids = list(skill_record_ids or [])
        self.tool_record_ids = list(tool_record_ids or [])
        self.semantic_calls: list[dict[str, object]] = []
        self.store = SimpleNamespace(_dense_enabled=True)

    async def semantic_search_context_records(
        self,
        *,
        namespace_prefix=None,
        query: str,
        limit: int = 8,
        context_type: str | None = None,
    ):
        self.semantic_calls.append(
            {
                "namespace_prefix": namespace_prefix,
                "query": query,
                "limit": limit,
                "context_type": context_type,
            }
        )
        record_ids = self.skill_record_ids if context_type == "skill" else self.tool_record_ids
        return [SimpleNamespace(record_id=record_id) for record_id in record_ids[: max(limit, 0)]]


def _loop(memory_manager: _MemoryManager) -> SimpleNamespace:
    return SimpleNamespace(
        main_task_service=None,
        memory_manager=memory_manager,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                skill_inventory_top_k=8,
                extension_tool_top_k=6,
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


def _skill(skill_id: str, description: str) -> SimpleNamespace:
    return SimpleNamespace(skill_id=skill_id, display_name=skill_id, description=description)


def _family(
    tool_id: str,
    description: str,
    *,
    callable: bool = True,
    available: bool = True,
    install_dir: str = "",
    metadata: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        tool_id=tool_id,
        display_name=tool_id,
        description=description,
        callable=callable,
        available=available,
        install_dir=install_dir,
        metadata=dict(metadata or {}),
        actions=[SimpleNamespace(executor_names=[tool_id])],
    )


def test_ceo_prompt_builder_keeps_memory_guidance() -> None:
    builder = CeoPromptBuilder(loop=SimpleNamespace(workspace=Path.cwd()))
    prompt = builder.build(skills=[])
    assert "memory_write" in prompt
    assert "Retrieved Context" in prompt
    assert "submit_next_stage" not in prompt


@pytest.mark.asyncio
async def test_message_builder_uses_dense_only_retrieval_scope_when_semantic_available() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _SemanticMemoryManager(
        response="remembered browser workflow",
        skill_record_ids=["skill:focused-skill", "skill:secondary-skill"],
        tool_record_ids=["tool:agent_browser", "tool:web_fetch"],
    )
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="focused browser workflow",
        exposure={
            "skills": [
                _skill("focused-skill", "Primary workflow"),
                _skill("secondary-skill", "Secondary workflow"),
            ],
            "tool_families": [
                _family("agent_browser", "Browser automation via semantic shortlist."),
                _family("web_fetch", "HTTP fetch helper."),
            ],
            "tool_names": ["filesystem", "agent_browser", "web_fetch"],
        },
        persisted_session=None,
    )

    assert prompt_builder.calls == [["focused-skill", "secondary-skill"]]
    assert result.trace["retrieval_scope"] == {
        "mode": "dense_only",
        "search_context_types": ["memory", "skill", "resource"],
        "allowed_context_types": ["memory", "skill", "resource"],
        "allowed_resource_record_ids": ["tool:agent_browser", "tool:web_fetch"],
        "allowed_skill_record_ids": ["skill:focused-skill", "skill:secondary-skill"],
    }


@pytest.mark.asyncio
async def test_message_builder_keeps_control_tools_visible_in_tool_selection() -> None:
    builder = CeoMessageBuilder(
        loop=SimpleNamespace(main_task_service=None, memory_manager=None, _memory_runtime_settings=SimpleNamespace(assembly=SimpleNamespace(core_tools=[]))),
        prompt_builder=_PromptBuilder(),
    )

    selected, trace = builder._select_tools(
        query_text="install a skill and maybe stop it if needed",
        visible_names=[
            "create_async_task",
            "skill-installer",
            "stop_tool_execution",
            "wait_tool_execution",
        ],
        visible_families=[],
        core_tools={"create_async_task"},
        extension_top_k=1,
    )

    assert "stop_tool_execution" in selected
    assert "wait_tool_execution" in selected
    assert trace["reserved"] == ["stop_tool_execution", "wait_tool_execution"]


@pytest.mark.asyncio
async def test_message_builder_renders_external_tool_context_for_unavailable_callable_tools() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="please fix the browser automation tool before using it",
        exposure={
            "skills": [],
            "tool_families": [
                _family(
                    "agent_browser",
                    "Browser automation via the upstream CLI.",
                    callable=True,
                    available=False,
                    metadata={"warnings": ["missing required bins"]},
                ),
            ],
            "tool_names": ["filesystem", "load_tool_context"],
        },
        persisted_session=None,
    )

    assert 'load_tool_context(tool_id="agent_browser")' in result.system_prompt
    assert "missing required bins" in result.system_prompt
    assert [item["tool_id"] for item in result.trace["external_tools"]] == ["agent_browser"]


@pytest.mark.asyncio
async def test_message_builder_uses_memory_only_retrieval_for_memory_intent() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _SemanticMemoryManager(
        response="remembered browser workflow",
        skill_record_ids=["skill:focused-skill"],
        tool_record_ids=["tool:agent_browser"],
    )
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="from now on default to the focused browser workflow",
        exposure={
            "skills": [_skill("focused-skill", "Primary workflow")],
            "tool_families": [_family("agent_browser", "Browser automation via semantic shortlist.")],
            "tool_names": ["filesystem", "agent_browser", "memory_write"],
        },
        persisted_session=None,
    )

    assert memory_manager.calls[0]["search_context_types"] == ["memory"]
    assert memory_manager.calls[0]["allowed_context_types"] == ["memory"]
    assert result.trace["memory_write_hint"]["triggered"] is True


@pytest.mark.asyncio
async def test_message_builder_includes_retrieval_and_full_transcript_without_duplicate_current_user() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="authoritative preference")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)
    persisted_session = Session(key="web:shared")
    persisted_session.add_message("user", "first question")
    persisted_session.add_message("assistant", "first answer")
    persisted_session.add_message(
        "user",
        "follow up question",
        metadata={"_transcript_turn_id": "turn-2", "_transcript_state": "pending"},
    )

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="follow up question",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=persisted_session,
        user_content="follow up question",
        user_metadata={"_transcript_turn_id": "turn-2"},
    )

    contents = [str(item.get("content") or "") for item in result.model_messages]
    assert contents[0] == "BASE PROMPT"
    assert "## Retrieved Context" in contents[1]
    assert contents.count("follow up question") == 1
    assert contents[-1] == "follow up question"
    assert result.trace["current_user_in_transcript"] is True
    rendered = "\n\n".join(contents)
    assert "Task Continuity" not in rendered
    assert "Stage Context" not in rendered
    assert "Archive Overview" not in rendered
    assert "live_raw_tail" not in rendered


@pytest.mark.asyncio
async def test_message_builder_prefers_checkpoint_history_over_transcript_once_available() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)
    persisted_session = Session(key="web:shared")
    persisted_session.add_message("user", "bootstrap transcript question")
    persisted_session.add_message("assistant", "bootstrap transcript answer")
    persisted_session.add_message(
        "user",
        "latest question from transcript",
        metadata={"_transcript_turn_id": "turn-2", "_transcript_state": "pending"},
    )

    checkpoint_messages = [
        {"role": "system", "content": "OLD SYSTEM"},
        {"role": "assistant", "content": "## Retrieved Context\n- stale memory"},
        {"role": "user", "content": "checkpoint question"},
        {"role": "assistant", "content": "checkpoint answer"},
        {"role": "user", "content": "follow up question"},
    ]

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="follow up question",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=persisted_session,
        checkpoint_messages=checkpoint_messages,
        user_content="follow up question",
        user_metadata={"_transcript_turn_id": "turn-2"},
    )

    contents = [str(item.get("content") or "") for item in result.model_messages]
    assert contents[0] == "BASE PROMPT"
    assert "OLD SYSTEM" not in contents
    assert "## Retrieved Context\n- stale memory" not in contents
    assert "checkpoint question" in contents
    assert "checkpoint answer" in contents
    assert "bootstrap transcript question" not in contents
    assert "bootstrap transcript answer" not in contents
    assert contents.count("follow up question") == 1
    assert contents[-1] == "follow up question"
    assert result.trace["current_user_in_transcript"] is True
