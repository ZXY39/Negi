from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import convert_to_messages

import g3ku.runtime.context.frontdoor_catalog_selection as selection_module
import g3ku.runtime.frontdoor.message_builder as message_builder_module
import g3ku.runtime.web_ceo_sessions as web_ceo_sessions
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.context.types import RetrievedContextBundle
from g3ku.runtime.frontdoor.capability_snapshot import build_capability_snapshot
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.prompt_builder import CeoPromptBuilder
from g3ku.runtime.frontdoor.tool_contract import (
    build_frontdoor_tool_contract,
    is_frontdoor_tool_contract_message,
    upsert_frontdoor_tool_contract_message,
)
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
            "## 本轮可见技能",
            "- 只有列出的技能在本轮可见。",
        ]
        lines.extend(f'- `{skill_id}` 在本轮可用。' for skill_id in ids)
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


def _tool_resource_record(
    tool_id: str,
    description: str,
    *,
    callable: bool = True,
    available: bool = True,
    install_dir: str = "",
    metadata: dict[str, object] | None = None,
    executor_names: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        tool_id=tool_id,
        display_name=tool_id,
        description=description,
        callable=callable,
        available=available,
        install_dir=install_dir,
        metadata=dict(metadata or {}),
        actions=[SimpleNamespace(executor_names=list(executor_names or [tool_id]))],
    )


def _stage_history_with_global_zone() -> tuple[list[dict[str, object]], dict[str, object]]:
    messages = [
        {"role": "user", "content": "bootstrap request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-1",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-1", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage one raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-2",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-2", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage two raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-3",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-3", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage three raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-4",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-4", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage four raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-5",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-5", "content": '{"ok": true}'},
        {"role": "assistant", "content": "active stage raw detail"},
    ]
    stage_state = {
        "active_stage_id": "frontdoor-stage-5",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_kind": "normal",
                "system_generated": False,
                "mode": "自主执行",
                "status": "completed",
                "stage_goal": "inspect stage one",
                "completed_stage_summary": "finished stage one",
                "key_refs": [],
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
            },
            {
                "stage_id": "frontdoor-stage-2",
                "stage_index": 2,
                "stage_kind": "normal",
                "system_generated": False,
                "mode": "自主执行",
                "status": "completed",
                "stage_goal": "inspect stage two",
                "completed_stage_summary": "finished stage two",
                "key_refs": [],
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
            },
            {
                "stage_id": "frontdoor-stage-3",
                "stage_index": 3,
                "stage_kind": "normal",
                "system_generated": False,
                "mode": "自主执行",
                "status": "completed",
                "stage_goal": "inspect stage three",
                "completed_stage_summary": "finished stage three",
                "key_refs": [],
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
            },
            {
                "stage_id": "frontdoor-stage-4",
                "stage_index": 4,
                "stage_kind": "normal",
                "system_generated": False,
                "mode": "自主执行",
                "status": "completed",
                "stage_goal": "inspect stage four",
                "completed_stage_summary": "finished stage four",
                "key_refs": [],
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
            },
            {
                "stage_id": "frontdoor-stage-5",
                "stage_index": 5,
                "stage_kind": "normal",
                "system_generated": False,
                "mode": "自主执行",
                "status": "active",
                "stage_goal": "inspect stage five",
                "completed_stage_summary": "",
                "key_refs": [],
                "tool_round_budget": 2,
                "tool_rounds_used": 0,
            },
        ],
    }
    return messages, stage_state


def test_ceo_prompt_builder_keeps_memory_guidance() -> None:
    builder = CeoPromptBuilder(loop=SimpleNamespace(workspace=Path.cwd()))
    prompt = builder.build(skills=[])
    assert "memory_write" in prompt
    assert "memory_delete" in prompt
    assert "memory_note" in prompt
    assert "MEMORY.md" in prompt
    assert "memory_search" not in prompt
    assert "已检索上下文" in prompt
    assert "submit_next_stage" in prompt


def test_capability_snapshot_exposure_revision_ignores_hidden_executor_names() -> None:
    first = build_capability_snapshot(
        visible_skills=[],
        visible_families=[
            _tool_resource_record(
                "agent_browser",
                "Browser automation via semantic shortlist.",
                executor_names=["agent_browser"],
            )
        ],
        visible_tool_names=["agent_browser"],
    )
    second = build_capability_snapshot(
        visible_skills=[],
        visible_families=[
            _tool_resource_record(
                "agent_browser",
                "Browser automation via semantic shortlist.",
                executor_names=["agent_browser", "hidden_browser_admin"],
            )
        ],
        visible_tool_names=["agent_browser"],
    )

    assert first.exposure_revision == second.exposure_revision
    assert first.stable_catalog_message == second.stable_catalog_message
    assert first.visible_tool_ids == ("agent_browser",)


def test_capability_snapshot_exposure_revision_ignores_warning_and_install_dir_churn() -> None:
    first = build_capability_snapshot(
        visible_skills=[],
        visible_families=[
            _tool_resource_record(
                "external_docs",
                "External docs short summary.",
                callable=False,
                available=False,
                install_dir="plugins/external_docs_v1",
                metadata={"warnings": ["missing index"], "l0": "External docs short summary."},
            )
        ],
        visible_tool_names=["load_tool_context"],
    )
    second = build_capability_snapshot(
        visible_skills=[],
        visible_families=[
            _tool_resource_record(
                "external_docs",
                "External docs short summary.",
                callable=False,
                available=False,
                install_dir="plugins/external_docs_v2",
                metadata={"warnings": ["refresh required"], "l0": "External docs short summary."},
            )
        ],
        visible_tool_names=["load_tool_context"],
    )

    assert first.exposure_revision == second.exposure_revision
    assert first.stable_catalog_message == second.stable_catalog_message


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
    monkeypatch.setattr(selection_module, "_frontdoor_query_rewrite_enabled", lambda: True)
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
                _tool_resource_record("agent_browser", "Browser automation via semantic shortlist."),
                _tool_resource_record("web_fetch", "HTTP fetch helper."),
            ],
            "tool_names": ["filesystem", "agent_browser", "web_fetch"],
        },
        persisted_session=None,
    )

    assert prompt_builder.calls == [[]]
    assert [item["skill_id"] for item in result.trace["selected_skills"]] == [
        "focused-skill",
        "secondary-skill",
    ]
    assert result.trace["semantic_frontdoor"]["queries"] == {
        "raw_query": "focused browser workflow",
        "skill_query": "semantic focused skill query",
        "tool_query": "semantic focused tool query",
        "status": "rewritten",
        "model": "frontdoor-query-rewriter",
    }
    assert result.trace["retrieval_scope"] == {
        "mode": "disabled",
        "search_context_types": [],
        "allowed_context_types": [],
        "allowed_resource_record_ids": [],
        "allowed_skill_record_ids": [],
    }
    overlay = str(result.turn_overlay_text or "")
    assert "## 本轮候选工具" not in overlay
    assert "candidate_tools = [" not in overlay
    assert result.tool_names == []


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
                _tool_resource_record("agent_browser", "Browser automation"),
                _tool_resource_record("web_fetch", "HTTP fetch helper"),
            ],
            "tool_names": ["filesystem", "agent_browser", "web_fetch"],
        },
        persisted_session=None,
    )

    assert prompt_builder.calls == [[]]
    assert [item["skill_id"] for item in result.trace["selected_skills"]] == [
        "focused-skill",
        "secondary-skill",
    ]
    assert result.tool_names == []
    assert result.candidate_tool_names == ["filesystem", "agent_browser", "web_fetch"]
    assert result.trace["semantic_frontdoor"]["mode"] == "visible_only"
    assert result.trace["retrieval_scope"]["mode"] == "disabled"
    assert result.trace["retrieval_scope"]["allowed_skill_record_ids"] == []
    assert result.trace["retrieval_scope"]["allowed_resource_record_ids"] == []


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
                _tool_resource_record("agent_browser", "Browser automation"),
                _tool_resource_record("web_fetch", "HTTP fetch helper"),
            ],
            "tool_names": ["filesystem", "agent_browser", "web_fetch"],
        },
        persisted_session=None,
    )

    assert prompt_builder.calls == [[]]
    assert [item["skill_id"] for item in result.trace["selected_skills"]] == ["focused-skill"]
    assert result.trace["semantic_frontdoor"]["mode"] == "disabled"
    assert result.trace["retrieval_scope"]["mode"] == "disabled"
    assert result.trace["tool_selection"].get("mode") != "visible_only"
    assert result.tool_names == []
    assert len(result.candidate_tool_names) == 1


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
            "tool_families": [_tool_resource_record("agent_browser", "Browser automation")],
            "tool_names": ["filesystem", "agent_browser"],
        },
        persisted_session=None,
    )

    assert result.trace["semantic_frontdoor"]["mode"] == "visible_only"
    assert result.trace["retrieval_scope"]["mode"] == "disabled"
    assert result.trace["retrieval_scope"]["allowed_skill_record_ids"] == []
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
        ],
        visible_families=[],
        core_tools={"create_async_task"},
        extension_top_k=1,
    )

    assert "stop_tool_execution" in selected
    assert "wait_tool_execution" not in selected
    assert trace["reserved_internal_tool_names"] == ["stop_tool_execution"]


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
                _tool_resource_record(
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
    assert "L0 concise skill summary." not in overlay
    assert '`external_docs`' not in system_prompt
    assert "## 本轮候选工具" not in overlay
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
    system_prompt = str(result.model_messages[0].get("content") or "")
    assert "Object skill short summary." not in overlay
    assert "Object skill short summary." not in system_prompt
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
                _tool_resource_record(
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
    assert '`external_docs`' not in system_prompt
    assert "Install dir not configured" not in system_prompt
    assert [item["tool_id"] for item in result.trace["external_tools"]] == ["external_docs"]


@pytest.mark.asyncio
async def test_message_builder_dense_unavailable_renders_l0_only_for_unavailable_callable_tool_resource() -> None:
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
                _tool_resource_record(
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
    assert "## Capability Exposure Snapshot" in system_prompt
    assert "## Visible Callable Tools" not in system_prompt
    assert "## Visible Skills" not in system_prompt
    assert "## Visible Tool Context Ids" not in system_prompt
    assert '`agent_browser`' not in system_prompt
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
                _tool_resource_record(
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

    assert "## Capability Exposure Snapshot" in result.system_prompt
    assert "## Visible Callable Tools" not in result.system_prompt
    assert "## Visible Skills" not in result.system_prompt
    assert "## Visible Tool Context Ids" not in result.system_prompt
    assert '`agent_browser`' not in result.system_prompt
    assert "missing required bins" not in result.system_prompt
    assert [item["tool_id"] for item in result.trace["external_tools"]] == ["agent_browser"]


@pytest.mark.asyncio
async def test_message_builder_uses_memory_write_hint_without_memory_retrieval_overlay() -> None:
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
            "tool_families": [_tool_resource_record("agent_browser", "Browser automation via semantic shortlist.")],
            "tool_names": ["filesystem", "agent_browser", "memory_write"],
        },
        persisted_session=None,
    )

    assert memory_manager.calls == []
    assert result.trace["memory_write_hint"]["triggered"] is True


@pytest.mark.asyncio
async def test_message_builder_polls_main_runtime_for_external_resource_changes_before_catalog_sync() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="")
    refresh_calls: list[str] = []

    class _MainService:
        async def maybe_refresh_external_resource_changes(self, *, session_id: str):
            refresh_calls.append(session_id)
            return {"refreshed": True, "catalog_synced": True}

    loop = _loop(memory_manager)
    loop.main_task_service = _MainService()
    builder = CeoMessageBuilder(loop=loop, prompt_builder=prompt_builder)

    await builder.build_for_ceo(
        session=_session(),
        query_text="browser workflow",
        exposure={
            "skills": [_skill("focused-skill", "Primary workflow")],
            "tool_families": [_tool_resource_record("agent_browser", "Browser automation via semantic shortlist.")],
            "tool_names": ["filesystem", "agent_browser"],
        },
        persisted_session=None,
    )

    assert refresh_calls == ["web:shared"]


@pytest.mark.asyncio
async def test_message_builder_keeps_callable_and_candidate_tools_separate() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="browser workflow",
        exposure={
            "skills": [],
            "tool_families": [
                _tool_resource_record("agent_browser", "Browser automation via semantic shortlist."),
                _tool_resource_record("web_fetch", "HTTP fetch helper."),
            ],
            "tool_names": ["load_tool_context", "agent_browser", "web_fetch"],
        },
        persisted_session=None,
    )

    assert result.tool_names == ["load_tool_context"]
    assert result.candidate_tool_names == ["agent_browser", "web_fetch"]


