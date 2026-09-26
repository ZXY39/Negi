from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import g3ku.config.model_manager as model_manager_module
from main.api import admin_rest


def _catalog() -> list[dict[str, object]]:
    return [
        {
            'key': key,
            'providerModel': 'openai:gpt-4.1',
            'apiKey': 'demo-key',
            'apiBase': None,
            'extraHeaders': None,
            'enabled': True,
            'retryOn': [],
            'description': '',
            'contextWindowTokens': 128000,
        }
        for key in ('m_a', 'm_b', 'm_x', 'm_emergency')
    ]


def _write_config(workspace: Path, *, roles: dict[str, object], groups: dict[str, object] | None = None) -> Path:
    config_dir = workspace / '.g3ku'
    config_dir.mkdir(parents=True, exist_ok=True)
    models: dict[str, object] = {'catalog': _catalog(), 'roles': roles}
    if groups is not None:
        models['loadBalanceGroups'] = groups
    path = config_dir / 'config.json'
    path.write_text(
        json.dumps(
            {
                'agents': {
                    'defaults': {
                        'workspace': '.',
                        'maxTokens': 1,
                        'temperature': 0.1,
                        'maxToolIterations': 1,
                        'memoryWindow': 1,
                        'reasoningEffort': 'low',
                    },
                    'roleIterations': {'ceo': 40, 'execution': 16, 'inspection': 16},
                    'multiAgent': {'orchestratorModelKey': None},
                },
                'models': models,
                'providers': {'openai': {'apiKey': '', 'apiBase': None, 'extraHeaders': None}},
                'web': {'host': '127.0.0.1', 'port': 1},
                'toolSecrets': {},
                'resources': {
                    'enabled': True,
                    'skillsDir': 'skills',
                    'toolsDir': 'tools',
                    'manifestName': 'resource.yaml',
                    'reload': {
                        'enabled': True,
                        'pollIntervalMs': 1000,
                        'debounceMs': 400,
                        'lazyReloadOnAccess': True,
                        'keepLastGoodVersion': True,
                    },
                    'locks': {'lockDir': '.g3ku/resource-locks', 'logicalDeleteGuard': True, 'windowsFsLock': True},
                    'statePath': '.g3ku/resources.state.json',
                },
                'mainRuntime': {'enabled': True, 'storePath': '.g3ku/main-runtime/runtime.sqlite3'},
            }
        ),
        encoding='utf-8',
    )
    return path


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    return TestClient(app)


_LEGACY_ROLES = {'ceo': ['m_x'], 'execution': ['m_a', 'm_b'], 'inspection': ['m_b'], 'memory': []}


def test_get_models_exposes_route_entries_and_groups(tmp_path: Path, monkeypatch) -> None:
    groups = {'g_shared': {'enabled': True, 'maxRetryRounds': 2, 'modelKeys': ['m_a', 'm_b']}}
    _write_config(
        tmp_path,
        roles={
            'ceo': ['m_x'],
            'execution': [{'type': 'load_balance', 'groupKey': 'g_shared'}, 'm_emergency'],
            'inspection': ['m_b'],
            'memory': [],
        },
        groups=groups,
    )
    monkeypatch.chdir(tmp_path)

    payload = _client().get('/api/models').json()

    # roles 是候选展开视图：组成员被展开，顺序按声明。
    assert payload['roles']['execution'] == ['m_a', 'm_b', 'm_emergency']
    # route_entries 同时给 snake_case 与 camelCase（JS 客户端两种都读），按语义比对。
    assert [
        {key: value for key, value in row.items() if not key.endswith('_key')}
        for row in payload['route_entries']['execution']
    ] == [
        {'type': 'load_balance', 'groupKey': 'g_shared'},
        {'type': 'model', 'modelKey': 'm_emergency'},
    ]
    assert payload['load_balance_groups']['g_shared']['model_keys'] == ['m_a', 'm_b']
    assert payload['load_balance_groups']['g_shared']['max_retry_rounds'] == 2


def test_saving_route_entries_with_new_group_is_atomic(tmp_path: Path, monkeypatch) -> None:
    path = _write_config(tmp_path, roles=_LEGACY_ROLES)
    monkeypatch.chdir(tmp_path)

    response = _client().put(
        '/api/models/roles/execution',
        json={
            'route_entries': [
                {'type': 'load_balance', 'groupKey': 'g_new'},
                {'type': 'model', 'modelKey': 'm_emergency'},
            ],
            'load_balance_groups': {'g_new': {'modelKeys': ['m_a', 'm_b'], 'maxRetryRounds': 1}},
        },
    )

    assert response.status_code == 200, response.text
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert saved['models']['roles']['execution'] == [
        {'type': 'load_balance', 'groupKey': 'g_new'},
        {'type': 'model', 'modelKey': 'm_emergency'},
    ]
    assert saved['models']['loadBalanceGroups']['g_new']['modelKeys'] == ['m_a', 'm_b']
    # 没出现组的其它链保持旧字符串数组形状。
    assert saved['models']['roles']['ceo'] == ['m_x']


