from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import g3ku.runtime.context.frontdoor_catalog_selection as selection_module
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


class _SplitPromptBuilder:
    def __init__(self) -> None:
        self.base_calls = 0
        self.skill_calls: list[list[str]] = []

    def build(self, *, skills: list) -> str:
        raise AssertionError("message builder should use split prompt builder methods")

    def build_base_prompt(self) -> str:
        self.base_calls += 1
        return "BASE PROMPT"

    def build_visible_skills_block(self, *, skills: list) -> str:
        ids = [
            str(getattr(item, "skill_id", "") or "").strip()
            for item in list(skills or [])
            if str(getattr(item, "skill_id", "") or "").strip()
        ]
        self.skill_calls.append(ids)
        if not ids:
            return ""
        lines = [
            "## Visible Skills For This Turn",
            "- Only the listed skills are available in this turn.",
        ]
        lines.extend(f'- `{skill_id}` available for this turn.' for skill_id in ids)
        return "\n".join(lines)


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
    assert "stateful_fact" in prompt
    assert "observed_at" in prompt
    assert "memory_delete" in prompt
    assert "memory_search" in prompt
    assert "Retrieved Context" in prompt
    assert "submit_next_stage" not in prompt


@pytest.mark.asyncio
async def test_message_builder_uses_dense_only_retrieval_scope_when_semantic_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _SemanticMemoryManager(
        response="remembered browser workflow",
        skill_record_ids=["skill:focused-skill", "skill:secondary-skill"],
        tool_record_ids=["tool:agent_browser", "tool:web_fetch"],
    )

    async def _invoke_model_rewrite(**kwargs) -> dict[str, str]:
        _ = kwargs
        return {
            "skill_query": "semantic focused skill query",
            "tool_query": "semantic focused tool query",
            "model": "frontdoor-query-rewriter",
        }

    monkeypatch.setattr(selection_module, "_invoke_frontdoor_catalog_rewrite_model", _invoke_model_rewrite)
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
    assert result.trace["semantic_frontdoor"]["queries"] == {
        "raw_query": "focused browser workflow",
        "skill_query": "semantic focused skill query",
        "tool_query": "semantic focused tool query",
        "status": "rewritten",
        "model": "frontdoor-query-rewriter",
    }
    assert result.trace["retrieval_scope"] == {
        "mode": "dense_only",
        "search_context_types": ["memory", "skill", "resource"],
        "allowed_context_types": ["memory", "skill", "resource"],
        "allowed_resource_record_ids": ["tool:agent_browser", "tool:web_fetch"],
        "allowed_skill_record_ids": ["skill:focused-skill", "skill:secondary-skill"],
    }


@pytest.mark.asyncio
async def test_message_builder_dense_unavailable_exposes_all_visible_skills_and_tools_even_when_top_k_is_one() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _SemanticMemoryManager(response="")
    memory_manager.store = SimpleNamespace(_dense_enabled=False)
    loop = SimpleNamespace(
        main_task_service=None,
        memory_manager=memory_manager,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                skill_inventory_top_k=1,
                extension_tool_top_k=1,
                core_tools=[],
            )
        ),
    )
    builder = CeoMessageBuilder(loop=loop, prompt_builder=prompt_builder)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="need every visible capability for fallback",
        exposure={
            "skills": [
                _skill("focused-skill", "Primary workflow"),
                _skill("secondary-skill", "Secondary workflow"),
            ],
            "tool_families": [
                _family("agent_browser", "Browser automation"),
                _family("web_fetch", "HTTP fetch helper"),
            ],
            "tool_names": ["filesystem", "agent_browser", "web_fetch"],
        },
        persisted_session=None,
    )

    assert prompt_builder.calls == [["focused-skill", "secondary-skill"]]
    assert result.tool_names == ["filesystem", "agent_browser", "web_fetch"]
    assert result.trace["semantic_frontdoor"]["mode"] == "visible_only"
    assert result.trace["retrieval_scope"]["mode"] == "visible_only"
    assert result.trace["retrieval_scope"]["allowed_skill_record_ids"] == [
        "skill:focused-skill",
        "skill:secondary-skill",
    ]
    assert result.trace["retrieval_scope"]["allowed_resource_record_ids"] == [
        "tool:agent_browser",
        "tool:web_fetch",
    ]