@pytest.mark.asyncio
async def test_message_builder_renders_candidate_tools_as_structured_tool_id_and_description_items() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="")

    class _MainService:
        def get_tool_toolskill(self, tool_id: str) -> dict[str, object]:
            payloads = {
                "filesystem_write": {"description": "Write file content to disk."},
                "filesystem_edit": {"description": "Edit one file in text-replace or line-range mode."},
            }
            return dict(payloads.get(str(tool_id or "").strip(), {}))

    loop = _loop(memory_manager)
    loop.main_task_service = _MainService()
    builder = CeoMessageBuilder(loop=loop, prompt_builder=prompt_builder)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="update the file contents",
        exposure={
            "skills": [],
            "tool_families": [
                _tool_resource_record(
                    "filesystem",
                    "Write, edit, copy, move, delete, and propose patches for local files.",
                    executor_names=["filesystem_write", "filesystem_edit"],
                ),
            ],
            "tool_names": ["load_tool_context", "filesystem_write", "filesystem_edit"],
        },
        persisted_session=None,
    )

    overlay = str(result.turn_overlay_text or "")
    assert "candidate_tools = [" not in overlay

    contract_messages = [
        item
        for item in list(result.dynamic_appendix_messages or [])
        if isinstance(item, dict)
        and str(item.get("role") or "").strip().lower() == "user"
        and isinstance(item.get("content"), str)
        and str(item.get("content") or "").strip()
    ]

    assert len(contract_messages) == 1
    payload = json.loads(contract_messages[0]["content"])
    assert payload["candidate_tools"] == [
        {
            "tool_id": "filesystem_edit",
            "description": "Edit one file in text-replace or line-range mode.",
        },
        {
            "tool_id": "filesystem_write",
            "description": "Write file content to disk.",
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query_text", "expected_tool_id"),
    [
        ("create a new markdown file for the summary", "filesystem_write"),
        ("append a new line to the existing file", "filesystem_edit"),
    ],
)
async def test_message_builder_prefers_specific_filesystem_candidates_for_write_intents(
    query_text: str,
    expected_tool_id: str,
) -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text=query_text,
        exposure={
            "skills": [],
            "tool_families": [
                _tool_resource_record(
                    "filesystem",
                    "Write, edit, copy, move, delete, and propose patches for local files.",
                    executor_names=[
                        "filesystem_write",
                        "filesystem_edit",
                        "filesystem_delete",
                    ],
                ),
            ],
            "tool_names": [
                "load_tool_context",
                "exec",
                "filesystem_write",
                "filesystem_edit",
                "filesystem_delete",
            ],
        },
        persisted_session=None,
    )

    assert result.tool_names == ["load_tool_context", "exec"]
    assert result.candidate_tool_names[0] == expected_tool_id


@pytest.mark.asyncio
async def test_message_builder_defaults_to_16_skill_and_tool_candidates_when_assembly_top_k_fields_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="")
    loop = SimpleNamespace(
        main_task_service=None,
        memory_manager=memory_manager,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                core_tools=[],
            )
        ),
    )
    builder = CeoMessageBuilder(loop=loop, prompt_builder=prompt_builder)

    async def _semantic_rankings(**kwargs):
        _ = kwargs
        return {
            "mode": "dense_only",
            "available": True,
            "skill_ids": [f"skill-{index:02d}" for index in range(20)],
            "tool_ids": [f"tool-{index:02d}" for index in range(20)],
            "trace": {},
        }

    monkeypatch.setattr(message_builder_module, "semantic_catalog_rankings", _semantic_rankings)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="rank the most relevant concrete tools and skills",
        exposure={
            "skills": [
                _skill(f"skill-{index:02d}", f"skill description {index:02d}")
                for index in range(20)
            ],
            "tool_families": [
                _tool_resource_record(f"tool-{index:02d}", f"tool description {index:02d}")
                for index in range(20)
            ],
            "tool_names": [f"tool-{index:02d}" for index in range(20)],
        },
        persisted_session=None,
    )

    assert len(result.trace["selected_skills"]) == 16
    assert len(result.candidate_tool_names) == 16
    assert result.trace["selected_skills"][0]["skill_id"] == "skill-00"
    assert result.candidate_tool_names[0] == "tool-00"


