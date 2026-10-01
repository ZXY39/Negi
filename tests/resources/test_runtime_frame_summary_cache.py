from __future__ import annotations

import sqlite3
from pathlib import Path

from main.models import NodeRecord, TaskRecord, TokenUsageSummary
from main.monitoring.file_store import TaskFileStore
from main.monitoring.log_service import (
    _SUMMARY_FRAME_CACHE_MAX_TASKS,
    TaskLogService,
    frame_is_stale,
)
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:summarycache'


def _task_record(task_id: str) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id='web:shared',
        title='demo',
        user_request='demo',
        status='in_progress',
        root_node_id='node:a',
        max_depth=1,
        created_at='2026-10-01T10:00:00+08:00',
        updated_at='2026-10-01T10:00:00+08:00',
        token_usage=TokenUsageSummary(tracked=True),
        metadata={},
    )


def _node_record(task_id: str, node_id: str, *, depth: int = 0) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        task_id=task_id,
        parent_node_id=None,
        root_node_id=node_id,
        depth=depth,
        node_kind='execution',
        status='in_progress',
        goal='demo',
        prompt='demo',
        input='demo',
        output=[],
        check_result='',
        final_output='',
        can_spawn_children=False,
        created_at='2026-10-01T10:00:00+08:00',
        updated_at='2026-10-01T10:00:00+08:00',
        token_usage=TokenUsageSummary(tracked=True),
    )


def _frame(node_id: str, *, goal: str, phase: str = 'running', runnable: bool = True) -> dict:
    return {
        'node_id': node_id,
        'depth': 0,
        'node_kind': 'execution',
        'phase': phase,
        'active': False,
        'runnable': runnable,
        'waiting': False,
        'stage_goal': goal,
        'stage_status': '进行中',
        'tool_calls': [],
    }


def _service(tmp_path: Path) -> TaskLogService:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    store.upsert_task(_task_record(TASK_ID))
    for node_id in ('node:a', 'node:b', 'node:c'):
        store.upsert_node(_node_record(TASK_ID, node_id))
    return TaskLogService(store=store, file_store=TaskFileStore(tmp_path / 'files'), registry=None, event_history_enabled=False)


def _reference_summary(service: TaskLogService) -> dict:
    """按"逐行读正文并装配"的旧口径算出摘要，用来证明缓存输出逐字一致。"""
    records = list(service._store.list_task_runtime_frames(TASK_ID))
    meta = service.read_task_runtime_meta(TASK_ID) or service._default_runtime_meta()
    return {
        'active_node_ids': [r.node_id for r in records if bool(r.active)],
        'runnable_node_ids': [r.node_id for r in records if bool(r.runnable)],
        'waiting_node_ids': [r.node_id for r in records if bool(r.waiting)],
        'dispatch_limits': dict(meta.get('dispatch_limits') or {}),
        'dispatch_running': dict(meta.get('dispatch_running') or {}),
        'dispatch_queued': dict(meta.get('dispatch_queued') or {}),
        'governance': dict(meta.get('governance') or {}),
        'distribution': dict(meta.get('distribution') or {}),
        'frames': [
            service._public_runtime_frame({**dict(r.payload or {}), 'stale': frame_is_stale(r.updated_at)})
            for r in records
        ],
    }


def test_cached_summary_matches_row_by_row_reference(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for node_id in ('node:a', 'node:b', 'node:c'):
        service.upsert_frame(TASK_ID, _frame(node_id, goal=f'goal-{node_id}'))

    cached = service._runtime_summary_payload(TASK_ID)
    cached_again = service._runtime_summary_payload(TASK_ID)

    assert cached == _reference_summary(service)
    assert cached_again == cached
    assert [item['stage_goal'] for item in cached['frames']] == ['goal-node:a', 'goal-node:b', 'goal-node:c']


def test_unchanged_frames_are_not_read_back(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for node_id in ('node:a', 'node:b', 'node:c'):
        service.upsert_frame(TASK_ID, _frame(node_id, goal=f'goal-{node_id}'))
    service._runtime_summary_payload(TASK_ID)

    reads: list[str] = []
    original = service._store.get_task_runtime_frame

    def counting(task_id: str, node_id: str):
        reads.append(str(node_id))
        return original(task_id, node_id)

    service._store.get_task_runtime_frame = counting  # type: ignore[method-assign]
    # 变更本身要读回那一帧（update_frame 的前置读），所以先把这次读记完再清空计数：
    # 断言的是"组装摘要时只回表变化的那一帧"。
    service.update_frame(TASK_ID, 'node:b', lambda frame: {**frame, 'stage_goal': 'changed'})
    reads.clear()

    summary = service._runtime_summary_payload(TASK_ID)

    assert reads == ['node:b']
    goals = {item['node_id']: item['stage_goal'] for item in summary['frames']}
    assert goals == {'node:a': 'goal-node:a', 'node:b': 'changed', 'node:c': 'goal-node:c'}
    assert summary == _reference_summary(service)


def test_rows_without_digest_are_never_served_from_cache(tmp_path: Path) -> None:
    """迁移前的存量行 payload_digest 为空串，不能被当成"没变"。"""
    service = _service(tmp_path)
    service.upsert_frame(TASK_ID, _frame('node:a', goal='first'))
    service._runtime_summary_payload(TASK_ID)

    connection = sqlite3.connect(str(service._store.path))
    try:
        connection.execute(
            "UPDATE task_runtime_frames SET payload_digest = '', payload_json = ? WHERE task_id = ? AND node_id = 'node:a'",
            (
                service._store.list_task_runtime_frames(TASK_ID)[0].model_dump_json().replace('first', 'rewritten'),
                TASK_ID,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    summary = service._runtime_summary_payload(TASK_ID)

    assert summary['frames'][0]['stage_goal'] == 'rewritten'


def test_cleared_ledger_drops_the_task_cache(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.upsert_frame(TASK_ID, _frame('node:a', goal='first'))
    assert service._runtime_summary_payload(TASK_ID)['frames']

    service._store.replace_task_runtime_frames(TASK_ID, [])

    assert service._runtime_summary_payload(TASK_ID)['frames'] == []
    assert TASK_ID not in service._summary_frame_cache


def test_cache_holds_only_a_handful_of_tasks(tmp_path: Path) -> None:
    service = _service(tmp_path)
    store = service._store
    for index in range(_SUMMARY_FRAME_CACHE_MAX_TASKS + 3):
        task_id = f'task:many{index}'
        store.upsert_task(_task_record(task_id))
        store.upsert_node(_node_record(task_id, 'node:a'))
        service.upsert_frame(task_id, _frame('node:a', goal='g'))
        service._runtime_summary_payload(task_id)

    assert len(service._summary_frame_cache) <= _SUMMARY_FRAME_CACHE_MAX_TASKS
