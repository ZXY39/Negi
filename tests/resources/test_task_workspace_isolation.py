from __future__ import annotations

from pathlib import Path

import pytest

from main.protocol import now_iso
from main.service.runtime_service import MainRuntimeService

# 在 conftest 的 autouse fixture 打补丁之前捕获原始实现，用于直接验证
# workspace_root 构造参数的优先级。
_WORKSPACE_ROOT_BEFORE_FIXTURE = MainRuntimeService._workspace_root


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


async def _create_web_task(service: MainRuntimeService, prompt: str):
    _mark_worker_online(service)
    return await service.create_task(prompt, session_id='web:shared')


def _real_repo_task_dir(task_id: str) -> Path:
    return Path.cwd() / 'temp' / 'tasks' / task_id.replace(':', '_')


@pytest.mark.asyncio
async def test_explicit_workspace_root_keeps_task_temp_dir_inside_it(tmp_path: Path):
    workspace = tmp_path / 'isolated-ws'
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        workspace_root=workspace,
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service, 'scratch workspace isolation')
        task_dir = (workspace / 'temp' / 'tasks' / record.task_id.replace(':', '_')).resolve()

        assert task_dir.is_dir()
        assert not _real_repo_task_dir(record.task_id).exists()
        runtime_meta = service.log_service.read_task_runtime_meta(record.task_id)
        assert Path(runtime_meta['task_temp_dir']).resolve() == task_dir
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_default_workspace_root_falls_back_to_test_tmp_dir_instead_of_repo(tmp_path: Path):
    # 未显式传 workspace_root：conftest 的兜底夹具把 cwd 回退替换为 tmp_path，
    # 任务临时目录必须落在 pytest 临时目录里，而不是真实仓库的 temp/tasks。
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    try:
        record = await _create_web_task(service, 'default workspace isolation')
        task_dir = (tmp_path / 'temp' / 'tasks' / record.task_id.replace(':', '_')).resolve()

        assert task_dir.is_dir()
        assert not _real_repo_task_dir(record.task_id).exists()
    finally:
        await service.close()


def test_workspace_root_override_wins_over_resource_manager(tmp_path: Path) -> None:
    # 直接调用原始 `_workspace_root` 实现（未被夹具替换），验证构造参数
    # workspace_root 的优先级高于 resource_manager.workspace。
    manager = type('_Manager', (), {'workspace': '/nonexistent-manager-ws'})()
    override = tmp_path / 'override-ws'
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        workspace_root=override,
        resource_manager=manager,
    )
    assert _WORKSPACE_ROOT_BEFORE_FIXTURE(service) == override.resolve()