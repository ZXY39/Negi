"""分块压缩的块数、失败前缀提交与原因分账（2026-10-13 统一规划器之后的合同）。

实盘读数（临时把执行链指向本机假模型、声明窗口 6 万→3 万）留下的两条旧处置都不对：

- 块数曾按"真实可压历史 / 3"这个目标次数放长，与装箱容量无关，放大轮会把块顶到超窗
  （估算≈99 万 ⇒ 每块 429,387 tok > 窗口 39 万）；
- 某一块没拿到摘要时，曾先在摘要里插一行"本块原文未被摘要替代"**却把原文一起吞掉**
  （谎报收口），也曾整次作废（下一跳把已压好的块重烧一遍）。

现在的合同（两条车道同一个规划器 `g3ku/runtime/chunk_compression_plan.py`）：
- C1：块数 = 贪心装箱(原子组) 的自然结果，容量 = 窗口 − 实测信封 − 输出余量；
- C2：一块失败就停在那里——已压好的最长成功前缀照常交付，失败那块及其后的原文
  原样留在本跳请求里，由下一跳按那时的窗口重算；
- C3：第一块就失败 ⇒ 一块都没收下 ⇒ 空产物 ⇒ 预检按压缩失败拒发，原因写明
  `0/N chunks committed` 并带 provider 原话。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from main.runtime.react_loop import ReActToolLoop


class _Resp:
    def __init__(self, content: str = '', error_text: str = '', usage: dict | None = None) -> None:
        self.content = content
        self.error_text = error_text
        self.usage = usage or {}
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


def _make_loop(fake_chat):
    # 分块循环每块前查一次暂停/取消，`_check_pause_or_cancel` 要读 store.get_task。
    log_service = SimpleNamespace(_store=SimpleNamespace(get_task=lambda task_id: None))
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service)
    loop._chat_with_optional_extensions = fake_chat
    loop._resolved_model_response_timeout_seconds = lambda **_: 30.0
    loop._is_empty_model_response = lambda r: not str(getattr(r, 'content', '') or '').strip()
    loop._node_durable_stage_state = lambda node_id: {}
    loop._node_stage_archive_plan = lambda **_: {}
    loop._node_token_compression_task_goal = lambda **_: '探针任务目标：汇总大文件'
    loop._node_compression_must_preserve_records = lambda **_: []
    return loop


def _compress(loop, messages, window):
    return asyncio.run(loop._request_node_token_compression_summary(
        task_id='task:probe',
        node_id='node:probe',
        model_refs=['stub-compress-1'],
        provider_model='openai:stub-compress',
        request_messages=messages,
        context_window_tokens=window,
    ))


def _chunk_chat(estimator, calls, *, fail_indexes=(), always_fail=False):
    async def fake_chat(*args, messages=None, **kwargs):
        payload = str((messages or [{}])[-1].get('content') or '')
        kind, index, total = 'single_shot', 0, 1
        try:
            data = json.loads(payload)
            kind = str(data.get('kind') or '')
            index = int(data.get('chunk_index') or 0)
            total = int(data.get('chunk_count') or 1)
        except Exception:
            data = {}
        calls.append({
            'kind': kind,
            'index': index,
            'total': total,
            'tokens': int(estimator(list(messages or []))),
        })
        if always_fail or (kind.endswith('_chunk') and index in fail_indexes):
            return _Resp(content='', error_text='HTTP 429: inference exceeds tpm/rpm limit')
        return _Resp(content=f'【摘要 {index}/{total}】保留结论与路径。')
    return fake_chat


# ---------------------------------------------------------------------------
# C1：块数由容量推导，与"目标次数"无关
# ---------------------------------------------------------------------------


def test_chunk_count_follows_capacity_and_executes_every_planned_chunk() -> None:
    probe = _make_loop(lambda *a, **k: _Resp(content='占位'))
    estimator = probe._estimate_node_compression_message_tokens
    messages = _history(60, 12000)
    assert estimator(messages) > 400_000, f'探针历史需要足够大才能验块数，实测 {estimator(messages)}'

    calls: list[dict] = []
    loop = _make_loop(_chunk_chat(estimator, calls))
    text, payload = _compress(loop, messages, window=390000)

    chunk_calls = [item for item in calls if item['kind'].endswith('_chunk')]
    assert payload.get('compression_mode') == 'llm_chunked'
    assert payload.get('capacity_source') == 'declared_window'
    assert int(payload.get('chunk_count') or 0) == len(chunk_calls), '计划块数就是实发块数'
    assert int(payload.get('chunk_committed_count') or 0) == len(chunk_calls), '全部成功 ⇒ 一块不落'
    assert int(payload.get('deferred_message_count') or 0) == 0
    capacity = int(payload.get('capacity_tokens') or 0)
    assert 0 < capacity < 390_000
    assert max(item['tokens'] for item in chunk_calls) <= capacity + 5_000, (
        f'块请求体量必须落在容量内（容量 {capacity}）'
    )
    assert text


def test_frontdoor_and_node_derive_the_same_chunk_count_from_the_same_capacity() -> None:
    """两条车道共用一个规划器：同一份组用量与窗口，块数必须一致。"""
    from g3ku.runtime.chunk_compression_plan import COMPRESSION_MODE_CHUNKED
    from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner

    counts = [90_000, 90_000, 90_000, 90_000, 90_000]
    window = 390_000
    envelope = 6_000

    runner = object.__new__(CreateAgentCeoFrontDoorRunner)
    front_pass = runner._frontdoor_plan_token_compression_pass(
        grouped_history=[([{'role': 'user', 'content': 'x'}], tokens) for tokens in counts],
        single_shot_request_tokens=sum(counts) + envelope,
        context_window_tokens=window,
        observed_span_tokens=0,
    )

    loop = _make_loop(lambda *a, **k: _Resp(content='x'))
    node_pass = loop._node_plan_token_compression_pass(
        grouped_history=[([{'role': 'user', 'content': 'x'}], tokens) for tokens in counts],
        single_shot_request_tokens=sum(counts) + envelope,
        context_window_tokens=window,
        observed_span_tokens=0,
    )

    assert front_pass.mode == node_pass.mode == COMPRESSION_MODE_CHUNKED
    assert front_pass.spans == node_pass.spans
    assert front_pass.capacity_tokens == node_pass.capacity_tokens


# ---------------------------------------------------------------------------
# C2：一块失败就停在在那里，前缀照常交付、其余原文留在请求里
# ---------------------------------------------------------------------------


def test_partial_chunk_failure_commits_the_prefix_and_keeps_the_rest_raw() -> None:
    calls: list[dict] = []
    messages = _history(60, 12000)
    estimator = _make_loop(lambda *a, **k: _Resp(content='占位'))._estimate_node_compression_message_tokens
    loop = _make_loop(_chunk_chat(estimator, calls, fail_indexes={2}))

    text, payload = _compress(loop, messages, window=390000)

    assert payload.get('compression_mode') == 'llm_chunked'
    assert list(payload.get('chunk_failed_indexes') or []) == [2]
    assert int(payload.get('chunk_committed_count') or 0) == 1
    assert int(payload.get('deferred_message_count') or 0) > 0
    assert payload.get('deferred_raw_messages'), '未压中的原文要交回调用方留在请求里'
    assert payload.get('chunk_error_text', '').startswith('HTTP 429')
    assert text.strip(), '部分失败仍要产出可用摘要（交回前缀，不再谎称全量收口）'
    assert '未取得摘要' not in text, f'停下的那块不许留缺口行：{text[:160]}'
    assert '下一跳按那时的窗口继续压缩' in text


# ---------------------------------------------------------------------------
# C3：第一块就失败 ⇒ 一块都没收下 ⇒ 空产物，原因写明 0/N chunks committed
# ---------------------------------------------------------------------------


def test_all_chunks_failed_yields_empty_product_and_refusal_reason() -> None:
    calls: list[dict] = []
    messages = _history(60, 12000)
    estimator = _make_loop(lambda *a, **k: _Resp(content='占位'))._estimate_node_compression_message_tokens
    loop = _make_loop(_chunk_chat(estimator, calls, always_fail=True))

    text, payload = _compress(loop, messages, window=390000)

    assert payload.get('compression_mode') == 'llm_chunked'
    assert text == '', f'一块都没收下时不能产出看起来成功的摘要：{text[:80]!r}'
    assert int(payload.get('chunk_committed_count') or 0) == 0
    assert int(payload.get('chunk_count') or 0) >= 1

    reason = ReActToolLoop._compression_failure_reason(payload)
    assert 'chunks committed' in reason
    assert 'HTTP 429: inference exceeds tpm/rpm limit' in reason, '限流原话必须留在原因里'


def test_single_shot_failure_quotes_provider_error() -> None:
    calls: list[dict] = []
    messages = _history(20, 600)
    estimator = _make_loop(lambda *a, **k: _Resp(content='占位'))._estimate_node_compression_message_tokens
    loop = _make_loop(_chunk_chat(estimator, calls, always_fail=True))

    text, payload = _compress(loop, messages, window=300000)

    assert payload.get('compression_mode') == 'llm'
    assert int(payload.get('history_message_count') or 0) > 0
    assert text == ''
    assert payload.get('helper_error_text', '').startswith('HTTP 429')
    reason = ReActToolLoop._compression_failure_reason(payload)
    assert 'empty summary' in reason
    assert 'HTTP 429: inference exceeds tpm/rpm limit' in reason


@pytest.mark.parametrize(
    'payload,expected',
    [
        (
            {
                'compression_mode': 'llm_chunked',
                'chunk_count': 3,
                'chunk_committed_count': 0,
                'chunk_error_text': 'HTTP 429: rate',
            },
            'chunks committed',
        ),
        (
            {
                'compression_mode': 'llm_chunked',
                'chunk_count': 3,
                'chunk_committed_count': 2,
                'chunk_error_text': 'HTTP 429: rate',
            },
            'empty summary',
        ),
        ({'compression_mode': 'llm', 'chunk_count': 1, 'helper_error_text': 'HTTP 503: busy'}, 'empty summary'),
    ],
)
def test_compression_failure_reason_branches(payload: dict, expected: str) -> None:
    assert expected in ReActToolLoop._compression_failure_reason(payload)
