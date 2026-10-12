"""任务快照档：轨迹正文不再外发，摘要与 `latest_tool_calls_full` 照旧，并按写戳复用。

实盘 `task:1d9cddf9858e`：详情响应 1.82 MB 里 `root_node.execution_trace` 占 **1.42 MB**，
而摘要只要 85 KB；一次 `root_node_detail` 构造 449–780 ms，8 个并发详情把它顶到 **1082 ms**，
同期一个本该 34 ms 的轻请求花到 **403 ms**。正文是选中节点后走 `detail_level='full'` 另取的。
"""

import inspect
from types import SimpleNamespace

from main.monitoring.query_service import TaskQueryService

TASK_ID = 'task:snapshot-lane'
NODE_ID = 'node:root'


def _summary_with_rounds() -> dict:
    return {'stages': [{'stage_id': 'stage:1', 'stage_index': 1, 'rounds': [{'round_index': 1, 'tools': []}]}]}


def _resolved_trace() -> dict:
    return {
        'stages': [{'stage_id': 'stage:9', 'stage_index': 9, 'rounds': [{'round_index': 9, 'tools': []}]}],
        'tool_steps': [{
            'tool_name': 'exec',
            'status': 'success',
            'arguments_text': '{"command":"ls"}',
            'output_text': 'x' * 400,
        }],
    }


class _DetailRecord:
    node_id = NODE_ID
    task_id = TASK_ID
    execution_trace_ref = 'artifact:trace-ref'

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __getattr__(self, name: str) -> str:
        return ''


class _Store:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.record = _DetailRecord(payload)
        self.record.updated_at = '2026-10-13T01:00:00+08:00'

    def get_task(self, task_id):
        return SimpleNamespace(task_id=task_id, root_node_id=NODE_ID)

    def get_task_node_detail(self, node_id):
        return self.record

    def get_task_node(self, node_id):
        return None

    def get_node(self, node_id):
        return None

    def get_task_node_pause(self, node_id):
        return None


class _LogService:
    def resolve_node_input_text(self, runtime_node):  # pragma: no cover - runtime_node 恒为 None
        return ''


def _service(monkeypatch) -> tuple[TaskQueryService, list[str]]:
    payload = {
        'node_id': NODE_ID,
        'task_id': TASK_ID,
        'execution_trace_summary': _summary_with_rounds(),
        'execution_trace_ref': 'artifact:trace-ref',
        'token_usage_by_model': [],
    }
    service = TaskQueryService(store=_Store(payload), file_store=None, log_service=_LogService())
    calls: list[str] = []

    def _spy_resolve(**kwargs):
        calls.append('resolve')
        return _resolved_trace()

    monkeypatch.setattr(service, '_resolve_execution_trace', _spy_resolve)
    monkeypatch.setattr(service, '_node_message_list', lambda **kwargs: [])
    monkeypatch.setattr(service, '_hydrate_execution_trace_output_texts', lambda trace: trace)
    return service, calls


def test_default_lane_still_ships_the_trace_body(monkeypatch) -> None:
    service, calls = _service(monkeypatch)
    detail = service.get_node_detail(TASK_ID, NODE_ID)
    assert calls == ['resolve']
    assert detail.execution_trace.get('tool_steps'), '按节点道的行为不能被改动波及'


def test_snapshot_lane_drops_body_but_keeps_summary_contents(monkeypatch) -> None:
    service, calls = _service(monkeypatch)
    detail = service.get_node_detail(TASK_ID, NODE_ID, ship_execution_trace_body=False)

    assert calls == ['resolve'], '正文照旧解析——摘要里的 latest_tool_calls_full 依它而造'
    assert not detail.execution_trace, '正文不外发'
    summary = detail.execution_trace_summary or {}
    assert summary.get('stages'), '摘要仍在'
    assert [item.get('tool_name') for item in summary.get('latest_tool_calls_full') or []] == ['exec'], (
        'summary 档附带的「最新一批」完整入参/出参必须保持有货'
    )


def test_snapshot_detail_is_reused_until_the_record_stamp_moves(monkeypatch) -> None:
    service, calls = _service(monkeypatch)
    first = service._snapshot_root_node_detail(TASK_ID, NODE_ID)
    second = service._snapshot_root_node_detail(TASK_ID, NODE_ID)
    assert first is second, '同一戳直接复用，不再重算'
    assert len(calls) == 1

    service._store.record.updated_at = '2026-10-13T01:00:09+08:00'
    third = service._snapshot_root_node_detail(TASK_ID, NODE_ID)
    assert third is not first, '节点写戳动了必须重算'
    assert len(calls) == 2
    assert third and not third.get('execution_trace')


def test_snapshot_cache_honours_the_age_cap(monkeypatch) -> None:
    service, calls = _service(monkeypatch)
    service._snapshot_root_node_detail(TASK_ID, NODE_ID)
    stamp, dumped, built_at = service._snapshot_root_detail_cache[TASK_ID]
    service._snapshot_root_detail_cache[TASK_ID] = (stamp, dumped, built_at - 60.0)
    service._snapshot_root_node_detail(TASK_ID, NODE_ID)
    assert len(calls) == 2, '超过 TTL 要重算——秒级粒度下戳相同不等于没改'


def test_task_snapshot_uses_the_body_free_lane() -> None:
    # 哨兵：这条道一旦被"顺手改回去"，1.42 MB 正文与并发排队会一起回来。
    body = inspect.getsource(TaskQueryService.get_task_snapshot)
    assert '_snapshot_root_node_detail(' in body
    lane = inspect.getsource(TaskQueryService._snapshot_root_node_detail)
    assert 'ship_execution_trace_body=False' in lane