@pytest.mark.asyncio
async def test_message_builder_excludes_fixed_builtin_resource_tools_from_semantic_tool_selection_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)
    captured: dict[str, object] = {}

    async def _semantic_rankings(**kwargs):
        captured.update(kwargs)
        return {
            "mode": "dense_only",
            "available": True,
            "skill_ids": [],
            "tool_ids": ["agent_browser"],
            "trace": {},
        }

    monkeypatch.setattr(message_builder_module, "semantic_catalog_rankings", _semantic_rankings)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="pick the best tool for browser work",
        exposure={
            "skills": [],
            "tool_families": [
                _tool_resource_record("exec", "Read-only shell helper."),
                _tool_resource_record("memory", "Write, delete, and load long-term memory notes.", executor_names=["memory_write", "memory_delete", "memory_note"]),
                _tool_resource_record("agent_browser", "Browser automation via semantic shortlist."),
            ],
            "tool_names": [
                "load_tool_context",
                "exec",
                "memory_write",
                "memory_delete",
                "memory_note",
                "agent_browser",
            ],
        },
        persisted_session=None,
    )

    semantic_visible_families = list(captured.get("visible_families") or [])
    semantic_tool_ids = [str(getattr(item, "tool_id", "") or item.get("tool_id") or "").strip() for item in semantic_visible_families]
    semantic_executor_names = []
    for item in semantic_visible_families:
        actions = list(getattr(item, "actions", None) or item.get("actions") or [])
        for action in actions:
            names = list(getattr(action, "executor_names", None) or action.get("executor_names") or [])
            semantic_executor_names.extend(str(name or "").strip() for name in names if str(name or "").strip())

    assert semantic_tool_ids == ["memory", "agent_browser"]
    assert semantic_executor_names == ["agent_browser"]
    assert result.candidate_tool_names[0] == "agent_browser"
    assert "exec" not in result.candidate_tool_names
    assert "memory_write" not in result.candidate_tool_names
    assert "memory_delete" not in result.candidate_tool_names
    assert "memory_note" not in result.candidate_tool_names


@pytest.mark.asyncio
async def test_message_builder_promotes_hydrated_tools_into_callable_list() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    async def _semantic_rankings(**kwargs):
        _ = kwargs
        return {"mode": "unavailable", "available": False, "trace": {}}

    original = message_builder_module.semantic_catalog_rankings
    message_builder_module.semantic_catalog_rankings = _semantic_rankings
    try:
        result = await builder.build_for_ceo(
            session=_session(),
            query_text="write the skill manifest",
            exposure={
                "skills": [],
                "tool_families": [
                    _tool_resource_record("filesystem_write", "Write a file with full content."),
                    _tool_resource_record("agent_browser", "Browser automation via semantic shortlist."),
                ],
                "tool_names": ["load_tool_context", "filesystem_write", "agent_browser"],
            },
            persisted_session=None,
            hydrated_tool_names=["filesystem_write"],
        )
    finally:
        message_builder_module.semantic_catalog_rankings = original

    assert result.tool_names == ["load_tool_context", "filesystem_write"]
    assert result.candidate_tool_names == ["agent_browser"]


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
            "tool_families": [_tool_resource_record("agent_browser", "Browser automation via semantic shortlist.")],
            "tool_names": ["filesystem", "agent_browser", "memory_write"],
        },
        persisted_session=persisted_session,
        user_content="from now on default to the focused browser workflow",
    )

    contents = [str(item.get("content") or "") for item in result.model_messages]
    rendered_messages = "\n\n".join(contents)
    overlay = str(getattr(result, "turn_overlay_text", "") or "")

    assert contents[0].startswith("BASE PROMPT")
    assert "## Capability Exposure Snapshot" in contents[0]
    assert "## 已检索上下文" not in rendered_messages
    assert "## 本轮最相关的技能" not in rendered_messages
    assert "## 本轮可见技能" not in rendered_messages
    assert "## 长期记忆写入提示" not in rendered_messages
    assert contents[-3:] == [
        "prior question",
        "prior answer",
        "from now on default to the focused browser workflow",
    ]
    assert "## 已检索上下文" not in overlay
    assert "## 长期记忆写入提示" in overlay
    assert "## 本轮最相关的技能" not in overlay
    assert "## 本轮可见技能" not in overlay
    assert "## 本轮候选工具" not in overlay
    assert prompt_builder.base_calls == 1
    assert prompt_builder.skill_calls == []
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
            "tool_families": [_tool_resource_record("agent_browser", "Browser automation via semantic shortlist.")],
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
    assert "## 已检索上下文" not in str(stable_messages[0]["content"] or "")
    assert "prior question" in stable_contents
    assert "prior answer" in stable_contents
    assert "from now on default to the focused browser workflow" in stable_contents
    assert any(
        "## 长期记忆写入提示" in str(item.get("content") or "")
        for item in dynamic_appendix_messages
    )
    assert any(
        "frontdoor_runtime_tool_contract" in str(item.get("content") or "")
        for item in dynamic_appendix_messages
    )
    assert "prior question" not in dynamic_contents
    assert "prior answer" not in dynamic_contents
    assert "from now on default to the focused browser workflow" not in dynamic_contents


@pytest.mark.asyncio
async def test_message_builder_appends_frontdoor_runtime_tool_contract_to_dynamic_appendix() -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="write the file with filesystem_write",
        exposure={
            "skills": [_skill("focused-skill", "Primary workflow")],
            "tool_families": [
                _tool_resource_record("filesystem_write", "Write file content."),
                _tool_resource_record("exec", "Read-only shell helper."),
            ],
            "tool_names": ["exec", "filesystem_write", "load_tool_context"],
        },
        persisted_session=None,
        hydrated_tool_names=["filesystem_write"],
    )

    contract_messages = [
        item
        for item in list(result.dynamic_appendix_messages or [])
        if isinstance(item, dict)
        and str(item.get("role") or "").strip().lower() == "user"
        and isinstance(item.get("content"), str)
        and str(item.get("content") or "").strip()
    ]

    assert len(contract_messages) == 1
    payload = json.loads(contract_messages[0]["content"])
    assert "filesystem_write" in payload["callable_tool_names"]
    assert all(
        str(item.get("tool_id") or "").strip() != "filesystem_write"
        for item in list(payload.get("candidate_tools") or [])
        if isinstance(item, dict)
    )
    assert payload["hydrated_tool_names"] == ["filesystem_write"]
    assert "visible_skill_ids" not in payload
    assert "rbac_visible_tool_names" not in payload
    assert "rbac_visible_skill_ids" not in payload


@pytest.mark.asyncio
async def test_message_builder_appends_frontdoor_runtime_tool_contract_with_exec_runtime_policy() -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="")
    loop = _loop(memory_manager)
    loop.main_task_service = SimpleNamespace(
        _current_exec_runtime_policy_payload=lambda: {
            'mode': 'full_access',
            'guardrails_enabled': False,
            'summary': 'exec will execute shell commands without exec-side guardrails.',
        }
    )
    builder = CeoMessageBuilder(loop=loop, prompt_builder=prompt_builder)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="inspect the repo with exec",
        exposure={
            "skills": [],
            "tool_families": [
                _tool_resource_record("exec", "Shell execution helper."),
            ],
            "tool_names": ["exec", "load_tool_context"],
        },
        persisted_session=None,
        hydrated_tool_names=["exec"],
    )

    contract_messages = [
        item
        for item in list(result.dynamic_appendix_messages or [])
        if isinstance(item, dict)
        and str(item.get("role") or "").strip().lower() == "user"
        and isinstance(item.get("content"), str)
        and str(item.get("content") or "").strip()
    ]

    assert len(contract_messages) == 1
    payload = json.loads(contract_messages[0]["content"])
    assert payload["exec_runtime_policy"] == {
        'mode': 'full_access',
        'guardrails_enabled': False,
        'summary': 'exec will execute shell commands without exec-side guardrails.',
    }
    assert "visible_skill_ids" not in payload
    assert "rbac_visible_tool_names" not in payload
    assert "rbac_visible_skill_ids" not in payload


def test_frontdoor_dynamic_appendix_records_prefer_state_tool_contract_over_stale_message() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    records = runner._dynamic_appendix_message_records_for_state(
        state={
            "tool_names": ["submit_next_stage", "filesystem_write"],
            "candidate_tool_names": [],
            "candidate_tool_items": [],
            "hydrated_tool_names": ["filesystem_write"],
            "visible_skill_ids": ["memory"],
            "candidate_skill_ids": ["memory"],
            "rbac_visible_tool_names": ["submit_next_stage", "filesystem_write"],
            "rbac_visible_skill_ids": ["memory"],
            "frontdoor_stage_state": {
                "active_stage_id": "stage:1",
                "transition_required": False,
                "stages": [
                    {"stage_id": "stage:1", "stage_goal": "write the file", "status": "active"},
                ],
            },
            "dynamic_appendix_messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message_type": "frontdoor_runtime_tool_contract",
                            "callable_tool_names": ["exec"],
                            "candidate_tool_names": ["filesystem_write"],
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
        }
    )

    contract_messages = [
        item
        for item in records
        if str(item.get("role") or "").strip().lower() == "user"
        and isinstance(item.get("content"), str)
        and str(item.get("content") or "").strip()
    ]

    assert len(contract_messages) == 1
    payload = json.loads(contract_messages[0]["content"])
    assert payload["callable_tool_names"] == ["submit_next_stage", "filesystem_write"]
    assert payload["candidate_tools"] == []
    assert payload["hydrated_tool_names"] == ["filesystem_write"]
    assert payload["candidate_skill_ids"] == ["memory"]
    assert "visible_skill_ids" not in payload
    assert "rbac_visible_tool_names" not in payload
    assert "rbac_visible_skill_ids" not in payload


