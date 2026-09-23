"""终态 outbox 投递可靠性单测（P0）：'abandoned' 留痕第三态、pending 取数口径、
delivered 保留 last_error、60s 常驻补投、在途并发上限。

对应 2026-09-23 生产事故：`task:543e0f15d798` 的终态事件在 16:55 四发投递全部超时后
停在 pending —— 那时唯一的补投点在进程 startup，于是压到 18:34 重启才吐出来，作为
一条陈旧心跳事件唤醒会话、自动向用户回了一条关于已废弃结果的"考古汇报"。
历史另一条 `task:7e2a270eec34` 压了 10h16m，同型。

裁决口径（见 docs/FIX_PLAN_heartbeat-silent-tool-and-terminal-outbox.md §2.0）：
**推送与否归模型**，所以这里没有"事件太旧就不投"的时效闸门 —— 陈旧行照投，
只把"永久滞留到下次重启"降级为"≤60s 自愈"。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from main.service.runtime_service import (
    _TASK_TERMINAL_DELIVERY_MAX_INFLIGHT,
    _TASK_TERMINAL_OUTBOX_ATTEMPT_LIMIT,
    MainRuntimeService,
)


class _DummyChatBackend:
    async def chat(self, **kwargs):
        return SimpleNamespace(content='', tool_calls=[], finish_reason='stop', usage={})


def _make_worker_service(tmp_path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(timespec='seconds')


def _put_terminal_row(service: MainRuntimeService, *, task_id: str, finished_at: str) -> str:
    dedupe_key = f'task-terminal:{task_id}:success:{finished_at}'
    service.store.put_task_terminal_outbox(
        dedupe_key=dedupe_key,
        task_id=task_id,
        session_id='ext:qq-official:demo',
        created_at=finished_at,
        payload={'dedupe_key': dedupe_key, 'task_id': task_id, 'session_id': 'ext:qq-official:demo', 'status': 'success'},
    )
    return dedupe_key


def test_attempt_limit_lands_abandoned_and_keeps_forensic_trace(tmp_path):
    service = _make_worker_service(tmp_path)
    dedupe_key = _put_terminal_row(service, task_id='task:stuck', finished_at=_iso(0))

    for _ in range(_TASK_TERMINAL_OUTBOX_ATTEMPT_LIMIT):
        service.store.mark_task_terminal_outbox_attempt(
            dedupe_key,
            attempted_at=_iso(0),
            error_text='task_terminal_callback_http_503',
            attempt_limit=_TASK_TERMINAL_OUTBOX_ATTEMPT_LIMIT,
        )

    entry = service.store.get_task_terminal_outbox(dedupe_key)
    assert entry is not None
    assert entry['delivery_state'] == 'abandoned'
    # 留痕不删行：反查"哪一行卡了多久、错在哪"全靠这两个字段
    assert int(entry['attempts']) == _TASK_TERMINAL_OUTBOX_ATTEMPT_LIMIT
    assert entry['last_error'] == 'task_terminal_callback_http_503'


def test_abandoned_row_stops_being_picked_up_while_pending_stays_visible(tmp_path):
    """常驻补投的取数口径必须排除 'abandoned'，否则被判否的行会每 60s 撞一次。"""
    service = _make_worker_service(tmp_path)
    abandoned = _put_terminal_row(service, task_id='task:given_up', finished_at=_iso(60))
    live = _put_terminal_row(service, task_id='task:retry_me', finished_at=_iso(60))
    service.store.mark_task_terminal_outbox_attempt(
        abandoned,
        attempted_at=_iso(0),
        error_text='task_terminal_callback_url_unavailable',
        attempt_limit=1,
    )

    keys = {str(entry.get('dedupe_key') or '') for entry in service.store.list_pending_task_terminal_outbox(limit=50)}
    assert keys == {live}


def test_very_old_pending_row_is_still_offered_to_replay(tmp_path):
    """没有时效闸门：10 小时前的 pending 行照样进补投队列，由模型决定说或不说。"""
    service = _make_worker_service(tmp_path)
    ten_hours = _iso(10 * 3600)
    dedupe_key = _put_terminal_row(service, task_id='task:decades_old', finished_at=ten_hours)

    keys = [str(entry.get('dedupe_key') or '') for entry in service.store.list_pending_task_terminal_outbox(limit=50)]
    assert keys == [dedupe_key]


def test_delivered_row_preserves_the_error_it_hit_on_the_way(tmp_path):
    """取证事故时拿不到原始错误，就是因为 delivered 会把 last_error 抹成空串。"""
    service = _make_worker_service(tmp_path)
    dedupe_key = _put_terminal_row(service, task_id='task:late_win', finished_at=_iso(0))
    service.store.mark_task_terminal_outbox_attempt(
        dedupe_key,
        attempted_at=_iso(0),
        error_text='ReadTimeout',
        attempt_limit=_TASK_TERMINAL_OUTBOX_ATTEMPT_LIMIT,
    )
    service.store.mark_task_terminal_outbox_delivered(dedupe_key, delivered_at=_iso(0))

    entry = service.store.get_task_terminal_outbox(dedupe_key)
    assert entry is not None
    assert entry['delivery_state'] == 'delivered'
    assert entry['last_error'] == 'ReadTimeout'


def test_worker_redrives_a_stranded_pending_row_without_waiting_for_restart(tmp_path):
    """startup 之外必须有一个常驻驱动点 —— 这正是 1h39m 滞留的成因。"""
    service = _make_worker_service(tmp_path)
    dedupe_key = _put_terminal_row(service, task_id='task:stranded', finished_at=_iso(3600))

    scheduled: list[str] = []
    service._schedule_task_terminal_delivery = lambda key: scheduled.append(str(key))

    service._schedule_pending_task_terminal_callbacks()

    assert scheduled == [dedupe_key]


class _StopTick(BaseException):
    """从被 patch 的 sleep 里退出无限循环；派生自 BaseException 才不会被循环体的
    ``except Exception: pass`` 吞掉。"""


@pytest.mark.asyncio
async def test_distribution_reconcile_tick_also_redrives_terminal_outbox(tmp_path, monkeypatch):
    """60s 节拍接入点：不能只在 startup 补投，也不必新起一条循环。"""
    service = _make_worker_service(tmp_path)
    dedupe_key = _put_terminal_row(service, task_id='task:tick', finished_at=_iso(7200))
    scheduled: list[str] = []
    service._schedule_task_terminal_delivery = lambda key: scheduled.append(str(key))

    async def _noop_reconcile():
        return None

    service.task_actor_service.reconcile_distribution_drivers = _noop_reconcile

    ticks = 0

    async def _stop_after_one_full_tick(_seconds: float):
        nonlocal ticks
        ticks += 1
        # 第一次 sleep 必须正常返回，否则本轮的两件事都被跳过（sleep 在 try 之前）
        if ticks >= 2:
            raise _StopTick

    monkeypatch.setattr('main.service.runtime_service.asyncio.sleep', _stop_after_one_full_tick)

    with pytest.raises(_StopTick):
        await service._distribution_reconcile_loop()

    assert ticks == 2
    assert scheduled == [dedupe_key]


@pytest.mark.asyncio
async def test_delivery_inflight_cap_blocks_a_restart_storm(tmp_path, monkeypatch):
    """web 长时间不可达时，pending 行不得全部长挂成上百个 task。"""
    service = _make_worker_service(tmp_path)
    for index in range(_TASK_TERMINAL_DELIVERY_MAX_INFLIGHT + 5):
        _put_terminal_row(service, task_id=f'task:storm-{index}', finished_at=_iso(0))

    monkeypatch.setattr('main.service.runtime_service.Path.cwd', lambda: tmp_path)

    async def _parking_deliver(_dedupe_key: str) -> None:
        return None

    service._deliver_task_terminal_outbox = _parking_deliver
    service._schedule_pending_task_terminal_callbacks()

    inflight = dict(service._task_terminal_delivery_tasks)
    assert len(inflight) == _TASK_TERMINAL_DELIVERY_MAX_INFLIGHT
    for task in inflight.values():
        task.cancel()
