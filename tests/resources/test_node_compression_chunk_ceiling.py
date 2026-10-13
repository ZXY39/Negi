"""分块压缩的容量与规模上限（三条车道共用同一个规划器）。

原先的旋钮是写死的：窗口×0.5 折减、2 万 token 地板、"历史/3"目标次数、放大两轮。
实测副作用是放大轮会把块顶到超窗（估算≈99 万 ⇒ 每块 429,387 tok > 窗口 39 万），
而估算≈390 万时按 /3 只切 3 块、每块 172 万 tok（窗口的 4.4 倍），9 次注定被拒的调用
之后才失败。

现在的合同只有一条派生式（`g3ku/runtime/chunk_compression_plan.py`）：

    容量 = 声明窗口 − 每块信封 − 输出余量(24000)，再被该节点/会话实测跨度 ×0.9 夹一刀
    块数 = 贪心装箱(原子组) 的自然结果，上限 12；容量非正 / 单组超容量 / 块数超上限
           ⇒ 一次都不发，交回拒因

落点各自不同：节点=预检拒发（可恢复暂停 + 心跳点名），会话=`frontdoor_context_window_exceeded`
（带拒因），记忆=不切块，超容量直接带真实原因失败。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from g3ku.runtime.chunk_compression_plan import (
    REFUSAL_CAPACITY_NON_POSITIVE,
    REFUSAL_GROUP_OVER_CAPACITY,
    REFUSAL_OVER_MAX_CHUNKS,
    plan_chunk_groups,
    resolve_chunk_capacity,
)
from main.runtime.react_loop import ReActToolLoop


class _Resp:
    def __init__(self, content: str = '', error_text: str = '') -> None:
        self.content = content
        self.error_text = error_text
        self.usage: dict = {}
        self.attempts: list = []
        self.provider_request_meta: dict = {}
        self.provider_request_body: dict = {}


def _history(n_pairs: int, chars_per_msg: int) -> list[dict]:
    msgs = [
        {'role': 'system', 'content': '你是执行节点。' * 40},
        {'role': 'user', 'content': '任务目标：读完大文件后汇总。' + 'x' * 400},
    ]
    for index in range(n_pairs):
        blob = f'hop-{index}-' + '实测数值与路径记录。' * max(1, chars_per_msg // 10)
        msgs.append({
            'role': 'assistant',
            'content': '',
            'tool_calls': [{
                'id': f'call_{index}',
                'type': 'function',
                'function': {'name': 'exec', 'arguments': json.dumps({'command': f'echo {blob}'}, ensure_ascii=False)},
            }],
        })
        msgs.append({'role': 'tool', 'tool_call_id': f'call_{index}', 'name': 'exec', 'content': 'ok' + 'y' * 200})
    return msgs


def _node_loop(record: list[int], *, fail: bool = False):
    state: dict = {}

    async def fake_chat(*args, messages=None, **kwargs):
        est = state['loop']._estimate_node_compression_message_tokens(list(messages or []))
        record.append(est)
        if fail:
            return _Resp(content='', error_text='HTTP 429: inference exceeds tpm/rpm limit')
        return _Resp(content='【摘要】保留结论与路径。')

    # 分块循环每块前查一次暂停/取消，`_check_pause_or_cancel` 要读 store.get_task；
    # 查不到任务按"不暂停不取消"处理，与生产同语义。
    log_service = SimpleNamespace(_store=SimpleNamespace(get_task=lambda task_id: None))
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service)
    state['loop'] = loop
    loop._chat_with_optional_extensions = fake_chat
    loop._resolved_model_response_timeout_seconds = lambda **_: 30.0
    loop._is_empty_model_response = lambda r: not str(getattr(r, 'content', '') or '').strip()
    loop._node_durable_stage_state = lambda node_id: {}
    loop._node_stage_archive_plan = lambda **_: {}
    loop._node_token_compression_task_goal = lambda **_: '探针任务目标'
    loop._node_compression_must_preserve_records = lambda **_: []

    async def run(messages, window, observed=0):
        return await loop._request_node_token_compression_summary(
            task_id='task:probe',
            node_id='node:probe',
            model_refs=['m'],
            provider_model='openai:m',
            request_messages=messages,
            context_window_tokens=window,
        )

    return loop, run


# ---------------------------------------------------------------------------
# 规划器本体：容量派生 + 三种拒因
# ---------------------------------------------------------------------------


def test_capacity_is_derived_from_window_not_from_a_fixed_target() -> None:
    capacity, source = resolve_chunk_capacity(context_window_tokens=390000, envelope_tokens=5000)
    assert capacity == 390000 - 5000 - 24000
    assert source == 'declared_window'

    # 声明窗口虚高时，实测跨度把容量压回可信范围（下界留一成余量）。
    clamped, clamped_source = resolve_chunk_capacity(
        context_window_tokens=390000, envelope_tokens=5000, observed_span_tokens=262144
    )
    assert clamped == int(262144 * 0.9)
    assert clamped_source == 'observed_request_span'

    tiny, _ = resolve_chunk_capacity(context_window_tokens=25000, envelope_tokens=5000)
    # 输出余量按窗口 10% 取（封顶 24000）：小窗口模型也得切得动，25000−5000−2500。
    assert tiny == 17500


def test_capacity_refuses_when_envelope_alone_eats_the_window() -> None:
    refusal = plan_chunk_groups([10], context_window_tokens=3000, envelope_tokens=5000)
    assert getattr(refusal, 'kind', '') == REFUSAL_CAPACITY_NON_POSITIVE


def test_plan_chunks_counts_follow_capacity() -> None:
    plan = plan_chunk_groups([100000] * 10, context_window_tokens=390000, envelope_tokens=5000)
    assert not hasattr(plan, 'kind'), getattr(plan, 'reason', '')
    assert plan.capacity_tokens == 361000
    assert plan.chunk_count == 4, plan.spans  # 1M/361K ⇒ 4 块，不写目标次数


def test_plan_refuses_when_groups_exceed_max_chunks() -> None:
    refusal = plan_chunk_groups([100000] * 45, context_window_tokens=390000, envelope_tokens=5000)
    assert getattr(refusal, 'kind', '') == REFUSAL_OVER_MAX_CHUNKS
    assert refusal.planned_chunks > 12
    assert 'max 12 chunks' in refusal.reason


def test_plan_refuses_when_single_group_over_capacity() -> None:
    refusal = plan_chunk_groups([800000, 1000], context_window_tokens=390000, envelope_tokens=5000)
    assert getattr(refusal, 'kind', '') == REFUSAL_GROUP_OVER_CAPACITY
    assert 'atomic group' in refusal.reason


# ---------------------------------------------------------------------------
# 节点车道：块不超窗、规模超限一次都不发
# ---------------------------------------------------------------------------


def test_node_chunks_never_exceed_declared_window() -> None:
    sizes: list[int] = []
    _loop, run = _node_loop(sizes)
    window = 390000
    text, payload = asyncio.run(run(_history(219, 4800), window))

    assert payload.get('compression_mode') == 'llm_chunked'
    assert sizes and max(sizes) <= window, f'块请求超窗：max={max(sizes)}'
    assert text


def test_node_huge_history_stays_inside_capacity() -> None:
    """390 万 tok 的历史按容量切 11 块（≤12 上限）：块块都在窗口内，不再产生巨块。"""
    sizes: list[int] = []
    _loop, run = _node_loop(sizes)
    text, payload = asyncio.run(run(_history(420, 10000), 390000))

    assert payload.get('compression_mode') == 'llm_chunked'
    assert 3 < len(sizes) <= 12, sizes
    assert max(sizes) <= 390000, f'块请求超窗：max={max(sizes)}'
    assert text


def test_observed_span_clamps_capacity_and_refuses() -> None:
    """声明窗口 39 万、实测跨度只有 30 万 ⇒ 容量被压到 27 万，块数越过 12 上限即一次都不发。"""
    sizes: list[int] = []
    loop, run = _node_loop(sizes)
    loop._log_service = SimpleNamespace(max_observed_request_span_tokens=lambda task_id, node_id: 300000)

    text, payload = asyncio.run(run(_history(420, 10000), 390000))

    assert sizes == [], f'容量被观测值夹小后应早退：{sizes}'
    assert text == ''
    reason = ReActToolLoop._compression_failure_reason(payload)
    assert reason.startswith('token compression skipped:')
    assert '观测跨度=300000' in reason, reason


def test_node_single_oversized_group_refuses_with_explicit_reason() -> None:
    """一条超大消息（工具参数/内联正文）现在给的是明确拒因，不再是含混的"无可压历史"。"""
    messages = [
        {'role': 'system', 'content': '你是执行节点。'},
        {'role': 'user', 'content': '任务目标：读大文件'},
    ]
    for index in range(20):
        blob = f'hop-{index}-' + '内容。' * 40000  # ≈ 每跳一大块
        messages.append({
            'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': f'call_{index}', 'type': 'function',
                'function': {'name': 'exec', 'arguments': json.dumps({'command': blob})}}],
        })
        messages.append({'role': 'tool', 'tool_call_id': f'call_{index}', 'name': 'exec', 'content': 'ok'})

    sizes: list[int] = []
    loop, run = _node_loop(sizes)
    est = loop._estimate_node_compression_message_tokens(messages)
    if est <= 390000:
        pytest.skip(f'探针没能把请求顶过窗口（实测 {est}）')
    text, payload = asyncio.run(run(messages, 390000))

    reason = ReActToolLoop._compression_failure_reason(payload)
    assert sizes == [] and text == ''
    assert 'token compression skipped:' in reason


# ---------------------------------------------------------------------------
# 会话车道：同一个规划器，拒因落到窗口超限错误
# ---------------------------------------------------------------------------


def _frontdoor_runner(record: list[int]):
    from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
    from g3ku.runtime.frontdoor._ceo_runtime_ops import _estimate_frontdoor_provider_request_tokens

    runner = object.__new__(CreateAgentCeoFrontDoorRunner)

    async def fake_helper(*, messages=None, **kwargs):
        record.append(int(_estimate_frontdoor_provider_request_tokens(
            provider_request_body=None,
            request_messages=list(messages or []),
            tool_schemas=[],
        ) or 0))
        return '【摘要】保留结论与路径。', {'role': 'assistant', 'content': '摘要'}

    async def no_emit(**kwargs):
        return None

    runner._run_frontdoor_compression_helper_request = fake_helper
    runner._emit_frontdoor_runtime_snapshot = no_emit
    return runner


def _frontdoor_history(n_pairs: int, chars_per_msg: int) -> list[dict]:
    msgs: list[dict] = []
    for index in range(n_pairs):
        blob = f'hop-{index}-' + '实测数值与路径记录。' * max(1, chars_per_msg // 10)
        msgs.append({
            'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': f'call_{index}', 'type': 'function',
                'function': {'name': 'exec', 'arguments': json.dumps({'command': f'echo {blob}'}, ensure_ascii=False)}}],
        })
        msgs.append({'role': 'tool', 'tool_call_id': f'call_{index}', 'name': 'exec', 'content': 'ok' + 'y' * 200})
    return msgs


def _frontdoor_run(runner, history, window, state=None, observed_span_tokens=0):
    return asyncio.run(
        runner._chunked_frontdoor_compression_summaries(
            system_prefix=[],
            older_history_messages=history,
            model_refs=['m'],
            context_window_tokens=window,
            state=state if state is not None else {},
            runtime=SimpleNamespace(),
            is_cancelled=None,
            model_info={},
            observed_span_tokens=observed_span_tokens,
        )
    )


def test_frontdoor_chunks_stay_inside_window() -> None:
    sizes: list[int] = []
    runner = _frontdoor_runner(sizes)
    combined, chunk_count, _merge = _frontdoor_run(runner, _frontdoor_history(219, 4800), 390000)
    assert combined
    assert max(sizes) <= 390000, f'会话车道块超窗：max={max(sizes)}'
    assert len(sizes) == chunk_count


def test_frontdoor_observed_span_reader_reads_request_history_usage() -> None:
    runner = _frontdoor_runner([])
    state = {'frontdoor_actual_request_history': [
        {'usage': {'input_tokens': 120000, 'output_tokens': 8000}},
        {'usage': {'input_tokens': 290000, 'output_tokens': 10000}},
    ]}
    assert runner._frontdoor_observed_request_span_tokens(state) == 300000
    assert runner._frontdoor_observed_request_span_tokens({}) == 0


def test_frontdoor_scale_overflow_raises_with_reason_and_no_calls() -> None:
    from g3ku.runtime.frontdoor._ceo_runtime_ops import FrontdoorCompressionRuntimeError

    sizes: list[int] = []
    runner = _frontdoor_runner(sizes)
    # 会话实测跨度 30 万 ⇒ 容量被夹到 27 万，390 万历史要 15 块，越过 12 上限直接报超限。
    with pytest.raises(FrontdoorCompressionRuntimeError) as raised:
        _frontdoor_run(runner, _frontdoor_history(420, 10000), 390000, observed_span_tokens=300000)
    assert sizes == []
    assert raised.value.code == 'frontdoor_context_window_exceeded'
    assert 'token compression skipped' in raised.value.message


# ---------------------------------------------------------------------------
# 记忆车道：不切块，超容量直接带真实原因失败
# ---------------------------------------------------------------------------


def _memory_manager():
    from g3ku.agent import memory_agent_runtime as module
    from g3ku.agent.memory_agent_runtime import MemoryManager

    manager = object.__new__(MemoryManager)
    return manager, module


def test_memory_guard_rejects_oversized_send(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, module = _memory_manager()
    monkeypatch.setattr(
        module, 'resolve_send_model_context_window_info',
        lambda **_: SimpleNamespace(context_window_tokens=390000),
    )
    monkeypatch.setattr(module, 'estimate_runtime_provider_request_preview_tokens', lambda **_: 500000)

    with pytest.raises(module._MemoryProviderError) as raised:
        manager._memory_window_guard(
            runtime_config=SimpleNamespace(),
            model_chain=['mem-1'],
            request_messages=[{'role': 'user', 'content': 'x'}],
            tool_schemas=[],
            usage_total={'input_tokens': 0, 'output_tokens': 0},
            request_artifacts=[],
        )
    assert 'memory send exceeds model window' in str(raised.value)
    assert '容量=366000' in str(raised.value)
    assert raised.value.discard_reason == 'window_refusal'


def test_memory_guard_allows_send_inside_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, module = _memory_manager()
    monkeypatch.setattr(
        module, 'resolve_send_model_context_window_info',
        lambda **_: SimpleNamespace(context_window_tokens=390000),
    )
    monkeypatch.setattr(module, 'estimate_runtime_provider_request_preview_tokens', lambda **_: 100000)

    manager._memory_window_guard(
        runtime_config=SimpleNamespace(),
        model_chain=['mem-1'],
        request_messages=[{'role': 'user', 'content': 'x'}],
        tool_schemas=[],
        usage_total={'input_tokens': 0, 'output_tokens': 0},
        request_artifacts=[],
    )


def test_memory_guard_does_not_clamp_capacity_by_prior_round_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """记忆批次每轮把全部历史重发一遍：拿此前轮次的用量夹容量必死在第 2 轮。

    形状取自实盘停车记录 failed_24378346de28：第 1 轮 provider 回执 输入 7500 /
    缓存 8192 / 输出 1806，第 2 轮估算 17920，而声明窗口 390000 根本装得下。
    """
    manager, module = _memory_manager()
    monkeypatch.setattr(
        module, 'resolve_send_model_context_window_info',
        lambda **_: SimpleNamespace(context_window_tokens=390000),
    )
    monkeypatch.setattr(module, 'estimate_runtime_provider_request_preview_tokens', lambda **_: 17920)

    manager._memory_window_guard(
        runtime_config=SimpleNamespace(),
        model_chain=['mem-1'],
        request_messages=[{'role': 'user', 'content': 'x'}],
        tool_schemas=[],
        usage_total={'input_tokens': 7500, 'output_tokens': 1806, 'cache_read_tokens': 8192},
        request_artifacts=[],
    )


def test_node_chunk_loop_checks_pause_before_every_chunk() -> None:
    """暂停要能在块与块之间生效。

    整个分块循环跑在 `run()` 的一次迭代里，只在迭代开头查一次等于"暂停得等整次压缩跑完"；
    前门那侧本来就是逐块查，这里同形。
    """
    sizes: list[int] = []
    loop, run = _node_loop(sizes)
    checks: list[tuple[str, str]] = []

    def _pause(task_id: str, node_id: str = '') -> None:
        checks.append((task_id, node_id))
        if len(checks) >= 2:
            raise asyncio.CancelledError()

    loop._check_pause_or_cancel = _pause

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run(_history(420, 10000), 390000))

    assert len(checks) == 2, checks
    assert len(sizes) == 1, f'第二块之前就该被拦下：{sizes}'
