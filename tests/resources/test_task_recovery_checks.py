from __future__ import annotations

from pathlib import Path

from main.runtime.recovery_check import RecoveryCheckDecision, RecoveryCheckEngine


def _engine(tmp_path: Path) -> RecoveryCheckEngine:
    return RecoveryCheckEngine(workspace_root=tmp_path)


def test_recovery_check_filesystem_write_verifies_done_when_target_matches(tmp_path: Path) -> None:
    target = tmp_path / "output.txt"
    target.write_text("expected body", encoding="utf-8")

    result = _engine(tmp_path).inspect_tool_call(
        tool_name="filesystem",
        arguments={
            "action": "write",
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
        tool_name="filesystem",
        arguments={
            "action": "edit",
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
        tool_name="filesystem",
        arguments={"action": "list", "path": str(tmp_path)},
        runtime_context={"task_temp_dir": str(tmp_path)},
    )

    assert result.decision == RecoveryCheckDecision.RERUN_SAFE
    assert result.expected_tool_status == ""
    assert "safe to rerun" in result.lost_result_summary
