from __future__ import annotations

from pathlib import Path

import asyncio
from types import SimpleNamespace

from main.runtime.recovery_check import RecoveryCheckDecision, RecoveryCheckEngine
from main.runtime.react_loop import ReActToolLoop


def _engine(tmp_path: Path) -> RecoveryCheckEngine:
    return RecoveryCheckEngine(workspace_root=tmp_path)


def test_recovery_check_filesystem_write_verifies_done_when_target_matches(tmp_path: Path) -> None:
    target = tmp_path / "output.txt"
    target.write_text("expected body", encoding="utf-8")

    result = _engine(tmp_path).inspect_tool_call(
        tool_name="filesystem_write",
        arguments={
            "path": str(target),
            "content": "expected body",
        },
        runtime_context={"task_temp_dir": str(tmp_path)},
    )

    assert result.decision == RecoveryCheckDecision.VERIFIED_DONE
    assert result.expected_tool_status == "success"
    assert "already matches requested content" in result.lost_result_summary
    assert result.evidence
    assert result.evidence[0]["kind"] == "file"
    assert result.evidence[0]["path"] == str(target)


def test_recovery_check_filesystem_edit_verifies_done_when_expected_edit_already_applied(tmp_path: Path) -> None:
    target = tmp_path / "edit.txt"
    target.write_text("alpha\nnew line\nomega\n", encoding="utf-8")

    result = _engine(tmp_path).inspect_tool_call(
        tool_name="filesystem_edit",
        arguments={
            "path": str(target),
            "target": {"by": "exact_text", "text": "old line"},
            "new_text": "new line",
        },
        runtime_context={"task_temp_dir": str(tmp_path)},
    )

    assert result.decision == RecoveryCheckDecision.VERIFIED_DONE
    assert result.expected_tool_status == "success"
    assert "requested edit is already reflected on disk" in result.lost_result_summary
    assert result.evidence
    assert result.evidence[0]["path"] == str(target)


def test_recovery_check_filesystem_edit_verifies_done_for_flat_text_pair(tmp_path: Path) -> None:
    target = tmp_path / "flat-edit.txt"
    target.write_text("alpha\nnew line\nomega\n", encoding="utf-8")

    result = _engine(tmp_path).inspect_tool_call(
        tool_name="filesystem_edit",
        arguments={
            "path": str(target),
            "old_text": "old line",
            "new_text": "new line",
        },
        runtime_context={"task_temp_dir": str(tmp_path)},
    )

    assert result.decision == RecoveryCheckDecision.VERIFIED_DONE
    assert result.expected_tool_status == "success"
    assert "requested edit is already reflected on disk" in result.lost_result_summary


def test_recovery_check_filesystem_copy_verifies_done_when_all_targets_exist_and_sources_remain(tmp_path: Path) -> None:
    source_a = tmp_path / "source-a.txt"
    source_b = tmp_path / "source-b.txt"
    target_a = tmp_path / "target-a.txt"
    target_b = tmp_path / "target-b.txt"
    source_a.write_text("alpha", encoding="utf-8")
    source_b.write_text("beta", encoding="utf-8")
    target_a.write_text("alpha", encoding="utf-8")
    target_b.write_text("beta", encoding="utf-8")

    result = _engine(tmp_path).inspect_tool_call(
        tool_name="filesystem_copy",
        arguments={
            "operations": [
                {"source": str(source_a), "destination": str(target_a)},
                {"source": str(source_b), "destination": str(target_b)},
            ]
        },
        runtime_context={"task_temp_dir": str(tmp_path)},
    )

    assert result.decision == RecoveryCheckDecision.VERIFIED_DONE
    assert result.expected_tool_status == "success"
    assert "copy request already completed" in result.lost_result_summary
    assert len(result.evidence) == 2


