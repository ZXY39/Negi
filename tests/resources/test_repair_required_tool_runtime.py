from __future__ import annotations

import json
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.resources import ResourceManager


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


def _write_repairable_tool(workspace: Path) -> None:
    tool_root = workspace / 'tools' / 'demo_repair'
    (tool_root / 'main').mkdir(parents=True, exist_ok=True)
    (tool_root / 'toolskills').mkdir(parents=True, exist_ok=True)
    (tool_root / 'resource.yaml').write_text(
        textwrap.dedent(
            """\
            schema_version: 1
            kind: tool
            name: demo_repair
            description: Demo repairable tool.
            tool_result_delivery_contract: runtime_managed
            tool_type: internal
            protocol: mcp
            mcp:
              transport: embedded
            requires:
              tools: []
              bins: []
              paths:
                - externaltools/demo_repair/runtime.ok
              env: []
            permissions:
              network: false
              filesystem:
                - workspace
            parameters:
              type: object
              properties:
                value:
                  type: string
                  description: Demo argument.
              required:
                - value
            exposure:
              agent: true
              main_runtime: true
            governance:
              family: demo_repair
              display_name: Demo Repair
              description: Demo repairable tool.
              actions:
                - id: run
                  label: Run Demo Repair Tool
                  risk_level: medium
                  destructive: false
                  allowed_roles:
                    - ceo
            toolskill:
              enabled: true
            """
        ),
        encoding='utf-8',
    )
    (tool_root / 'toolskills' / 'SKILL.md').write_text('# demo_repair\n\nRepair me when unavailable.\n', encoding='utf-8')
    (tool_root / 'main' / 'tool.py').write_text(
        textwrap.dedent(
            """\
            from __future__ import annotations

            import json
            from typing import Any

            from g3ku.agent.tools.base import Tool


            class DemoRepairTool(Tool):
                @property
                def name(self) -> str:
                    return 'demo_repair'

                @property
                def description(self) -> str:
                    return 'Demo repairable tool.'

                @property
                def parameters(self) -> dict[str, Any]:
                    return {
                        'type': 'object',
                        'properties': {
                            'value': {'type': 'string', 'description': 'Demo argument.'},
                        },
                        'required': ['value'],
                    }

                async def execute(self, value: str = '', **kwargs: Any) -> str:
                    return json.dumps({'ok': True, 'tool_state': 'ready', 'value': value, 'extra': kwargs}, ensure_ascii=False)


            def build(runtime):
                return DemoRepairTool()
            """
        ),
        encoding='utf-8',
    )


@pytest.mark.asyncio
async def test_resource_manager_registers_repair_required_stub_and_restores_real_tool_after_fix(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    (workspace / 'externaltools').mkdir(parents=True, exist_ok=True)
    _write_repairable_tool(workspace)

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    try:
        descriptor = manager.get_tool_descriptor('demo_repair')
        assert descriptor is not None
        assert descriptor.available is False

        tool = manager.get_tool('demo_repair')
        assert tool is not None
        assert tool.description.startswith('【待修复】')
        payload = json.loads(await tool.execute())
        assert payload['repair_required'] is True
        assert payload['tool_state'] == 'repair_required'

        runtime_flag = workspace / 'externaltools' / 'demo_repair' / 'runtime.ok'
        runtime_flag.parent.mkdir(parents=True, exist_ok=True)
        runtime_flag.write_text('ok\n', encoding='utf-8')

        manager.reload_now(trigger='test-fix')

        fixed_descriptor = manager.get_tool_descriptor('demo_repair')
        assert fixed_descriptor is not None
        assert fixed_descriptor.available is True

        fixed_tool = manager.get_tool('demo_repair')
        assert fixed_tool is not None
        assert not fixed_tool.description.startswith('【待修复】')
        fixed_payload = json.loads(await fixed_tool.execute(value='done'))
        assert fixed_payload['ok'] is True
        assert fixed_payload['tool_state'] == 'ready'
        assert fixed_payload['value'] == 'done'
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_repair_required_tool_ignores_runtime_context_in_argument_preview(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    (workspace / 'externaltools').mkdir(parents=True, exist_ok=True)
    _write_repairable_tool(workspace)

    manager = ResourceManager(workspace, app_config=_resource_app_config())

    class _ProgressSink:
        def emit(self, *_args, **_kwargs):
            return None

    try:
        tool = manager.get_tool('demo_repair')
        assert tool is not None
        payload = json.loads(
            await tool.execute(
                value='demo',
                __g3ku_runtime={'session_key': 'web:shared', 'on_progress': _ProgressSink().emit},
            )
        )

        assert payload['repair_required'] is True
        assert payload['argument_preview'] == {'value': 'demo'}
    finally:
        manager.close()
