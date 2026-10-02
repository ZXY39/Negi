"""按模型 token 明细的记忆化：口径不变，只把"每次快照重解全表"变成按需。

`_projection_token_usage_by_model` 要用 JSON1 把该任务全部 `task_node_details.payload_json`
解一遍（实盘在跑任务 938 行 / 61.07 MB），wall 采样里同一条栈累计 10.5 s / 180 s。
它不能改成"任务行上存一份"：spawn 评审那条车道的用量只进节点、不进任务总量，
换成任务级 rollup 会系统性少算面板读数。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from main import models as main_models
from main.monitoring import query_service as query_service_module
from main.protocol import now_iso
from main.service.runtime_service import MainRuntimeService


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


def _node(task_id: str, node_id: str, input_tokens: int, *, updated_at: str) -> main_models.NodeRecord:
    return main_models.NodeRecord(
        node_id=node_id,
        task_id=task_id,
        root_node_id=node_id,
        status='success',
        goal=node_id,
        prompt='p',
        created_at=updated_at,
        updated_at=updated_at,
        token_usage={'tracked': True, 'input_tokens': input_tokens, 'call_count': 1},
        token_usage_by_model=[
            {
                'model_key': 'm-test',
                'provider_id': 'p',
                'provider_model': 'pm',
                'tracked': True,
                'input_tokens': input_tokens,
                'output_tokens': 1,
                'call_count': 1,
                'calls_with_usage': 1,
            }
        ],
    )


async def _task_with_projected_nodes(service: MainRuntimeService, tmp_path: Path):
    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )
    record = await service.create_task('token 记忆化', session_id='web:shared')
    stamp = '2026-10-02T10:00:00+08:00'
    service.store.upsert_node(_node(record.task_id, 'node:one', 100, updated_at=stamp))
    service.log_service.sync_node_read_model(record.task_id, 'node:one')
    return record


def _spy_compute(service: MainRuntimeService) -> list[int]:
    queries = service.query_service
    calls: list[int] = []
    original = queries._compute_projection_token_usage_by_model

    def _spy(task_id: str):
        items = original(task_id)
        calls.append(len(items))
        return items

    queries._compute_projection_token_usage_by_model = _spy  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
async def test_repeated_reads_compute_once_while_stamp_is_unchanged(tmp_path: Path):
    service = _service(tmp_path)
    record = await _task_with_projected_nodes(service, tmp_path)
    calls = _spy_compute(service)

    first = service.query_service._projection_token_usage_by_model(record.task_id)
    second = service.query_service._projection_token_usage_by_model(record.task_id)
    assert len(calls) == 1
    # 记忆化不改读数：缓存里那份与真实聚合逐字段相同。
    assert [item.model_dump(mode='json') for item in second] == [
        item.model_dump(mode='json') for item in first
    ]
    assert first[0].input_tokens == 100


@pytest.mark.asyncio
async def test_newer_node_write_invalidates(tmp_path: Path):
    service = _service(tmp_path)
    record = await _task_with_projected_nodes(service, tmp_path)
    calls = _spy_compute(service)
    service.query_service._projection_token_usage_by_model(record.task_id)
    assert len(calls) == 1

    service.store.upsert_node(
        _node(record.task_id, 'node:two', 250, updated_at='2026-10-02T10:05:00+08:00')
    )
    service.log_service.sync_node_read_model(record.task_id, 'node:two')
    items = service.query_service._projection_token_usage_by_model(record.task_id)
    assert len(calls) == 2
    assert items[0].input_tokens == 350


@pytest.mark.asyncio
async def test_same_second_write_is_bounded_by_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`updated_at` 只有秒级粒度：同秒原地改写读成同一个戳，靠 TTL 封顶。"""
    service = _service(tmp_path)
    record = await _task_with_projected_nodes(service, tmp_path)
    calls = _spy_compute(service)
    service.query_service._projection_token_usage_by_model(record.task_id)
    assert len(calls) == 1

    # 同一秒内原地改写同一个节点（行数不变、updated_at 不变）：戳读起来没动，仍吃缓存。
    service.store.update_node(
        'node:one',
        lambda r: r.model_copy(
            update={
                'token_usage_by_model': [
                    main_models.ModelTokenUsageRecord.model_validate(
                        {**r.token_usage_by_model[0].model_dump(mode='json'), 'input_tokens': 999}
                    )
                ]
            }
        ),
    )
    service.log_service.sync_node_read_model(record.task_id, 'node:one')
    items = service.query_service._projection_token_usage_by_model(record.task_id)
    assert len(calls) == 1
    assert items[0].input_tokens == 100  # 缓存里还是改写前的值

    # 超过 TTL 后必须重算，不能永远吃这份同秒旧值。
    clock = {'now': time.monotonic() + query_service_module._TASK_TOKEN_ROLLUP_MAX_AGE_SECONDS + 1}
    monkeypatch.setattr(query_service_module.time, 'monotonic', lambda: clock['now'])
    items = service.query_service._projection_token_usage_by_model(record.task_id)
    assert len(calls) == 2
    assert items[0].input_tokens == 999


@pytest.mark.asyncio
async def test_cache_is_bounded_per_task(tmp_path: Path):
    service = _service(tmp_path)
    record = await _task_with_projected_nodes(service, tmp_path)
    calls = _spy_compute(service)
    for index in range(query_service_module._TASK_TOKEN_ROLLUP_CACHE_MAX_TASKS + 2):
        task_id = f'task:cache-{index}'
        service.store.upsert_node(_node(task_id, f'node:{index}', 10, updated_at=now_iso()))
        service.query_service._projection_token_usage_by_model(task_id)
    assert len(service.query_service._token_rollup_cache) <= (
        query_service_module._TASK_TOKEN_ROLLUP_CACHE_MAX_TASKS
    )
    assert record.task_id not in service.query_service._token_rollup_cache or len(calls) >= 1
