"""追加通知在压缩车道上的代持合同。

实盘病例 `task:7e41b467b8aa`：18:27:21 并入的「立即评估并派生子节点」以裸 user 正文进了
账本，18:33 起每跳走整史压缩，摘要正文里 `追加/主 Agent/串行` 命中 0，反而写着
「所有调研由根节点完成，未派生子节点」——把刚被否掉的分工写成既成事实每跳重喂一次。
尾块判据当时取的是**压缩前整条账本**的可见性，所以代持块一次都没生成。
"""

from __future__ import annotations

import json

from main.runtime.append_notice_context import (
    APPEND_NOTICE_TAIL_PREFIX,
    build_append_notice_tail_messages,
    render_append_notice_must_preserve_block,
    roll_append_notice_context_for_compression_stage,
    score_append_notice_preservation,
    select_uncovered_append_notices,
)

# 真通知正文的开头（逐字取自 node.metadata.append_notice_context.notice_records[0]）
_NOTICE_MESSAGE = (
    '【追加指令（2026-10-09，主 Agent 要求立即执行）】任务根节点请立即评估当前进度并派生子节点'
    '完成剩余调研工作，不要继续纯串行推进：'
)

_CONTEXT = {
    'notice_records': [
        {
            'notification_id': 'root-notice:epoch:0df0cb663c08:1',
            'epoch_id': 'epoch:0df0cb663c08',
            'source_node_id': 'node:827ce308b1df',
            'message': _NOTICE_MESSAGE,
            'received_at': '2026-10-09T18:27:21+08:00',
            'consumed_at': '2026-10-09T18:27:33+08:00',
            'merged_at': '2026-10-09T18:27:33+08:00',
            'compression_stage_id': '',
            'superseded_at': '',
        }
    ],
    'compression_segments': [],
}

# 实盘摘要正文的相关片段
_LOST_SUMMARY = '## 任务执行压缩摘要\n- **执行模式**：所有调研由根节点完成，未派生子节点。'
_KEPT_SUMMARY = (
    '## 任务执行压缩摘要\n- 待办：'
    + _NOTICE_MESSAGE
    + '（2026-10-09 主 Agent 追加，尚未执行）'
)


def test_uncovered_notice_is_pinned_into_must_preserve_block() -> None:
    records = select_uncovered_append_notices(_CONTEXT)
    assert [item['notification_id'] for item in records] == ['root-notice:epoch:0df0cb663c08:1']
    block = render_append_notice_must_preserve_block(records)
    assert block.startswith('【必须逐条保留的追加要求】')
    assert _NOTICE_MESSAGE in block
    assert '[2026-10-09T18:27:33+08:00]' in block
    # 已代持/已作废的记录不再进必须保留段
    rolled = roll_append_notice_context_for_compression_stage(
        _CONTEXT, compression_stage_id='token-compact:node:827ce308b1df:207', created_at='now'
    )
    assert select_uncovered_append_notices(rolled) == []
    assert render_append_notice_must_preserve_block([]) == ''


def test_summary_that_drops_the_requirement_scores_as_miss() -> None:
    probe = score_append_notice_preservation(_LOST_SUMMARY, select_uncovered_append_notices(_CONTEXT))
    assert probe['open_count'] == 1
    assert probe['hit_count'] == 0
    assert probe['miss_count'] == 1
    assert probe['miss_notification_ids'] == ['root-notice:epoch:0df0cb663c08:1']


def test_summary_that_carries_the_requirement_scores_as_hit() -> None:
    probe = score_append_notice_preservation(_KEPT_SUMMARY, select_uncovered_append_notices(_CONTEXT))
    assert probe['hit_count'] == 1
    assert probe['miss_count'] == 0
    assert probe['miss_notification_ids'] == []


def test_tail_block_visibility_is_window_scoped_not_ledger_scoped() -> None:
    records = select_uncovered_append_notices(_CONTEXT)
    # 原文不在本次发送窗口（压缩后的 18 条体）⇒ 必须出代持块，哪怕账本里还留着正文
    block = build_append_notice_tail_messages(_CONTEXT, visible_user_messages=[])
    assert len(block) == 1
    assert block[0]['role'] == 'assistant'
    assert block[0]['content'].startswith(APPEND_NOTICE_TAIL_PREFIX)
    payload = json.loads(block[0]['content'][len(APPEND_NOTICE_TAIL_PREFIX):].strip())
    assert payload['kind'] == 'raw_notice_window'
    assert payload['notices'][0]['message'] == _NOTICE_MESSAGE
    # 原文仍在窗口里 ⇒ 不重复注入
    assert build_append_notice_tail_messages(_CONTEXT, visible_user_messages=[_NOTICE_MESSAGE]) == []
    assert records and records[0]['consumed_at'] == '2026-10-09T18:27:33+08:00'


def test_roll_on_miss_is_idempotent_and_carries_verbatim_text() -> None:
    stage_id = 'token-compact:node:827ce308b1df:207'
    once = roll_append_notice_context_for_compression_stage(
        _CONTEXT, compression_stage_id=stage_id, created_at='2026-10-09T18:33:26+08:00'
    )
    twice = roll_append_notice_context_for_compression_stage(
        once, compression_stage_id=stage_id, created_at='2026-10-09T18:34:51+08:00'
    )
    segments = list(twice.get('compression_segments') or [])
    # 同一批历史被重复压到同一个 stage id：不得叠出第二份代持段
    assert len(segments) == 1
    assert segments[0]['notice_ids'] == ['root-notice:epoch:0df0cb663c08:1']
    assert _NOTICE_MESSAGE in segments[0]['summary_text']
    block = build_append_notice_tail_messages(twice, visible_user_messages=[])
    assert len(block) == 1
    payload = json.loads(block[0]['content'][len(APPEND_NOTICE_TAIL_PREFIX):].strip())
    assert payload['kind'] == 'compressed_notice_window'
    assert _NOTICE_MESSAGE in payload['summary_text']