@pytest.mark.asyncio
async def test_message_builder_semantic_disabled_keeps_top_k_selection_and_non_visible_only_trace() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="")
    loop = SimpleNamespace(
        main_task_service=None,
        memory_manager=memory_manager,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                skill_inventory_top_k=1,
                extension_tool_top_k=1,
                core_tools=[],
            )
        ),
    )
    builder = CeoMessageBuilder(loop=loop, prompt_builder=prompt_builder)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="focused browser workflow",
        exposure={
            "skills": [
                _skill("focused-skill", "Primary workflow"),
                _skill("secondary-skill", "Secondary workflow"),
            ],
            "tool_families": [
                _family("agent_browser", "Browser automation"),
                _family("web_fetch", "HTTP fetch helper"),
            ],
            "tool_names": ["filesystem", "agent_browser", "web_fetch"],
        },
        persisted_session=None,
    )

    assert prompt_builder.calls == [["focused-skill"]]
    assert result.trace["semantic_frontdoor"]["mode"] == "disabled"
    assert result.trace["retrieval_scope"]["mode"] == "rbac_fallback"
    assert result.trace["selected_tools"].get("mode") != "visible_only"
    assert len(result.tool_names) == 1


@pytest.mark.asyncio
async def test_message_builder_dense_unavailable_retrieval_scope_includes_dict_visible_skill_ids() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _SemanticMemoryManager(response="")
    memory_manager.store = SimpleNamespace(_dense_enabled=False)
    loop = SimpleNamespace(
        main_task_service=None,
        memory_manager=memory_manager,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                skill_inventory_top_k=1,
                extension_tool_top_k=1,
                core_tools=[],
            )
        ),
    )
    builder = CeoMessageBuilder(loop=loop, prompt_builder=prompt_builder)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="need fallback scope",
        exposure={
            "skills": [
                {
                    "skill_id": "dict-skill",
                    "display_name": "Dict Skill",
                    "description": "Dict short summary. Dict details should not appear.",
                    "l0": "Dict short summary.",
                },
                _skill("object-skill", "Object workflow"),
            ],
            "tool_families": [_family("agent_browser", "Browser automation")],
            "tool_names": ["filesystem", "agent_browser"],
        },
        persisted_session=None,
    )

    assert result.trace["semantic_frontdoor"]["mode"] == "visible_only"
    assert result.trace["retrieval_scope"]["mode"] == "visible_only"
    assert result.trace["retrieval_scope"]["allowed_skill_record_ids"] == [
        "skill:dict-skill",
        "skill:object-skill",
    ]
    assert [item["skill_id"] for item in result.trace["selected_skills"]] == [
        "dict-skill",
        "object-skill",
    ]


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
async def test_message_builder_dense_unavailable_renders_l0_only_skill_and_external_tool_summaries() -> None:
    memory_manager = _SemanticMemoryManager(response="")
    memory_manager.store = SimpleNamespace(_dense_enabled=False)
    loop = SimpleNamespace(
        workspace=Path.cwd(),
        main_task_service=None,
        memory_manager=memory_manager,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                skill_inventory_top_k=1,
                extension_tool_top_k=1,
                core_tools=[],
            )
        ),
    )
    builder = CeoMessageBuilder(
        loop=loop,
        prompt_builder=CeoPromptBuilder(loop=SimpleNamespace(workspace=Path.cwd())),
    )

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="fallback summary mode",
        exposure={
            "skills": [
                {
                    "skill_id": "focused-skill",
                    "display_name": "Focused Skill",
                    "description": "L0 concise skill summary. Detail sentence should not appear.",
                    "l0": "L0 concise skill summary.",
                }
            ],
            "tool_families": [
                _family(
                    "external_docs",
                    "L0 external tool summary. Detail sentence should not appear.",
                    callable=False,
                    available=False,
                    install_dir="plugins/external_docs",
                    metadata={"l0": "L0 external tool summary."},
                )
            ],
            "tool_names": ["filesystem", "load_tool_context"],
        },
        persisted_session=None,
    )

    system_prompt = str(result.model_messages[0].get("content") or "")
    overlay = str(getattr(result, "turn_overlay_text", "") or "")
    assert "L0 concise skill summary." in overlay
    assert "L0 external tool summary." in system_prompt
    assert "Detail sentence should not appear." not in system_prompt
    assert "Detail sentence should not appear." not in overlay


