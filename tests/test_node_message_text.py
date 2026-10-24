"""Message prefix rendering and the notice-tail dedupe interaction."""

from __future__ import annotations

from main.runtime.append_notice_context import build_append_notice_tail_messages
from main.runtime.node_message_text import (
    render_node_message_text,
    strip_node_message_prefix,
)


class TestPrefix:
    def test_parent_and_child_labels(self):
        parent = render_node_message_text(
            kind='parent_notice', source_node_id='node:p', message='do it'
        )
        child = render_node_message_text(
            kind='child_final', source_node_id='node:c', message='done'
        )
        assert parent == '来自父节点，ID是node:p，内容为：\ndo it'
        assert child == '来自子节点，ID是node:c，内容为：\ndone'

    def test_sibling_verifier_and_frontdoor_labels(self):
        assert render_node_message_text(
            kind='peer_notice', source_node_id='node:s', message='hi'
        ).startswith('来自同级节点')
        assert render_node_message_text(
            kind='acceptance_verdict', source_node_id='node:v', message='reject'
        ).startswith('来自验收节点')
        assert render_node_message_text(
            kind='frontdoor_notice', message='session'
        ) == '来自会话，内容为：\nsession'
        assert render_node_message_text(kind='user_direct', message='user') == (
            '来自用户，内容为：\nuser'
        )

    def test_system_relay_label_wins(self):
        assert render_node_message_text(
            kind='child_final', source_node_id='node:c', message='x', system_relay=True
        ).startswith('来自系统')

    def test_strip_round_trip(self):
        rendered = render_node_message_text(
            kind='child_progress', source_node_id='node:c', message='line1\nline2'
        )
        assert strip_node_message_prefix(rendered) == 'line1\nline2'

    def test_strip_does_not_eat_plain_text(self):
        assert strip_node_message_prefix('一行普通消息') == '一行普通消息'
        assert strip_node_message_prefix('来自父节点，但没有正文头') == '来自父节点，但没有正文头'


def test_tail_window_does_not_duplicate_a_prefixed_visible_message():
    context = {
        'notice_records': [
            {
                'notification_id': 'notif:1',
                'source_node_id': 'node:c',
                'message': 'done',
                'received_at': '2026-10-25T00:00:00+00:00',
                'consumed_at': '2026-10-25T00:00:01+00:00',
                'merged_at': '2026-10-25T00:00:01+00:00',
            }
        ],
        'compression_segments': [],
    }
    visible = [
        render_node_message_text(kind='child_final', source_node_id='node:c', message='done')
    ]
    assert build_append_notice_tail_messages(context, visible_user_messages=visible) == []
