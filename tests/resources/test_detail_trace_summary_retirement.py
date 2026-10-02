"""明细行不再内联整份轨迹摘要：可重算的那一份没有家，只有 ref。

实盘 `task:1d9cddf9858e`：`task_node_details` 989 行 / **71.0 MB**，其中
`payload.execution_trace_summary` 占 **60.4 MB（85%）**，而 **65 MB 落在永不再改写的终态行**
上——任何整任务详情读都得连它一起扫（token 汇总那一次实测 **793.8 ms**，它真正要的
`token_usage_by_model` 只有 **710.5 KB**）。摘要本身是可重算的：构造函数作用在
外置 artifact 上与行内存量那份逐字节相同（见 `test_execution_trace_summary_compaction.py`）。

树上的阶段文案因此换家：`task_nodes` 投影整任务 1.26 MB / 8.3 ms，对比整任务明细 71 MB。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


from main.models import NodeRecord
from main.monitoring.log_service import _latest_execution_stage_goal
from main.monitoring.models import TaskProjectionNodeDetailRecord, TaskProjectionNodeRecord
from main.monitoring.query_service import TaskQueryService
from main.protocol import now_iso
from main.runtime.execution_trace_compaction import build_execution_trace_summary
from main.service.runtime_service import MainRuntimeService
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:retire'


class _StubChatBackend:
    async def complete(self, *args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError('chat backend must not be called in this test')


def _summary(stages: list[dict]) -> dict:
    return {'stages': stages}


def _legacy_detail_payload(summary: dict) -> dict:
    return {
        'goal': 'do the thing',
        'execution_trace_summary': summary,
        'token_usage_by_model': [{'model_key': 'm1', 'input_tokens': 7, 'output_tokens': 3}],
    }


def _open_store(path: Path) -> SQLiteTaskStore:
    return SQLiteTaskStore(path)


def _seed_legacy_rows(store: SQLiteTaskStore) -> None:
    """按迁移前的形状写：明细行内联摘要，投影行没有 latest_stage_goal。"""
    stamp = now_iso()
    summaries = {
        'node:a': _summary([
            {'stage_id': 'stage:1', 'stage_index': 1, 'stage_goal': 'oldest goal', 'rounds': []},
            {'stage_id': 'stage:3', 'stage_index': 3, 'stage_goal': 'newest goal', 'rounds': []},
            {'stage_id': 'stage:2', 'stage_index': 2, 'stage_goal': '', 'rounds': []},
        ]),
        'node:b': _summary([]),
        'node:c': _summary([
            {'stage_id': 'stage:x', 'stage_index': 4, 'stage_goal': 'goal from x', 'rounds': []},
            {'stage_id': 'stage:y', 'stage_index': 4, 'stage_goal': 'goal from y', 'rounds': []},
        ]),
    }
    for node_id, summary in summaries.items():
        store.upsert_task_node_detail(
            TaskProjectionNodeDetailRecord(
                node_id=node_id,
                task_id=TASK_ID,
                updated_at=stamp,
                payload=_legacy_detail_payload(summary),
            )
        )
        store.upsert_task_node(
            TaskProjectionNodeRecord(
                node_id=node_id,
                task_id=TASK_ID,
                root_node_id=node_id,
                updated_at=stamp,
                payload={'latest_stage_goal': ''},
            )
        )


def _pretend_written_before_this_version(path: Path) -> None:
    """把库退回"这份代码之前写成的样子"：迁移闸门是 `PRAGMA user_version`。

    新建库在第一次打开时就已经是目标版本（那条 UPDATE 一条都没命中），所以测试要模拟
    实盘那种老文件——version 0 + 明细行内联摘要——才能真的走到迁移。
    """
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA user_version = 0')


def test_reopening_the_store_retires_inline_summaries_and_stamps_goals(tmp_path: Path):
    path = tmp_path / 'runtime.sqlite3'
    store = _open_store(path)
    try:
        _seed_legacy_rows(store)
    finally:
        store.close()
    _pretend_written_before_this_version(path)

    store = _open_store(path)
    try:
        goals = {
            str(item.node_id): str(dict(item.payload or {}).get('latest_stage_goal') or '')
            for item in store.list_task_nodes(TASK_ID)
        }
        assert goals == {'node:a': 'newest goal', 'node:b': '', 'node:c': 'goal from y'}
        remaining = store._fetchall(  # noqa: SLF001 - 只读账本
            "SELECT COUNT(*) AS c FROM task_node_details "
            "WHERE json_extract(payload_json, '$.payload.execution_trace_summary') IS NOT NULL",
            (),
        )[0]
        assert int(remaining['c']) == 0
        # 同一次打开里其它键必须原样留着：删的是那一份副本，不是整行
        detail = store.get_task_node_detail('node:a')
        assert detail is not None
        payload = dict(detail.payload or {})
        assert payload['goal'] == 'do the thing'
        assert payload['token_usage_by_model'][0]['model_key'] == 'm1'
    finally:
        store.close()


def test_second_open_does_nothing(tmp_path: Path):
    """闸门读 PRAGMA user_version，不扫明细表：判"要不要跑"不许再为那 71 MB 买单。"""
    path = tmp_path / 'runtime.sqlite3'
    store = _open_store(path)
    try:
        _seed_legacy_rows(store)
    finally:
        store.close()

    store = _open_store(path)
    version = int(store._conn.execute('PRAGMA user_version').fetchone()[0])  # noqa: SLF001
    store.close()
    assert version == SQLiteTaskStore._DETAIL_TRACE_SUMMARY_RETIREMENT_VERSION  # noqa: SLF001

    stamp = now_iso()
    with sqlite3.connect(path) as conn:
        conn.execute(
            'INSERT INTO task_node_details (node_id, task_id, updated_at, payload_json) VALUES (?, ?, ?, ?)',
            (
                'node:late',
                TASK_ID,
                stamp,
                json.dumps({
                    'node_id': 'node:late',
                    'task_id': TASK_ID,
                    'updated_at': stamp,
                    'payload': _legacy_detail_payload(_summary([{'stage_id': 'stage:9', 'stage_index': 9, 'stage_goal': 'late goal', 'rounds': []}])),
                }),
            ),
        )

    store = _open_store(path)
    try:
        row = store.get_task_node_detail('node:late')
        assert row is not None
        # 版本已到位 ⇒ 不再重扫；新写侧本来就不内联，这一行只是历史数据的形状样本
        assert 'execution_trace_summary' in dict(row.payload or {})
    finally:
        store.close()


def test_sql_goal_rule_matches_the_writer_helper():
    """补戳语句与写侧取值必须同口径：`(stage_index, stage_id)` 最大且 goal 非空。

    node:c 那两行 stage_index 相同，只有 stage_id 能定胜负——SQL 与 Python 不一致时
    存量补出来的标签会和新写出来的同节点不同名。
    """
    node = NodeRecord(
        node_id='node:c',
        task_id=TASK_ID,
        root_node_id=TASK_ID,
        status='success',
        goal='do the thing',
        prompt='p',
        created_at=now_iso(),
        updated_at=now_iso(),
        metadata={
            'execution_stages': {
                'stages': [
                    {'stage_id': 'stage:x', 'stage_index': 4, 'stage_goal': 'goal from x'},
                    {'stage_id': 'stage:y', 'stage_index': 4, 'stage_goal': 'goal from y'},
                ]
            }
        },
    )

    assert _latest_execution_stage_goal(node) == 'goal from y'


def test_projection_written_by_the_service_carries_the_stage_goal(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_StubChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    node = NodeRecord(
        node_id='node:proj',
        task_id='task:proj',
        root_node_id='task:proj',
        status='in_progress',
        goal='do the thing',
        prompt='p',
        created_at=now_iso(),
        updated_at=now_iso(),
        metadata={
            'execution_stages': {
                'stages': [
                    {'stage_id': 'stage:1', 'stage_index': 1, 'stage_goal': 'first'},
                    {'stage_id': 'stage:2', 'stage_index': 2, 'stage_goal': 'second'},
                ]
            }
        },
    )
    detail = service.log_service._task_projection_node_detail_record(node)  # noqa: SLF001
    projection = service.log_service._task_projection_node_record(node)  # noqa: SLF001

    assert 'execution_trace_summary' not in dict(detail.payload or {})
    assert dict(projection.payload or {})['latest_stage_goal'] == 'second'


def test_detail_api_still_returns_the_same_summary_document(tmp_path: Path):
    """删掉存量那一份之后，面板读到的必须还是同一份文档（按 ref 现算）。"""
    service = MainRuntimeService(
        chat_backend=_StubChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )

    async def _boot():
        return await service.create_task('摘要换家', session_id='web:shared')

    import asyncio

    record = asyncio.run(_boot())
    node_id = record.root_node_id
    service.log_service.sync_node_read_model(record.task_id, node_id, externalize_execution_trace=True)

    stored = service.store.get_task_node_detail(node_id)
    assert stored is not None
    assert 'execution_trace_summary' not in dict(stored.payload or {})

    payload = service.get_node_detail_payload(record.task_id, node_id, detail_level='summary')
    assert payload is not None
    served = payload['item']['execution_trace_summary']

    ref = str(stored.execution_trace_ref or dict(stored.payload or {}).get('execution_trace_ref') or '').strip()
    resolved = service.log_service.resolve_content_ref(ref) if ref else ''
    trace = json.loads(resolved) if resolved else {}
    expected = TaskQueryService._sanitize_execution_trace_summary(build_execution_trace_summary(trace))

    assert json.dumps(served, ensure_ascii=False, sort_keys=True) == json.dumps(expected, ensure_ascii=False, sort_keys=True)
