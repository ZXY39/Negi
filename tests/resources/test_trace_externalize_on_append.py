"""P-A：整份轨迹 artifact 只在"会被读"或"不再变"时存在，追加不再抢跑。

不变量：`task_execution_trace` 是 (task,node) 单例，旧写法每次可见输出都把整份重写一遍
（实盘 1,012 份里 702 份 >256 KB、均值 721.7 KB；墙钟里 gzip 的 `_write_raw` 是最大叶帧 1.84 s），
而读方用的是行内 `execution_trace_summary`。读侧本来就有按需重建那条路
（`query_service.py:726-737` 与 `_resolve_execution_trace` 的 fallback）。

关键不是"少写一次盘"，而是**外置与 ref 必须一起处理**：只关外置、留着旧 ref，
`full` 详情会照旧 ref 读到过期轨迹。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from main.protocol import now_iso
from main.service.runtime_service import MainRuntimeService

_FAT = 'y' * 40_000


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


async def _boot(tmp_path: Path):
    service = _service(tmp_path)
    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )
    record = await service.create_task('轨迹外置', session_id='web:shared')
    return service, record


def _trace_rows(service: MainRuntimeService, node_id: str) -> list[tuple]:
    return list(
        service.store._fetchall(  # noqa: SLF001 - 只读账本
            "SELECT artifact_id, json_extract(payload_json,'$.kind') kind "
            "FROM artifacts WHERE node_id = ? AND json_extract(payload_json,'$.kind')='task_execution_trace'",
            (node_id,),
        )
    )


def _detail_ref(service: MainRuntimeService, task_id: str, node_id: str) -> str:
    record = service.store.get_task_node_detail(node_id)
    assert record is not None
    return str(record.execution_trace_ref or '').strip()


@pytest.mark.asyncio
async def test_append_invalidates_ref_and_writes_no_trace_artifact(tmp_path: Path):
    service, record = await _boot(tmp_path)
    node_id = record.root_node_id
    # 建任务时那份照旧存在（今天连空轨迹都会落一份，最小 577 B），本刀管的是"追加不再重写它"。
    before = len(_trace_rows(service, node_id))

    for index in range(3):
        service.log_service.append_node_output(
            record.task_id,
            node_id,
            content=f'第{index}拍输出 ' + _FAT,
            tool_calls=[{'id': f'call:{index}', 'name': 'exec', 'arguments': {'command': f'echo {index}'}}],
        )

    assert _detail_ref(service, record.task_id, node_id) == ''
    assert len(_trace_rows(service, node_id)) == before


@pytest.mark.asyncio
async def test_status_transition_still_produces_one_artifact(tmp_path: Path):
    service, record = await _boot(tmp_path)
    node_id = record.root_node_id
    service.log_service.append_node_output(record.task_id, node_id, content='一拍', tool_calls=[])

    service.log_service.update_node_status(record.task_id, node_id, status='success', final_output='干完了')

    ref = _detail_ref(service, record.task_id, node_id)
    assert ref.startswith('artifact:'), '状态转换是"最后一次不再变"，必须留下真快照'
    assert len(_trace_rows(service, node_id)) == 1


@pytest.mark.asyncio
async def test_full_detail_still_returns_trace_without_a_ref(tmp_path: Path):
    """ref 作废不等于读不到：`full` 必须走按需重建，读到的是当前内容。"""
    service, record = await _boot(tmp_path)
    node_id = record.root_node_id
    service.log_service.append_node_output(
        record.task_id,
        node_id,
        content='在飞的一拍',
        tool_calls=[{'id': 'call:live', 'name': 'exec', 'arguments': {'command': 'echo live'}}],
    )
    assert _detail_ref(service, record.task_id, node_id) == ''

    payload = service.get_node_detail_payload(record.task_id, node_id, detail_level='full')
    assert payload is not None
    trace = payload['item'].get('execution_trace') or {}
    steps = list(trace.get('tool_steps') or [])
    assert [item.get('tool_call_id') for item in steps] == ['call:live']
    assert 'echo live' in str(steps[0].get('arguments_text') or '')


@pytest.mark.asyncio
async def test_summary_detail_keeps_inline_summary_after_append(tmp_path: Path):
    """界面读的是行内摘要：作废 ref 不能把它一起弄没。"""
    service, record = await _boot(tmp_path)
    node_id = record.root_node_id
    service.log_service.append_node_output(
        record.task_id,
        node_id,
        content='带工具的一拍',
        tool_calls=[{'id': 'call:sum', 'name': 'exec', 'arguments': {'command': 'ls'}}],
    )
    payload = service.get_node_detail_payload(record.task_id, node_id, detail_level='summary')
    assert payload is not None
    assert 'execution_trace_summary' in payload['item']
    assert 'execution_trace' not in payload['item']
