"""事件循环停摆的独立线程观测：阈值转移、按会话取任务、同步投递与抓栈。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from main.models import TaskRecord
from main.runtime.tool_pressure_monitor import WorkerPressureMonitor
from main.service.runtime_service import MainRuntimeService
from main.service.task_stall_callback import normalize_task_stall_payload

STAMP = '2026-11-01T00:00:00+08:00'


def _service(tmp_path: Path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=SimpleNamespace(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='worker',
    )


def _task(
    task_id: str,
    *,
    session_id: str,
    status: str = 'in_progress',
    is_paused: bool = False,
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id=session_id,
        title=task_id,
        user_request='u',
        root_node_id=f'{task_id}:root',
        status=status,
        is_paused=is_paused,
        created_at=STAMP,
        updated_at=STAMP,
    )


def _monitor(*, alarms: list[float], stacks: list[float]) -> WorkerPressureMonitor:
    return WorkerPressureMonitor(
        controller=SimpleNamespace(),
        store=SimpleNamespace(),
        on_loop_stall=alarms.append,
        on_loop_stack=stacks.append,
    )


def test_observe_loop_stall_fires_once_per_episode_and_rearms() -> None:
    alarms: list[float] = []
    stacks: list[float] = []
    monitor = _monitor(alarms=alarms, stacks=stacks)

    assert monitor.observe_loop_stall(500) == (False, False)
    # 抓栈线 10 s，告警线 30 s：先用较低的那条留下现场。
    assert monitor.observe_loop_stall(11_000) == (False, True)
    assert monitor.observe_loop_stall(31_000) == (True, False)
    # 同一次停摆不重复发。
    assert monitor.observe_loop_stall(45_000) == (False, False)
    assert stacks == [11_000]
    assert alarms == [31_000]

    # 回落即重新武装；下一次停摆两条各再触发一次。
    assert monitor.observe_loop_stall(200) == (False, False)
    assert monitor.observe_loop_stall(31_000) == (True, True)
    assert len(alarms) == 2 and len(stacks) == 2


def test_loop_stall_hook_failure_does_not_break_the_sampler() -> None:
    def _boom(_lag: float) -> None:
        raise RuntimeError('injected')

    monitor = WorkerPressureMonitor(
        controller=SimpleNamespace(),
        store=SimpleNamespace(),
        on_loop_stall=_boom,
        on_loop_stack=_boom,
    )

    assert monitor.observe_loop_stall(31_000) == (True, True)


def test_payloads_pick_one_task_per_session(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store.upsert_task(_task('task:a-old', session_id='web:a'))
    service.store.upsert_task(_task('task:a-new', session_id='web:a'))
    service.store.upsert_task(_task('task:b', session_id='web:b'))
    service.store.upsert_task(_task('task:done', session_id='web:c', status='success'))
    service.store.upsert_task(_task('task:paused', session_id='web:d', is_paused=True))
    service.log_service.update_task_runtime_meta(
        'task:a-old', last_visible_output_at='2026-11-01T11:00:00+08:00'
    )
    service.log_service.update_task_runtime_meta(
        'task:a-new', last_visible_output_at='2026-11-01T13:00:00+08:00'
    )
    service.log_service.update_task_runtime_meta(
        'task:b', last_visible_output_at='2026-11-01T12:00:00+08:00'
    )

    payloads = service._build_loop_stall_payloads(125_000)

    by_session = {item['session_id']: item for item in payloads}
    assert set(by_session) == {'web:a', 'web:b'}
    assert by_session['web:a']['task_id'] == 'task:a-new'
    assert by_session['web:b']['task_id'] == 'task:b'
    for payload in payloads:
        normalized = normalize_task_stall_payload(payload)
        assert normalized, payload
        assert normalized['bucket_minutes'] == 1
        assert 'event loop stalled' in normalized['runtime_summary_excerpt']


def test_post_stall_sync_posts_json(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> bool:
            return False

    def _urlopen(request, timeout):
        captured['url'] = request.full_url
        captured['body'] = json.loads(request.data.decode('utf-8'))
        captured['headers'] = {str(k).lower(): v for k, v in request.header_items()}
        captured['timeout'] = timeout
        return _Response()

    monkeypatch.setattr('main.service.runtime_service.urllib.request.urlopen', _urlopen)

    status = MainRuntimeService._post_stall_sync(
        'http://127.0.0.1:18790/api/internal/task-stall',
        payload={'task_id': 'task:a'},
        headers={'x-g3ku-internal-token': 't'},
        timeout=5.0,
    )

    assert status == 200
    assert captured['body'] == {'task_id': 'task:a'}
    assert str(captured['url']).endswith('/api/internal/task-stall')
    assert captured['headers']['x-g3ku-internal-token'] == 't'
    assert captured['timeout'] == 5.0


def test_capture_loop_stack_writes_all_threads(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    target = tmp_path / 'loop-stall-stacks.log'
    monkeypatch.setattr(
        MainRuntimeService,
        '_loop_stack_log_path',
        staticmethod(lambda: target),
    )

    service._capture_loop_stack(12_000)

    text = target.read_text(encoding='utf-8')
    assert 'loop stall lag_ms=12000' in text
    assert 'File "' in text
