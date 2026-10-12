"""慢读两档留痕：<200 ms 不报；200–800 ms 每 (表,调用方) 只报首见；>=800 ms 每次都报并带毫秒。

第二档存在的理由：首见行的时间戳只代表"这对组合第一次越过阈值"，把它当事件时刻会误归因；
要按时刻对上一次卡顿，必须有带毫秒、每次都说的那一行。
"""

import pytest
from loguru import logger

from main.storage import sqlite_store as store_mod


class _LaneProbe:
    """只借 _with_reader_lane 需要的成员，不连数据库；去重集各自独立，测试之间不串。"""

    _SLOW_READ_MS = store_mod.SQLiteTaskStore._SLOW_READ_MS
    _SLOW_READ_EVENT_MS = store_mod.SQLiteTaskStore._SLOW_READ_EVENT_MS
    _with_reader_lane = store_mod.SQLiteTaskStore._with_reader_lane

    def __init__(self) -> None:
        self._slow_read_lanes: set[str] = set()

    def _outer_caller(self) -> str:
        return store_mod.SQLiteTaskStore._outer_caller()


@pytest.fixture()
def lines():
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(message.record["message"]), format="{message}", level="INFO")
    yield captured
    logger.remove(sink_id)


def test_read_under_threshold_is_silent(lines) -> None:
    probe = _LaneProbe()
    assert probe._with_reader_lane('sqlite.query.fetchall:nodes', 150.0) == 'sqlite.query.fetchall:nodes'
    assert lines == []


def test_first_tier_reports_each_new_pair_once(lines) -> None:
    probe = _LaneProbe()
    assert probe._SLOW_READ_MS <= 300.0 < probe._SLOW_READ_EVENT_MS
    annotated = probe._with_reader_lane('sqlite.query.fetchall:tasks', 300.0)
    assert annotated.startswith('sqlite.query.fetchall:tasks[from=')
    assert len(lines) == 1 and 'new reader lane' in lines[0]
    probe._with_reader_lane('sqlite.query.fetchall:tasks', 400.0)
    assert len(lines) == 1, '同一 (表, 调用方) 第二档不该重复报首见'
    probe._with_reader_lane('sqlite.query.fetchone:tasks', 300.0)
    assert len(lines) == 2, '换了表要算新组合'


def test_second_tier_reports_every_time_with_ms(lines) -> None:
    probe = _LaneProbe()
    probe._with_reader_lane('sqlite.query.fetchall:task_node_details', 900.0)
    probe._with_reader_lane('sqlite.query.fetchall:task_node_details', 3400.0)
    assert len(lines) == 2, '>=事件档必须每次都留，首见去重不能吞掉它'
    assert 'took 900 ms' in lines[0] and 'took 3400 ms' in lines[1]
    assert 'task_node_details' in lines[0] and 'from=' in lines[1]
