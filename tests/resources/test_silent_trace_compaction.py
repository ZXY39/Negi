"""静默痕迹跨压缩的新契约：换成按来源聚合的台账行，来源终结即失效。

旧契约（逐字回插痕迹组 + 只有 `subject=task:` 才退休）实盘账：240 组里只有 1 组的
subject 是任务 id，其余 239 组永不退休；2026-11-06 08:02 那条轮里，逐字保留的旧定时
计划压过了被折进摘要的本轮指令，热搜日报被换成了一条打招呼。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime.tool_history import analyze_tool_call_history
from main.runtime.mid_insert import (
    SILENT_TRACE_LEDGER_PREFIX,
    parse_silent_trace_ledger,
    render_silent_trace_ledger,
)

SILENT_CALL_ID = "call-silent-1"


def _runner(
    *,
    status_by_task: dict[str, str] | None = None,
    raises: bool = False,
    cron_job_ids: list[str] | None = None,
    cron_unreadable: bool = False,
) -> CeoFrontDoorRunner:
    class _Service:
        def get_task(self, task_id: str):
            if raises:
                raise RuntimeError("store unavailable")
            status = status_by_task or {}
            value = status.get(str(task_id or "").strip())
            if value is None:
                return None
            return SimpleNamespace(status=value)

    loop_kwargs: dict = {"main_task_service": _Service()}
    if cron_job_ids is not None:
        loop_kwargs["cron_service"] = SimpleNamespace(
            list_jobs=lambda **_: [SimpleNamespace(id=str(job_id)) for job_id in cron_job_ids]
        )
    elif cron_unreadable:
        loop_kwargs["cron_service"] = SimpleNamespace(
            list_jobs=lambda **_: (_ for _ in ()).throw(RuntimeError("boom"))
        )
    return CeoFrontDoorRunner(loop=SimpleNamespace(**loop_kwargs))


def _group(*subjects: str, source_key: str = "") -> list[dict]:
    tool_calls = []
    tool_results = []
    for index, subject in enumerate(subjects or (["",]), start=1):
        call_id = f"call-silent-{index}"
        tool_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "silent",
                    "arguments": json.dumps(
                        {"reason": "already covered", "subject": subject}, ensure_ascii=False
                    ),
                },
            }
        )
        receipt = {"silenced": True, "reason": "already covered", "subject": subject}
        if source_key:
            receipt["source_key"] = source_key
        tool_results.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "silent",
                "content": json.dumps(receipt, ensure_ascii=False),
            }
        )
    return [
        {"role": "assistant", "content": "", "tool_calls": tool_calls},
        *tool_results,
    ]


# ---------- 来源判据 ----------


def test_receipt_source_key_wins_over_subject() -> None:
    runner = _runner(status_by_task={"task:done": "in_progress"})
    group = _group("task:done", source_key="cron:832dea14")
    assert runner._silent_group_source_key(group) == "cron:832dea14"


def test_subject_task_id_is_the_fallback_source() -> None:
    runner = _runner()
    assert runner._silent_group_source_key(_group("task:abc123")) == "task:abc123"


def test_free_text_subject_without_receipt_source_is_unresolvable() -> None:
    """实盘那 239/240 组的形状：subject 是一句自由文本，回执里没有来源键。"""
    runner = _runner()
    group = _group("09:54 按计划静默，最后一次额度留给 10:24 收尾")
    assert runner._silent_group_source_key(group) == ""


def test_terminal_task_retires_but_running_and_unreadable_do_not() -> None:
    for status in ("success", "failed"):
        assert _runner(status_by_task={"task:done": status})._frontdoor_silent_source_terminal("task:done") is True
    assert _runner(status_by_task={"task:run": "in_progress"})._frontdoor_silent_source_terminal("task:run") is False
    assert _runner(raises=True)._frontdoor_silent_source_terminal("task:gone") is False


def test_removed_cron_job_retires_but_a_live_one_does_not() -> None:
    assert _runner(cron_job_ids=["832dea14"])._frontdoor_silent_source_terminal("cron:832dea14") is False
    assert _runner(cron_job_ids=["832dea14"])._frontdoor_silent_source_terminal("cron:1b285eb9") is True
    # 读不到 cron 服务时保守按"任务还在"，不许因为观测缺失就删判据
    assert _runner(cron_unreadable=True)._frontdoor_silent_source_terminal("cron:1b285eb9") is False


def test_unresolvable_source_is_terminal_for_ledger_purposes() -> None:
    """没有可查来源 ⇒ 这条痕迹的寿命无从判定，不再跨压缩累积。"""
    assert _runner()._frontdoor_silent_source_terminal("") is True


# ---------- 台账行 ----------


def test_ledger_replaces_verbatim_rows_with_one_aggregated_row() -> None:
    runner = _runner(cron_job_ids=["832dea14"])
    older = [{"role": "user", "content": "bootstrap"}]
    tail = [{"role": "assistant", "content": "recent"}]
    groups = [
        *_group("凌晨节点无需介入", source_key="cron:832dea14"),
        *_group("又一轮静默", source_key="cron:832dea14"),
    ]
    kept_older, kept_tail, rows, counts = runner._frontdoor_silent_trace_ledger(
        older_history_messages=older,
        recent_tail=tail,
        preserved_silent_groups=groups,
    )
    assert rows and len(rows) == 1
    content = rows[0]["content"]
    assert content.startswith(SILENT_TRACE_LEDGER_PREFIX)
    assert "cron:832dea14 | 已静默 2 次" in content
    assert kept_older == older and kept_tail == tail
    assert counts == {
        "silent_trace_group_count": 2,
        "silent_trace_dropped_count": 0,
        "silent_ledger_entry_count": 1,
    }


def test_ledger_drops_free_text_and_terminal_sources_and_counts_them() -> None:
    runner = _runner(cron_job_ids=["live"], status_by_task={"task:done": "success"})
    groups = [
        *_group("旧计划句", source_key=""),                    # 无可查来源
        *_group("已跑满的任务", source_key="cron:dead"),        # 来源已终结
        *_group("已收口的交付", source_key="task:done"),        # 任务终态
        *_group("仍在跑的定时", source_key="cron:live"),        # 保留
    ]
    _older, _tail, rows, counts = runner._frontdoor_silent_trace_ledger(
        older_history_messages=[],
        recent_tail=[],
        preserved_silent_groups=groups,
    )
    assert counts["silent_trace_group_count"] == 4
    assert counts["silent_trace_dropped_count"] == 3
    assert counts["silent_ledger_entry_count"] == 1
    assert "cron:live" in rows[0]["content"]
    for gone in ("cron:dead", "task:done", "旧计划句"):
        assert gone not in rows[0]["content"]


def test_prior_ledger_row_is_lifted_out_and_merged_not_stacked() -> None:
    runner = _runner(cron_job_ids=["832dea14", "old:gone"])
    prior = render_silent_trace_ledger(
        [
            {"source_key": "cron:832dea14", "count": 3, "subject": "上一批的静默"},
            {"source_key": "cron:gone", "count": 5, "subject": "早该失效的条目"},
        ]
    )
    assert prior is not None
    older = [{"role": "user", "content": "bootstrap"}, prior]
    _older, _tail, rows, counts = runner._frontdoor_silent_trace_ledger(
        older_history_messages=older,
        recent_tail=[],
        preserved_silent_groups=_group("本轮又静默一次", source_key="cron:832dea14"),
    )
    assert prior not in _older, "旧台账行不得留在待压缩区间，否则会被摘要复述一遍"
    entries = parse_silent_trace_ledger(rows[0])
    assert entries == [{"source_key": "cron:832dea14", "count": 4, "subject": "本轮又静默一次"}]
    assert counts["silent_trace_dropped_count"] == 5


def test_ledger_row_in_the_retained_tail_is_also_replaced() -> None:
    """尾部那份旧台账行同样摘掉重发：否则一次压缩产出行、下一份又叠一份。"""
    runner = _runner(cron_job_ids=["832dea14"])
    prior = render_silent_trace_ledger([{"source_key": "cron:832dea14", "count": 1, "subject": "上次"}])
    assert prior is not None
    _older, tail, rows, _counts = runner._frontdoor_silent_trace_ledger(
        older_history_messages=[],
        recent_tail=[{"role": "assistant", "content": "recent"}, prior],
        preserved_silent_groups=[],
    )
    assert tail == [{"role": "assistant", "content": "recent"}]
    assert parse_silent_trace_ledger(rows[0]) == [{"source_key": "cron:832dea14", "count": 1, "subject": "上次"}]


def test_no_ledger_row_when_nothing_survives() -> None:
    runner = _runner(cron_job_ids=[])
    _older, _tail, rows, counts = runner._frontdoor_silent_trace_ledger(
        older_history_messages=[],
        recent_tail=[],
        preserved_silent_groups=_group("旧痕迹", source_key="cron:gone"),
    )
    assert rows == []
    assert counts["silent_ledger_entry_count"] == 0


# ---------- 车道进口：真跑一次压缩，验证发出去的形状 ----------


async def test_token_compression_emits_ledger_instead_of_verbatim_traces(
    monkeypatch,
) -> None:
    from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
    from g3ku.runtime.frontdoor.state_models import CeoRuntimeContext

    runner = CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda _tid: None),
            cron_service=SimpleNamespace(list_jobs=lambda **_: [SimpleNamespace(id="832dea14")]),
        )
    )
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_model": "openai:gpt-5.2",
            "context_window_tokens": 32000,
        },
        raising=False,
    )
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **_: 18000, raising=False)
    monkeypatch.setattr(runner, "_emit_frontdoor_runtime_snapshot", lambda **_: asyncio.sleep(0))
    monkeypatch.setattr(
        runner,
        "_persist_frontdoor_internal_request_artifact",
        lambda **kwargs: {"request_id": "internal-request-ledger"},
        raising=False,
    )

    async def _call_model_with_tools(**kwargs):
        _ = kwargs
        return {"content": "[压缩后的较早历史摘要]"}

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        announce_context_compression_boundary=lambda **_: None,
        _frontdoor_actual_request_path="",
        _frontdoor_request_body_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "visible baseline"},
        ],
        _emit_state_snapshot=lambda: None,
    )
    runtime = SimpleNamespace(
        context=CeoRuntimeContext(loop=None, session=session, session_key="web:shared", on_progress=None)
    )

    request_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "older-1"},
        *_group("09:54 按计划静默，最后一次额度留给 10:24 收尾", source_key="cron:832dea14"),
        {"role": "assistant", "content": "older-2"},
        {"role": "user", "content": "older-3"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_exec_1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}
        ]},
        {"role": "tool", "content": "result-1", "tool_call_id": "call_exec_1"},
    ]

    result = await runner._run_frontdoor_llm_token_compression(
        state={"session_key": "web:shared", "model_refs": ["ceo_primary"]},
        runtime=runtime,
        request_messages=request_messages,
        model_refs=["ceo_primary"],
        tool_schemas=[],
    )

    assert result.history_shrink_reason == "token_compression"
    rewritten = [dict(item) for item in list(result.request_messages or [])]
    silent_rows = [
        item
        for item in rewritten
        if str(item.get("role") or "") == "assistant"
        and any(
            str(((tc.get("function") or {}).get("name")) or "").strip() == "silent"
            for tc in list(item.get("tool_calls") or [])
        )
    ]
    assert silent_rows == [], "逐字静默痕迹不得再回插：它会以第一人称压过摘要里的本轮指令"
    ledger_rows = [
        item
        for item in rewritten
        if str(item.get("content") or "").startswith(SILENT_TRACE_LEDGER_PREFIX)
    ]
    assert len(ledger_rows) == 1
    assert "cron:832dea14" in ledger_rows[0]["content"]
    # 台账排在保留尾部之后，不插进摘要与近况之间
    assert rewritten.index(ledger_rows[0]) > max(
        index for index, item in enumerate(rewritten) if str(item.get("role") or "") == "tool"
    )
    assert analyze_tool_call_history(rewritten).orphan_tool_result_ids == []
