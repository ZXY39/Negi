"""节点车道必须把"实际发出去的参数"留成可回读的证据。

实盘依据（`.g3ku/main-runtime/runtime.sqlite3`，2026-09-20→10-10）：13 条
`Invalid final result submission detected ... reasoning-only` 全部 `output_tokens=65536`
且 `finish_reason=length`，而台账里 108 条同类行的 `sent_max_tokens` 出现 **0 次**
（`chat` 协议当时根本不回传请求体）。没有这两个标量，"思考把输出预算吃满"与
"窗口只剩一万六"在读数上同形，任何按档位做的处置都无法验收。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import g3ku.providers.openai_chat_provider as chat_provider_module
from g3ku.providers.base import LLMResponse
from g3ku.providers.openai_chat_provider import OpenAIChatProvider
from main.monitoring.log_service import TaskLogService
from main.runtime.react_loop import ReActToolLoop
from main.storage.sqlite_store import SQLiteTaskStore


class _StubCompletions:
    def __init__(self, sink: list[dict]) -> None:
        self._sink = sink

    async def create(self, **kwargs):
        self._sink.append(dict(kwargs))
        return object()


class _StubClient:
    def __init__(self, sink: list[dict]) -> None:
        self.chat = SimpleNamespace(completions=_StubCompletions(sink))


def _provider_with_stubbed_stream(monkeypatch, *, stream_result) -> tuple[OpenAIChatProvider, list[dict]]:
    sink: list[dict] = []
    provider = OpenAIChatProvider(api_key="k", api_base="https://example.test/v1", default_model="sens-x")
    provider._client = _StubClient(sink)

    async def _consume(stream, **kwargs):
        return stream_result

    monkeypatch.setattr(chat_provider_module, "consume_openai_like_chat_stream", _consume)
    return provider, sink


def test_chat_provider_attaches_sent_params_without_body(monkeypatch) -> None:
    provider, sink = _provider_with_stubbed_stream(
        monkeypatch,
        stream_result=("", [], "length", {"output_tokens": 65536}, "reasoning text"),
    )
    response = asyncio.run(
        provider.chat(
            [{"role": "user", "content": "长正文" * 50}],
            tools=[{"type": "function", "function": {"name": "exec"}}, {"type": "function", "function": {"name": "exec"}}],
            max_tokens=65536,
            temperature=0.1,
            reasoning_effort="xhigh",
        )
    )
    body = dict(response.provider_request_body or {})
    assert body["max_tokens"] == 65536
    assert body["reasoning_effort"] == "xhigh"
    assert body["model"] == "sens-x"
    assert body["tool_count"] == 2
    # 正文与工具束本体不进这份副本：sidecar 与台账会各复制一次。
    assert "messages" not in body
    assert "tools" not in body
    assert str((response.provider_request_meta or {}).get("provider")) == "openai_chat"
    assert sink, "流式路径必须真的发出过请求"


def test_chat_provider_omits_max_tokens_when_not_configured(monkeypatch) -> None:
    provider, _sink = _provider_with_stubbed_stream(
        monkeypatch,
        stream_result=("done", [], "stop", {"output_tokens": 3}, None),
    )
    response = asyncio.run(provider.chat([{"role": "user", "content": "hi"}], reasoning_effort=None))
    body = dict(response.provider_request_body or {})
    meta = dict(response.provider_request_meta or {})
    assert str(meta.get("provider") or "") == "openai_chat", "证据必须证明确实走了这条 provider"
    assert "max_tokens" not in body
    assert "reasoning_effort" not in body


def test_sent_param_evidence_keeps_absent_and_zero_apart() -> None:
    assert TaskLogService._sent_request_param_evidence({"max_tokens": 0, "reasoning_effort": "  "}) == {
        "sent_max_tokens": 0,
        "sent_reasoning_effort": "",
    }
    assert TaskLogService._sent_request_param_evidence({})["sent_max_tokens"] is None
    assert TaskLogService._sent_request_param_evidence(None)["sent_max_tokens"] is None


def test_sent_param_evidence_reads_responses_protocol_key_names() -> None:
    """Responses 车道的输出上限与档位键名和 Chat 不同，读错等于整条道没有发送面证据。

    实盘：切到 responses 模型的节点跳 `sent_max_tokens` 恒为 None（`max_output_tokens`
    与嵌套 `reasoning.effort` 没被读）。
    """
    evidence = TaskLogService._sent_request_param_evidence(
        {"model": "deepseek-v4-flash", "max_output_tokens": 32768, "reasoning": {"effort": "high"}}
    )
    assert evidence == {"sent_max_tokens": 32768, "sent_reasoning_effort": "high"}
    # 两种键名同时在场时以 Chat 名为准，不重复计。
    both = TaskLogService._sent_request_param_evidence({"max_tokens": 4096, "max_output_tokens": 32768})
    assert both["sent_max_tokens"] == 4096


def test_reasoning_only_label_works_on_responses_body() -> None:
    capped = LLMResponse(
        content="",
        finish_reason="length",
        usage={"output_tokens": 32768},
        reasoning_content="thinking",
        provider_request_body={"model": "deepseek-v4-flash", "max_output_tokens": 32768},
    )
    clamped = LLMResponse(
        content="",
        finish_reason="length",
        usage={"output_tokens": 6000},
        reasoning_content="thinking",
        provider_request_body={"model": "deepseek-v4-flash", "max_output_tokens": 32768},
    )
    assert ReActToolLoop._reasoning_only_shape_label(capped) == "output-capped"
    assert ReActToolLoop._reasoning_only_shape_label(clamped) == "window-clamped"


def _ledger_payload(**overrides) -> dict:
    args = {
        "task_id": "task:forensics",
        "node_id": "node:forensics",
        "call_index": 1,
        "model_messages": [{"role": "user", "content": "hi"}],
        "request_messages": None,
        "prompt_cache_key": "",
        "tool_calls": [],
        "delta_usage": SimpleNamespace(model_dump=lambda mode=None: {"output_tokens": 65536}),
        "delta_usage_by_model": [],
        "request_message_count": None,
        "request_message_chars": None,
    }
    args.update(overrides)
    return TaskLogService._model_call_payload(**args)


def test_model_call_payload_records_sent_scalars() -> None:
    payload = _ledger_payload(**TaskLogService._sent_request_param_evidence(
        {"max_tokens": 65536, "reasoning_effort": "xhigh"}
    ))
    assert payload["sent_max_tokens"] == 65536
    assert payload["sent_reasoning_effort"] == "xhigh"
    # 未上报必须读成"没这项"，不能塌成 0（0 是合法发送值，见上一条用例）。
    unset = _ledger_payload()
    assert unset["sent_max_tokens"] is None
    assert unset["sent_reasoning_effort"] == ""


def test_reasoning_only_label_splits_by_binding_limit() -> None:
    def _resp(*, finish_reason: str, output: int, sent_max) -> LLMResponse:
        body = {"model": "sens-x"}
        if sent_max is not None:
            body["max_tokens"] = sent_max
        return LLMResponse(
            content="",
            finish_reason=finish_reason,
            usage={"output_tokens": output},
            reasoning_content="thinking",
            provider_request_body=body,
        )

    label = ReActToolLoop._reasoning_only_shape_label
    # 顶满我们声明的上限：思考把输出预算吃光（实盘 13 条）。
    assert label(_resp(finish_reason="length", output=65536, sent_max=65536)) == "output-capped"
    # 截在比上限更小的值上：是上限之外的东西拦住了它，即窗口剩余（实盘 10 条，
    # 输入 241572/245792 时 provider 只给 20572/16352）。
    assert label(_resp(finish_reason="length", output=16352, sent_max=65536)) == "window-clamped"
    assert label(_resp(finish_reason="stop", output=512, sent_max=65536)) == "not-truncated"
    assert label(_resp(finish_reason="length", output=65536, sent_max=None)) == "unknown"


class _CapturingLogService:
    def __init__(self) -> None:
        self.error_logs: list[str] = []

    def append_task_error_log(self, task_id, node_id, *, error_text, node_title="", **kwargs) -> None:
        _ = task_id, node_id, node_title, kwargs
        self.error_logs.append(str(error_text))


def test_error_history_line_names_sent_cap_and_effort() -> None:
    log_service = _CapturingLogService()
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service)
    response = LLMResponse(
        content="",
        finish_reason="length",
        usage={"output_tokens": 65536},
        reasoning_content="thinking" * 100,
        provider_request_meta={"provider": "openai_chat", "endpoint": "https://example.test/v1/chat/completions"},
        provider_request_body={"model": "sens-x", "max_tokens": 65536, "reasoning_effort": "xhigh", "tool_count": 12},
    )
    loop._record_invalid_final_submission_error_log(
        task_id="task:forensics",
        node_id="node:forensics",
        node_title="抓取证据",
        count=1,
        reason="reply carried no tool call and no text (reasoning-only)",
        response=response,
        response_tool_calls=[],
    )
    assert len(log_service.error_logs) == 1
    text = log_service.error_logs[0]
    assert "sent_max_tokens=65536" in text
    assert "sent_reasoning_effort=xhigh" in text
    assert "provider_model=sens-x" in text
    assert "疑似触及输出token上限被截断" in text


def test_observed_request_span_is_per_node_and_null_safe(tmp_path) -> None:
    """窗口观测下界的读数口：按节点隔离，缺字段的行不能塌成 0。"""
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    assert store.max_observed_request_span_tokens('task:span', 'node:span') == 0
    for eff, out in ((106839, 65536), (245792, 16352), (241572, 20572)):
        store.append_task_model_call(
            task_id='task:span',
            node_id='node:span',
            created_at='2026-10-10T04:00:00+08:00',
            payload={'observed_input_truth': {'effective_input_tokens': eff}, 'delta_usage': {'output_tokens': out}},
        )
    # 没落 observed_input_truth 的行：和值为 NULL，MAX 忽略它，不能算成 0 拉低读数。
    store.append_task_model_call(
        task_id='task:span',
        node_id='node:span',
        created_at='2026-10-10T04:00:01+08:00',
        payload={'delta_usage': {'output_tokens': 7}},
    )
    assert store.max_observed_request_span_tokens('task:span', 'node:span') == 262144
    store.append_task_model_call(
        task_id='task:span',
        node_id='node:other',
        created_at='2026-10-10T04:00:02+08:00',
        payload={'observed_input_truth': {'effective_input_tokens': 400000}, 'delta_usage': {'output_tokens': 9}},
    )
    assert store.max_observed_request_span_tokens('task:span', 'node:span') == 262144
    assert store.max_observed_request_span_tokens('task:span', 'node:other') == 400009
