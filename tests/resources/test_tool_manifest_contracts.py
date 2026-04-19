from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import yaml

from g3ku.resources.registry import ResourceRegistry
from main.governance.action_mapper import DEFAULT_TOOL_FAMILIES


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / 'tools'


class _DummyService:
    def __init__(self):
        self.content_store = None
        self.artifact_store = None
        self.store = None

    async def startup(self) -> None:
        return None


class _DummyCronService:
    def add_job(self, **kwargs):
        return SimpleNamespace(name='job', id='job:1')

    def list_jobs(self):
        return []

    def remove_job(self, job_id: str):
        return False


class _DummyMemoryManager:
    async def search_tool_view(self, **kwargs):
        return {}

    async def write_explicit_memory_items(self, **kwargs):
        return {"ok": True, "written": [], "deleted": [], "searchable": True}


class _DummyBus:
    async def publish_outbound(self, message) -> None:
        return None


class _DummyLoop:
    _store_enabled = True
    bus = _DummyBus()


def _runtime_stub() -> SimpleNamespace:
    return SimpleNamespace(
        workspace=REPO_ROOT,
        loop=_DummyLoop(),
        services=SimpleNamespace(
            main_task_service=_DummyService(),
            cron_service=_DummyCronService(),
            memory_manager=_DummyMemoryManager(),
        ),
    )


