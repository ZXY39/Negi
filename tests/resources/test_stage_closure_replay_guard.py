from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from main.models import ExecutionStageRecord, ExecutionStageRound, ExecutionStageState
from main.monitoring.log_service import TaskLogService
from main.runtime.react_loop import ReActToolLoop
from main.runtime.recovery_check import RecoveryCheckDecision
from main.runtime.stage_budget import STAGE_TOOL_NAME

GOAL = "派生子节点并行取证5大板块"


def _ledger(*, active_stage_id: str = "stage:2", goals: tuple[str, ...] = ("加载技能", GOAL)) -> ExecutionStageState:
    stages = [
        ExecutionStageRecord(
            stage_id=f"stage:{index + 1}",
            stage_index=index + 1,
            stage_goal=goal,
            status="完成" if goal != GOAL else "进行中",
            created_at="2026-09-29T03:09:23+08:00",
        )
        for index, goal in enumerate(goals)
    ]
    return ExecutionStageState(active_stage_id=active_stage_id, stages=stages)


def _log_service(state: ExecutionStageState, frame: dict | None) -> TaskLogService:
    service = object.__new__(TaskLogService)
    service._store = SimpleNamespace(get_node=lambda node_id: SimpleNamespace(node_id=node_id))
    service._execution_stage_state = lambda node: state
    service.read_runtime_frame_payload = lambda task_id, node_id: dict(frame or {})
    return service


def test_stage_transition_already_applied_finds_a_stage_created_by_the_same_submission() -> None:
    service = _log_service(_ledger(), None)

    applied = service.stage_transition_already_applied("task:x", "node:c", stage_goal=GOAL)

    assert applied["stage_index"] == 2
    assert applied["status"] == "进行中"
    assert applied["active"] is True
    # 空目标与不存在的目标都不能被认成"已应用"，否则会吞掉模型真正发出的第一次收口。
    assert service.stage_transition_already_applied("task:x", "node:c", stage_goal="") == {}
    assert service.stage_transition_already_applied("task:x", "node:c", stage_goal="换一个目标") == {}


def test_unresolved_dispatch_reports_every_claim_when_children_still_running() -> None:
    stage = ExecutionStageRecord(
        stage_id="stage:2",
        stage_index=2,
        stage_goal=GOAL,
        status="进行中",
        rounds=[ExecutionStageRound(round_id="round-1", tool_call_ids=["call-spawn"])],
    )
    service = _log_service(_ledger(), {"phase": "waiting_children", "pending_tool_calls": [], "tool_calls": []})

    assert service._unresolved_dispatch_in_stage(task_id="task:x", node_id="node:c", stage=stage) == ["call-spawn"]

    # 子节点已终态、结果也回来：不再拦结清（否则模型永远无法推进阶段）。
    settled = _log_service(
        _ledger(),
        {
            "phase": "before_model",
            "pending_tool_calls": [],
            "tool_calls": [{"tool_call_id": "call-spawn", "finished_at": "2026-10-03T21:45:34+08:00"}],
        },
    )
    assert settled._unresolved_dispatch_in_stage(task_id="task:x", node_id="node:c", stage=stage) == []

    # 判据只认帧的活状态：结果表有缺行，拿它当"未送达"会连合法裁撤一起拦住。
    pending = _log_service(
        _ledger(),
        {"phase": "before_model", "pending_tool_calls": [{"id": "call-spawn", "name": "spawn_child_nodes"}], "tool_calls": []},
    )
    assert pending._unresolved_dispatch_in_stage(task_id="task:x", node_id="node:c", stage=stage) == ["call-spawn"]


def test_eviction_archive_marks_a_call_that_had_not_returned_yet() -> None:
    service = object.__new__(TaskLogService)
    service._store = SimpleNamespace(list_task_node_tool_results=lambda task_id, node_id: [])
    stage = ExecutionStageRecord(
        stage_id="stage:2",
        stage_index=2,
        stage_goal=GOAL,
        rounds=[ExecutionStageRound(round_id="round-1", tool_call_ids=["call-spawn"])],
    )

    record = service._execution_stage_eviction_record("task:x", "node:c", stage)

    # 归档是模型唯一的回读入口：留一个光杆 call id 等于谎称"打开就能知道做了什么"。
    assert record["rounds"][0]["tools"] == [
        {"tool_call_id": "call-spawn", "status": "unresolved_at_eviction"}
    ]


