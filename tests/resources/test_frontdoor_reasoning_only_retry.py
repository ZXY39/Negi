"""收尾跳「只思考、不产出」这条车道的重问契约。

实盘起点：会话 `ext:qq-official-1903529517:f8a8001865631301` 的 task_terminal 心跳轮，
最后一跳 `output_tokens == thinking_tokens`（941/941）、零 `text_delta`、零工具调用、
`finish_reason=stop`。空响应重放道按设计把 `reasoning_content` 算非空所以不重放，
回合直接落到 `_empty_response_explanation()` 那句英文并投递给了用户。

契约：这一跳重问**一次**，提醒行引用上一跳的思考原文（那段思考不进历史，不引用等于
让模型从零再推）；二次仍然只想不写就交出空正文 + 结构化标记，由收口侧处置——
用户轮按既有规则静默，心跳轮直接落固定文案，不再自开修复轮。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.core.messages import UserInputMessage
from g3ku.heartbeat.session_service import WebSessionHeartbeatService
from g3ku.providers.fallback import ModelProviderResponseError
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.session.manager import SessionManager

_REASONING = 'The report exists on disk, so summarize the findings for the user now.'
_EMPTY_EXPLANATION = 'The turn completed without visible assistant text'


def _state(**response) -> dict[str, object]:
    payload = {
        'content': '',
        'tool_calls': [],
        'finish_reason': 'stop',
        'reasoning_content': _REASONING,
    }
    payload.update(response)
    return {
        'response_payload': payload,
        'route_kind': 'direct_reply',
        'used_tools': ['filesystem_stat', 'show_sticker_category'],
        'verified_task_ids': [],
    }


def _state_after_one_retry() -> dict[str, object]:
    state = _state()
    state['reasoning_only_retry_count'] = 1
    return state


@pytest.mark.asyncio
async def test_reasoning_only_hop_reasks_once_with_quoted_reasoning() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    result = await runner._graph_normalize_model_output(_state(), runtime=SimpleNamespace())

    assert result['next_step'] == 'call_model'
    assert result['final_output'] == ''
    assert result['reasoning_only_retry_count'] == 1
    overlay = str(result['repair_overlay_text'] or '')
    assert _REASONING in overlay
    assert 'internal reasoning only' in overlay
    # 提醒必须给指代物：只说"要产出内容"而不交回那段思考，等于让它从零再推一遍。
    assert 'Do not repeat that reasoning' in overlay


@pytest.mark.asyncio
async def test_second_reasoning_only_hop_finalizes_empty_and_marks_exhausted() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    result = await runner._graph_normalize_model_output(
        _state_after_one_retry(), runtime=SimpleNamespace()
    )

    assert result['next_step'] == 'finalize'
    assert result['reasoning_only_retry_exhausted'] is True
    # 不再替模型编一条交付物：空正文才是收口侧能正确处置的形状。
    assert _EMPTY_EXPLANATION not in str(result['final_output'])


@pytest.mark.asyncio
async def test_quoted_reasoning_is_bounded() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    result = await runner._graph_normalize_model_output(
        _state(reasoning_content='x' * 4600), runtime=SimpleNamespace()
    )

    overlay = str(result['repair_overlay_text'] or '')
    assert '...[truncated]' in overlay
    assert len(overlay) < 5000


@pytest.mark.asyncio
async def test_encrypted_reasoning_still_reasks_without_a_quote() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    # Responses 车道的项可能只有密文：抄不进提醒，但形状照样是"只想不写"，不能放弃重问。
    result = await runner._graph_normalize_model_output(
        _state(
            reasoning_content=None,
            reasoning_items=[{'id': 'rs_1', 'encrypted_content': 'ENC1', 'g3ku_reasoning_model': 'm'}],
        ),
        runtime=SimpleNamespace(),
    )

    assert result['next_step'] == 'call_model'
    overlay = str(result['repair_overlay_text'] or '')
    assert 'internal reasoning only' in overlay
    assert '"""' not in overlay
    assert 'ENC1' not in overlay


