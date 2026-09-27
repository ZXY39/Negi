from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from main.models import TaskMessageDistributionEpoch
from main.protocol import now_iso
from main.runtime.task_actor_service import record_frozen_node_id
from main.storage.sqlite_store import SQLiteTaskStore


def _make_store(tmp_path: Path) -> SQLiteTaskStore:
    # 显式路径：绝不允许落到默认数据根（生产运行库）。
    return SQLiteTaskStore(tmp_path / "runtime.sqlite3")


def _fake_log_service(meta: dict) -> SimpleNamespace:
    writes: list[dict] = []

    def _read(task_id: str) -> dict:
        return dict(meta)

    def _update(task_id: str, **payload) -> dict:
        writes.append(dict(payload))
        distribution = payload.get('distribution')
        if isinstance(distribution, dict):
            meta['distribution'] = dict(distribution)
        return meta

    service = SimpleNamespace(
        read_task_runtime_meta=_read,
        update_task_runtime_meta=_update,
    )
    service.writes = writes  # type: ignore[attr-defined]
    return service


def _seed_epoch(store: SQLiteTaskStore) -> None:
    store.upsert_task_message_distribution_epoch(
        TaskMessageDistributionEpoch(
            epoch_id='epoch:001',
            task_id='task:demo',
            root_node_id='node:root',
            root_message='notice',
            state='barrier_draining',
            created_at=now_iso(),
            payload={'barrier_node_ids': ['node:root', 'node:a', 'node:b']},
        )
    )


def test_record_frozen_node_id_writes_epoch_and_meta_ledgers(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    try:
        _seed_epoch(store)
        log_service = _fake_log_service({'distribution': {'state': 'barrier_draining'}})

        assert record_frozen_node_id(
            store, log_service, task_id='task:demo', epoch_id='epoch:001', node_id='node:a',
        ) is True
        assert record_frozen_node_id(
            store, log_service, task_id='task:demo', epoch_id='epoch:001', node_id='node:b',
        ) is True

        saved = store.get_task_message_distribution_epoch('task:demo', 'epoch:001')
        assert saved is not None
        assert saved.payload.get('frozen_node_ids') == ['node:a', 'node:b']
        # 其它 distribution 字段不得被整包写回抹掉。
        assert log_service.writes[-1]['distribution']['state'] == 'barrier_draining'
        assert log_service.writes[-1]['distribution']['frozen_node_ids'] == ['node:a', 'node:b']
    finally:
        store.close()


def test_record_frozen_node_id_is_idempotent(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    try:
        _seed_epoch(store)
        log_service = _fake_log_service({'distribution': {}})

        assert record_frozen_node_id(
            store, log_service, task_id='task:demo', epoch_id='epoch:001', node_id='node:a',
        ) is True
        # 重复冻结不写库、不写 meta：11 个节点每波都可能重进，不能逐秒重复落盘。
        meta_writes_before = len(log_service.writes)
        assert record_frozen_node_id(
            store, log_service, task_id='task:demo', epoch_id='epoch:001', node_id='node:a',
        ) is False
        assert len(log_service.writes) == meta_writes_before

        saved = store.get_task_message_distribution_epoch('task:demo', 'epoch:001')
        assert saved is not None
        assert saved.payload.get('frozen_node_ids') == ['node:a']
    finally:
        store.close()


def test_record_frozen_node_id_tolerates_missing_epoch(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    try:
        log_service = _fake_log_service({'distribution': {}})
        # meta 与 epochs 表脱同步时不得抛错——冻结控制流必须原样走完。
        assert record_frozen_node_id(
            store, log_service, task_id='task:demo', epoch_id='epoch:missing', node_id='node:a',
        ) is False
        assert log_service.writes == []
    finally:
        store.close()


def test_distribution_sanitizer_passes_frozen_node_ids() -> None:
    from main.monitoring.log_service import TaskLogService

    cleaned = TaskLogService._sanitize_distribution_state(
        {'state': 'barrier_draining', 'blocked_node_ids': ['node:a'], 'frozen_node_ids': ['node:a', '', None]}
    )
    assert cleaned['frozen_node_ids'] == ['node:a']


def test_tree_snapshot_live_frames_carry_request_and_retry_fields() -> None:
    """快照车道必须与 live.patch 车道同字段。

    两条投影各自维护字段集是这类 bug 的根源：树快照落地会整体替换前端的帧索引，
    快照里缺的字段就等于在每次刷新时被抹掉（分发期间重试 toast 长期不显示即此因）。
    """
    from main.monitoring.log_service import TaskLogService
    from main.monitoring.models import TaskLiveFrame

    frame = {
        'node_id': 'node:a',
        'await_marker': 'model.chat.dispatch',
        'await_started_at': '2026-09-27T20:06:00+08:00',
        'model_retry_status': {'state': 'retrying', 'retry_count': 3},
    }
    public = TaskLogService._public_runtime_frame(frame)
    live = TaskLiveFrame.model_validate(
        {
            'node_id': 'node:a',
            'await_marker': public['await_marker'],
            'await_started_at': public['await_started_at'],
            'model_retry_status': public['model_retry_status'],
        }
    )
    assert live.await_started_at == '2026-09-27T20:06:00+08:00'
    assert (live.model_retry_status or {}).get('retry_count') == 3
