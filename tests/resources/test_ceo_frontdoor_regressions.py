from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

import g3ku.shells.web as web_shell
from g3ku.agent.tools.base import Tool
from g3ku.content import ContentNavigationService, parse_content_envelope
from g3ku.integrations.langchain_runtime import ProviderChatModelAdapter
from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.context.assembly import ContextAssemblyService
from g3ku.runtime.context.types import ContextAssemblyResult, RetrievedContextBundle
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime.frontdoor.interaction_trace import new_interaction_trace, record_stage_round, submit_next_stage
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.session.manager import Session, SessionManager
from main.service.runtime_service import CreateAsyncTaskTool
from main.storage.artifact_store import TaskArtifactStore
from main.storage.sqlite_store import SQLiteTaskStore


class _PromptBuilder:
    def build(self, *, skills: list) -> str:
        _ = skills
        return 'BASE PROMPT'


class _AssemblyMemoryManager:
    def __init__(self, response: str = '') -> None:
        self.response = response
        self.retrieve_calls: list[dict[str, object]] = []

    async def retrieve_context_bundle(self, **kwargs):
        self.retrieve_calls.append(dict(kwargs))
        return RetrievedContextBundle(
            query=str(kwargs.get('query') or ''),
            records=(
                [
                    {
                        'record_id': 'memory-1',
                        'context_type': 'memory',
                        'l0': 'remembered fact',
                        'l1': self.response.strip(),
                        'l2_preview': '',
                        'source': 'test',
                        'confidence': 1.0,
                    }
                ]
                if self.response
                else []
            ),
        )


def _assembly_loop(memory_manager: _AssemblyMemoryManager) -> SimpleNamespace:
    return SimpleNamespace(
        main_task_service=None,
        memory_manager=memory_manager,
        _use_rag_memory=lambda: True,
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


def _synthetic_context_message(summary: str) -> dict[str, object]:
    return {
        'role': 'assistant',
        'content': f'## Synthetic Context\n- {summary}',
    }


def test_ceo_memory_write_does_not_count_against_stage_budget() -> None:
    trace = new_interaction_trace()
    trace, _ = submit_next_stage(trace, stage_goal='remember durable preference', tool_round_budget=1)
    trace, round_payload = record_stage_round(
        trace,
        tool_calls=[{'id': 'tool-1', 'name': 'memory_write'}],
        extra_non_budget_tools=CeoFrontDoorRunner._CEO_NON_BUDGET_TOOLS,
    )

    assert round_payload is not None
    assert round_payload['budget_counted'] is False
    assert trace['stages'][0]['tool_rounds_used'] == 0

class _FilesystemTool(Tool):
    def __init__(self, *, description: str) -> None:
        self._description = description

    @property
    def name(self) -> str:
        return 'filesystem'

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, object]:
        return {
            'type': 'object',
            'properties': {
                'path': {'type': 'string'},
            },
            'required': ['path'],
        }

    async def execute(self, path: str, **kwargs) -> str:
        _ = kwargs
        return str(path or '')


def _filesystem_tool(*, description: str) -> _FilesystemTool:
    return _FilesystemTool(description=description)


class _CronTool(Tool):
    @property
    def name(self) -> str:
        return 'cron'

    @property
    def description(self) -> str:
        return 'Schedule reminders and recurring tasks.'

    @property
    def parameters(self) -> dict[str, object]:
        return {
            'type': 'object',
            'properties': {
                'action': {'type': 'string'},
                'job_id': {'type': 'string'},
            },
            'required': ['action'],
        }

    async def execute(self, action: str, job_id: str | None = None, **kwargs) -> str:
        _ = kwargs
        return f'{action}:{job_id or ""}'


def _cron_tool() -> _CronTool:
    return _CronTool()


class _DirectLoadTool(Tool):
    @property
    def name(self) -> str:
        return 'direct_load_tool'

    @property
    def description(self) -> str:
        return 'Return a large direct-load payload.'

    @property
    def parameters(self) -> dict[str, object]:
        return {
            'type': 'object',
            'properties': {},
            'required': [],
        }

    async def execute(self, **kwargs) -> str:
        _ = kwargs
        payload = {
            'ok': True,
            'level': 'l2',
            'content': '\n'.join(f'skill line {index:03d}' for index in range(1, 321)),
            'l0': 'skill short summary',
            'l1': 'skill structured overview',
            'path': '/virtual/full_body_skill.md',
            'uri': 'g3ku://skill/full_body_skill',
        }
        return json.dumps(payload, ensure_ascii=False)


class _RecordingTool(Tool):
    def __init__(self, name: str, sink: list[tuple[str, str]]) -> None:
        self._name = name
        self._sink = sink

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f'record {self._name}'

    @property
    def parameters(self) -> dict[str, object]:
        return {
            'type': 'object',
            'properties': {
                'value': {'type': 'string'},
            },
            'required': ['value'],
        }

    async def execute(self, value: str, **kwargs) -> str:
        _ = kwargs
        self._sink.append((self._name, value))
        return json.dumps({'ok': True, 'tool': self._name, 'value': value}, ensure_ascii=False)


class _CountTool(Tool):
    def __init__(self, sink: list[int]) -> None:
        self._sink = sink

    @property
    def name(self) -> str:
        return 'count_tool'

    @property
    def description(self) -> str:
        return 'record integer counts'

    @property
    def parameters(self) -> dict[str, object]:
        return {
            'type': 'object',
            'properties': {
                'count': {'type': 'integer'},
            },
            'required': ['count'],
        }

    async def execute(self, count: int, **kwargs) -> str:
        _ = kwargs
        self._sink.append(int(count))
        return json.dumps({'ok': True, 'count': int(count)}, ensure_ascii=False)


