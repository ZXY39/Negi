from __future__ import annotations

from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ddg_web_search_is_gated_by_web_fetch_requirement():
    resource_path = REPO_ROOT / 'skills' / 'ddg-web-search' / 'resource.yaml'
    if not resource_path.exists():
        pytest.skip("ddg-web-search skill is not installed in this workspace")
    manifest = yaml.safe_load(resource_path.read_text(encoding='utf-8'))
    required_tools = ((manifest.get('requires') or {}).get('tools') or [])
    assert 'web_fetch' in required_tools


def test_memory_skill_uses_current_search_guidance():
    content = (REPO_ROOT / 'skills' / 'memory' / 'SKILL.md').read_text(encoding='utf-8')
    assert 'memory/HISTORY.md' not in content
    assert 'memory_note(ref=' in content
    assert 'MEMORY.md' in content


def test_tmux_skill_does_not_claim_exec_background_mode():
    content = (REPO_ROOT / 'skills' / 'tmux' / 'SKILL.md').read_text(encoding='utf-8')
    assert 'exec 后台模式' not in content


def test_model_config_tool_no_longer_contains_removed_memory_action():
    content = (REPO_ROOT / 'g3ku' / 'agent' / 'tools' / 'model_config.py').read_text(encoding='utf-8')
    assert 'set_memory_models' not in content