def test_frontdoor_dynamic_appendix_records_require_canonical_tool_state_fields() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    with pytest.raises(RuntimeError, match="运行时工具合同"):
        runner._dynamic_appendix_message_records_for_state(
            state={
                "tool_names": ["submit_next_stage"],
                "candidate_tool_names": [],
                "candidate_tool_items": [],
                "frontdoor_stage_state": {
                    "active_stage_id": "stage:1",
                    "transition_required": False,
                    "stages": [{"stage_id": "stage:1", "stage_goal": "write the file", "status": "active"}],
                },
            }
        )


@pytest.mark.parametrize(
    ("query_text", "expected_terms"),
    [
        ("请记住，今后都默认用中文回复。", {"请记住", "今后", "默认"}),
        ("以后不要再自动改写我的提示词。", {"以后", "不要再"}),
        ("今后别再自动创建新任务。", {"今后", "别再"}),
        ("这个目录结构长期按这个来。", {"长期按这个来"}),
    ],
)
def test_message_builder_detects_chinese_memory_write_phrases(
    query_text: str,
    expected_terms: set[str],
) -> None:
    matched = set(CeoMessageBuilder._detect_memory_write_intent(query_text))

    assert expected_terms.issubset(matched)


def test_frontdoor_tool_contract_upsert_accepts_legacy_dict_and_writes_json_string() -> None:
    contract = build_frontdoor_tool_contract(
        callable_tool_names=["submit_next_stage", "filesystem_write"],
        candidate_tool_names=["agent_browser"],
        candidate_tool_items=[{"tool_id": "agent_browser", "description": "Browser automation"}],
        hydrated_tool_names=["filesystem_write"],
        frontdoor_stage_state={
            "active_stage_id": "stage:1",
            "transition_required": False,
            "stages": [{"stage_id": "stage:1", "status": "active", "stage_goal": "write"}],
        },
        visible_skill_ids=["memory"],
        candidate_skill_ids=["memory"],
        rbac_visible_tool_names=["submit_next_stage", "filesystem_write", "agent_browser"],
        rbac_visible_skill_ids=["memory"],
        contract_revision="frontdoor:v1",
    )
    legacy_message = {
        "role": "user",
        "content": {
            "message_type": "frontdoor_runtime_tool_contract",
            "callable_tool_names": ["exec"],
            "candidate_tool_names": ["filesystem_write"],
        },
    }

    assert is_frontdoor_tool_contract_message(legacy_message)
    assert is_frontdoor_tool_contract_message(contract.to_message())

    updated = upsert_frontdoor_tool_contract_message([legacy_message], contract)
    assert len(updated) == 1
    assert is_frontdoor_tool_contract_message(updated[0])
    assert isinstance(updated[0]["content"], str)
    payload = json.loads(updated[0]["content"])
    assert payload["callable_tool_names"] == ["submit_next_stage", "filesystem_write"]
    assert payload["candidate_tools"] == [
        {
            "tool_id": "agent_browser",
            "description": "Browser automation",
        }
    ]
    assert payload["hydrated_tool_names"] == ["filesystem_write"]
    assert payload["candidate_skill_ids"] == ["memory"]
    assert "visible_skill_ids" not in payload
    assert "rbac_visible_tool_names" not in payload
    assert "rbac_visible_skill_ids" not in payload


@pytest.mark.asyncio
async def test_message_builder_keeps_capability_snapshot_stable_when_semantic_skill_selection_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _SemanticMemoryManager(response="")
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
    exposure = {
        "skills": [
            _skill("focused-skill", "Focused workflow summary."),
            _skill("secondary-skill", "Secondary workflow summary."),
        ],
        "tool_families": [
            _tool_resource_record("agent_browser", "Browser automation via semantic shortlist."),
            _tool_resource_record("web_fetch", "HTTP fetch helper."),
        ],
        "tool_names": ["filesystem", "agent_browser", "web_fetch"],
    }

    async def _semantic_rankings(*, query_text: str, **kwargs) -> dict[str, object]:
        _ = kwargs
        if query_text == "focused browser workflow":
            return {
                "mode": "dense_only",
                "available": True,
                "skill_ids": ["focused-skill", "secondary-skill"],
                "tool_ids": ["agent_browser", "web_fetch"],
                "trace": {"queries": {"raw_query": query_text}},
            }
        return {
            "mode": "dense_only",
            "available": True,
            "skill_ids": ["secondary-skill", "focused-skill"],
            "tool_ids": ["web_fetch", "agent_browser"],
            "trace": {"queries": {"raw_query": query_text}},
        }

    monkeypatch.setattr("g3ku.runtime.frontdoor.message_builder.semantic_catalog_rankings", _semantic_rankings)

    first = await builder.build_for_ceo(
        session=_session(),
        query_text="focused browser workflow",
        exposure=exposure,
        persisted_session=None,
        user_content="same user turn",
    )
    second = await builder.build_for_ceo(
        session=_session(),
        query_text="fetch workflow details",
        exposure=exposure,
        persisted_session=None,
        user_content="same user turn",
    )

    assert first.trace["selected_skills"] != second.trace["selected_skills"]
    assert first.turn_overlay_text == ""
    assert second.turn_overlay_text == ""
    assert first.dynamic_appendix_messages != second.dynamic_appendix_messages
    assert first.stable_messages[0] == second.stable_messages[0]
    assert first.trace["capability_snapshot"] == second.trace["capability_snapshot"]
    assert first.cache_family_revision == second.cache_family_revision
    assert "## Capability Exposure Snapshot" in str(first.stable_messages[0]["content"] or "")
    assert "focused-skill" not in str(first.turn_overlay_text or "")
    assert "secondary-skill" not in str(second.turn_overlay_text or "")


@pytest.mark.asyncio
async def test_message_builder_uses_exposure_revision_when_capability_snapshot_exists() -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)
    snapshot = build_capability_snapshot(
        visible_skills=[
            _skill("focused-skill", "Focused workflow summary."),
            _skill("secondary-skill", "Secondary workflow summary."),
        ],
        visible_families=[
            _tool_resource_record("agent_browser", "Browser automation via semantic shortlist."),
            _tool_resource_record("web_fetch", "HTTP fetch helper."),
        ],
        visible_tool_names=["filesystem", "agent_browser", "web_fetch"],
    )

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="focused browser workflow",
        exposure={
            "skills": [
                _skill("focused-skill", "Focused workflow summary."),
                _skill("secondary-skill", "Secondary workflow summary."),
            ],
            "tool_families": [
                _tool_resource_record("agent_browser", "Browser automation via semantic shortlist."),
                _tool_resource_record("web_fetch", "HTTP fetch helper."),
            ],
            "tool_names": ["filesystem", "agent_browser", "web_fetch"],
            "capability_snapshot": snapshot,
        },
        persisted_session=None,
    )

    assert result.cache_family_revision == snapshot.exposure_revision
    stable_system = str(result.stable_messages[0]["content"] or "")
    assert "## Capability Exposure Snapshot" in stable_system
    assert snapshot.exposure_revision in stable_system
    assert "## Visible Callable Tools" not in stable_system
    assert "## Visible Skills" not in stable_system
    assert "## Visible Tool Context Ids" not in stable_system


def test_context_assembly_result_dynamic_appendix_model_messages_stays_combined_compatibility_view() -> None:
    from g3ku.runtime.context.types import ContextAssemblyResult

    result = ContextAssemblyResult(
        stable_messages=[
            {"role": "system", "content": "BASE PROMPT"},
            {"role": "user", "content": "prior question"},
        ],
        dynamic_appendix_messages=[
            {"role": "assistant", "content": "## 已检索上下文\n- memory"},
        ],
        tool_names=["filesystem"],
        trace={},
    )

    assert result.model_messages == [
        {"role": "system", "content": "BASE PROMPT"},
        {"role": "user", "content": "prior question"},
        {"role": "assistant", "content": "## 已检索上下文\n- memory"},
    ]


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
    assert contents[0].startswith("BASE PROMPT")
    assert "## Capability Exposure Snapshot" in contents[0]
    assert "## 本轮候选工具" not in "\n\n".join(contents)
    assert "## 本轮最相关的技能" not in "\n\n".join(contents)
    assert "frontdoor_runtime_tool_contract" in contents[1]
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
        {"role": "assistant", "content": "## 已检索上下文\n- stale memory"},
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
    assert contents[0].startswith("BASE PROMPT")
    assert "## Capability Exposure Snapshot" in contents[0]
    assert "OLD SYSTEM" not in contents
    assert "## 已检索上下文\n- stale memory" not in contents
    assert "checkpoint question" in contents
    assert "checkpoint answer" in contents
    assert "bootstrap transcript question" not in contents
    assert "bootstrap transcript answer" not in contents
    assert contents.count("follow up question") == 1
    assert contents[-1] == "follow up question"
    assert result.trace["current_user_in_transcript"] is True


