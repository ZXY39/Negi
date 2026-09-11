"""delete_task 物理目录双路径回收回归测试。

修复缺陷一：delete_task 用确定性路径 `_task_temp_dir`（<workspace>/temp/
tasks/<id>）删任务临时目录，而任务实际落盘位置记录在 runtime_meta 的
`task_temp_dir`（终态清理/用量统计/压缩归档均走 `_effective_task_temp_dir`），
两处不一致导致 worker/web 模式下删除任务后 temp 目录静默残留（实例：已删
任务残留 408 MB）。现在双路径兜底：meta 记录路径与确定性路径都删。

修复缺陷二：删除链路 ignore_errors=True 静默吞错；现在走 fs_utils.remove_tree
（只读文件强删 + 残留显式告警）。
"""

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


def _terminal_task(task_id: str) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id="web:demo",
        title="temp 回收测试",
        user_request="x",
        status="failed",
        root_node_id="node-root",
        created_at="2026-09-11T10:00:00+08:00",
        updated_at="2026-09-11T10:00:00+08:00",
        finished_at="2026-09-11T10:00:00+08:00",
    )


def _wire_delete_preconditions(service: MainRuntimeService) -> None:
    # 排空门与分发清理协作者依赖真实任务模型/worker，非本用例被测对象。
    service.worker_state = lambda: "offline"  # type: ignore[method-assign]
    service.store.list_unfinished_task_commands = lambda command_type="": []  # type: ignore[method-assign]
    service._cancel_distribution_for_force_delete = lambda **kwargs: None  # type: ignore[method-assign]


async def test_delete_task_removes_meta_recorded_temp_dir(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    _wire_delete_preconditions(service)
    task_id = "task:meta-temp"
    service.store.upsert_task(_terminal_task(task_id))

    # runtime_meta 记录的非默认 temp 位置（create_task 落盘路径的真实形态）
    meta_dir = tmp_path / "worker-space" / "temp" / "task_meta-temp"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "big-evidence.json").write_text("x" * 1024, encoding="utf-8")
    service.log_service.update_task_runtime_meta(task_id, task_temp_dir=str(meta_dir))

    # 确定性路径同时存在残留（历史半写/双根并存形态）
    default_dir = tmp_path / "temp" / "tasks" / task_id.replace(":", "_")
    default_dir.mkdir(parents=True, exist_ok=True)
    (default_dir / "leftover.txt").write_text("y", encoding="utf-8")

    deleted = await service.delete_task(task_id)

    assert deleted is not None
    assert not meta_dir.exists(), "runtime_meta 记录的 temp 目录必须被删除"
    assert not default_dir.exists(), "确定性路径的 temp 目录必须被兜底删除"


async def test_delete_task_falls_back_to_default_temp_dir_without_meta(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    _wire_delete_preconditions(service)
    task_id = "task:no-meta"
    service.store.upsert_task(_terminal_task(task_id))

    default_dir = tmp_path / "temp" / "tasks" / task_id.replace(":", "_")
    default_dir.mkdir(parents=True, exist_ok=True)
    (default_dir / "leftover.txt").write_text("y", encoding="utf-8")
    # 未写 runtime_meta：_effective_task_temp_dir 回退确定性路径

    deleted = await service.delete_task(task_id)

    assert deleted is not None
    assert not default_dir.exists()


async def test_delete_task_removes_readonly_git_style_temp_dir(tmp_path) -> None:
    """git 克隆只读文件不得再让 temp 目录静默残留。"""
    import os
    import stat

    service = _make_web_service(tmp_path)
    _wire_delete_preconditions(service)
    task_id = "task:readonly-temp"
    service.store.upsert_task(_terminal_task(task_id))

    default_dir = tmp_path / "temp" / "tasks" / task_id.replace(":", "_")
    objects_dir = default_dir / "repos" / "pkg" / ".git" / "objects" / "aa"
    objects_dir.mkdir(parents=True, exist_ok=True)
    blob = objects_dir / "blob1"
    blob.write_text("payload", encoding="utf-8")
    os.chmod(blob, stat.S_IREAD)

    try:
        deleted = await service.delete_task(task_id)
        assert deleted is not None
        assert not default_dir.exists(), "含只读文件的 temp 目录必须被完整删除"
    finally:
        for path in [default_dir, *default_dir.rglob("*")] if default_dir.exists() else []:
            try:
                os.chmod(path, stat.S_IRWXU)
            except OSError:
                pass