@pytest.mark.asyncio
async def test_responses_lane_reasoning_summary_is_quoted() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    result = await runner._graph_normalize_model_output(
        _state(
            reasoning_content=None,
            reasoning_items=[{'id': 'rs_1', 'summary': [{'text': 'Concluded the report is ready'}]}],
        ),
        runtime=SimpleNamespace(),
    )

    assert result['next_step'] == 'call_model'
    assert 'Concluded the report is ready' in str(result['repair_overlay_text'])


@pytest.mark.asyncio
async def test_visible_text_finalizes_without_reask() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    result = await runner._graph_normalize_model_output(
        _state(content='报告已经交付，结论如下。'), runtime=SimpleNamespace()
    )

    assert result['next_step'] == 'finalize'
    assert result['final_output'] == '报告已经交付，结论如下。'
    assert 'reasoning_only_retry_count' not in result


@pytest.mark.asyncio
async def test_tool_call_hop_finalizes_normally_and_clears_the_counter() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    state = _state_after_one_retry()
    state['response_payload'] = {
        'content': '',
        'tool_calls': [
            {
                'id': 'call_1',
                'type': 'function',
                'function': {'name': 'task_progress', 'arguments': '{"task_id":"task:demo"}'},
            }
        ],
        'finish_reason': 'tool_calls',
        'reasoning_content': _REASONING,
    }
    for key in ('visible_tool_names', 'provider_tool_names', 'candidate_tool_names', 'tool_names'):
        state[key] = ['task_progress']

    result = await runner._graph_normalize_model_output(state, runtime=SimpleNamespace())

    assert result['next_step'] == 'review_tool_calls'
    # 有任何一次正常推进就把计数清零：跨推进累计会让后面真正需要重问的那一跳直接判死。
    assert result['reasoning_only_retry_count'] == 0


@pytest.mark.asyncio
async def test_response_without_any_reasoning_keeps_the_existing_fallthrough() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    result = await runner._graph_normalize_model_output(
        _state(reasoning_content=''), runtime=SimpleNamespace()
    )

    assert result['next_step'] == 'finalize'
    # 零思考零正文那一族归空响应重放道管，本刀不接管、不叠加。
    assert _EMPTY_EXPLANATION in str(result['final_output'])
    assert 'reasoning_only_retry_exhausted' not in result


@pytest.mark.asyncio
async def test_provider_error_response_still_raises_before_the_reask() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    with pytest.raises(ModelProviderResponseError):
        await runner._graph_normalize_model_output(
            _state(finish_reason='error', error_text='HTTP 429: tpm exhausted'),
            runtime=SimpleNamespace(),
        )


class _Registry:
    def __init__(self) -> None:
        self._seq: dict[str, int] = {}
        self.published: list[tuple[str, dict[str, object]]] = []

    async def subscribe_ceo(self, session_id: str):
        _ = session_id
        return None

    async def subscribe_global_ceo(self):
        return None

    async def unsubscribe_ceo(self, session_id: str, queue) -> None:
        _ = session_id, queue

    async def unsubscribe_global_ceo(self, queue) -> None:
        _ = queue

    def next_ceo_seq(self, session_id: str) -> int:
        key = str(session_id or '')
        self._seq[key] = self._seq.get(key, 0) + 1
        return self._seq[key]

    def publish_ceo(self, session_id: str, envelope: dict[str, object]) -> None:
        self.published.append((str(session_id or ''), dict(envelope)))

    def publish_global_ceo(self, envelope: dict[str, object]) -> None:
        self.published.append(('global', dict(envelope)))


