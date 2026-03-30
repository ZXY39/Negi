from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.context.assembly import ContextAssemblyService
from g3ku.runtime.context.types import RetrievedContextBundle
from g3ku.runtime.frontdoor.prompt_builder import CeoPromptBuilder


class _PromptBuilder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def build(self, *, skills: list) -> str:
        ids = [str(getattr(item, 'skill_id', '') or '').strip() for item in list(skills or []) if str(getattr(item, 'skill_id', '') or '').strip()]
        self.calls.append(ids)
        return 'BASE PROMPT'


class _MemoryManager:
    def __init__(self, response: str = '# Retrieved Context\n\n- [memory] remembered fact') -> None:
        self.calls: list[dict[str, object]] = []
        self.response = response

    async def sync_catalog(self, service, *, skill_ids=None, tool_ids=None):
        _ = service, skill_ids, tool_ids
        return {'created': 0, 'updated': 0, 'removed': 0}

    async def retrieve_context_bundle(self, **kwargs):
        self.calls.append(dict(kwargs))
        return RetrievedContextBundle(
            query=str(kwargs.get('query') or ''),
            records=(
                [
                    {
                        'record_id': 'memory-1',
                        'context_type': 'memory',
                        'l0': 'remembered fact',
                        'l1': self.response.replace('# Retrieved Context', '').strip(),
                        'l2_preview': '',
                        'source': 'test',
                        'confidence': 1.0,
                    }
                ]
                if self.response
                else []
            ),
        )


class _SemanticMemoryManager(_MemoryManager):
    def __init__(
        self,
        *,
        response: str = '# Retrieved Context\n\n- [memory] remembered fact',
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
                'namespace_prefix': namespace_prefix,
                'query': query,
                'limit': limit,
                'context_type': context_type,
            }
        )
        record_ids = self.skill_record_ids if context_type == 'skill' else self.tool_record_ids
        return [SimpleNamespace(record_id=record_id) for record_id in record_ids[: max(limit, 0)]]


def _loop(memory_manager: _MemoryManager, *, main_task_service=None) -> SimpleNamespace:
    return SimpleNamespace(
        main_task_service=main_task_service,
        memory_manager=memory_manager,
        _use_rag_memory=lambda: True,
        memory_window=100,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                max_prompt_tokens=3200,
                live_raw_tail_turns=4,
                task_continuity_max_tokens=320,
                stage_context_max_tokens=640,
                latest_archive_overview_max_tokens=420,
                older_archive_abstracts_top_k=4,
                older_archive_abstracts_max_tokens=320,
                retrieved_context_max_tokens=1200,
                recent_messages_limit=24,
                archive_summary_top_k=2,
                archive_summary_max_tokens=320,
                skill_inventory_top_k=8,
                skill_inventory_max_tokens=480,
                extension_tool_top_k=6,
                core_tools=[],
            )
        ),
    )


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
    )


def _skill(skill_id: str, description: str) -> SimpleNamespace:
    return SimpleNamespace(skill_id=skill_id, display_name=skill_id, description=description)


