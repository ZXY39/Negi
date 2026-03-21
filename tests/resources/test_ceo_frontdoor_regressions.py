from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

import g3ku.shells.web as web_shell
from g3ku.integrations.langchain_runtime import ProviderChatModelAdapter
from g3ku.providers.base import LLMResponse
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.context.assembly import ContextAssemblyService
from g3ku.runtime.context.types import ContextAssemblyResult
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.session.manager import Session, SessionManager


class _PromptBuilder:
    def build(self, *, skills: list) -> str:
        _ = skills
        return 'BASE PROMPT'


class _AssemblyMemoryManager:
    def __init__(self, response: str = '') -> None:
        self.response = response
        self.retrieve_calls: list[dict[str, object]] = []

    async def retrieve_block(self, **kwargs):
        self.retrieve_calls.append(dict(kwargs))
        return self.response


def _assembly_loop(memory_manager: _AssemblyMemoryManager) -> SimpleNamespace:
    return SimpleNamespace(
        main_task_service=None,
        memory_manager=memory_manager,
        _use_rag_memory=lambda: True,
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                archive_summary_top_k=2,
                archive_summary_max_tokens=320,
                skill_inventory_top_k=8,
                skill_inventory_max_tokens=480,
                extension_tool_top_k=6,
                core_tools=[],
            )
        ),
    )


def _session_state() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
    )


def _append_turn(session: Session, turn_index: int) -> None:
    session.add_message('user', f'user turn {turn_index}')
    session.add_message('assistant', f'assistant turn {turn_index}')


def _frontdoor_summary_message(summary: str) -> dict[str, object]:
    return {
        'role': 'assistant',
        'content': (
            f"{web_ceo_sessions.FRONTDOOR_COMPACT_HISTORY_PREFIX}\n"
            f'{{"kind":"frontdoor_context","summary":"{summary}","summary_turn_count":2,"raw_tail_turns":4}}'
        ),
    }


def _filesystem_tool(*, description: str) -> StructuredTool:
    def _run(path: str) -> str:
        return path

    return StructuredTool.from_function(_run, name='filesystem', description=description)


def test_frontdoor_context_resolution_falls_back_then_uses_metadata() -> None:
    session = Session(key='web:shared')
    for turn_index in range(1, 7):
        _append_turn(session, turn_index)

    context, source = web_ceo_sessions.resolve_frontdoor_context(session)

    assert source == 'fallback'
    assert context['summary_turn_count'] == 2
    assert context['raw_tail_turns'] == 4
    assert 'user turn 1' in context['summary_text']
    assert 'assistant turn 2' in context['summary_text']
    assert 'user turn 5' not in context['summary_text']
    assert 'assistant turn 6' not in context['summary_text']

    compact_message = web_ceo_sessions.build_frontdoor_compact_history_message(context)
    assert compact_message is not None
    assert str(compact_message['content']).startswith(web_ceo_sessions.FRONTDOOR_COMPACT_HISTORY_PREFIX)

    recent_history = web_ceo_sessions.extract_frontdoor_recent_history(session, raw_tail_turns=4)
    assert [item['content'] for item in recent_history[:2]] == ['user turn 3', 'assistant turn 3']
    assert [item['content'] for item in recent_history[-2:]] == ['user turn 6', 'assistant turn 6']

    session.metadata['frontdoor_context'] = context
    resolved_again, second_source = web_ceo_sessions.resolve_frontdoor_context(session)
    assert second_source == 'metadata'
    assert resolved_again == context


