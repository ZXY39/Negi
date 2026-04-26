from __future__ import annotations

from pathlib import Path

from g3ku.runtime.frontdoor.tool_contract import (
    build_frontdoor_tool_contract,
    upsert_frontdoor_tool_contract_message,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ceo_frontdoor_prompt_mentions_create_async_task_file_targets() -> None:
    prompt = (REPO_ROOT / "g3ku/runtime/prompts/ceo_frontdoor.md").read_text(encoding="utf-8")

    assert "`file_targets`" in prompt
    assert "`path`" in prompt
    assert "`ref`" in prompt
    assert "`user_uploads`" in prompt
    assert "`current_uploads`" in prompt
    assert "`user_image_and_docx`" in prompt


def test_frontdoor_contract_guides_models_to_copy_reopen_targets_into_file_targets() -> None:
    contract = build_frontdoor_tool_contract(
        callable_tool_names=["create_async_task", "content_open"],
        candidate_tool_names=[],
        candidate_tool_items=[],
        hydrated_tool_names=[],
        frontdoor_stage_state={
            "active_stage_id": "stage:1",
            "transition_required": False,
            "stages": [{"stage_id": "stage:1", "status": "active", "stage_goal": "dispatch"}],
        },
        visible_skill_ids=[],
        candidate_skill_ids=[],
        rbac_visible_tool_names=["create_async_task", "content_open"],
        rbac_visible_skill_ids=[],
        contract_revision="frontdoor:v1",
        attachment_reopen_targets=[
            {
                "name": "resume.docx",
                "kind": "file",
                "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "path": "D:/Uploads/resume.docx",
            }
        ],
    )

    updated = upsert_frontdoor_tool_contract_message([], contract)
    contract_text = str(updated[0]["content"] or "")

    assert "attachment_reopen_targets:" in contract_text
    assert "create_async_task.file_targets" in contract_text
    assert "create_async_task.task" in contract_text
    assert "current_uploads" in contract_text