def test_recovery_check_filesystem_move_verifies_done_when_targets_exist_and_sources_are_gone(tmp_path: Path) -> None:
    source_a = tmp_path / "source-a.txt"
    source_b = tmp_path / "source-b.txt"
    target_a = tmp_path / "target-a.txt"
    target_b = tmp_path / "target-b.txt"
    target_a.write_text("alpha", encoding="utf-8")
    target_b.write_text("beta", encoding="utf-8")

    result = _engine(tmp_path).inspect_tool_call(
        tool_name="filesystem_move",
        arguments={
            "operations": [
                {"source": str(source_a), "destination": str(target_a)},
                {"source": str(source_b), "destination": str(target_b)},
            ]
        },
        runtime_context={"task_temp_dir": str(tmp_path)},
    )

    assert result.decision == RecoveryCheckDecision.VERIFIED_DONE
    assert result.expected_tool_status == "success"
    assert "move request already completed" in result.lost_result_summary
    assert len(result.evidence) == 2


def test_recovery_check_filesystem_delete_verifies_done_when_targets_are_missing(tmp_path: Path) -> None:
    target_a = tmp_path / "target-a.txt"
    target_b = tmp_path / "target-b.txt"

    result = _engine(tmp_path).inspect_tool_call(
        tool_name="filesystem_delete",
        arguments={
            "paths": [str(target_a), str(target_b)],
        },
        runtime_context={"task_temp_dir": str(tmp_path)},
    )

    assert result.decision == RecoveryCheckDecision.VERIFIED_DONE
    assert result.expected_tool_status == "success"
    assert "delete request already completed" in result.lost_result_summary
    assert len(result.evidence) == 2


def test_recovery_check_exec_defaults_to_model_decide_when_side_effect_is_uncertain(tmp_path: Path) -> None:
    result = _engine(tmp_path).inspect_tool_call(
        tool_name="exec",
        arguments={"command": "git apply patch.diff"},
        runtime_context={"task_temp_dir": str(tmp_path)},
    )

    assert result.decision == RecoveryCheckDecision.MODEL_DECIDE
    assert result.expected_tool_status == "interrupted"
    assert "must verify whether the previous side effect already completed" in result.lost_result_summary
    assert result.evidence == []


def test_recovery_check_undeclared_tool_is_not_rerun(tmp_path: Path) -> None:
    result = _engine(tmp_path).inspect_tool_call(
        tool_name="content",
        arguments={"action": "search", "path": str(tmp_path), "query": "needle"},
        runtime_context={"task_temp_dir": str(tmp_path)},
    )

    assert result.decision == RecoveryCheckDecision.MODEL_DECIDE
    assert result.expected_tool_status == "interrupted"
    assert "no rerun-safe declaration" in result.lost_result_summary
    assert result.evidence == []


def test_recovery_check_declared_rerun_safe_tool_is_rerun(tmp_path: Path) -> None:
    result = _engine(tmp_path).inspect_tool_call(
        tool_name="content",
        arguments={"action": "search", "path": str(tmp_path), "query": "needle"},
        runtime_context={"task_temp_dir": str(tmp_path)},
        rerun_safe=True,
    )

    assert result.decision == RecoveryCheckDecision.RERUN_SAFE
    assert result.expected_tool_status == ""
    assert "declares itself rerun-safe" in result.lost_result_summary


def test_recovery_check_declaration_cannot_lighten_exec(tmp_path: Path) -> None:
    result = _engine(tmp_path).inspect_tool_call(
        tool_name="exec",
        arguments={"command": "make deploy"},
        runtime_context={"task_temp_dir": str(tmp_path)},
        rerun_safe=True,
    )

    assert result.decision == RecoveryCheckDecision.MODEL_DECIDE



# --- 恢复时按批内逐条判档（帧活状态里已完成的调用直接复用其结果） ---


class _EngineStub:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def inspect_tool_call(self, *, tool_name: str, arguments: dict, runtime_context: dict, rerun_safe: bool = False):
        self.asked.append(tool_name)
        decision = RecoveryCheckDecision.MODEL_DECIDE if tool_name == "exec" else RecoveryCheckDecision.RERUN_SAFE
        return SimpleNamespace(
            decision=decision,
            expected_tool_status="interrupted" if decision is RecoveryCheckDecision.MODEL_DECIDE else "",
            lost_result_summary="stub summary",
            evidence=[],
        )


