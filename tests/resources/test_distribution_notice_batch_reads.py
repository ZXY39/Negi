"""分发信箱的两条判定车道：一次批量读，不逐节点回表。

实盘在跑任务 938 个节点，旧写法每次判定要发 938～1,876 条查询（还带逐节点 `get_node`
把 246 KB/行的 payload 读回建模），wall 采样里两条车道合计 9.24 s / 180 s。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import TaskNodeNotification
from main.protocol import now_iso
from main.runtime.node_runner import NodeRunner
from main.storage.sqlite_store import SQLiteTaskStore

_TASK = 'task:batch'


def _notice(notification_id: str, node_id: str, status: str, *, merged_at: str = '') -> TaskNodeNotification:
    return TaskNodeNotification(
        notification_id=notification_id,
        task_id=_TASK,
        node_id=node_id,
        epoch_id='epoch:1',
        status=status,
        created_at=now_iso(),
        merged_at=merged_at,
    )


@pytest.fixture()
def store(tmp_path: Path):
    instance = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        yield instance
    finally:
        instance.close()


def test_sql_predicate_matches_the_python_predicate(store: SQLiteTaskStore):
    from main.runtime.node_runner import _notification_awaits_injection

    cases = [
        _notice('n:delivered', 'node:a', 'delivered'),
        _notice('n:consumed-unmerged', 'node:a', 'consumed'),
        _notice('n:consumed-merged', 'node:b', 'consumed', merged_at='2026-10-02T10:00:00+08:00'),
        _notice('n:merged', 'node:b', 'merged'),
        _notice('n:delivered-blank-merge', 'node:c', 'delivered', merged_at='2026-10-02T10:00:00+08:00'),
    ]
    for record in cases:
        store.upsert_task_node_notification(record)

    expected = sum(1 for record in cases if _notification_awaits_injection(record))
    assert store.count_task_notifications_awaiting_injection(_TASK) == expected == 3
    assert store.map_task_notifications_awaiting_injection(_TASK) == {'node:a': 2, 'node:c': 1}


def test_mailbox_count_issues_one_query(store: SQLiteTaskStore):
    runner = SimpleNamespace(_store=store)
    store.upsert_task_node_notification(_notice('n:1', 'node:a', 'delivered'))

    def _boom(*args, **kwargs):
        raise AssertionError('信箱条数不该再逐节点查通知表')

    store.list_task_node_notifications = _boom  # type: ignore[method-assign]
    store.list_task_nodes = _boom  # type: ignore[method-assign]
    assert NodeRunner.pending_distribution_mailbox_count(runner, task_id=_TASK) == 1


def test_pending_nodes_use_projection_counts_without_touching_nodes_table(store: SQLiteTaskStore):
    """根节点那半读投影行的 `pending_append_notice_count`，子节点那半读一次分组结果。"""
    runner = SimpleNamespace(_store=store)
    store.upsert_task_node_notification(_notice('n:child', 'node:child', 'consumed'))
    records = {
        'node:root': SimpleNamespace(node_id='node:root', payload={'pending_append_notice_count': 2}),
        'node:child': SimpleNamespace(node_id='node:child', payload={'pending_append_notice_count': 0}),
        'node:quiet': SimpleNamespace(node_id='node:quiet', payload={'pending_append_notice_count': 0}),
    }
    store.list_task_nodes = lambda task_id: list(records.values())  # type: ignore[assignment]

    def _boom(*args, **kwargs):
        raise AssertionError('新投影行齐全时不该回读运行时节点表')

    store.get_node = _boom  # type: ignore[method-assign]
    store.iter_nodes = _boom  # type: ignore[method-assign]

    assert NodeRunner.nodes_with_pending_distribution_notices(runner, task_id=_TASK) == [
        'node:root',
        'node:child',
    ]


def test_legacy_projection_row_falls_back_to_runtime_metadata_once(store: SQLiteTaskStore):
    scanned: list[str] = []
    runner = SimpleNamespace(
        _store=store,
        _pending_root_notice_records=lambda *, node: (
            [{'notification_id': 'n:legacy'}] if node.node_id == 'node:legacy' else []
        ),
    )
    store.list_task_nodes = lambda task_id: [  # type: ignore[assignment]
        SimpleNamespace(node_id='node:legacy', payload={}),
        SimpleNamespace(node_id='node:modern', payload={'pending_append_notice_count': 0}),
    ]

    def _fake_iter(task_id: str):
        scanned.append(task_id)
        yield SimpleNamespace(node_id='node:legacy')

    store.iter_nodes = _fake_iter  # type: ignore[method-assign]
    assert NodeRunner.nodes_with_pending_distribution_notices(runner, task_id=_TASK) == ['node:legacy']
    # 缺字段的行只批量补读一次，不逐节点 get_node。
    assert scanned == [_TASK]
