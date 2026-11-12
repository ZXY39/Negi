"""任务级模型链覆盖的运行时解析契约。

覆盖：entry 解析器与全局 builder 同口径（组展开、禁用降级、回滚闸门）；
组注册面扩为整份配置（全局链未引用的组也能准入）；resolver 的车道独立、
D5 继承、失效回退、缓存键控；node_runner 选择点接入。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from g3ku.config.schema import Config
from main.models import (
    TASK_MODEL_CONFIG_EXECUTION_ENTRIES_KEY,
    TASK_MODEL_CONFIG_INSPECTION_ENTRIES_KEY,
    TASK_MODEL_CONFIG_KEY,
    TaskRecord,
)
from main.runtime.model_key_concurrency import ModelKeyConcurrencyController
from main.runtime.model_load_balancer import ModelLoadBalancer
from main.runtime.model_route import (
    build_model_route_plan,
    build_model_route_plan_from_entries,
    resolve_config_load_balance_groups,
)
from main.service.runtime_service import MainRuntimeService


def _payload(inspection: list | None = None) -> dict[str, Any]:
    return {
        'agents': {'defaults': {'workspace': '.'}},
        'models': {
            'catalog': [
                {'key': 'm_a', 'llmConfigId': 'rec_a', 'enabled': True, 'contextWindowTokens': 200000},
                {'key': 'm_b', 'llmConfigId': 'rec_b', 'enabled': True, 'contextWindowTokens': 128000},
                {'key': 'm_off', 'llmConfigId': 'rec_off', 'enabled': False, 'contextWindowTokens': 200000},
                {'key': 'm_x', 'llmConfigId': 'rec_x', 'enabled': True, 'contextWindowTokens': 200000},
            ],
            'loadBalanceGroups': {
                'g_shared': {'enabled': True, 'maxRetryRounds': 2, 'modelKeys': ['m_a', 'm_b']},
                # 没有任何全局链引用它：任务覆盖链引用它时准入仍须授予。
                'g_unused': {'enabled': True, 'maxRetryRounds': 1, 'modelKeys': ['m_b', 'm_x']},
                'g_off': {'enabled': False, 'modelKeys': ['m_a']},
            },
            'roles': {
                'ceo': ['m_x'],
                'execution': ['m_a'],
                'inspection': inspection if inspection is not None else [],
                'memory': [],
            },
        },
    }


class _DummyChatBackend:
    async def chat(self, **kwargs):
        return SimpleNamespace(content='', tool_calls=[], finish_reason='stop', usage={})


def _service(tmp_path, cfg: Config) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        app_config=cfg,
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    service._runtime_model_revision = 1
    return service


def _task(task_id: str, execution: list | None = None, inspection: list | None = None) -> TaskRecord:
    config: dict[str, Any] = {}
    if execution is not None:
        config[TASK_MODEL_CONFIG_EXECUTION_ENTRIES_KEY] = execution
    if inspection is not None:
        config[TASK_MODEL_CONFIG_INSPECTION_ENTRIES_KEY] = inspection
    return TaskRecord(
        task_id=task_id,
        session_id='web:demo',
        title='覆盖测试',
        user_request='请帮我做一件事',
        status='in_progress',
        root_node_id='node-root',
        created_at='2026-11-12T10:00:00+08:00',
        updated_at='2026-11-12T10:00:00+08:00',
        metadata={TASK_MODEL_CONFIG_KEY: config} if config else {},
    )


def test_from_entries_matches_global_builder() -> None:
    cfg = Config.model_validate(_payload(inspection=['m_x']))
    cfg_dict = _payload(inspection=['m_x'])
    cfg_dict['models']['roles']['execution'] = [
        {'type': 'load_balance', 'groupKey': 'g_shared'},
        {'type': 'model', 'modelKey': 'm_a'},
    ]
    global_plan = build_model_route_plan(Config.model_validate(cfg_dict), 'execution', revision=5)
    override_plan = build_model_route_plan_from_entries(
        cfg,
        [{'type': 'load_balance', 'group_key': 'g_shared'}, {'type': 'model', 'model_key': 'm_a'}],
        revision=5,
    )
    assert override_plan.signature() == global_plan.signature()
    assert override_plan.candidate_model_keys == global_plan.candidate_model_keys
    assert override_plan.config_revision == 5


def test_from_entries_degrades_invalid_members_and_groups() -> None:
    cfg = Config.model_validate(_payload())
    plan = build_model_route_plan_from_entries(
        cfg,
        [
            {'type': 'load_balance', 'group_key': 'g_deleted'},
            {'type': 'model', 'model_key': 'm_off'},
            {'type': 'load_balance', 'group_key': 'g_off'},
            {'type': 'model', 'model_key': 'm_b'},
        ],
    )
    # 被删组与禁用 direct 跳过；禁用组槽位保留但候选为空（与全局同口径）。
    assert [route.kind for route in plan.routes] == ['load_balance', 'model']
    assert plan.routes[0].group_key == 'g_off'
    assert plan.routes[0].candidates == ()
    assert plan.routes[1].model_key == 'm_b'
    assert build_model_route_plan_from_entries(cfg, [{'type': 'model', 'model_key': 'm_off'}]).routes == []


def test_from_entries_rollback_gate_flattens_groups() -> None:
    payload = _payload()
    payload['mainRuntime'] = {'enabled': True, 'modelRouteLoadBalanceEnabled': False}
    cfg = Config.model_validate(payload)
    plan = build_model_route_plan_from_entries(
        cfg,
        [{'type': 'load_balance', 'group_key': 'g_off'}, {'type': 'load_balance', 'group_key': 'g_shared'}],
    )
    # 闸门关闭时禁用组的启用成员也摊平成 direct（与全局 builder 行为逐字一致）。
    assert [route.kind for route in plan.routes] == ['model', 'model', 'model']
    assert [route.model_key for route in plan.routes] == ['m_a', 'm_a', 'm_b']
    assert plan.load_balance_group_keys == []


def test_resolve_config_groups_covers_unreferenced_and_disabled() -> None:
    cfg = Config.model_validate(_payload())
    groups = resolve_config_load_balance_groups(cfg)
    assert set(groups) == {'g_shared', 'g_unused', 'g_off'}
    assert [member.model_key for member in groups['g_unused'].members] == ['m_b', 'm_x']
    assert groups['g_off'].enabled is False
    assert groups['g_off'].members == []


def test_registration_covers_group_outside_global_chains() -> None:
    """核心修复：任务链引用的组不在全局链上时，准入不得以 unknown_group 跳过。"""
    cfg = Config.model_validate(_payload(inspection=['m_x']))
    concurrency = ModelKeyConcurrencyController(
        resolve_model_limits=lambda model_ref: {'key_indexes': [0], 'per_key_limits': {0: None}},
    )
    balancer = ModelLoadBalancer(permit_source=concurrency)
    service = object.__new__(MainRuntimeService)
    service._app_config = cfg
    service.node_runner = SimpleNamespace(_execution_model_routes=None, _acceptance_model_routes=None)
    service.model_load_balancer = balancer

    MainRuntimeService._apply_model_route_plans(
        service,
        build_model_route_plan(cfg, 'execution'),
        build_model_route_plan(cfg, 'inspection'),
        revision=3,
    )

    lease, reason = balancer.select(node_id='node:1', route_index=0, group_key='g_unused')
    assert lease is not None, reason
    assert lease.model_key in {'m_b', 'm_x'}
    assert balancer.snapshot()['config_revision'] == 3


def test_resolver_defaults_to_global(tmp_path) -> None:
    cfg = Config.model_validate(_payload(inspection=['m_x']))
    service = _service(tmp_path, cfg)
    service.store.upsert_task(_task('task:plain'))
    assert service.node_runner.task_model_routes_resolver is not None
    assert service._resolve_task_model_routes('task:plain', 'execution') is None
    assert service._resolve_task_model_routes('task:plain', 'acceptance') is None
    assert service._resolve_task_model_routes('task:missing', 'execution') is None
    assert service._resolve_task_model_routes('', 'execution') is None


def test_resolver_execution_override_with_unreferenced_group(tmp_path) -> None:
    cfg = Config.model_validate(_payload(inspection=['m_x']))
    service = _service(tmp_path, cfg)
    service.store.upsert_task(_task(
        'task:override',
        execution=[{'type': 'load_balance', 'group_key': 'g_unused'}, {'type': 'model', 'model_key': 'm_b'}],
    ))
    plan = service._resolve_task_model_routes('task:override', 'execution')
    assert plan is not None
    assert plan.signature() == 'lb:g_unused[m_b,m_x]|model:m_b'
    assert plan.config_revision == 1
    # 检验车道不受执行覆盖影响（全局检验已配置）。
    assert service._resolve_task_model_routes('task:override', 'acceptance') is None
    # 其他任务不受影响。
    service.store.upsert_task(_task('task:bystander'))
    assert service._resolve_task_model_routes('task:bystander', 'execution') is None


def test_resolver_inspection_override_independent(tmp_path) -> None:
    cfg = Config.model_validate(_payload(inspection=['m_x']))
    service = _service(tmp_path, cfg)
    service.store.upsert_task(_task('task:inspect', inspection=[{'type': 'model', 'model_key': 'm_b'}]))
    assert service._resolve_task_model_routes('task:inspect', 'execution') is None
    plan = service._resolve_task_model_routes('task:inspect', 'acceptance')
    assert plan is not None
    assert plan.signature() == 'model:m_b'


def test_resolver_inspection_inherits_task_execution_when_global_empty(tmp_path) -> None:
    cfg = Config.model_validate(_payload(inspection=[]))
    service = _service(tmp_path, cfg)
    service.store.upsert_task(_task(
        'task:inherit',
        execution=[{'type': 'model', 'model_key': 'm_b'}],
    ))
    execution_plan = service._resolve_task_model_routes('task:inherit', 'execution')
    acceptance_plan = service._resolve_task_model_routes('task:inherit', 'acceptance')
    assert execution_plan is not None
    # D5：全局检验为空时，任务检验继承本任务生效的执行链（同一份缓存 plan）。
    assert acceptance_plan is execution_plan
    # 执行无覆盖时检验也回退全局（全局层面继承执行）。
    service.store.upsert_task(_task('task:inherit-plain'))
    assert service._resolve_task_model_routes('task:inherit-plain', 'acceptance') is None


def test_resolver_invalid_override_falls_back_to_global(tmp_path) -> None:
    cfg = Config.model_validate(_payload(inspection=['m_x']))
    service = _service(tmp_path, cfg)
    service.store.upsert_task(_task('task:invalid', execution=[{'type': 'model', 'model_key': 'm_off'}]))
    assert service._resolve_task_model_routes('task:invalid', 'execution') is None
    service.store.upsert_task(_task('task:deleted-group', execution=[{'type': 'load_balance', 'group_key': 'g_deleted'}]))
    assert service._resolve_task_model_routes('task:deleted-group', 'execution') is None


def test_resolver_cache_keys_on_revision_and_entries(tmp_path) -> None:
    cfg = Config.model_validate(_payload(inspection=['m_x']))
    service = _service(tmp_path, cfg)
    service.store.upsert_task(_task('task:cache', execution=[{'type': 'model', 'model_key': 'm_b'}]))
    first = service._resolve_task_model_routes('task:cache', 'execution')
    assert service._resolve_task_model_routes('task:cache', 'execution') is first
    # revision 前进（配置刷新）⇒ 新 plan。
    service._runtime_model_revision = 2
    second = service._resolve_task_model_routes('task:cache', 'execution')
    assert second is not first
    assert second.config_revision == 2
    # 覆盖改写 ⇒ 新 plan；改回原样命中旧键。
    service.store.upsert_task(_task('task:cache', execution=[{'type': 'model', 'model_key': 'm_x'}]))
    third = service._resolve_task_model_routes('task:cache', 'execution')
    assert third.signature() == 'model:m_x'
    service.store.upsert_task(_task('task:cache', execution=[{'type': 'model', 'model_key': 'm_b'}]))
    assert service._resolve_task_model_routes('task:cache', 'execution') is second


def test_node_runner_selection_point_uses_resolver(tmp_path) -> None:
    cfg = Config.model_validate(_payload(inspection=['m_x']))
    service = _service(tmp_path, cfg)
    service.store.upsert_task(_task('task:runner', execution=[{'type': 'model', 'model_key': 'm_b'}]))
    service.store.upsert_task(_task('task:runner-other'))
    runner = service.node_runner

    node = SimpleNamespace(node_kind='execution', task_id='task:runner')
    assert runner._model_routes_for(node).signature() == 'model:m_b'
    assert runner._model_refs_for(node) == ['m_b']
    # 同任务检验车道仍走全局。
    acceptance_node = SimpleNamespace(node_kind='acceptance', task_id='task:runner')
    assert runner._model_routes_for(acceptance_node) is runner._acceptance_model_routes
    # 其他任务走全局。
    other = SimpleNamespace(node_kind='execution', task_id='task:runner-other')
    assert runner._model_routes_for(other) is runner._execution_model_routes


def test_refresh_clears_override_cache(tmp_path) -> None:
    cfg = Config.model_validate(_payload(inspection=['m_x']))
    service = _service(tmp_path, cfg)
    service.store.upsert_task(_task('task:refresh', execution=[{'type': 'model', 'model_key': 'm_b'}]))
    first = service._resolve_task_model_routes('task:refresh', 'execution')
    assert service._task_model_route_plan_cache
    # ensure_runtime_config_current 的刷新路径清缓存（这里直接模拟其清表动作的触发点：
    # revision 未变但缓存被清 ⇒ 重建新对象）。
    service._task_model_route_plan_cache.clear()
    assert service._resolve_task_model_routes('task:refresh', 'execution') is not first