def test_live_raw_tail_returns_recent_complete_turns_without_compact_history() -> None:
    session = Session(key='web:shared')
    for turn_index in range(1, 7):
        _append_turn(session, turn_index)

    recent_history = web_ceo_sessions.extract_live_raw_tail(session, turn_limit=4)
    assert [item['content'] for item in recent_history[:2]] == ['user turn 3', 'assistant turn 3']
    assert [item['content'] for item in recent_history[-2:]] == ['user turn 6', 'assistant turn 6']
    assert all(
        not str(item.get('content') or '').startswith('[[G3KU_COMPACT_HISTORY_V1]]')
        for item in recent_history
    )


def test_extract_execution_live_raw_tail_falls_back_to_transcript_when_runtime_snapshot_only_has_current_user() -> None:
    persisted_session = Session(key='web:shared')
    _append_turn(persisted_session, 1)
    _append_turn(persisted_session, 2)

    runtime_session = SimpleNamespace(
        inflight_turn_snapshot=lambda: {
            'status': 'running',
            'user_message': {'role': 'user', 'content': 'what was my first message?'},
            'tool_events': [],
        },
        interaction_trace_snapshot=lambda: None,
        current_stage_snapshot=lambda: None,
        paused_execution_context_snapshot=lambda: None,
    )

    recent_history, source = web_ceo_sessions.extract_execution_live_raw_tail(
        runtime_session,
        persisted_session,
        turn_limit=2,
        require_active_stage=False,
    )

    assert source == 'transcript'
    assert [item['content'] for item in recent_history] == [
        'user turn 1',
        'assistant turn 1',
        'user turn 2',
        'assistant turn 2',
    ]


def test_extract_execution_live_raw_tail_prefers_runtime_snapshot_with_tool_progress() -> None:
    persisted_session = Session(key='web:shared')
    _append_turn(persisted_session, 1)
    _append_turn(persisted_session, 2)

    runtime_session = SimpleNamespace(
        inflight_turn_snapshot=lambda: {
            'status': 'running',
            'user_message': {'role': 'user', 'content': 'continue the active work'},
            'tool_events': [
                {
                    'tool_name': 'task_progress',
                    'status': 'success',
                    'text': 'Task status: in_progress',
                }
            ],
        },
        interaction_trace_snapshot=lambda: None,
        current_stage_snapshot=lambda: None,
        paused_execution_context_snapshot=lambda: None,
    )

    recent_history, source = web_ceo_sessions.extract_execution_live_raw_tail(
        runtime_session,
        persisted_session,
        turn_limit=2,
        require_active_stage=False,
    )

    assert source == 'live_runtime'
    assert recent_history[0]['content'] == 'continue the active work'
    assert 'Task status: in_progress' in str(recent_history[1]['content'] or '')


def test_update_ceo_session_after_turn_persists_last_task_memory() -> None:
    session = Session(key='web:shared')
    session.add_message('user', 'open browser')
    session.add_message(
        'assistant',
        'created async task',
        tool_events=[
            {
                'tool_name': 'create_async_task',
                'status': 'success',
                'text': '创建任务成功task:demo123',
            }
        ],
    )

    changed = web_ceo_sessions.update_ceo_session_after_turn(
        session,
        user_text='open browser',
        assistant_text='created async task',
        route_kind='task_dispatch',
    )

    assert changed is True
    assert session.metadata['last_task_memory'] == {
        'version': web_ceo_sessions.TASK_MEMORY_VERSION,
        'task_ids': ['task:demo123'],
        'source': 'transcript',
        'reason': '',
        'updated_at': session.messages[-1]['timestamp'],
        'task_results': [],
    }


def test_update_ceo_session_after_turn_persists_last_task_result_handles() -> None:
    session = Session(key='web:shared')
    session.add_message('user', 'show recommendation result')
    session.add_message(
        'assistant',
        'task finished',
        metadata={
            'source': 'heartbeat',
            'reason': 'task_terminal',
            'task_ids': ['task:demo123'],
            'task_results': [
                {
                    'task_id': 'task:demo123',
                    'node_id': 'node:root',
                    'node_kind': 'execution',
                    'node_reason': 'root_terminal',
                    'output': 'Top 3 recommendation list with rationale',
                    'output_ref': 'artifact:artifact:root-output',
                    'check_result': 'accepted',
                }
            ],
        },
    )

    changed = web_ceo_sessions.update_ceo_session_after_turn(
        session,
        user_text='show recommendation result',
        assistant_text='task finished',
        route_kind='direct_reply',
    )

    assert changed is True
    assert session.metadata['last_task_memory'] == {
        'version': web_ceo_sessions.TASK_MEMORY_VERSION,
        'task_ids': ['task:demo123'],
        'source': 'heartbeat',
        'reason': 'task_terminal',
        'updated_at': session.messages[-1]['timestamp'],
        'task_results': [
            {
                'task_id': 'task:demo123',
                'node_id': 'node:root',
                'node_kind': 'execution',
                'node_reason': 'root_terminal',
                'output_excerpt': 'Top 3 recommendation list with rationale',
                'output_ref': 'artifact:artifact:root-output',
                'check_result': 'accepted',
            }
        ],
    }


def test_extract_live_raw_tail_preserves_heartbeat_and_compact_traces() -> None:
    session = Session(key='web:shared')
    _append_turn(session, 1)
    session.add_message('user', 'user turn 2')
    session.add_message(
        'assistant',
        'assistant turn 2',
        tool_events=[
            {'tool_name': 'submit_next_stage', 'status': 'success', 'text': 'stage created'},
            {
                'tool_name': 'task_progress',
                'status': 'success',
                'text': 'Task status: in_progress\n(node:abc,in_progress,working on task:trace-1)',
            },
        ],
        interaction_trace={
            'stages': [
                {
                    'status': 'completed',
                    'stage_goal': 'Inspect task:trace-1 and summarize the latest runtime state for follow-up.',
                    'tool_rounds_used': 1,
                }
            ]
        },
    )
    _append_turn(session, 3)
    session.add_message(
        'assistant',
        'task:trace-1 failed in heartbeat',
        metadata={'source': 'heartbeat', 'reason': 'task_terminal', 'task_ids': ['task:trace-1']},
    )

    recent_history = web_ceo_sessions.extract_live_raw_tail(session, turn_limit=2)

    assert [item['content'] for item in recent_history[:3]] == [
        'user turn 2',
        (
            'assistant turn 2\n'
            'Recent tool results:\n'
            '- task_progress (success): Task status: in_progress (node:abc,in_progress,working on task:trace-1)\n'
            'Stage snapshot:\n'
            '- status: completed\n'
            '- goal: Inspect task:trace-1 and summarize the latest runtime state for follow-up.\n'
            '- tool_rounds_used: 1'
        ),
        'user turn 3',
    ]
    assert recent_history[-1]['role'] == 'assistant'
    assert recent_history[-1]['content'] == (
        'task:trace-1 failed in heartbeat\n'
        'Task metadata:\n'
        '- task_ids: task:trace-1\n'
        '- source: heartbeat\n'
        '- reason: task_terminal'
    )


