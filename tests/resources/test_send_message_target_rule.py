"""`send_message` 的收件人规则：任务级补充只发根节点，定向通知才点名节点。

实盘形状（会话 `ext:qq-official-1903529517:f8a8001865631301`）：前门一条广播发给
根节点 + 9 个子节点，3 分钟后根节点又把同一段文字向那 9 个子节点转述一遍——
子节点各读两份，而父节点压根不知道子节点已经收到过。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from g3ku.resources import ResourceManager
from g3ku.runtime.tool_contract_block import render_tool_contract
from main.service.runtime_service import MainRuntimeService

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = REPO_ROOT / 'tools' / 'send_message'
SKILL_BODY = TOOL_ROOT / 'toolskills' / 'SKILL.md'
FRONTDOOR_PROMPT = REPO_ROOT / 'g3ku' / 'runtime' / 'prompts' / 'ceo_frontdoor.md'


class _DummyChatBackend:
    async def chat(self, **kwargs: Any) -> Any:
        raise AssertionError(f'chat backend should not be called in this test: {kwargs!r}')


def _resource_app_config() -> SimpleNamespace:
    return SimpleNamespace(
        resources=SimpleNamespace(
            enabled=True,
            skills_dir='skills',
            tools_dir='tools',
            manifest_name='resource.yaml',
            state_path='.g3ku/resources.state.json',
            reload=SimpleNamespace(
                enabled=False,
                poll_interval_ms=10000,
                debounce_ms=100,
                lazy_reload_on_access=False,
                keep_last_good_version=True,
            ),
            locks=SimpleNamespace(
                lock_dir='.g3ku/resource-locks',
                logical_delete_guard=True,
                windows_fs_lock=True,
            ),
        )
    )


def test_toolskill_is_enabled_and_carries_the_target_rule() -> None:
    manifest = yaml.safe_load((TOOL_ROOT / 'resource.yaml').read_text(encoding='utf-8-sig'))
    assert (manifest.get('toolskill') or {}).get('enabled') is True, '关掉 toolskill 等于这条规则到不了模型'

    body = SKILL_BODY.read_text(encoding='utf-8')
    assert 'root node only' in body
    assert 'Do not put several nodes in one call' in body
    assert 'a parent never receives a copy of the mail addressed to its children' in body


def test_frontdoor_prompt_does_not_offer_the_broadcast_escape_hatch() -> None:
    """提示词只留一句指针，规则本体住在那份随契约到达的 toolskill 里。"""
    prompt = FRONTDOOR_PROMPT.read_text(encoding='utf-8')
    assert '必要时同时给相关执行节点' not in prompt
    assert '只有针对某个节点的定向通知才点名那个节点' in prompt
    assert '收件人规则按该工具说明执行' in prompt


@pytest.mark.asyncio
async def test_loader_delivers_the_rule_in_the_contract_block(tmp_path: Path) -> None:
    """规则必须活到模型可见的那一份契约里，而不是只躺在 skill 文件里。"""
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(TOOL_ROOT, workspace / 'tools' / 'send_message', ignore=shutil.ignore_patterns('__pycache__'))

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=workspace,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    service.bind_resource_manager(manager)
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-bind')
    service.resource_registry.refresh_from_current_resources()
    service.policy_engine.sync_default_role_policies()
    try:
        loaded = service.load_tool_context(actor_role='ceo', session_id='web:shared', tool_id='send_message')
        block = render_tool_contract(loaded)
        assert 'root node only' in block
        assert 'Do not put several nodes in one call' in block
    finally:
        manager.close()
        await service.close()
