"""Member send-size span: one ruler for both arms, and it reports without acting.

Live trigger (2026-10-25, task:4555863a6a49): a SenseNova-hosted member answered
285,391-token bodies and rejected 287,020 with a generic
`inference request is invalid`, while every record on that chain declared
`context_window_tokens=390000`, so neither admission nor compression shrank anything.
Comparing the provider-reported max (accepted arm) against our own estimate (rejected
arm) produced an *inverted* span (261,853 vs 251,793) — these tests pin that the span is
built only from the send-side estimate, and that a rejection below the accepted maximum
is refused as evidence.
"""

from __future__ import annotations

from typing import Any

import pytest

from main.monitoring.log_service import TaskLogService
from main.runtime import model_size_span
from main.runtime.react_loop import ReActToolLoop


@pytest.fixture(autouse=True)
def _clean_state():
    model_size_span.reset_for_tests()
    yield
    model_size_span.reset_for_tests()


class TestSpanInference:
    def test_no_span_before_any_rejection(self):
        state = model_size_span.note_send_size('seva-lite', tokens=200_000, accepted=True)
        assert state['known'] is False
        assert model_size_span.span_for('seva-lite') == {}

    def test_span_needs_an_accepted_size_below_the_rejected_one(self):
        model_size_span.note_send_size('seva-lite', tokens=285_391, accepted=True)
        state = model_size_span.note_send_size(
            'seva-lite', tokens=287_020, accepted=False, declared_window_tokens=390_000,
            compression_trigger_tokens=296_400,
        )
        assert state['known'] is True
        assert state['accepted_max'] == 285_391
        assert state['rejected_min'] == 287_020
        assert state['changed'] is True

    def test_rejection_smaller_than_the_accepted_max_is_not_evidence(self):
        # 这就是混尺子的形状：usage 口径的接受最大值高于 estimate 口径的拒绝最小值。
        model_size_span.note_send_size('mixed', tokens=261_853, accepted=True)
        state = model_size_span.note_send_size('mixed', tokens=251_793, accepted=False)
        assert state['known'] is False
        assert model_size_span.span_for('mixed') == {}

    def test_changed_only_on_first_inference_and_on_widening(self):
        model_size_span.note_send_size('fl', tokens=285_391, accepted=True)
        first = model_size_span.note_send_size('fl', tokens=287_020, accepted=False)
        again = model_size_span.note_send_size('fl', tokens=288_000, accepted=False)
        wider = model_size_span.note_send_size('fl', tokens=286_000, accepted=False)
        assert first['changed'] is True
        assert again['changed'] is False, 'a repeat rejection inside the same span owes no new line'
        assert wider['changed'] is True, 'a narrower floor is new information'
        assert wider['rejected_count'] == 3

    def test_key_normalization_and_garbage_input(self):
        assert model_size_span.note_send_size('', tokens=100, accepted=True)['known'] is False
        assert model_size_span.note_send_size('Sens', tokens=0, accepted=True)['known'] is False
        model_size_span.note_send_size('Sens', tokens=100_000, accepted=True)
        # 区间要等一次更高的拒绝才成立；键按大小写折叠共用同一份状态。
        model_size_span.note_send_size('SENS', tokens=120_000, accepted=False)
        assert model_size_span.span_for('sens')['accepted_max'] == 100_000


class TestReportText:
    def test_describe_carries_the_four_numbers_and_the_no_write_clause(self):
        model_size_span.note_send_size('seva-lite', tokens=285_391, accepted=True)
        state = model_size_span.note_send_size(
            'seva-lite', tokens=287_020, accepted=False,
            declared_window_tokens=390_000, compression_trigger_tokens=296_400,
        )
        state['model_key'] = 'seva-lite'
        text = model_size_span.describe_span(state)
        assert '285,391' in text and '287,020' in text
        assert '390,000' in text and '296,400' in text
        assert 'Runtime does not change configuration' in text
        assert 'same send-side ruler' in text

    def test_no_text_without_evidence(self):
        assert model_size_span.describe_span({}) == ''