@pytest.mark.asyncio
async def test_message_builder_dense_unavailable_renders_l0_style_for_object_skills() -> None:
    memory_manager = _SemanticMemoryManager(response="")
    memory_manager.store = SimpleNamespace(_dense_enabled=False)
    loop = SimpleNamespace(
        workspace=Path.cwd(),
        main_task_service=None,
        memory_manager=memory_manager,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                skill_inventory_top_k=1,
                extension_tool_top_k=1,
                core_tools=[],
            )
        ),
    )
    builder = CeoMessageBuilder(
        loop=loop,
        prompt_builder=CeoPromptBuilder(loop=SimpleNamespace(workspace=Path.cwd())),
    )

    object_skill = SimpleNamespace(
        skill_id="object-skill",
        display_name="Object Skill",
        description="Object skill short summary. Object detail sentence should not appear.",
    )

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="fallback summary mode object skill",
        exposure={
            "skills": [object_skill],
            "tool_families": [],
            "tool_names": ["filesystem", "load_tool_context"],
        },
        persisted_session=None,
    )

    overlay = str(getattr(result, "turn_overlay_text", "") or "")
    assert "Object skill short summary." in overlay
    assert "Object detail sentence should not appear." not in overlay


@pytest.mark.asyncio
async def test_message_builder_dense_unavailable_keeps_non_callable_external_tool_with_empty_install_dir() -> None:
    memory_manager = _SemanticMemoryManager(response="")
    memory_manager.store = SimpleNamespace(_dense_enabled=False)
    loop = SimpleNamespace(
        workspace=Path.cwd(),
        main_task_service=None,
        memory_manager=memory_manager,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                skill_inventory_top_k=1,
                extension_tool_top_k=1,
                core_tools=[],
            )
        ),
    )
    builder = CeoMessageBuilder(
        loop=loop,
        prompt_builder=CeoPromptBuilder(loop=SimpleNamespace(workspace=Path.cwd())),
    )

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="fallback summary mode external tool",
        exposure={
            "skills": [],
            "tool_families": [
                _family(
                    "external_docs",
                    "External docs short summary. Detail sentence should not appear.",
                    callable=False,
                    available=False,
                    install_dir="",
                    metadata={"l0": "External docs short summary."},
                )
            ],
            "tool_names": ["filesystem", "load_tool_context"],
        },
        persisted_session=None,
    )

    system_prompt = str(result.model_messages[0].get("content") or "")
    assert '`external_docs`' in system_prompt
    assert "External docs short summary." in system_prompt
    assert "Install dir not configured" in system_prompt
    assert [item["tool_id"] for item in result.trace["external_tools"]] == ["external_docs"]


@pytest.mark.asyncio
async def test_message_builder_dense_unavailable_renders_l0_only_for_unavailable_callable_tool_family() -> None:
    memory_manager = _SemanticMemoryManager(response="")
    memory_manager.store = SimpleNamespace(_dense_enabled=False)
    loop = SimpleNamespace(
        workspace=Path.cwd(),
        main_task_service=None,
        memory_manager=memory_manager,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                skill_inventory_top_k=1,
                extension_tool_top_k=1,
                core_tools=[],
            )
        ),
    )
    builder = CeoMessageBuilder(
        loop=loop,
        prompt_builder=CeoPromptBuilder(loop=SimpleNamespace(workspace=Path.cwd())),
    )

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="fallback summary mode unavailable callable tool",
        exposure={
            "skills": [],
            "tool_families": [
                _family(
                    "agent_browser",
                    "Callable fallback short summary. Later sentence should not appear.",
                    callable=True,
                    available=False,
                    metadata={"l0": "Callable fallback short summary. Later l0 sentence should not appear."},
                )
            ],
            "tool_names": ["filesystem", "load_tool_context"],
        },
        persisted_session=None,
    )

    system_prompt = str(result.model_messages[0].get("content") or "")
    assert "## Tool Resources That Require `load_tool_context`" in system_prompt
    assert '`agent_browser`' in system_prompt
    assert "Callable fallback short summary." in system_prompt
    assert "Later sentence should not appear." not in system_prompt
    assert "Later l0 sentence should not appear." not in system_prompt


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
async def test_message_builder_moves_turn_specific_context_into_overlay_for_stable_prefix() -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="authoritative preference")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)
    persisted_session = Session(key="web:shared")
    persisted_session.add_message("user", "prior question")
    persisted_session.add_message("assistant", "prior answer")

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="from now on default to the focused browser workflow",
        exposure={
            "skills": [_skill("focused-skill", "Primary workflow")],
            "tool_families": [_family("agent_browser", "Browser automation via semantic shortlist.")],
            "tool_names": ["filesystem", "agent_browser", "memory_write"],
        },
        persisted_session=persisted_session,
        user_content="from now on default to the focused browser workflow",
    )

    contents = [str(item.get("content") or "") for item in result.model_messages]
    rendered_messages = "\n\n".join(contents)
    overlay = str(getattr(result, "turn_overlay_text", "") or "")

    assert contents[0] == "BASE PROMPT"
    assert "## Retrieved Context" not in rendered_messages
    assert "Visible Skills For This Turn" not in rendered_messages
    assert "Long-Term Memory Write Hint" not in rendered_messages
    assert contents[-3:] == [
        "prior question",
        "prior answer",
        "from now on default to the focused browser workflow",
    ]
    assert "## Retrieved Context" in overlay
    assert "Visible Skills For This Turn" in overlay
    assert "Long-Term Memory Write Hint" in overlay
    assert prompt_builder.base_calls == 1
    assert prompt_builder.skill_calls == [["focused-skill"]]
    assert result.trace["turn_overlay_present"] is True
    assert result.trace["stable_prefix_message_count"] == len(result.model_messages)
    assert result.trace["turn_overlay_character_count"] == len(overlay)
    assert str(result.trace["turn_overlay_text_hash"] or "").strip()


