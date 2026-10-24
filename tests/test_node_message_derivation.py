"""机械推导 contract for node-mailbox messages.

These cases pin the guarantees the derivation exists for:
1. a model-sent message can never claim to be a terminal result or a verdict;
2. priority is the runtime's judgement, not a sender-declared field.
"""

from __future__ import annotations

import pytest

from main.models import TaskNodeNotification
from main.runtime.node_message_kind import (
    KIND_ACCEPTANCE_VERDICT,
    KIND_CHILD_ABNORMAL_PAUSE,
    KIND_CHILD_FINAL,
    KIND_CHILD_PROGRESS,
    KIND_FRONTDOOR_NOTICE,
    KIND_PARENT_NOTICE,
    KIND_PEER_NOTICE,
    KIND_USER_DIRECT,
    ORIGIN_FRONTDOOR,
    ORIGIN_NODE,
    ORIGIN_USER,
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    RELATION_CHILD,
    RELATION_PARENT,
    RELATION_PEER,
    VERDICT_PASS,
    VERDICT_REJECT,
    VERDICT_REJECT_NO_PROGRESS,
    MessageClassificationError,
    classify,
    derive_message_kind,
    derive_priority,
    validate_verdict,
)


class TestDeriveKind:
    @pytest.mark.parametrize(
        ('origin', 'relation', 'expected'),
        [
            (ORIGIN_USER, '', KIND_USER_DIRECT),
            (ORIGIN_FRONTDOOR, '', KIND_FRONTDOOR_NOTICE),
            (ORIGIN_NODE, RELATION_PARENT, KIND_PARENT_NOTICE),
            (ORIGIN_NODE, RELATION_CHILD, KIND_CHILD_PROGRESS),
            (ORIGIN_NODE, RELATION_PEER, KIND_PEER_NOTICE),
        ],
    )
    def test_origin_and_relation(self, origin, relation, expected):
        assert derive_message_kind(origin=origin, relation=relation) == expected

    @pytest.mark.parametrize(
        'system_kind',
        [KIND_CHILD_FINAL, KIND_CHILD_ABNORMAL_PAUSE, KIND_ACCEPTANCE_VERDICT],
    )
    def test_system_kinds_pass_through(self, system_kind):
        assert derive_message_kind(origin='system', system_kind=system_kind) == system_kind

    def test_model_cannot_forge_a_system_kind(self):
        # 模型只有 origin/relation 可声明；这条钉住它拿不到终局与裁决两类。
        assert derive_message_kind(origin=ORIGIN_NODE, relation=RELATION_CHILD) == KIND_CHILD_PROGRESS
        assert derive_message_kind(origin=ORIGIN_NODE, relation=RELATION_PARENT) == KIND_PARENT_NOTICE
        assert derive_message_kind(origin=ORIGIN_NODE, relation=RELATION_PEER) == KIND_PEER_NOTICE

    def test_unknown_system_kind_rejected(self):
        with pytest.raises(MessageClassificationError):
            derive_message_kind(origin='system', system_kind='child_maybe')

    @pytest.mark.parametrize('origin', ['', 'agent', 'ceo'])
    def test_unknown_origin_rejected(self, origin):
        with pytest.raises(MessageClassificationError):
            derive_message_kind(origin=origin)

    def test_node_origin_requires_relation(self):
        with pytest.raises(MessageClassificationError):
            derive_message_kind(origin=ORIGIN_NODE)


