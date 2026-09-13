"""任务删除全量清除（wipe）单测：23 表清零、governance 清理、产出导出、缓存清理。"""

from __future__ import annotations

from types import SimpleNamespace

from main.models import TaskRecord
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        return SimpleNamespace(content='', tool_calls=[], finish_reason='stop', usage={})


def _make_web_service(tmp_path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )


def _wire_delete_preconditions(service: MainRuntimeService) -> None:
    service.worker_state = lambda: "offline"  # type: ignore[method-assign]
    service.store.list_unfinished_task_commands = lambda command_type="": []  # type: ignore[method-assign]
    service._cancel_distribution_for_force_delete = lambda **kwargs: None  # type: ignore[method-assign]


def _terminal_task(task_id: str, **kwargs) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id="web:demo",
        title="wipe 测试",
        user_request="x",
        status="failed",
        root_node_id="node-root",
        created_at="2026-09-11T10:00:00+08:00",
        updated_at="2026-09-11T10:00:00+08:00",
        finished_at="2026-09-11T10:00:00+08:00",
        **kwargs,
    )


def _seed_task_rows(service: MainRuntimeService, task_id: str) -> None:
    store = service.store
    store.upsert_task(_terminal_task(task_id))
    store._execute_write(
        'INSERT INTO task_node_details (node_id, task_id, updated_at, input_text, input_ref, output_text, '
        'output_ref, check_result, check_result_ref, final_output, final_output_ref, failure_reason, '
        'prompt_summary, execution_trace_ref, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ('node-root', task_id, '2026-09-11T10:00:00+08:00', 'in', '', 'out', '', '', '', '', '', '', '', '', '{}'),
    )
    store._execute_write(
        'INSERT INTO task_model_calls (task_id, node_id, created_at, payload_json) VALUES (?, ?, ?, ?)',
        (task_id, 'node-root', '2026-09-11T10:00:00+08:00', '{}'),
    )
    store._execute_write(
        'INSERT INTO heartbeat_node_retry_state (node_id, task_id, session_id, consecutive_failures, '
        'first_failure_at, last_attempt_at, next_eligible_at, escalated, updated_at) '
        "VALUES (?, ?, ?, 1, '', '', '', 0, ?)",
        ('node-root', task_id, 'web:demo', '2026-09-11T10:00:00+08:00'),
    )
    store.upsert_task_disk_usage(task_id, 12345)
    store.append_task_event(
        task_id=task_id, session_id='web:demo', event_type='task.node.patch',
        created_at='2026-09-11T10:00:00+08:00', payload={'node_id': 'node-root'},
    )


def _table_counts(service: MainRuntimeService, task_id: str) -> dict[str, int]:
    tables = [
        'tasks', 'nodes', 'task_nodes', 'task_node_details', 'task_node_rounds',
        'task_node_tool_results', 'task_model_calls', 'task_runtime_frames', 'task_runtime_meta',
        'task_node_pauses', 'task_error_logs', 'task_commands', 'task_terminal_outbox',
        'task_stall_outbox', 'task_distribution_error_outbox', 'task_summary_outbox',
        'task_node_notifications', 'task_message_distribution_epochs', 'task_events',
        'artifacts', 'task_disk_usage', 'heartbeat_node_retry_state', 'task_projection_meta',
    ]
    counts = {}
    for table in tables:
        row = service.store._fetchone(f'SELECT COUNT(*) AS n FROM {table} WHERE task_id = ?', (task_id,))
        counts[table] = int(row['n'] if row else 0)
    return counts


