"""通知块按内容幂等：历史里已有同一份块就不再插第二份。

实盘事故（本仓库自身改动引入）：探针节点 `node:3c3fe3a0f5a8` 的账本 135 帧里，
逐字节相同的 `[G3KU_APPEND_NOTICE_TAIL_V1]` 块叠了 **36 份**（每份 245 字符）。
成因是段分支不受可见性过滤、装配结果又被回写进 `frame.messages`，于是每跳再插一份。
这里只挡新副本：回删已经发出去的帧等于重写 provider 前缀，代价比留重复块大。
"""

from __future__ import annotations

from main.runtime.append_notice_context import (
    APPEND_NOTICE_TAIL_PREFIX,
    build_append_notice_tail_messages,
    dedupe_append_notice_tail_messages,
    is_append_notice_tail_message,
    roll_append_notice_context_for_compression_stage,
)

_MESSAGE = '【探针追加要求 PROBE-9FM2】从下一轮起先把剩余执行轮拆成 2 个子节点并行跑，再汇总提交。'

_CONTEXT = {
    'notice_records': [
        {
            'notification_id': 'root-notice:epoch:93538601d9cd:1',
            'epoch_id': 'epoch:93538601d9cd',
            'source_node_id': 'node:3c3fe3a0f5a8',
            'message': _MESSAGE,
            'consumed_at': '2026-10-09T21:59:08+08:00',
            'compression_stage_id': '',
            'superseded_at': '',
        }
    ],
    'compression_segments': [],
}


def _rolled(context: dict, stage_id: str) -> dict:
    return roll_append_notice_context_for_compression_stage(
        context, compression_stage_id=stage_id, created_at='2026-10-09T22:02:31+08:00'
    )


def _copies(block: dict, count: int) -> list[dict]:
    return [dict(block) for _ in range(count)]


def test_fresh_history_still_gets_one_block() -> None:
    rolled = _rolled(_CONTEXT, 'token-compact:node:3c3fe3a0f5a8:5')
    blocks = build_append_notice_tail_messages(rolled, visible_user_messages=[])
    assert len(blocks) == 1
    kept, dropped = dedupe_append_notice_tail_messages(blocks, [{'role': 'system', 'content': '执行节点'}])
    assert len(kept) == 1 and dropped == 0
    assert is_append_notice_tail_message(kept[0])
    assert kept[0]['content'].startswith(APPEND_NOTICE_TAIL_PREFIX)


def test_existing_identical_block_suppresses_the_new_copy() -> None:
    stage_id = 'token-compact:node:3c3fe3a0f5a8:5'
    rolled = _rolled(_CONTEXT, stage_id)
    blocks = build_append_notice_tail_messages(rolled, visible_user_messages=[])
    # 复现实盘：账本里已经沉淀了 36 份同样的块
    contaminated = [{'role': 'system', 'content': '执行节点'}] + _copies(blocks[0], 36)
    kept, dropped = dedupe_append_notice_tail_messages(blocks, contaminated)
    assert kept == []
    assert dropped == 1
    # 幂等：同一份历史再走一遍过滤器，结果不变（第 37 份永远进不了发送面）
    again, dropped_again = dedupe_append_notice_tail_messages(blocks, contaminated)
    assert again == [] and dropped_again == 1


def test_distinct_blocks_are_both_kept() -> None:
    stage_id = 'token-compact:node:3c3fe3a0f5a8:5'
    rolled = _rolled(_CONTEXT, stage_id)
    segment_block = build_append_notice_tail_messages(rolled, visible_user_messages=[])[0]
    history = [{'role': 'system', 'content': '执行节点'}] + _copies(segment_block, 2)
    # 第二条通知刚到（还没代持），它的 raw 块与已沉淀的段块内容不同，必须一起在场
    second = {
        'notification_id': 'root-notice:epoch:93538601d9cd:2',
        'epoch_id': 'epoch:93538601d9cd',
        'source_node_id': 'node:3c3fe3a0f5a8',
        'message': '【探针追加要求 PROBE-2BX7】汇总时把两次结论分节写，不要合并成一段。',
        'consumed_at': '2026-10-09T22:10:00+08:00',
        'compression_stage_id': '',
        'superseded_at': '',
    }
    context_two = {
        'notice_records': list(rolled.get('notice_records') or []) + [second],
        'compression_segments': list(rolled.get('compression_segments') or []),
    }
    candidates = build_append_notice_tail_messages(context_two, visible_user_messages=[])
    assert len(candidates) == 2
    kept, dropped = dedupe_append_notice_tail_messages(candidates, history)
    assert dropped == 1  # 第一条通知的段块已在历史里
    assert len(kept) == 1 and kept[0]['content'] not in [str(item.get('content') or '') for item in history]


def test_non_notice_messages_are_never_touched_by_the_filter() -> None:
    plain = {'role': 'user', 'content': f'{APPEND_NOTICE_TAIL_PREFIX} 这不是块，只是正文里提到前缀'}
    candidate = {'role': 'assistant', 'content': plain['content']}
    kept, dropped = dedupe_append_notice_tail_messages([candidate], [plain])
    assert dropped == 0 and kept == [candidate]
