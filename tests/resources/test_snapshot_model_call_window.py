"""任务快照的逐次调用窗口。

契约：
1) 快照默认只带最近 `_TASK_SNAPSHOT_MODEL_CALL_ROWS` 条，读口永远带 SQL 侧 LIMIT；
   `limit=None` 不再等于"整任务账本"（实盘在飞单任务 31686 行 / 75 MB，
   冷页一次读回 18.4 s、热态读回+建模+序列化 1979 ms）。
2) 总条数走计数口 `count_task_model_calls`，界面那句"任务开始以来共 N 次调用"用它，
   不能用明细行的长度反推。
3) 显式传数照常生效，`0` 是"不取"。
"""

from __future__ import annotations

from pathlib import Path

from main.models import NodeRecord, TaskRecord, TokenUsageSummary
from main.monitoring.file_store import TaskFileStore
from main.monitoring.log_service import TaskLogService
from main.monitoring.query_service import _TASK_SNAPSHOT_MODEL_CALL_ROWS, TaskQueryService
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:mcwindow'
NODE_ID = 'node:0'


def _node(index: int) -> NodeRecord:
    return NodeRecord(
        node_id=f'node:{index}', task_id=TASK_ID, parent_node_id=None, root_node_id=NODE_ID,
        depth=0, node_kind='execution', status='in_progress', goal='demo', prompt='demo',
        input='x' * 64, output=[], check_result='', final_output='', can_spawn_children=False,
        created_at='2026-10-01T10:00:00+08:00', updated_at='2026-10-01T10:00:00+08:00',
        token_usage=TokenUsageSummary(tracked=True),
    )


def _services(tmp_path: Path, *, calls: int) -> tuple[SQLiteTaskStore, TaskQueryService]:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    store.upsert_task(TaskRecord(
        task_id=TASK_ID, session_id='web:shared', title='demo', user_request='demo',
        status='in_progress', root_node_id=NODE_ID, max_depth=1,
        created_at='2026-10-01T10:00:00+08:00', updated_at='2026-10-01T10:00:00+08:00',
        token_usage=TokenUsageSummary(tracked=True), metadata={},
    ))
    log_service = TaskLogService(
        store=store, file_store=TaskFileStore(tmp_path / 'files'), registry=None,
        event_history_enabled=False,
    )
    log_service.create_node(TASK_ID, _node(0))
    for index in range(calls):
        store.append_task_model_call(
            task_id=TASK_ID,
            node_id=NODE_ID,
            created_at=f'2026-10-01T10:{index % 60:02d}:00+08:00',
            payload={
                'call_index': index,
                'node_id': NODE_ID,
                'call_kind': 'normal',
                'prepared_message_count': 3,
                'delta_usage': {'tracked': True, 'input_tokens': 10 + index, 'output_tokens': 2},
            },
        )
    query_service = TaskQueryService(
        store=store, file_store=log_service._file_store, log_service=log_service,
    )
    return store, query_service


def test_snapshot_default_carries_a_bounded_window_and_the_true_count(tmp_path: Path) -> None:
    total = _TASK_SNAPSHOT_MODEL_CALL_ROWS + 5
    store, query_service = _services(tmp_path, calls=total)

    payload = query_service.get_task_snapshot(TASK_ID, mark_read=False)

    assert payload is not None
    carried = payload['recent_model_calls']
    assert len(carried) == _TASK_SNAPSHOT_MODEL_CALL_ROWS
    # 带回来的必须是最新的那一窗，不是最旧的
    assert carried[-1]['call_index'] == total - 1
    assert carried[0]['call_index'] == total - _TASK_SNAPSHOT_MODEL_CALL_ROWS
    assert payload['summary']['total_model_calls'] == total
    assert payload['counts']['total_model_calls'] == total


def test_the_ledger_read_never_asks_for_the_whole_task(tmp_path: Path) -> None:
    """读口的每一次调用都必须带 SQL 侧 LIMIT——None 才是那个 18.4 s 的来源。"""
    _store, query_service = _services(tmp_path, calls=3)
    seen: list[object] = []
    original = query_service._store.list_task_model_calls

    def spying(task_id: str, *, limit: int | None = 50):
        seen.append(limit)
        return original(task_id, limit=limit)

    query_service._store.list_task_model_calls = spying  # type: ignore[method-assign]

    query_service.get_task_snapshot(TASK_ID, mark_read=False)
    query_service.get_task_snapshot(TASK_ID, mark_read=False, model_call_limit=None)

    assert seen, '快照没有读调用账本'
    assert all(isinstance(item, int) and item >= 1 for item in seen), seen


def test_explicit_limits_still_apply(tmp_path: Path) -> None:
    total = _TASK_SNAPSHOT_MODEL_CALL_ROWS + 5
    _store, query_service = _services(tmp_path, calls=total)

    small = query_service.get_task_snapshot(TASK_ID, mark_read=False, model_call_limit=2)
    none = query_service.get_task_snapshot(TASK_ID, mark_read=False, model_call_limit=0)
    wide = query_service.get_task_snapshot(TASK_ID, mark_read=False, model_call_limit=400)

    assert [item['call_index'] for item in small['recent_model_calls']] == [total - 2, total - 1]
    assert none['recent_model_calls'] == []
    assert len(wide['recent_model_calls']) == total
    assert wide['summary']['total_model_calls'] == total


def test_count_matches_the_full_listing(tmp_path: Path) -> None:
    store, _query_service = _services(tmp_path, calls=7)
    assert store.count_task_model_calls(TASK_ID) == len(store.list_task_model_calls(TASK_ID, limit=None)) == 7
    assert store.count_task_model_calls('task:missing') == 0