def test_group_created_before_being_dragged_into_a_chain_still_persists(tmp_path: Path, monkeypatch) -> None:
    """模型页的组列允许「先建组、还没拖进链」就保存：链保持扁平，组照样落盘。

    这是配置列与后端的契约点——`load_balance_groups` 是全局资源，不依赖某条链引用它。
    """
    path = _write_config(
        tmp_path,
        roles=_LEGACY_ROLES,
        groups={'g_existing': {'modelKeys': ['m_a'], 'maxRetryRounds': 1}},
    )
    monkeypatch.chdir(tmp_path)

    response = _client().put(
        '/api/models/roles/execution',
        json={
            'model_keys': ['m_a', 'm_b'],
            'load_balance_groups': {
                'g_existing': {'modelKeys': ['m_a'], 'maxRetryRounds': 1},
                'g_staged': {'modelKeys': ['m_a', 'm_b'], 'maxRetryRounds': 2},
            },
        },
    )

    assert response.status_code == 200, response.text
    saved = json.loads(path.read_text(encoding='utf-8'))
    # 链没有被组的出现改成 route 形状：还是旧字符串数组。
    assert saved['models']['roles']['execution'] == ['m_a', 'm_b']
    assert sorted(saved['models']['loadBalanceGroups']) == ['g_existing', 'g_staged']
    assert saved['models']['loadBalanceGroups']['g_staged']['maxRetryRounds'] == 2


