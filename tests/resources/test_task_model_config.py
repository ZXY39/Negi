"""任务级模型链覆盖（任务详情「模型配置」）写侧契约。

覆盖：entry 归一（与全局 route entry 同形、读侧容忍畸形）；PATCH 校验档位
（模型 404/409、组 404、禁用组可存、形状 400）；空数组恢复跟随、缺席车道不动；
metadata 其他键不被破坏；任何写入都不触碰全局配置（不反向同步）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main.api.rest as rest_module
import main.service.runtime_service as runtime_service_module
from g3ku.config.schema import Config
from main.models import (
    TASK_MODEL_CONFIG_EXECUTION_ENTRIES_KEY,
    TASK_MODEL_CONFIG_INSPECTION_ENTRIES_KEY,
    TASK_MODEL_CONFIG_KEY,
    TaskRecord,
    normalize_task_model_config,
    normalize_task_model_route_entries,
)
from main.service.runtime_service import MainRuntimeService


def _config_payload() -> dict[str, Any]:
    return {
        'agents': {'defaults': {'workspace': '.'}},
        'models': {
            'catalog': [
                {'key': 'm_a', 'llmConfigId': 'rec_a', 'enabled': True, 'contextWindowTokens': 200000},
                {'key': 'm_b', 'llmConfigId': 'rec_b', 'enabled': True, 'contextWindowTokens': 128000},
                {'key': 'm_off', 'llmConfigId': 'rec_off', 'enabled': False, 'contextWindowTokens': 200000},
            ],
            'loadBalanceGroups': {
                'g_shared': {'enabled': True, 'maxRetryRounds': 2, 'modelKeys': ['m_a', 'm_b']},
                'g_off': {'enabled': False, 'modelKeys': ['m_a']},
            },
            'roles': {
                'ceo': ['m_a'],
                'execution': ['m_a'],
                'inspection': [],
                'memory': [],
            },
        },
    }


class _DummyChatBackend:
    async def chat(self, **kwargs):
        return SimpleNamespace(content='', tool_calls=[], finish_reason='stop', usage={})


def _make_service(tmp_path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )


def _task(task_id: str = 'task:model-config', metadata: dict | None = None) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id='web:demo',
        title='模型配置测试',
        user_request='请帮我做一件事',
        status='in_progress',
        root_node_id='node-root',
        created_at='2026-11-12T10:00:00+08:00',
        updated_at='2026-11-12T10:00:00+08:00',
        metadata=dict(metadata or {}),
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cfg = Config.model_validate(_config_payload())
    service = _make_service(tmp_path)
    service.store.upsert_task(_task(metadata={
        'final_acceptance': {'required': True, 'prompt': '验收标准'},
        'custom_key': 'keep-me',
    }))
    monkeypatch.setattr(runtime_service_module, 'get_runtime_config', lambda *a, **k: (cfg, 1, False))

    async def _startup():
        return None

    monkeypatch.setattr(service, 'startup', _startup)
    monkeypatch.setattr(rest_module, '_service', lambda: service)
    app = FastAPI()
    app.include_router(rest_module.router, prefix='/api')
    return SimpleNamespace(client=TestClient(app), service=service, cfg=cfg, task_id='task:model-config')


def test_normalize_entries_keeps_order_and_tolerates_malformed() -> None:
    raw = [
        {'type': 'model', 'modelKey': 'm_a'},
        {'type': 'load_balance', 'groupKey': 'g_shared'},
        {'type': 'model'},
        {'type': 'load_balance', 'group_key': ''},
        'not-a-dict',
        {'type': 'model', 'model_key': 'm_b'},
    ]
    assert normalize_task_model_route_entries(raw) == [
        {'type': 'model', 'model_key': 'm_a'},
        {'type': 'load_balance', 'group_key': 'g_shared'},
        {'type': 'model', 'model_key': 'm_b'},
    ]
    assert normalize_task_model_route_entries(None) == []
    config = normalize_task_model_config({
        'executionRouteEntries': raw,
        'inspection_route_entries': [{'type': 'model', 'model_key': 'm_b'}],
    })
    assert config[TASK_MODEL_CONFIG_EXECUTION_ENTRIES_KEY][0] == {'type': 'model', 'model_key': 'm_a'}
    assert config[TASK_MODEL_CONFIG_INSPECTION_ENTRIES_KEY] == [{'type': 'model', 'model_key': 'm_b'}]
    assert normalize_task_model_config(None) == {
        TASK_MODEL_CONFIG_EXECUTION_ENTRIES_KEY: [],
        TASK_MODEL_CONFIG_INSPECTION_ENTRIES_KEY: [],
    }


def test_get_default_follows_global(env) -> None:
    response = env.client.get(f'/api/tasks/{env.task_id}/model-config')
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['execution'] == {'follows_global': True, 'route_entries': []}
    assert payload['inspection'] == {'follows_global': True, 'route_entries': []}
    # 默认态不落 metadata 键。
    assert TASK_MODEL_CONFIG_KEY not in (env.service.store.get_task(env.task_id).metadata or {})


def test_patch_writes_override_and_preserves_other_metadata(env) -> None:
    config_before = env.cfg.model_dump_json()
    entries = [
        {'type': 'load_balance', 'group_key': 'g_shared'},
        {'type': 'model', 'model_key': 'm_b'},
    ]
    response = env.client.patch(
        f'/api/tasks/{env.task_id}/model-config',
        json={'execution_route_entries': entries},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['execution']['follows_global'] is False
    assert payload['execution']['route_entries'] == entries
    # 缺席的车道不动，仍跟随全局。
    assert payload['inspection']['follows_global'] is True
    task = env.service.store.get_task(env.task_id)
    assert task.metadata['custom_key'] == 'keep-me'
    assert task.metadata['final_acceptance']['prompt'] == '验收标准'
    assert task.metadata[TASK_MODEL_CONFIG_KEY][TASK_MODEL_CONFIG_EXECUTION_ENTRIES_KEY] == entries
    # 不反向同步：全局配置字节不变。
    assert env.cfg.model_dump_json() == config_before


def test_patch_camel_case_alias_and_both_lanes(env) -> None:
    response = env.client.patch(
        f'/api/tasks/{env.task_id}/model-config',
        json={
            'executionRouteEntries': [{'type': 'model', 'modelKey': 'm_b'}],
            'inspectionRouteEntries': [{'type': 'load_balance', 'groupKey': 'g_shared'}],
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['execution']['route_entries'] == [{'type': 'model', 'model_key': 'm_b'}]
    assert payload['inspection']['route_entries'] == [{'type': 'load_balance', 'group_key': 'g_shared'}]


def test_empty_array_restores_follow_global_and_absent_lane_untouched(env) -> None:
    env.client.patch(
        f'/api/tasks/{env.task_id}/model-config',
        json={
            'execution_route_entries': [{'type': 'model', 'model_key': 'm_b'}],
            'inspection_route_entries': [{'type': 'model', 'model_key': 'm_a'}],
        },
    )
    # 只清执行：检验覆盖保留。
    response = env.client.patch(
        f'/api/tasks/{env.task_id}/model-config',
        json={'execution_route_entries': []},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['execution']['follows_global'] is True
    assert payload['inspection']['follows_global'] is False
    # 两车道都清：整键摘掉。
    response = env.client.patch(
        f'/api/tasks/{env.task_id}/model-config',
        json={'inspection_route_entries': []},
    )
    assert response.status_code == 200, response.text
    assert response.json()['inspection']['follows_global'] is True
    assert TASK_MODEL_CONFIG_KEY not in (env.service.store.get_task(env.task_id).metadata or {})


def test_disabled_group_is_allowed_like_global(env) -> None:
    response = env.client.patch(
        f'/api/tasks/{env.task_id}/model-config',
        json={'execution_route_entries': [{'type': 'load_balance', 'group_key': 'g_off'}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()['execution']['route_entries'] == [{'type': 'load_balance', 'group_key': 'g_off'}]


@pytest.mark.parametrize('entries,status,code', [
    ([{'type': 'model', 'model_key': 'missing'}], 404, 'model_key_not_found'),
    ([{'type': 'model', 'model_key': 'm_off'}], 409, 'model_key_disabled'),
    ([{'type': 'load_balance', 'group_key': 'missing'}], 404, 'group_key_not_found'),
    ([{'type': 'model', 'model_key': 'm_a', 'group_key': 'g_shared'}], 400, 'invalid_route_entry'),
    ([{'type': 'load_balance', 'group_key': 'g_shared', 'model_key': 'm_a'}], 400, 'invalid_route_entry'),
    ([{'type': 'load_balance'}], 400, 'invalid_route_entry'),
    ([{'type': 'model'}], 400, 'invalid_route_entry'),
    ([{'type': 'mystery', 'model_key': 'm_a'}], 400, 'invalid_route_entry'),
    (['not-a-dict'], 400, 'invalid_route_entry'),
    ('not-a-list', 400, 'invalid_route_entries'),
])
def test_patch_validation_errors(env, entries, status, code) -> None:
    config_before = env.cfg.model_dump_json()
    response = env.client.patch(
        f'/api/tasks/{env.task_id}/model-config',
        json={'execution_route_entries': entries},
    )
    assert response.status_code == status, response.text
    detail = response.json()['detail']
    assert detail['code'] == code
    assert detail['message']
    # 校验失败不落任何写入，也不触碰全局。
    assert TASK_MODEL_CONFIG_KEY not in (env.service.store.get_task(env.task_id).metadata or {})
    assert env.cfg.model_dump_json() == config_before


def test_unknown_task_404(env) -> None:
    assert env.client.get('/api/tasks/task:missing/model-config').status_code == 404
    assert env.client.patch(
        '/api/tasks/task:missing/model-config',
        json={'execution_route_entries': []},
    ).status_code == 404
    assert env.service.get_task_model_config_payload('task:missing') is None
    assert env.service.update_task_model_config('task:missing', execution_route_entries=[]) is None


def test_service_validation_raises_typed_error(env) -> None:
    from main.errors import TaskModelConfigError

    with pytest.raises(TaskModelConfigError) as exc:
        env.service.update_task_model_config(
            env.task_id,
            execution_route_entries=[{'type': 'model', 'model_key': 'missing'}],
        )
    assert exc.value.code == 'model_key_not_found'
    assert exc.value.model_key == 'missing'
