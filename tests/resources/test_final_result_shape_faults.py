"""`submit_final_result` 的提交体归属：哪些由运行时等价补齐，哪些仍算模型的交付违约。

实盘基线（`.g3ku/main-runtime/runtime.sqlite3` 的 task_error_logs，2026-10-02）：
53 条 `Invalid final result submission detected` 里 19 条是缺必填字段、5 条是
`start_line should be integer`、24 条是零工具调用，其中 3 条 `finish_reason=length`。
缺字段与类型错里有一部分是运行时能等价补齐的（大小写、'12' 这种纯数字串、执行节点
由 status 唯一决定的 delivery_status）；补齐之外的那一半必须继续拒收，否则就是替
模型编裁定。零工具调用与被截断的提交体根本不是交付违约，改记形态故障、不占模型的
无效提交预算。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.providers.base import LLMResponse, ToolCallRequest
from main.runtime.internal_tools import SubmitFinalResultTool
from main.runtime.react_loop import ReActToolLoop


@pytest.fixture(autouse=True)
def _default_node_send_preflight_context_window(monkeypatch: pytest.MonkeyPatch) -> None:
    import main.runtime.react_loop as react_loop_module
    from main.runtime.chat_backend import SendModelContextWindowInfo

    def _resolve(**kwargs) -> SendModelContextWindowInfo:
        refs = list(kwargs.get("model_refs") or [])
        model_key = str(refs[0] or "").strip() if refs else ""
        return SendModelContextWindowInfo(
            model_key=model_key,
            provider_id="test",
            provider_model=f"test:{model_key}" if model_key else "test",
            resolved_model=model_key,
            context_window_tokens=32000,
            resolution_error="",
        )

    monkeypatch.setattr(
        react_loop_module,
        "get_runtime_config",
        lambda **_: (SimpleNamespace(), 0, False),
        raising=False,
    )
    monkeypatch.setattr(
        react_loop_module.runtime_chat_backend,
        "resolve_send_model_context_window_info",
        _resolve,
        raising=False,
    )


class _FakeTaskStore:
    def __init__(self) -> None:
        self._task = SimpleNamespace(cancel_requested=False, pause_requested=False)
        self._node = None

    def get_task(self, task_id: str):
        _ = task_id
        return self._task

    def get_node_pause_flags(self, node_id: str):
        # 与真 Store 同形：只回暂停判定要的三键，不建整行模型。
        node = self.get_node(node_id)
        if node is None:
            return None
        return {
            'pause_requested': bool(getattr(node, 'pause_requested', False)),
            'is_paused': bool(getattr(node, 'is_paused', False)),
            'pause_reason': str(getattr(node, 'pause_reason', '') or ''),
        }

    def get_node(self, node_id: str):
        _ = node_id
        return self._node


class _FakeLogService:
    def __init__(self) -> None:
        self._store = _FakeTaskStore()
        self._content_store = None
        self._frames: dict[tuple[str, str], dict[str, object]] = {}
        self.error_logs: list[dict[str, str]] = []
        self.output_calls: list[dict[str, object]] = []

    def upsert_frame(self, task_id: str, payload: dict[str, object], publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        node_id = str((payload or {}).get("node_id") or "").strip()
        self._frames[(str(task_id), node_id)] = dict(payload or {})

    def append_node_output(self, *args, **kwargs) -> None:
        _ = args
        self.output_calls.append(dict(kwargs))

    def update_frame(self, task_id: str, node_id: str, mutate, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        key = (str(task_id), str(node_id))
        current = dict(self._frames.get(key) or {})
        self._frames[key] = dict(mutate(current) or {})

    def remove_frame(self, task_id: str, node_id: str, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        self._frames.pop((str(task_id), str(node_id)), None)

    def read_runtime_frame(self, task_id: str, node_id: str):
        return dict(self._frames.get((str(task_id), str(node_id))) or {})

    def set_pause_state(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def update_node_input(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def append_task_error_log(self, task_id, node_id, *, error_text, node_title="", **kwargs) -> None:
        _ = node_title, kwargs
        self.error_logs.append({"task_id": str(task_id), "node_id": str(node_id), "error_text": str(error_text)})

    def list_task_node_error_logs(self, task_id, node_id) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(error_text=item["error_text"])
            for item in self.error_logs
            if item["task_id"] == str(task_id) and item["node_id"] == str(node_id)
        ]


def _submit_final_result_tool(*, node_kind: str = "execution") -> SubmitFinalResultTool:
    async def _submit(payload: dict[str, object]) -> dict[str, object]:
        return dict(payload)

    return SubmitFinalResultTool(_submit, node_kind=node_kind)


def _final_call(call_id: str, arguments: dict[str, object]) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name="submit_final_result", arguments=dict(arguments))


def _good_final_arguments() -> dict[str, object]:
    return {
        "status": "success",
        "delivery_status": "final",
        "summary": "done",
        "answer": "done",
        "evidence": [{"ref": "artifact:artifact:demo-ref", "note": "demo"}],
        "remaining_work": [],
        "blocking_reason": "",
    }


async def _run_final_result_loop(*, responses, node_kind, task_id, node_id, max_iterations, configure=None):
    requests: list[dict[str, object]] = []
    queue = list(responses)

    class _Backend:
        async def chat(self, **kwargs):
            requests.append(dict(kwargs))
            return queue.pop(0)

    logs = _FakeLogService()
    loop = ReActToolLoop(chat_backend=_Backend(), log_service=logs, max_iterations=max_iterations)
    if configure is not None:
        configure(loop)
    result = await loop.run(
        task=SimpleNamespace(task_id=task_id),
        node=SimpleNamespace(node_id=node_id, depth=0, node_kind=node_kind, goal="demo"),
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": json.dumps({"task_id": task_id, "goal": "demo"}, ensure_ascii=False)},
        ],
        tools={"submit_final_result": _submit_final_result_tool(node_kind=node_kind)},
        model_refs=["fake"],
        runtime_context={"task_id": task_id, "node_id": node_id},
        max_iterations=max_iterations,
    )
    return result, requests, logs


@pytest.mark.asyncio
async def test_react_loop_coerces_cased_enums_and_digit_string_lines_for_execution_nodes() -> None:
    payload = {
        "status": "SUCCESS ",
        "summary": "done",
        "answer": "done",
        "evidence": [{"ref": "artifact:artifact:demo-ref", "kind": "ARTIFACT", "start_line": "12", "end_line": " 15 "}],
        "remaining_work": [],
        "blocking_reason": "",
    }
    result, requests, logs = await _run_final_result_loop(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:coerce-final", payload)],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            )
        ],
        node_kind="execution",
        task_id="task-coerce-final",
        node_id="node-coerce-final",
        max_iterations=1,
    )

    # 枚举只在大小写/空白层面收敛，行号按落地模型的 int 收敛；执行节点的
    # delivery_status 由 status 唯一决定，一次就落地、不占无效提交预算。
    assert result.status == "success"
    assert result.delivery_status == "final"
    assert len(requests) == 1
    assert result.evidence[0].kind == "artifact"
    assert result.evidence[0].start_line == 12
    assert result.evidence[0].end_line == 15
    assert logs.error_logs == []


@pytest.mark.asyncio
async def test_react_loop_keeps_line_range_text_as_a_rejection() -> None:
    payload = _good_final_arguments()
    payload["evidence"] = [{"ref": "artifact:artifact:demo-ref", "start_line": "12-15"}]
    result, _requests, logs = await _run_final_result_loop(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:line-range-final", payload)],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            )
        ],
        node_kind="execution",
        task_id="task-line-range-final",
        node_id="node-line-range-final",
        max_iterations=1,
    )

    # '12-15' 塞进一个字段：猜哪头是 start 就是替模型编内容，继续拒收。
    assert result.status == "failed"
    assert "evidence[0].start_line should be integer" in result.blocking_reason
    assert len(logs.error_logs) == 1
    assert "第1次" in logs.error_logs[0]["error_text"]


@pytest.mark.asyncio
async def test_react_loop_derives_final_delivery_status_for_success_acceptance_verdict() -> None:
    # 实盘 2026-10-03 四条（node:7d51bc29f402 12:36:08 等）全是这个形状：验收节点写了
    # status='success' 却漏 delivery_status。两份正文里 success 只有一种搭配（通过⇒final、
    # 阻塞核验成立也是⇒final），补齐不改变裁定。
    payload = _good_final_arguments()
    payload.pop("delivery_status")
    result, requests, logs = await _run_final_result_loop(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:acceptance-success", payload)],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            )
        ],
        node_kind="acceptance",
        task_id="task-acceptance-success",
        node_id="node-acceptance-success",
        max_iterations=1,
    )

    assert result.status == "success"
    assert result.delivery_status == "final"
    assert len(requests) == 1
    assert logs.error_logs == []


@pytest.mark.asyncio
async def test_react_loop_does_not_invent_delivery_status_for_failed_acceptance_verdict() -> None:
    payload = {
        "status": "failed",
        "summary": "rejected",
        "answer": "not ok",
        "evidence": [{"ref": "artifact:artifact:demo-ref"}],
        "remaining_work": ["修 X"],
        "blocking_reason": "",
    }
    result, _requests, logs = await _run_final_result_loop(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:acceptance-no-delivery", payload)],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            )
        ],
        node_kind="acceptance",
        task_id="task-acceptance-no-delivery",
        node_id="node-acceptance-no-delivery",
        max_iterations=1,
    )

    # 验收节点 failed 时 final(打回) 与 blocked(终止循环) 语义相反：这一击仍算模型的违约。
    assert result.status == "failed"
    assert "missing required delivery_status" in result.blocking_reason
    assert "1 consecutive times" in result.blocking_reason
    assert len(logs.error_logs) == 1


@pytest.mark.asyncio
async def test_react_loop_charges_reasoning_only_reply_to_shape_fault_not_model_budget() -> None:
    result, requests, logs = await _run_final_result_loop(
        responses=[
            LLMResponse(
                content="",
                reasoning_content="我要再核一遍来源",
                tool_calls=[],
                finish_reason="stop",
                usage={"input_tokens": 8, "output_tokens": 22},
            ),
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:after-reasoning-only", _good_final_arguments())],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            ),
        ],
        node_kind="execution",
        task_id="task-reasoning-only-final",
        node_id="node-reasoning-only-final",
        max_iterations=3,
    )

    assert result.status == "success"
    assert len(requests) == 2
    assert len(logs.error_logs) == 1
    assert "reasoning-only" in logs.error_logs[0]["error_text"]
    assert "must be submitted via" not in logs.error_logs[0]["error_text"]


@pytest.mark.asyncio
async def test_react_loop_charges_truncated_final_submission_to_shape_fault() -> None:
    truncated_payload = {
        "status": "success",
        "delivery_status": "final",
        "answer": "很长的正文写到一半被输出上限切断",
        "evidence": [{"ref": "artifact:artifact:demo-ref"}],
        "remaining_work": [],
        "blocking_reason": "",
    }
    result, requests, logs = await _run_final_result_loop(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:truncated-final", truncated_payload)],
                finish_reason="length",
                usage={"input_tokens": 8, "output_tokens": 5460},
            ),
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:after-truncated", _good_final_arguments())],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            ),
        ],
        node_kind="execution",
        task_id="task-truncated-final",
        node_id="node-truncated-final",
        max_iterations=3,
    )

    assert result.status == "success"
    assert len(logs.error_logs) == 1
    fault_text = logs.error_logs[0]["error_text"]
    assert "provider output limit truncated" in fault_text
    # 拿不到 sent_max_tokens 时也要看得出是截断（实盘 53 条里该字段出现 0 次）
    assert "疑似触及输出token上限被截断" in fault_text
    second_turn_messages = json.dumps(requests[1].get("messages") or [], ensure_ascii=False)
    assert "cut off by the provider output limit" in second_turn_messages


@pytest.mark.asyncio
async def test_react_loop_still_strikes_model_violation_after_a_shape_fault() -> None:
    # 形态计数不许"热"着免罚后续真实违约：判据只看当次回包是否被输出上限切断。
    truncated = _good_final_arguments()
    truncated.pop("summary")
    bad_verdict = _good_final_arguments()
    bad_verdict["status"] = "partial"
    result, requests, logs = await _run_final_result_loop(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:shape-then-violation", truncated)],
                finish_reason="length",
                usage={"input_tokens": 8, "output_tokens": 5460},
            ),
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:out-of-enum", bad_verdict)],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            ),
        ],
        node_kind="execution",
        task_id="task-shape-then-violation",
        node_id="node-shape-then-violation",
        max_iterations=2,
    )

    assert len(requests) == 2
    assert len(logs.error_logs) == 2
    assert "provider output limit truncated" in logs.error_logs[0]["error_text"]
    assert "status must be one of" in logs.error_logs[1]["error_text"]
    frame = logs.read_runtime_frame("task-shape-then-violation", "node-shape-then-violation")
    assert int(frame.get("invalid_final_submission_count") or 0) == 1
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_react_loop_accepts_explicit_null_line_numbers_on_url_evidence() -> None:
    # 实盘 node:0d5b47f98bb7 2026-10-02 20:10:55：url 证据写成 `"start_line": null`，
    # 三条各占 2 个字段，被 `should be integer` 连拒 6 项。落地模型把 null 当缺字段，
    # 所以删键等价；区间串 '12-15' 仍必须拒收（见上一条测试）。
    payload = _good_final_arguments()
    payload["evidence"] = [
        {"ref": "artifact:artifact:demo-ref", "start_line": 10, "end_line": 20},
        {"kind": "url", "path": "", "ref": "https://example.com/a", "start_line": None, "end_line": None},
        {"kind": "url", "path": "", "ref": "https://example.com/b", "start_line": None, "end_line": None},
    ]
    result, requests, logs = await _run_final_result_loop(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:null-lines", payload)],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            )
        ],
        node_kind="acceptance",
        task_id="task-null-lines",
        node_id="node-null-lines",
        max_iterations=1,
    )

    assert result.status == "success"
    assert len(requests) == 1
    assert logs.error_logs == []
    assert [item.start_line for item in result.evidence] == [10, None, None]


def test_final_result_repair_tags_cover_only_equivalence_repairs() -> None:
    raw = {
        "status": "SUCCESS ",
        "delivery_status": "Final",
        "summary": "s",
        "answer": "a",
        "evidence": [{"kind": "file", "path": "a.py", "start_line": "12", "end_line": None}],
        "remaining_work": [],
        "blocking_reason": "",
    }
    normalized = ReActToolLoop._normalize_final_result_payload(
        raw_payload=raw, message_history=[], response_content="", node_kind="execution"
    )
    tags = ReActToolLoop._final_result_repair_tags(raw_payload=raw, normalized_payload=normalized)
    assert sorted(tags) == [
        "enum_case:delivery_status",
        "enum_case:status",
        "line_null_dropped",
        "line_to_int",
    ]

    # 策略性改写（success 清空 remaining_work/blocking_reason）不算补齐
    policy_raw = {
        "status": "success",
        "delivery_status": "final",
        "summary": "s",
        "answer": "a",
        "evidence": [{"ref": "artifact:artifact:demo-ref"}],
        "remaining_work": ["还有一件事"],
        "blocking_reason": "理由",
    }
    policy_norm = ReActToolLoop._normalize_final_result_payload(
        raw_payload=policy_raw, message_history=[], response_content="", node_kind="execution"
    )
    assert policy_norm["remaining_work"] == [] and policy_norm["blocking_reason"] == ""
    assert ReActToolLoop._final_result_repair_tags(raw_payload=policy_raw, normalized_payload=policy_norm) == []


@pytest.mark.asyncio
async def test_react_loop_records_each_repair_as_audit_event(monkeypatch: pytest.MonkeyPatch) -> None:
    import g3ku.audit_events as audit_module

    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        audit_module,
        "emit_audit_event",
        lambda *args, **kwargs: events.append({"args": args, "detail": kwargs.get("detail") or {}}),
    )

    payload = _good_final_arguments()
    payload.pop("delivery_status")
    result, _requests, _logs = await _run_final_result_loop(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:repair-audit", payload)],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            )
        ],
        node_kind="acceptance",
        task_id="task-repair-audit",
        node_id="node-repair-audit",
        max_iterations=1,
    )

    assert result.status == "success"
    assert len(events) == 1
    args = events[0]["args"]
    assert args[0] == "task" and args[1] == "info" and args[2] == "node_final_result_repaired"
    detail = events[0]["detail"]
    assert detail["node_id"] == "node-repair-audit"
    assert detail["node_kind"] == "acceptance"
    assert detail["repairs"] == {"delivery_from_status": 1}


@pytest.mark.asyncio
async def test_react_loop_emits_nothing_when_the_submission_is_already_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import g3ku.audit_events as audit_module

    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        audit_module,
        "emit_audit_event",
        lambda *args, **kwargs: events.append(dict(kwargs)),
    )

    result, _requests, logs = await _run_final_result_loop(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:clean-final", _good_final_arguments())],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            )
        ],
        node_kind="acceptance",
        task_id="task-clean-final",
        node_id="node-clean-final",
        max_iterations=1,
    )

    assert result.status == "success"
    assert logs.error_logs == []
    assert events == []


def _loop_for_dedupe() -> ReActToolLoop:
    return ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)


def test_control_tool_rejection_receipt_is_not_collapsed_on_repeat() -> None:
    # 实盘形状：同一份拒收文本第二次进来时，旧行为折成 250 字 reused 信封，
    # 把契约尾段（answer/evidence/...）整段丢掉。
    text = (
        "Error: summary must be at least 1 chars\n"
        "该工具没有可加载的扩展说明，参数契约如下（必填项及其类型与取值结构）："
        "status=string(success|failed)、delivery_status=string(final|blocked)、summary=string(非空)、"
        "answer=string、evidence=array<object{必填:kind=string(file|artifact|url)}>、"
        "remaining_work=array<string>、blocking_reason=string"
    )
    first = {"role": "tool", "tool_call_id": "call-a", "name": "submit_final_result", "content": text}
    again = {"role": "tool", "tool_call_id": "call-b", "name": "submit_final_result", "content": text}

    out = _loop_for_dedupe()._dedupe_tool_messages([again], existing_messages=[first])

    assert out[0]["content"] == text
    assert '"reused"' not in out[0]["content"]


def test_stage_tool_error_receipt_also_survives_repeat() -> None:
    text = "Error: tool_round_budget must be >= 1"
    first = {"role": "tool", "tool_call_id": "call-a", "name": "submit_next_stage", "content": text}
    again = {"role": "tool", "tool_call_id": "call-b", "name": "submit_next_stage", "content": text}

    out = _loop_for_dedupe()._dedupe_tool_messages([again], existing_messages=[first])

    assert out[0]["content"] == text


def test_ordinary_tool_duplicate_result_still_collapses() -> None:
    # 豁免只针对控制工具的拒收回执；普通工具逐字相同的输出仍要走复用折叠，
    # 否则这条道省上下文的本职就没了。
    text = '{"status": "success", "exit_code": 0, "head_preview": "same", "stdout": "same"}'
    first = {"role": "tool", "tool_call_id": "call-a", "name": "exec", "content": text}
    again = {"role": "tool", "tool_call_id": "call-b", "name": "exec", "content": text}

    out = _loop_for_dedupe()._dedupe_tool_messages([again], existing_messages=[first])

    assert '"reused"' in out[0]["content"]
    assert '"same_as": "call-a"' in out[0]["content"]


def _empty_prose_verdict() -> dict[str, object]:
    """实盘 node:ce794e2526f2 的形状：裁定全搬进 evidence.note，两个正文留空。"""
    payload = _good_final_arguments()
    payload['summary'] = ''
    payload['answer'] = ''
    payload['evidence'] = [{'kind': 'file', 'path': 'C_tax_compliance.md', 'note': '通过：4 项不符合已逐项修正'}]
    return payload


@pytest.mark.asyncio
async def test_second_identical_rejection_escalates_the_repair_overlay() -> None:
    bad = _empty_prose_verdict()
    result, requests, logs = await _run_final_result_loop(
        responses=[
            LLMResponse(content='', tool_calls=[_final_call('call:dup-1', bad)], finish_reason='tool_calls',
                         usage={'input_tokens': 8, 'output_tokens': 4}),
            LLMResponse(content='', tool_calls=[_final_call('call:dup-2', bad)], finish_reason='tool_calls',
                         usage={'input_tokens': 8, 'output_tokens': 4}),
            LLMResponse(content='', tool_calls=[_final_call('call:dup-3', _good_final_arguments())],
                         finish_reason='tool_calls', usage={'input_tokens': 8, 'output_tokens': 4}),
        ],
        node_kind='acceptance',
        task_id='task-dup-overlay',
        node_id='node-dup-overlay',
        max_iterations=4,
    )

    assert result.status == 'success'
    assert len(requests) == 3
    first_repair = json.dumps(requests[1].get('messages') or [], ensure_ascii=False)
    # 第一次被拒就要回答"这两个字段是干什么的"，否则模型会把"别写占位"读成"别重复内容"
    assert 'Field roles' in first_repair
    assert 'is NOT a placeholder' in first_repair
    second_repair = json.dumps(requests[2].get('messages') or [], ensure_ascii=False)
    assert 'rejected 2 times in a row' in second_repair
    assert '"<一句话裁定' in second_repair
    assert len(logs.error_logs) == 2


@pytest.mark.asyncio
async def test_identical_rejection_early_stops_on_the_third_strike() -> None:
    bad = _empty_prose_verdict()
    responses = [
        LLMResponse(content='', tool_calls=[_final_call(f'call:same-{i}', bad)], finish_reason='tool_calls',
                     usage={'input_tokens': 8, 'output_tokens': 4})
        for i in range(6)
    ]
    result, requests, logs = await _run_final_result_loop(
        responses=responses,
        node_kind='acceptance',
        task_id='task-dup-early-stop',
        node_id='node-dup-early-stop',
        max_iterations=8,
    )

    assert result.status == 'failed'
    assert result.summary == 'final result submission guard triggered'
    assert '同一份载荷连续第 3 次被拒' in result.blocking_reason
    # 早停比总预算少两拍：每拍新输入 12 万 token 起，重复同一形状不会再新增信息
    assert len(requests) == 3
    assert len(logs.error_logs) == 3


def _fold_probe_payload(note: str) -> dict[str, object]:
    payload = _good_final_arguments()
    payload['summary'] = ''
    payload['answer'] = ''
    payload['evidence'] = [{'kind': 'file', 'path': 'frag.md', 'note': note}]
    return payload


def _resp(call_id: str, arguments: dict[str, object]) -> LLMResponse:
    return LLMResponse(
        content='',
        tool_calls=[_final_call(call_id, arguments)],
        finish_reason='tool_calls',
        usage={'input_tokens': 8, 'output_tokens': 4},
    )


@pytest.mark.asyncio
async def test_repeated_identical_submission_is_folded_in_history() -> None:
    note = 'UNIQUE-NOTE-只应出现一次'
    bad = _fold_probe_payload(note)
    result, requests, _logs = await _run_final_result_loop(
        responses=[
            _resp('call:fold-1', bad),
            _resp('call:fold-2', bad),
            _resp('call:fold-3', _good_final_arguments()),
        ],
        node_kind='acceptance',
        task_id='task-fold',
        node_id='node-fold',
        max_iterations=4,
    )

    assert result.status == 'success'
    first_repair = json.dumps(requests[1].get('messages') or [], ensure_ascii=False)
    assert first_repair.count(note) == 1
    assert 'repeated_submission_folded' not in first_repair

    second_repair = json.dumps(requests[2].get('messages') or [], ensure_ascii=False)
    # 全文只留最早那份；第二份折成一行，但回执里的契约仍逐字在案
    assert second_repair.count(note) == 1
    assert 'repeated_submission_folded' in second_repair
    assert 'repeated_times' in second_repair


@pytest.mark.asyncio
async def test_different_violation_on_repeat_is_not_folded() -> None:
    other = _good_final_arguments()
    other['summary'] = ''
    other['answer'] = ''
    other['evidence'] = [{'kind': 'nope', 'note': '另一种错'}]
    _result, requests, _logs = await _run_final_result_loop(
        responses=[
            _resp('call:nd-1', _fold_probe_payload('第一条的理由')),
            _resp('call:nd-2', other),
            _resp('call:nd-3', _good_final_arguments()),
        ],
        node_kind='acceptance',
        task_id='task-no-fold',
        node_id='node-no-fold',
        max_iterations=4,
    )

    third = json.dumps(requests[2].get('messages') or [], ensure_ascii=False)
    assert 'repeated_submission_folded' not in third


# ---------------------------------------------------------------------------
# 上游中途关掉 SSE 的那一跳：它不是模型的交付违约，也不该在 Token 统计里长得像
# "这次没花钱"。实盘 2026-10-04 它有两处落刀位置——切在思考段（正文 0-39 字符）与
# 切在写答案段（102-209 字符，结尾分别停在"（并""加入 P""**Pornhub"）——是同一种
# 传输故障，所以归一判据只能是"没有任何分片携带 finish_reason"，正文长度不参与。
# ---------------------------------------------------------------------------


def _stream_abort_reply(*, content: str = "", chunks: int = 3050) -> LLMResponse:
    """provider 如实标出的断流回包（chat 与 responses 两条车道同一个形状）。"""
    return LLMResponse(
        content=content,
        tool_calls=[],
        finish_reason="error",
        error_text=f"stream closed before finish_reason after {int(chunks)} chunks",
        error_kind="StreamIncomplete",
        usage={},
        reasoning_content="先把三个来源逐条核一遍",
        stream_incomplete=True,
    )


def _terminated_reasoning_only_reply(*, output_tokens: int = 203) -> LLMResponse:
    """流正常结束，只是思考把输出配额吃光（实盘 sensenova 那 8 条，归形态故障车道）。"""
    return LLMResponse(
        content="",
        tool_calls=[],
        finish_reason="length",
        usage={"input_tokens": 821, "output_tokens": output_tokens, "cache_hit_tokens": 261120},
        reasoning_content="整段思考占满了输出预算",
    )


@pytest.mark.asyncio
async def test_react_loop_turns_chain_wide_stream_abort_into_recoverable_pause() -> None:
    result, requests, logs = await _run_final_result_loop(
        responses=[_stream_abort_reply(chunks=66)],
        node_kind="execution",
        task_id="task-chain-abort",
        node_id="node-chain-abort",
        max_iterations=2,
    )

    # 换过槽位仍未终止 = 提供侧烧穿整条链：落可恢复的暂停（不是终态判死），
    # 也不写"节点没交付"的错误账——那是上一版把这族记成 ReAct 违约的地方。
    assert len(requests) == 1
    assert result.status == "failed"
    assert result.delivery_status == "blocked"
    assert result.failure_disposition == "pause"
    assert result.summary == "model stream aborted across model chain"
    assert "closed before finish_reason" in result.blocking_reason
    assert logs.error_logs == []


@pytest.mark.asyncio
async def test_stream_abort_treatment_ignores_how_much_text_leaked() -> None:
    half_answer = (
        "核验结果充分，可以裁定。关键实测证据：文件真实存在，逐字摘录了三个来源的段落，"
        "并且每条 URL 都返回 HTTP 200，标题与正文都对得上；因此本阶段判定为通过。补充："
    )
    assert len(half_answer) > 64

    empty_case, _r1, logs_empty = await _run_final_result_loop(
        responses=[_stream_abort_reply(content="")],
        node_kind="execution",
        task_id="task-abort-empty-text",
        node_id="node-abort-empty-text",
        max_iterations=2,
    )
    leaked_case, _r2, logs_leaked = await _run_final_result_loop(
        responses=[_stream_abort_reply(content=half_answer, chunks=66)],
        node_kind="execution",
        task_id="task-abort-leaked-text",
        node_id="node-abort-leaked-text",
        max_iterations=2,
    )

    # 同一族故障不能因为"切在思考段"和"切在写答案段"分成两个归属：前一版用长度阈值
    # 就把 209 字符那条推给了纯文本道，连着三次把 node:8ff960cc5876 打成错误暂停。
    assert empty_case.summary == leaked_case.summary == "model stream aborted across model chain"
    assert empty_case.failure_disposition == leaked_case.failure_disposition == "pause"
    assert logs_empty.error_logs == []
    assert logs_leaked.error_logs == []


@pytest.mark.asyncio
async def test_react_loop_keeps_terminated_reasoning_only_reply_in_shape_lane() -> None:
    result, requests, logs = await _run_final_result_loop(
        responses=[
            _terminated_reasoning_only_reply(),
            _terminated_reasoning_only_reply(),
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:after-shape", _good_final_arguments())],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            ),
        ],
        node_kind="execution",
        task_id="task-terminated-reasoning-only",
        node_id="node-terminated-reasoning-only",
        max_iterations=4,
    )

    # 正常终止、只是思考吃满配额的回复仍归形态故障车道：它不是传输故障，
    # 5 次上限与拒收提示都不由断流那族接管。
    assert len(requests) == 3
    assert result.status == "success"
    assert len(logs.error_logs) == 2
    assert "reasoning-only" in logs.error_logs[0]["error_text"]


@pytest.mark.asyncio
async def test_react_loop_forwards_stream_incomplete_flag_to_the_model_call_ledger() -> None:
    _result, _requests, logs = await _run_final_result_loop(
        responses=[_stream_abort_reply(content="半截回答", chunks=66)],
        node_kind="execution",
        task_id="task-ledger-flag",
        node_id="node-ledger-flag",
        max_iterations=1,
    )

    assert logs.output_calls
    assert bool(logs.output_calls[0].get("stream_incomplete")) is True


def test_stream_abort_classifies_as_chain_fallback_not_same_slot_retry() -> None:
    from g3ku.providers.fallback import response_requires_fallback, response_requires_retry

    abort = _stream_abort_reply()

    # 正确处置是"换下一位"（fallback），绝不是"在同一个坏槽位上重发"（同槽重试）：
    # error_text 因此必须避开 network / 429 的关键字，否则会在断流的位上空转。
    assert response_requires_fallback(abort) is True
    assert response_requires_retry(abort) is False
    assert response_requires_retry(abort, retry_on=["network", "429"]) is False


def test_model_call_payload_and_record_carry_stream_incomplete() -> None:
    from main.monitoring.log_service import TaskLogService
    from main.monitoring.models import TaskModelCallRecord, TokenUsageSummary

    usage = TokenUsageSummary.model_validate(
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_hit_tokens": 0,
            "call_count": 1,
            "calls_without_usage": 1,
        }
    )
    payload = TaskLogService._model_call_payload(
        task_id="task-x",
        node_id="node-x",
        call_index=1,
        model_messages=[{"role": "user", "content": "hi"}],
        request_messages=[{"role": "user", "content": "hi"}],
        prompt_cache_key=None,
        tool_calls=[],
        delta_usage=usage,
        delta_usage_by_model=[],
        request_message_count=1,
        request_message_chars=2,
        stream_incomplete=True,
    )
    assert payload["stream_incomplete"] is True
    assert TaskModelCallRecord.model_validate(payload).stream_incomplete is True
    # 旧行没这个键：读侧按 False 处理，不能把历史 0 一律改判成断流。
    assert TaskModelCallRecord.model_validate({"call_index": 1}).stream_incomplete is False


class _FakeChatStreamCompletions:
    def __init__(self, chunks: list) -> None:
        self._chunks = list(chunks)

    async def create(self, **kwargs):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()


def _fake_chat_client(chunks: list):
    return SimpleNamespace(chat=SimpleNamespace(completions=_FakeChatStreamCompletions(chunks)))


@pytest.mark.asyncio
async def test_openai_chat_provider_labels_unterminated_stream_as_provider_error() -> None:
    from g3ku.providers.openai_chat_provider import OpenAIChatProvider

    provider = OpenAIChatProvider(api_key="k", api_base="https://example.invalid/v1")
    provider._client = _fake_chat_client(
        [
            {"choices": [{"delta": {"reasoning_content": "先核对来源"}}]},
            {"choices": [{"delta": {"content": "结论是"}}]},
        ]
    )

    response = await provider.chat(messages=[{"role": "user", "content": "ping"}])

    # 没收到终止分片就不再冒充 stop：上层拿到的是提供侧故障，模型链会前进到下一位。
    assert response.finish_reason == "error"
    assert response.error_kind == "StreamIncomplete"
    assert "closed before finish_reason" in str(response.error_text or "")
    assert response.stream_incomplete is True
    assert response.content == "结论是"


@pytest.mark.asyncio
async def test_openai_chat_provider_keeps_unterminated_stream_with_tool_calls_as_reply() -> None:
    from g3ku.providers.openai_chat_provider import OpenAIChatProvider

    provider = OpenAIChatProvider(api_key="k", api_base="https://example.invalid/v1")
    provider._client = _fake_chat_client(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "exec", "arguments": '{"command":"dir"}'},
                                }
                            ]
                        }
                    }
                ]
            },
        ]
    )

    response = await provider.chat(messages=[{"role": "user", "content": "ping"}])

    # 带工具调用的未终止流故意不标 error：重发会丢掉一次真实交付，宁可原样交给上层。
    assert response.tool_calls
    assert response.finish_reason == "stop"
    assert response.error_kind is None
    assert response.stream_incomplete is True


@pytest.mark.asyncio
async def test_responses_stream_records_terminal_event_in_diagnostics() -> None:
    from g3ku.providers import responses_protocol_helpers as helpers
    from g3ku.providers.responses_provider import _SSEDiagnosticsResponseProxy

    class _RawResponse:
        status_code = 200

        def __init__(self, lines: list) -> None:
            self._lines = lines

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    completed = _SSEDiagnosticsResponseProxy(
        _RawResponse(
            [
                'data: {"type":"response.completed","response":{"status":"completed",'
                '"usage":{"input_tokens":3}}}',
                "",
            ]
        ),
        first_line_timeout_seconds=5,
        idle_line_timeout_seconds=5,
    )
    await helpers._consume_sse(completed)
    assert completed._diagnostics.finish_reason_seen is True

    cut_off = _SSEDiagnosticsResponseProxy(
        _RawResponse(
            [
                'data: {"type":"response.output_text.delta","delta":"结论是"}',
                "",
            ]
        ),
        first_line_timeout_seconds=5,
        idle_line_timeout_seconds=5,
    )
    _content, calls, _finish_reason, usage, _items = await helpers._consume_sse(cut_off)
    # 没有 response.completed 就等于没说完了：终止位保持 False，usage 也不会出现。
    assert cut_off._diagnostics.finish_reason_seen is False
    assert calls == []
    assert usage == {}


@pytest.mark.asyncio
async def test_reasoning_only_error_line_names_the_binding_limit() -> None:
    """顶满声明上限的那一跳，错误行要同时印出类名、发出的上限与档位。

    这条是处置判据的读数面：`sent_max_tokens` 缺席时"吃满上限"与"被窗口挤掉"同形
    （实盘 108 条同类行的 sent_max_tokens 出现 0 次）。首跳即停的行为见
    `test_react_loop_pauses_on_first_output_capped_reasoning_only_hop`。
    """
    blown = LLMResponse(
        content="",
        finish_reason="length",
        usage={"input_tokens": 158337, "output_tokens": 65536, "thinking_tokens": 65536},
        reasoning_content="thinking" * 64,
        provider_request_body={"model": "sens-x", "max_tokens": 65536, "reasoning_effort": "xhigh"},
    )
    _result, requests, logs = await _run_final_result_loop(
        responses=[blown],
        node_kind="execution",
        task_id="task-reasoning-capped",
        node_id="node-reasoning-capped",
        max_iterations=3,
    )
    assert len(requests) == 1
    assert len(logs.error_logs) == 1
    text = logs.error_logs[0]["error_text"]
    assert "[output-capped]" in text
    assert "sent_max_tokens=65536" in text
    assert "sent_reasoning_effort=xhigh" in text
    assert "疑似触及输出token上限被截断" in text


@pytest.mark.asyncio
async def test_reasoning_only_without_truncation_is_not_labeled_as_truncated() -> None:
    """`finish_reason=stop` 的零工具调用回包没被任何上限截断：给它加截断措辞就是印假事实。"""
    stopped = LLMResponse(
        content="",
        finish_reason="stop",
        usage={"input_tokens": 400, "output_tokens": 300, "thinking_tokens": 300},
        reasoning_content="thinking",
        provider_request_body={"model": "sens-x", "max_tokens": 65536},
    )
    recovery = LLMResponse(
        content="",
        tool_calls=[_final_call("call:after-stop", _good_final_arguments())],
        finish_reason="tool_calls",
        usage={"input_tokens": 10, "output_tokens": 20},
    )
    result, _requests, logs = await _run_final_result_loop(
        responses=[stopped, recovery],
        node_kind="execution",
        task_id="task-reasoning-stop",
        node_id="node-reasoning-stop",
        max_iterations=3,
    )
    assert result.status == "success"
    assert len(logs.error_logs) == 1
    text = logs.error_logs[0]["error_text"]
    assert "[not-truncated]" in text
    assert "疑似触及输出token上限被截断" not in text


@pytest.mark.asyncio
async def test_react_loop_pauses_on_first_output_capped_reasoning_only_hop() -> None:
    """思考顶满声明的输出上限：首跳即落可恢复暂停，不再多问一次。

    实盘某节点在 2.5 小时里连撞 11 次同样形态、白烧约 69 万输出 token，因为形态计数
    被中间的正常工具轮清零；暂停理由必须写清发出去的是什么档位与"改配置才有效"。
    """
    blown = LLMResponse(
        content="",
        finish_reason="length",
        usage={"input_tokens": 158337, "output_tokens": 65536, "thinking_tokens": 65536},
        reasoning_content="thinking" * 64,
        provider_request_body={"model": "sens-x", "max_tokens": 65536, "reasoning_effort": "xhigh"},
    )
    result, requests, logs = await _run_final_result_loop(
        responses=[blown],
        node_kind="execution",
        task_id="task-output-capped-pause",
        node_id="node-output-capped-pause",
        max_iterations=5,
    )
    assert len(requests) == 1, "首跳即停，不该再烧一跳"
    assert result.status == "failed"
    assert result.delivery_status == "blocked"
    assert result.failure_disposition == "pause"
    assert "model_config_fault:" in result.blocking_reason
    assert "sent_reasoning_effort=xhigh" in result.blocking_reason
    assert "sent_max_tokens=65536" in result.blocking_reason
    assert "思考强度" in result.blocking_reason
    assert "同类跳次=1" in result.blocking_reason
    assert len(logs.error_logs) == 1
    assert "[output-capped]" in logs.error_logs[0]["error_text"]


@pytest.mark.asyncio
async def test_react_loop_does_not_pause_when_a_smaller_limit_bound_the_reply() -> None:
    """截在比声明上限更小的值上 ⇒ 不是档位吃满，本轮仍按形态计数续跑（处置不同，见窗口那条道）。"""
    clamped = LLMResponse(
        content="",
        finish_reason="length",
        usage={"input_tokens": 245792, "output_tokens": 16352, "thinking_tokens": 16352},
        reasoning_content="thinking" * 64,
        provider_request_body={"model": "sens-x", "max_tokens": 65536, "reasoning_effort": "xhigh"},
    )
    recovery = LLMResponse(
        content="",
        tool_calls=[_final_call("call:after-clamped", _good_final_arguments())],
        finish_reason="tool_calls",
        usage={"input_tokens": 10, "output_tokens": 20},
    )
    result, requests, logs = await _run_final_result_loop(
        responses=[clamped, recovery],
        node_kind="execution",
        task_id="task-window-clamped-continue",
        node_id="node-window-clamped-continue",
        max_iterations=5,
    )
    assert len(requests) == 2
    assert result.status == "success"
    assert "[window-clamped]" in logs.error_logs[0]["error_text"]
    assert "model_config_fault" not in str(result.blocking_reason or "")
