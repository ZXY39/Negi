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


def _write_tool(workspace: Path, name: str, description: str, *, handler_field: str = 'description') -> Path:
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
    # `description` 是抽象成员，必须存在；`handler_field` 决定哪一层写那句超限文本，
    # 用来钉拒绝文案点的属性名跟的是"这个类真正写的那一层"。
    short_property = (
        ''
        if handler_field == 'description'
        else '    @property\n'
        '    def description(self):\n'
        "        return 'Short handler description.'\n"
    )
    (root / 'main' / 'tool.py').write_text(
        'from g3ku.agent.tools.base import Tool\n'
        '\n'
        'class TestTool(Tool):\n'
        '    @property\n'
        '    def name(self):\n'
        "        return %r\n"
        f'{short_property}'
        '    @property\n'
        f'    def {handler_field}(self):\n'
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


def test_rejected_tool_description_names_its_carrier_and_quotes_the_text(tmp_path: Path) -> None:
    """拒绝文案必须自己回答"量的是哪一份、原文是什么"。

    实盘（`ext:qq-official-1903529517:f8a8001865631301`，2026-11-01 22:19–22:44）：装载路径量的是
    handler 类里的 `description`（那份 317 字符），而 repair 态印进 `tools[]` 的【待修复】句子取的是
    `resource.yaml` 的 `description:`。同一个"工具描述"两个读者两套来源，模型看到的只有后者，
    于是它把 yaml 从 112 改到 93、每一跳都被同一个 317 拒掉，最后只能向操作员要"重启运行时"。
    原文案只报字符数与"rewrite it as one sentence"，没有指明被量的载体，模型没有任何线索能找到那
    317 个字符——所以这里钉三件事：点名 handler 文件与属性、引到有界的原文、并把清单那一份明确
    标成"不是它"。
    """
    manager = ResourceManager(tmp_path)
    try:
        offending = 'CARRIERPROBE ' * 30
        assert len(offending) > TOOL_DESCRIPTION_MAX_CHARS
        _write_tool(tmp_path, 'carrier_tool', offending)
        manager.reload_now()
        descriptor = manager.get_tool_descriptor('carrier_tool')
        assert descriptor is not None
        assert descriptor.available is False
        error = next(item for item in descriptor.errors if 'description_too_long' in item)
        assert 'tools/carrier_tool/main/tool.py:description' in error
        assert 'CARRIERPROBE' in error
        # 被拒文本必然超上限，全文回贴会让这条拒绝自己变成一个新的长块
        assert '...(truncated)' in error
        # 点名"不是清单那一份"才是这一刀的落点：模型上一轮改的正是 resource.yaml
        assert 'not tools/carrier_tool/resource.yaml' in error
    finally:
        manager.close()


def test_shortening_a_rejected_tool_makes_it_available(tmp_path: Path) -> None:
    """拒绝回执承诺的"修好后下一次装载就放行"必须有实现兜着。

    与 skill 侧同名用例同形：工具档过去没有正样本，回执里那句"next resource reload re-opens it"
    就是空头承诺。
    """
    manager = ResourceManager(tmp_path)
    try:
        root = _write_tool(tmp_path, 'fix_tool', 'x' * (TOOL_DESCRIPTION_MAX_CHARS + 1))
        manager.reload_now()
        assert manager.get_tool_descriptor('fix_tool').available is False
        (root / 'main' / 'tool.py').write_text(
            (root / 'main' / 'tool.py').read_text(encoding='utf-8').replace(
                'x' * (TOOL_DESCRIPTION_MAX_CHARS + 1), 'One short sentence.'
            ),
            encoding='utf-8',
        )
        manager.reload_now()
        assert manager.get_tool_descriptor('fix_tool').available is True
    finally:
        manager.close()


def test_rejected_tool_description_names_the_overridden_model_description(tmp_path: Path) -> None:
    """属性名跟着这个类真正写的那一层走。

    装载包装类问的是 `handler.model_description`，基类默认转读 `description`。两类写法都能触发
    超限，但只有覆盖 `model_description` 的那一类，让模型去 grep `model_description` 才找得到；
    上一条用例钉的是默认转读那一档，这里钉覆盖那一档，两条互为对照臂。
    """
    manager = ResourceManager(tmp_path)
    try:
        _write_tool(
            tmp_path,
            'model_field_tool',
            'MODELFIELDCAR ' * 20,
            handler_field='model_description',
        )
        manager.reload_now()
        descriptor = manager.get_tool_descriptor('model_field_tool')
        assert descriptor is not None
        assert descriptor.available is False
        error = next(item for item in descriptor.errors if 'description_too_long' in item)
        assert 'tools/model_field_tool/main/tool.py:model_description' in error
        assert 'MODELFIELDCAR' in error
    finally:
        manager.close()


def test_rejected_skill_description_names_the_manifest_as_its_carrier(tmp_path: Path) -> None:
    """载体标签跟着实际胜出的那一份走，不是硬编码 tool.py。

    skill 侧没有 handler 覆盖：量的是 `skills/<name>/resource.yaml` 的 `description:`，
    所以这一档必须点名清单文件。它是上一条用例的对照臂——把载体判据写死成"永远是主模块"
    会在这里当场红。
    """
    manager = ResourceManager(tmp_path)
    try:
        offending = 'SKILLCARRIER ' * 20
        assert len(offending) > SKILL_DESCRIPTION_MAX_CHARS
        _write_skill(tmp_path, 'carrier_skill', offending)
        manager.reload_now()
        descriptor = manager.get_skill('carrier_skill')
        assert descriptor is not None
        assert descriptor.available is False
        error = next(item for item in descriptor.errors if 'description_too_long' in item)
        assert 'skills/carrier_skill/resource.yaml' in error
        assert 'SKILLCARRIER' in error
    finally:
        manager.close()


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
