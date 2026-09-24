"""任务临时文件清除（卡片「清除临时文件」）契约单测。

覆盖：只清 temp/tasks/<id> 不动任务行与产出；门槛严格等于终态（暂停中的任务也拒）；
temp 根与 temp/tasks 根永不受影响（runtime_meta 可以把任务指到这两级）；
meta 指向自定义路径时双路径都收；清除后重算占用记账。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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


def _task(task_id: str, *, status: str = 'success', is_paused: bool = False) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id="web:demo",
        title=f'测试任务 {task_id}',
        user_request="请帮我做一件事",
        status=status,
        is_paused=is_paused,
        root_node_id="node-root",
        created_at='2026-09-16T10:00:00+08:00',
        updated_at='2026-09-16T10:00:00+08:00',
        finished_at='2026-09-16T11:00:00+08:00' if status in {'success', 'failed'} else '',
    )


def _fill_temp_dir(service: MainRuntimeService, task_id: str, *, payload_bytes: int = 4096):
    temp_dir = service._task_temp_dir(task_id, create=True)
    (temp_dir / 'scratch.bin').write_bytes(b'x' * payload_bytes)
    nested = temp_dir / 'sub' / 'deep'
    nested.mkdir(parents=True, exist_ok=True)
    (nested / 'draft.txt').write_text('draft output', encoding='utf-8')
    return temp_dir


def _point_meta_at(service: MainRuntimeService, task_id: str, path) -> None:
    service.log_service.update_task_runtime_meta(task_id, task_temp_dir=str(path))


async def test_terminal_task_temp_dir_cleared_but_task_kept(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    task_id = 'task:clear-ok'
    service.store.upsert_task(_task(task_id, status='success'))
    temp_dir = _fill_temp_dir(service, task_id, payload_bytes=4096)
    service._reconcile_task_disk_usage(task_id)
    usage_before = service.store.get_task_disk_usages([task_id])[task_id]

    result = await service.clear_task_temp_files(task_id)

    assert result['found'] is True
    assert result['freed_bytes'] >= 4096
    assert not temp_dir.exists()
    # 只删临时目录：任务行、台账、产出与事件都不动
    assert service.store.get_task(task_id) is not None
    assert service.store.list_task_delete_ledger_rows() == []
    usage_after = service.store.get_task_disk_usages([task_id])[task_id]
    assert usage_after < usage_before


async def test_other_tasks_dirs_untouched(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    service.store.upsert_task(_task('task:victim', status='failed'))
    service.store.upsert_task(_task('task:bystander', status='failed'))
    victim = _fill_temp_dir(service, 'task:victim')
    bystander = _fill_temp_dir(service, 'task:bystander')

    result = await service.clear_task_temp_files('task:victim')

    assert [str(item) for item in result['removed_dirs']] == [str(victim)]
    assert not victim.exists()
    assert bystander.exists()


@pytest.mark.parametrize('status,is_paused', [
    ('in_progress', False),
    # 暂停不是独立 status：暂停态是 in_progress + is_paused，仍可能被 resume，
    # 临时目录依旧是 exec/filesystem 的默认落点，所以一并拒绝。
    ('in_progress', True),
])
async def test_non_terminal_and_paused_rejected(tmp_path, status, is_paused) -> None:
    service = _make_web_service(tmp_path)
    task_id = 'task:running'
    service.store.upsert_task(_task(task_id, status=status, is_paused=is_paused))
    temp_dir = _fill_temp_dir(service, task_id)

    with pytest.raises(ValueError) as exc:
        await service.clear_task_temp_files(task_id)

    assert str(exc.value) == 'task_not_terminal'
    assert temp_dir.exists()


async def test_unknown_task_returns_none(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    assert await service.clear_task_temp_files('task:missing') is None


@pytest.mark.parametrize('root_kind', ['temp', 'temp_tasks'])
async def test_meta_pointing_at_a_root_never_removes_it(tmp_path, root_kind) -> None:
    """`_effective_task_temp_dir` 只挡 `temp`，meta 仍可指向 `temp/tasks`。

    清除动作必须自己把两级根都排除掉，否则一次点击清空所有任务的临时区。
    """
    service = _make_web_service(tmp_path)
    task_id = 'task:bad-meta'
    service.store.upsert_task(_task(task_id, status='success'))
    root = service._task_temp_root(create=True) if root_kind == 'temp_tasks' else (tmp_path / 'temp')
    root.mkdir(parents=True, exist_ok=True)
    mine = _fill_temp_dir(service, task_id)
    bystander = _fill_temp_dir(service, 'task:other')
    _point_meta_at(service, task_id, root)

    result = await service.clear_task_temp_files(task_id)

    assert root.exists()
    assert bystander.exists()
    assert str(root) not in [str(item) for item in result['removed_dirs']]
    # 确定性默认路径仍按双路径口径收掉，本任务自己的目录被清
    assert not mine.exists()


async def test_custom_meta_path_and_default_path_both_cleared(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    task_id = 'task:relocated'
    service.store.upsert_task(_task(task_id, status='success'))
    relocated = tmp_path / 'elsewhere' / 'scratch'
    relocated.mkdir(parents=True)
    (relocated / 'a.bin').write_bytes(b'y' * 2048)
    default_dir = _fill_temp_dir(service, task_id, payload_bytes=1024)
    _point_meta_at(service, task_id, relocated)

    result = await service.clear_task_temp_files(task_id)

    assert result['freed_bytes'] >= 2048 + 1024
    assert not relocated.exists()
    assert not default_dir.exists()
    assert len(result['removed_dirs']) == 2


async def test_second_call_reports_nothing_removed(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    task_id = 'task:twice'
    service.store.upsert_task(_task(task_id, status='success'))
    _fill_temp_dir(service, task_id)

    assert (await service.clear_task_temp_files(task_id))['removed_dirs']

    second = await service.clear_task_temp_files(task_id)
    assert second['removed_dirs'] == []
    assert second['freed_bytes'] == 0


class _StubService:
    """只暴露端点用到的四个成员，用来验状态码映射。"""

    def __init__(self, *, result=None, error=None):
        self._result = result
        self._error = error
        self.calls: list[str] = []

    async def startup(self) -> None:
        return None

    def normalize_task_id(self, task_id: str) -> str:
        return str(task_id or '').strip()

    async def clear_task_temp_files(self, task_id: str):
        self.calls.append(task_id)
        if self._error is not None:
            raise self._error
        return self._result


def _build_client(service):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from main.api import rest

    app = FastAPI()
    app.include_router(rest.router, prefix='/api')
    rest.get_agent = lambda: SimpleNamespace(main_task_service=service)
    return TestClient(app)


def test_endpoint_status_code_mapping() -> None:
    ok = {'found': True, 'task_id': 'task:1', 'removed_dirs': ['a'], 'failed_dirs': [], 'freed_bytes': 2048}
    service = _StubService(result=ok)
    client = _build_client(service)

    response = client.post('/api/tasks/task%3A1/clear-temp')

    assert response.status_code == 200
    assert response.json()['freed_bytes'] == 2048
    assert service.calls == ['task:1']

    busy = _StubService(error=ValueError('task_not_terminal'))
    assert _build_client(busy).post('/api/tasks/task%3A2/clear-temp').status_code == 409

    gone = _StubService(result=None)
    assert _build_client(gone).post('/api/tasks/task%3A3/clear-temp').status_code == 404

    # 'summary' 不是任务 id，必须被路由守卫挡掉而不是当成任务查
    assert _build_client(_StubService(result=ok)).post('/api/tasks/summary/clear-temp').status_code == 404


def test_rest_route_exposed() -> None:
    from main.api import rest

    matches = [
        route for route in rest.router.routes
        if getattr(route, 'path', '') == '/tasks/{task_id}/clear-temp'
    ]
    assert len(matches) == 1
    assert 'POST' in matches[0].methods