@pytest.mark.asyncio
async def test_context_assembly_uses_frontdoor_summary_and_recent_tail() -> None:
    persisted_session = Session(key='web:shared')
    for turn_index in range(1, 7):
        _append_turn(persisted_session, turn_index)

    prompt_builder = _PromptBuilder()
    memory_manager = _AssemblyMemoryManager(response='')
    service = ContextAssemblyService(loop=_assembly_loop(memory_manager), prompt_builder=prompt_builder)

    fallback_result = await service.build_for_ceo(
        session=_session_state(),
        query_text='recap our earlier discussion',
        exposure={'skills': [], 'tool_families': [], 'tool_names': []},
        persisted_session=persisted_session,
    )

    assert fallback_result.trace['frontdoor_context']['source'] == 'fallback'
    assert fallback_result.recent_history[0]['role'] == 'assistant'
    assert str(fallback_result.recent_history[0]['content']).startswith(web_ceo_sessions.FRONTDOOR_COMPACT_HISTORY_PREFIX)
    assert [item['content'] for item in fallback_result.recent_history[1:3]] == ['user turn 3', 'assistant turn 3']
    assert [item['content'] for item in fallback_result.recent_history[-2:]] == ['user turn 6', 'assistant turn 6']

    persisted_session.metadata['frontdoor_context'] = web_ceo_sessions.build_frontdoor_context(
        persisted_session,
        raw_tail_turns=4,
        route_kind='direct_reply',
    )
    metadata_result = await service.build_for_ceo(
        session=_session_state(),
        query_text='recap our earlier discussion',
        exposure={'skills': [], 'tool_families': [], 'tool_names': []},
        persisted_session=persisted_session,
    )

    assert metadata_result.trace['frontdoor_context']['source'] == 'metadata'
    assert metadata_result.trace['frontdoor_context']['summary_turn_count'] == 2
    assert metadata_result.trace['recent_history_count'] == 9


class _IngestRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ingest_turn(self, **kwargs) -> None:
        self.calls.append(dict(kwargs))


class _MultiAgentRunner:
    async def run_turn(self, *, user_input, session, on_progress=None) -> str:
        _ = user_input, on_progress
        setattr(session, '_last_route_kind', 'direct_reply')
        return 'assistant reply'


@pytest.mark.asyncio
async def test_runtime_agent_session_prompt_keeps_rag_ingest_payload_raw(tmp_path, monkeypatch) -> None:
    async def _noop_refresh(*, force: bool = False, reason: str = '') -> None:
        _ = force, reason
        return None

    async def _noop_cancel(session_key: str) -> None:
        _ = session_key
        return None

    monkeypatch.setattr(web_shell, 'refresh_web_agent_runtime', _noop_refresh)
    monkeypatch.setattr(web_ceo_sessions, 'workspace_path', lambda: tmp_path)

    memory_manager = _IngestRecorder()
    session_manager = SessionManager(tmp_path)
    loop = SimpleNamespace(
        model='gpt-test',
        reasoning_effort=None,
        multi_agent_runner=_MultiAgentRunner(),
        sessions=session_manager,
        memory_manager=memory_manager,
        _use_rag_memory=lambda: True,
        prompt_trace=False,
        commit_service=None,
        create_session_cancellation_token=lambda session_key: SimpleNamespace(cancel=lambda reason=None: None),
        release_session_cancellation_token=lambda session_key, token: None,
        cancel_session_tasks=_noop_cancel,
    )
    runtime_session = RuntimeAgentSession(loop, session_key='web:shared', channel='web', chat_id='shared')

    result = await runtime_session.prompt('what changed?')

    assert result.output == 'assistant reply'
    assert memory_manager.calls == [
        {
            'session_key': 'web:shared',
            'channel': 'web',
            'chat_id': 'shared',
            'messages': [
                {'role': 'user', 'content': 'what changed?'},
                {'role': 'assistant', 'content': 'assistant reply'},
            ],
        }
    ]

    persisted_session = session_manager.get_or_create('web:shared')
    assert persisted_session.get_history() == [
        {'role': 'user', 'content': 'what changed?'},
        {'role': 'assistant', 'content': 'assistant reply'},
    ]
    assert len(persisted_session.messages) == 2
    assert persisted_session.metadata['frontdoor_context']['last_route_kind'] == 'direct_reply'
    assert all(
        not str(message.get('content') or '').startswith(web_ceo_sessions.FRONTDOOR_COMPACT_HISTORY_PREFIX)
        for message in persisted_session.messages
    )


class _FakeToolRegistry:
    def __init__(self, tools: list[StructuredTool]) -> None:
        self._tools = list(tools)

    def get(self, name: str):
        _ = name
        return None

    def push_runtime_context(self, context: dict[str, object]):
        _ = context
        return object()

    def pop_runtime_context(self, token) -> None:
        _ = token

    def to_langchain_tools_filtered(self, allowed_names):
        _ = allowed_names
        return list(self._tools)