@pytest.mark.asyncio
async def test_message_builder_keeps_batched_user_messages_in_transcript_history_without_duping_current_user() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)
    persisted_session = Session(key="web:shared")
    persisted_session.add_message("user", "first question")
    persisted_session.add_message("assistant", "first answer")
    persisted_session.add_message(
        "user",
        "first batched follow-up",
        metadata={
            "_transcript_turn_id": "turn-batch-1",
            "_transcript_state": "pending",
            "_transcript_batch_id": "batch-1",
        },
    )
    persisted_session.add_message(
        "user",
        "second batched follow-up",
        metadata={
            "_transcript_turn_id": "turn-batch-2",
            "_transcript_state": "pending",
            "_transcript_batch_id": "batch-1",
        },
    )

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="first batched follow-up\n\nsecond batched follow-up",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=persisted_session,
        user_content="second batched follow-up",
        user_metadata={"_transcript_turn_id": "turn-batch-2", "_transcript_batch_id": "batch-1"},
    )

    contents = [str(item.get("content") or "") for item in result.model_messages]
    assert contents.count("first batched follow-up") == 1
    assert contents.count("second batched follow-up") == 1
    assert contents[-2:] == ["first batched follow-up", "second batched follow-up"]
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
async def test_message_builder_keeps_empty_memory_overlay_when_snapshot_reader_is_unavailable() -> None:
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
        "retrieved_record_count": 0,
        "retrieval_scope_mode": "disabled",
        "retrieved_context_present": False,
    }
    assert result.trace["message_injection"] == {
        "history_source": "checkpoint",
        "history_message_count": 2,
        "current_user_appended": True,
        "retrieved_context_in_model_messages": False,
    }
    assert str(result.turn_overlay_text or "") == ""
    assert result.model_messages[-3:] == [
        {"role": "user", "content": "prior user"},
        {"role": "assistant", "content": "prior answer"},
        {"role": "user", "content": "remembered preference"},
    ]


@pytest.mark.asyncio
async def test_message_builder_ignores_stale_retrieved_context_checkpoint_when_snapshot_reader_is_unavailable() -> None:
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
            {"role": "assistant", "content": "## 已检索上下文\n- stale retrieved memory"},
        ],
        user_content="remembered preference",
    )

    overlay_text = str(result.turn_overlay_text or "")
    model_rendered = "\n\n".join(str(item.get("content") or "") for item in result.model_messages)

    assert overlay_text == ""
    assert "stale retrieved memory" in model_rendered
    assert "durable preference" not in overlay_text
    assert "durable preference" not in model_rendered


@pytest.mark.asyncio
async def test_message_builder_trace_includes_frontdoor_context_span_timings() -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="durable preference")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="remembered preference",
        exposure={
            "skills": [_skill("focused-skill", "Primary workflow")],
            "tool_families": [_tool_resource_record("agent_browser", "Browser automation")],
            "tool_names": ["filesystem", "agent_browser"],
        },
        persisted_session=None,
        checkpoint_messages=[
            {"role": "user", "content": "prior user"},
            {"role": "assistant", "content": "prior answer"},
        ],
        user_content="remembered preference",
    )

    spans = dict(result.trace.get("frontdoor_spans_ms") or {})

    assert set(spans) == {
        "collect_context_sources",
        "semantic_catalog_rankings",
        "retrieve_context_bundle",
        "resolve_history_injection",
        "inject_turn_context",
    }
    for value in spans.values():
        assert isinstance(value, (int, float))
        assert float(value) >= 0.0

@pytest.mark.asyncio
async def test_message_builder_task_ledger_preserves_continuity_when_history_visibility_filters_internal_status_turns() -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)
    persisted_session = Session(key="web:shared")
    persisted_session.add_message("user", "Install the weather skill")
    persisted_session.add_message("assistant", "I started the install.")
    persisted_session.add_message(
        "assistant",
        "Background task task:demo-ledger finished successfully.",
        metadata={
            "source": "heartbeat",
            "history_visible": False,
            "task_ids": ["task:demo-ledger"],
            "task_results": [
                {
                    "task_id": "task:demo-ledger",
                    "node_id": "node:root",
                    "node_kind": "execution",
                    "node_reason": "root_terminal",
                    "output": "Weather skill installed successfully",
                    "output_ref": "artifact:weather-skill",
                    "check_result": "accepted",
                }
            ],
        },
    )
    persisted_session.metadata = {
        "last_task_memory": web_ceo_sessions.build_last_task_memory(persisted_session),
    }

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="what happened with that task?",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=persisted_session,
        user_content="what happened with that task?",
    )

    stable_contents = [str(item.get("content") or "") for item in result.stable_messages]
    overlay = str(result.turn_overlay_text or "")

    assert "Background task task:demo-ledger finished successfully." not in "\n\n".join(stable_contents)
    assert result.trace["transcript_message_count"] == 2
    assert result.trace["history_source"] == "transcript"
    assert "## Task Ledger" in overlay
    assert "task:demo-ledger" in overlay
    assert "Weather skill installed successfully" in overlay
    assert "artifact:weather-skill" in overlay
    assert any("## Task Ledger" in str(item.get("content") or "") for item in result.dynamic_appendix_messages)


@pytest.mark.asyncio
async def test_message_builder_applies_frontdoor_stage_workset_compaction_to_history() -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    checkpoint_messages = [
        {"role": "user", "content": "bootstrap request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-1",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-1", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage one raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-2",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-2", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage two raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-3",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-3", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage three raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-4",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-4", "content": '{"ok": true}'},
        {"role": "assistant", "content": "stage four raw detail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-5",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "submit_next_stage", "tool_call_id": "call-stage-5", "content": '{"ok": true}'},
        {"role": "assistant", "content": "active stage raw detail"},
    ]

    def _tool_entry(
        stage_index: int,
        *,
        tool_name: str,
        arguments: dict[str, object],
        output_text: str,
    ) -> dict[str, object]:
        return {
            "tool_call_id": f"call-stage-{stage_index}-{tool_name}",
            "tool_name": tool_name,
            "arguments": dict(arguments),
            "arguments_text": f"{tool_name} ({json.dumps(arguments, ensure_ascii=False, sort_keys=True)})",
            "output_text": output_text,
            "output_preview_text": "",
            "output_ref": "",
            "status": "success",
            "started_at": f"2026-04-16T09:0{stage_index}:00+08:00",
            "finished_at": f"2026-04-16T09:0{stage_index}:01+08:00",
            "timestamp": f"2026-04-16T09:0{stage_index}:01+08:00",
            "elapsed_seconds": 1.0,
            "kind": "tool_result",
            "source": "user",
        }

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="continue",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=None,
        checkpoint_messages=checkpoint_messages,
        user_content="continue",
        frontdoor_stage_state={
            "active_stage_id": "frontdoor-stage-5",
            "transition_required": False,
            "stages": [
                {
                    "stage_id": "frontdoor-stage-1",
                    "stage_index": 1,
                    "stage_kind": "normal",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "completed",
                    "stage_goal": "inspect stage one",
                    "completed_stage_summary": "finished stage one",
                    "key_refs": [],
                    "tool_round_budget": 2,
                    "tool_rounds_used": 1,
                },
                {
                    "stage_id": "frontdoor-stage-2",
                    "stage_index": 2,
                    "stage_kind": "normal",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "completed",
                    "stage_goal": "inspect stage two",
                    "completed_stage_summary": "finished stage two",
                    "key_refs": [],
                    "tool_round_budget": 2,
                    "tool_rounds_used": 1,
                    "rounds": [
                        {
                            "round_id": "frontdoor-stage-2:round-1",
                            "round_index": 1,
                            "tool_names": ["filesystem_write"],
                            "tool_call_ids": ["call-stage-2-filesystem_write"],
                            "budget_counted": True,
                            "tools": [
                                _tool_entry(
                                    2,
                                    tool_name="filesystem_write",
                                    arguments={"path": "stage-two.txt", "content": "raw"},
                                    output_text="stage two inline output",
                                )
                            ],
                        }
                    ],
                },
                {
                    "stage_id": "frontdoor-stage-3",
                    "stage_index": 3,
                    "stage_kind": "normal",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "completed",
                    "stage_goal": "inspect stage three",
                    "completed_stage_summary": "finished stage three",
                    "key_refs": [],
                    "tool_round_budget": 2,
                    "tool_rounds_used": 1,
                    "rounds": [
                        {
                            "round_id": "frontdoor-stage-3:round-1",
                            "round_index": 1,
                            "tool_names": ["filesystem_write"],
                            "tool_call_ids": ["call-stage-3-filesystem_write"],
                            "budget_counted": True,
                            "tools": [
                                _tool_entry(
                                    3,
                                    tool_name="filesystem_write",
                                    arguments={"path": "stage-three.txt", "content": "raw"},
                                    output_text="stage three inline output",
                                )
                            ],
                        }
                    ],
                },
                {
                    "stage_id": "frontdoor-stage-4",
                    "stage_index": 4,
                    "stage_kind": "normal",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "completed",
                    "stage_goal": "inspect stage four",
                    "completed_stage_summary": "finished stage four",
                    "key_refs": [],
                    "tool_round_budget": 2,
                    "tool_rounds_used": 1,
                    "rounds": [
                        {
                            "round_id": "frontdoor-stage-4:round-1",
                            "round_index": 1,
                            "tool_names": ["filesystem_write"],
                            "tool_call_ids": ["call-stage-4-filesystem_write"],
                            "budget_counted": True,
                            "tools": [
                                _tool_entry(
                                    4,
                                    tool_name="filesystem_write",
                                    arguments={"path": "stage-four.txt", "content": "raw"},
                                    output_text="stage four inline output",
                                )
                            ],
                        }
                    ],
                },
                {
                    "stage_id": "frontdoor-stage-5",
                    "stage_index": 5,
                    "stage_kind": "normal",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "active",
                    "stage_goal": "inspect stage five",
                    "completed_stage_summary": "",
                    "key_refs": [],
                    "tool_round_budget": 2,
                    "tool_rounds_used": 0,
                    "rounds": [
                        {
                            "round_id": "frontdoor-stage-5:round-1",
                            "round_index": 1,
                            "tool_names": ["filesystem_write"],
                            "tool_call_ids": ["call-stage-5-filesystem_write"],
                            "budget_counted": True,
                            "tools": [
                                _tool_entry(
                                    5,
                                    tool_name="filesystem_write",
                                    arguments={"path": "stage-five.txt", "content": "raw"},
                                    output_text="active stage inline output",
                                )
                            ],
                        }
                    ],
                },
            ],
        },
    )

    stable_contents = [str(item.get("content") or "") for item in result.stable_messages]
    rendered = "\n\n".join(stable_contents)
    raw_blocks = [
        json.loads(content.split("\n", 1)[1])
        for content in stable_contents
        if content.startswith("[G3KU_STAGE_RAW_V1]")
    ]
    assert "stage one raw detail" not in rendered
    assert "stage two raw detail" not in rendered
    assert "stage three raw detail" not in rendered
    assert "stage four raw detail" not in rendered
    assert "active stage raw detail" not in rendered
    assert [int(item["stage_index"]) for item in raw_blocks] == [2, 3, 4, 5]
    assert raw_blocks[0]["rounds"][0]["tools"][0]["output_text"] == "stage two inline output"
    assert raw_blocks[3]["rounds"][0]["tools"][0]["arguments"] == {
        "path": "stage-five.txt",
        "content": "raw",
    }
    assert any(content.startswith("[G3KU_STAGE_COMPACT_V1]") for content in stable_contents)


