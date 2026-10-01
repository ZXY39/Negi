from __future__ import annotations

from pathlib import Path
from types import MethodType, SimpleNamespace

from main.models import NodeRecord, TaskRecord, TokenUsageSummary
from main.monitoring.file_store import TaskFileStore
from main.monitoring.log_service import TaskLogService
from main.monitoring.query_service import (
    _LIVE_FRAME_CACHE_MAX_TASKS,
    TaskQueryService,
)
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:liveframecache'


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


def _node_record(task_id: str, node_id: str) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        task_id=task_id,
        parent_node_id=None,
        root_node_id=node_id,
        depth=0,
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


def _frame(node_id: str, *, goal: str) -> dict:
    return {
        'node_id': node_id,
        'depth': 0,
        'node_kind': 'execution',
        'phase': 'running',
        'active': False,
        'runnable': True,
        'waiting': False,
        'stage_goal': goal,
        'stage_status': '进行中',
        'tool_calls': [{'tool_call_id': 'call-1', 'tool_name': 'filesystem_read', 'status': 'running'}],
        'child_pipelines': [],
    }


def _harness(tmp_path: Path, *, node_ids: tuple[str, ...] = ('node:a', 'node:b', 'node:c'), task_id: str = TASK_ID):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    store.upsert_task(_task_record(task_id))
    for node_id in node_ids:
        store.upsert_node(_node_record(task_id, node_id))
    log_service = TaskLogService(
        store=store,
        file_store=TaskFileStore(tmp_path / 'files'),
        registry=None,
        event_history_enabled=False,
    )
    for node_id in node_ids:
        log_service.upsert_frame(task_id, _frame(node_id, goal=f'goal-{node_id}'))
    query_service = TaskQueryService(store=store, file_store=log_service._file_store, log_service=log_service)
    return store, log_service, query_service


def _count_payload_reads(query_service: TaskQueryService) -> list[str]:
    reads: list[str] = []
    original = query_service._store.get_task_runtime_frame

    def counting(task_id: str, node_id: str):
        reads.append(str(node_id))
        return original(task_id, node_id)

    query_service._store.get_task_runtime_frame = counting  # type: ignore[method-assign]
    return reads


def test_second_snapshot_reads_no_frame_payload(tmp_path) -> None:
    store, _log_service, query_service = _harness(tmp_path)

    first = query_service._projection_live_state(TASK_ID)
    reads = _count_payload_reads(query_service)
    second = query_service._projection_live_state(TASK_ID)

    assert first is not None and second is not None
    assert [item.model_dump() for item in first.frames] == [item.model_dump() for item in second.frames]
    assert [item.stage_goal for item in second.frames] == ['goal-node:a', 'goal-node:b', 'goal-node:c']
    assert reads == []
    assert set(store.list_task_runtime_frame_heads(TASK_ID)[0].keys()) >= {'payload_digest'}


def test_a_changed_frame_is_read_back_and_the_others_are_not(tmp_path) -> None:
    _store, log_service, query_service = _harness(tmp_path)
    query_service._projection_live_state(TASK_ID)
    log_service.update_frame(TASK_ID, 'node:b', lambda frame: {**frame, 'stage_goal': 'changed'})
    # 计数器要在变更之后装：`update_frame` 自己会回读那一帧一次，混进来会把判据糊掉。
    reads = _count_payload_reads(query_service)
    state = query_service._projection_live_state(TASK_ID)

    assert state is not None
    assert reads == ['node:b']
    assert [item.stage_goal for item in state.frames] == ['goal-node:a', 'changed', 'goal-node:c']


def test_head_only_columns_are_re_stamped_without_a_payload_read(tmp_path) -> None:
    """抬头列（phase/active/depth/node_kind）每拍从帧头重算：只改这些列不许被缓存吃掉。"""
    store, _log_service, query_service = _harness(tmp_path)
    before = query_service._projection_live_state(TASK_ID)
    assert before is not None and 'node:a' in before.active_node_ids
    reads = _count_payload_reads(query_service)

    with store._conn:
        store._conn.execute(
            "UPDATE task_runtime_frames SET phase='waiting_external', active=0, depth=7 "
            "WHERE task_id=? AND node_id='node:a'",
            (TASK_ID,),
        )
        store._conn.commit()

    state = query_service._projection_live_state(TASK_ID)

    assert state is not None
    assert reads == []
    frame = next(item for item in state.frames if item.node_id == 'node:a')
    assert frame.phase == 'waiting_external'
    assert frame.depth == 7
    assert 'node:a' not in state.active_node_ids
    # 没被改过的两帧仍从缓存取，正文一个字节都不该再读（排序按帧头 depth 走，node:a 被推到末位）
    assert {item.node_id: item.stage_goal for item in state.frames} == {
        'node:a': 'goal-node:a',
        'node:b': 'goal-node:b',
        'node:c': 'goal-node:c',
    }


def test_dropped_frames_leave_the_cache(tmp_path) -> None:
    store, _log_service, query_service = _harness(tmp_path)
    query_service._projection_live_state(TASK_ID)
    assert len(query_service._live_frame_cache[TASK_ID]) == 3

    with store._conn:
        store._conn.execute("DELETE FROM task_runtime_frames WHERE task_id=? AND node_id='node:b'", (TASK_ID,))
        store._conn.commit()
    query_service._projection_live_state(TASK_ID)

    assert set(query_service._live_frame_cache[TASK_ID]) == {'node:a', 'node:c'}


def test_cache_is_bounded_per_task(tmp_path) -> None:
    _store, _log_service, query_service = _harness(tmp_path)
    task_ids = [f'task:bound{index}' for index in range(_LIVE_FRAME_CACHE_MAX_TASKS + 3)]
    for task_id in task_ids:
        query_service._store.upsert_task(_task_record(task_id))
        query_service._store.upsert_node(_node_record(task_id, 'node:a'))
        query_service._log_service.upsert_frame(task_id, _frame('node:a', goal='g'))
        query_service._projection_live_state(task_id)

    assert len(query_service._live_frame_cache) <= _LIVE_FRAME_CACHE_MAX_TASKS
    assert task_ids[0] not in query_service._live_frame_cache


def test_prune_asks_the_store_for_the_node_scoped_rows(tmp_path) -> None:
    """actual-request 剪枝每次模型调用都走一遍：不许把整任务的 artifact 建成模型。"""
    calls: list[str] = []

    class _ArtifactStore:
        def list_artifacts(self, task_id: str):
            calls.append('list_artifacts')
            return []

        def list_artifacts_for_node(self, task_id: str, node_id):
            calls.append('list_artifacts_for_node')
            return []

    service = SimpleNamespace(
        _content_store=SimpleNamespace(_artifact_store=_ArtifactStore()),
        _store=SimpleNamespace(delete_artifacts_by_ids=lambda ids: 0),
    )

    MethodType(TaskLogService._prune_stale_actual_request_artifacts, service)(
        task_id=TASK_ID, node_id='node:a', keep_artifact_id='artifact:keep'
    )

    assert calls == ['list_artifacts_for_node']
