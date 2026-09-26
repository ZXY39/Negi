from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from g3ku.config.schema import Config
from main.runtime.model_load_balancer import quota_bucket_key
from main.runtime.model_route import build_model_route_plan
from main.runtime.node_runner import NodeRunner
from main.service.runtime_service import MainRuntimeService


def _config_payload() -> dict[str, Any]:
    return {
        'agents': {'defaults': {'workspace': '.'}},
        'models': {
            'catalog': [
                {'key': 'm_a', 'llmConfigId': 'rec_a', 'enabled': True, 'contextWindowTokens': 200000, 'imageMultimodalEnabled': True},
                {'key': 'm_b', 'llmConfigId': 'rec_b', 'enabled': True, 'contextWindowTokens': 128000},
                {'key': 'm_off', 'llmConfigId': 'rec_off', 'enabled': False, 'contextWindowTokens': 200000},
                {'key': 'm_x', 'llmConfigId': 'rec_x', 'enabled': True, 'contextWindowTokens': 200000},
                {'key': 'm_emergency', 'llmConfigId': 'rec_e', 'enabled': True, 'contextWindowTokens': 200000},
            ],
            'loadBalanceGroups': {
                'g_shared': {'enabled': True, 'maxRetryRounds': 2, 'modelKeys': ['m_a', 'm_b']},
                'g_off': {'enabled': False, 'modelKeys': ['m_a']},
            },
            'roles': {
                'ceo': ['m_x'],
                'execution': [{'type': 'load_balance', 'groupKey': 'g_shared'}, {'type': 'model', 'modelKey': 'm_emergency'}],
                'inspection': [{'type': 'load_balance', 'groupKey': 'g_shared'}],
                'memory': [],
            },
        },
    }


def _runner_stub(execution_routes: Any, acceptance_routes: Any, *, execution_refs: list[str], acceptance_refs: list[str]) -> NodeRunner:
    # 只验证路由解析与上下文视图，不需要 store/react_loop 等真实协作者。
    runner = object.__new__(NodeRunner)
    runner._execution_model_routes = execution_routes
    runner._acceptance_model_routes = acceptance_routes
    runner._execution_model_refs = list(execution_refs)
    runner._acceptance_model_refs = list(acceptance_refs)
    return runner


def _node(node_kind: str) -> SimpleNamespace:
    return SimpleNamespace(node_kind=node_kind)


def test_build_model_route_plan_expands_groups_and_keeps_order() -> None:
    cfg = Config.model_validate(_config_payload())

    plan = build_model_route_plan(cfg, 'execution', revision=42)

    assert [route.kind for route in plan.routes] == ['load_balance', 'model']
    assert plan.routes[0].group_key == 'g_shared'
    assert plan.routes[0].candidates == ('m_a', 'm_b')
    assert plan.routes[0].group.max_retry_rounds == 2
    assert plan.routes[1].model_key == 'm_emergency'
    # 候选展开视图保留，但不再代表 fallback 顺序。
    assert plan.candidate_model_keys == ['m_a', 'm_b', 'm_emergency']
    assert plan.load_balance_group_keys == ['g_shared']
    assert plan.config_revision == 42
    assert plan.signature() == 'lb:g_shared[m_a,m_b]|model:m_emergency'


def test_build_model_route_plan_skips_disabled_members_and_groups() -> None:
    payload = _config_payload()
    payload['models']['roles']['execution'] = [
        {'type': 'load_balance', 'groupKey': 'g_off'},
        {'type': 'model', 'modelKey': 'm_emergency'},
    ]
    cfg = Config.model_validate(payload)

    plan = build_model_route_plan(cfg, 'execution')

    # 禁用的组整段不贡献候选（entry 仍在链上，选择层会给出 group_disabled）。
    assert plan.routes[0].candidates == ()
    assert plan.routes[0].group.enabled is False
    assert plan.candidate_model_keys == ['m_emergency']


def test_build_model_route_plan_of_empty_chain_returns_no_routes() -> None:
    payload = _config_payload()
    payload['models']['roles']['memory'] = []
    cfg = Config.model_validate(payload)

    assert build_model_route_plan(cfg, 'memory').routes == []