@pytest.mark.asyncio
async def test_message_builder_renders_retained_raw_stage_blocks_from_stage_state_without_active_stage() -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    checkpoint_messages = [
        {"role": "user", "content": "bootstrap request"},
        {"role": "assistant", "content": "checkpoint stage one raw detail"},
        {"role": "assistant", "content": "checkpoint stage two raw detail"},
        {"role": "assistant", "content": "checkpoint stage three raw detail"},
        {"role": "assistant", "content": "checkpoint stage four raw detail"},
    ]

    def _tool_entry(
        stage_index: int,
        *,
        tool_name: str,
        arguments: dict[str, object],
        output_text: str,
        output_preview_text: str = "",
        output_ref: str = "",
    ) -> dict[str, object]:
        return {
            "tool_call_id": f"call-stage-{stage_index}-{tool_name}",
            "tool_name": tool_name,
            "arguments": dict(arguments),
            "arguments_text": f"{tool_name} ({json.dumps(arguments, ensure_ascii=False, sort_keys=True)})",
            "output_text": output_text,
            "output_preview_text": output_preview_text,
            "output_ref": output_ref,
            "status": "success",
            "started_at": f"2026-04-16T10:0{stage_index}:00+08:00",
            "finished_at": f"2026-04-16T10:0{stage_index}:01+08:00",
            "timestamp": f"2026-04-16T10:0{stage_index}:01+08:00",
            "elapsed_seconds": 1.0,
            "kind": "tool_result",
            "source": "user",
        }

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="continue",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem_write", "content_open"]},
        persisted_session=None,
        checkpoint_messages=checkpoint_messages,
        user_content="continue",
        frontdoor_stage_state={
            "active_stage_id": "",
            "transition_required": False,
            "stages": [
                {
                    "stage_id": "frontdoor-stage-1",
                    "stage_index": 1,
                    "stage_kind": "normal",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "completed",
                    "stage_goal": "inspect stage one",
                    "completed_stage_summary": "finished stage one",
                    "key_refs": [],
                    "tool_round_budget": 2,
                    "tool_rounds_used": 1,
                    "rounds": [
                        {
                            "round_id": "frontdoor-stage-1:round-1",
                            "round_index": 1,
                            "tool_names": ["content_open"],
                            "tool_call_ids": ["call-stage-1-content_open"],
                            "budget_counted": True,
                            "tools": [
                                _tool_entry(
                                    1,
                                    tool_name="content_open",
                                    arguments={"path": "README.md"},
                                    output_text="stage one inline output",
                                )
                            ],
                        }
                    ],
                },
                {
                    "stage_id": "frontdoor-stage-2",
                    "stage_index": 2,
                    "stage_kind": "normal",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "completed",
                    "stage_goal": "inspect stage two",
                    "completed_stage_summary": "finished stage two",
                    "key_refs": [],
                    "tool_round_budget": 2,
                    "tool_rounds_used": 1,
                    "rounds": [
                        {
                            "round_id": "frontdoor-stage-2:round-1",
                            "round_index": 1,
                            "tool_names": ["filesystem_write"],
                            "tool_call_ids": ["call-stage-2-filesystem_write"],
                            "budget_counted": True,
                            "tools": [
                                _tool_entry(
                                    2,
                                    tool_name="filesystem_write",
                                    arguments={"path": "notes.txt", "content": "hello"},
                                    output_text="stage two inline output",
                                )
                            ],
                        }
                    ],
                },
                {
                    "stage_id": "frontdoor-stage-3",
                    "stage_index": 3,
                    "stage_kind": "normal",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "completed",
                    "stage_goal": "inspect stage three",
                    "completed_stage_summary": "finished stage three",
                    "key_refs": [],
                    "tool_round_budget": 2,
                    "tool_rounds_used": 1,
                    "rounds": [
                        {
                            "round_id": "frontdoor-stage-3:round-1",
                            "round_index": 1,
                            "tool_names": ["content_open"],
                            "tool_call_ids": ["call-stage-3-content_open"],
                            "budget_counted": True,
                            "tools": [
                                _tool_entry(
                                    3,
                                    tool_name="content_open",
                                    arguments={"path": "artifact.txt"},
                                    output_text="",
                                    output_preview_text="output captured in ref",
                                    output_ref="artifact:artifact:stage-three",
                                )
                            ],
                        }
                    ],
                },
                {
                    "stage_id": "frontdoor-stage-4",
                    "stage_index": 4,
                    "stage_kind": "normal",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "completed",
                    "stage_goal": "inspect stage four",
                    "completed_stage_summary": "finished stage four",
                    "key_refs": [],
                    "tool_round_budget": 2,
                    "tool_rounds_used": 1,
                    "rounds": [
                        {
                            "round_id": "frontdoor-stage-4:round-1",
                            "round_index": 1,
                            "tool_names": ["filesystem_write"],
                            "tool_call_ids": ["call-stage-4-filesystem_write"],
                            "budget_counted": True,
                            "tools": [
                                _tool_entry(
                                    4,
                                    tool_name="filesystem_write",
                                    arguments={"path": "summary.txt", "content": "done"},
                                    output_text="stage four inline output",
                                )
                            ],
                        }
                    ],
                },
            ],
        },
    )

    stable_contents = [str(item.get("content") or "") for item in result.stable_messages]
    rendered = "\n\n".join(stable_contents)
    raw_blocks = [
        json.loads(content.split("\n", 1)[1])
        for content in stable_contents
        if content.startswith("[G3KU_STAGE_RAW_V1]")
    ]

    assert "checkpoint stage one raw detail" not in rendered
    assert "checkpoint stage two raw detail" not in rendered
    assert "checkpoint stage three raw detail" not in rendered
    assert "checkpoint stage four raw detail" not in rendered
    assert len(raw_blocks) == 3
    assert [int(item["stage_index"]) for item in raw_blocks] == [2, 3, 4]
    assert raw_blocks[0]["rounds"][0]["tools"][0]["arguments"] == {"path": "notes.txt", "content": "hello"}
    assert raw_blocks[1]["rounds"][0]["tools"][0]["output_ref"] == "artifact:artifact:stage-three"
    assert raw_blocks[2]["rounds"][0]["tools"][0]["output_text"] == "stage four inline output"
    assert any(content.startswith("[G3KU_STAGE_COMPACT_V1]") for content in stable_contents)