def test_bulk_llm_route_save_returns_route_entries_and_groups(tmp_path: Path, monkeypatch) -> None:
    """`PUT /api/llm/routes` 必须带回 route_entries 与组定义。

    模型页保存后直接用响应刷新前端状态；只回 `routes`（候选展开视图）的话，刚保存的组
    在下一次渲染里就变成逐个成员——界面上看是「保存后组卡消失，点刷新才回来」。
    """
    _write_config(tmp_path, roles=_LEGACY_ROLES)
    monkeypatch.chdir(tmp_path)

    response = _client().put(
        '/api/llm/routes',
        json={
            'updates': {
                'execution': {
                    'route_entries': [
                        {'type': 'load_balance', 'groupKey': 'g_bulk'},
                        {'type': 'model', 'modelKey': 'm_emergency'},
                    ],
                    'load_balance_groups': {'g_bulk': {'modelKeys': ['m_a', 'm_b'], 'maxRetryRounds': 2}},
                }
            }
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [
        {key: value for key, value in row.items() if not key.endswith('_key')}
        for row in payload['route_entries']['execution']
    ] == [
        {'type': 'load_balance', 'groupKey': 'g_bulk'},
        {'type': 'model', 'modelKey': 'm_emergency'},
    ]
    # routes 仍是候选展开视图（旧客户端在读），组必须同时可见。
    assert payload['routes']['execution'] == ['m_a', 'm_b', 'm_emergency']
    assert payload['load_balance_groups']['g_bulk']['model_keys'] == ['m_a', 'm_b']
    assert payload['load_balance_groups']['g_bulk']['max_retry_rounds'] == 2


def test_legacy_model_keys_payload_still_saves_flat_shape(tmp_path: Path, monkeypatch) -> None:
    path = _write_config(tmp_path, roles=_LEGACY_ROLES)
    monkeypatch.chdir(tmp_path)

    response = _client().put('/api/models/roles/inspection', json={'model_keys': ['m_a', 'm_b', 'm_a']})

    assert response.status_code == 200, response.text
    saved = json.loads(path.read_text(encoding='utf-8'))
    # legacy 扁平链保持静默去重，不打断旧客户端。
    assert saved['models']['roles']['inspection'] == ['m_a', 'm_b']


def test_explicit_route_entries_reject_duplicates(tmp_path: Path, monkeypatch) -> None:
    _write_config(tmp_path, roles=_LEGACY_ROLES)
    monkeypatch.chdir(tmp_path)

    response = _client().put(
        '/api/models/roles/execution',
        json={'route_entries': [{'type': 'model', 'modelKey': 'm_a'}, {'type': 'model', 'modelKey': 'm_a'}]},
    )

    assert response.status_code == 400
    assert 'Duplicate model route entry' in response.text


def test_group_duplicate_member_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _write_config(tmp_path, roles=_LEGACY_ROLES)
    monkeypatch.chdir(tmp_path)

    response = _client().put(
        '/api/models/roles/execution',
        json={
            'route_entries': [{'type': 'load_balance', 'groupKey': 'g_dup'}],
            'load_balance_groups': {'g_dup': {'modelKeys': ['m_a', 'm_a']}},
        },
    )

    assert response.status_code == 400
    assert 'Duplicate member' in response.text


def test_group_max_retry_rounds_is_bounded(tmp_path: Path, monkeypatch) -> None:
    _write_config(tmp_path, roles=_LEGACY_ROLES)
    monkeypatch.chdir(tmp_path)

    response = _client().put(
        '/api/models/roles/execution',
        json={
            'route_entries': [{'type': 'load_balance', 'groupKey': 'g_rounds'}],
            'load_balance_groups': {'g_rounds': {'modelKeys': ['m_a'], 'maxRetryRounds': 4}},
        },
    )

    assert response.status_code == 400
    assert 'maxRetryRounds' in response.text


def test_ceo_chain_rejects_group_reference(tmp_path: Path, monkeypatch) -> None:
    _write_config(tmp_path, roles=_LEGACY_ROLES, groups={'g_shared': {'modelKeys': ['m_a']}})
    monkeypatch.chdir(tmp_path)

    response = _client().put(
        '/api/models/roles/ceo',
        json={'route_entries': [{'type': 'load_balance', 'groupKey': 'g_shared'}]},
    )

    assert response.status_code == 400
    assert '负载均衡组当前仅支持' in response.text


def test_route_entry_and_legacy_chain_cannot_be_mixed(tmp_path: Path, monkeypatch) -> None:
    _write_config(tmp_path, roles=_LEGACY_ROLES)
    monkeypatch.chdir(tmp_path)

    response = _client().put(
        '/api/models/roles/execution',
        json={'model_keys': ['m_a'], 'route_entries': [{'type': 'model', 'modelKey': 'm_b'}]},
    )

    assert response.status_code == 400
    assert 'route_entries wins' in response.text


def test_bulk_save_rejects_unknown_group_without_partial_update(tmp_path: Path, monkeypatch) -> None:
    path = _write_config(tmp_path, roles=_LEGACY_ROLES)
    monkeypatch.chdir(tmp_path)
    client = _client()

    response = client.put(
        '/api/models/routes/batch',
        json={
            'updates': {
                'execution': {
                    'route_entries': [{'type': 'load_balance', 'groupKey': 'g_ok'}],
                    'load_balance_groups': {'g_ok': {'modelKeys': ['m_a']}},
                },
                'inspection': {'route_entries': [{'type': 'load_balance', 'groupKey': 'g_missing'}]},
            }
        },
    )

    assert response.status_code == 400
    assert 'Unknown load balance group' in response.text
    saved = json.loads(path.read_text(encoding='utf-8'))
    # 校验发生在写盘之前：execution 不能被半提交。
    assert saved['models']['roles']['execution'] == ['m_a', 'm_b']
    assert 'loadBalanceGroups' not in saved['models']


def test_deleting_lone_group_member_reports_actionable_error(tmp_path: Path, monkeypatch) -> None:
    _write_config(
        tmp_path,
        roles={
            'ceo': ['m_x'],
            'execution': [{'type': 'load_balance', 'groupKey': 'g_solo'}],
            'inspection': ['m_b'],
            'memory': [],
        },
        groups={'g_solo': {'modelKeys': ['m_a']}},
    )
    monkeypatch.chdir(tmp_path)
    client = _client()

    response = client.delete('/api/models/m_a')

    assert response.status_code == 400
    assert '最后一个成员' in response.text
    assert 'g_solo' in response.text


def test_rename_model_rewrites_group_members_through_api(tmp_path: Path, monkeypatch) -> None:
    path = _write_config(
        tmp_path,
        roles={
            'ceo': ['m_x'],
            'execution': [{'type': 'load_balance', 'groupKey': 'g_shared'}, 'm_b'],
            'inspection': ['m_b'],
            'memory': [],
        },
        groups={'g_shared': {'modelKeys': ['m_a', 'm_b']}},
    )
    monkeypatch.chdir(tmp_path)

    response = _client().post('/api/llm/bindings/m_a/rename', json={'key': 'm_renamed'})

    assert response.status_code == 200, response.text
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert saved['models']['loadBalanceGroups']['g_shared']['modelKeys'] == ['m_renamed', 'm_b']
    assert saved['models']['roles']['execution'][0] == {'type': 'load_balance', 'groupKey': 'g_shared'}


def test_model_manager_scope_list_is_candidate_view(tmp_path: Path, monkeypatch) -> None:
    # 直接锁定管理面对「这条链用到了哪些模型」的口径：组成员也算。
    _write_config(
        tmp_path,
        roles={
            'ceo': ['m_x'],
            'execution': [{'type': 'load_balance', 'groupKey': 'g_shared'}],
            'inspection': ['m_b'],
            'memory': [],
        },
        groups={'g_shared': {'modelKeys': ['m_a', 'm_b']}},
    )
    monkeypatch.chdir(tmp_path)

    manager = model_manager_module.ModelManager.load()

    assert 'execution' in manager.get_model('m_a')['scopes']
    assert manager.load_balance_groups_payload_view()['g_shared']['model_keys'] == ['m_a', 'm_b']