def _family(
    tool_id: str,
    description: str,
    *,
    callable: bool = True,
    available: bool = True,
    install_dir: str = '',
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


def test_ceo_prompt_builder_includes_memory_write_guidance() -> None:
    builder = CeoPromptBuilder(loop=SimpleNamespace(workspace=Path.cwd()))

    prompt = builder.build(skills=[])

    assert 'memory_write' in prompt
    assert '长期记住' in prompt
    assert 'Retrieved Context' in prompt


class _TaskService:
    def __init__(self, active_tasks: list[dict[str, str]] | None = None) -> None:
        self.active_tasks = list(active_tasks or [])
        self.calls: list[tuple[str, int]] = []

    def list_active_task_snapshots_for_session(self, session_id: str, *, limit: int = 3) -> list[dict[str, str]]:
        self.calls.append((str(session_id or ''), int(limit)))
        return list(self.active_tasks[:limit])


@pytest.mark.asyncio
async def test_ceo_context_assembly_defaults_to_memory_only_retrieval_for_general_query() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager()
    service = ContextAssemblyService(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await service.build_for_ceo(
        session=_session(),
        query_text='what do you remember about my deployment preferences?',
        exposure={
            'skills': [_skill('demo_skill', 'Handle rollback planning')],
            'tool_families': [_family('external_browser', 'Browser automation', callable=False, install_dir='D:/ext/browser')],
            'tool_names': ['filesystem'],
        },
        persisted_session=None,
    )

    assert prompt_builder.calls == [['demo_skill']]
    assert len(memory_manager.calls) == 1
    assert memory_manager.calls[0]['search_context_types'] == ['memory', 'skill', 'resource']
    assert memory_manager.calls[0]['allowed_context_types'] == ['memory', 'skill', 'resource']
    assert memory_manager.calls[0]['allowed_resource_record_ids'] == ['tool:external_browser']
    assert memory_manager.calls[0]['allowed_skill_record_ids'] == ['skill:demo_skill']
    assert result.trace['retrieval_scope'] == {
        'mode': 'rbac_fallback',
        'search_context_types': ['memory', 'skill', 'resource'],
        'allowed_context_types': ['memory', 'skill', 'resource'],
        'allowed_resource_record_ids': ['tool:external_browser'],
        'allowed_skill_record_ids': ['skill:demo_skill'],
    }


@pytest.mark.asyncio
async def test_ceo_context_assembly_keeps_prompt_inventory_stable_when_rbac_fallback_applies() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response='# Retrieved Context\n\n- [skill:demo_skill] Detailed skill context\n- [resource:tool:external_browser] Detailed tool context')
    service = ContextAssemblyService(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    general_result = await service.build_for_ceo(
        session=_session(),
        query_text='recap our current workspace preferences',
        exposure={
            'skills': [
                _skill('demo_skill', 'Handle rollback planning'),
                _skill('other_skill', 'Handle metrics review'),
            ],
            'tool_families': [
                _family('external_browser', 'Browser automation', callable=False, install_dir='D:/ext/browser'),
                _family('external_search', 'Search helper', callable=False, install_dir='D:/ext/search'),
            ],
            'tool_names': ['filesystem', 'load_tool_context'],
        },
        persisted_session=None,
    )
    targeted_result = await service.build_for_ceo(
        session=_session(),
        query_text='how do I use demo_skill with the external_browser tool?',
        exposure={
            'skills': [
                _skill('demo_skill', 'Handle rollback planning'),
                _skill('other_skill', 'Handle metrics review'),
            ],
            'tool_families': [
                _family('external_browser', 'Browser automation', callable=False, install_dir='D:/ext/browser'),
                _family('external_search', 'Search helper', callable=False, install_dir='D:/ext/search'),
            ],
            'tool_names': ['filesystem', 'load_tool_context'],
        },
        persisted_session=None,
    )

    assert len(prompt_builder.calls) == 2
    assert prompt_builder.calls[0] == ['demo_skill', 'other_skill']
    assert prompt_builder.calls[1] == ['demo_skill', 'other_skill']
    assert len(memory_manager.calls) == 2
    assert memory_manager.calls[0]['search_context_types'] == ['memory', 'skill', 'resource']
    assert memory_manager.calls[0]['allowed_context_types'] == ['memory', 'skill', 'resource']
    assert memory_manager.calls[0]['allowed_skill_record_ids'] == ['skill:demo_skill', 'skill:other_skill']
    assert memory_manager.calls[0]['allowed_resource_record_ids'] == ['tool:external_browser', 'tool:external_search']
    assert memory_manager.calls[1]['search_context_types'] == ['memory', 'skill', 'resource']
    assert memory_manager.calls[1]['allowed_context_types'] == ['memory', 'skill', 'resource']
    assert memory_manager.calls[1]['allowed_skill_record_ids'] == ['skill:demo_skill', 'skill:other_skill']
    assert memory_manager.calls[1]['allowed_resource_record_ids'] == ['tool:external_browser', 'tool:external_search']
    assert general_result.trace['retrieval_scope'] == {
        'mode': 'rbac_fallback',
        'search_context_types': ['memory', 'skill', 'resource'],
        'allowed_context_types': ['memory', 'skill', 'resource'],
        'allowed_resource_record_ids': ['tool:external_browser', 'tool:external_search'],
        'allowed_skill_record_ids': ['skill:demo_skill', 'skill:other_skill'],
    }
    assert targeted_result.trace['retrieval_scope'] == {
        'mode': 'rbac_fallback',
        'search_context_types': ['memory', 'skill', 'resource'],
        'allowed_context_types': ['memory', 'skill', 'resource'],
        'allowed_resource_record_ids': ['tool:external_browser', 'tool:external_search'],
        'allowed_skill_record_ids': ['skill:demo_skill', 'skill:other_skill'],
    }
    assert [item['tool_id'] for item in targeted_result.trace['external_tools']] == ['external_browser', 'external_search']


@pytest.mark.asyncio
async def test_ceo_context_assembly_keeps_explicitly_named_skill_in_stable_summary_order() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response='# Retrieved Context\n\n- [skill:focused-skill] Detailed skill context')
    service = ContextAssemblyService(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await service.build_for_ceo(
        session=_session(),
        query_text='why did you say focused-skill is not visible?',
        exposure={
            'skills': [
                _skill('focused-skill', 'Primary workflow'),
                _skill('secondary-skill', 'Secondary workflow'),
            ],
            'tool_families': [],
            'tool_names': ['filesystem', 'load_skill_context'],
        },
        persisted_session=None,
    )

    assert len(prompt_builder.calls) == 1
    assert prompt_builder.calls[0] == ['focused-skill', 'secondary-skill']
    assert len(memory_manager.calls) == 1
    assert memory_manager.calls[0]['allowed_skill_record_ids'] == ['skill:focused-skill', 'skill:secondary-skill']
    assert result.trace['retrieval_scope'] == {
        'mode': 'rbac_fallback',
        'search_context_types': ['memory', 'skill'],
        'allowed_context_types': ['memory', 'skill'],
        'allowed_resource_record_ids': [],
        'allowed_skill_record_ids': ['skill:focused-skill', 'skill:secondary-skill'],
    }


@pytest.mark.asyncio
async def test_ceo_context_assembly_lists_unavailable_callable_tools_as_context_resources() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response='')
    service = ContextAssemblyService(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await service.build_for_ceo(
        session=_session(),
        query_text='please fix the browser automation tool before using it',
        exposure={
            'skills': [],
            'tool_families': [
                _family(
                    'agent_browser',
                    'Browser automation via the upstream CLI.',
                    callable=True,
                    available=False,
                    metadata={'warnings': ['missing required bins']},
                ),
            ],
            'tool_names': ['filesystem', 'load_tool_context'],
        },
        persisted_session=None,
    )

    assert 'load_tool_context(tool_id="agent_browser")' in result.system_prompt
    assert '`agent_browser`' in result.system_prompt
    assert 'missing required bins' in result.system_prompt


@pytest.mark.asyncio
async def test_ceo_context_assembly_includes_active_task_snapshot_message() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response='')
    task_service = _TaskService(
        active_tasks=[
            {
                'task_id': 'task:cont-1',
                'title': '续跑失败任务',
                'core_requirement': '继续完成打开网页的自动化流程',
                'continuation_of_task_id': 'task:old-1',
                'status': 'in_progress',
                'updated_at': '2026-03-28T10:00:00+08:00',
            }
        ]
    )
    service = ContextAssemblyService(loop=_loop(memory_manager, main_task_service=task_service), prompt_builder=prompt_builder)

    result = await service.build_for_ceo(
        session=_session(),
        query_text='重建任务，继续完成',
        exposure={'skills': [], 'tool_families': [], 'tool_names': ['create_async_task']},
        persisted_session=None,
    )

    assert task_service.calls == [('web:shared', 3)]
    assert result.recent_history[0]['role'] == 'assistant'
    assert '## Task Continuity' in str(result.recent_history[0]['content'])
    assert 'continuation_of_task_id=task:old-1' in str(result.recent_history[0]['content'])
    assert result.trace['active_tasks'] == {'count': 1, 'included': True}


@pytest.mark.asyncio
async def test_ceo_context_assembly_lists_enabled_but_unregistered_callable_tools_as_context_resources() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response='')
    service = ContextAssemblyService(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await service.build_for_ceo(
        session=_session(),
        query_text='help me repair the browser automation integration',
        exposure={
            'skills': [],
            'tool_families': [
                _family(
                    'agent_browser',
                    'Browser automation via the upstream CLI.',
                    callable=True,
                    available=True,
                ),
            ],
            'tool_names': ['filesystem', 'load_tool_context'],
        },
        persisted_session=None,
    )

    assert 'load_tool_context(tool_id="agent_browser")' in result.system_prompt
    assert '`agent_browser`' in result.system_prompt
    assert 'enabled but not currently registered in the callable function tool list' in result.system_prompt
    assert [item['tool_id'] for item in result.trace['external_tools']] == ['agent_browser']
    assert result.trace['external_tools'][0]['available'] is True
    assert result.trace['external_tools'][0]['callable'] is True
    assert result.trace['external_tools'][0]['registered_callable'] is False


@pytest.mark.asyncio
async def test_ceo_context_assembly_marks_registered_unavailable_callable_tools_as_repair_required() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response='')
    service = ContextAssemblyService(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await service.build_for_ceo(
        session=_session(),
        query_text='repair the browser tool and continue',
        exposure={
            'skills': [],
            'tool_families': [
                _family(
                    'agent_browser',
                    'Browser automation via the upstream CLI.',
                    callable=True,
                    available=False,
                    metadata={'warnings': ['missing required paths']},
                ),
            ],
            'tool_names': ['filesystem', 'load_tool_context', 'agent_browser'],
        },
        persisted_session=None,
    )

    assert '【待修复】' in result.system_prompt
    assert 'structured repair guidance' in result.system_prompt
    assert [item['tool_id'] for item in result.trace['external_tools']] == ['agent_browser']
    assert result.trace['external_tools'][0]['registered_callable'] is True


@pytest.mark.asyncio
async def test_ceo_context_assembly_reorders_skill_inventory_with_semantic_shortlist() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _SemanticMemoryManager(
        response='',
        skill_record_ids=['skill:secondary-skill', 'skill:focused-skill'],
    )
    service = ContextAssemblyService(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await service.build_for_ceo(
        session=_session(),
        query_text='help me reason about the focused workflow',
        exposure={
            'skills': [
                _skill('focused-skill', 'Primary workflow'),
                _skill('secondary-skill', 'Secondary workflow'),
            ],
            'tool_families': [],
            'tool_names': ['filesystem', 'load_skill_context'],
        },
        persisted_session=None,
    )

    assert prompt_builder.calls == [['secondary-skill', 'focused-skill']]
    assert result.trace['semantic_frontdoor']['mode'] == 'dense_only'
    assert [item['skill_id'] for item in result.trace['selected_skills']] == ['secondary-skill', 'focused-skill']
    assert result.trace['selected_skills'][0]['semantic_rank'] == 1
    assert result.trace['selected_skills'][1]['semantic_rank'] == 2
    assert [call['context_type'] for call in memory_manager.semantic_calls] == ['skill', 'resource']


@pytest.mark.asyncio
async def test_ceo_context_assembly_uses_semantic_shortlist_for_extension_tools() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _SemanticMemoryManager(
        response='',
        tool_record_ids=['tool:agent_browser', 'tool:web_fetch'],
    )
    loop = _loop(memory_manager)
    loop._memory_runtime_settings.assembly.extension_tool_top_k = 1
    service = ContextAssemblyService(loop=loop, prompt_builder=prompt_builder)

    result = await service.build_for_ceo(
        session=_session(),
        query_text='open the browser for me',
        exposure={
            'skills': [],
            'tool_families': [
                _family('agent_browser', 'Browser automation via semantic shortlist.'),
                _family('web_fetch', 'HTTP fetch helper.'),
            ],
            'tool_names': ['filesystem', 'agent_browser', 'web_fetch'],
        },
        persisted_session=None,
    )

    assert result.tool_names == ['agent_browser']
    assert result.trace['selected_tools']['extension'] == ['agent_browser']
    assert result.trace['semantic_frontdoor']['tools'][0]['tool_id'] == 'agent_browser'


@pytest.mark.asyncio
async def test_ceo_context_assembly_uses_dense_only_retrieval_scope_when_semantic_available() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _SemanticMemoryManager(
        response='',
        skill_record_ids=['skill:focused-skill', 'skill:secondary-skill'],
        tool_record_ids=['tool:agent_browser', 'tool:web_fetch'],
    )
    service = ContextAssemblyService(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await service.build_for_ceo(
        session=_session(),
        query_text='focused browser workflow',
        exposure={
            'skills': [
                _skill('focused-skill', 'Primary workflow'),
                _skill('secondary-skill', 'Secondary workflow'),
            ],
            'tool_families': [
                _family('agent_browser', 'Browser automation via semantic shortlist.'),
                _family('web_fetch', 'HTTP fetch helper.'),
            ],
            'tool_names': ['filesystem', 'agent_browser', 'web_fetch'],
        },
        persisted_session=None,
    )

    assert result.trace['retrieval_scope'] == {
        'mode': 'dense_only',
        'search_context_types': ['memory', 'skill', 'resource'],
        'allowed_context_types': ['memory', 'skill', 'resource'],
        'allowed_resource_record_ids': ['tool:agent_browser', 'tool:web_fetch'],
        'allowed_skill_record_ids': ['skill:focused-skill', 'skill:secondary-skill'],
    }


@pytest.mark.asyncio
async def test_ceo_context_assembly_prefers_memory_only_retrieval_for_memory_intent_even_when_semantic_available() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _SemanticMemoryManager(
        response='',
        skill_record_ids=['skill:focused-skill', 'skill:secondary-skill'],
        tool_record_ids=['tool:agent_browser', 'tool:web_fetch'],
    )
    service = ContextAssemblyService(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await service.build_for_ceo(
        session=_session(),
        query_text='from now on default to the focused browser workflow',
        exposure={
            'skills': [
                _skill('focused-skill', 'Primary workflow'),
                _skill('secondary-skill', 'Secondary workflow'),
            ],
            'tool_families': [
                _family('agent_browser', 'Browser automation via semantic shortlist.'),
                _family('web_fetch', 'HTTP fetch helper.'),
            ],
            'tool_names': ['filesystem', 'agent_browser', 'web_fetch', 'memory_write'],
        },
        persisted_session=None,
    )

    assert memory_manager.calls[0]['search_context_types'] == ['memory']
    assert memory_manager.calls[0]['allowed_context_types'] == ['memory']
    assert memory_manager.calls[0]['allowed_resource_record_ids'] == []
    assert memory_manager.calls[0]['allowed_skill_record_ids'] == []
    assert result.trace['retrieval_scope'] == {
        'mode': 'dense_only',
        'search_context_types': ['memory'],
        'allowed_context_types': ['memory'],
        'allowed_resource_record_ids': [],
        'allowed_skill_record_ids': [],
    }


@pytest.mark.asyncio
async def test_ceo_context_assembly_adds_retrieved_memory_resolution_hint_for_memory_intent() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response='# Retrieved Context\n\n- [memory] 用户要求以后所有整理文档类的结果默认放在桌面。')
    service = ContextAssemblyService(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await service.build_for_ceo(
        session=_session(),
        query_text='以后所有整理文档类的结果默认放哪里',
        exposure={
            'skills': [],
            'tool_families': [],
            'tool_names': ['memory_write'],
        },
        persisted_session=None,
    )

    rendered = "\n\n".join(str(item.get('content') or '') for item in result.model_messages)
    assert 'Retrieved Memory Resolution Hint' in rendered
    assert 'Authoritative Retrieved Default' not in rendered
    assert 'restate the retrieved default directly' in rendered
    assert '用户要求以后所有整理文档类的结果默认放在桌面' in rendered
    assert 'authoritative_memory_fact' not in result.trace


@pytest.mark.asyncio
async def test_ceo_context_assembly_adds_memory_write_hint_for_explicit_memory_request() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response='')
    service = ContextAssemblyService(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await service.build_for_ceo(
        session=_session(),
        query_text='Please remember: default to pnpm from now on.',
        exposure={
            'skills': [],
            'tool_families': [],
            'tool_names': ['memory_write'],
        },
        persisted_session=None,
    )

    assert 'Long-Term Memory Write Hint' in result.system_prompt
    assert '`memory_write`' in result.system_prompt
    assert result.trace['memory_write_hint']['triggered'] is True
    assert result.trace['memory_write_hint']['visible'] is True
    assert 'default to' in result.trace['memory_write_hint']['matched_terms']