@pytest.mark.asyncio
async def test_message_builder_injects_global_summary_block_and_includes_hidden_heartbeat_execution_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="")
    loop = _loop(memory_manager)
    loop.context_length = 200_000
    builder = CeoMessageBuilder(loop=loop, prompt_builder=prompt_builder)

    persisted_session = Session(key="web:shared")
    persisted_session.add_message(
        "assistant",
        "Heartbeat finished a background inspection.",
        canonical_context={"stages": [{"stage_goal": "inspect repo", "rounds": []}]},
        metadata={"source": "heartbeat", "history_visible": False},
    )

    captured_messages: list[dict[str, object]] = []

    async def _fake_summary(messages, *, max_output_tokens, model_key=None):
        _ = max_output_tokens, model_key
        captured_messages.extend(list(messages or []))
        return "## 长期目标\n继续当前任务"

    monkeypatch.setattr(
        "g3ku.runtime.frontdoor.message_builder.summarize_global_context_model_first",
        _fake_summary,
    )
    monkeypatch.setattr(
        "g3ku.runtime.frontdoor.message_builder.estimate_message_tokens",
        lambda messages: 120_000 if messages else 0,
    )

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="continue",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=persisted_session,
        checkpoint_messages=[
            {"role": "user", "content": "older user"},
            {"role": "assistant", "content": "older assistant"},
        ],
        user_content="continue",
        frontdoor_stage_state={"active_stage_id": "", "transition_required": False, "stages": []},
    )

    stable_contents = [str(item.get("content") or "") for item in result.stable_messages]
    assert any(content.startswith("[G3KU_LONG_CONTEXT_SUMMARY_V1]") for content in stable_contents)
    assert any("Heartbeat finished a background inspection." in str(item.get("content") or "") for item in captured_messages)
    assert result.trace["global_summary_present"] is True
    semantic_state = dict(result.trace.get("semantic_context_state") or {})
    compression_state = dict(result.trace.get("compression_state_payload") or {})
    assert semantic_state.get("summary_text") == "## 长期目标\n继续当前任务"
    assert compression_state.get("status") == "ready"
    assert compression_state.get("source") == "semantic"


@pytest.mark.asyncio
async def test_message_builder_trace_reports_pre_summary_and_effective_prompt_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    monkeypatch.setattr(
        "g3ku.runtime.frontdoor.message_builder.estimate_message_tokens",
        lambda messages: len(list(messages or [])) * 50,
    )

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="continue",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=None,
        user_content="continue",
        frontdoor_canonical_context={
            "active_stage_id": "",
            "transition_required": False,
            "stages": [
                {
                    "stage_id": f"frontdoor-stage-{index}",
                    "stage_index": index,
                    "stage_kind": "normal",
                    "representation": "raw",
                    "system_generated": False,
                    "mode": "自主执行",
                    "status": "completed",
                    "stage_goal": f"inspect stage {index}",
                    "completed_stage_summary": f"finished stage {index}",
                    "key_refs": [],
                    "tool_round_budget": 2,
                    "tool_rounds_used": 1,
                    "rounds": [],
                }
                for index in range(1, 5)
            ],
        },
    )

    assert "prompt_estimate_tokens" not in result.trace
    assert result.trace["pre_summary_prompt_tokens"] == 300
    assert result.trace["effective_prompt_tokens"] >= 300


@pytest.mark.asyncio
async def test_message_builder_reuses_covered_global_summary_without_recomputation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="")
    loop = _loop(memory_manager)
    loop.context_length = 200_000
    builder = CeoMessageBuilder(loop=loop, prompt_builder=prompt_builder)

    summary_calls = 0

    async def _fake_summary(messages, *, max_output_tokens, model_key=None):
        nonlocal summary_calls
        _ = messages, max_output_tokens, model_key
        summary_calls += 1
        return "## Goals\nFresh summary"

    def _fake_estimate(messages):
        rendered = "\n".join(str(item.get("content") or "") for item in list(messages or []) if isinstance(item, dict))
        return 120_000 if "BASE PROMPT" in rendered else 5_000

    monkeypatch.setattr(
        "g3ku.runtime.frontdoor.message_builder.summarize_global_context_model_first",
        _fake_summary,
    )
    monkeypatch.setattr(
        "g3ku.runtime.frontdoor.message_builder.estimate_message_tokens",
        _fake_estimate,
    )
    checkpoint_messages, frontdoor_stage_state = _stage_history_with_global_zone()

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="continue",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=None,
        checkpoint_messages=checkpoint_messages,
        user_content="continue",
        frontdoor_stage_state=frontdoor_stage_state,
        semantic_context_state={
            "summary_text": "## Goals\nExisting summary",
            "coverage_history_source": "checkpoint",
            "coverage_message_index": 3,
            "coverage_stage_index": 1,
            "needs_refresh": False,
            "failure_cooldown_until": "",
            "updated_at": "2026-04-13T18:00:00",
        },
    )

    assert summary_calls == 0
    assert result.trace["semantic_context_state"]["summary_text"] == "## Goals\nExisting summary"


@pytest.mark.asyncio
async def test_message_builder_directly_continues_request_body_seed_without_stage_rebuild() -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)
    _checkpoint_messages, frontdoor_stage_state = _stage_history_with_global_zone()
    request_body_seed_messages = [
        {"role": "system", "content": "BASE PROMPT"},
        {"role": "user", "content": "previous user"},
        {"role": "assistant", "content": "previous final answer"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-exec-1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "exec",
            "tool_call_id": "call-exec-1",
            "content": '{"status":"success"}',
        },
    ]

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="strict follow up",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=None,
        checkpoint_messages=[],
        request_body_seed_messages=request_body_seed_messages,
        user_content="strict follow up",
        frontdoor_stage_state=frontdoor_stage_state,
    )

    stable_contents = [str(item.get("content") or "") for item in result.stable_messages]
    model_contents = [str(item.get("content") or "") for item in result.model_messages]
    rendered = "\n\n".join(stable_contents)

    assert stable_contents[0] == "BASE PROMPT"
    assert "previous final answer" in rendered
    assert result.stable_messages[3]["role"] == "assistant"
    assert result.stable_messages[3]["content"] == ""
    assert result.stable_messages[3]["tool_calls"] == [
        {
            "id": "call-exec-1",
            "type": "function",
            "function": {"name": "exec", "arguments": "{}"},
        }
    ]
    assert result.stable_messages[4]["role"] == "tool"
    assert result.stable_messages[4]["tool_call_id"] == "call-exec-1"
    assert result.stable_messages[4]["name"] == "exec"
    assert stable_contents[-1] == '{"status":"success"}'
    assert "strict follow up" in model_contents
    assert "[G3KU_STAGE_COMPACT_V1]" not in rendered
    assert "[G3KU_STAGE_RAW_V1]" not in rendered
    assert result.trace["history_source"] == "session_window"


@pytest.mark.asyncio
async def test_message_builder_reuses_existing_summary_while_failure_cooldown_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="")
    loop = _loop(memory_manager)
    loop.context_length = 200_000
    builder = CeoMessageBuilder(loop=loop, prompt_builder=prompt_builder)

    summary_calls = 0

    async def _fake_summary(messages, *, max_output_tokens, model_key=None):
        nonlocal summary_calls
        _ = messages, max_output_tokens, model_key
        summary_calls += 1
        return "## Goals\nFresh summary"

    def _fake_estimate(messages):
        rendered = "\n".join(str(item.get("content") or "") for item in list(messages or []) if isinstance(item, dict))
        return 120_000 if "BASE PROMPT" in rendered else 5_000

    monkeypatch.setattr(
        "g3ku.runtime.frontdoor.message_builder.summarize_global_context_model_first",
        _fake_summary,
    )
    monkeypatch.setattr(
        "g3ku.runtime.frontdoor.message_builder.estimate_message_tokens",
        _fake_estimate,
    )
    checkpoint_messages, frontdoor_stage_state = _stage_history_with_global_zone()

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="continue",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=None,
        checkpoint_messages=checkpoint_messages,
        user_content="continue",
        frontdoor_stage_state=frontdoor_stage_state,
        semantic_context_state={
            "summary_text": "## Goals\nExisting summary",
            "coverage_history_source": "checkpoint",
            "coverage_message_index": 3,
            "coverage_stage_index": 1,
            "needs_refresh": True,
            "failure_cooldown_until": "2999-01-01T00:00:00",
            "updated_at": "2026-04-13T18:00:00",
        },
    )

    assert summary_calls == 0
    assert result.trace["semantic_context_state"]["summary_text"] == "## Goals\nExisting summary"
    assert result.trace["semantic_context_state"]["failure_cooldown_until"] == "2999-01-01T00:00:00"


