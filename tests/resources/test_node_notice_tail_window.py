"""节点通知尾窗：未消费 + 最近 N 条已消费留在尾块，其余退场可检索。"""

from __future__ import annotations

import json

from main.runtime import mid_insert as mi
from main.runtime.append_notice_context import (
    APPEND_NOTICE_TAIL_PREFIX,
    NOTICE_INDEX_PREFIX,
    build_append_notice_tail_messages,
    is_notice_index_message,
    live_append_notice_ids,
    prune_append_notice_context_segments,
    roll_append_notice_context_for_compression_stage,
    select_append_notice_tail_records,
    select_uncovered_append_notices,
)
from main.runtime.react_loop import ReActToolLoop


def _record(
    index: int,
    *,
    merged: bool = True,
    superseded: bool = False,
    text: str | None = None,
) -> dict:
    stamp = f'2026-10-01T00:{index:02d}:00+08:00'
    return {
        'notification_id': f'root-notice:{index}',
        'epoch_id': 'epoch:1',
        'source_node_id': 'node:root',
        'kind': 'user_direct',
        'message': text or f'notice {index}',
        'received_at': stamp,
        'consumed_at': stamp if merged else '',
        'merged_at': stamp if merged else '',
        'compression_stage_id': '',
        'superseded_at': '2026-10-02T00:00:00+08:00' if superseded else '',
    }


def _context(records: list[dict], segments: list[dict] | None = None) -> dict:
    return {
        'notice_records': list(records),
        'compression_segments': list(segments or []),
    }


def _raw_block(notification_ids: list[str]) -> dict:
    payload = {
        'kind': 'raw_notice_window',
        'notice_count': len(notification_ids),
        'notices': [
            {'notification_id': notification_id, 'message': notification_id}
            for notification_id in notification_ids
        ],
    }
    return {
        'role': 'user',
        'content': f'{APPEND_NOTICE_TAIL_PREFIX}\n'
        f'{json.dumps(payload, ensure_ascii=False, sort_keys=True)}',
    }


def test_tail_window_keeps_unconsumed_and_latest_consumed() -> None:
    records = [_record(index) for index in range(7)]
    unconsumed = _record(99, merged=False)
    live = select_append_notice_tail_records(
        _context([unconsumed, *records]),
        consumed_window=5,
    )
    ids = [item['notification_id'] for item in live]
    assert ids[0] == 'root-notice:99'
    assert 'root-notice:0' not in ids
    assert 'root-notice:1' not in ids
    assert ids[-5:] == [f'root-notice:{index}' for index in range(2, 7)]


def test_superseded_and_covered_records_do_not_enter_raw_window() -> None:
    superseded = _record(1, superseded=True)
    covered = _record(2)
    covered['compression_stage_id'] = 'stage:cover'
    context = _context([superseded, covered])
    assert [item['notification_id'] for item in select_append_notice_tail_records(context)] == [
        'root-notice:2'
    ]
    assert select_uncovered_append_notices(context) == []


def test_raw_window_excludes_records_outside_consumed_window() -> None:
    records = [_record(index) for index in range(7)]
    messages = build_append_notice_tail_messages(
        _context(records),
        visible_user_messages=[],
    )
    assert len(messages) == 1
    payload = json.loads(
        messages[0]['content'][len(APPEND_NOTICE_TAIL_PREFIX) :].strip()
    )
    assert [item['notification_id'] for item in payload['notices']] == [
        f'root-notice:{index}' for index in range(2, 7)
    ]