def _resume_loop():
    engine = _EngineStub()
    replayed: list[list[str]] = []
    loop = object.__new__(ReActToolLoop)
    loop._recovery_check_engine = engine
    loop._log_service = SimpleNamespace(
        update_node_input=lambda *args, **kwargs: None,
        update_frame=lambda *args, **kwargs: None,
        upsert_synthetic_tool_result=lambda **kwargs: None,
    )
    loop._runtime_frame = lambda task_id, node_id: {
        "active_round_id": "round-1",
        "pending_tool_calls": [
            {"id": "call-done", "name": "filesystem_stat", "arguments": {"paths": []}},
            {"id": "call-exec", "name": "exec", "arguments": {"command": "make deploy"}},
            {"id": "call-read", "name": "content_search", "arguments": {"ref": "artifact:x"}},
        ],
        "tool_calls": [
            {"tool_call_id": "call-done", "tool_name": "filesystem_stat", "status": "success",
             "started_at": "t0", "finished_at": "t1", "elapsed_seconds": 1.0,
             "result_content": "ALREADY-RECORDED-STAT-BODY"},
            {"tool_call_id": "call-exec", "tool_name": "exec", "status": "running",
             "started_at": "t0", "finished_at": "", "elapsed_seconds": None},
            {"tool_call_id": "call-read", "tool_name": "content_search", "status": "queued",
             "started_at": "", "finished_at": "", "elapsed_seconds": None},
        ],
    }
    loop._distribution_priority_blocks_recovery = lambda **kwargs: False
    loop._pending_tool_turn_content = lambda **kwargs: "round text"
    loop._collect_content_refs = lambda history: []
    loop._overflowed_search_signatures = lambda history: set()
    loop._execution_stage_frame_payload = lambda **kwargs: {}
    loop._execution_stage_gate = lambda **kwargs: {}
    loop._recovery_check_tool_call_id = lambda *args, **kwargs: "recovery-check-1"
    loop._record_recovery_resolution_tool_result = lambda **kwargs: None

    async def _execute(**kwargs):
        replayed.append([str(call.id) for call in list(kwargs.get("response_tool_calls") or [])])
        return [
            {
                "index": 0,
                "live_state": {"tool_call_id": "call-read", "tool_name": "content_search", "status": "success"},
                "tool_message": {"role": "tool", "tool_call_id": "call-read", "name": "content_search",
                                 "content": "fresh search body"},
            }
        ]

    async def _noop_async(**kwargs):
        return None

    loop._execute_tool_calls = _execute
    loop._record_tool_result_batch = _noop_async
    task = SimpleNamespace(task_id="task:x", root_node_id="node:r")
    node = SimpleNamespace(node_id="node:c", task_id="task:x", depth=1, node_kind="execution",
                           metadata={}, status="in_progress", goal="g")
    return loop, engine, replayed, task, node


def _tool_messages(history):
    return {str(m.get("tool_call_id")): str(m.get("content") or "") for m in history if m.get("role") == "tool"}


def test_resume_reuses_recorded_result_and_asks_per_call() -> None:
    loop, engine, replayed, task, node = _resume_loop()

    history = asyncio.run(loop._resume_pending_tool_turn_if_needed(
        task=task, node=node, message_history=[{"role": "user", "content": "start"}],
        tools={"content_search": object(), "exec": object(), "filesystem_stat": object()},
        runtime_context={"node_id": node.node_id},
    ))

    assert history is not None
    messages = _tool_messages(history)
    # 1) 已完成的那条：复用停机前留在帧里的结果正文，不问分类器、不重放
    assert "ALREADY-RECORDED-STAT-BODY" in messages.get("call-done", "")
    assert "filesystem_stat" not in engine.asked
    # 2) 发起未完成的 exec 与从未发起的只读调用都照常问分类器（前者 model_decide、后者重放）
    assert engine.asked == ["exec", "content_search"]
    # 3) 从未发起的只读调用：整批不再被一锅端，只有这一条被重放
    assert replayed == [["call-read"]]


