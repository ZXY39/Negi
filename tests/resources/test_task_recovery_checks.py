from __future__ import annotations

from pathlib import Path

from main.runtime.recovery_check import RecoveryCheckDecision, RecoveryCheckEngine


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
            "old_text": "old line",
            "new_text": "new line",
        },
        runtime_context={"task_temp_dir": str(tmp_path)},
    )

    assert result.decision == RecoveryCheckDecision.VERIFIED_DONE
    assert result.expected_tool_status == "success"
    assert "requested edit is already reflected on disk" in result.lost_result_summary
    assert result.evidence
    assert result.evidence[0]["path"] == str(target)


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


def test_recovery_check_read_only_tools_default_to_rerun_safe(tmp_path: Path) -> None:
    result = _engine(tmp_path).inspect_tool_call(
        tool_name="content",
        arguments={"action": "search", "path": str(tmp_path), "query": "needle"},
        runtime_context={"task_temp_dir": str(tmp_path)},
    )

    assert result.decision == RecoveryCheckDecision.RERUN_SAFE
    assert result.expected_tool_status == ""
    assert "safe to rerun" in result.lost_result_summary
