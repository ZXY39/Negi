from __future__ import annotations

from pathlib import Path

from g3ku.resources.registry import ResourceRegistry


def _write_tool(root: Path, name: str, requires: list[str]) -> None:
    tool_dir = root / 'tools' / name
    (tool_dir / 'main').mkdir(parents=True, exist_ok=True)
    (tool_dir / 'main' / 'tool.py').write_text('', encoding='utf-8')
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
