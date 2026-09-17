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
