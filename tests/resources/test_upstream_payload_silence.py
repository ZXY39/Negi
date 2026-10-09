from __future__ import annotations

import time

import pytest
from loguru import logger

from g3ku.providers.fallback import (
    MINUTE_WINDOW_RETRY_BASE_SECONDS,
    MINUTE_WINDOW_RETRY_CAP_SECONDS,
    model_retry_backoff_seconds,
)
from g3ku.providers.responses_provider import _SSEDiagnosticsResponseProxy
from g3ku.providers.streaming_timeouts import (
    UPSTREAM_PAYLOAD_SILENCE_REFRESH_SECONDS,
    UPSTREAM_PAYLOAD_SILENCE_SECONDS,
    StreamingDiagnostics,
    is_payload_chunk_kind,
    notice_payload_silence,
)
from g3ku.utils.retry_keywords import classify_throttle_dimension, is_minute_window_throttle

_TPM_TEXT = "_RetryableResponsesError: HTTP 429: inference exceeds tpm/rpm limit"
_RPM_TEXT = "Error calling Responses API: HTTP 429: rpm exhausted"
_ENTITLEMENT_TEXT = "HTTP 429: token plan entitlement exhausted"
_RPS_TEXT = "Error code: 429 - rps limit"
_NETWORK_TEXT = "HTTP 429: too many requests"


def test_minute_window_dimensions_are_the_only_ones_paced_by_a_window() -> None:
    assert classify_throttle_dimension(_TPM_TEXT) == "rpm"
    assert is_minute_window_throttle(_TPM_TEXT) is True
    assert is_minute_window_throttle(_RPM_TEXT) is True
    # 额度打光与亚秒窗口都不值得等到一分钟：前者等多久都不会自己恢复，
    # 后者下一拍就通。
    assert is_minute_window_throttle(_ENTITLEMENT_TEXT) is False
    assert is_minute_window_throttle(_RPS_TEXT) is False
    assert is_minute_window_throttle(_NETWORK_TEXT) is False
    assert is_minute_window_throttle("") is False


def test_minute_window_throttle_paces_by_whole_windows() -> None:
    first = model_retry_backoff_seconds(1, error_text=_TPM_TEXT)
    assert MINUTE_WINDOW_RETRY_BASE_SECONDS * 0.7 <= first <= MINUTE_WINDOW_RETRY_BASE_SECONDS * 1.3
    third = model_retry_backoff_seconds(3, error_text=_RPM_TEXT)
    assert third > MINUTE_WINDOW_RETRY_BASE_SECONDS * 2
    tenth = model_retry_backoff_seconds(10, error_text=_RPM_TEXT)
    assert tenth <= MINUTE_WINDOW_RETRY_CAP_SECONDS * 1.3


def test_other_failures_keep_the_exponential_pace() -> None:
    for text in (_ENTITLEMENT_TEXT, _RPS_TEXT, _NETWORK_TEXT, ""):
        delay = model_retry_backoff_seconds(1, error_text=text)
        assert delay <= MINUTE_WINDOW_RETRY_BASE_SECONDS, text
    assert model_retry_backoff_seconds(2) < model_retry_backoff_seconds(5, error_text=_NETWORK_TEXT)


def test_payload_kind_vocabulary_matches_both_protocols() -> None:
    payload = [
        "text_delta",
        "reasoning_delta",
        "tool_call_delta",
        "data:response.output_text.delta",
        "data:response.reasoning_summary_text.delta",
        "data:response.function_call_arguments.delta",
    ]
    scaffolding = [
        "chunk",
        "line",
        "non_choice_chunk",
        "event:response.created",
        "event:response.in_progress",
        "data:unknown",
    ]
    assert [is_payload_chunk_kind(kind) for kind in payload] == [True] * len(payload)
    assert [is_payload_chunk_kind(kind) for kind in scaffolding] == [False] * len(scaffolding)


def _silent_diagnostics() -> StreamingDiagnostics:
    diagnostics = StreamingDiagnostics.start("responses")
    diagnostics.started_at = time.perf_counter() - (UPSTREAM_PAYLOAD_SILENCE_SECONDS + 1)
    for _ in range(3):
        diagnostics.note_chunk("line")
    return diagnostics


