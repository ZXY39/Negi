from __future__ import annotations

from pathlib import Path

from main.models import NodeRecord, TaskRecord, TokenUsageSummary
from main.monitoring.file_store import TaskFileStore
from main.monitoring.log_service import TaskLogService
from main.monitoring.query_service import TaskQueryService
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:countonly'


def _node(index: int) -> NodeRecord:
    node_id = f'node:{index}'
    return NodeRecord(
        node_id=node_id, task_id=TASK_ID, parent_node_id=None, root_node_id='node:0',
        depth=0, node_kind='execution', status='in_progress', goal='demo', prompt='demo',
        input='x' * 4096, output=[], check_result='', final_output='', can_spawn_children=False,
        created_at='2026-10-01T10:00:00+08:00', updated_at='2026-10-01T10:00:00+08:00',
        token_usage=TokenUsageSummary(tracked=True),
    )


def _services(tmp_path: Path, *, nodes: int) -> tuple[SQLiteTaskStore, TaskLogService, TaskQueryService]:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    store.upsert_task(TaskRecord(
        task_id=TASK_ID, session_id='web:shared', title='demo', user_request='demo',
        status='in_progress', root_node_id='node:0', max_depth=1,
        created_at='2026-10-01T10:00:00+08:00', updated_at='2026-10-01T10:00:00+08:00',
        token_usage=TokenUsageSummary(tracked=True), metadata={},
    ))
    log_service = TaskLogService(
        store=store, file_store=TaskFileStore(tmp_path / 'files'), registry=None,
        event_history_enabled=False,
    )
    # 读模型表（task_nodes / task_node_rounds）由 log_service 落，直接 upsert_node 不会填它
    for index in range(nodes):
        log_service.create_node(TASK_ID, _node(index))
    return store, log_service, TaskQueryService(
        store=store, file_store=log_service._file_store, log_service=log_service,
    )


def test_counts_match_the_full_listing(tmp_path: Path) -> None:
    """计数口与整表建模必须给同一个数，含空任务与不存在的任务。"""
    for nodes in (0, 1, 4):
        store, _log_service, _query = _services(tmp_path / f'case-{nodes}', nodes=nodes)
        assert store.count_task_nodes(TASK_ID) == len(store.list_task_nodes(TASK_ID)) == nodes
        assert store.count_task_node_rounds(TASK_ID) == len(store.list_task_node_rounds(TASK_ID)) == 0
        assert store.count_task_nodes('task:missing') == 0


def test_snapshot_counts_survive_the_swap(tmp_path: Path) -> None:
    """条数改走计数口之后，快照里这两个数必须与整表建模一致。

    注意：快照本身还有别的消费者真的要读节点与轮次行（树装配），所以这里**不**断言
    "一次都不 list"——那属于把判据写过头。省下的那一遍整批建模由
    `recent_long_blocks` 的实测条数来判（见计划文档 P1）。
    """
    store, _log_service, query_service = _services(tmp_path / 'snapshot', nodes=4)
    payload = query_service.get_task_snapshot(TASK_ID, mark_read=False)

    assert payload is not None
    assert payload['summary']['total_nodes'] == store.count_task_nodes(TASK_ID) == 4
    assert payload['summary']['total_rounds'] == store.count_task_node_rounds(TASK_ID) == 0
    # 帧表只投影一次：两处必须是同一份内容
    assert payload['runtime_summary']['frames'] == payload['frontier']
    assert [item['node_id'] for item in payload['runtime_summary']['frames']] == [
        item['node_id'] for item in payload['frontier']
    ]


def test_frontier_and_summary_share_one_projection(tmp_path: Path) -> None:
    """空台账时 `runtime_summary` 的兜底形状不许因为重构丢掉任何一个键。"""
    store, log_service, query_service = _services(tmp_path / 'empty-summary', nodes=1)
    payload = query_service.get_task_snapshot(TASK_ID, mark_read=False)
    assert payload is not None
    summary = payload['runtime_summary']
    for key in ('active_node_ids', 'runnable_node_ids', 'waiting_node_ids', 'dispatch_limits',
                'dispatch_running', 'dispatch_queued', 'distribution', 'frames'):
        assert key in summary, key
