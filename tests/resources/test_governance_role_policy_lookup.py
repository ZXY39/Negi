"""RBAC 窄查询必须与「全表读 + Python 过滤」逐值等值。

`_find_role_policy` 早先每次都调 `list_role_policies()`（全表 270 行，逐行
json.loads + pydantic 校验），改成按列下推 SQL 之后，这条测试守住两件事：
列里的 actor_role 与解析后的公开形态一致，且精确动作优先于通配（NULL 动作）。
"""

from __future__ import annotations

from main.governance.models import PermissionSubject, RolePolicyMatrixRecord
from main.governance.policy_engine import MainRuntimePolicyEngine


def _policy(*, actor_role: str, resource_id: str, action_id: str | None, effect: str) -> RolePolicyMatrixRecord:
    stamp = '2026-10-01T00:00:00+08:00'
    return RolePolicyMatrixRecord(
        policy_id=f'pol:{actor_role}:{resource_id}:{action_id or "any"}',
        actor_role=actor_role,
        resource_kind='tool_family',
        resource_id=resource_id,
        action_id=action_id,
        effect=effect,
        source='default',
        created_at=stamp,
        updated_at=stamp,
    )


def _store(tmp_path):
    from main.governance.store import GovernanceStore

    return GovernanceStore(tmp_path / 'governance.sqlite3')


def _reference_find(store, *, actor_role: str, resource_kind: str, resource_id: str, action_id: str):
    policies = [
        policy
        for policy in store.list_role_policies()
        if policy.actor_role == actor_role
        and policy.resource_kind == resource_kind
        and policy.resource_id == resource_id
        and policy.action_id in {None, action_id}
    ]
    policies.sort(key=lambda item: 1 if item.action_id == action_id else 0, reverse=True)
    return policies[0] if policies else None


def test_narrow_lookup_matches_full_scan_reference(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        store.replace_default_role_policies([
            _policy(actor_role='execution', resource_id='exec', action_id='run', effect='allow'),
            _policy(actor_role='execution', resource_id='exec', action_id=None, effect='deny'),
            _policy(actor_role='ceo', resource_id='exec', action_id='run', effect='deny'),
            _policy(actor_role='execution', resource_id='other', action_id='run', effect='deny'),
            _policy(actor_role='execution', resource_id='exec', action_id='search', effect='deny'),
        ])
        engine = MainRuntimePolicyEngine(store=store, resource_registry=None)

        for role, kind, rid, action in (
            ('execution', 'tool_family', 'exec', 'run'),
            ('execution', 'tool_family', 'exec', 'search'),
            ('ceo', 'tool_family', 'exec', 'run'),
            ('inspection', 'tool_family', 'exec', 'run'),
            ('execution', 'tool_family', 'missing', 'run'),
            ('execution', 'skill', 'exec', 'load'),
        ):
            narrow = engine._find_role_policy(
                subject=PermissionSubject(actor_role=role, user_key='u-test', session_id='web:test'),
                resource_kind=kind,
                resource_id=rid,
                action_id=action,
            )
            reference = _reference_find(store, actor_role=role, resource_kind=kind, resource_id=rid, action_id=action)
            assert (narrow.policy_id if narrow else None) == (reference.policy_id if reference else None), (
                role, kind, rid, action,
                narrow.policy_id if narrow else None,
                reference.policy_id if reference else None,
            )

        exact_first = engine._find_role_policy(
            subject=PermissionSubject(actor_role='execution', user_key='u:test', session_id='web:test'),
            resource_kind='tool_family',
            resource_id='exec',
            action_id='run',
        )
        assert exact_first is not None and exact_first.action_id == 'run' and exact_first.effect == 'allow'

        wildcard_only = engine._find_role_policy(
            subject=PermissionSubject(actor_role='execution', user_key='u:test', session_id='web:test'),
            resource_kind='tool_family',
            resource_id='exec',
            action_id='copy',
        )
        assert wildcard_only is not None and wildcard_only.action_id is None and wildcard_only.effect == 'deny'
    finally:
        store.close()
