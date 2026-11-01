from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from g3ku.resources.description_limits import (
    SKILL_DESCRIPTION_MAX_CHARS,
    TOOL_DESCRIPTION_MAX_CHARS,
)
from g3ku.resources.manager import ResourceManager


def _write_skill(workspace: Path, name: str, description: str) -> Path:
    root = workspace / 'skills' / name
    root.mkdir(parents=True)
    (root / 'resource.yaml').write_text(
        yaml.safe_dump(
            {
                'schema_version': 1,
                'kind': 'skill',
                'name': name,
                'description': description,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    (root / 'SKILL.md').write_text('# test skill\n', encoding='utf-8')
    return root


def _write_tool(workspace: Path, name: str, description: str) -> Path:
    root = workspace / 'tools' / name
    (root / 'main').mkdir(parents=True)
    (root / 'resource.yaml').write_text(
        yaml.safe_dump(
            {
                'schema_version': 1,
                'kind': 'tool',
                'name': name,
                'description': 'Short manifest description.',
                'protocol': 'mcp',
                'mcp': {'transport': 'embedded'},
                'tool_result_delivery_contract': 'runtime_managed',
                'parameters': {
                    'type': 'object',
                    'properties': {'x': {'type': 'string'}},
                },
                'governance': {
                    'display_name': 'Long Tool',
                    'description': 'test tool',
                    'allowed_roles': ['ceo'],
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    (root / 'main' / 'tool.py').write_text(
        'from g3ku.agent.tools.base import Tool\n'
        '\n'
        'class TestTool(Tool):\n'
        '    @property\n'
        '    def name(self):\n'
        "        return %r\n"
        '    @property\n'
        '    def description(self):\n'
        "        return %r\n"
        '    @property\n'
        '    def parameters(self):\n'
        "        return {'type': 'object', 'properties': {'x': {'type': 'string'}}}\n"
        '    async def execute(self, **kwargs):\n'
        "        return 'ok'\n"
        '\n'
        'def build(runtime):\n'
        '    return TestTool()\n' % (name, description),
        encoding='utf-8',
    )
    return root


def test_existing_over_cap_skill_is_grandfathered(tmp_path: Path) -> None:
    _write_skill(tmp_path, 'legacy_skill', 'x' * (SKILL_DESCRIPTION_MAX_CHARS + 1))
    manager = ResourceManager(tmp_path)
    try:
        descriptor = manager.get_skill('legacy_skill')
        assert descriptor is not None
        assert descriptor.available is True
    finally:
        manager.close()


def test_new_over_cap_skill_is_repair_required(tmp_path: Path) -> None:
    manager = ResourceManager(tmp_path)
    try:
        _write_skill(tmp_path, 'new_skill', 'x' * (SKILL_DESCRIPTION_MAX_CHARS + 1))
        manager.reload_now()
        descriptor = manager.get_skill('new_skill')
        assert descriptor is not None
        assert descriptor.available is False
        assert any('description_too_long' in error for error in descriptor.errors)
    finally:
        manager.close()


def test_new_over_cap_tool_is_repair_required(tmp_path: Path) -> None:
    manager = ResourceManager(tmp_path)
    try:
        _write_tool(tmp_path, 'long_tool', 'x' * (TOOL_DESCRIPTION_MAX_CHARS + 1))
        manager.reload_now()
        descriptor = manager.get_tool_descriptor('long_tool')
        assert descriptor is not None
        assert descriptor.available is False
        assert any('description_too_long' in error for error in descriptor.errors)
        instance = manager.get_tool('long_tool')
        assert type(instance).__name__ == 'RepairRequiredTool'
        from main.governance.resource_bridge import build_tool_records

        record = next(r for r in build_tool_records(manager.list_tools()) if r.tool_id == 'long_tool')
        assert record.available is False
        assert any('description_too_long' in error for error in record.metadata['errors'])
    finally:
        manager.close()


def test_over_cap_rejection_survives_a_manager_restart(tmp_path: Path) -> None:
    manager = ResourceManager(tmp_path)
    try:
        _write_skill(tmp_path, 'rejected_skill', 'x' * (SKILL_DESCRIPTION_MAX_CHARS + 1))
        manager.reload_now()
        assert manager.get_skill('rejected_skill').available is False
    finally:
        manager.close()

    restarted = ResourceManager(tmp_path)
    try:
        descriptor = restarted.get_skill('rejected_skill')
        assert descriptor is not None
        assert descriptor.available is False
        assert any('description_too_long' in error for error in descriptor.errors)
    finally:
        restarted.close()


def test_shortening_a_rejected_skill_makes_it_available(tmp_path: Path) -> None:
    manager = ResourceManager(tmp_path)
    try:
        root = _write_skill(tmp_path, 'fix_me', 'x' * (SKILL_DESCRIPTION_MAX_CHARS + 1))
        manager.reload_now()
        assert manager.get_skill('fix_me').available is False
        (root / 'resource.yaml').write_text(
            yaml.safe_dump(
                {
                    'schema_version': 1,
                    'kind': 'skill',
                    'name': 'fix_me',
                    'description': 'One short sentence.',
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding='utf-8',
        )
        manager.reload_now()
        assert manager.get_skill('fix_me').available is True
    finally:
        manager.close()


def test_growing_a_grandfathered_over_cap_skill_is_rejected(tmp_path: Path) -> None:
    root = _write_skill(tmp_path, 'legacy_grow', 'x' * (SKILL_DESCRIPTION_MAX_CHARS + 40))
    manager = ResourceManager(tmp_path)
    try:
        assert manager.get_skill('legacy_grow').available is True
        (root / 'resource.yaml').write_text(
            yaml.safe_dump(
                {
                    'schema_version': 1,
                    'kind': 'skill',
                    'name': 'legacy_grow',
                    'description': 'x' * (SKILL_DESCRIPTION_MAX_CHARS + 41),
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding='utf-8',
        )
        manager.reload_now()
        descriptor = manager.get_skill('legacy_grow')
        assert descriptor is not None
        assert descriptor.available is False
        assert any('description_too_long' in error for error in descriptor.errors)
    finally:
        manager.close()


def test_installer_rejects_an_over_cap_generated_description(tmp_path: Path) -> None:
    import importlib.util

    tool_path = Path(__file__).resolve().parents[2] / 'tools' / 'skill-installer' / 'main' / 'tool.py'
    spec = importlib.util.spec_from_file_location('skill_installer_description_budget_test', tool_path)
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    skill_root = tmp_path / 'upstream'
    skill_root.mkdir()
    (skill_root / 'SKILL.md').write_text(
        '---\nname: long_skill\ndescription: '
        + ('x' * (SKILL_DESCRIPTION_MAX_CHARS + 1))
        + '\n---\n\n# body\n',
        encoding='utf-8',
    )
    with pytest.raises(installer.DescriptionTooLongError) as excinfo:
        installer.SkillInstallerTool(workspace=tmp_path)._ensure_resource_manifest(
            skill_root=skill_root,
            requested_name='long_skill',
            source=installer.GitHubSource(
                owner='example',
                repo='example',
                ref='main',
                path='skills/long_skill',
                url='https://github.com/example/example',
            ),
        )
    assert 'description_too_long' in str(excinfo.value)
    assert not (skill_root / 'resource.yaml').exists()


def test_repo_tool_manifests_fit_the_description_budget() -> None:
    root = Path(__file__).resolve().parents[2] / 'tools'
    over = []
    for manifest_path in sorted(root.glob('*/resource.yaml')):
        payload = yaml.safe_load(manifest_path.read_text(encoding='utf-8')) or {}
        description = str(payload.get('description') or '').strip()
        if len(description) > TOOL_DESCRIPTION_MAX_CHARS:
            over.append((manifest_path.parent.name, len(description)))
    assert over == [], f'tool descriptions over {TOOL_DESCRIPTION_MAX_CHARS}: {over}'


def test_synthetic_resident_control_tools_sit_outside_the_budget_lane() -> None:
    """这道闸只打在 manager 的装载路径上：合成常驻控制工具根本不经过它。

    分界线不是"要不要预热/算不算内置"，是有没有 `tools/<name>/resource.yaml`——
    `create_async_task` / `task_summary` 免预热，却是资源包，照旧受限（上一条用例管它们）。
    这几颗没有 toolskill 正文可搬，函数级描述就是唯一的模型可见载体，所以允许写到
    需要那么长；判据正文仍只许住提示词，那条分工由 `test_acceptance_gating_contract.py` 钉。

    正样本（防这条用例白过）：把闸接到统一入口上时，`silent`(500) 与 `time`(169) 的
    实际读数会当场被判 `description_too_long`、`Resource was not added`，症状不是报错而是
    静默/时钟/闹钟这几项能力从请求面上消失。所以这里同时钉三件事：名单里的名字确实没有
    资源包、确实存在超预算的描述、前门就地装进来的实例仍是各自的工具类。
    """
    from main.runtime.tool_names import SYNTHETIC_RESIDENT_TOOL_NAMES

    root = Path(__file__).resolve().parents[2] / 'tools'
    pack_names = set()
    for manifest_path in sorted(root.glob('*/resource.yaml')):
        payload = yaml.safe_load(manifest_path.read_text(encoding='utf-8')) or {}
        pack_names.add(str(payload.get('name') or manifest_path.parent.name).strip())
    assert not set(SYNTHETIC_RESIDENT_TOOL_NAMES) & pack_names, (
        '常驻控制工具一旦有了 resource.yaml，它就同时受字数预算；这条用例的前提失效，'
        '要先决定该名字走哪条道，再改这里。'
    )

    from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps

    runtime = SimpleNamespace(context=SimpleNamespace(session=SimpleNamespace(_frontdoor_turns=[])))
    resident = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)._frontdoor_resident_tools(
        state={'frontdoor_turn_id': 'turn:test', 'analysis_text': ''},
        runtime=runtime,
    )
    assert set(resident) == set(SYNTHETIC_RESIDENT_TOOL_NAMES)

    lengths = {name: len(str(tool.model_description or '').encode('utf-8')) for name, tool in resident.items()}
    over = {name: size for name, size in lengths.items() if size > TOOL_DESCRIPTION_MAX_CHARS}
    assert {'silent', 'time'} <= set(over), f'正样本消失（描述都被压短了？）: {lengths}'
    for name, tool in resident.items():
        assert type(tool).__name__ != 'RepairRequiredTool', f'{name} 被闸挡在门外: {lengths[name]} B'
