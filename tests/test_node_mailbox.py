"""Delivery matrix, fingerprint dedupe, and target-list normalization."""

from __future__ import annotations

import pytest

from main.runtime.node_mailbox import (
    PARK_PAUSED,
    PARK_RUNNING,
    PARK_TERMINAL,
    PARK_WAITING,
    REJECT_TARGET_LIST_UNPARSED,
    REJECT_TARGET_NOT_IN_TASK,
    REJECT_TARGET_PAUSED_NEEDS_RESUME,
    REJECT_TARGET_TERMINAL,
    REJECT_UNKNOWN_TARGET_STATE,
    message_fingerprint,
    normalize_targets,
    plan_delivery,
    unresolved_target_reason,
)


class TestDeliveryMatrix:
    @pytest.mark.parametrize('origin', ['user', 'frontdoor', 'node'])
    def test_terminal_rejects_every_sender(self, origin):
        assert plan_delivery(target_park_state=PARK_TERMINAL, sender_origin=origin) == (
            False,
            REJECT_TARGET_TERMINAL,
        )

    def test_paused_rejects_human_free_text(self):
        assert plan_delivery(target_park_state=PARK_PAUSED, sender_origin='user') == (
            False,
            REJECT_TARGET_PAUSED_NEEDS_RESUME,
        )

    @pytest.mark.parametrize('origin', ['parent', 'node', 'frontdoor', 'system'])
    def test_paused_accepts_machine_senders(self, origin):
        # 机器发送方没有恢复按钮，消息本身就是处理手段。
        assert plan_delivery(target_park_state=PARK_PAUSED, sender_origin=origin) == (True, '')

    @pytest.mark.parametrize('origin', ['user', 'frontdoor', 'node', 'parent'])
    @pytest.mark.parametrize('state', [PARK_RUNNING, PARK_WAITING])
    def test_running_and_waiting_accept_everything(self, state, origin):
        assert plan_delivery(target_park_state=state, sender_origin=origin) == (True, '')

    def test_unknown_state_rejected(self):
        assert plan_delivery(target_park_state='', sender_origin='node') == (
            False,
            REJECT_UNKNOWN_TARGET_STATE,
        )


class TestFingerprint:
    def test_stable_for_same_content(self):
        first = message_fingerprint(kind='parent_notice', source_node_id='a', message='上去看看')
        second = message_fingerprint(kind='parent_notice', source_node_id='a', message='  上去看看 ')
        assert first == second

    def test_distinguishes_content_kind_and_source(self):
        base = message_fingerprint(kind='parent_notice', source_node_id='a', message='x')
        assert base != message_fingerprint(kind='child_progress', source_node_id='a', message='x')
        assert base != message_fingerprint(kind='parent_notice', source_node_id='b', message='x')
        assert base != message_fingerprint(kind='parent_notice', source_node_id='a', message='y')

    def test_ref_participates(self):
        base = message_fingerprint(kind='parent_notice', source_node_id='a', message='x')
        assert base != message_fingerprint(
            kind='parent_notice', source_node_id='a', message='x', ref='artifact:1'
        )

    def test_is_short_and_hex(self):
        value = message_fingerprint(kind='k', source_node_id='s', message='m')
        assert len(value) == 16
        int(value, 16)


class TestNormalizeTargets:
    def test_single_value(self):
        assert normalize_targets('node:1') == ['node:1']

    def test_list_preserves_order_and_dedupes(self):
        assert normalize_targets(['b', 'a', 'b', ' a ']) == ['b', 'a']

    def test_empty_inputs(self):
        assert normalize_targets(None) == []
        assert normalize_targets([]) == []
        assert normalize_targets('   ') == []

    def test_accepts_set(self):
        assert sorted(normalize_targets({'a', 'b'})) == ['a', 'b']


class TestUnresolvedTargetReason:
    """地址档与形状档必须分开：模型要改的东西不同。"""

    def test_container_shaped_text_is_the_shape_arm(self):
        assert unresolved_target_reason('["node:a"') == REJECT_TARGET_LIST_UNPARSED
        assert unresolved_target_reason('{"a": 1}') == REJECT_TARGET_LIST_UNPARSED

    def test_plain_token_naming_no_node_is_the_address_arm(self):
        assert unresolved_target_reason('node:does-not-exist') == REJECT_TARGET_NOT_IN_TASK
        assert unresolved_target_reason('千反田爱瑠') == REJECT_TARGET_NOT_IN_TASK

    def test_empty_and_none_do_not_become_the_shape_arm(self):
        assert unresolved_target_reason('') == REJECT_TARGET_NOT_IN_TASK
        assert unresolved_target_reason(None) == REJECT_TARGET_NOT_IN_TASK