@pytest.mark.asyncio
async def test_message_builder_uses_resolved_ceo_context_window_instead_of_loop_context_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="")
    loop = _loop(memory_manager)
    loop.context_length = 200_000
    builder = CeoMessageBuilder(loop=loop, prompt_builder=prompt_builder)

    async def _fake_summary(messages, *, max_output_tokens, model_key=None):
        _ = messages, max_output_tokens, model_key
        return "## Goals\nCurrent task"

    monkeypatch.setattr(
        message_builder_module,
        "_resolve_ceo_context_window_tokens",
        lambda loop: 64_000,
        raising=False,
    )
    monkeypatch.setattr(
        "g3ku.runtime.frontdoor.message_builder.summarize_global_context_model_first",
        _fake_summary,
    )
    monkeypatch.setattr(
        "g3ku.runtime.frontdoor.message_builder.estimate_message_tokens",
        lambda messages: 120_000 if messages else 0,
    )

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="continue",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=None,
        checkpoint_messages=[
            {"role": "user", "content": "older user"},
            {"role": "assistant", "content": "older assistant"},
        ],
        user_content="continue",
    )

    assert result.trace["global_summary_trigger_tokens"] == 20_000
    assert result.trace["global_summary_pressure_warn_tokens"] == 54_400
    assert result.trace["global_summary_force_refresh_tokens"] == 60_800


@pytest.mark.asyncio
async def test_message_builder_global_summary_uses_externalized_compression_block_without_archive_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="")
    loop = _loop(memory_manager)
    loop.context_length = 200_000
    builder = CeoMessageBuilder(loop=loop, prompt_builder=prompt_builder)

    captured_messages: list[dict[str, object]] = []

    async def _fake_summary(messages, *, max_output_tokens, model_key=None):
        _ = max_output_tokens, model_key
        captured_messages.extend(list(messages or []))
        return "## Goals\nUse the externalized block only"

    monkeypatch.setattr(
        "g3ku.runtime.frontdoor.message_builder.summarize_global_context_model_first",
        _fake_summary,
    )
    monkeypatch.setattr(
        "g3ku.runtime.frontdoor.message_builder.estimate_message_tokens",
        lambda messages: 120_000 if messages else 0,
    )
    checkpoint_messages, frontdoor_stage_state = _stage_history_with_global_zone()
    frontdoor_stage_state["stages"].insert(
        0,
        {
            "stage_id": "frontdoor-stage-compression-0",
            "stage_index": 0,
            "stage_kind": "compression",
            "system_generated": True,
            "status": "completed",
            "stage_goal": "older archived work",
            "completed_stage_summary": "archived summary",
            "archive_ref": "artifact:artifact:frontdoor-stage-archive",
            "archive_stage_index_start": 1,
            "archive_stage_index_end": 10,
        },
    )

    await builder.build_for_ceo(
        session=_session(),
        query_text="continue",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=None,
        checkpoint_messages=checkpoint_messages,
        user_content="continue",
        frontdoor_stage_state=frontdoor_stage_state,
    )

    rendered = "\n\n".join(str(item.get("content") or "") for item in captured_messages)
    assert "[G3KU_STAGE_EXTERNALIZED_V1]" in rendered
    assert "artifact:artifact:frontdoor-stage-archive" in rendered
    assert "archived summary" in rendered


@pytest.mark.asyncio
async def test_message_builder_history_visibility_checkpoint_round_trip_keeps_hidden_internal_assistant_turn_out_of_history() -> None:
    prompt_builder = _SplitPromptBuilder()
    memory_manager = _MemoryManager(response="")
    builder = CeoMessageBuilder(loop=_loop(memory_manager), prompt_builder=prompt_builder)
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    checkpoint_messages = runner._state_message_records(
        convert_to_messages(
            [
                {"role": "user", "content": "checkpoint question"},
                {"role": "assistant", "content": "checkpoint answer"},
                {
                    "role": "assistant",
                    "content": "Background task task:demo-checkpoint finished successfully.",
                    "metadata": {"history_visible": False, "source": "heartbeat"},
                },
            ]
        )
    )

    result = await builder.build_for_ceo(
        session=_session(),
        query_text="follow up question",
        exposure={"skills": [], "tool_families": [], "tool_names": ["filesystem"]},
        persisted_session=None,
        checkpoint_messages=checkpoint_messages,
        user_content="follow up question",
    )

    contents = [str(item.get("content") or "") for item in result.model_messages]
    assert "checkpoint question" in contents
    assert "checkpoint answer" in contents
    assert "Background task task:demo-checkpoint finished successfully." not in contents
    assert contents[-1] == "follow up question"
    assert result.trace["history_source"] == "checkpoint"
    assert result.trace["checkpoint_message_count"] == 2


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

    overlay = str(result.turn_overlay_text or "")
    assert "turn memory snippet" not in overlay
    assert memory_manager.calls == []
    assert result.trace["same_session_turn_memory_filtered_count"] == 0
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
    assert "other session snippet" not in overlay
    assert memory_manager.calls == []
    assert result.trace["same_session_turn_memory_filtered_count"] == 0
    assert result.trace["retrieved_record_count"] == 0


@pytest.mark.asyncio
async def test_ceo_context_assembly_injects_memory_snapshot_without_memory_search_overlay() -> None:
    loop = SimpleNamespace(
        memory_manager=SimpleNamespace(
            snapshot_text=lambda **_: "---\n2026/4/17-self：\n完成任务必须说明任务总耗时\n",
            sync_catalog=None,
        ),
        main_task_service=None,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                skill_inventory_top_k=16,
                extension_tool_top_k=16,
                core_tools=["memory_write", "memory_delete", "memory_note"],
                frontdoor_global_summary_trigger_ratio=0.5,
                frontdoor_global_summary_target_ratio=0.2,
                frontdoor_global_summary_min_output_tokens=2000,
                frontdoor_global_summary_max_output_ratio=0.05,
                frontdoor_global_summary_max_output_tokens_ceiling=12000,
                frontdoor_global_summary_pressure_warn_ratio=0.85,
                frontdoor_global_summary_force_refresh_ratio=0.95,
                frontdoor_global_summary_min_delta_tokens=2000,
                frontdoor_global_summary_failure_cooldown_seconds=600,
                frontdoor_global_summary_model="",
            )
        ),
    )
    builder = CeoMessageBuilder(loop=loop, prompt_builder=_PromptBuilder())
    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
    )

    result = await builder._collect_turn_context_sources(
        session=session,
        query_text="记住这个规则",
        exposure={"tool_names": ["memory_write", "memory_delete", "memory_note"], "tool_families": [], "skills": []},
        user_content="记住这个规则",
        hydrated_tool_names=[],
    )

    overlay = "\n\n".join(result["turn_overlay_parts"])
    assert "2026/4/17-self：" in overlay
    assert "memory_search" not in overlay


@pytest.mark.asyncio
async def test_frontdoor_uses_frozen_memory_snapshot_only_once_per_turn() -> None:
    snapshot_calls: list[dict[str, object]] = []
    snapshot_values = iter(
        [
            "---\n2026/4/17-self：\nUse the first frozen snapshot\n",
            "---\n2026/4/17-self：\nDo not hot-reload within the same turn\n",
        ]
    )

    def _snapshot_text(**kwargs):
        snapshot_calls.append(dict(kwargs))
        return next(snapshot_values)

    loop = SimpleNamespace(
        memory_manager=SimpleNamespace(
            snapshot_text=_snapshot_text,
            sync_catalog=None,
        ),
        main_task_service=None,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                skill_inventory_top_k=16,
                extension_tool_top_k=16,
                core_tools=["memory_write", "memory_delete", "memory_note"],
                frontdoor_global_summary_trigger_ratio=0.5,
                frontdoor_global_summary_target_ratio=0.2,
                frontdoor_global_summary_min_output_tokens=2000,
                frontdoor_global_summary_max_output_ratio=0.05,
                frontdoor_global_summary_max_output_tokens_ceiling=12000,
                frontdoor_global_summary_pressure_warn_ratio=0.85,
                frontdoor_global_summary_force_refresh_ratio=0.95,
                frontdoor_global_summary_min_delta_tokens=2000,
                frontdoor_global_summary_failure_cooldown_seconds=600,
                frontdoor_global_summary_model="",
            )
        ),
    )
    builder = CeoMessageBuilder(loop=loop, prompt_builder=_PromptBuilder())
    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
    )

    result = await builder._collect_turn_context_sources(
        session=session,
        query_text="remember this rule",
        exposure={"tool_names": ["memory_write", "memory_delete", "memory_note"], "tool_families": [], "skills": []},
        user_content="remember this rule",
        hydrated_tool_names=[],
    )

    overlay = "\n\n".join(result["turn_overlay_parts"])
    assert len(snapshot_calls) == 1
    assert "Use the first frozen snapshot" in overlay
    assert "Do not hot-reload within the same turn" not in overlay
