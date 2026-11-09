from __future__ import annotations

import shutil
from pathlib import Path

from g3ku.resources.manager import ResourceManager
from g3ku.resources.registry import ResourceRegistry


def _write_tool(root: Path, name: str, requires: list[str]) -> None:
    tool_dir = root / 'tools' / name
    (tool_dir / 'main').mkdir(parents=True, exist_ok=True)
    (tool_dir / 'main' / 'tool.py').write_text(
        'from g3ku.agent.tools.base import Tool\n'
        '\n'
        '\n'
        'class ProbeTool(Tool):\n'
        '    @property\n'
        '    def name(self):\n'
        f'        return {name!r}\n'
        '\n'
        '    @property\n'
        '    def description(self):\n'
        f'        return {name + " tool"!r}\n'
        '\n'
        '    @property\n'
        '    def parameters(self):\n'
        "        return {'type': 'object', 'properties': {}, 'required': []}\n"
        '\n'
        '    async def execute(self, **kwargs):\n'
        "        return 'ok'\n"
        '\n'
        '\n'
        'def build(runtime):\n'
        '    return ProbeTool()\n',
        encoding='utf-8',
    )
    requires_block = ', '.join(requires)
    (tool_dir / 'resource.yaml').write_text(
        'schema_version: 1\n'
        'kind: tool\n'
        f'name: {name}\n'
        f'description: {name} tool\n'
        'tool_type: internal\n'
        'requires:\n'
        f'  tools: [{requires_block}]\n'
        '  bins: []\n'
        '  env: []\n'
        'parameters:\n'
        '  type: object\n'
        '  properties: {}\n'
        'tool_result_delivery_contract: runtime_managed\n'
        'toolskill:\n'
        '  enabled: false\n',
        encoding='utf-8',
    )


def test_tool_requires_tools_is_probed_against_discovered_names(tmp_path: Path) -> None:
    _write_tool(tmp_path, 'alpha', [])
    _write_tool(tmp_path, 'beta', ['alpha'])
    _write_tool(tmp_path, 'gamma', ['missing_tool'])

    registry = ResourceRegistry(
        tmp_path,
        skills_dir=tmp_path / 'skills',
        tools_dir=tmp_path / 'tools',
    )

    tools = registry.discover().tools

    assert tools['alpha'].available is True
    assert tools['beta'].available is True
    assert tools['gamma'].available is False
    assert 'missing required tools: missing_tool' in tools['gamma'].warnings


def test_targeted_refresh_rebuilds_dependents_when_required_tool_changes(tmp_path: Path) -> None:
    _write_tool(tmp_path, 'alpha', [])
    _write_tool(tmp_path, 'beta', ['alpha'])
    manager = ResourceManager(tmp_path)
    manager.reload_now(trigger='test')

    alpha_root = tmp_path / 'tools' / 'alpha'
    descriptors = {descriptor.name: descriptor for descriptor in manager.list_tools()}
    assert descriptors['beta'].available is True

    shutil.rmtree(alpha_root)
    manager.refresh_roots(tool_roots={alpha_root})

    descriptors = {descriptor.name: descriptor for descriptor in manager.list_tools()}
    assert 'alpha' not in descriptors
    assert descriptors['beta'].available is False
    assert 'missing required tools: alpha' in descriptors['beta'].warnings

    _write_tool(tmp_path, 'alpha', [])
    manager.refresh_roots(tool_roots={alpha_root})

    descriptors = {descriptor.name: descriptor for descriptor in manager.list_tools()}
    assert descriptors['beta'].available is True
    assert 'missing required tools' not in descriptors['beta'].warnings