class _TaskService:
    def __init__(self) -> None:
        self.registry = _Registry()
        self.delivered: list[tuple[str, str]] = []
        self.store = SimpleNamespace(mark_task_terminal_outbox_delivered=self._mark)
        self.tasks: dict[str, object] = {}
        self.node_details: dict[tuple[str, str], dict[str, object]] = {}

    def _mark(self, dedupe_key: str, *, delivered_at: str) -> None:
        self.delivered.append((str(dedupe_key or ''), str(delivered_at or '')))

    def get_task(self, task_id: str):
        return self.tasks.get(str(task_id or '').strip())

    def get_node_detail_payload(self, task_id: str, node_id: str):
        return self.node_details.get((str(task_id or ''), str(node_id or '')))


class _RuntimeManager:
    def __init__(self, session) -> None:
        self._session = session

    def get_or_create(self, **kwargs):
        _ = kwargs
        return self._session


class _FakeSession:
    """按跑次吐出 `RunResult` 形状的假会话，用来数修复轮到底进没进。"""

    def __init__(self, *, results: list[SimpleNamespace]) -> None:
        self.state = SimpleNamespace(status='idle', is_running=False)
        self.prompts: list[UserInputMessage] = []
        self._listeners = set()
        self._results = list(results)
        self.turn_id = 'turn-reasoning-only-retry'

    def subscribe(self, listener):
        self._listeners.add(listener)

        def _unsubscribe() -> None:
            self._listeners.discard(listener)

        return _unsubscribe

    async def prompt(self, user_message, persist_transcript: bool = False) -> SimpleNamespace:
        self.prompts.append(user_message)
        if self._results:
            return self._results.pop(0)
        return SimpleNamespace(output='', is_silent_reply=False)


def _service(tmp_path, session_id: str, task_id: str, live: _FakeSession) -> tuple[WebSessionHeartbeatService, _TaskService]:
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)

    task_service = _TaskService()
    task_service.tasks[task_id] = SimpleNamespace(
        task_id=task_id,
        root_node_id='node:root',
        metadata={},
        final_output='报告正文',
        final_output_ref='',
        failure_reason='',
    )
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live),
        main_task_service=task_service,
        session_manager=session_manager,
    )
    service._started = True
    assert service.enqueue_task_terminal_payload(
        {
            'task_id': task_id,
            'session_id': session_id,
            'title': '研究两个插件实现原理',
            'status': 'success',
            'brief_text': 'done',
            'failure_reason': '',
            'finished_at': '2026-10-31T03:24:03+08:00',
            'dedupe_key': f'task-terminal:{task_id}:success:2026-10-31T03:24:03+08:00',
        }
    ) is True
    return service, task_service


async def test_frontdoor_reask_exhausted_lands_fixed_text_without_repair_rounds(tmp_path) -> None:
    task_id = 'task:reask-exhausted'
    live = _FakeSession(
        results=[SimpleNamespace(output='', is_silent_reply=False, reasoning_only_reask_exhausted=True)]
    )
    session_id = 'web:ceo-reasoning-only-exhausted'
    service, task_service = _service(tmp_path, session_id, task_id, live)

    await service._run_session(session_id)

    # 前门已经带着思考引用重问过一轮；这里不再把整份请求体重打 5 次。
    assert len(live.prompts) == 1
    finals = [
        envelope
        for _, envelope in task_service.registry.published
        if str(envelope.get('type') or '') == 'ceo.reply.final'
    ]
    assert finals, '固定文案必须真的投递给用户'
    text = str((finals[0].get('data') or {}).get('text') or '')
    assert '整理该结果时连续失败' in text
    assert task_id in text
    assert _EMPTY_EXPLANATION not in text


async def test_empty_reply_without_the_flag_still_runs_repair_rounds(tmp_path) -> None:
    task_id = 'task:plain-empty'
    live = _FakeSession(results=[SimpleNamespace(output='', is_silent_reply=False) for _ in range(8)])
    session_id = 'web:ceo-plain-empty-reply'
    service, _task_service = _service(tmp_path, session_id, task_id, live)

    await service._run_session(session_id)

    # 对照臂：没有那颗标记时，既有的有界修复轮一条不少——本刀没有改它。
    assert len(live.prompts) > 1
