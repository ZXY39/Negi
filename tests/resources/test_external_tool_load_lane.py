"""The agent-facing ``load_tool_context`` lane and registered external tools.

An external tool owns no executor instance, so both canonical name lists in the
runtime frame (built from instantiated names) exclude it by construction. These
tests pin that the loader still reaches it, that RBAC keeps denying other roles,
and that the fallback never widens the gate for normal callable tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.main_runtime import LoadToolContextTool
from g3ku.resources import ResourceManager
from main.runtime.react_loop import ReActToolLoop
from main.service.runtime_service import MainRuntimeService

EXTERNAL_TOOL = 'external_browser'


async def _execute_load(tool, *, tool_id: str, runtime: dict[str, object]) -> str:
    """Feed the runtime frame the way the live lane does.

    The private-looking parameter name is rewritten inside the class body, so the
    runtime resolves it from the signature instead of spelling it out.
    """
    parameter = ReActToolLoop._runtime_context_parameter_name(tool)
    assert parameter, 'loader must accept a runtime frame'
    return await tool.execute(**{parameter: runtime, 'tool_id': tool_id})


def _resource_app_config() -> SimpleNamespace:
    return SimpleNamespace(
        resources=SimpleNamespace(
            enabled=True,
            skills_dir='skills',
            tools_dir='tools',
            manifest_name='resource.yaml',
            state_path='.g3ku/resources.state.json',
            reload=SimpleNamespace(
                enabled=True,
                poll_interval_ms=200,
                debounce_ms=100,
                lazy_reload_on_access=True,
                keep_last_good_version=True,
            ),
            locks=SimpleNamespace(
                lock_dir='.g3ku/resource-locks',
                logical_delete_guard=True,
                windows_fs_lock=True,
            ),
        )
    )


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f'chat backend should not be used in this test: {kwargs!r}')


def _write_external_tool(workspace: Path, *, name: str = EXTERNAL_TOOL) -> Path:
    root = workspace / 'tools' / name
    (root / 'toolskills').mkdir(parents=True, exist_ok=True)
    install_dir = workspace / '.g3ku' / 'external-tools' / name
    install_dir.mkdir(parents=True, exist_ok=True)
    (root / 'resource.yaml').write_text(
        f"""schema_version: 1
kind: tool
name: {name}
description: Registered external tool.
tool_type: external
install_dir: .g3ku/external-tools/{name}
protocol: mcp
mcp:
  transport: embedded
parameters:
  type: object
  properties: {{}}
  required: []
governance:
  display_name: External Browser
  description: Registered external tool.
  risk_level: medium
  destructive: false
  allowed_roles:
    - ceo
    - execution
toolskill:
  enabled: true
""",
        encoding='utf-8',
    )
    (root / 'toolskills' / 'SKILL.md').write_text(
        f'# {name}\n\n## 何时使用\n\nNeed it.\n\n## 安装\n\nInstall outside tools.\n\n'
        '## 更新\n\nPull upstream.\n\n## 使用\n\nRun the driver from install_dir.\n',
        encoding='utf-8',
    )
    return install_dir


def _frontdoor_runtime(*, actor_role: str = 'ceo') -> dict[str, object]:
    """A frame that looks exactly like what the frontdoor publishes: instantiated
    names only, so the external tool is absent from both lists."""
    return {
        'tool_contract_enforced': True,
        'actor_role': actor_role,
        'session_key': 'web:shared',
        'candidate_tool_names': [],
        'callable_tool_names': ['exec'],
        'hydrated_tool_names': [],
        'rbac_visible_tool_names': ['exec'],
    }


def _build_service(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    install_dir = _write_external_tool(workspace)

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        resource_manager=manager,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
    )
    return service, manager, install_dir


@pytest.mark.asyncio
async def test_agent_lane_loads_registered_external_tool(tmp_path: Path) -> None:
    service, manager, install_dir = _build_service(tmp_path)
    try:
        await service.startup()
        tool = LoadToolContextTool(lambda: service)
        payload = json.loads(
            await _execute_load(tool, tool_id=EXTERNAL_TOOL, runtime=_frontdoor_runtime())
        )

        assert payload.get('ok') is True, payload
        assert payload.get('tool_id') == EXTERNAL_TOOL
        assert payload.get('tool_type') == 'external'
        assert payload.get('callable') is False
        assert str(payload.get('install_dir') or '').strip() == str(install_dir.resolve())
        assert '## 安装' in str(payload.get('content') or '')
        # External tools are never promotable, so the payload must not tell the
        # model to go pick this name out of the candidate list.
        assert payload.get('promotion') == 'external_resource'
        assert payload.get('will_be_hydrated_next_turn') is False
        assert list(payload.get('hydration_targets') or []) == []
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_agent_lane_rejects_external_tool_when_role_is_not_granted(tmp_path: Path) -> None:
    service, manager, _install_dir = _build_service(tmp_path)
    try:
        await service.startup()
        tool = LoadToolContextTool(lambda: service)
        text = await _execute_load(
            tool,
            tool_id=EXTERNAL_TOOL,
            runtime=_frontdoor_runtime(actor_role='inspection'),
        )

        assert text.startswith('Error:')
        assert EXTERNAL_TOOL in text
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_live_fallback_does_not_widen_the_gate_for_callable_tools() -> None:
    """A callable resource that is absent from the frame lists stays gated: the
    fallback exists only for records that cannot enter those lists by design."""
    calls: list[dict[str, object]] = []

    class _StubService:
        async def startup(self) -> None:
            return None

        def list_visible_tools(self, *, actor_role: str, session_id: str) -> list[object]:
            return [SimpleNamespace(tool_id='web_fetch', tool_type='internal', callable=True)]

        def load_tool_context_v2(self, **kwargs: object) -> dict[str, object]:
            calls.append(dict(kwargs))
            return {'ok': True, 'tool_id': str(kwargs.get('tool_id') or '')}

    tool = LoadToolContextTool(lambda: _StubService())
    text = await _execute_load(tool, tool_id='web_fetch', runtime=_frontdoor_runtime())

    assert text.startswith('Error:')
    assert calls == []