def test_extract_live_raw_tail_skips_internal_cron_user_prompts() -> None:
    session = Session(key='web:shared')
    _append_turn(session, 1)
    session.add_message(
        'user',
        'internal cron prompt',
        metadata={'cron_internal': True, 'cron_job_id': 'job-77'},
    )
    session.add_message(
        'assistant',
        'scheduled progress update',
        metadata={'source': 'cron', 'cron_job_id': 'job-77'},
    )
    _append_turn(session, 2)

    recent_history = web_ceo_sessions.extract_live_raw_tail(session, turn_limit=2)
    contents = [item['content'] for item in recent_history]
    assert 'internal cron prompt' not in contents
    assert any(str(item).startswith('scheduled progress update') for item in contents)


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

    assert 'frontdoor_context' not in fallback_result.trace
    assert [item['content'] for item in fallback_result.recent_history[:2]] == ['user turn 3', 'assistant turn 3']
    assert [item['content'] for item in fallback_result.recent_history[-2:]] == ['user turn 6', 'assistant turn 6']
    assert fallback_result.trace['model_messages_count'] == 10


@pytest.mark.asyncio
async def test_context_assembly_includes_compact_task_memory_message() -> None:
    persisted_session = Session(key='web:shared')
    persisted_session.add_message('user', 'open browser')
    persisted_session.add_message(
        'assistant',
        'created async task',
        tool_events=[
            {
                'tool_name': 'create_async_task',
                'status': 'success',
                'text': '创建任务成功task:ctx-1',
            }
        ],
    )
    web_ceo_sessions.update_ceo_session_after_turn(
        persisted_session,
        user_text='open browser',
        assistant_text='created async task',
        route_kind='task_dispatch',
    )

    prompt_builder = _PromptBuilder()
    memory_manager = _AssemblyMemoryManager(response='')
    service = ContextAssemblyService(loop=_assembly_loop(memory_manager), prompt_builder=prompt_builder)

    result = await service.build_for_ceo(
        session=_session_state(),
        query_text='why did the task fail',
        exposure={'skills': [], 'tool_families': [], 'tool_names': []},
        persisted_session=persisted_session,
    )

    assert any(
        '## Task Continuity' in str(item.get('content') or '')
        for item in result.recent_history
    )


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
    assert 'frontdoor_context' not in persisted_session.metadata
    assert persisted_session.metadata['last_preview_text'] == 'assistant reply'
    assert all(
        not str(message.get('content') or '').startswith('[[G3KU_COMPACT_HISTORY_V1]]')
        for message in persisted_session.messages
    )


class _FakeToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in list(tools)}
        self.runtime_contexts: list[dict[str, object]] = []

    def get(self, name: str):
        return self._tools.get(str(name or '').strip())

    def push_runtime_context(self, context: dict[str, object]):
        self.runtime_contexts.append(dict(context))
        return object()

    def pop_runtime_context(self, token) -> None:
        _ = token

class _BackendRecorder:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.responses.pop(0)


class _AsyncTaskService:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    async def startup(self) -> None:
        return None

    def find_reusable_continuation_task(self, *, session_id: str, continuation_of_task_id: str):
        if session_id == 'web:shared' and continuation_of_task_id == 'task:old-1':
            return SimpleNamespace(task_id='task:cont-1')
        return None

    async def create_task(self, task: str, *, session_id: str = 'web:shared', max_depth: int | None = None, **kwargs):
        self.created.append(
            {
                'task': task,
                'session_id': session_id,
                'max_depth': max_depth,
                'kwargs': kwargs,
            }
        )
        return SimpleNamespace(task_id='task:new-1')


class _ErrorTool(Tool):
    @property
    def name(self) -> str:
        return "error_tool"

    @property
    def description(self) -> str:
        return "Return an error-shaped result"

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        _ = kwargs
        return "Error: simulated tool failure"


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_binds_session_stable_prompt_cache_key(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    current_assembly = {
        'result': ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[
                    _synthetic_context_message('summary one'),
                {'role': 'user', 'content': 'recent user'},
                {'role': 'assistant', 'content': 'recent assistant'},
            ],
            tool_names=['filesystem'],
            trace={},
        )
    }
    backend = _BackendRecorder([LLMResponse(content='done', finish_reason='stop') for _ in range(6)])
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

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

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
    first_key = str(backend.calls[0]['prompt_cache_key'])
    second_key = str(backend.calls[1]['prompt_cache_key'])
    assert first_key == second_key

    current_assembly['result'] = ContextAssemblyResult(
        system_prompt='SYSTEM PROMPT',
        recent_history=[
            _synthetic_context_message('summary two changed'),
            {'role': 'user', 'content': 'recent user'},
            {'role': 'assistant', 'content': 'recent assistant'},
        ],
        tool_names=['filesystem'],
        trace={},
    )
    await runner.run_turn(user_input=user_input, session=session)
    third_key = str(backend.calls[2]['prompt_cache_key'])
    assert third_key == first_key

    current_assembly['result'] = ContextAssemblyResult(
        system_prompt='SYSTEM PROMPT',
        recent_history=[
            _synthetic_context_message('summary one'),
            {'role': 'user', 'content': 'recent user'},
            {'role': 'assistant', 'content': 'recent assistant'},
        ],
        tool_names=['filesystem'],
        trace={},
    )
    loop.tools = _FakeToolRegistry([_filesystem_tool(description='Read and write files from disk')])
    await runner.run_turn(user_input=user_input, session=session)
    fourth_key = str(backend.calls[3]['prompt_cache_key'])
    assert fourth_key == first_key

    other_session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:other'),
        _memory_channel='web',
        _memory_chat_id='other',
        _channel='web',
        _chat_id='other',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )
    await runner.run_turn(user_input=user_input, session=other_session)
    fifth_key = str(backend.calls[4]['prompt_cache_key'])
    assert fifth_key != first_key

    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-other'])
    await runner.run_turn(user_input=user_input, session=session)
    sixth_key = str(backend.calls[5]['prompt_cache_key'])
    assert sixth_key != first_key