def test_resume_does_not_replay_a_completed_call_even_when_classifier_would() -> None:
    loop, engine, replayed, task, node = _resume_loop()

    asyncio.run(loop._resume_pending_tool_turn_if_needed(
        task=task, node=node, message_history=[],
        tools={"content_search": object(), "exec": object(), "filesystem_stat": object()},
        runtime_context={"node_id": node.node_id},
    ))

    assert "call-done" not in [cid for batch in replayed for cid in batch]
    assert "filesystem_stat" not in engine.asked


# --- 可重放档位来自工具声明（Tool.rerun_safe / 清单 recovery_policy） ---


def _declaration_loop(tmp_path: Path):
    replayed: list[list[str]] = []
    loop = object.__new__(ReActToolLoop)
    loop._recovery_check_engine = RecoveryCheckEngine(workspace_root=tmp_path)
    loop._log_service = SimpleNamespace(
        update_node_input=lambda *args, **kwargs: None,
        update_frame=lambda *args, **kwargs: None,
        upsert_synthetic_tool_result=lambda **kwargs: None,
    )
    loop._runtime_frame = lambda task_id, node_id: {
        "active_round_id": "round-1",
        "pending_tool_calls": [
            {"id": "call-read", "name": "content_search", "arguments": {"query": "needle"}},
            {"id": "call-write", "name": "memory_write", "arguments": {"content": "note"}},
        ],
        "tool_calls": [],
    }
    loop._distribution_priority_blocks_recovery = lambda **kwargs: False
    loop._pending_tool_turn_content = lambda **kwargs: "round text"
    loop._collect_content_refs = lambda history: []
    loop._overflowed_search_signatures = lambda history: set()
    loop._execution_stage_frame_payload = lambda **kwargs: {}
    loop._execution_stage_gate = lambda **kwargs: {}
    loop._recovery_check_tool_call_id = lambda *args, **kwargs: "recovery-check-1"
    loop._record_recovery_resolution_tool_result = lambda **kwargs: None

    async def _execute(**kwargs):
        replayed.append([str(call.id) for call in list(kwargs.get("response_tool_calls") or [])])
        return [
            {
                "index": 0,
                "live_state": {"tool_call_id": "call-read", "tool_name": "content_search", "status": "success"},
                "tool_message": {"role": "tool", "tool_call_id": "call-read", "name": "content_search",
                                 "content": "fresh search body"},
            }
        ]

    async def _noop_async(**kwargs):
        return None

    loop._execute_tool_calls = _execute
    loop._record_tool_result_batch = _noop_async
    task = SimpleNamespace(task_id="task:x", root_node_id="node:r")
    node = SimpleNamespace(node_id="node:c", task_id="task:x", depth=1, node_kind="execution",
                           metadata={}, status="in_progress", goal="g")
    return loop, replayed, task, node


def test_resume_replays_only_calls_whose_tool_declares_rerun_safe(tmp_path: Path) -> None:
    loop, replayed, task, node = _declaration_loop(tmp_path)

    history = asyncio.run(loop._resume_pending_tool_turn_if_needed(
        task=task, node=node, message_history=[],
        tools={
            "content_search": SimpleNamespace(rerun_safe=True),
            "memory_write": SimpleNamespace(rerun_safe=False),
        },
        runtime_context={"node_id": node.node_id},
    ))

    messages = _tool_messages(history)
    assert replayed == [["call-read"]]
    assert "fresh search body" in messages.get("call-read", "")
    # 未声明的那条不重放：把裁定交回模型，正文里带"结果在停机中丢失"。
    assert "may have already produced side effects" in messages.get("call-write", "")


def test_resume_treats_a_tool_missing_from_the_callable_map_as_undeclared(tmp_path: Path) -> None:
    loop, replayed, task, node = _declaration_loop(tmp_path)

    history = asyncio.run(loop._resume_pending_tool_turn_if_needed(
        task=task, node=node, message_history=[],
        tools={},
        runtime_context={"node_id": node.node_id},
    ))

    messages = _tool_messages(history)
    assert replayed == []
    assert "no rerun-safe declaration" in messages.get("call-read", "")
