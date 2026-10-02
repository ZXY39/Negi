"""spawn 评审的树摘要改读投影表：分支清单必须逐字节等价。

这段文本是评审模型的判断输入（决定它看哪 10 个分支），不是显示——所以换数据源
必须证明排序与字段口径没变：投影 `title == goal or node_id`、`sort_key == created_at:node_id`。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord
from main.protocol import now_iso
from main.runtime.node_runner import NodeRunner
from main.service.runtime_service import MainRuntimeService


class _StubChatBackend:
    async def complete(self, *args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError('chat backend must not be called in this test')


def _service(tmp_path: Path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=_StubChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )


def _node(task_id: str, node_id: str, parent_id: str, *, depth: int, goal: str, created_at: str) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        task_id=task_id,
        parent_node_id=parent_id,
        root_node_id=task_id.replace('task:', 'node:'),
        depth=depth,
        status='in_progress',
        goal=goal,
        prompt='p',
        created_at=created_at,
        updated_at=created_at,
    )


def _legacy_runtime_summary(store, task_id: str, parent: NodeRecord) -> dict:
    """改之前的算法：整任务建 NodeRecord，按 (depth, created_at, node_id) 排。"""
    nodes = list(store.list_nodes(task_id))
    path_ids: set[str] = set()
    current = parent
    while current is not None:
        if current.node_id:
            path_ids.add(current.node_id)
        current = store.get_node(current.parent_node_id) if current.parent_node_id else None
    others = sorted(
        (node for node in nodes if node.node_id not in path_ids),
        key=lambda item: (int(item.depth or 0), str(item.created_at or ''), str(item.node_id or '')),
    )
    lines = [
        '{} {}- ({},{},{},{})'.format(
            '  ' * int(node.depth or 0),
            int(node.depth or 0),
            node.node_id,
            node.status,
            NodeRunner._trim_diagnostic_text(node.goal, max_chars=80),
            '',
        )
        for node in others[:10]
    ]
    return {'execution_node_count': len(nodes), 'visible_other_branch_lines': lines}


@pytest.mark.asyncio
async def test_projection_summary_matches_runtime_summary_line_for_line(tmp_path: Path):
    service = _service(tmp_path)
    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )
    record = await service.create_task('评审树摘要等价', session_id='web:shared')
    task_id = record.task_id
    root = service.store.get_node(record.root_node_id)
    assert root is not None

    # 同 depth 下故意用同一秒 + 不同 id，再放一个空 goal 的分支（title 会退化成 node_id）。
    rows = [
        ('node:c', 1, '第三个分支', '2026-10-02T10:00:05+08:00'),
        ('node:a', 1, '第一个分支', '2026-10-02T10:00:05+08:00'),
        ('node:b', 1, '', '2026-10-02T10:00:04+08:00'),
        ('node:d', 2, '孙子分支', '2026-10-02T10:00:06+08:00'),
    ]
    for node_id, depth, goal, stamp in rows:
        service.store.upsert_node(_node(task_id, node_id, record.root_node_id, depth=depth, goal=goal, created_at=stamp))
        service.log_service.sync_node_read_model(task_id, node_id)

    runner = SimpleNamespace(
        _store=service.store,
        _trim_diagnostic_text=NodeRunner._trim_diagnostic_text,
        _spawn_review_stage_goal=lambda **kwargs: '',
    )
    new = NodeRunner._spawn_review_tree_summary(runner, task_id=task_id, parent=root)
    old = _legacy_runtime_summary(service.store, task_id, root)

    assert new['execution_node_count'] == old['execution_node_count'] == 5
    assert new['visible_other_branch_lines'] == old['visible_other_branch_lines'], (
        new['visible_other_branch_lines'], old['visible_other_branch_lines']
    )
    # 空 goal 的分支不能被投影 title（会退化成 node_id）冒名顶替：那一格的 goal 段仍是空。
    assert any('(node:b,in_progress,,)' in line for line in new['visible_other_branch_lines']), new['visible_other_branch_lines']


@pytest.mark.asyncio
async def test_projection_summary_does_not_read_the_runtime_nodes_table(tmp_path: Path):
    service = _service(tmp_path)
    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )
    record = await service.create_task('评审树摘要窄读', session_id='web:shared')
    root = service.store.get_node(record.root_node_id)

    def _boom(*args, **kwargs):
        raise AssertionError('评审树摘要不该再整任务读 nodes')

    service.store.list_nodes = _boom  # type: ignore[method-assign]
    runner = SimpleNamespace(
        _store=service.store,
        _trim_diagnostic_text=NodeRunner._trim_diagnostic_text,
        _spawn_review_stage_goal=lambda **kwargs: '',
    )
    summary = NodeRunner._spawn_review_tree_summary(runner, task_id=record.task_id, parent=root)
    assert summary['execution_node_count'] == 1