@pytest.mark.asyncio
async def test_ceo_frontdoor_stage_overlay_moves_to_tail_without_changing_prefix(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    backend = _BackendRecorder(
        [
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='stage-1',
                        name='submit_next_stage',
                        arguments={'stage_goal': 'load skill guidance', 'tool_round_budget': 1},
                    )
                ],
                finish_reason='tool_calls',
            ),
            LLMResponse(content='done', finish_reason='stop'),
        ]
    )
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
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[{'role': 'user', 'content': 'recent user'}],
            tool_names=['filesystem'],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    await runner.run_turn(user_input=SimpleNamespace(content='current question'), session=session)

    assert len(backend.calls) == 2
    first_prefix = str((backend.calls[0]['messages'][0] or {}).get('content') or '')
    second_prefix = str((backend.calls[1]['messages'][0] or {}).get('content') or '')
    assert first_prefix == second_prefix == 'SYSTEM PROMPT'
    first_tail = str((backend.calls[0]['messages'][-1] or {}).get('content') or '')
    second_tail = str((backend.calls[1]['messages'][-1] or {}).get('content') or '')
    assert first_tail != second_tail
    assert 'System note for this turn only:' in first_tail
    assert 'System note for this turn only:' in second_tail


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_uses_cron_internal_system_message_and_cron_only_tools(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    backend = _BackendRecorder([LLMResponse(content='done', finish_reason='stop')])
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools=_FakeToolRegistry([
            _filesystem_tool(description='Read files from disk'),
            _cron_tool(),
        ]),
        max_iterations=12,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {'skills': [], 'tool_families': [], 'tool_names': ['filesystem', 'cron']}

    async def _build_for_ceo(*, session, query_text: str, exposure, persisted_session):
        _ = session, query_text, exposure, persisted_session
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['filesystem', 'cron'],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )
    user_input = SimpleNamespace(
        content='检查当前发布状态',
        metadata={
            'cron_internal': True,
            'cron_job_id': 'job-77',
            'cron_stop_condition': '发布完成后或用户要求取消',
            'cron_stop_condition_explicit': True,
        },
    )

    output = await runner.run_turn(user_input=user_input, session=session)

    assert output == 'done'
    tools = backend.calls[0]['tools']
    assert [item['function']['name'] for item in tools] == ['cron']
    messages = backend.calls[0]['messages']
    assert messages[0] == {'role': 'system', 'content': 'SYSTEM PROMPT'}
    assert messages[1]['role'] == 'system'
    assert 'Current cron job id: job-77' in str(messages[1]['content'])
    assert 'Exit condition: 发布完成后或用户要求取消' in str(messages[1]['content'])
    assert all('execution_policy.mode' not in str(message.get('content') or '') for message in messages)


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_exposes_ordinary_tools_before_first_stage(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    backend = _BackendRecorder([LLMResponse(content='done', finish_reason='stop')])
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
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['filesystem'],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    await runner.run_turn(user_input=SimpleNamespace(content='use tools if needed'), session=session)

    tool_names = [
        str(((item or {}).get('function') or {}).get('name') or '').strip()
        for item in list(backend.calls[0].get('tools') or [])
    ]
    assert 'submit_next_stage' in tool_names
    assert 'filesystem' in tool_names


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_keeps_direct_load_tool_result_inline(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    class _MainTaskService:
        def __init__(self, *, content_store) -> None:
            self.log_service = SimpleNamespace(_content_store=content_store)

        async def startup(self) -> None:
            return None

    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    content_store = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    backend = _BackendRecorder(
        [
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='stage-1',
                        name='submit_next_stage',
                        arguments={'stage_goal': 'load direct tool context', 'tool_round_budget': 2},
                    )
                ],
                finish_reason='tool_calls',
            ),
            LLMResponse(
                content='',
                tool_calls=[ToolCallRequest(id='tool-1', name='direct_load_tool', arguments={})],
                finish_reason='tool_calls',
            ),
            LLMResponse(content='done', finish_reason='stop'),
        ]
    )
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=_MainTaskService(content_store=content_store),
        tools=_FakeToolRegistry([_DirectLoadTool()]),
        max_iterations=12,
        resource_manager=None,
        tool_execution_manager=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {'skills': [], 'tool_families': [], 'tool_names': ['direct_load_tool']}

    async def _build_for_ceo(*, session, query_text: str, exposure, persisted_session):
        _ = session, query_text, exposure, persisted_session
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['direct_load_tool'],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    try:
        output = await runner.run_turn(user_input=SimpleNamespace(content='load tool context'), session=session)

        assert output == 'done'
        assert len(backend.calls) == 3
        tool_messages = [
            item
            for item in list(backend.calls[2].get('messages') or [])
            if (
                str(item.get('role') or '').strip() == 'tool'
                and str(item.get('name') or '').strip() == 'direct_load_tool'
            )
        ]
        assert len(tool_messages) == 1
        assert parse_content_envelope(tool_messages[0]['content']) is None
        payload = json.loads(str(tool_messages[0]['content']))
        assert payload['uri'] == 'g3ku://skill/full_body_skill'
        assert payload['content'].startswith('skill line 001')
    finally:
        store.close()


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_reuses_existing_continuation_task_when_user_rebuilds(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    backend = _BackendRecorder(
        [
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='stage-1',
                        name='submit_next_stage',
                        arguments={'stage_goal': 'reuse active continuation task', 'tool_round_budget': 1},
                    )
                ],
                finish_reason='tool_calls',
            ),
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='tool-1',
                        name='create_async_task',
                        arguments={
                            'task': '继续完成失败任务，不要从零开始',
                            'core_requirement': '继续完成打开网页的自动化流程',
                            'execution_policy': {'mode': 'focus'},
                            'continuation_of_task_id': 'task:old-1',
                        },
                    )
                ],
                finish_reason='tool_calls',
            ),
            LLMResponse(content='已沿用进行中的续跑任务。', finish_reason='stop'),
        ]
    )
    async_task_service = _AsyncTaskService()
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=async_task_service,
        tools=_FakeToolRegistry([CreateAsyncTaskTool(async_task_service)]),
        max_iterations=12,
        resource_manager=None,
        tool_execution_manager=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {'skills': [], 'tool_families': [], 'tool_names': ['create_async_task']}

    async def _build_for_ceo(*, session, query_text: str, exposure, persisted_session):
        _ = session, query_text, exposure, persisted_session
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[
                {
                    'role': 'assistant',
                    'content': (
                        '## Task Continuity\n'
                        '### Active Tasks\n'
                        '- `task:cont-1`: continuation_of_task_id=task:old-1; status=in_progress; updated_at=2026-03-28T10:00:00+08:00'
                    ),
                }
            ],
            tool_names=['create_async_task'],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content='重建任务，继续完成'), session=session)

    assert output == '已沿用进行中的续跑任务。'
    assert async_task_service.created == []


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_blocks_final_text_when_stage_budget_is_exhausted(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    backend = _BackendRecorder(
        [
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='stage-1',
                        name='submit_next_stage',
                        arguments={'stage_goal': 'inspect files', 'tool_round_budget': 1},
                    )
                ],
                finish_reason='tool_calls',
            ),
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='tool-1',
                        name='filesystem',
                        arguments={'path': 'README.md'},
                    )
                ],
                finish_reason='tool_calls',
            ),
            LLMResponse(
                content='因为本阶段预算已耗尽，所以我先停在这里。',
                finish_reason='stop',
            ),
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='stage-2',
                        name='submit_next_stage',
                        arguments={'stage_goal': 'summarize findings', 'tool_round_budget': 1},
                    )
                ],
                finish_reason='tool_calls',
            ),
            LLMResponse(content='done after next stage', finish_reason='stop'),
        ]
    )
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
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['filesystem'],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content='use tools if needed'), session=session)

    assert output == 'done after next stage'
    assert len(backend.calls) == 5
    blocked_messages = list(backend.calls[3].get('messages') or [])
    assert any(
        str(item.get('role') or '') == 'user'
        and 'Do not finish yet.' in str(item.get('content') or '')
        and 'submit_next_stage' in str(item.get('content') or '')
        for item in blocked_messages
    )
    trace = getattr(session, '_interaction_trace', None)
    assert trace is not None
    stages = list(trace.get('stages') or [])
    assert [stage.get('stage_id') for stage in stages] == ['ceo-stage-1', 'ceo-stage-2']
    assert stages[0]['tool_round_budget'] == 1
    assert stages[0]['tool_rounds_used'] == 1
    assert stages[1]['tool_round_budget'] == 1
    assert stages[1]['status'] == 'completed'


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_allows_stage_setup_only_text_without_retry(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    backend = _BackendRecorder(
        [
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='stage-1',
                        name='submit_next_stage',
                        arguments={'stage_goal': 'open bilibili homepage', 'tool_round_budget': 2},
                    )
                ],
                finish_reason='tool_calls',
            ),
            LLMResponse(
                content='现在已切换到新阶段，继续直接打开哔哩哔哩首页。',
                finish_reason='stop',
            ),
        ]
    )
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
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['filesystem'],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content='open bilibili'), session=session)

    assert output == '现在已切换到新阶段，继续直接打开哔哩哔哩首页。'
    assert len(backend.calls) == 2
    trace = getattr(session, '_interaction_trace', None)
    assert trace is not None
    stages = list(trace.get('stages') or [])
    assert len(stages) == 1
    assert stages[0]['tool_rounds_used'] == 0
    assert stages[0]['status'] == 'completed'


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_emits_tool_error_after_trace_sync(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    error_tool = _ErrorTool()
    tool_registry = _FakeToolRegistry([error_tool])
    backend = _BackendRecorder(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="stage-1",
                        name="submit_next_stage",
                        arguments={"stage_goal": "trigger tool failure", "tool_round_budget": 1},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="tool-1",
                        name="error_tool",
                        arguments={},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="stage-2",
                        name="submit_next_stage",
                        arguments={"stage_goal": "wrap up after failure", "tool_round_budget": 1},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools=tool_registry,
        max_iterations=12,
        resource_manager=None,
        tool_execution_manager=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["error_tool"]}

    async def _build_for_ceo(*, session, query_text: str, exposure, persisted_session):
        _ = session, query_text, exposure, persisted_session
        return ContextAssemblyResult(
            system_prompt="SYSTEM PROMPT",
            recent_history=[],
            tool_names=["error_tool"],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_chat_backend", lambda: backend)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])

    class _Session:
        def __init__(self) -> None:
            self.state = SimpleNamespace(session_key="web:shared")
            self._memory_channel = "web"
            self._memory_chat_id = "shared"
            self._channel = "web"
            self._chat_id = "shared"
            self._active_cancel_token = None
            self._interaction_trace = None
            self.trace_snapshots: list[dict[str, object] | None] = []

        def inflight_turn_snapshot(self):
            return None

        def set_interaction_trace(self, trace, *, stage=None) -> None:
            self._interaction_trace = copy.deepcopy(trace)
            self._current_stage = copy.deepcopy(stage)
            self.trace_snapshots.append(copy.deepcopy(trace))

    session = _Session()
    observed_error_trace: list[dict[str, object] | None] = []

    async def _on_progress(content: str, *, event_kind: str | None = None, event_data=None, **kwargs) -> None:
        _ = content, event_data, kwargs
        if event_kind == "tool_error":
            observed_error_trace.append(copy.deepcopy(session._interaction_trace))

    output = await runner.run_turn(
        user_input=SimpleNamespace(content="do the failing thing"),
        session=session,
        on_progress=_on_progress,
    )

    assert output == "done"
    assert observed_error_trace
    trace = observed_error_trace[-1]
    assert trace is not None
    stage = list(trace.get("stages") or [])[0]
    tool = list(stage.get("rounds") or [])[0]["tools"][0]
    assert tool["tool_name"] == "error_tool"
    assert tool["status"] == "error"
    assert tool["output_text"] == "Error: simulated tool failure"


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_passes_session_task_defaults_into_runtime_context(monkeypatch, tmp_path) -> None:
    class _CaptureRuntimeTool(Tool):
        def __init__(self) -> None:
            self.seen_runtime: dict[str, object] | None = None

        @property
        def name(self) -> str:
            return 'capture_runtime'

        @property
        def description(self) -> str:
            return 'capture runtime'

        @property
        def parameters(self) -> dict[str, object]:
            return {
                'type': 'object',
                'properties': {'value': {'type': 'string'}},
                'required': ['value'],
            }

        async def execute(self, value: str, __g3ku_runtime: dict[str, object] | None = None, **kwargs) -> str:
            runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
            self.seen_runtime = dict(runtime)
            return value

    async def _noop_ready() -> None:
        return None

    capture_tool = _CaptureRuntimeTool()
    tool_registry = _FakeToolRegistry([capture_tool])
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools=tool_registry,
        max_iterations=12,
        resource_manager=None,
        tool_execution_manager=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    persisted_session = loop.sessions.get_or_create('web:shared')
    persisted_session.metadata['task_defaults'] = {'max_depth': 4}

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {'skills': [], 'tool_families': [], 'tool_names': ['capture_runtime']}

    async def _build_for_ceo(*, session, query_text: str, exposure, persisted_session):
        _ = session, query_text, exposure, persisted_session
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['capture_runtime'],
            trace={},
        )

    backend = _BackendRecorder(
        [
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='stage-1',
                        name='submit_next_stage',
                        arguments={'stage_goal': 'capture runtime', 'tool_round_budget': 1},
                    )
                ],
                finish_reason='tool_calls',
            ),
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='tool-1',
                        name='capture_runtime',
                        arguments={'value': 'ok'},
                    )
                    ],
                    finish_reason='tool_calls',
                ),
                LLMResponse(content='done', finish_reason='stop'),
                LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='stage-2',
                            name='submit_next_stage',
                            arguments={'stage_goal': 'finish runtime capture', 'tool_round_budget': 1},
                        )
                    ],
                    finish_reason='tool_calls',
                ),
                LLMResponse(content='done', finish_reason='stop'),
            ]
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    await runner.run_turn(user_input=SimpleNamespace(content='dispatch work'), session=session)

    assert loop.tools.runtime_contexts[-1]['task_defaults'] == {'max_depth': 4}
    assert 'recommended_execution_policy' not in loop.tools.runtime_contexts[-1]


