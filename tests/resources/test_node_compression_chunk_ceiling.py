"""分块压缩的容量与规模上限（三条车道共用同一个规划器）。

原先的旋钮是写死的：窗口×0.5 折减、2 万 token 地板、"历史/3"目标次数、放大两轮。
实测副作用是放大轮会把块顶到超窗（估算≈99 万 ⇒ 每块 429,387 tok > 窗口 39 万），
而估算≈390 万时按 /3 只切 3 块、每块 172 万 tok（窗口的 4.4 倍），9 次注定被拒的调用
之后才失败。

现在的合同（`g3ku/runtime/chunk_compression_plan.py`）：

    容量 = 声明窗口 − 每块信封 − 输出余量(24000)，再被该节点/会话实测跨度 ×0.9 夹一刀
    单发还是分块 = append-only 单发请求体（同一估算器实测）+ 输出余量 ≤ 声明窗口
    块数 = 贪心装箱(原子组) 的自然结果，上限 12；一跳内最多 2 轮，溢出块在第二轮接着压
    单组超容量 ⇒ 头尾收成能装箱的形状；收不动也照发
    只有容量非正（窗口连信封都装不下）才一次都不发

落点各自不同：节点=预检拒发（可恢复暂停 + 心跳点名），会话=放弃这一 pass、原请求体照发，
记忆=不切块，超容量直接带真实原因失败。
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


def _node_loop(record: list[int], *, fail: bool = False, calls: list | None = None,
               fail_chunk_indexes: set[int] | None = None):
    state: dict = {}

    async def fake_chat(*args, messages=None, **kwargs):
        est = state['loop']._estimate_node_compression_message_tokens(list(messages or []))
        record.append(est)
        if calls is not None:
            calls.append(list(messages or []))
        if fail:
            return _Resp(content='', error_text='HTTP 429: inference exceeds tpm/rpm limit')
        if fail_chunk_indexes is not None:
            for item in list(messages or []):
                text = str(item.get('content') or '')
                if '"chunk_index"' in text:
                    if int(json.loads(text).get('chunk_index') or 0) in fail_chunk_indexes:
                        return _Resp(content='', error_text='HTTP 429: inference exceeds tpm/rpm limit')
                    break
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


def test_observed_span_clamps_capacity_and_defers_overflow_within_two_rounds() -> None:
    """声明窗口 39 万、实测跨度只有 12 万 ⇒ 容量被压到 10.8 万，块数远超 12 上限。

    旧处置是"一次都不发、整次压缩报拒因"；现在第一轮压满 12 块、第二轮接着收溢出块，
    还剩下的原样留在请求里，由下一跳按那时的窗口重算——规模问题不再变成错误。
    """
    sizes: list[int] = []
    loop, run = _node_loop(sizes)
    loop._log_service = SimpleNamespace(
        _store=SimpleNamespace(get_task=lambda task_id: None),
        max_observed_request_span_tokens=lambda task_id, node_id: 120000,
    )

    text, payload = asyncio.run(run(_history(420, 10000), 390000))

    assert payload.get('compression_mode') == 'llm_chunked'
    assert payload.get('capacity_source') == 'observed_request_span'
    assert int(payload.get('capacity_tokens') or 0) == int(120000 * 0.9)
    assert len(sizes) > 12, f'溢出块应在本跳第二轮继续压：{len(sizes)}'
    assert int(payload.get('compression_pass_rounds') or 0) == 2
    assert int(payload.get('deferred_message_count') or 0) > 0, '压不完的原始历史必须报数'
    assert payload.get('deferred_raw_messages'), '留在请求里的原始历史要交回调用方'
    assert '下一跳按那时的窗口继续压缩' in text, '摘要要明写还有原文在场'
    assert max(sizes) <= 390000, f'块请求超窗：max={max(sizes)}'


def test_node_oversized_group_is_head_tail_truncated_and_still_compressed() -> None:
    """一条超大工具结果单独超过块容量：收成头尾两段照样压，而不是整次压缩拒发。"""
    messages = [
        {'role': 'system', 'content': '你是执行节点。'},
        {'role': 'user', 'content': '任务目标：读大文件'},
    ]
    for index in range(30):
        # 一条 150 万字的工具结果 ≈ 37.5 万 tok：单颗原子组就超过 39 万窗口的块容量
        # （≈36.3 万），其余各 ≈9 千 tok 把总量顶过窗口，逼出分块 + 截断两档。
        blob = '内容。' * 500_000 if index == 3 else '内容。' * 4_000
        messages.append({
            'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': f'call_{index}', 'type': 'function',
                'function': {'name': 'exec', 'arguments': json.dumps({'command': f'echo hop-{index}'})}}],
        })
        messages.append({'role': 'tool', 'tool_call_id': f'call_{index}', 'name': 'exec', 'content': blob})

    sizes: list[int] = []
    calls: list[list[dict]] = []
    loop, run = _node_loop(sizes, calls=calls)
    est = loop._estimate_node_compression_message_tokens(messages)
    assert est > 390000, f'探针没能把请求顶过窗口（实测 {est}），这条用例就是空的'

    text, payload = asyncio.run(run(messages, 390000))

    assert payload.get('truncation_group_count') or 0 >= 1, '单组超容量要进截断档'
    assert text and sizes, '收成头尾两段后照样压，不再交回拒因'
    assert 'token compression skipped' not in str(payload.get('chunk_refused_reason') or '')
    assert any('中段省略' in json.dumps(item, ensure_ascii=False, default=str) for item in calls), (
        '超容量的那块正文必须被收成头尾两段'
    )


def test_node_failed_chunk_ships_the_committed_prefix_and_keeps_the_rest_raw() -> None:
    """第 2 块没拿到摘要：第 1 块照常交付，第 2 块及其后的原文交回调用方留在请求里。

    旧行为是在摘要里插一行"本块未取得摘要"却把原文一起吞掉，等于谎报收口；而 helper
    空响应整次作废又会让下一跳把已压好的块重烧一遍。
    """
    sizes: list[int] = []
    loop, run = _node_loop(sizes, fail_chunk_indexes={2})
    history = _history(219, 4800)

    text, payload = asyncio.run(run(history, 390000))

    assert payload.get('compression_mode') == 'llm_chunked'
    assert payload.get('chunk_failed_indexes') == [2], payload.get('chunk_failed_indexes')
    assert text.count('[分块摘要 1/') == 1
    assert '未取得摘要' not in text, f'停下的那块不许留缺口行：{text[:200]}'
    deferred = list(payload.get('deferred_raw_messages') or [])
    assert deferred and len(deferred) == int(payload.get('deferred_message_count') or 0)
    assert len(deferred) < len(history) - 4, '压中的部分必须真的不在场'
    assert '下一跳按那时的窗口继续压缩' in text
    assert any(item.get('tool_calls') for item in deferred), '留在请求里的仍是完整工具调用组'


# ---------------------------------------------------------------------------
# 会话车道：同一个规划器，溢出块在本跳第二轮继续压
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
        return '【摘要】保留结论与路径。', {'role': 'assistant', 'content': '摘要'}, ''

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
    """从前门车道的两个入口喂：先规划（`_frontdoor_plan_token_compression_pass`），再执行。

    单发请求体读数按"整段前缀 + 历史 + 一条指令"给，与生产的 append-only 形状同构，
    这样规划器算出的信封是真的。
    """
    from g3ku.runtime.tool_history import iter_compaction_atomic_groups

    grouped = [
        (list(group), runner._estimate_frontdoor_compression_body_tokens(group))
        for group in iter_compaction_atomic_groups(history)
    ]
    single_shot = [*history, {'role': 'user', 'content': '【上下文压缩指令】'}]
    compression_pass = runner._frontdoor_plan_token_compression_pass(
        grouped_history=grouped,
        single_shot_request_tokens=runner._estimate_frontdoor_compression_body_tokens(single_shot),
        context_window_tokens=window,
        observed_span_tokens=observed_span_tokens,
    )
    return asyncio.run(
        runner._chunked_frontdoor_compression_summaries(
            grouped_history=grouped,
            compression_pass=compression_pass,
            model_refs=['m'],
            state=state if state is not None else {},
            runtime=SimpleNamespace(),
            is_cancelled=None,
            model_info={},
        )
    )


def test_frontdoor_chunks_stay_inside_window() -> None:
    sizes: list[int] = []
    runner = _frontdoor_runner(sizes)
    combined, chunk_count, _merge, _failed, _rounds, deferred, committed, _provider_error = _frontdoor_run(
        runner, _frontdoor_history(219, 4800), 390000
    )
    assert combined
    assert max(sizes) <= 390000, f'会话车道块超窗：max={max(sizes)}'
    assert len(sizes) == chunk_count == committed
    assert deferred == [], f'块数没超上限就不该留下原始历史：{len(deferred)}'


def test_frontdoor_observed_span_reader_reads_request_history_usage() -> None:
    runner = _frontdoor_runner([])
    state = {'frontdoor_actual_request_history': [
        {'usage': {'input_tokens': 120000, 'output_tokens': 8000}},
        {'usage': {'input_tokens': 290000, 'output_tokens': 10000}},
    ]}
    assert runner._frontdoor_observed_request_span_tokens(state) == 300000
    assert runner._frontdoor_observed_request_span_tokens({}) == 0


def test_frontdoor_scale_overflow_defers_inside_the_hop_instead_of_raising() -> None:
    from g3ku.runtime.chunk_compression_plan import COMPRESSION_MAX_PASS_ROUNDS

    sizes: list[int] = []
    runner = _frontdoor_runner(sizes)
    # 会话实测跨度 12 万 ⇒ 容量被夹到 10.8 万，390 万历史要几十块，越过 12 上限。
    # 旧处置是抛 `frontdoor_context_window_exceeded`；现在两轮压满，剩下的原样交回。
    combined, chunk_count, _merge, failed, rounds, deferred, committed, _provider_error = _frontdoor_run(
        runner, _frontdoor_history(420, 10000), 390000, observed_span_tokens=120000
    )
    assert sizes, '溢出块应在本跳第二轮继续压'
    assert rounds == COMPRESSION_MAX_PASS_ROUNDS
    assert failed == 0
    assert committed == len(sizes) < chunk_count
    assert deferred, '压不完的原始历史必须交回，重写请求体时留在摘要之后'
    assert '下一跳按那时的窗口继续压缩' in combined


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
