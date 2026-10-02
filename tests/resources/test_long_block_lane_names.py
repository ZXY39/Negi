"""长块榜必须能报出车道名，不是只有表名。

判一条没插桩的车道只能靠 py-spy，而 wall 采样在这里已经用了两轮（`--gil` 看不见
sqlite step，它释放 GIL）。这块榜是排障时的第一现场，缺名字就等于每次都重新挂探针。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from main.protocol import now_iso
from main.service.runtime_service import MainRuntimeService
from main.storage.sqlite_store import SQLiteTaskStore


class _CapturingRecorder:
    def __init__(self) -> None:
        self.sections: list[str] = []

    def record(self, *, section: str, elapsed_ms: float, started_at: str | None = None) -> None:
        self.sections.append(section)


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


@pytest.mark.asyncio
async def test_append_and_snapshot_lanes_are_named(tmp_path: Path):
    service = _service(tmp_path)
    recorder = _CapturingRecorder()
    service.log_service._debug_recorder = recorder  # noqa: SLF001
    service.query_service._debug_recorder = recorder  # noqa: SLF001
    service.store._debug_recorder = recorder  # noqa: SLF001

    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )
    record = await service.create_task('车道命名', session_id='web:shared')
    service.log_service.append_node_output(
        record.task_id,
        record.root_node_id,
        content='一拍',
        tool_calls=[{'id': 'call:1', 'name': 'exec', 'arguments': {'command': 'ls'}}],
    )
    service.query_service.get_task_snapshot(record.task_id, mark_read=False)

    sections = set(recorder.sections)
    # 阈值以下（200 ms）的段本来就不入库，所以这里断言的是"这条车道被 track 过"：
    # 用 capture 记录器看 record() 有没有被拿正确的名字调用，不看榜上留没留。
    assert 'log_service.sync_node_read_models' in sections
    # 外置那一格带"是谁叫我"的车道名，没有它就只能再挂一次 py-spy 才能回答这个问题。
    writer_names = [name for name in sections if name.startswith('log_service.externalize_execution_trace[from=')]
    assert writer_names, sorted(sections)
    assert any('from=initialize_task' in name for name in writer_names), writer_names
    assert {
        'query_service.get_task_snapshot.live_state',
        'query_service.get_task_snapshot.root_node_detail',
        'query_service.get_task_snapshot.token_rollup',
    } <= sections


@pytest.mark.asyncio
async def test_task_level_rebuild_lane_is_named(tmp_path: Path):
    service = _service(tmp_path)
    recorder = _CapturingRecorder()
    service.log_service._debug_recorder = recorder  # noqa: SLF001
    service.log_service.sync_task_read_models('task:absent')
    assert 'log_service.sync_task_read_models' in set(recorder.sections)


def test_slow_reads_name_their_caller_and_fast_ones_do_not_pay(tmp_path: Path):
    """慢读带 `[from=<谁>]`；快读不付栈遍历的开销。

    `fetchone:tasks` 这种单行小查都能花 4 秒时，"哪张表"已经答不了问题：
    是大读在共享 `_read_lock` 上把它排住了，还是进程在缺页——两者修法不同。
    """
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        assert store._with_reader_lane('sqlite.query.fetchall:nodes', 120.0) == 'sqlite.query.fetchall:nodes'
        named = store._with_reader_lane('sqlite.query.fetchall:nodes', 900.0)
        assert named == 'sqlite.query.fetchall:nodes[from=test_slow_reads_name_their_caller_and_fast_ones_do_not_pay]', named
        # 同一个 (表, 车道) 只报一次首现日志：集合去重，不每拍刷日志。
        assert ('sqlite.query.fetchall:nodes:test_slow_reads_name_their_caller_and_fast_ones_do_not_pay'
                in store._slow_read_lanes)
        before = len(store._slow_read_lanes)
        store._with_reader_lane('sqlite.query.fetchall:nodes', 900.0)
        assert len(store._slow_read_lanes) == before
    finally:
        store.close()
