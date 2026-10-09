"""压缩跳把投影交给下一跳当发送基线（治"每跳重算摘要 + 每跳判超窗"的死循环）。

实盘 `task:7e41b467b8aa`：18:33:26 起连续 8 跳 `prepared_message_count` 恒为 18、
`cache_hit_tokens` 恒为 14,336（= 头部两条），因为下一跳仍从全量账本重装配，预检按全量
估 ⇒ 每跳判超窗 ⇒ 每跳重新摘要 ⇒ 摘要块排在第 3 条每跳换字节。
"""

from __future__ import annotations

from main.runtime.react_loop import ReActToolLoop


def _messages(count: int, *, prefix: str) -> list[dict[str, str]]:
    return [
        {'role': 'assistant' if index % 2 else 'tool', 'content': f'{prefix}-{index}'}
        for index in range(count)
    ]


def test_compacted_hop_hands_the_projection_to_the_next_baseline() -> None:
    body = [{'role': 'system', 'content': '执行节点'}, {'role': 'assistant', 'content': '[G3KU_TOKEN_COMPACT_V2]\n{}'}]
    assert ReActToolLoop._node_send_baseline_after_compaction(body, {'applied': True}) == body
    # 没压的跳不接管基线，append-only 链照旧
    assert ReActToolLoop._node_send_baseline_after_compaction(body, {'applied': False}) is None
    assert ReActToolLoop._node_send_baseline_after_compaction(body, None) is None
    assert ReActToolLoop._node_send_baseline_after_compaction([], {'applied': True}) is None


def test_non_dict_records_are_dropped_from_the_adopted_baseline() -> None:
    body = [{'role': 'system', 'content': 'x'}, None, 'junk', {'role': 'tool', 'content': 'y'}]
    assert ReActToolLoop._node_send_baseline_after_compaction(body, {'applied': True}) == [
        {'role': 'system', 'content': 'x'},
        {'role': 'tool', 'content': 'y'},
    ]


def test_chain_from_adopted_projection_appends_new_frames_only() -> None:
    # 继承载体：上一跳真实发出去的是裁过的投影，账本仍是全量
    projection = [{'role': 'system', 'content': '执行节点'}, {'role': 'user', 'content': '{"goal":"g"}'}] + _messages(
        14, prefix='sent'
    )
    ledger = projection + _messages(180, prefix='ledger')
    new_frames = _messages(2, prefix='new')
    tail = [{'role': 'user', 'content': '## Runtime Tool Contract ...'}]

    messages, source = ReActToolLoop._same_turn_append_only_request_messages_with_source(
        previous_request_messages=projection,
        current_model_messages=ledger,
        pending_delta_messages=new_frames,
        request_tail_messages=tail,
    )
    assert source == 'same_turn_chain'
    # 采纳投影后不得被全量账本重新撑开：请求体 = 投影 + 本跳新增 + 尾部三件
    assert messages[: len(projection)] == projection
    assert len(messages) == len(projection) + len(new_frames) + len(tail)
    assert len(messages) < len(ledger)
