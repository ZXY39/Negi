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
# 上游中途关掉 SSE 的那一跳：它既不是模型的交付违约，也不该在 Token 统计里
# 长得像"这次没花钱"。实盘 2026-10-04 单日 15 条这种回包（全部落在
# deepseek-v4-flash-4），逐次调用表里输入/缓存/命中全记 0，而请求体实际发了
# ~5.3 万 token；其中 node:d6cfe60d2a20 的 3 连断流被纯文本道记满 3 次记分，
# 节点被错误暂停——那 3 条的日志逐条写着 finish_reason_seen=0。
# ---------------------------------------------------------------------------


def _no_delay(loop) -> None:
    loop._empty_response_retry_delay_seconds = lambda attempt_count: 0.0


def _aborted_reply(*, content: str = "", reasoning: str = "先把三个来源逐条核一遍") -> LLMResponse:
    """provider 自证的断流形状：没有任何分片带 finish_reason。"""
    return LLMResponse(
        content=content,
        tool_calls=[],
        finish_reason="stop",
        usage={},
        reasoning_content=reasoning,
        stream_incomplete=True,
    )


def _terminated_reasoning_only_reply(*, output_tokens: int = 203) -> LLMResponse:
    """流是正常结束的，只是思考把输出配额吃光（实盘 sensenova 那 8 条）。"""
    return LLMResponse(
        content="",
        tool_calls=[],
        finish_reason="length",
        usage={"input_tokens": 821, "output_tokens": output_tokens, "cache_hit_tokens": 261120},
        reasoning_content="整段思考占满了输出预算",
    )


# 长度对齐实盘同日 6 条真纯文本回复（95–1361 字符），断流漏出的半截最长只有 39 字符。
_SUBSTANTIAL_PROSE = (
    "核验结果充分，可以裁定。关键实测证据：文件真实存在，逐字摘录了三个来源的段落，"
    "并且每条 URL 都返回 HTTP 200，标题与正文都对得上；因此本阶段判定为通过。"
    "补充一点：第二个来源的口径与官方 PDF 一致，差异只在四舍五入。"
)


@pytest.mark.asyncio
async def test_react_loop_replays_unterminated_stream_reply_without_charging_the_node() -> None:
    result, requests, logs = await _run_final_result_loop(
        responses=[
            _aborted_reply(),
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:after-abort", _good_final_arguments())],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            ),
        ],
        node_kind="execution",
        task_id="task-abort-replay",
        node_id="node-abort-replay",
        max_iterations=2,
        configure=_no_delay,
    )

    assert len(requests) == 2
    assert result.status == "success"
    # 断流那一跳不该留下任何"节点没交付"的记录。
    assert logs.error_logs == []


@pytest.mark.asyncio
async def test_react_loop_persistent_stream_abort_ends_as_provider_failure() -> None:
    result, requests, logs = await _run_final_result_loop(
        responses=[_aborted_reply(), _aborted_reply(), _aborted_reply(), _aborted_reply()],
        node_kind="execution",
        task_id="task-abort-exhausted",
        node_id="node-abort-exhausted",
        max_iterations=2,
        configure=_no_delay,
    )

    # 走提供侧重放预算（3 次），而不是把节点的 5 次交付预算烧穿。
    assert len(requests) == 3
    assert result.status == "failed"
    assert result.delivery_status == "blocked"
    assert result.summary == "model stream aborted replay limit reached"
    assert "closed before finish_reason" in result.blocking_reason
    assert logs.error_logs == []


@pytest.mark.asyncio
async def test_react_loop_replays_aborted_stream_that_leaked_a_fragment() -> None:
    # 实盘 05:12:11 / 05:23:20 两条：'预算 14/'（6 字符）与那句 39 字符的
    # "……关键突破口：" ——都是流被掐断前漏出的半截，后者连记满 3 次把节点打进了暂停。
    result, requests, logs = await _run_final_result_loop(
        responses=[
            _aborted_reply(content="hk01 正文不含人民币 2.73%/第六，不可用作条目11来源。关键突破口："),
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:after-fragment", _good_final_arguments())],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            ),
        ],
        node_kind="execution",
        task_id="task-abort-fragment",
        node_id="node-abort-fragment",
        max_iterations=2,
        configure=_no_delay,
    )

    assert len(requests) == 2
    assert result.status == "success"
    assert logs.error_logs == []


@pytest.mark.asyncio
async def test_react_loop_does_not_replay_aborted_stream_with_substantial_text() -> None:
    substantial = _SUBSTANTIAL_PROSE
    assert len(substantial.strip()) > 64

    result, _requests, logs = await _run_final_result_loop(
        responses=[
            _aborted_reply(content=substantial),
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:after-prose", _good_final_arguments())],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            ),
        ],
        node_kind="execution",
        task_id="task-abort-with-text",
        node_id="node-abort-with-text",
        max_iterations=2,
        configure=_no_delay,
    )

    # 谓词必须挂在"这一跳没有有效交付"上：正文够长的回包仍归交付车道判，
    # 不许被提供侧重放吞掉（同日 6 条真纯文本回复长度 95–1361 字符）。
    assert result.summary != "model stream aborted replay limit reached"
    assert "closed before finish_reason" not in str(getattr(result, "blocking_reason", "") or "")
    assert all("closed before finish_reason" not in row["error_text"] for row in logs.error_logs)


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
        configure=_no_delay,
    )

    # 今天实盘那 8 条 sensenova（out==think、finish_reason=length、流完整终止）
    # 仍归形态故障车道：预算不变，也不该伪装成上游断流。
    assert len(requests) == 3
    assert result.status == "success"
    assert len(logs.error_logs) == 2
    assert "reasoning-only" in logs.error_logs[0]["error_text"]


@pytest.mark.asyncio
async def test_react_loop_forwards_stream_incomplete_flag_to_the_model_call_ledger() -> None:
    # 用一条正文够长、仍归交付车道判的断流：那一跳会落账本，标志必须跟着落。
    _result, _requests, logs = await _run_final_result_loop(
        responses=[
            _aborted_reply(content=_SUBSTANTIAL_PROSE),
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:ledger-flag", _good_final_arguments())],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            ),
        ],
        node_kind="execution",
        task_id="task-ledger-flag",
        node_id="node-ledger-flag",
        max_iterations=2,
        configure=_no_delay,
    )

    assert logs.output_calls
    assert bool(logs.output_calls[0].get("stream_incomplete")) is True

    _clean, _clean_requests, clean_logs = await _run_final_result_loop(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[_final_call("call:ledger-clean", _good_final_arguments())],
                finish_reason="tool_calls",
                usage={"input_tokens": 8, "output_tokens": 4},
            )
        ],
        node_kind="execution",
        task_id="task-ledger-clean",
        node_id="node-ledger-clean",
        max_iterations=1,
    )
    assert clean_logs.output_calls
    assert bool(clean_logs.output_calls[0].get("stream_incomplete")) is False


def test_model_call_payload_and_record_carry_stream_incomplete() -> None:
    from main.monitoring.log_service import TaskLogService
    from main.monitoring.models import TaskModelCallRecord, TokenUsageSummary

    usage = TokenUsageSummary.model_validate(
        {"input_tokens": 0, "output_tokens": 0, "cache_hit_tokens": 0, "call_count": 1, "calls_without_usage": 1}
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
