from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ddg_web_search_is_gated_by_web_fetch_requirement():
    manifest = yaml.safe_load((REPO_ROOT / 'skills' / 'ddg-web-search' / 'resource.yaml').read_text(encoding='utf-8'))
    required_tools = ((manifest.get('requires') or {}).get('tools') or [])
    assert 'web_fetch' in required_tools


def test_memory_skill_uses_current_search_guidance():
    content = (REPO_ROOT / 'skills' / 'memory' / 'SKILL.md').read_text(encoding='utf-8')
    assert 'rg -n -i "关键词" memory/HISTORY.md' in content
    assert '使用 `exec` 工具运行 grep' not in content


def test_tmux_skill_does_not_claim_exec_background_mode():
    content = (REPO_ROOT / 'skills' / 'tmux' / 'SKILL.md').read_text(encoding='utf-8')
    assert 'exec 后台模式' not in content


def test_model_config_tool_no_longer_contains_removed_memory_action():
    content = (REPO_ROOT / 'g3ku' / 'agent' / 'tools' / 'model_config.py').read_text(encoding='utf-8')
    assert 'set_memory_models' not in content

