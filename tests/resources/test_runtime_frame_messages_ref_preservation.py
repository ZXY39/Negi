"""帧正文指针 preservation 回归（task:c7f1dbfae6e2，2026-09-19）。

事故链：验收节点在飞轮被进程收尾打断 → 启动恢复走 read_runtime_state +
replace_runtime_frames 的读-改-写回路 → messages_ref 解析失败时 hydrate 只给出
空 messages、且 _sanitize_runtime_frame 枚举键里根本没有 messages_ref →
_runtime_frame_record 用一份空正文覆盖原指针 → 节点 durable 历史被静默写没，
下一跳退回 fallback_seed 重建（执行节点当轮只剩 5 条消息）。

本文件锁定修复契约：
- A1：ref 解析不出正文时，写帧必须保留原 messages_ref / messages_count 并落 WARN；
- A2：真正携带正文的写照常换新指针，不被保护逻辑挡住；
- A3：启动恢复的整帧重写不是内容销毁通道；
- A4：update_frame 的 mutator 回路同样不丢指针。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import main.monitoring.log_service as log_service_module
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


def _build_service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
        execution_model_refs=["fake"],
        acceptance_model_refs=["fake"],
    )
    async def _noop_enqueue_task(_task_id: str) -> None:
        return None

    service.global_scheduler.enqueue_task = _noop_enqueue_task
    return service


def _frame_payload(service: MainRuntimeService, task_id: str, node_id: str) -> dict[str, object]:
    record = service.store.get_task_runtime_frame(task_id, node_id)
    assert record is not None
    return dict(record.payload or {})


def _write_frame(service: MainRuntimeService, task_id: str, node_id: str, depth: int, messages) -> None:
    service.log_service.upsert_frame(
        task_id,
        {
            "node_id": node_id,
            "depth": depth,
            "node_kind": "execution",
            "phase": "before_model",
            "messages": list(messages),
            # 生产帧总是带选择面快照，正文外置分支因此总会执行——这正是解析失败时
            # 旧历史被空正文覆盖的前提条件，测试必须复现它而不是绕过它。
            "contract_visible_skill_ids": ["demo.skill"],
        },
        publish_snapshot=False,
    )


def _messages(count: int, tag: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": f"{tag}-{index}"} for index in range(count)]


@pytest.mark.asyncio
async def test_frame_write_preserves_messages_ref_when_content_unresolvable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_service(tmp_path)
    record = await service.create_task("frame preservation", session_id="web:ceo-demo")
    root_id = record.root_node_id

    _write_frame(service, record.task_id, root_id, 0, _messages(6, "before"))
    stored = _frame_payload(service, record.task_id, root_id)
    original_ref = str(stored["messages_ref"])
    assert original_ref
    assert int(stored["messages_count"]) == 6

    fake_logger = MagicMock()
    monkeypatch.setattr(log_service_module, "logger", fake_logger)
    monkeypatch.setattr(service.log_service, "_resolve_content_ref", lambda ref: "")

    # 启动恢复的读-改-写回路：hydrate 拿不到正文，再原样写回。
    state = service.log_service.read_runtime_state(record.task_id) or {}
    service.log_service.replace_runtime_frames(
        record.task_id,
        frames=list(state.get("frames") or []),
        active_node_ids=list(state.get("active_node_ids") or []),
        runnable_node_ids=list(state.get("runnable_node_ids") or []),
        waiting_node_ids=list(state.get("waiting_node_ids") or []),
        publish_snapshot=False,
    )

    after = _frame_payload(service, record.task_id, root_id)
    assert str(after["messages_ref"]) == original_ref
    assert int(after["messages_count"]) == 6

    # 指针字符串按节点稳定，光比 ref 抓不到回归：正文必须还在。
    monkeypatch.undo()
    hydrated = service.log_service.read_runtime_frame(record.task_id, root_id) or {}
    assert len(hydrated.get("messages") or []) == 6

    warnings = [str(call) for call in fake_logger.warning.call_args_list]
    assert any("keeping existing messages_ref" in text for text in warnings)
    assert any("resolved to no messages" in text for text in warnings)


@pytest.mark.asyncio
async def test_frame_write_with_messages_still_rotates_messages_ref(
    tmp_path: Path,
) -> None:
    service = _build_service(tmp_path)
    record = await service.create_task("frame rotation", session_id="web:ceo-demo")
    root_id = record.root_node_id

    _write_frame(service, record.task_id, root_id, 0, _messages(3, "first"))
    first = _frame_payload(service, record.task_id, root_id)
    assert int(first["messages_count"]) == 3

    _write_frame(service, record.task_id, root_id, 0, _messages(9, "second"))
    second = _frame_payload(service, record.task_id, root_id)
    assert int(second["messages_count"]) == 9

    # 帧正文按节点单例外置：ref 字符串稳定，正文与计数必须跟着真实写入走。
    hydrated = service.log_service.read_runtime_frame(record.task_id, root_id) or {}
    assert len(hydrated.get("messages") or []) == 9


@pytest.mark.asyncio
async def test_update_frame_mutator_loop_does_not_drop_messages_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_service(tmp_path)
    record = await service.create_task("frame mutate", session_id="web:ceo-demo")
    root_id = record.root_node_id

    _write_frame(service, record.task_id, root_id, 0, _messages(4, "mutate"))
    original_ref = str(_frame_payload(service, record.task_id, root_id)["messages_ref"])

    monkeypatch.setattr(service.log_service, "_resolve_content_ref", lambda ref: "")
    service.log_service.update_frame(
        record.task_id,
        root_id,
        lambda frame: {**dict(frame or {}), "phase": "after_model"},
        publish_snapshot=False,
    )

    after = _frame_payload(service, record.task_id, root_id)
    assert str(after["messages_ref"]) == original_ref
    assert int(after["messages_count"]) == 4
    assert str(after["phase"]) == "after_model"

    monkeypatch.undo()
    hydrated = service.log_service.read_runtime_frame(record.task_id, root_id) or {}
    assert len(hydrated.get("messages") or []) == 4


@pytest.mark.asyncio
async def test_interrupted_task_recovery_keeps_frame_history_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_service(tmp_path)
    record = await service.create_task("recovery frame", session_id="web:ceo-demo")
    root_id = record.root_node_id

    _write_frame(service, record.task_id, root_id, 0, _messages(5, "recovery"))
    original_ref = str(_frame_payload(service, record.task_id, root_id)["messages_ref"])

    fake_logger = MagicMock()
    monkeypatch.setattr(log_service_module, "logger", fake_logger)
    monkeypatch.setattr(service.log_service, "_resolve_content_ref", lambda ref: "")

    service._recover_interrupted_task(record.task_id)

    after = _frame_payload(service, record.task_id, root_id)
    assert str(after["messages_ref"]) == original_ref
    assert int(after["messages_count"]) == 5

    monkeypatch.undo()
    hydrated = service.log_service.read_runtime_frame(record.task_id, root_id) or {}
    assert len(hydrated.get("messages") or []) == 5

    # 恢复仍按设计标记为异常中断恢复，本修复不改变控制语义。
    task = service.store.get_task(record.task_id)
    assert task is not None
    assert str((task.metadata or {}).get("recovery_notice") or "")
