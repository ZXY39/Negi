"""工具入参只有一份持久家：投影表 `task_node_tool_results`，不是节点行。

`append_node_output` 每拍都要重写整行，历史条目里的 `tool_calls[].arguments` 跟着
一起被读回再抄回去——实盘在跑的任务 task:1d9cddf9858e 在 output 里内联了 52,449 条
调用的入参共 54.0 MB，而这批条目自己的正文只有 2.25 MB。现在行内最多留「最新一拍」
的入参（那一拍的投影行要等工具执行回来才写），旧条目在下一拍追加时被摘掉。

本文件钉住四条：
- 追加下一拍后，上一拍条目不再带 arguments，但 id/name 原样保留；
- 摘掉之后读侧仍从投影行取到逐字节相同的入参文本（界面不降级）；
- 行内那份只在「刚写完、还没执行回来」的这一拍存在，且只有一份；
- 迁移前的存量行（arguments 内联、没有投影行）照旧读得到。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from main.monitoring.execution_trace import build_execution_trace
from main.protocol import now_iso
from main.service.runtime_service import MainRuntimeService

_FAT_ARG = 'a' * 4096


class _StubChatBackend:
    """这些用例不打模型，只需要服务能起来。"""

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


def _calls(round_index: int) -> list[dict[str, object]]:
    return [
        {
            'id': f'call:{round_index}:1',
            'name': 'filesystem',
            'arguments': {'path': f'/tmp/round-{round_index}', 'content': _FAT_ARG},
        }
    ]


def _mark_worker_online(service: MainRuntimeService) -> None:
    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )


async def _task(service: MainRuntimeService):
    _mark_worker_online(service)
    return await service.create_task('入参单份存储', session_id='web:shared')


def _record_tool_result(service: MainRuntimeService, *, task_id: str, node_id: str, call: dict[str, object]) -> None:
    stamp = now_iso()
    service.log_service.record_tool_result_batch(
        task_id=task_id,
        node_id=node_id,
        response_tool_calls=[
            SimpleNamespace(id=call['id'], name=call['name'], arguments=call['arguments']),
        ],
        results=[
            {
                'live_state': {
                    'tool_call_id': call['id'],
                    'tool_name': call['name'],
                    'status': 'success',
                    'started_at': stamp,
                    'finished_at': stamp,
                    'elapsed_seconds': 0.1,
                },
                'tool_message': {
                    'role': 'tool',
                    'tool_call_id': call['id'],
                    'name': call['name'],
                    'content': 'ok',
                    'status': 'success',
                },
            }
        ],
    )


@pytest.mark.asyncio
async def test_next_append_strips_previous_round_arguments(tmp_path: Path):
    service = _service(tmp_path)
    record = await _task(service)
    node_id = record.root_node_id

    service.log_service.append_node_output(record.task_id, node_id, content='第一拍', tool_calls=_calls(1))
    first = service.store.get_node(node_id)
    assert first is not None
    assert 'arguments' in first.output[-1].tool_calls[0]

    service.log_service.append_node_output(record.task_id, node_id, content='第二拍', tool_calls=_calls(2))
    node = service.store.get_node(node_id)
    assert node is not None and len(node.output) == 2
    old, newest = node.output[0], node.output[1]
    assert 'arguments' not in old.tool_calls[0]
    assert old.tool_calls[0] == {'id': 'call:1:1', 'name': 'filesystem'}
    assert 'arguments' in newest.tool_calls[0]


@pytest.mark.asyncio
async def test_arguments_body_does_not_accumulate_in_the_row(tmp_path: Path):
    service = _service(tmp_path)
    record = await _task(service)
    node_id = record.root_node_id

    for index in range(1, 7):
        service.log_service.append_node_output(record.task_id, node_id, content=f'第{index}拍', tool_calls=_calls(index))
        _record_tool_result(
            service,
            task_id=record.task_id,
            node_id=node_id,
            call={'id': f'call:{index}:1', 'name': 'filesystem', 'arguments': _calls(index)[0]['arguments']},
        )

    raw = json.dumps(service.store.get_node(node_id).model_dump(mode='json'), ensure_ascii=False)
    # 正文只出现在「最新一拍」一次；旧拍只剩抬头，正文的家在投影行。
    assert raw.count(_FAT_ARG) == 1


@pytest.mark.asyncio
async def test_trace_reads_arguments_from_projection_row_after_stripping(tmp_path: Path):
    service = _service(tmp_path)
    record = await _task(service)
    node_id = record.root_node_id
    call = _calls(1)[0]

    service.log_service.append_node_output(record.task_id, node_id, content='第一拍', tool_calls=[call])
    _record_tool_result(service, task_id=record.task_id, node_id=node_id, call=call)
    service.log_service.append_node_output(record.task_id, node_id, content='第二拍', tool_calls=_calls(2))

    node = service.store.get_node(node_id)
    assert node is not None
    assert 'arguments' not in node.output[0].tool_calls[0]

    rows = list(service.store.list_task_node_tool_results(record.task_id, node_id) or [])
    trace = build_execution_trace(node, tool_results=rows, live_tool_calls=[])
    steps = {item['tool_call_id']: item for item in trace['tool_steps']}
    # 摘掉行内正文之后，界面读到的入参文本与摘之前逐字节相同。
    assert steps['call:1:1']['arguments_text'] == json.dumps(call['arguments'], ensure_ascii=False, indent=2)


@pytest.mark.asyncio
async def test_latest_round_renders_arguments_from_inline_entry(tmp_path: Path):
    service = _service(tmp_path)
    record = await _task(service)
    node_id = record.root_node_id
    call = _calls(1)[0]

    service.log_service.append_node_output(record.task_id, node_id, content='在飞的一拍', tool_calls=[call])
    node = service.store.get_node(node_id)
    assert node is not None
    trace = build_execution_trace(node, tool_results=[], live_tool_calls=[])
    step = trace['tool_steps'][0]
    assert step['arguments_text'] == json.dumps(call['arguments'], ensure_ascii=False, indent=2)


def test_legacy_inline_entry_without_projection_row_still_renders():
    from main.models import NodeOutputEntry, NodeRecord

    arguments = {'path': '/tmp/legacy'}
    entry = NodeOutputEntry(
        seq=1,
        content='旧的一拍',
        tool_calls=[{'id': 'call:legacy', 'name': 'exec', 'arguments': arguments}],
        created_at=now_iso(),
    )
    node = NodeRecord(
        node_id='node:legacy',
        task_id='task:legacy',
        root_node_id='node:legacy',
        goal='g',
        prompt='p',
        status='success',
        output=[entry],
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    trace = build_execution_trace(node, tool_results=[], live_tool_calls=[])
    assert trace['tool_steps'][0]['arguments_text'] == json.dumps(arguments, ensure_ascii=False, indent=2)
