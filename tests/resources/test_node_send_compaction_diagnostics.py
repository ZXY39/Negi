"""节点压缩跳的读数合同。

这条车道原先在逐跳明细与 actual-request artifact 里**没有任何压缩标记**：
`token_preflight_diagnostics` 只写进运行帧且下一跳就被覆盖，所以"这一跳压没压"
只能靠 `prepared_message_count` 掉回十几条来反推（取证时按 `TOKEN_COMPACT_V2`
搜 8 天的 `task_model_calls` 是 0 命中）。这几条用例钉住标记位的形状，
并钉住"没压"与"缺字段"必须可区分。
"""

from __future__ import annotations

from main.monitoring.log_service import TaskLogService
from main.runtime.react_loop import ReActToolLoop

_LIVE_DIAGNOSTICS = {
    'applied': True,
    'mode': 'llm',
    'compression_helper_call': {
        'history_message_count': 207,
        'helper_usage': {
            'input_tokens': 289114,
            'output_tokens': 6021,
            'cache_hit_tokens': 262144,
        },
    },
    'estimate_source': 'preview_estimate',
    'comparable_to_previous_request': False,
}


def test_compacted_hop_marks_projection_kind_and_helper_usage() -> None:
    fields = ReActToolLoop._node_send_compaction_diagnostics(_LIVE_DIAGNOSTICS)
    assert fields['compaction_applied'] is True
    assert fields['compaction_mode'] == 'llm'
    assert fields['projection_kind'] == 'token_compacted'
    assert fields['comparable_to_previous_request'] is False
    # helper 用量是账外成本：节点累计与逐跳 delta_usage 求和逐字段相等，
    # 说明摘要调用从未进入任何读数面，只能靠这里抬出来的副本对账。
    assert fields['helper_usage']['input_tokens'] == 289114
    assert fields['helper_usage']['output_tokens'] == 6021


def test_uncompacted_hop_is_distinguishable_from_missing_field() -> None:
    fields = ReActToolLoop._node_send_compaction_diagnostics({'applied': False, 'mode': 'llm'})
    assert fields['compaction_applied'] is False
    # 未压的跳不得残留上一跳的 mode/projection，否则"每跳都在压"会被读成"没压"。
    assert fields['compaction_mode'] == ''
    assert fields['projection_kind'] == ''
    assert fields['helper_usage'] == {}
    assert set(fields) == {
        'compaction_applied',
        'compaction_mode',
        'projection_kind',
        'helper_usage',
        'estimate_source',
        'comparable_to_previous_request',
    }


def test_send_diagnostics_fields_are_whitelisted_and_typed() -> None:
    fields = TaskLogService._send_diagnostics_fields(None)
    assert fields == {
        'compaction_applied': False,
        'compaction_mode': '',
        'projection_kind': '',
        'helper_usage': {},
        'estimate_source': '',
        'comparable_to_previous_request': False,
    }
    junk = TaskLogService._send_diagnostics_fields(
        {
            'compaction_applied': True,
            'projection_kind': 'token_compacted',
            'helper_usage': 'not-a-dict',
            'comparable_to_previous_request': 1,
            'unrelated_key': 'x',
        }
    )
    assert junk['helper_usage'] == {}
    assert junk['comparable_to_previous_request'] is True
    assert 'unrelated_key' not in junk


def test_actual_request_artifact_carries_compaction_markers() -> None:
    payload = TaskLogService._actual_request_artifact_payload(
        task_id='task:sample',
        node_id='node:sample',
        call_index=90,
        created_at='2026-10-09T18:44:40+08:00',
        model_messages=[{'role': 'system', 'content': '执行节点'}],
        request_messages=[{'role': 'system', 'content': '执行节点'}],
        prompt_cache_key='cache-key-sample',
        request_message_count=1,
        request_message_chars=6,
        send_diagnostics=ReActToolLoop._node_send_compaction_diagnostics(_LIVE_DIAGNOSTICS),
    )
    assert payload is not None
    assert payload['compaction_applied'] is True
    assert payload['projection_kind'] == 'token_compacted'
    assert payload['helper_usage']['cache_hit_tokens'] == 262144