@pytest.mark.asyncio
async def test_silence_notice_fires_once_then_refreshes_on_a_beat() -> None:
    seen: list[dict[str, int]] = []

    async def _callback(info: dict[str, int]) -> None:
        seen.append(dict(info))

    diagnostics = _silent_diagnostics()
    await notice_payload_silence(diagnostics, _callback)
    await notice_payload_silence(diagnostics, _callback)
    assert len(seen) == 1
    assert seen[0]["waiting_seconds"] >= int(UPSTREAM_PAYLOAD_SILENCE_SECONDS)
    assert seen[0]["chunk_count"] == 3

    diagnostics.silence_last_report_at = time.perf_counter() - UPSTREAM_PAYLOAD_SILENCE_REFRESH_SECONDS
    await notice_payload_silence(diagnostics, _callback)
    assert len(seen) == 2

    # 载荷分片一到就复位：正常的长思考不能被报成等待上游。
    diagnostics.note_chunk("data:response.reasoning_summary_text.delta")
    await notice_payload_silence(diagnostics, _callback)
    assert len(seen) == 2
    assert diagnostics.payload_silence_seconds() < 1.0


@pytest.mark.asyncio
async def test_silence_notice_writes_one_forensic_log_line_per_stall() -> None:
    records: list[str] = []
    sink_id = logger.add(lambda message: records.append(str(message)), level="WARNING")
    try:
        diagnostics = _silent_diagnostics()
        for _ in range(5):
            await notice_payload_silence(diagnostics, lambda info: None)
            diagnostics.silence_last_report_at = time.perf_counter() - UPSTREAM_PAYLOAD_SILENCE_REFRESH_SECONDS
    finally:
        logger.remove(sink_id)

    anchors = [line for line in records if "no payload chunk for" in line]
    assert len(anchors) == 1
    assert "line:3" in anchors[0]


class _FakeStreamedResponse:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def aiter_lines(self):
        async def _generate():
            for line in self._lines:
                yield line

        return _generate()


@pytest.mark.asyncio
async def test_responses_stream_reports_silence_from_the_line_loop() -> None:
    seen: list[dict[str, int]] = []

    def _callback(info: dict[str, int]) -> None:
        seen.append(dict(info))

    lines = [
        "event:response.output_text.delta",
        "data:response.output_text.delta",
        "event:response.keepalive",
        "data:response.keepalive",
        ": keep-alive",
        "event:response.output_item.added",
        "data:response.output_item.added",
    ]
    proxy = _SSEDiagnosticsResponseProxy(
        _FakeStreamedResponse(lines),
        first_line_timeout_seconds=5.0,
        idle_line_timeout_seconds=5.0,
        on_upstream_wait=_callback,
    )

    iterator = proxy.aiter_lines().__aiter__()
    assert [await iterator.__anext__() for _ in range(2)] == lines[:2]
    assert seen == []

    # 正文之后就只剩 keep-alive：把最后一个载荷分片的到达时刻推到阈值之前，
    # 后续每一行都必须把「等待上游」报出来，且只报一次。
    proxy._diagnostics.last_payload_received_at -= UPSTREAM_PAYLOAD_SILENCE_SECONDS + 1
    rest = []
    while True:
        try:
            rest.append(await iterator.__anext__())
        except StopAsyncIteration:
            break
    assert rest == lines[2:]
    assert len(seen) == 1
    # 计数是上报那一刻的累计值：第 3 行（第一个 keep-alive）越阈，后面几行被刷新窗口拦住。
    assert seen[0]["chunk_count"] == 3
    assert seen[0]["waiting_seconds"] >= int(UPSTREAM_PAYLOAD_SILENCE_SECONDS)


@pytest.mark.asyncio
async def test_responses_stream_stays_quiet_while_payload_flows() -> None:
    seen: list[dict[str, int]] = []
    proxy = _SSEDiagnosticsResponseProxy(
        _FakeStreamedResponse(["event:response.output_text.delta", "data:response.output_text.delta"] * 4),
        first_line_timeout_seconds=5.0,
        idle_line_timeout_seconds=5.0,
        on_upstream_wait=lambda info: seen.append(info),
    )

    assert [line async for line in proxy.aiter_lines()]

    assert seen == []