@pytest.mark.asyncio
async def test_message_builder_exposes_dynamic_appendix_messages_for_prompt_cache_key_contract() -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="authoritative preference")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)
    persisted_session = Session(key="web:shared")
    persisted_session.add_message("user", "prior question")
    persisted_session.add_message("assistant", "prior answer")

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="from now on default to the focused browser workflow",
        exposure={
            "skills": [_skill("focused-skill", "Primary workflow")],
            "tool_families": [_family("agent_browser", "Browser automation via semantic shortlist.")],
            "tool_names": ["filesystem", "agent_browser", "memory_write"],
        },
        persisted_session=persisted_session,
        user_content="from now on default to the focused browser workflow",
    )

    stable_messages = list(getattr(result, "stable_messages"))
    dynamic_appendix_messages = list(getattr(result, "dynamic_appendix_messages"))
    stable_contents = [str(item.get("content") or "") for item in stable_messages]
    dynamic_contents = [str(item.get("content") or "") for item in dynamic_appendix_messages]

    assert stable_messages[0]["role"] == "system"
    assert "## Retrieved Context" not in str(stable_messages[0]["content"] or "")
    assert "prior question" in stable_contents
    assert "prior answer" in stable_contents
    assert "from now on default to the focused browser workflow" in stable_contents
    assert any(
        "## Retrieved Context" in str(item.get("content") or "")
        for item in dynamic_appendix_messages
    )
    assert "prior question" not in dynamic_contents
    assert "prior answer" not in dynamic_contents
    assert "from now on default to the focused browser workflow" not in dynamic_contents


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


@pytest.mark.asyncio
async def test_message_builder_falls_back_to_transcript_when_checkpoint_history_is_incomplete() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)
    persisted_session = Session(key="web:shared")
    persisted_session.add_message("user", "prior question")
    persisted_session.add_message("assistant", "prior answer")

    checkpoint_messages = [
        {"role": "system", "content": "OLD SYSTEM"},
        {"role": "user", "content": "prior question"},
    ]

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="next question",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=persisted_session,
        checkpoint_messages=checkpoint_messages,
        user_content="next question",
    )

    contents = [str(item.get("content") or "") for item in result.model_messages]
    assert "OLD SYSTEM" not in contents
    assert "prior question" in contents
    assert "prior answer" in contents
    assert contents.count("next question") == 1
    assert contents[-1] == "next question"
    assert result.trace["history_source"] == "transcript"


@pytest.mark.asyncio
async def test_message_builder_keeps_repeated_user_text_after_completed_turn() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    checkpoint_messages = [
        {"role": "system", "content": "OLD SYSTEM"},
        {"role": "user", "content": "repeat this"},
        {"role": "assistant", "content": "done"},
    ]

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="repeat this",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=None,
        checkpoint_messages=checkpoint_messages,
        user_content="repeat this",
    )

    contents = [str(item.get("content") or "") for item in result.model_messages]
    assert contents.count("repeat this") == 2
    assert contents[-2:] == ["done", "repeat this"]
    assert result.trace["current_user_in_history"] is False