def test_node_runner_candidate_view_follows_group_members() -> None:
    cfg = Config.model_validate(_config_payload())
    execution_plan = build_model_route_plan(cfg, 'execution')
    inspection_plan = build_model_route_plan(cfg, 'inspection')
    runner = _runner_stub(
        execution_plan,
        inspection_plan,
        execution_refs=['legacy_only'],
        acceptance_refs=['legacy_only'],
    )

    assert runner._model_refs_for(_node('execution')) == ['m_a', 'm_b', 'm_emergency']
    assert runner._model_refs_for(_node('acceptance')) == ['m_a', 'm_b']
    assert runner._has_load_balance_routes(_node('execution')) is True
    assert runner._load_balance_group_keys(_node('execution')) == ['g_shared']
    assert runner._model_route_entries_payload(_node('execution')) == [
        {'type': 'load_balance', 'group_key': 'g_shared', 'model_keys': ['m_a', 'm_b'], 'max_retry_rounds': 2},
        {'type': 'model', 'model_key': 'm_emergency'},
    ]


def test_node_runner_without_plan_falls_back_to_legacy_refs() -> None:
    runner = _runner_stub(None, None, execution_refs=['m_a', 'm_b'], acceptance_refs=['m_b', 'm_a'])

    assert runner._model_refs_for(_node('execution')) == ['m_a', 'm_b']
    assert runner._has_load_balance_routes(_node('execution')) is False
    # 没有 plan 时 route 视图退化成「按链序的 direct entry」，不改写旧语义。
    assert runner._model_route_entries_payload(_node('acceptance')) == [
        {'type': 'model', 'model_key': 'm_b'},
        {'type': 'model', 'model_key': 'm_a'},
    ]


def test_service_route_helpers_use_plan_and_inherit_execution_for_inspection() -> None:
    cfg = Config.model_validate(_config_payload())
    service = SimpleNamespace(_app_config=cfg, node_runner=SimpleNamespace(_execution_model_routes=None, _acceptance_model_routes=None))

    execution_routes = MainRuntimeService._initial_model_routes(service, cfg, 'execution')
    inspection_routes = MainRuntimeService._initial_model_routes(service, cfg, 'inspection')

    assert execution_routes is not None and execution_routes.load_balance_group_keys == ['g_shared']
    # inspection 自己也配了组时不继承；为空时才继承整份执行 route plan。
    assert inspection_routes is not None and inspection_routes.routes[0].group_key == 'g_shared'

    empty_roles = _config_payload()
    empty_roles['models']['roles']['inspection'] = []
    cfg2 = Config.model_validate(empty_roles)
    service2 = SimpleNamespace(_app_config=cfg2)
    inherited = MainRuntimeService._initial_model_routes(service2, cfg2, 'inspection')
    assert [route.kind for route in inherited.routes] == ['load_balance', 'model']


def test_service_balancer_groups_come_from_resolved_plans() -> None:
    cfg = Config.model_validate(_config_payload())
    service = SimpleNamespace(
        node_runner=SimpleNamespace(
            _execution_model_routes=build_model_route_plan(cfg, 'execution'),
            _acceptance_model_routes=build_model_route_plan(cfg, 'inspection'),
        )
    )

    groups = MainRuntimeService._resolved_load_balance_groups(service, cfg)

    assert set(groups) == {'g_shared'}
    assert groups['g_shared'].max_retry_rounds == 2
    assert [member.model_key for member in groups['g_shared'].members] == ['m_a', 'm_b']


def test_quota_buckets_prefer_declared_pool_and_return_empty_when_unresolvable() -> None:
    cfg = SimpleNamespace(
        get_managed_model=lambda key: SimpleNamespace(quota_pool_key='gw_shared' if key == 'm_a' else ''),
        workspace_path=None,
    )
    service = SimpleNamespace(_app_config=cfg)

    assert MainRuntimeService._resolve_quota_buckets(service, 'm_a') == ['pool:gw_shared']
    # 解析不出 binding（这里 resolve_chat_target 会抛错）时返回空，由 balancer 记 unresolved。
    assert MainRuntimeService._resolve_quota_buckets(service, 'm_b') == []
    assert MainRuntimeService._resolve_quota_buckets(service, '') == []

    service_no_config = SimpleNamespace(_app_config=None)
    assert MainRuntimeService._resolve_quota_buckets(service_no_config, 'm_a') == []


def test_quota_bucket_key_distinguishes_keys_on_same_endpoint() -> None:
    one = quota_bucket_key(endpoint='https://gw.example/v1', api_key='sk-1')
    two = quota_bucket_key(endpoint='https://gw.example/v1', api_key='sk-2')
    assert one != two
    assert quota_bucket_key(endpoint='', api_key='') == ''