class _FakeModelClient:
    def __init__(self, default_model: str = 'openai_codex/gpt-test') -> None:
        self.default_model = default_model
        self.bind_calls: list[dict[str, object]] = []

    def bind(self, **kwargs):
        self.bind_calls.append(dict(kwargs))
        return SimpleNamespace(default_model=self.default_model, bound_kwargs=dict(kwargs))


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_binds_stable_prompt_cache_key(monkeypatch, tmp_path) -> None:
    create_agent_calls: list[dict[str, object]] = []
    agent_invocations: list[dict[str, object]] = []

    def _fake_create_agent(*, model, tools, checkpointer, store, name, middleware=()):
        create_agent_calls.append(
            {
                'model': model,
                'tools': tools,
                'checkpointer': checkpointer,
                'store': store,
                'name': name,
                'middleware': middleware,
            }
        )

        class _Agent:
            async def ainvoke(self, payload, config=None):
                agent_invocations.append({'payload': payload, 'config': config})
                return {'messages': [AIMessage(content='done')]}

        return _Agent()

    async def _noop_ready() -> None:
        return None

    current_assembly = {
        'result': ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[
                _frontdoor_summary_message('summary one'),
                {'role': 'user', 'content': 'recent user'},
                {'role': 'assistant', 'content': 'recent assistant'},
            ],
            tool_names=['filesystem'],
            trace={},
        )
    }
    fake_model = _FakeModelClient()
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools=_FakeToolRegistry([_filesystem_tool(description='Read files from disk')]),
        max_iterations=12,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {'skills': [], 'tool_families': [], 'tool_names': ['filesystem']}

    async def _build_for_ceo(*, session, query_text: str, exposure, persisted_session):
        _ = session, query_text, exposure, persisted_session
        return current_assembly['result']

    monkeypatch.setattr('g3ku.runtime.frontdoor.ceo_runner.create_agent', _fake_create_agent)
    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_ceo_model_client', lambda: (fake_model, ['openai_codex:gpt-test']))

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )
    user_input = SimpleNamespace(content='current question')

    await runner.run_turn(user_input=user_input, session=session)
    await runner.run_turn(user_input=user_input, session=session)
    first_key = str(fake_model.bind_calls[0]['prompt_cache_key'])
    second_key = str(fake_model.bind_calls[1]['prompt_cache_key'])
    assert first_key == second_key

    current_assembly['result'] = ContextAssemblyResult(
        system_prompt='SYSTEM PROMPT',
        recent_history=[
            _frontdoor_summary_message('summary two changed'),
            {'role': 'user', 'content': 'recent user'},
            {'role': 'assistant', 'content': 'recent assistant'},
        ],
        tool_names=['filesystem'],
        trace={},
    )
    await runner.run_turn(user_input=user_input, session=session)
    third_key = str(fake_model.bind_calls[2]['prompt_cache_key'])
    assert third_key != first_key

    current_assembly['result'] = ContextAssemblyResult(
        system_prompt='SYSTEM PROMPT',
        recent_history=[
            _frontdoor_summary_message('summary one'),
            {'role': 'user', 'content': 'recent user'},
            {'role': 'assistant', 'content': 'recent assistant'},
        ],
        tool_names=['filesystem'],
        trace={},
    )
    loop.tools = _FakeToolRegistry([_filesystem_tool(description='Read and write files from disk')])
    await runner.run_turn(user_input=user_input, session=session)
    fourth_key = str(fake_model.bind_calls[3]['prompt_cache_key'])
    assert fourth_key != first_key
    assert create_agent_calls[0]['name'] == 'g3ku_ceo_frontdoor'
    assert agent_invocations[0]['payload']['messages'][0]['role'] == 'system'


class _ProviderRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        return LLMResponse(content='ok', finish_reason='stop')


@pytest.mark.asyncio
async def test_provider_chat_model_adapter_forwards_prompt_cache_key() -> None:
    provider = _ProviderRecorder()
    adapter = ProviderChatModelAdapter(
        provider=provider,
        default_model='gpt-test',
        default_temperature=0.2,
        default_max_tokens=128,
    )

    await adapter._agenerate([HumanMessage(content='hello')], prompt_cache_key='stable-frontdoor-key')

    assert provider.calls[0]['prompt_cache_key'] == 'stable-frontdoor-key'