@pytest.mark.asyncio
@pytest.mark.xfail(reason='CEO frontdoor no longer uses LangChain create_agent', strict=False)
async def test_ceo_frontdoor_runner_uses_last_non_empty_ai_message_when_final_ai_message_is_empty(monkeypatch, tmp_path) -> None:
    def _fake_create_agent(*, model, tools, checkpointer, store, name, middleware=()):
        _ = model, tools, checkpointer, store, name, middleware

        class _Agent:
            async def ainvoke(self, payload, config=None):
                _ = payload, config
                return {
                    'messages': [
                        AIMessage(content='我来访问。'),
                        AIMessage(content=''),
                    ]
                }

        return _Agent()

    async def _noop_ready() -> None:
        return None

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
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['filesystem'],
            trace={},
        )

    monkeypatch.setattr('g3ku.runtime.frontdoor.ceo_runner.create_agent', _fake_create_agent)
    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_ceo_model_client', lambda: (_FakeModelClient(), ['openai_codex:gpt-test']))

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content='你来访问'), session=session)

    assert output == '我来访问。'
    assert getattr(session, '_last_route_kind', '') == 'direct_reply'


@pytest.mark.asyncio
@pytest.mark.xfail(reason='CEO frontdoor no longer uses LangChain create_agent', strict=False)
async def test_ceo_frontdoor_runner_returns_visible_fallback_when_all_ai_messages_are_empty(monkeypatch, tmp_path) -> None:
    def _fake_create_agent(*, model, tools, checkpointer, store, name, middleware=()):
        _ = model, tools, checkpointer, store, name, middleware

        class _Agent:
            async def ainvoke(self, payload, config=None):
                _ = payload, config
                return {'messages': [AIMessage(content='')]}

        return _Agent()

    async def _noop_ready() -> None:
        return None

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
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['filesystem'],
            trace={},
        )

    monkeypatch.setattr('g3ku.runtime.frontdoor.ceo_runner.create_agent', _fake_create_agent)
    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_ceo_model_client', lambda: (_FakeModelClient(), ['openai_codex:gpt-test']))

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content='你来访问'), session=session)

    assert '没有生成可展示的回复' in output
    assert '你来访问' in output
    assert getattr(session, '_last_route_kind', '') == 'direct_reply'


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_uses_stable_prompt_cache_key_without_langchain(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    current_assembly = {
        'result': ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[
                    _synthetic_context_message('summary one'),
                {'role': 'user', 'content': 'recent user'},
                {'role': 'assistant', 'content': 'recent assistant'},
            ],
            tool_names=['filesystem'],
            trace={},
        )
    }
    backend = _BackendRecorder([LLMResponse(content='done', finish_reason='stop') for _ in range(4)])
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

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

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
    first_key = str(backend.calls[0]['prompt_cache_key'])
    second_key = str(backend.calls[1]['prompt_cache_key'])
    assert first_key == second_key

    other_session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:other'),
        _memory_channel='web',
        _memory_chat_id='other',
        _channel='web',
        _chat_id='other',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )
    await runner.run_turn(user_input=user_input, session=other_session)
    third_key = str(backend.calls[2]['prompt_cache_key'])
    assert third_key != first_key


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_passes_session_task_defaults_into_runtime_context_without_langchain(monkeypatch, tmp_path) -> None:
    class _CaptureRuntimeTool(Tool):
        def __init__(self) -> None:
            self.seen_runtime: dict[str, object] | None = None

        @property
        def name(self) -> str:
            return 'capture_runtime'

        @property
        def description(self) -> str:
            return 'capture runtime'

        @property
        def parameters(self) -> dict[str, object]:
            return {
                'type': 'object',
                'properties': {'value': {'type': 'string'}},
                'required': ['value'],
            }

        async def execute(self, value: str, __g3ku_runtime: dict[str, object] | None = None, **kwargs) -> str:
            runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
            self.seen_runtime = dict(runtime)
            return value

    async def _noop_ready() -> None:
        return None

    capture_tool = _CaptureRuntimeTool()
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools=_FakeToolRegistry([capture_tool]),
        max_iterations=12,
        resource_manager=None,
        tool_execution_manager=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)
    persisted_session = loop.sessions.get_or_create('web:shared')
    persisted_session.metadata['task_defaults'] = {'max_depth': 4}
    loop.sessions.save(persisted_session)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {'skills': [], 'tool_families': [], 'tool_names': ['capture_runtime']}

    async def _build_for_ceo(*, session, query_text: str, exposure, persisted_session):
        _ = session, query_text, exposure, persisted_session
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['capture_runtime'],
            trace={},
        )

    backend = _BackendRecorder(
        [
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='stage-1',
                        name='submit_next_stage',
                        arguments={'stage_goal': 'capture runtime', 'tool_round_budget': 1},
                    )
                ],
                finish_reason='tool_calls',
            ),
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='tool-1',
                        name='capture_runtime',
                        arguments={'value': 'ok'},
                    )
                    ],
                    finish_reason='tool_calls',
                ),
                LLMResponse(content='done', finish_reason='stop'),
                LLMResponse(
                    content='',
                    tool_calls=[
                        ToolCallRequest(
                            id='stage-2',
                            name='submit_next_stage',
                            arguments={'stage_goal': 'finish runtime capture', 'tool_round_budget': 1},
                        )
                    ],
                    finish_reason='tool_calls',
                ),
                LLMResponse(content='done', finish_reason='stop'),
            ]
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    await runner.run_turn(user_input=SimpleNamespace(content='dispatch work'), session=session)

    assert loop.tools.runtime_contexts[-1]['task_defaults'] == {'max_depth': 4}


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_returns_direct_reply_without_langchain(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

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
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['filesystem'],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: _BackendRecorder([LLMResponse(content='我来访问。', finish_reason='stop')]))
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content='你来访问'), session=session)

    assert output == '我来访问。'
    assert getattr(session, '_last_route_kind', '') == 'direct_reply'


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_executes_xml_tool_call_directly_without_repair(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    executed: list[tuple[str, str]] = []
    backend = _BackendRecorder(
        [
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='stage-1',
                        name='submit_next_stage',
                        arguments={'stage_goal': 'repair xml tool call', 'tool_round_budget': 2},
                    )
                ],
                finish_reason='tool_calls',
            ),
            LLMResponse(
                content='<minimax:tool_call><invoke name="record_tool"><parameter name="value">alpha</parameter></invoke></minimax:tool_call>',
                tool_calls=[],
                finish_reason='stop',
            ),
            LLMResponse(content='repair succeeded', finish_reason='stop'),
        ]
    )
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools=_FakeToolRegistry([_RecordingTool('record_tool', executed)]),
        max_iterations=12,
        resource_manager=None,
        tool_execution_manager=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {'skills': [], 'tool_families': [], 'tool_names': ['record_tool']}

    async def _build_for_ceo(*, session, query_text: str, exposure, persisted_session):
        _ = session, query_text, exposure, persisted_session
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['record_tool'],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content='repair xml please'), session=session)

    assert output == 'repair succeeded'
    assert executed == [('record_tool', 'alpha')]
    assert len(backend.calls) == 3
    assert not any(
        'XML-style pseudo tool calling' in str(item.get('content') or '')
        for item in list(backend.calls[2].get('messages') or [])
    )


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_repairs_xml_tool_call_via_json_payload_after_local_extraction_fails(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    executed: list[int] = []
    backend = _BackendRecorder(
        [
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='stage-1',
                        name='submit_next_stage',
                        arguments={'stage_goal': 'repair xml tool call', 'tool_round_budget': 2},
                    )
                ],
                finish_reason='tool_calls',
            ),
            LLMResponse(
                content='<minimax:tool_call><invoke name="count_tool"><parameter name="count">oops</parameter></invoke></minimax:tool_call>',
                tool_calls=[],
                finish_reason='stop',
            ),
            LLMResponse(
                content='{"name":"count_tool","arguments":{"count":2}}',
                tool_calls=[],
                finish_reason='stop',
            ),
            LLMResponse(content='repair succeeded', finish_reason='stop'),
        ]
    )
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools=_FakeToolRegistry([_CountTool(executed)]),
        max_iterations=12,
        resource_manager=None,
        tool_execution_manager=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {'skills': [], 'tool_families': [], 'tool_names': ['count_tool']}

    async def _build_for_ceo(*, session, query_text: str, exposure, persisted_session):
        _ = session, query_text, exposure, persisted_session
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['count_tool'],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content='repair xml please'), session=session)

    assert output == 'repair succeeded'
    assert executed == [2]
    assert len(backend.calls) == 4
    repair_messages = list(backend.calls[2].get('messages') or [])
    assert any('XML-style pseudo tool calling' in str(item.get('content') or '') for item in repair_messages)
    assert any('must be an integer' in str(item.get('content') or '') for item in repair_messages)


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_fails_safely_after_three_xml_repair_attempts(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    backend = _BackendRecorder(
        [
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='stage-1',
                        name='submit_next_stage',
                        arguments={'stage_goal': 'repair xml tool call', 'tool_round_budget': 2},
                    )
                ],
                finish_reason='tool_calls',
            ),
            LLMResponse(
                content='<minimax:tool_call><invoke name="count_tool"><parameter name="count">oops</parameter></invoke></minimax:tool_call>',
                tool_calls=[],
                finish_reason='stop',
            ),
            LLMResponse(content='still invalid', tool_calls=[], finish_reason='stop'),
            LLMResponse(
                content='<minimax:tool_call><invoke name="count_tool"><parameter name="count">oops</parameter></invoke></minimax:tool_call>',
                tool_calls=[],
                finish_reason='stop',
            ),
        ]
    )
    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools=_FakeToolRegistry([_CountTool([])]),
        max_iterations=12,
        resource_manager=None,
        tool_execution_manager=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {'skills': [], 'tool_families': [], 'tool_names': ['count_tool']}

    async def _build_for_ceo(*, session, query_text: str, exposure, persisted_session):
        _ = session, query_text, exposure, persisted_session
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['count_tool'],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content='repair xml please'), session=session)

    assert 'XML pseudo tool-call repair failed 3 consecutive times' in output
    assert '<minimax:tool_call>' not in output
    assert len(backend.calls) == 4
    trace = getattr(session, '_interaction_trace', None)
    assert trace is not None
    assert list(trace.get('stages') or [])[0]['status'] == 'failed'


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_still_uses_persisted_session_history_when_checkpointer_exists(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    session_manager = SessionManager(tmp_path)
    persisted_session = session_manager.get_or_create('web:shared')
    persisted_session.add_message('user', 'first question')
    persisted_session.add_message('assistant', 'first answer')
    session_manager.save(persisted_session)

    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=session_manager,
        _checkpointer=object(),
        _store=None,
        main_task_service=None,
        tools=_FakeToolRegistry([_filesystem_tool(description='Read files from disk')]),
        max_iterations=12,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    captured: dict[str, object] = {}

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {'skills': [], 'tool_families': [], 'tool_names': ['filesystem']}

    async def _build_for_ceo(*, session, query_text: str, exposure, persisted_session):
        _ = session, query_text, exposure
        captured['persisted_session'] = persisted_session
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['filesystem'],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: _BackendRecorder([LLMResponse(content='done', finish_reason='stop')]))
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content='follow up'), session=session)

    assert output == 'done'
    assert captured['persisted_session'] is not None


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_does_not_short_circuit_to_authoritative_memory_without_model_call(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    backend = _BackendRecorder([LLMResponse(content='请按正常前门流程继续处理。', finish_reason='stop')])
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
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['filesystem'],
            trace={'authoritative_memory_fact': '用户要求以后所有整理文档类的结果默认放在桌面。'},
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(
        user_input=SimpleNamespace(content='你理解错了，我的意思是以后所有整理文档类的结果默认放哪里'),
        session=session,
    )

    assert output == '请按正常前门流程继续处理。'
    assert len(backend.calls) == 1
    assert getattr(session, '_last_route_kind', '') == 'direct_reply'


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


def test_build_completed_stage_abstracts_keeps_stage_goal_and_status() -> None:
    session = Session(key='web:shared')
    _append_turn(session, 1)

    trace = {
        'stages': [
            {
                'stage_id': 'ceo-stage-1',
                'stage_index': 1,
                'status': 'completed',
                'stage_goal': 'stage only handoff',
                'tool_round_budget': 1,
                'tool_rounds_used': 0,
                'rounds': [],
            }
        ]
    }
    abstracts = web_ceo_sessions.build_completed_stage_abstracts(trace)

    assert abstracts[0].startswith('Stage 1')
    assert 'Goal: stage only handoff' in abstracts[0]
    assert 'Status: completed' in abstracts[0]


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_retries_empty_stage_turn_and_returns_structured_message(monkeypatch, tmp_path) -> None:
    async def _noop_ready() -> None:
        return None

    backend = _BackendRecorder(
        [
            LLMResponse(
                content='',
                tool_calls=[
                    ToolCallRequest(
                        id='stage-1',
                        name='submit_next_stage',
                        arguments={'stage_goal': 'load skill guidance', 'tool_round_budget': 1},
                    )
                ],
                finish_reason='tool_calls',
            ),
            LLMResponse(content='', finish_reason='stop'),
            LLMResponse(content='', finish_reason='stop'),
        ]
    )
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
        return ContextAssemblyResult(
            system_prompt='SYSTEM PROMPT',
            recent_history=[],
            tool_names=['filesystem'],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, 'resolve_for_actor', _resolve_for_actor)
    monkeypatch.setattr(runner._assembly, 'build_for_ceo', _build_for_ceo)
    monkeypatch.setattr(runner, '_resolve_chat_backend', lambda: backend)
    monkeypatch.setattr(runner, '_resolve_ceo_model_refs', lambda: ['openai_codex:gpt-test'])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key='web:shared'),
        _memory_channel='web',
        _memory_chat_id='shared',
        _channel='web',
        _chat_id='shared',
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )

    output = await runner.run_turn(user_input=SimpleNamespace(content='install a skill'), session=session)

    assert '空响应' in output
    assert '尚未创建异步任务' in output
    assert len(backend.calls) == 3
    retry_messages = list(backend.calls[2].get('messages') or [])
    assert any(
        str(item.get('role') or '') == 'user'
        and 'previous model turn was empty' in str(item.get('content') or '')
        for item in retry_messages
    )
    assert getattr(session, '_last_route_kind', '') == 'stage_only'
    trace = getattr(session, '_interaction_trace', None)
    assert trace is not None
    assert list(trace.get('stages') or [])[0]['status'] == 'failed'
