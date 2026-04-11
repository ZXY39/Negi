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
        assert "`filesystem_delete`" in prompt
        assert "`filesystem_propose_patch`" in prompt
        assert "`filesystem_list`" not in prompt
        assert "`filesystem_search`" not in prompt
        assert "`filesystem_describe`" not in prompt