def test_segments_only_render_when_they_hold_a_live_notice() -> None:
    records = [_record(index) for index in range(7)]
    segments = [
        {
            'compression_stage_id': 'stage:old',
            'created_at': '',
            'notice_ids': ['root-notice:0', 'root-notice:1'],
            'notice_count': 2,
            'summary_text': 'old requirement',
        },
        {
            'compression_stage_id': 'stage:live',
            'created_at': '',
            'notice_ids': ['root-notice:6'],
            'notice_count': 1,
            'summary_text': 'live requirement',
        },
    ]
    messages = build_append_notice_tail_messages(
        _context(records, segments),
        visible_user_messages=[],
    )
    contents = [item['content'] for item in messages]
    assert len([item for item in contents if 'stage:old' in item]) == 0
    assert len([item for item in contents if 'stage:live' in item]) == 1


def test_prune_drops_dead_segments_and_keeps_legacy_segments() -> None:
    records = [_record(index) for index in range(7)]
    segments = [
        {
            'compression_stage_id': 'stage:dead',
            'created_at': '',
            'notice_ids': ['root-notice:0'],
            'notice_count': 1,
            'summary_text': 'dead',
        },
        {
            'compression_stage_id': 'stage:live',
            'created_at': '',
            'notice_ids': ['root-notice:6'],
            'notice_count': 1,
            'summary_text': 'live',
        },
        {
            'compression_stage_id': 'stage:legacy',
            'created_at': '',
            'notice_ids': [],
            'notice_count': 0,
            'summary_text': 'legacy without ids',
        },
    ]
    pruned = prune_append_notice_context_segments(_context(records, segments))
    stage_ids = [item['compression_stage_id'] for item in pruned['compression_segments']]
    assert stage_ids == ['stage:live', 'stage:legacy']
    # 台账不裁剪
    assert len(pruned['notice_records']) == len(records)


def test_prune_keeps_segment_for_covered_record_inside_window() -> None:
    records = [_record(index) for index in range(7)]
    records[6]['compression_stage_id'] = 'stage:live'
    segments = [
        {
            'compression_stage_id': 'stage:live',
            'created_at': '',
            'notice_ids': ['root-notice:6'],
            'notice_count': 1,
            'summary_text': 'live',
        }
    ]
    context = _context(records, segments)
    pruned = prune_append_notice_context_segments(
        context,
        live_notice_ids=live_append_notice_ids(context),
    )
    assert [item['compression_stage_id'] for item in pruned['compression_segments']] == [
        'stage:live'
    ]
    # 已被代持的记录由段承载，不重复进 raw 窗口；窗口内其余记录仍在。
    uncovered_ids = {
        item['notification_id'] for item in select_uncovered_append_notices(context)
    }
    assert 'root-notice:6' not in uncovered_ids
    assert uncovered_ids == {f'root-notice:{index}' for index in range(2, 6)}


def test_roll_only_covers_records_inside_the_live_window() -> None:
    records = [_record(index) for index in range(7)]
    rolled = roll_append_notice_context_for_compression_stage(
        _context(records),
        compression_stage_id='stage:1',
        created_at='now',
    )
    segments = list(rolled['compression_segments'])
    assert len(segments) == 1
    assert segments[0]['notice_ids'] == [
        f'root-notice:{index}' for index in range(2, 7)
    ]


def test_rewrite_rebuilds_live_tail_and_drops_consumed_only_block() -> None:
    records = [_record(index) for index in range(7)]
    context = _context(records)
    old_tail = _raw_block(['root-notice:0'])
    request_messages = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'bootstrap'},
        old_tail,
        {'role': 'user', 'content': 'history 1'},
        {'role': 'assistant', 'content': 'history 2'},
        {'role': 'user', 'content': 'recent 1'},
        {'role': 'assistant', 'content': 'recent 2'},
    ]
    rewritten, payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
        node_id='node:x',
        request_messages=request_messages,
        compressed_text='summary',
        recent_tail_count=2,
        live_notice_ids=live_append_notice_ids(context),
        append_notice_context=context,
    )
    contents = [str(item.get('content') or '') for item in rewritten]
    assert not any('"root-notice:0"' in content for content in contents)
    compact_index = next(
        index for index, content in enumerate(contents) if content.startswith('[TOKEN_COMPACT_V2]')
    )
    notice_indexes = [
        index
        for index, content in enumerate(contents)
        if content.startswith(APPEND_NOTICE_TAIL_PREFIX)
    ]
    assert notice_indexes and max(notice_indexes) < compact_index
    assert payload['append_notice_tail_count'] == len(notice_indexes)