@pytest.mark.asyncio
async def test_message_builder_does_not_dedupe_same_text_when_turn_ids_mismatch() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)
    persisted_session = Session(key="web:shared")
    persisted_session.add_message(
        "user",
        "repeat this",
        metadata={"_transcript_turn_id": "old-turn", "_transcript_state": "complete"},
    )
    persisted_session.add_message("assistant", "done")

    checkpoint_messages = [
        {"role": "system", "content": "OLD SYSTEM"},
        {
            "role": "user",
            "content": "repeat this",
            "metadata": {"_transcript_turn_id": "old-turn"},
        },
    ]

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="repeat this",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=persisted_session,
        checkpoint_messages=checkpoint_messages,
        user_content="repeat this",
        user_metadata={"_transcript_turn_id": "new-turn"},
    )

    contents = [str(item.get("content") or "") for item in result.model_messages]
    assert "OLD SYSTEM" not in contents
    assert contents.count("repeat this") == 2
    assert contents[-2:] == ["done", "repeat this"]
    assert result.trace["current_user_in_checkpoint"] is False
    assert result.trace["history_source"] == "transcript"


@pytest.mark.asyncio
async def test_message_builder_collects_retrieved_context_separately_from_history_injection() -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="durable preference")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="remembered preference",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=None,
        checkpoint_messages=[
            {"role": "user", "content": "prior user"},
            {"role": "assistant", "content": "prior answer"},
        ],
        user_content="remembered preference",
    )

    assert result.trace["context_collection"] == {
        "retrieved_record_count": 1,
        "retrieval_scope_mode": "rbac_fallback",
        "retrieved_context_present": True,
    }
    assert result.trace["message_injection"] == {
        "history_source": "checkpoint",
        "history_message_count": 2,
        "current_user_appended": True,
        "retrieved_context_in_model_messages": False,
    }
    assert "## Retrieved Context" in str(result.turn_overlay_text or "")
    assert result.model_messages[-3:] == [
        {"role": "user", "content": "prior user"},
        {"role": "assistant", "content": "prior answer"},
        {"role": "user", "content": "remembered preference"},
    ]


@pytest.mark.asyncio
async def test_message_builder_ignores_same_session_turn_memory_records_for_ceo_history_recall() -> None:
    memory_manager = _MemoryManager(response="")

    async def _retrieve_context_bundle(**kwargs):
        memory_manager.calls.append(dict(kwargs))
        return RetrievedContextBundle(
            query=str(kwargs.get("query") or ""),
            records=[
                {
                    "record_id": "turn-memory-1",
                    "context_type": "memory",
                    "l0": "turn memory",
                    "l1": "turn memory snippet",
                    "l2_preview": "",
                    "source": "turn",
                    "session_key": "web:shared",
                    "channel": "web",
                    "chat_id": "shared",
                    "confidence": 1.0,
                }
            ],
        )

    memory_manager.retrieve_context_bundle = _retrieve_context_bundle  # type: ignore[method-assign]
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=_SplitPromptBuilder())

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="follow up question",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=None,
        checkpoint_messages=[
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
        ],
        user_content="follow up question",
    )

    assert memory_manager.calls[0]["exclude_same_session_turn_memory"] is True
    overlay = str(result.turn_overlay_text or "")
    assert "turn memory snippet" not in overlay
    assert result.trace["same_session_turn_memory_filtered_count"] == 1
    assert result.trace["retrieved_record_count"] == 0


@pytest.mark.asyncio
async def test_message_builder_keeps_turn_memory_from_different_session_in_shared_chat() -> None:
    memory_manager = _MemoryManager(response="")

    async def _retrieve_context_bundle(**kwargs):
        memory_manager.calls.append(dict(kwargs))
        return RetrievedContextBundle(
            query=str(kwargs.get("query") or ""),
            records=[
                {
                    "record_id": "turn-memory-other-session",
                    "context_type": "memory",
                    "l0": "other session turn memory",
                    "l1": "other session snippet",
                    "l2_preview": "",
                    "source": "turn",
                    "session_key": "web:other",
                    "channel": "web",
                    "chat_id": "shared",
                    "confidence": 1.0,
                }
            ],
        )

    memory_manager.retrieve_context_bundle = _retrieve_context_bundle  # type: ignore[method-assign]
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=_SplitPromptBuilder())

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="follow up question",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=None,
        user_content="follow up question",
    )

    overlay = str(result.turn_overlay_text or "")
    assert "other session snippet" in overlay
    assert result.trace["same_session_turn_memory_filtered_count"] == 0
    assert result.trace["retrieved_record_count"] == 1
