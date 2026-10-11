"""RBAC 窄查询必须与「全表读 + Python 过滤」逐值等值。

`_find_role_policy` 早先每次都调 `list_role_policies()`（全表逐行 json.loads +
pydantic 校验），改成按列下推 SQL 之后，这条测试守住三件事：

1. 列里的 actor_role 与解析后的公开形态逐字一致。不一致窄查询就查不到，
   `_evaluate` 会判成 policy_denied。
2. 精确动作优先于通配（NULL 动作）。工具面改成「一律以工具为单位一层」后，工具行的
   resource_kind 是 'tool' 且 action_id 恒为 NULL；技能行仍带 action_id='load'，
   优先级这条判据只有技能面还活着，所以在技能面上钉。
3. 按列过滤（窄查询）与解析后再过滤（全表扫）命中同一行。
"""

from __future__ import annotations

import sqlite3

from main.governance.models import PermissionSubject, RolePolicyMatrixRecord
from main.governance.policy_engine import MainRuntimePolicyEngine
from main.governance.roles import to_public_actor_role


def _policy(
    *,
    actor_role: str,
    resource_kind: str,
    resource_id: str,
    action_id: str | None,
    effect: str,
) -> RolePolicyMatrixRecord:
    stamp = '2026-10-01T00:00:00+08:00'
    return RolePolicyMatrixRecord(
        policy_id=f'pol:{actor_role}:{resource_kind}:{resource_id}:{action_id or "any"}',
        actor_role=actor_role,
        resource_kind=resource_kind,
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


def _reference_find(store, *, actor_role: str, resource_kind: str, resource_id: str, action_id: str | None):
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


def _seed(store) -> list[RolePolicyMatrixRecord]:
    return [
        # 工具面：一条工具一行，没有动作层，action_id 恒为 NULL。
        _policy(actor_role='execution', resource_kind='tool', resource_id='exec', action_id=None, effect='allow'),
        _policy(actor_role='ceo', resource_kind='tool', resource_id='exec', action_id=None, effect='deny'),
        _policy(actor_role='inspection', resource_kind='tool', resource_id='filesystem_stat', action_id=None, effect='allow'),
        _policy(actor_role='execution', resource_kind='tool', resource_id='other', action_id=None, effect='deny'),
        # 技能面：仍带 action_id='load'，并且留一条 NULL 通配用来钉优先级。
        _policy(actor_role='execution', resource_kind='skill', resource_id='exec', action_id='load', effect='allow'),
        _policy(actor_role='execution', resource_kind='skill', resource_id='exec', action_id=None, effect='deny'),
    ]


def test_narrow_lookup_matches_full_scan_reference(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        store.replace_default_role_policies(_seed(store))
        engine = MainRuntimePolicyEngine(store=store, resource_registry=None)

        for role, kind, rid, action in (
            ('execution', 'tool', 'exec', None),
            ('ceo', 'tool', 'exec', None),
            ('inspection', 'tool', 'filesystem_stat', None),
            ('inspection', 'tool', 'exec', None),
            ('execution', 'tool', 'missing', None),
            ('execution', 'skill', 'exec', None),
            ('execution', 'skill', 'exec', 'load'),
            ('execution', 'skill', 'exec', 'other'),
            ('execution', 'tool_family', 'exec', None),
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
            resource_kind='skill',
            resource_id='exec',
            action_id='load',
        )
        assert exact_first is not None and exact_first.action_id == 'load' and exact_first.effect == 'allow'

        wildcard_only = engine._find_role_policy(
            subject=PermissionSubject(actor_role='execution', user_key='u:test', session_id='web:test'),
            resource_kind='skill',
            resource_id='exec',
            action_id='copy',
        )
        assert wildcard_only is not None and wildcard_only.action_id is None and wildcard_only.effect == 'deny'

        tool_row = engine._find_role_policy(
            subject=PermissionSubject(actor_role='execution', user_key='u:test', session_id='web:test'),
            resource_kind='tool',
            resource_id='exec',
            action_id=None,
        )
        assert tool_row is not None and tool_row.action_id is None and tool_row.effect == 'allow'
    finally:
        store.close()


def test_actor_role_column_is_the_verbatim_public_shape_of_the_payload(tmp_path) -> None:
    """列里的 actor_role 必须与 payload 的公开形态逐字一致，否则窄查询查不到。"""
    store = _store(tmp_path)
    try:
        records = _seed(store)
        store.replace_default_role_policies(records)

        conn = sqlite3.connect(str(store.path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                'SELECT policy_id, actor_role, resource_kind, resource_id, action_id, payload_json '
                'FROM role_policy_matrix'
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == len(records)
        engine = MainRuntimePolicyEngine(store=store, resource_registry=None)
        for row in rows:
            payload = RolePolicyMatrixRecord.model_validate_json(str(row['payload_json']))
            assert str(row['actor_role']) == payload.actor_role
            assert str(row['actor_role']) == to_public_actor_role(payload.actor_role)
            assert str(row['resource_kind']) == payload.resource_kind
            assert str(row['resource_id']) == payload.resource_id
            assert row['action_id'] == payload.action_id
            # 列值查不到就等于 _evaluate 判 policy_denied，这里逐行钉一次。
            found = engine._find_role_policy(
                subject=PermissionSubject(
                    actor_role=payload.actor_role, user_key='u:test', session_id='web:test'
                ),
                resource_kind=payload.resource_kind,
                resource_id=payload.resource_id,
                action_id=payload.action_id,
            )
            assert found is not None and found.policy_id == payload.policy_id
    finally:
        store.close()
