from __future__ import annotations

import json
from pathlib import Path

import pytest

from main.protocol import now_iso
from main.service.runtime_service import MainRuntimeService, TaskDeleteTool, TaskStatsTool


TASK_KEYWORDS = '\u4efb\u52a1\u5173\u952e\u8bcd'
TASK_IDS = '\u4efb\u52a1id\u5217\u8868'


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


def _mark_worker_online(service: MainRuntimeService) -> None:
    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )


async def _create_web_task(service: MainRuntimeService, prompt: str, *, session_id: str = 'web:shared'):
    _mark_worker_online(service)
    return await service.create_task(prompt, session_id=session_id)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def _task_event_dir(service: MainRuntimeService, task_id: str) -> Path:
    return service._task_event_history_dir(task_id)


def _set_task_status(service: MainRuntimeService, task_id: str, status: str) -> None:
    service.store.update_task(
        task_id,
        lambda record: record.model_copy(
            update={
                'status': status,
                'updated_at': now_iso(),
                'finished_at': now_iso() if status in {'success', 'failed'} else None,
            }
        ),
    )


@pytest.mark.asyncio
async def test_task_stats_tool_list_filters_by_date_keyword_and_reports_disk_usage(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    stats_tool = TaskStatsTool(service)
    try:
        first = await _create_web_task(service, 'prepare first announcement draft')
        second = await _create_web_task(service, 'compile role heat ranking')
        service.store.update_task(
            first.task_id,
            lambda record: record.model_copy(
                update={
                    'created_at': '2026-04-05T09:00:00+08:00',
                    'updated_at': '2026-04-05T09:00:00+08:00',
                }
            ),
        )
        service.store.update_task(
            second.task_id,
            lambda record: record.model_copy(
                update={
                    'created_at': '2026-04-05T10:00:00+08:00',
                    'updated_at': '2026-04-05T10:00:00+08:00',
                }
            ),
        )
        _set_task_status(service, first.task_id, 'success')
        _set_task_status(service, second.task_id, 'failed')
        baseline_disk_usage = service._task_disk_usage_bytes(first.task_id)

        _write_text(service.file_store.task_dir(first.task_id) / 'node.json', 'A' * 10)
        _write_text(Path(service.artifact_store._task_dir(first.task_id)) / 'artifact.txt', 'B' * 20)
        _write_text(service._task_temp_dir(first.task_id) / 'tmp.log', 'C' * 30)
        _write_text(_task_event_dir(service, first.task_id) / '1.json', 'D' * 40)

        payload = json.loads(
            await stats_tool.execute(
                mode='list',
                **{
                    'from': '2026/4/5',
                    'to': '2026/4/5',
                    TASK_KEYWORDS: ['announcement', 'missing'],
                },
            )
        )

        assert payload['mode'] == 'list'
        assert len(payload['items']) == 1
        item = payload['items'][0]
        assert item['task_id'] == first.task_id
        assert item['status'] == 'success'
        assert item['prompt_preview_100'].startswith('prepare first announcement draft')
        assert item['disk_usage_bytes'] == baseline_disk_usage + 100
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_task_stats_tool_id_mode_preserves_input_order_and_reports_not_found(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    stats_tool = TaskStatsTool(service)
    try:
        first = await _create_web_task(service, 'task one')
        second = await _create_web_task(service, 'task two')

        payload = json.loads(
            await stats_tool.execute(
                mode='id',
                **{TASK_IDS: [second.task_id, 'task:missing', first.task_id]},
            )
        )

        assert payload['mode'] == 'id'
        assert [item['task_id'] for item in payload['items']] == [second.task_id, 'task:missing', first.task_id]
        assert payload['items'][1]['result'] == 'not_found'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_task_delete_tool_preview_and_confirm_deletes_full_task_disk_footprint(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    delete_tool = TaskDeleteTool(service)
    try:
        record = await _create_web_task(service, 'prepare task for deletion')
        _set_task_status(service, record.task_id, 'success')

        task_file_dir = service.file_store.task_dir(record.task_id)
        artifact_dir = Path(service.artifact_store._task_dir(record.task_id))
        task_temp_dir = service._task_temp_dir(record.task_id)
        event_dir = _task_event_dir(service, record.task_id)

        _write_text(task_file_dir / 'node.json', 'A')
        _write_text(artifact_dir / 'artifact.txt', 'B')
        _write_text(task_temp_dir / 'temp.txt', 'C')
        _write_text(event_dir / '1.json', 'D')

        preview = json.loads(await delete_tool.execute(mode='preview', **{TASK_IDS: [record.task_id]}))
        assert preview['mode'] == 'preview'
        assert preview['items'][0]['task_id'] == record.task_id
        assert preview['items'][0]['status'] == 'success'
        token = preview['confirmation_token']
        assert token

        confirmed = json.loads(
            await delete_tool.execute(
                mode='confirm',
                confirmation_token=token,
                **{TASK_IDS: [record.task_id]},
            )
        )
        assert confirmed['mode'] == 'confirm'
        assert confirmed['items'][0]['result'] == 'deleted'
        assert service.get_task(record.task_id) is None
        assert not task_file_dir.exists()
        assert not artifact_dir.exists()
        assert not task_temp_dir.exists()
        assert not event_dir.exists()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_terminal_task_auto_cleans_empty_task_temp_dir(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service, 'finish with no temp output')
        task_temp_dir = service._task_temp_dir(record.task_id)

        assert task_temp_dir.exists()

        service.log_service.mark_task_failed(record.task_id, reason='expected terminal cleanup')

        assert service.get_task(record.task_id) is not None
        assert not task_temp_dir.exists()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_terminal_task_keeps_nonempty_task_temp_dir(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service, 'finish with retained temp output')
        task_temp_dir = service._task_temp_dir(record.task_id)
        _write_text(task_temp_dir / 'tmp.log', 'keep me')

        service.log_service.mark_task_failed(record.task_id, reason='expected retained temp output')

        assert task_temp_dir.exists()
        assert (task_temp_dir / 'tmp.log').read_text(encoding='utf-8') == 'keep me'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_delete_task_records_for_session_deletes_full_disk_footprint_for_in_progress_tasks(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service, 'session task cleanup target', session_id='web:ceo-delete-target')

        task_file_dir = service.file_store.task_dir(record.task_id)
        artifact_dir = Path(service.artifact_store._task_dir(record.task_id))
        task_temp_dir = service._task_temp_dir(record.task_id)
        event_dir = _task_event_dir(service, record.task_id)

        _write_text(task_file_dir / 'node.json', 'A')
        _write_text(artifact_dir / 'artifact.txt', 'B')
        _write_text(task_temp_dir / 'temp.txt', 'C')
        _write_text(event_dir / '1.json', 'D')

        deleted = await service.delete_task_records_for_session('web:ceo-delete-target')

        assert deleted == 1
        assert service.get_task(record.task_id) is None
        assert not task_file_dir.exists()
        assert not artifact_dir.exists()
        assert not task_temp_dir.exists()
        assert not event_dir.exists()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_task_delete_tool_rejects_token_mismatch(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    delete_tool = TaskDeleteTool(service)
    try:
        first = await _create_web_task(service, 'task one')
        second = await _create_web_task(service, 'task two')
        _set_task_status(service, first.task_id, 'success')
        _set_task_status(service, second.task_id, 'success')

        preview = json.loads(await delete_tool.execute(mode='preview', **{TASK_IDS: [first.task_id]}))
        confirmed = json.loads(
            await delete_tool.execute(
                mode='confirm',
                confirmation_token=preview['confirmation_token'],
                **{TASK_IDS: [second.task_id]},
            )
        )

        assert confirmed['mode'] == 'confirm'
        assert confirmed['items'][0]['result'] == 'token_mismatch'
        assert service.get_task(first.task_id) is not None
        assert service.get_task(second.task_id) is not None
    finally:
        await service.close()