class TestSendSideRulerIsCarriedOnBothArms:
    """两臂同尺的前提：逐跳行里得有发送前那一个数，且不被 `applied` 门挡住。"""

    def test_reactor_projection_keeps_the_size_fields_without_compaction(self):
        payload = ReActToolLoop._node_send_compaction_diagnostics({
            'applied': False,
            'final_request_tokens': 287_020,
            'effective_trigger_tokens': 296_400,
            'context_window_tokens': 390_000,
            'would_trigger_token_compression': False,
            'ruler_source': 'group:v4',
        })
        assert payload['compaction_applied'] is False
        assert payload['final_request_tokens'] == 287_020
        assert payload['effective_trigger_tokens'] == 296_400
        assert payload['context_window_tokens'] == 390_000
        assert payload['would_trigger_token_compression'] is False

    def test_log_whitelist_is_the_second_door_and_must_not_drop_them(self):
        # 串联两道只补一道时，键会在、值是空——所以这里按同一份输入分别过一遍。
        projected = ReActToolLoop._node_send_compaction_diagnostics({
            'applied': True,
            'mode': 'llm',
            'final_request_tokens': 285_391,
            'effective_trigger_tokens': 296_400,
            'context_window_tokens': 390_000,
            'would_trigger_token_compression': True,
        })
        row = TaskLogService._send_diagnostics_fields(projected)
        assert row['final_request_tokens'] == 285_391
        assert row['effective_trigger_tokens'] == 296_400
        assert row['context_window_tokens'] == 390_000
        assert row['would_trigger_token_compression'] is True

    def test_missing_estimate_degrades_to_zero_not_absent(self):
        row = TaskLogService._send_diagnostics_fields({'final_request_tokens': 0})
        assert row['final_request_tokens'] == 0


class TestServedMemberAttribution:
    def test_last_attempt_wins(self):
        response = type('R', (), {'attempts': [{'model_key': 'a'}, {'model_key': 'sensenova-6.8-flash-lite'}]})()
        assert ReActToolLoop._served_attempt_model_key(response) == 'sensenova-6.8-flash-lite'

    def test_object_attempts_and_absence(self):
        attempt = type('A', (), {'model_key': 'glm-5.2-2'})()
        assert ReActToolLoop._served_attempt_model_key(type('R', (), {'attempts': [attempt]})()) == 'glm-5.2-2'
        assert ReActToolLoop._served_attempt_model_key(type('R', (), {'attempts': []})()) == ''


