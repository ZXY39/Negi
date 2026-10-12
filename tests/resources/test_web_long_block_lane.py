"""web 进程自己的长块账本要从 /api/tasks/worker-status 读得到。

worker 那份在 `payload['debug']` 里，说的是任务进程在忙什么；浏览器这次请求等的是
web 进程。缺这一本时，一次 3.4 秒的详情请求只能看到"没有任何一条读超过 800 ms"，
答不出时间在谁身上。
"""

import asyncio

from main.api import rest


class _StubRecorder:
    def __init__(self) -> None:
        self.recent = [{'section': 'sqlite.query.fetchall:nodes', 'elapsed_ms': 1234.5, 'started_at': 'x'}]
        self.worst = [{'section': 'query_service.get_task_snapshot', 'elapsed_ms': 3410.0, 'started_at': 'y'}]

    def snapshot(self):
        return list(self.recent)

    def worst_snapshot(self):
        return list(self.worst)


class _StubService:
    def __init__(self) -> None:
        self.runtime_debug_recorder = _StubRecorder()

    async def startup(self) -> None:
        return None

    def worker_status_payload(self) -> dict[str, object]:
        return {'worker': {'status': 'running'}, 'session_id': 'web:shared'}


def test_worker_status_carries_this_process_long_blocks(monkeypatch) -> None:
    service = _StubService()
    monkeypatch.setattr(rest, '_service', lambda: service)

    payload = asyncio.run(rest.get_task_worker_status())

    assert payload['ok'] is True
    assert payload['worker'] == {'status': 'running'}, '原有 worker 载荷不能被覆盖'
    assert payload['session_id'] == 'web:shared'
    assert payload['web_long_blocks'] == service.runtime_debug_recorder.recent
    assert payload['web_worst_long_blocks'] == service.runtime_debug_recorder.worst


def test_long_block_rows_keep_the_fields_triage_needs(monkeypatch) -> None:
    monkeypatch.setattr(rest, '_service', lambda: _StubService())
    payload = asyncio.run(rest.get_task_worker_status())

    for key in ('web_long_blocks', 'web_worst_long_blocks'):
        rows = payload[key]
        assert rows, f'{key} 不该是空的'
        for row in rows:
            assert set(row) == {'section', 'elapsed_ms', 'started_at'}, f'{key} 字段口径变了：{row}'