def _load_built_tool(tool_dir: Path):
    module_path = tool_dir / 'main' / 'tool.py'
    spec = importlib.util.spec_from_file_location(f'test_{tool_dir.name}', module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.build(_runtime_stub())


def test_tool_manifests_match_explicit_parameter_contracts():
    for tool_name in [
        'agent_browser',
        'create_async_task_cn',
        'cron',
        'filesystem_copy',
        'filesystem_delete',
        'filesystem_edit',
        'filesystem_move',
        'filesystem_propose_patch',
        'filesystem_write',
        'load_skill_context',
        'load_tool_context',
        'memory_note',
        'memory_write',
        'model_config',
        'task_append_notice_cn',
        'task_delete_cn',
        'task_fetch_cn',
        'task_failed_nodes_cn',
        'task_node_detail_cn',
        'task_progress_cn',
        'task_stats_cn',
        'task_summary_cn',
    ]:
        tool_dir = TOOLS_ROOT / tool_name
        manifest = yaml.safe_load((tool_dir / 'resource.yaml').read_text(encoding='utf-8'))
        built = _load_built_tool(tool_dir)
        schema = built.parameters

        assert manifest['name'] == built.name
        assert set((manifest.get('parameters') or {}).get('properties', {}).keys()) == set((schema.get('properties') or {}).keys())
        assert set((manifest.get('parameters') or {}).get('required', []) or []) == set(schema.get('required') or [])


def test_filesystem_split_mutation_manifests_replace_legacy_monolith():
    assert not (TOOLS_ROOT / 'filesystem').exists()

    for tool_name in [
        'filesystem_write',
        'filesystem_edit',
        'filesystem_copy',
        'filesystem_move',
        'filesystem_delete',
        'filesystem_propose_patch',
    ]:
        manifest_path = TOOLS_ROOT / tool_name / 'resource.yaml'
        assert manifest_path.exists(), f'missing manifest for {tool_name}'

    delete_manifest = yaml.safe_load((TOOLS_ROOT / 'filesystem_delete' / 'resource.yaml').read_text(encoding='utf-8'))
    delete_properties = dict((delete_manifest.get('parameters') or {}).get('properties') or {})
    assert 'paths' in delete_properties
    assert 'path' not in delete_properties

    copy_manifest = yaml.safe_load((TOOLS_ROOT / 'filesystem_copy' / 'resource.yaml').read_text(encoding='utf-8'))
    copy_properties = dict((copy_manifest.get('parameters') or {}).get('properties') or {})
    assert 'operations' in copy_properties

    move_manifest = yaml.safe_load((TOOLS_ROOT / 'filesystem_move' / 'resource.yaml').read_text(encoding='utf-8'))
    move_properties = dict((move_manifest.get('parameters') or {}).get('properties') or {})
    assert 'operations' in move_properties


def test_all_manifest_parameters_have_descriptions():
    for manifest_path in TOOLS_ROOT.glob('*/resource.yaml'):
        manifest = yaml.safe_load(manifest_path.read_text(encoding='utf-8')) or {}
        properties = ((manifest.get('parameters') or {}).get('properties') or {})
        for param_name, payload in properties.items():
            assert isinstance(payload, dict)
            assert str(payload.get('description') or '').strip(), f'{manifest_path.parent.name}.{param_name} is missing description'


def test_task_runtime_resource_tools_are_self_describing_and_not_host_wrapped():
    expected = {
        'create_async_task_cn': ('create_async_task', 'create_async_task'),
        'task_append_notice_cn': ('task_append_notice', 'append_notice_cn'),
        'task_delete_cn': ('task_delete', 'delete_cn'),
        'task_failed_nodes_cn': ('task_failed_nodes', 'failed_nodes_cn'),
        'task_fetch_cn': ('task_list', 'list_cn'),
        'task_node_detail_cn': ('task_node_detail', 'node_detail_cn'),
        'task_progress_cn': ('task_progress', 'progress_cn'),
        'task_stats_cn': ('task_stats', 'stats_cn'),
        'task_summary_cn': ('task_summary', 'summary_cn'),
    }
    for tool_dir_name, (tool_name, action_id) in expected.items():
        manifest = yaml.safe_load((TOOLS_ROOT / tool_dir_name / 'resource.yaml').read_text(encoding='utf-8'))
        governance = manifest.get('governance') or {}
        action_ids = [str(item.get('id') or '') for item in list(governance.get('actions') or [])]

        assert manifest['name'] == tool_name
        assert governance.get('family') == 'task_runtime'
        assert action_id in action_ids

        tool_source = (TOOLS_ROOT / tool_dir_name / 'main' / 'tool.py').read_text(encoding='utf-8')
        assert 'main.service.runtime_service' not in tool_source


def test_task_runtime_resource_tools_do_not_depend_on_host_action_mapper_entries():
    hosted = {
        'create_async_task',
        'task_append_notice',
        'task_delete',
        'task_failed_nodes',
        'task_list',
        'task_node_detail',
        'task_progress',
        'task_stats',
        'task_summary',
    }
    assert hosted.isdisjoint(DEFAULT_TOOL_FAMILIES)


def test_skill_installer_method_description_matches_runtime_strategy():
    manifest = yaml.safe_load((TOOLS_ROOT / 'skill-installer' / 'resource.yaml').read_text(encoding='utf-8'))
    method_description = manifest['parameters']['properties']['method']['description']
    assert '涓' not in method_description
    assert '鍙' not in method_description
    assert 'auto_prefer' in method_description
    assert 'git sparse checkout' in method_description


def test_selected_tool_manifests_do_not_contain_known_mojibake_tokens() -> None:
    manifest_paths = [
        TOOLS_ROOT / 'cron' / 'resource.yaml',
        TOOLS_ROOT / 'model_config' / 'resource.yaml',
        TOOLS_ROOT / 'skill-installer' / 'resource.yaml',
        TOOLS_ROOT / 'web_fetch' / 'resource.yaml',
        TOOLS_ROOT / 'create_async_task_cn' / 'resource.yaml',
        TOOLS_ROOT / 'task_append_notice_cn' / 'resource.yaml',
        TOOLS_ROOT / 'task_stats_cn' / 'resource.yaml',
    ]
    bad_tokens = ('銆', '锛', '€', '缁窇', '鍒涘缓寮傛', '浠诲姟缁熻')

    for manifest_path in manifest_paths:
        text = manifest_path.read_text(encoding='utf-8')
        for token in bad_tokens:
            assert token not in text, f'{manifest_path} still contains mojibake token: {token}'


def test_tool_result_inline_full_flag_defaults_false_and_mirrors_manifest_metadata(tmp_path):
    registry = ResourceRegistry(workspace=tmp_path, skills_dir=tmp_path / 'skills', tools_dir=tmp_path / 'tools')
    tool_root = tmp_path / 'tools' / 'demo_tool'
    main_root = tool_root / 'main'
    main_root.mkdir(parents=True)
    (main_root / 'tool.py').write_text('def build(runtime):\n    return None\n', encoding='utf-8')

    manifest = {
        'schema_version': 1,
        'kind': 'tool',
        'name': 'demo_tool',
        'description': 'Demo tool',
    }
    (tool_root / 'resource.yaml').write_text(yaml.safe_dump(manifest, sort_keys=False), encoding='utf-8')

    descriptor = registry.build_tool_descriptor(tool_root)

    assert descriptor is not None
    assert descriptor.tool_result_inline_full is False
    assert descriptor.metadata['tool_result_inline_full'] is False

    manifest['tool_result_inline_full'] = True
    (tool_root / 'resource.yaml').write_text(yaml.safe_dump(manifest, sort_keys=False), encoding='utf-8')

    descriptor = registry.build_tool_descriptor(tool_root)

    assert descriptor is not None
    assert descriptor.tool_result_inline_full is True
    assert descriptor.metadata['tool_result_inline_full'] is True


def test_callable_tool_requires_explicit_result_delivery_contract_when_not_inline_full(tmp_path):
    registry = ResourceRegistry(workspace=tmp_path, skills_dir=tmp_path / 'skills', tools_dir=tmp_path / 'tools')
    tool_root = tmp_path / 'tools' / 'demo_tool'
    main_root = tool_root / 'main'
    main_root.mkdir(parents=True)
    (main_root / 'tool.py').write_text('def build(runtime):\n    return None\n', encoding='utf-8')

    manifest = {
        'schema_version': 1,
        'kind': 'tool',
        'name': 'demo_tool',
        'description': 'Demo tool',
    }
    (tool_root / 'resource.yaml').write_text(yaml.safe_dump(manifest, sort_keys=False), encoding='utf-8')

    descriptor = registry.build_tool_descriptor(tool_root)

    assert descriptor is not None
    assert descriptor.available is False
    assert any('tool_result_delivery_contract' in str(item or '') for item in descriptor.errors)

    manifest['tool_result_delivery_contract'] = 'runtime_managed'
    (tool_root / 'resource.yaml').write_text(yaml.safe_dump(manifest, sort_keys=False), encoding='utf-8')

    descriptor = registry.build_tool_descriptor(tool_root)

    assert descriptor is not None
    assert descriptor.available is True
    assert descriptor.metadata['tool_result_delivery_contract'] == 'runtime_managed'


def test_preview_with_ref_contract_requires_output_ref_paths(tmp_path):
    registry = ResourceRegistry(workspace=tmp_path, skills_dir=tmp_path / 'skills', tools_dir=tmp_path / 'tools')
    tool_root = tmp_path / 'tools' / 'demo_tool'
    main_root = tool_root / 'main'
    main_root.mkdir(parents=True)
    (main_root / 'tool.py').write_text('def build(runtime):\n    return None\n', encoding='utf-8')

    manifest = {
        'schema_version': 1,
        'kind': 'tool',
        'name': 'demo_tool',
        'description': 'Demo tool',
        'tool_result_delivery_contract': 'preview_with_ref',
    }
    (tool_root / 'resource.yaml').write_text(yaml.safe_dump(manifest, sort_keys=False), encoding='utf-8')

    descriptor = registry.build_tool_descriptor(tool_root)

    assert descriptor is not None
    assert descriptor.available is False
    assert any('tool_result_output_ref_paths' in str(item or '') for item in descriptor.errors)

    manifest['tool_result_output_ref_paths'] = ['output_ref']
    (tool_root / 'resource.yaml').write_text(yaml.safe_dump(manifest, sort_keys=False), encoding='utf-8')

    descriptor = registry.build_tool_descriptor(tool_root)

    assert descriptor is not None
    assert descriptor.available is True
    assert descriptor.metadata['tool_result_output_ref_paths'] == ['output_ref']