class TestDerivePriority:
    @pytest.mark.parametrize(
        'kind',
        [KIND_USER_DIRECT, KIND_FRONTDOOR_NOTICE, KIND_CHILD_ABNORMAL_PAUSE],
    )
    def test_always_waking_kinds(self, kind):
        assert derive_priority(kind=kind) == PRIORITY_HIGH

    @pytest.mark.parametrize('verdict', [VERDICT_PASS, VERDICT_REJECT])
    def test_ordinary_verdicts_are_normal(self, verdict):
        assert derive_priority(kind=KIND_ACCEPTANCE_VERDICT, verdict=verdict) == PRIORITY_NORMAL

    def test_no_progress_verdict_is_high(self):
        assert (
            derive_priority(kind=KIND_ACCEPTANCE_VERDICT, verdict=VERDICT_REJECT_NO_PROGRESS)
            == PRIORITY_HIGH
        )

    @pytest.mark.parametrize(
        'kind',
        [KIND_PARENT_NOTICE, KIND_CHILD_PROGRESS, KIND_PEER_NOTICE, KIND_CHILD_FINAL],
    )
    def test_node_messages_are_normal(self, kind):
        assert derive_priority(kind=kind) == PRIORITY_NORMAL

    def test_empty_kind_rejected(self):
        with pytest.raises(MessageClassificationError):
            derive_priority(kind='')


class TestValidators:
    @pytest.mark.parametrize('verdict', [VERDICT_PASS, VERDICT_REJECT, VERDICT_REJECT_NO_PROGRESS])
    def test_known_verdicts(self, verdict):
        assert validate_verdict(verdict) == verdict

    @pytest.mark.parametrize('verdict', ['', 'partial', '通过'])
    def test_unknown_verdict_rejected(self, verdict):
        with pytest.raises(MessageClassificationError):
            validate_verdict(verdict)


def test_classify_pairs_kind_and_priority():
    assert classify(origin=ORIGIN_NODE, relation=RELATION_PARENT) == (
        KIND_PARENT_NOTICE,
        PRIORITY_NORMAL,
    )
    assert classify(origin=ORIGIN_NODE, relation=RELATION_PEER) == (
        KIND_PEER_NOTICE,
        PRIORITY_NORMAL,
    )
    assert classify(origin=ORIGIN_USER) == (KIND_USER_DIRECT, PRIORITY_HIGH)
    assert classify(
        origin='system', system_kind=KIND_ACCEPTANCE_VERDICT, verdict=VERDICT_REJECT
    ) == (KIND_ACCEPTANCE_VERDICT, PRIORITY_NORMAL)


class TestNotificationModel:
    def test_new_fields_default_empty_so_legacy_rows_parse(self):
        # 存量 payload_json 没有这些键；默认值必须让旧行照旧解析。
        record = TaskNodeNotification.model_validate(
            {
                'notification_id': 'notif:1',
                'task_id': 'task:1',
                'node_id': 'node:1',
                'epoch_id': 'epoch:1',
                'status': 'delivered',
                'created_at': '2026-10-23T00:00:00+00:00',
            }
        )
        assert record.kind == ''
        assert record.priority == ''
        assert record.intent_kind == ''
        assert record.submission_id == ''
        assert record.verdict == ''
        assert record.batch_id == ''
        assert record.held_at == ''
        assert record.released_at == ''

    def test_round_trip_keeps_derived_fields(self):
        record = TaskNodeNotification(
            notification_id='notif:2',
            task_id='task:1',
            node_id='node:1',
            epoch_id='epoch:1',
            status='delivered',
            created_at='2026-10-23T00:00:00+00:00',
            kind=KIND_ACCEPTANCE_VERDICT,
            priority=PRIORITY_HIGH,
            submission_id='sub:9',
            verdict=VERDICT_REJECT_NO_PROGRESS,
            batch_id='call:9',
            held_at='2026-10-23T00:00:01+00:00',
        )
        restored = TaskNodeNotification.model_validate_json(record.model_dump_json())
        assert restored.kind == KIND_ACCEPTANCE_VERDICT
        assert restored.priority == PRIORITY_HIGH
        assert restored.submission_id == 'sub:9'
        assert restored.verdict == VERDICT_REJECT_NO_PROGRESS
        assert restored.batch_id == 'call:9'
        assert restored.held_at == '2026-10-23T00:00:01+00:00'
