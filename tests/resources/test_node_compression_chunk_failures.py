"""分块压缩的块数与失败计数（2026-10-10 实盘/离线读数后的合同）。

实盘读数（临时把执行链指向本机假模型、声明窗口 6 万→3 万）：

- 12 条以内的历史 ⇒ 尾部（`_NODE_TOKEN_COMPACTION_RECENT_TAIL_COUNT=12`）把整段吃掉 ⇒
  可压段为 0 ⇒ helper 一次都没调 ⇒ 直接拒发
  `estimated_total_tokens (163263) exceeded context_window_tokens (60000) with no compressible history`
- 窗口 3 万、历史 31.8 万 token ⇒ 按 2 万 token 地板切成 **24 块**（24 次 helper 调用）；
  其中 1 块被限流 ⇒ 该块正文留空、循环继续、**不报任何失败**，节点拿着丢一块的投影继续跑；
  14 块全部限流 ⇒ 产物只剩 198 个字符的空标签，**照样当成功**。

本文件锁定三条合同：
- C1：块数按"真实可压历史 / 3"动态放长，超大历史也在 3 次内收口（两条车道同源）；
- C2：部分块失败要可数、要在投影里写明缺口，但不拦发布；
- C3：全部块失败 ⇒ 空产物 ⇒ 预检按压缩失败拒发，原因带 provider 原话与失败块数。
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
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=SimpleNamespace())
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


def _chunk_chat(calls, *, fail_indexes=(), always_fail=False):
    async def fake_chat(*args, messages=None, **kwargs):
        payload = str((messages or [{}])[-1].get('content') or '')
        kind, index, total = 'single_shot', 0, 1
        try:
            data = json.loads(payload)
            kind = str(data.get('kind') or '')
            index = int(data.get('chunk_index') or 0)
            total = int(data.get('chunk_count') or 1)
        except Exception:
            pass
        calls.append({'kind': kind, 'index': index, 'total': total})
        if always_fail or index in fail_indexes:
            return _Resp(content='', error_text='HTTP 429: inference exceeds tpm/rpm limit')
        return _Resp(content=f'【摘要 {index}/{total}】保留结论与路径。')
    return fake_chat


# ---------------------------------------------------------------------------
# C1：块数按 /3 放长，不再被 2 万 token 地板切成几十块
# ---------------------------------------------------------------------------


def test_chunk_budget_bounds_passes_to_three_for_large_history() -> None:
    calls: list[dict] = []
    loop = _make_loop(_chunk_chat(calls))
    messages = _history(30, 6000)
    est = loop._estimate_node_compression_message_tokens(messages)
    assert est > 90000, f'探针历史需要足够大才能验块数，实测 {est}'

    text, payload = _compress(loop, messages, window=30000)

    assert payload.get('compression_mode') == 'llm_chunked'
    # 基线在这里是 24 块（20000 地板）；合同是 3 次内收口。
    assert int(payload.get('chunk_count') or 0) <= 3, payload.get('chunk_count')
    assert len(calls) == int(payload.get('chunk_count') or 0)
    assert text


def test_frontdoor_chunk_budget_uses_the_same_three_pass_rule() -> None:
    from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner

    runner = object.__new__(CreateAgentCeoFrontDoorRunner)
    calls: list[int] = []

    async def fake_helper(*, messages=None, **kwargs):
        payload = str((messages or [{}])[-1].get('content') or '')
        try:
            data = json.loads(payload)
        except Exception:
            data = {}
        calls.append(int(data.get('chunk_index') or 0))
        return '【摘要】保留结论与路径。', {'role': 'assistant', 'content': '摘要'}

    async def no_emit(**kwargs):
        return None

    runner._run_frontdoor_compression_helper_request = fake_helper
    runner._emit_frontdoor_runtime_snapshot = no_emit
    history = _history(30, 6000)
    combined, chunk_count, _merge = asyncio.run(
        runner._chunked_frontdoor_compression_summaries(
            system_prefix=[],
            older_history_messages=history[2:],
            model_refs=['stub-compress-1'],
            context_window_tokens=30000,
            state={},
            runtime=SimpleNamespace(),
            is_cancelled=None,
            model_info={},
        )
    )
    assert chunk_count <= 3, chunk_count
    assert len(calls) == chunk_count
    assert combined


# ---------------------------------------------------------------------------
# C2：部分块失败要可数、要写明缺口，但不拦发布
# ---------------------------------------------------------------------------


def test_partial_chunk_failure_is_counted_and_labeled_in_projection() -> None:
    calls: list[dict] = []
    loop = _make_loop(_chunk_chat(calls, fail_indexes={2}))
    messages = _history(30, 6000)

    text, payload = _compress(loop, messages, window=30000)

    assert int(payload.get('chunk_failed_count') or 0) == 1
    assert list(payload.get('chunk_failed_indexes') or []) == [2]
    assert '未取得摘要' in text
    assert payload.get('chunk_error_text', '').startswith('HTTP 429')
    assert text.strip(), '部分失败仍要产出可用摘要（只标缺口，不清空）'


# ---------------------------------------------------------------------------
# C3：全部块失败 ⇒ 空产物 ⇒ 原因写成"没有任何一块拿到摘要"并带 provider 原话
# ---------------------------------------------------------------------------


def test_all_chunks_failed_yields_empty_product_and_refusal_reason() -> None:
    calls: list[dict] = []
    loop = _make_loop(_chunk_chat(calls, always_fail=True))
    messages = _history(20, 6000)

    text, payload = _compress(loop, messages, window=30000)

    assert text == '', f'全部块失败不能产出看起来成功的摘要：{text[:80]!r}'
    assert int(payload.get('chunk_failed_count') or 0) == int(payload.get('chunk_count') or 0) > 0

    reason = ReActToolLoop._compression_failure_reason(payload)
    assert 'any chunk' in reason
    assert 'HTTP 429: inference exceeds tpm/rpm limit' in reason, '限流原话必须留在原因里'


def test_single_shot_failure_quotes_provider_error() -> None:
    calls: list[dict] = []
    loop = _make_loop(_chunk_chat(calls, always_fail=True))
    messages = _history(20, 600)

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
        ({'chunk_count': 3, 'chunk_failed_count': 3, 'chunk_error_text': 'HTTP 429: rate'}, 'any chunk'),
        ({'chunk_count': 3, 'chunk_failed_count': 1, 'chunk_error_text': 'HTTP 429: rate'}, 'empty summary'),
        ({'helper_error_text': 'HTTP 503: busy'}, 'empty summary'),
    ],
)
def test_compression_failure_reason_branches(payload: dict, expected: str) -> None:
    assert expected in ReActToolLoop._compression_failure_reason(payload)
