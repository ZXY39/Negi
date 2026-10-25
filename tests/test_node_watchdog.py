"""P6 watchdog: structural pause criteria + root-only heartbeat selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from main.models import NodeRecord, TaskRecord
from main.runtime.node_watchdog import PARKED_PHASES, watchdog_pause_reason
from main.service.runtime_service import MainRuntimeService

TASK = 'task:watchdog'
ROOT = 'node:root'
NOW = datetime(2026, 10, 24, 3, 0, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(minutes=30)).isoformat()
RECENT = (NOW - timedelta(seconds=30)).isoformat()
STAMP = '2026-10-24T02:30:00+00:00'


def _call(
    *,
    tool: str = 'web_fetch',
    arguments: str = '{"url":"https://example.com"}',
    status: str = 'success',
    finished_at: str = RECENT,
):
    return SimpleNamespace(
        tool_name=tool,
        arguments_text=arguments,
        status=status,
        finished_at=finished_at,
    )


def test_repeated_identical_call_pauses() -> None:
    reason = watchdog_pause_reason(
        frame={'phase': 'before_model', 'tool_calls': []},
        tool_results=[_call(), _call(), _call()],
        now=NOW,
        stall_seconds=600,
        last_event_at=STAMP,
    )
    assert reason == 'repeated_identical_tool_call'


def test_varied_args_not_paused() -> None:
    reason = watchdog_pause_reason(
        frame={'phase': 'before_model', 'tool_calls': []},
        tool_results=[
            _call(arguments='{"url":"https://a.example"}'),
            _call(arguments='{"url":"https://b.example"}'),
            _call(arguments='{"url":"https://c.example"}'),
        ],
        now=NOW,
        stall_seconds=600,
        last_event_at=RECENT,
    )
    assert reason == ''


def test_tool_error_streak_pauses() -> None:
    reason = watchdog_pause_reason(
        frame={'phase': 'before_model', 'tool_calls': []},
        tool_results=[
            _call(status='error'),
            _call(status='error'),
            _call(status='error'),
        ],
        now=NOW,
        stall_seconds=600,
        last_event_at=STAMP,
    )
    assert reason == 'tool_error_streak'


def test_waiting_nodes_not_stalled() -> None:
    for phase in PARKED_PHASES:
        reason = watchdog_pause_reason(
            frame={'phase': phase, 'tool_calls': []},
            tool_results=[],
            now=NOW,
            stall_seconds=600,
            last_event_at=OLD,
        )
        assert reason == '', phase


def test_running_tool_is_not_paused_cooperatively() -> None:
    reason = watchdog_pause_reason(
        frame={'phase': 'waiting_tool_results', 'tool_calls': [{'status': 'running'}]},
        tool_results=[],
        now=NOW,
        stall_seconds=600,
        last_event_at=OLD,
    )
    assert reason == ''


def test_no_new_events_pauses() -> None:
    reason = watchdog_pause_reason(
        frame={'phase': 'before_model', 'tool_calls': []},
        tool_results=[],
        now=NOW,
        stall_seconds=600,
        last_event_at=OLD,
    )
    assert reason == 'no_new_events'


def test_model_turn_in_flight_is_not_paused() -> None:
    reason = watchdog_pause_reason(
        frame={
            'phase': 'before_model',
            'tool_calls': [],
            'await_marker': 'model.chat.await_response',
        },
        tool_results=[],
        now=NOW,
        stall_seconds=600,
        last_event_at=OLD,
    )
    assert reason == ''


def test_route_lease_wait_is_not_paused() -> None:
    reason = watchdog_pause_reason(
        frame={
            'phase': 'before_model',
            'tool_calls': [],
            'await_marker': 'node_turn.acquire',
        },
        tool_results=[],
        now=NOW,
        stall_seconds=600,
        last_event_at=OLD,
    )
    assert reason == ''


def test_model_retry_is_not_paused() -> None:
    reason = watchdog_pause_reason(
        frame={
            'phase': 'before_model',
            'tool_calls': [],
            'await_marker': 'model.chat.await_response',
            'model_retry_status': {'state': 'retrying', 'retry_count': 3},
        },
        tool_results=[],
        now=NOW,
        stall_seconds=600,
        last_event_at=OLD,
    )
    assert reason == ''


def test_waiting_upstream_within_stall_window_is_not_paused() -> None:
    reason = watchdog_pause_reason(
        frame={
            'phase': 'before_model',
            'tool_calls': [],
            'await_marker': 'model.chat.await_response',
            'model_retry_status': {'state': 'waiting_upstream', 'waiting_seconds': 120},
        },
        tool_results=[],
        now=NOW,
        stall_seconds=600,
        last_event_at=OLD,
    )
    assert reason == ''


def test_waiting_upstream_past_stall_window_pauses() -> None:
    reason = watchdog_pause_reason(
        frame={
            'phase': 'before_model',
            'tool_calls': [],
            'await_marker': 'model.chat.await_response',
            'model_retry_status': {'state': 'waiting_upstream', 'waiting_seconds': 600},
        },
        tool_results=[],
        now=NOW,
        stall_seconds=600,
        last_event_at=RECENT,
    )
    assert reason == 'no_new_events'


def _service(tmp_path: Path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=SimpleNamespace(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )


def test_stall_pauses_node_and_records_history(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        service.store.upsert_task(
            TaskRecord(
                task_id=TASK,
                title='t',
                user_request='u',
                root_node_id=ROOT,
                status='in_progress',
                created_at=old,
                updated_at=old,
            )
        )
        service.store.upsert_node(
            NodeRecord(
                node_id=ROOT,
                task_id=TASK,
                parent_node_id='',
                root_node_id=ROOT,
                depth=0,
                node_kind='execution',
                goal='root',
                prompt='p',
                status='in_progress',
                created_at=old,
                updated_at=old,
            )
        )
        service.log_service.update_frame(
            TASK,
            ROOT,
            lambda frame: {**dict(frame or {}), 'phase': 'before_model'},
        )

        paused = service.node_watchdog.scan_task(TASK)

        assert paused == [ROOT]
        node = service.store.get_node(ROOT)
        assert node.status == 'in_progress'
        assert node.is_paused is True
        assert node.pause_reason == 'error'
        history = list((node.metadata or {}).get('watchdog_history') or [])
        assert history and history[-1]['reason'] == 'no_new_events'
    finally:
        import asyncio

        asyncio.run(service.close())


def test_scan_skips_in_flight_model_turn(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        service.store.upsert_task(
            TaskRecord(
                task_id=TASK,
                title='t',
                user_request='u',
                root_node_id=ROOT,
                status='in_progress',
                created_at=old,
                updated_at=old,
            )
        )
        service.store.upsert_node(
            NodeRecord(
                node_id=ROOT,
                task_id=TASK,
                parent_node_id='',
                root_node_id=ROOT,
                depth=0,
                node_kind='execution',
                goal='root',
                prompt='p',
                status='in_progress',
                created_at=old,
                updated_at=old,
            )
        )
        service.log_service.update_frame(
            TASK,
            ROOT,
            lambda frame: {
                **dict(frame or {}),
                'phase': 'before_model',
                'await_marker': 'model.chat.await_response',
                'model_retry_status': {'state': 'retrying', 'retry_count': 3},
            },
        )

        paused = service.node_watchdog.scan_task(TASK)

        assert paused == []
        node = service.store.get_node(ROOT)
        assert node.is_paused is False
        assert not list((node.metadata or {}).get('watchdog_history') or [])
    finally:
        import asyncio

        asyncio.run(service.close())


def test_heartbeat_selects_root_only(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        child = 'node:child'
        service.store.upsert_task(
            TaskRecord(
                task_id=TASK,
                title='t',
                user_request='u',
                root_node_id=ROOT,
                status='in_progress',
                created_at=STAMP,
                updated_at=OLD,
            )
        )
        for node_id, parent in ((ROOT, ''), (child, ROOT)):
            service.store.upsert_node(
                NodeRecord(
                    node_id=node_id,
                    task_id=TASK,
                    parent_node_id=parent,
                    root_node_id=ROOT,
                    depth=0 if not parent else 1,
                    node_kind='execution',
                    goal=node_id,
                    prompt='p',
                    status='in_progress',
                    created_at=STAMP,
                    updated_at=OLD,
                )
            )
        for node_id in (ROOT, child):
            service.log_service.set_node_pause_state(
                TASK,
                node_id,
                pause_requested=True,
                is_paused=True,
                pause_reason='error',
                remark='test',
            )

        rows = service.store.list_new_error_pauses()

        assert [row['node_id'] for row in rows] == [ROOT]
    finally:
        import asyncio

        asyncio.run(service.close())
