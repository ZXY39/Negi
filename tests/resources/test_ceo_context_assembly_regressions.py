from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.runtime.context.assembly import ContextAssemblyService


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

    async def retrieve_block(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.response


def _loop(memory_manager: _MemoryManager) -> SimpleNamespace:
    return SimpleNamespace(
        main_task_service=None,
        memory_manager=memory_manager,
        _use_rag_memory=lambda: True,
        memory_window=100,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
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


def _family(tool_id: str, description: str, *, callable: bool = True, install_dir: str = '') -> SimpleNamespace:
    return SimpleNamespace(
        tool_id=tool_id,
        display_name=tool_id,
        description=description,
        callable=callable,
        install_dir=install_dir,
        actions=[SimpleNamespace(executor_names=[tool_id])],
    )


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
    assert memory_manager.calls[0]['search_context_types'] == ['memory']
    assert memory_manager.calls[0]['allowed_context_types'] == ['memory']
    assert memory_manager.calls[0]['allowed_resource_record_ids'] == []
    assert memory_manager.calls[0]['allowed_skill_record_ids'] == []
    assert result.trace['retrieval_scope']['search_context_types'] == ['memory']


@pytest.mark.asyncio
async def test_ceo_context_assembly_dedupes_prompt_inventory_against_targeted_retrieval() -> None:
    prompt_builder = _PromptBuilder()
    memory_manager = _MemoryManager(response='# Retrieved Context\n\n- [skill:demo_skill] Detailed skill context\n- [resource:tool:external_browser] Detailed tool context')
    service = ContextAssemblyService(loop=_loop(memory_manager), prompt_builder=prompt_builder)

    result = await service.build_for_ceo(
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

    assert prompt_builder.calls == [['other_skill']]
    assert len(memory_manager.calls) == 1
    call = memory_manager.calls[0]
    assert call['search_context_types'] == ['memory', 'skill', 'resource']
    assert call['allowed_context_types'] == ['memory', 'skill', 'resource']
    assert call['allowed_skill_record_ids'] == ['skill:demo_skill']
    assert call['allowed_resource_record_ids'] == ['tool:external_browser']
    assert result.trace['retrieval_scope']['deduped_skill_ids'] == ['demo_skill']
    assert result.trace['retrieval_scope']['deduped_tool_ids'] == ['external_browser']
    assert [item['tool_id'] for item in result.trace['external_tools']] == ['external_search']