def test_deep_raw_window_is_hoisted_only_when_it_is_live() -> None:
    context = _context([_record(index) for index in range(7)])
    dead_block = _raw_block(['root-notice:0'])
    live_block = _raw_block(['root-notice:6'])
    request_messages = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'bootstrap'},
        {'role': 'user', 'content': 'history 1'},
        dead_block,
        {'role': 'assistant', 'content': 'history 2'},
        live_block,
        {'role': 'user', 'content': 'recent 1'},
        {'role': 'assistant', 'content': 'recent 2'},
    ]
    parts = ReActToolLoop._split_request_messages_for_token_compaction(
        request_messages=request_messages,
        recent_tail_count=2,
        live_notice_ids=live_append_notice_ids(context),
    )
    assert live_block in parts['append_notice_tail']
    assert dead_block in parts['compressible_history']


def test_archived_consumed_notice_span_reports_count_and_oldest() -> None:
    from main.runtime.append_notice_context import archived_consumed_notice_span

    context = _context([_record(index) for index in range(7)])
    count, oldest = archived_consumed_notice_span(context)
    assert count == 2
    assert oldest == '2026-10-01T00:00:00+08:00'
    assert archived_consumed_notice_span(_context([_record(1)])) == (0, '')


def test_token_compaction_block_records_retired_notice_span() -> None:
    context = _context([_record(index) for index in range(7)])
    request_messages = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'bootstrap'},
        *[
            {
                'role': 'user' if index % 2 == 0 else 'assistant',
                'content': f'history {index}',
            }
            for index in range(40)
        ],
    ]
    rewritten, payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
        node_id='node:x',
        request_messages=request_messages,
        compressed_text='summary',
        recent_tail_count=4,
        append_notice_context=context,
    )
    assert payload['retired_notice_count'] == 2
    assert payload['oldest_retired_notice_at'] == '2026-10-01T00:00:00+08:00'
    block = next(
        item
        for item in rewritten
        if str(item.get('content') or '').startswith('[TOKEN_COMPACT_V2]')
    )
    metadata = json.loads(str(block['content']).splitlines()[1])
    assert metadata['retired_notice_count'] == 2
    assert metadata['oldest_retired_notice_at'] == '2026-10-01T00:00:00+08:00'


def test_token_compaction_block_omits_retired_span_without_archive() -> None:
    context = _context([_record(index) for index in range(5)])
    request_messages = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'bootstrap'},
        *[
            {
                'role': 'user' if index % 2 == 0 else 'assistant',
                'content': f'history {index}',
            }
            for index in range(40)
        ],
    ]
    _rewritten, payload = ReActToolLoop._rewrite_request_messages_for_token_compaction(
        node_id='node:x',
        request_messages=request_messages,
        compressed_text='summary',
        recent_tail_count=4,
        append_notice_context=context,
    )
    assert 'retired_notice_count' not in payload
    assert 'oldest_retired_notice_at' not in payload


def test_legacy_notice_index_message_is_stripped_from_prompt_tail() -> None:
    # 索引行不再由任何写入侧产出；存量消息按前缀识别后照旧在 prompt_tail 剥离。
    message = {
        'role': 'user',
        'content': f'{NOTICE_INDEX_PREFIX}\nOlder consumed notices outside the live window: 2',
        'metadata': {'ui_visible': False},
    }
    assert is_notice_index_message(message)
    assert mi.classify_message(message) == mi.KIND_NOTICE_INDEX
    assert mi.strip_for_stage(
        [message],
        stage=mi.STAGE_PROMPT_TAIL,
        lane=mi.LANE_NODE,
    ) == []
