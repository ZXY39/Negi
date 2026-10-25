"""根节点「停在执行相位却没有执行器」的悬空回归（task:d82fca788a41，2026-10-25）。

实盘现场：根节点第 12 跳发出 `manage_child_nodes` + `exec` 后，回合协程消失，
但帧停在 `waiting_tool_results`、工具行停在 running。调度循环看到「根非终态 + 无活
entry + 信箱无未读」就把任务当停靠交回，`run_task` 收尾关闭 dispatcher——之后没有
任何东西会再唤醒根，任务静默悬空 12 分钟（只有 worker 重启的启动恢复能救）。

本文件锁定契约：
- 根停在执行相位（`before_model` / `after_model` / `waiting_tool_results`）、
  没有执行器、也没有未读时，调度循环必须先重派根，而不是直接返回 control_only；
- 停靠相位（`waiting_explicit` / `waiting_acceptance`）保持原语义：不点火，等投递；
- 重派拿不到活执行器时，根被可见地 park（错误行 + `pause_reason='error'`），
  绝不留静默幽灵。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from g3ku.providers.base import LLMResponse, ToolCallRequest
from main.models import NodeFinalResult
from main.service.runtime_service import MainRuntimeService


@pytest.fixture(autouse=True)
def _default_node_send_preflight_context_window(monkeypatch: pytest.MonkeyPatch) -> None:
    import main.runtime.react_loop as react_loop_module
    from main.runtime.chat_backend import SendModelContextWindowInfo

    def _resolve(**kwargs) -> SendModelContextWindowInfo:
        refs = list(kwargs.get("model_refs") or [])
        model_key = str(refs[0] or "").strip() if refs else ""
        return SendModelContextWindowInfo(
            model_key=model_key,
            provider_id="test",
            provider_model=f"test:{model_key}" if model_key else "test",
            resolved_model=model_key,
            context_window_tokens=32000,
            resolution_error="",
        )

    monkeypatch.setattr(
        react_loop_module,
        "get_runtime_config",
        lambda **_: (SimpleNamespace(), 0, False),
        raising=False,
    )
    monkeypatch.setattr(
        react_loop_module.runtime_chat_backend,
        "resolve_send_model_context_window_info",
        _resolve,
        raising=False,
    )


class _QueuedChatBackend:
    def __init__(self, responses: list[object]) -> None:
        self.calls: list[dict[str, object]] = []
        self._responses = list(responses)

    async def chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        if not self._responses:
            raise AssertionError("queued chat backend exhausted")
        return self._responses.pop(0)


def _build_service(tmp_path: Path, *, chat_backend) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=chat_backend,
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


def _final_response(*, call_id: str, text: str) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCallRequest(
                id=call_id,
                name="submit_final_result",
                arguments={
                    "status": "success",
                    "delivery_status": "final",
                    "summary": text,
                    "answer": text,
                    "evidence": [],
                    "remaining_work": [],
                    "blocking_reason": "",
                },
            )
        ]
    )


def _control_only_result() -> NodeFinalResult:
    return NodeFinalResult(
        status="success",
        delivery_status="partial",
        summary="root is parked; waiting for a wake source",
        answer="",
        evidence=[],
        remaining_work=[],
        blocking_reason="",
    )


def _write_inflight_frame(service: MainRuntimeService, task_id: str, node_id: str, phase: str) -> None:
    """在飞现场：工具已经发出、结果没回，执行器却不见了。"""
    call_id = "call_00_stranded"
    service.log_service.update_frame(
        task_id,
        node_id,
        lambda frame: {
            **frame,
            "phase": phase,
            "pending_tool_calls": [
                {"id": call_id, "name": "exec", "arguments": {"command": "echo hi"}}
            ],
            "active_round_id": f"round:{node_id}:{phase}",
            "active_round_tool_call_ids": [call_id],
            "tool_calls": [
                {
                    "tool_call_id": call_id,
                    "tool_name": "exec",
                    "status": "running",
                    "started_at": "2026-10-25T14:37:46+08:00",
                    "finished_at": "",
                    "elapsed_seconds": None,
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_stranded_root_is_revived_before_control_only_return(tmp_path: Path) -> None:
    backend = _QueuedChatBackend([_final_response(call_id="call:revive", text="根节点恢复后收口")])
    service = _build_service(tmp_path, chat_backend=backend)
    try:
        record = await service.create_task("stranded root", session_id="web:shared")
        task_id, root_id = record.task_id, record.root_node_id
        _write_inflight_frame(service, task_id, root_id, "waiting_tool_results")
        dispatcher = service.task_actor_service._create_dispatcher(task_id)
        service.task_actor_service._dispatchers[task_id] = dispatcher

        _result, control_only = await service.task_actor_service._drive_scheduler_loop(
            task_id=task_id,
            dispatcher=dispatcher,
            root_node_id=root_id,
            current_result=_control_only_result(),
        )

        assert backend.calls, "停在执行相位且没有执行器的根必须被重派"
        root = service.store.get_node(root_id)
        assert root is not None and root.status == "success"
        assert control_only is False
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_parked_root_is_not_revived(tmp_path: Path) -> None:
    backend = _QueuedChatBackend([_final_response(call_id="call:should-not-run", text="不该被点火")])
    service = _build_service(tmp_path, chat_backend=backend)
    try:
        record = await service.create_task("parked root", session_id="web:shared")
        task_id, root_id = record.task_id, record.root_node_id
        _write_inflight_frame(service, task_id, root_id, "waiting_explicit")
        dispatcher = service.task_actor_service._create_dispatcher(task_id)
        service.task_actor_service._dispatchers[task_id] = dispatcher

        _result, control_only = await service.task_actor_service._drive_scheduler_loop(
            task_id=task_id,
            dispatcher=dispatcher,
            root_node_id=root_id,
            current_result=_control_only_result(),
        )

        assert backend.calls == [], "停靠中的根不得被点火"
        assert control_only is True
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stranded_root_is_parked_when_revival_cannot_start_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _QueuedChatBackend([_final_response(call_id="call:parked", text="不该跑")])
    service = _build_service(tmp_path, chat_backend=backend)
    fake_logger = MagicMock()
    monkeypatch.setattr("main.runtime.task_actor_service.logger", fake_logger)
    try:
        record = await service.create_task("unrevivable root", session_id="web:shared")
        task_id, root_id = record.task_id, record.root_node_id
        _write_inflight_frame(service, task_id, root_id, "before_model")
        dispatcher = service.task_actor_service._create_dispatcher(task_id)

        async def _never_resumed(*_args, **_kwargs) -> str:
            return "skipped:node_terminal"

        monkeypatch.setattr(service.task_actor_service, "resume_node_entry", _never_resumed)

        _result, control_only = await service.task_actor_service._drive_scheduler_loop(
            task_id=task_id,
            dispatcher=dispatcher,
            root_node_id=root_id,
            current_result=_control_only_result(),
        )

        assert backend.calls == []
        assert control_only is True
        root = service.store.get_node(root_id)
        assert root is not None and bool(getattr(root, "is_paused", False))
        errors = list(service.store.list_task_error_logs(task_id))
        assert any("stranded_root" in str(getattr(item, "error_text", "")) for item in errors)
    finally:
        await service.close()
