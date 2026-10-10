"""分块的单块硬上限与规模早退（2026-10-10 实测暴露的 /3 副作用）。

`_3 次收口` 的放大轮原先没有窗口上限，实测两条形状：

- 估算 ≈99 万 tok / 窗口 39 万：三分量 323,096 先切 4 块，放大轮把预算顶到 430,795
  ⇒ 每块请求 **429,387 tok > 声明窗口** ⇒ 真网关必然拒这一块；
- 估算 ≈390 万：三分量 1,291,793 ⇒ 只切 3 块，每块 **1,725,362 tok ≈ 窗口的 4.4 倍**
  ⇒ 每块退避重试 3 次 = 9 次注定被拒的调用，然后才失败。

合同（两条车道同源）：
- C1 单块硬上限：块预算 = min(max(窗口档, 三分量), 窗口 − 信封 − 余量)，放大轮同样被夹住
  ⇒ 宁可切 4 块，也不发一块注定超窗的请求；
- C2 规模早退：按硬上限切仍超过 `_NODE_COMPRESSION_MAX_CHUNKS`(8) 块 ⇒ **一次都不发**，
  节点侧落 `token compression skipped: …` 拒发（可恢复暂停），会话侧直接报窗口超限。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

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


def _node_loop(record: list[int]):
    state: dict = {}

    async def fake_chat(*args, messages=None, **kwargs):
        est = state['loop']._estimate_node_compression_message_tokens(list(messages or []))
        record.append(est)
        return _Resp(content='【摘要】保留结论与路径。')

    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=SimpleNamespace())
    state['loop'] = loop
    loop._chat_with_optional_extensions = fake_chat
    loop._resolved_model_response_timeout_seconds = lambda **_: 30.0
    loop._is_empty_model_response = lambda r: not str(getattr(r, 'content', '') or '').strip()
    loop._node_durable_stage_state = lambda node_id: {}
    loop._node_stage_archive_plan = lambda **_: {}
    loop._node_token_compression_task_goal = lambda **_: '探针任务目标'
    loop._node_compression_must_preserve_records = lambda **_: []

    async def run(messages, window):
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
# C1：单块不得超过声明窗口
# ---------------------------------------------------------------------------


def test_ninety_nine_wan_shape_no_chunk_exceeds_window() -> None:
    """放大轮原先会把块顶到 42.9 万 tok > 窗口 39 万 ⇒ 每块注定被网关拒。"""
    sizes: list[int] = []
    _loop, run = _node_loop(sizes)
    messages = _history(219, 4800)  # 单发压缩请求 ≈ 99 万 tok
    window = 390000
    text, payload = asyncio.run(run(messages, window))

    assert payload.get('compression_mode') == 'llm_chunked'
    assert sizes, '这条形状仍然分块'
    # 基线：3 块 × 429,387 tok（超窗）。合同：每块都必须在窗口之内。
    assert max(sizes) <= window, f'块请求超出声明窗口：max={max(sizes)} window={window}'
    assert len(sizes) == int(payload.get('chunk_count') or 0)
    assert text


def test_mid_scale_shape_takes_more_chunks_instead_of_oversized_ones() -> None:
    """夹不住 /3 的规模（≈150 万）要落进"多切几块"，而不是超窗块，也不该早退。"""
    sizes: list[int] = []
    _loop, run = _node_loop(sizes)
    window = 390000
    text, payload = asyncio.run(run(_history(330, 8000), window))

    assert payload.get('compression_mode') == 'llm_chunked'
    assert int(payload.get('chunk_count') or 0) > 3, '这个规模 3 块装不进窗口'
    assert int(payload.get('chunk_count') or 0) <= 8, '未达早退线时照常逐块压'
    assert max(sizes) <= window
    assert text


def test_seventy_eight_wan_shape_still_three_chunks_inside_window() -> None:
    """78 万这条不需要多切：三分量本来就在窗口内，仍是 3 块。"""
    sizes: list[int] = []
    _loop, run = _node_loop(sizes)
    text, payload = asyncio.run(run(_history(120, 4800), 390000))

    assert payload.get('compression_mode') == 'llm_chunked'
    assert int(payload.get('chunk_count') or 0) <= 3
    assert max(sizes) <= 390000
    assert text


# ---------------------------------------------------------------------------
# C2：规模早退——一次都不发
# ---------------------------------------------------------------------------


def test_ten_million_scale_refuses_before_any_helper_call() -> None:
    sizes: list[int] = []
    _loop, run = _node_loop(sizes)
    messages = _history(420, 10000)  # 单发压缩请求 ≈ 390 万 tok，窗口 39 万

    text, payload = asyncio.run(run(messages, 390000))

    assert sizes == [], f'规模早退不该发出任何压缩请求：{len(sizes)} 次'
    assert text == ''
    planned = int(payload.get('chunk_count') or 0)
    assert planned > 8, planned
    reason = ReActToolLoop._compression_failure_reason(payload)
    assert reason.startswith('token compression skipped:')
    assert 'per-chunk ceiling' in reason and 'max 8 chunks' in reason


def test_compression_failure_reason_prefers_refusal_over_chunk_failure() -> None:
    payload = {'chunk_count': 12, 'chunk_failed_count': 0, 'chunk_refused_reason': 'compressible history 3875344 tok needs 12 chunks over the 361998 tok per-chunk ceiling (max 8 chunks)'}
    reason = ReActToolLoop._compression_failure_reason(payload)
    assert reason.startswith('token compression skipped:')
    assert 'any chunk' not in reason


# ---------------------------------------------------------------------------
# 会话车道同源
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


def test_frontdoor_chunks_stay_inside_window() -> None:
    sizes: list[int] = []
    runner = _frontdoor_runner(sizes)
    window = 390000
    combined, chunk_count, _merge = asyncio.run(
        runner._chunked_frontdoor_compression_summaries(
            system_prefix=[],
            older_history_messages=_frontdoor_history(219, 4800),
            model_refs=['m'],
            context_window_tokens=window,
            state={},
            runtime=SimpleNamespace(),
            is_cancelled=None,
            model_info={},
        )
    )
    assert combined
    assert max(sizes) <= window, f'会话车道块超窗：max={max(sizes)}'
    assert len(sizes) == chunk_count


@pytest.mark.parametrize('pairs', [420])
def test_frontdoor_scale_overflow_raises_without_any_call(pairs: int) -> None:
    from g3ku.runtime.frontdoor._ceo_runtime_ops import FrontdoorCompressionRuntimeError

    sizes: list[int] = []
    runner = _frontdoor_runner(sizes)
    with pytest.raises(FrontdoorCompressionRuntimeError) as raised:
        asyncio.run(
            runner._chunked_frontdoor_compression_summaries(
                system_prefix=[],
                older_history_messages=_frontdoor_history(pairs, 10000),
                model_refs=['m'],
                context_window_tokens=390000,
                state={},
                runtime=SimpleNamespace(),
                is_cancelled=None,
                model_info={},
            )
        )
    assert sizes == []
    assert raised.value.code == 'frontdoor_context_window_exceeded'