async def test_delete_task_wipes_all_db_tables_and_dirs(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    _wire_delete_preconditions(service)
    task_id = "task:wipe-all"
    _seed_task_rows(service, task_id)
    # event-history 单份快照 + temp 残留
    assert service.store.write_task_live_snapshot(task_id, '{"frame": {}}')
    temp_dir = tmp_path / "temp" / "tasks" / task_id.replace(":", "_")
    temp_dir.mkdir(parents=True, exist_ok=True)
    (temp_dir / "scratch.txt").write_text("x", encoding="utf-8")
    # governance 审批行（context_id=task_id，含命令明文）
    service.governance_store.create_exec_approval({
        'approval_id': 'ap-1', 'command_norm': 'rm', 'command_text': 'rm -rf /tmp/x',
        'cwd': '', 'guard_reason': 'destructive', 'actor_role': 'worker', 'lane': 'exec',
        'context_id': task_id, 'status': 'pending', 'decision_scope': '', 'decided_by': '',
        'created_at': '2026-09-11T10:00:00+08:00', 'decided_at': '', 'expires_at': '',
    })

    before = _table_counts(service, task_id)
    assert before['tasks'] == 1 and before['task_disk_usage'] == 1 and before['heartbeat_node_retry_state'] == 1

    deleted = await service.delete_task(task_id)
    assert deleted is not None

    after = _table_counts(service, task_id)
    assert all(v == 0 for v in after.values()), {k: v for k, v in after.items() if v}
    # governance 行清零
    assert service.governance_store.list_exec_approvals(limit=50) == []
    # 目录清零：event-history / temp
    assert not (service.store._event_history_dir / task_id.replace(":", "_")).exists()
    assert not temp_dir.exists()
    # 台账记录存在且 wiped=1
    rows = service.store.list_task_delete_ledger_rows()
    assert [r['task_id'] for r in rows] == [task_id]
    assert rows[0]['wiped'] == 1 and rows[0]['reason'] == 'user_delete'


async def test_delete_task_exports_deliverables_only(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    _wire_delete_preconditions(service)
    task_id = "task:wipe-export"
    _seed_task_rows(service, task_id)
    store = service.artifact_store
    kept_final = store.create_json_artifact(
        task_id=task_id, node_id='node-root', kind='final_output', title='最终报告',
        payload={'text': 'final content'}, extension='.json', mime_type='application/json',
        preview_text='final',
    )
    kept_report = store.create_text_artifact(
        task_id=task_id, node_id='node-root', kind='node_output', title='weekly report draft',
        content='# report body', extension='.md', mime_type='text/markdown',
    )
    dropped = store.create_json_artifact(
        task_id=task_id, node_id='node-root', kind='task_actual_request', title='node-actual-request:n:1',
        payload={'messages': []}, extension='.json', mime_type='application/json',
        preview_text='request',
    )
    assert kept_final is not None and dropped is not None

    await service.delete_task(task_id)

    deliverables = tmp_path / "deliverables" / task_id.replace(":", "_")
    assert deliverables.is_dir()
    names = sorted(item.name for item in deliverables.iterdir())
    assert 'manifest.json' in names
    # kept 导出（final_output 命中 kind 判据）；非 kept 不导出
    exported = [n for n in names if n != 'manifest.json']
    assert any(kept_final.artifact_id.split(':')[-1][-8:] in n for n in exported)
    assert not any(dropped.artifact_id.split(':')[-1][-8:] in n for n in exported)
    assert any('report' in n.lower() for n in exported)
    import json as _json
    manifest = _json.loads((deliverables / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['task_id'] == task_id
    assert manifest['reason'] == 'user_delete'
    assert manifest['counts']['exported'] >= 1
    # 源 artifacts 目录整体删除
    assert not (tmp_path / "artifacts" / task_id.replace(":", "_")).exists()


async def test_late_event_after_delete_does_not_resurrect(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    _wire_delete_preconditions(service)
    task_id = "task:wipe-late"
    _seed_task_rows(service, task_id)
    await service.delete_task(task_id)

    seq = service.store.append_task_event(
        task_id=task_id, session_id='web:demo', event_type='task.node.patch',
        created_at='2026-09-11T11:00:00+08:00', payload={'late': True},
    )
    assert seq == 0
    assert service.store.list_task_events(task_id=task_id, limit=10) == []
    assert not service.store.write_task_live_snapshot(task_id, '{"frame": {}}')
    assert not (service.store._event_history_dir / task_id.replace(":", "_")).exists()
    result = service.store.put_task_summary_outbox(
        task_id=task_id, session_id='web:demo', created_at='2026-09-11T11:00:00+08:00', payload={'event_type': 'task.summary.patch'},
    )
    assert result.get('skipped') == 'task_deleted'
