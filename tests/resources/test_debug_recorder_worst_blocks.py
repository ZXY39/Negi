"""长块记录器必须留得住"稀有但致命"的那一块。

只留最近 N 条那一本在实盘不够用：`get_task_snapshot` 的 210–543 ms 噪声块以每秒级频率
出现，lag 采到 8,169 ms 的那一分钟，榜上最高只剩 543 ms——秒级归因就此断线。
"""

from __future__ import annotations

from main.runtime.debug_recorder import RuntimeDebugRecorder


def test_noise_does_not_evict_the_rare_multi_second_block():
    recorder = RuntimeDebugRecorder(max_entries=4, threshold_ms=200.0, worst_entries=4)
    recorder.record(section='query_service.get_task_snapshot', elapsed_ms=430.0, started_at='t1')
    recorder.record(section='log_service.update_frame', elapsed_ms=8169.0, started_at='t2')
    for index in range(10):
        recorder.record(section='query_service.get_task_snapshot', elapsed_ms=260.0, started_at=f'n{index}')

    recent = [item['section'] for item in recorder.snapshot()]
    assert 'log_service.update_frame' not in recent
    worst = recorder.worst_snapshot()
    assert worst[0]['section'] == 'log_service.update_frame'
    assert worst[0]['elapsed_ms'] == 8169.0
    assert worst[0]['started_at'] == 't2'


def test_worst_list_keeps_longest_and_drops_shortest():
    recorder = RuntimeDebugRecorder(max_entries=8, threshold_ms=200.0, worst_entries=3)
    for elapsed in (300.0, 900.0, 500.0, 700.0, 400.0):
        recorder.record(section=f'section:{elapsed}', elapsed_ms=elapsed)
    assert [item['elapsed_ms'] for item in recorder.worst_snapshot()] == [900.0, 700.0, 500.0]


def test_threshold_still_gates_both_lists():
    recorder = RuntimeDebugRecorder(threshold_ms=200.0)
    recorder.record(section='fast', elapsed_ms=199.0)
    assert recorder.snapshot() == []
    assert recorder.worst_snapshot() == []


def test_recent_list_keeps_arrival_order():
    recorder = RuntimeDebugRecorder(max_entries=3, threshold_ms=200.0)
    for elapsed in (250.0, 900.0, 300.0):
        recorder.record(section=f'section:{elapsed}', elapsed_ms=elapsed)
    assert [item['elapsed_ms'] for item in recorder.snapshot()] == [250.0, 900.0, 300.0]
