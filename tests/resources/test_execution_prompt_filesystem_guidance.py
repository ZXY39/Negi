from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_execution_prompts_use_exec_for_reading_and_filesystem_for_mutation() -> None:
    for relative_path in (
        "main/prompts/node_execution.md",
        "main/prompts/acceptance_execution.md",
    ):
        prompt = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "`exec`" in prompt
        assert "`filesystem_write`" in prompt
        assert "`filesystem_edit`" in prompt
        assert "`filesystem_copy`" in prompt
        assert "`filesystem_move`" in prompt
        assert "`filesystem_delete`" in prompt
        assert "`filesystem_propose_patch`" in prompt
        assert "`filesystem_list`" not in prompt
        assert "`filesystem_search`" not in prompt
        assert "`filesystem_describe`" not in prompt


def test_prompts_and_exec_manifest_do_not_hardcode_exec_as_read_only() -> None:
    prompt_paths = (
        "g3ku/runtime/prompts/ceo_frontdoor.md",
        "main/prompts/node_execution.md",
        "main/prompts/acceptance_execution.md",
    )
    for relative_path in prompt_paths:
        prompt = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "只读 `exec`" not in prompt
        assert "read-only `exec`" not in prompt
        assert "runtime tool contract" in prompt or "load_tool_context" in prompt

    exec_manifest = (REPO_ROOT / "tools/exec/resource.yaml").read_text(encoding="utf-8")
    assert "read-only shell commands" not in exec_manifest


def test_ceo_frontdoor_prompt_prefers_direct_visual_reasoning_for_current_turn_images() -> None:
    prompt = (REPO_ROOT / "g3ku/runtime/prompts/ceo_frontdoor.md").read_text(encoding="utf-8")
    assert "当前轮已经包含图片输入时，优先直接基于图片内容回答" in prompt
    assert "不要为了查看同一张当前轮图片而优先调用 `exec`、`content_open`" in prompt
