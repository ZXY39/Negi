from __future__ import annotations

from main.service.runtime_service import (
    _TASK_EVENT_QUEUE_MAX_BYTES,
    _TASK_EVENT_QUEUE_MAX_ITEMS,
    MainRuntimeService,
)

_NO_LOOP = object()


class _Queue:
    """只带 `_enqueue_task_event_callback` 真正用到的那几样状态与方法。"""

    _find_task_event_coalesce_slot = staticmethod(
        MainRuntimeService.__dict__['_find_task_event_coalesce_slot'].__func__
        if hasattr(MainRuntimeService.__dict__['_find_task_event_coalesce_slot'], '__func__')
        else MainRuntimeService.__dict__['_find_task_event_coalesce_slot']
    )
    _task_event_queue_pressure = MainRuntimeService._task_event_queue_pressure
    _note_task_event_drop = MainRuntimeService._note_task_event_drop

    def __init__(self) -> None:
        self.worker_id = 'worker:test'
        self._task_event_pending: list[dict] = []
        self._task_event_flush_task = None
        self._task_event_avg_item_bytes: dict[str, int] = {}
        self._task_event_dropped_by_type: dict[str, int] = {}
        self._task_event_heavy_types: set[str] = set()
        self._task_event_drop_logged_mono = 0.0
        self._task_event_stats = {
            'task_event_queued_count': 0.0,
            'task_event_coalesced_count': 0.0,
            'task_event_dropped_count': 0.0,
            'task_event_dropped_coalesced_count': 0.0,
            'task_event_dropped_other_count': 0.0,
        }

    def _ensure_task_event_flush_task(self, loop) -> None:
        return None

    def enqueue(self, payload: dict) -> None:
        # loop 在本文件里没人读（上面的桩件吃掉它），所以给一个显式假值：
        # `asyncio.get_event_loop()` 依赖线程上的环境循环，同进程里前一个 asyncio
        # 用例关掉循环后它会抛 RuntimeError。
        MainRuntimeService._enqueue_task_event_callback(self, payload, _NO_LOOP)


def test_byte_budget_evicts_the_oldest_superseded_patch_first() -> None:
    """一条 live.patch 就是"该任务当前全量状态"：超预算时先扔最旧的那份，
    而不是把新到的审计事实丢掉。"""
    service = _Queue()
    service._task_event_avg_item_bytes = {'task.live.patch': _TASK_EVENT_QUEUE_MAX_BYTES * 3 // 5}
    # 排空口学到 MB 级载荷后会把该类型记成重类型，字节水位因此不必等到 256 条才开始算
    service._task_event_heavy_types = {'task.live.patch'}
    service.enqueue({'event_type': 'task.live.patch', 'task_id': 'task:a'})
    service.enqueue({'event_type': 'task.live.patch', 'task_id': 'task:b'})

    service.enqueue({'event_type': 'task.node.patch', 'task_id': 'task:a'})

    types = [item['event_type'] for item in service._task_event_pending]
    assert 'task.node.patch' in types
    assert types.count('task.live.patch') == 1
    assert service._task_event_pending[0]['task_id'] == 'task:b'
    assert service._task_event_stats['task_event_dropped_coalesced_count'] == 1.0
    assert service._task_event_stats['task_event_dropped_other_count'] == 0.0
    assert service._task_event_dropped_by_type == {'task.live.patch': 1}


def test_item_cap_still_drops_the_newest_non_terminal(monkeypatch) -> None:
    """没有可合类可扔时维持原语义：丢最新一条非终态事件，但要能点名类型。"""
    logged: list[str] = []

    class _Recorder:
        def warning(self, message, *args):
            logged.append(str(message).format(*args) if args else str(message))

    monkeypatch.setattr('main.service.runtime_service.logger', _Recorder())
    service = _Queue()
    for index in range(_TASK_EVENT_QUEUE_MAX_ITEMS):
        service._task_event_pending.append({'event_type': 'task.node.patch', 'task_id': f'task:{index}'})

    service.enqueue({'event_type': 'task.model.call', 'task_id': 'task:late'})

    assert len(service._task_event_pending) == _TASK_EVENT_QUEUE_MAX_ITEMS
    assert service._task_event_stats['task_event_dropped_other_count'] == 1.0
    assert service._task_event_dropped_by_type == {'task.model.call': 1}
    assert logged and 'task.model.call' in logged[0] and 'estimated_bytes' in logged[0]


def test_terminal_event_buys_a_slot_instead_of_being_dropped() -> None:
    """终态事件被丢 = 任务永不收口，所以它只能挤位置，不能被丢。"""
    service = _Queue()
    for index in range(_TASK_EVENT_QUEUE_MAX_ITEMS):
        service._task_event_pending.append({'event_type': 'task.node.patch', 'task_id': f'task:{index}'})

    service.enqueue({'event_type': 'task.terminal', 'task_id': 'task:a'})

    assert service._task_event_pending[-1]['event_type'] == 'task.terminal'
    assert service._task_event_stats['task_event_dropped_count'] == 1.0


def test_pressure_scan_reports_composition_and_estimate() -> None:
    service = _Queue()
    service._task_event_avg_item_bytes = {'task.live.patch': 5000}
    pending = [
        {'event_type': 'task.live.patch'},
        {'event_type': 'task.live.patch'},
        {'event_type': 'task.node.patch'},
    ]

    estimated, counts = MainRuntimeService._task_event_queue_pressure(service, pending)

    # 没学过均值的类型按 _TASK_EVENT_UNKNOWN_ITEM_BYTES(8 KB) 计
    assert estimated == 5000 * 2 + 8192
    assert counts == {'task.live.patch': 2, 'task.node.patch': 1}


def test_drain_learns_the_average_payload_size() -> None:
    service = _Queue()
    big = {'event_type': 'task.live.patch', 'task_id': 'task:a', 'runtime_summary': {'frames': [{'x': 'y' * 200}]}}
    service._task_event_pending.append(big)

    batch = MainRuntimeService._drain_task_event_batch(service)

    assert [item for item in batch] == [big]
    assert service._task_event_avg_item_bytes['task.live.patch'] > 200
