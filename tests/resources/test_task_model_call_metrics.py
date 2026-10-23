from __future__ import annotations

from g3ku.providers.base import LLMModelAttempt, normalize_usage_payload
from g3ku.providers.streaming_timeouts import StreamingDiagnostics
from main.monitoring.log_service import TaskLogService


def _attempt(**overrides) -> LLMModelAttempt:
    payload = {
        'model_key': 'm',
        'provider_id': 'openai',
        'provider_model': 'gpt-x',
        'usage': {'input_tokens': 10, 'output_tokens': 2},
    }
    payload.update(overrides)
    return LLMModelAttempt(**payload)


def test_model_call_metrics_sum_durations_and_thinking_tokens() -> None:
    metrics = TaskLogService._model_call_attempt_metrics(
        [
            _attempt(duration_ms=120.4, first_token_ms=30.0, usage={'thinking_tokens': 40}),
            _attempt(duration_ms=880.6, first_token_ms=210.6, usage={'thinking_tokens': 60}),
        ]
    )

    assert metrics['duration_ms'] == 1001
    # 首 token 取最后一次上报值，而不是最小值：重试链上先失败的请求可能更快返回。
    assert metrics['first_token_ms'] == 211
    assert metrics['thinking_tokens'] == 100


def test_model_call_metrics_keep_missing_gauges_as_none() -> None:
    metrics = TaskLogService._model_call_attempt_metrics([_attempt()])

    assert metrics == {'duration_ms': None, 'first_token_ms': None, 'thinking_tokens': None}


def test_model_call_metrics_report_zero_thinking_tokens_when_provider_says_zero() -> None:
    metrics = TaskLogService._model_call_attempt_metrics([_attempt(usage={'thinking_tokens': 0})])

    assert metrics['thinking_tokens'] == 0


def test_model_call_metrics_without_attempts_returns_all_none() -> None:
    assert TaskLogService._model_call_attempt_metrics(None) == {
        'duration_ms': None,
        'first_token_ms': None,
        'thinking_tokens': None,
    }


def test_normalize_usage_payload_reads_nested_reasoning_tokens() -> None:
    assert normalize_usage_payload(
        {
            'prompt_tokens': 100,
            'completion_tokens': 60,
            'completion_tokens_details': {'reasoning_tokens': 45},
        }
    )['thinking_tokens'] == 45

    # 未上报时不能写入 0，否则前端无法区分「没思考」与「provider 没回传」。
    assert 'thinking_tokens' not in normalize_usage_payload(
        {'prompt_tokens': 100, 'completion_tokens': 60}
    )


def test_streaming_diagnostics_first_token_ms_prefers_first_chunk() -> None:
    diagnostics = StreamingDiagnostics.start('test')
    assert diagnostics.first_token_ms() is None

    diagnostics.first_chunk_received_at = diagnostics.started_at + 0.25
    diagnostics.first_text_delta_received_at = diagnostics.started_at + 0.75
    assert diagnostics.first_token_ms() == 250.0

    # 只测到文本增量（没有更早的分片）时退回文本增量。
    delta_only = StreamingDiagnostics.start('test2')
    delta_only.first_text_delta_received_at = delta_only.started_at + 0.5
    assert delta_only.first_token_ms() == 500.0


def test_streaming_diagnostics_measures_from_the_request_start() -> None:
    import time

    request_started_at = time.perf_counter() - 0.4
    diagnostics = StreamingDiagnostics.start('test', started_at=request_started_at)
    diagnostics.note_chunk('chunk')

    # 起点由调用方在发请求前取：从"开始迭代响应"起算会得到亚毫秒的假读数。
    assert 380.0 <= diagnostics.first_token_ms() <= 460.0


def test_token_usage_counts_calls_not_attempts() -> None:
    from main.token_usage import build_token_usage_from_attempts

    summary, by_model = build_token_usage_from_attempts(
        [
            _attempt(model_key='flash', usage={}),
            _attempt(model_key='flash', usage={}),
            _attempt(model_key='v4', usage={'input_tokens': 100, 'output_tokens': 5, 'cache_hit_tokens': 20}),
        ],
        tracked=True,
    )

    # 一次调用试了三颗成员：调用数是 1，不是 3；被服务的那一发有 usage。
    assert summary.call_count == 1
    assert summary.calls_with_usage == 1
    assert summary.calls_without_usage == 0
    assert summary.is_partial is False
    assert summary.input_tokens == 100
    assert sorted((item.model_key, item.call_count) for item in by_model) == [('flash', 0), ('v4', 1)]


def test_token_usage_marks_partial_only_when_the_call_had_no_usage() -> None:
    from main.token_usage import build_token_usage_from_attempts

    summary, _by_model = build_token_usage_from_attempts(
        [_attempt(usage={}), _attempt(usage={})],
        tracked=True,
    )

    assert summary.call_count == 1
    assert summary.calls_with_usage == 0
    assert summary.calls_without_usage == 1
    assert summary.is_partial is True
