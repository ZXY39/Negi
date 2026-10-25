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
    REJECT_TARGET_TERMINAL,
    REJECT_UNKNOWN_TARGET_STATE,
    delivery_defers_wake,
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

    @pytest.mark.parametrize('origin', ['user', 'parent', 'node', 'frontdoor', 'system'])
    def test_paused_accepts_every_sender(self, origin):
        # 收件人是谁都由发送方决定不了；暂停只改变叫醒与否（见 delivery_defers_wake）。
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


class TestDeferredWake:
    def test_human_mail_to_a_paused_node_waits_for_the_resume_button(self):
        assert delivery_defers_wake(target_park_state=PARK_PAUSED, sender_origin='user')

    @pytest.mark.parametrize('origin', ['frontdoor', 'node', 'parent', 'system'])
    def test_machine_senders_still_wake_a_paused_node(self, origin):
        # 机器没有恢复按钮，唤醒就是它的处理手段；摘掉唤醒等于消息永远读不到。
        assert not delivery_defers_wake(target_park_state=PARK_PAUSED, sender_origin=origin)

    @pytest.mark.parametrize('state', [PARK_RUNNING, PARK_WAITING, PARK_TERMINAL])
    def test_only_the_paused_target_defers(self, state):
        assert not delivery_defers_wake(target_park_state=state, sender_origin='user')


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
