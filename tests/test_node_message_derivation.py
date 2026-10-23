"""机械推导 contract for node-mailbox messages.

These cases pin the two guarantees the derivation exists for:
1. a model-sent message can never claim to be a terminal result or a verdict;
2. priority follows "did the sender park", not "what type is it".
"""

from __future__ import annotations

import pytest

from main.models import TaskNodeNotification
from main.runtime.node_message_kind import (
    INTENT_INSTRUCTION,
    INTENT_PROGRESS,
    INTENT_REQUEST,
    KIND_ACCEPTANCE_VERDICT,
    KIND_CHILD_ABNORMAL_PAUSE,
    KIND_CHILD_FINAL,
    KIND_CHILD_PROGRESS,
    KIND_FRONTDOOR_NOTICE,
    KIND_PARENT_NOTICE,
    KIND_USER_DIRECT,
    ORIGIN_FRONTDOOR,
    ORIGIN_NODE,
    ORIGIN_USER,
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    RELATION_CHILD,
    RELATION_PARENT,
    VERDICT_PASS,
    VERDICT_REJECT,
    VERDICT_REJECT_NO_PROGRESS,
    MessageClassificationError,
    classify,
    derive_message_kind,
    derive_priority,
    validate_intent_kind,
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

    @pytest.mark.parametrize('kind', [KIND_PARENT_NOTICE, KIND_CHILD_PROGRESS])
    def test_request_intent_is_high(self, kind):
        assert derive_priority(kind=kind, intent_kind=INTENT_REQUEST) == PRIORITY_HIGH

    @pytest.mark.parametrize('intent', [INTENT_PROGRESS, INTENT_INSTRUCTION, ''])
    @pytest.mark.parametrize('kind', [KIND_PARENT_NOTICE, KIND_CHILD_PROGRESS])
    def test_non_request_intents_are_normal(self, kind, intent):
        assert derive_priority(kind=kind, intent_kind=intent) == PRIORITY_NORMAL

    def test_terminal_result_is_normal(self):
        assert derive_priority(kind=KIND_CHILD_FINAL) == PRIORITY_NORMAL

    def test_empty_kind_rejected(self):
        with pytest.raises(MessageClassificationError):
            derive_priority(kind='')


class TestValidators:
    def test_intent_defaults_to_progress(self):
        assert validate_intent_kind('') == INTENT_PROGRESS

    @pytest.mark.parametrize('intent', [INTENT_PROGRESS, INTENT_REQUEST, INTENT_INSTRUCTION])
    def test_known_intents(self, intent):
        assert validate_intent_kind(intent) == intent

    def test_unknown_intent_rejected(self):
        with pytest.raises(MessageClassificationError):
            validate_intent_kind('索要')

    @pytest.mark.parametrize('verdict', [VERDICT_PASS, VERDICT_REJECT, VERDICT_REJECT_NO_PROGRESS])
    def test_known_verdicts(self, verdict):
        assert validate_verdict(verdict) == verdict

    @pytest.mark.parametrize('verdict', ['', 'partial', '通过'])
    def test_unknown_verdict_rejected(self, verdict):
        with pytest.raises(MessageClassificationError):
            validate_verdict(verdict)


def test_classify_pairs_kind_and_priority():
    assert classify(
        origin=ORIGIN_NODE, relation=RELATION_PARENT, intent_kind=INTENT_REQUEST
    ) == (KIND_PARENT_NOTICE, PRIORITY_HIGH)
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
        assert record.submission_id == ''
        assert record.verdict == ''

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
        )
        restored = TaskNodeNotification.model_validate_json(record.model_dump_json())
        assert restored.kind == KIND_ACCEPTANCE_VERDICT
        assert restored.priority == PRIORITY_HIGH
        assert restored.submission_id == 'sub:9'
        assert restored.verdict == VERDICT_REJECT_NO_PROGRESS
