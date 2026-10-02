"""终态任务的残余节点自愈必须按计数判定，不搬正文。

worker 启动对每个终态任务都要问一次"还有没有没结算的节点"。旧写法为此整任务读回
payload_json 并逐行建模：实盘 63 个终态任务合计 234.8 MB，实盘命中的残余是 0 条。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from main.models import NodeRecord
from main.protocol import now_iso
from main.service.runtime_service import MainRuntimeService
from main.storage.sqlite_store import SQLiteTaskStore

_TERMINAL = ('success', 'failed')


def _node(task_id: str, node_id: str, status: str) -> NodeRecord:
    stamp = now_iso()
    return NodeRecord(
        node_id=node_id,
        task_id=task_id,
        root_node_id=node_id,
        status=status,
        goal=node_id,
        prompt='p',
        input='x' * 50_000,
        created_at=stamp,
        updated_at=stamp,
    )


def test_count_skips_terminal_and_counts_the_rest(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        for index, status in enumerate(_TERMINAL):
            store.upsert_node(_node('task:a', f'node:t{index}', status))
        store.upsert_node(_node('task:a', 'node:live', 'in_progress'))
        store.upsert_node(_node('task:a', 'node:live2', 'in_progress'))
        store.upsert_node(_node('task:b', 'node:other', 'in_progress'))
        assert store.count_unsettled_task_nodes('task:a') == 2
        assert store.count_unsettled_task_nodes('task:b') == 1
        assert store.count_unsettled_task_nodes('task:absent') == 0
    finally:
        store.close()


def test_count_matches_the_python_predicate_on_padded_case(tmp_path: Path):
    """存量行的 status 可能有空格/大写（模型是 Literal，裸写入过）。

    SQL 侧 `lower(trim(status))` 必须与 `TaskLogService._is_terminal_status` 同口径，
    否则闸门会在"Python 认为还没结算"的行上报 0，自愈被静默跳过。
    """
    from main.monitoring.log_service import TaskLogService

    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        for node_id, raw_status in (
            ('node:upper', 'SUCCESS'),
            ('node:padded', ' failed '),
            ('node:run', ' IN_PROGRESS '),
        ):
            store.upsert_node(_node('task:c', node_id, 'in_progress'))
            store._execute_write(  # noqa: SLF001 - 就是要绕开模型造存量形态
                'UPDATE nodes SET status = ? WHERE node_id = ?',
                (raw_status, node_id),
            )
        unsettled = store.count_unsettled_task_nodes('task:c')
        expected = sum(
            1
            for row in store._fetchall(  # noqa: SLF001 - 判据本身就是这条 SQL 的对照
                'SELECT status FROM nodes WHERE task_id = ?', ('task:c',)
            )
            if not TaskLogService._is_terminal_status(row['status'])
        )
        assert unsettled == expected == 1
    finally:
        store.close()


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


def _terminate(service: MainRuntimeService, task_id: str) -> None:
    service.store.update_task(
        task_id,
        lambda record: record.model_copy(update={'status': 'success', 'updated_at': now_iso()}),
    )


@pytest.mark.asyncio
async def test_sweep_does_not_read_node_bodies_when_nothing_is_unsettled(tmp_path: Path):
    service = _service(tmp_path)
    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )
    record = await service.create_task('残余扫描', session_id='web:shared')
    service.store.update_node(
        record.root_node_id,
        lambda r: r.model_copy(update={'status': 'success', 'updated_at': now_iso()}),
    )
    service.store.upsert_node(_node(record.task_id, 'node:done', 'success'))
    _terminate(service, record.task_id)

    def _boom(*args, **kwargs):
        raise AssertionError('残余为 0 时不该整任务读节点正文')

    service.store.iter_nodes = _boom  # type: ignore[method-assign]
    service.store.list_nodes = _boom  # type: ignore[method-assign]
    assert service.log_service.sweep_residual_nodes(record.task_id) == []


@pytest.mark.asyncio
async def test_projection_staleness_gate(tmp_path: Path):
    """启动重建的跳过判据：投影戳 == 节点戳，且两张投影都在。"""
    service = _service(tmp_path)
    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )
    record = await service.create_task('投影戳判定', session_id='web:shared')
    service.store.upsert_node(_node(record.task_id, 'node:sync', 'success'))
    service.log_service.sync_node_read_model(record.task_id, 'node:sync')
    assert service.store.count_stale_task_node_projections(record.task_id) == 0

    # 投影落后于节点（崩溃在"写完节点、没来得及写投影"之间）必须判为需要重建。
    # 这里直接改旧投影行的戳：`updated_at` 是秒级粒度，同秒内两次写入会读成相等，
    # 所以不能用"再写一次节点"来造漂移。
    service.store._execute_write(  # noqa: SLF001
        'UPDATE task_node_details SET updated_at = ? WHERE node_id = ?',
        ('2020-01-01T00:00:00+08:00', 'node:sync'),
    )
    assert service.store.count_stale_task_node_projections(record.task_id) == 1

    service.log_service.sync_node_read_model(record.task_id, 'node:sync')
    assert service.store.count_stale_task_node_projections(record.task_id) == 0

    # 缺投影行同样要判脏（明细在、task_nodes 被删的情况）。
    service.store._execute_write('DELETE FROM task_nodes WHERE node_id = ?', ('node:sync',))  # noqa: SLF001
    assert service.store.count_stale_task_node_projections(record.task_id) == 1


@pytest.mark.asyncio
async def test_sweep_still_flips_a_residual_node(tmp_path: Path):
    service = _service(tmp_path)
    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )
    record = await service.create_task('残余扫描', session_id='web:shared')
    residual = _node(record.task_id, 'node:stuck', 'in_progress')
    service.store.upsert_node(residual)
    service.store.upsert_node(_node(record.task_id, 'node:done', 'success'))
    _terminate(service, record.task_id)

    swept = service.log_service.sweep_residual_nodes(record.task_id)
    # create_task 的根节点本身也是 in_progress，一起被扫是预期的。
    assert set(item.node_id for item in swept) == {record.root_node_id, 'node:stuck'}
    settled = service.store.get_node('node:stuck')
    assert settled is not None
    assert settled.status == 'failed'
    assert 'task_terminal_cleanup' in str(settled.failure_reason or '')