class _FakeLogService:
    def __init__(self) -> None:
        self.rows: list[str] = []

    def append_task_error_log(self, task_id: str, node_id: str, *, error_text: str, node_title: str = '') -> None:
        self.rows.append(f'{task_id}|{node_id}|{node_title}|{error_text}')

    def raise_here(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError('ledger offline')


def _loop() -> ReActToolLoop:
    loop = object.__new__(ReActToolLoop)
    loop._log_service = _FakeLogService()  # type: ignore[attr-defined]
    return loop


_DIAG = {
    'final_request_tokens': 285_391,
    'effective_trigger_tokens': 296_400,
    'context_window_tokens': 390_000,
    'model_key': 'deepseek-flash',
}


class TestFeedWritesOneReportPerNewSpan:
    def _response(self, *, finish: str, model_key: str, tokens: int) -> Any:
        return type('R', (), {
            'finish_reason': finish,
            'attempts': [{'model_key': model_key}],
            'usage': {'input_tokens': tokens},
        })()

    def test_accepted_then_rejected_produces_exactly_one_line(self):
        loop = _loop()
        loop._note_model_size_span(
            response=self._response(finish='stop', model_key='seva-lite', tokens=285_391),
            diagnostics=dict(_DIAG), task_id='task:x', node_id='node:a', node_title='甲',
        )
        assert loop._log_service.rows == []  # 只有接受臂时不该报
        loop._note_model_size_span(
            response=self._response(finish='error', model_key='seva-lite', tokens=287_020),
            diagnostics={**_DIAG, 'final_request_tokens': 287_020},
            task_id='task:x', node_id='node:a', node_title='甲',
        )
        assert len(loop._log_service.rows) == 1
        assert '285,391' in loop._log_service.rows[0] and 'task:x|node:a|甲' in loop._log_service.rows[0]
        # 同一区间内再拒一次：不重复占位
        loop._note_model_size_span(
            response=self._response(finish='error', model_key='seva-lite', tokens=288_000),
            diagnostics={**_DIAG, 'final_request_tokens': 288_000},
            task_id='task:x', node_id='node:a', node_title='甲',
        )
        assert len(loop._log_service.rows) == 1

    def test_no_estimate_means_no_bookkeeping(self):
        loop = _loop()
        loop._note_model_size_span(
            response=self._response(finish='error', model_key='seva-lite', tokens=1),
            diagnostics={}, task_id='task:x', node_id='node:a', node_title='甲',
        )
        assert loop._log_service.rows == []
        assert model_size_span.span_for('seva-lite') == {}

    def test_ledger_failure_does_not_break_the_hop(self):
        loop = _loop()
        loop._log_service.raise_here = None  # type: ignore[attr-defined]

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError('ledger offline')

        loop._log_service.append_task_error_log = _boom  # type: ignore[attr-defined]
        loop._note_model_size_span(
            response=self._response(finish='stop', model_key='seva-lite', tokens=285_391),
            diagnostics=dict(_DIAG), task_id='task:x', node_id='node:a', node_title='甲',
        )
        loop._note_model_size_span(
            response=self._response(finish='error', model_key='seva-lite', tokens=287_020),
            diagnostics={**_DIAG, 'final_request_tokens': 287_020},
            task_id='task:x', node_id='node:a', node_title='甲',
        )
        # 写失败也不能上抛，且状态已经记下（下一次仍然会尝试报）
        assert model_size_span.span_for('seva-lite')['rejected_min'] == 287_020


class TestRejectionWithoutAnyReply:
    """A rejected hop that never got a response still has to reach the interval.

    Live 2026-10-26 (task:578bebcdf701 node:20bb6c2ea7d1): the provider answered 288,991
    and then 400'd the next send with no reply at all, so the per-hop ledger never wrote a
    row and the rejected arm stayed empty forever.
    """

    def test_rejected_flag_needs_no_response_object(self):
        loop = _loop()
        loop._note_model_size_span(
            response=self._accepted('seva-lite', 285_391),
            diagnostics=dict(_DIAG), task_id='task:x', node_id='node:a', node_title='甲',
        )
        text = loop._note_model_size_span(
            response=None,
            diagnostics={**_DIAG, 'final_request_tokens': 325_000},
            task_id='task:x', node_id='node:a', node_title='甲',
            rejected=True,
            model_key_override='seva-lite',
        )
        assert 'model_size_span: seva-lite' in text
        assert '325,000' in text
        assert text in loop._log_service.rows[0]

    def test_absent_response_without_the_flag_would_be_miscounted_as_accepted(self):
        """Why `rejected=True` exists: `finish_reason` on no response reads as success."""
        loop = _loop()
        loop._note_model_size_span(
            response=self._accepted('seva-lite', 285_391),
            diagnostics=dict(_DIAG), task_id='task:x', node_id='node:a', node_title='甲',
        )
        text = loop._note_model_size_span(
            response=None,
            diagnostics={**_DIAG, 'final_request_tokens': 325_000},
            task_id='task:x', node_id='node:a', node_title='甲',
            model_key_override='seva-lite',
        )
        assert text == ''
        assert loop._log_service.rows == []

    def test_no_report_until_the_interval_closes(self):
        loop = _loop()
        text = loop._note_model_size_span(
            response=None,
            diagnostics={**_DIAG, 'final_request_tokens': 325_000},
            task_id='task:x', node_id='node:a', node_title='甲',
            rejected=True,
            model_key_override='lonely-member',
        )
        assert text == ''

    def test_member_comes_from_the_lease_when_nothing_was_served(self):
        loop = _loop()
        loop._note_model_size_span(
            response=self._accepted('sensenova-6.8-flash-lite', 285_391),
            diagnostics=dict(_DIAG), task_id='task:x', node_id='node:a', node_title='甲',
        )
        # 接单成员由租约点名，诊断里的 model_key 不能把这一笔挪到别人头上——
        # 否则墙会算到一颗根本没被这一发问过的成员上。
        loop._note_model_size_span(
            response=None,
            diagnostics={**_DIAG, 'final_request_tokens': 325_000, 'model_key': 'glm-5.2-2'},
            task_id='task:x', node_id='node:a', node_title='甲',
            rejected=True,
            model_key_override='sensenova-6.8-flash-lite',
        )
        assert model_size_span.span_for('sensenova-6.8-flash-lite')['rejected_min'] == 325_000
        assert model_size_span.span_for('glm-5.2-2') == {}

    @staticmethod
    def _accepted(model_key: str, tokens: int) -> Any:
        return type('R', (), {
            'finish_reason': 'stop',
            'attempts': [{'model_key': model_key}],
            'usage': {'input_tokens': tokens},
        })()


class TestSizeRejectionGate:
    def test_structured_400_is_size_evidence(self):
        exc = RuntimeError('inference request is invalid')
        exc.status_code = 400  # type: ignore[attr-defined]
        assert ReActToolLoop._is_size_rejection_sample(exc) is True

    def test_throttling_is_not_size_evidence(self):
        exc = RuntimeError('rate limited')
        exc.status_code = 429  # type: ignore[attr-defined]
        assert ReActToolLoop._is_size_rejection_sample(exc) is False

    def test_words_about_invalid_request_are_not_size_evidence(self):
        # 同一批网关把 429 也标成 invalid_request_error，按文本判会把墙算低。
        assert ReActToolLoop._is_size_rejection_sample(
            RuntimeError("{'message': 'inference request is invalid', 'type': 'invalid_request_error'}")
        ) is False

    def test_timeout_is_not_size_evidence(self):
        assert ReActToolLoop._is_size_rejection_sample(RuntimeError('read timeout')) is False
