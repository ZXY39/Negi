from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_execution_prompts_direct_directories_to_list_and_search_tools() -> None:
    for relative_path in (
        'main/prompts/node_execution.md',
        'main/prompts/acceptance_execution.md',
    ):
        prompt = (REPO_ROOT / relative_path).read_text(encoding='utf-8')
        assert '`filesystem_list`' in prompt
        assert '`filesystem_search`' in prompt
        assert '`filesystem_describe`' in prompt
        assert '不要把目录路径传给' in prompt