def _resume_loop(pending: list[dict], ledger_state: ExecutionStageState):
    loop = object.__new__(ReActToolLoop)
    replayed: list[list[str]] = []
    loop._recovery_check_engine = SimpleNamespace(
        inspect_tool_call=lambda **kwargs: SimpleNamespace(
            decision=RecoveryCheckDecision.RERUN_SAFE,
            expected_tool_status="",
            lost_result_summary="stub summary",
            evidence=[],
        )
    )
    loop._log_service = SimpleNamespace(
        update_node_input=lambda *args, **kwargs: None,
        update_frame=lambda *args, **kwargs: None,
        upsert_synthetic_tool_result=lambda **kwargs: None,
        stage_transition_already_applied=lambda task_id, node_id, *, stage_goal: (
            {"stage_id": "stage:2", "stage_index": 2, "status": "进行中", "active": True}
            if any(str(stage.stage_goal or "") == stage_goal for stage in ledger_state.stages)
            else {}
        ),
    )
    loop._runtime_frame = lambda task_id, node_id: {
        "active_round_id": "round-1",
        "pending_tool_calls": pending,
        "tool_calls": [],
    }
    loop._distribution_priority_blocks_recovery = lambda **kwargs: False
    loop._pending_tool_turn_content = lambda **kwargs: "round text"
    loop._pending_tool_turn_reasoning_field = lambda **kwargs: {}
    loop._collect_content_refs = lambda history: []
    loop._overflowed_search_signatures = lambda history: set()
    loop._recovery_check_tool_call_id = lambda *args, **kwargs: "recovery-check-1"
    loop._record_recovery_resolution_tool_result = lambda **kwargs: None
    loop._record_recovery_check_tool_result = lambda **kwargs: None
    loop._recovery_check_overall_decision = lambda items: RecoveryCheckDecision.RERUN_SAFE
    loop._recovery_checked_tool_content = lambda item: "content"
    loop._dedupe_tool_messages = lambda messages, existing_messages=None: list(messages)
    loop._prepare_messages = lambda history, runtime_context=None: list(history)

    async def _execute(**kwargs):
        calls = list(kwargs.get("response_tool_calls") or [])
        replayed.append([str(call.id) for call in calls])
        return [
            {
                "index": index,
                "live_state": {"tool_call_id": str(call.id), "tool_name": str(call.name), "status": "success"},
                "tool_message": {"role": "tool", "tool_call_id": str(call.id), "name": str(call.name),
                                 "content": "replayed body"},
            }
            for index, call in enumerate(calls)
        ]

    loop._execute_tool_calls = _execute
    task = SimpleNamespace(task_id="task:x", root_node_id="node:r")
    node = SimpleNamespace(node_id="node:c", task_id="task:x", depth=1, node_kind="execution",
                           metadata={}, status="in_progress", goal="g")
    return loop, replayed, task, node


def _tool_messages(history) -> dict[str, str]:
    return {str(m.get("tool_call_id")): str(m.get("content") or "") for m in history if m.get("role") == "tool"}


def test_resume_suppresses_replayed_submit_next_stage() -> None:
    pending = [
        {
            "id": "call-submit",
            "name": STAGE_TOOL_NAME,
            "arguments": {"stage_goal": GOAL, "tool_round_budget": 15,
                          "completed_stage_summary": "上一阶段总结", "drop_completed_stage_tool_detail": True},
        },
        {"id": "call-spawn", "name": "spawn_child_nodes", "arguments": {"children": [{"goal": "板块1"}]}},
    ]
    loop, replayed, task, node = _resume_loop(pending, _ledger())

    history = asyncio.run(loop._resume_pending_tool_turn_if_needed(
        task=task, node=node, message_history=[{"role": "user", "content": "start"}],
        tools={STAGE_TOOL_NAME: SimpleNamespace(rerun_safe=True), "spawn_child_nodes": SimpleNamespace(rerun_safe=True)},
        runtime_context={"node_id": node.node_id},
    ))

    assert history is not None
    messages = _tool_messages(history)
    # 重放已经落到账本的收口＝把阶段再往前推一格 + 用旧摘要裁撤还持有未送达调用的阶段：必须抑制。
    assert "suppressed_replay" in messages["call-submit"]
    assert json.loads(messages["call-submit"])["suppressed_replay"] is True
    # 同批里那条真正还在飞的派生照常重放，不受抑制影响。
    assert replayed == [["call-spawn"]]
