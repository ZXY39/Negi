from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.main_runtime import LoadToolContextTool
from main.governance.tool_context import build_tool_context_fingerprint
from main.models import NodeRecord, TaskRecord
from main.protocol import now_iso
from main.runtime.node_runner import NodeRunner
from main.runtime.react_loop import ReActToolLoop


class _ProbeTaskStore:
    def __init__(self, *, task: TaskRecord, node: NodeRecord) -> None:
        self._task = task
        self._node = node

    def get_task(self, task_id: str):
        _ = task_id
        return self._task

    def get_node(self, node_id: str):
        if str(node_id or "").strip() == str(self._node.node_id or "").strip():
            return self._node
        return None

    def update_node(self, node_id: str, mutate):
        if str(node_id or "").strip() != str(self._node.node_id or "").strip():
            return None
        self._node = mutate(self._node)
        return self._node


class _ProbeLogService:
    def __init__(self) -> None:
        self.frames: dict[tuple[str, str], dict[str, object]] = {}

    def upsert_frame(self, task_id: str, payload: dict[str, object], publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        node_id = str((payload or {}).get("node_id") or "").strip()
        self.frames[(str(task_id), node_id)] = dict(payload or {})

    def update_frame(self, task_id: str, node_id: str, mutate, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        key = (str(task_id), str(node_id))
        current = dict(self.frames.get(key) or {})
        self.frames[key] = dict(mutate(current) or {})

    def read_runtime_frame(self, task_id: str, node_id: str):
        return dict(self.frames.get((str(task_id), str(node_id))) or {})


@pytest.mark.asyncio
async def test_react_loop_records_memory_probe_before_direct_load_repeat_check() -> None:
    class _InlineToolContextService:
        async def startup(self) -> None:
            return None

        def load_tool_context_v2(self, **kwargs):
            _ = kwargs
            return {
                "ok": True,
                "tool_id": "exec",
                "content": "# exec",
                "parameter_contract_markdown": "## Parameter Contract",
                "required_parameters": ["command"],
                "example_arguments": {"command": "pwd"},
                "warnings": [],
                "errors": [],
                "callable": True,
                "available": True,
                "repair_required": False,
                "callable_now": True,
                "will_be_hydrated_next_turn": False,
                "hydration_targets": [],
                "tool_context_fingerprint": "tcf:current",
            }

    log_service = _ProbeLogService()
    task_id = "task-memory-probe"
    node_id = "node-memory-probe"
    log_service.upsert_frame(task_id, {"node_id": node_id, "phase": "before_model"}, publish_snapshot=False)
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=log_service, max_iterations=2)

    prior_payload = {
        "ok": True,
        "tool_id": "exec",
        "content": "# exec",
        "parameter_contract_markdown": "## Parameter Contract",
        "required_parameters": ["command"],
        "example_arguments": {"command": "pwd"},
        "warnings": [],
        "errors": [],
        "callable": True,
        "available": True,
        "repair_required": False,
        "callable_now": True,
        "will_be_hydrated_next_turn": False,
        "hydration_targets": [],
    }
    prior_payload["tool_context_fingerprint"] = build_tool_context_fingerprint(prior_payload)

    violations = await loop._load_tool_context_direct_read_violations(
        response_tool_calls=[SimpleNamespace(id="call-1", name="load_tool_context", arguments={"tool_id": "exec"})],
        task=SimpleNamespace(task_id=task_id),
        node=SimpleNamespace(node_id=node_id, node_kind="execution"),
        message_history=[
            {
                "role": "tool",
                "tool_call_id": "call-old",
                "name": "load_tool_context",
                "content": json.dumps(prior_payload, ensure_ascii=False),
            }
        ],
        runtime_context={
            "session_key": "web:shared",
            "actor_role": "execution",
            "rbac_visible_tool_names": ["exec"],
            "callable_tool_names": ["load_tool_context", "exec"],
            "full_callable_tool_names": ["load_tool_context", "exec"],
        },
        tools={"load_tool_context": LoadToolContextTool(lambda: _InlineToolContextService())},
    )

    frame = log_service.read_runtime_frame(task_id, node_id)
    probe = dict(frame.get("memory_error_probe") or {})

    assert violations
    assert probe["stage"] == "load_tool_context_direct_read_violations"
    assert probe["message_history_count"] == 1
    assert probe["response_tool_call_count"] == 1
    assert probe["latest_load_tool_context_message_count"] == 1
    assert probe["requested_tool_ids"] == ["exec"]


def test_node_runner_captures_memory_error_diagnostics_snapshot() -> None:
    task = TaskRecord(
        task_id="task-memory-error",
        session_id="web:shared",
        title="memory error diagnostics",
        user_request="diagnose runtime memory error",
        status="in_progress",
        root_node_id="node-memory-error",
        max_depth=0,
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    node = NodeRecord(
        node_id="node-memory-error",
        task_id=task.task_id,
        root_node_id="node-memory-error",
        depth=0,
        node_kind="execution",
        status="in_progress",
        goal="diagnose runtime memory error",
        prompt="diagnose runtime memory error",
        created_at=now_iso(),
        updated_at=now_iso(),
        metadata={
            "latest_runtime_actual_request_ref": "artifact:artifact:abc123",
            "latest_runtime_actual_request_message_count": 29,
            "latest_runtime_observed_input_truth": {
                "effective_input_tokens": 40480,
                "input_tokens": 6176,
                "cache_hit_tokens": 34304,
            },
            "execution_stages": {
                "active_stage_id": "stage-oom",
                "stages": [
                    {
                        "stage_id": "stage-oom",
                        "stage_goal": "probe the next before_model transition",
                    }
                ],
            },
        },
    )
    store = _ProbeTaskStore(task=task, node=node)
    log_service = _ProbeLogService()
    log_service.upsert_frame(
        task.task_id,
        {
            "node_id": node.node_id,
            "phase": "before_model",
            "await_marker": "react_loop.run",
            "stage_goal": "probe the next before_model transition",
            "memory_error_probe": {
                "stage": "load_tool_context_direct_read_violations",
                "message_history_count": 12,
                "requested_tool_ids": ["content_open", "content_search"],
            },
        },
        publish_snapshot=False,
    )

    runner = NodeRunner(
        store=store,
        log_service=log_service,
        react_loop=SimpleNamespace(),
        tool_provider=lambda task, node: {},
        execution_model_refs=["execution"],
        acceptance_model_refs=["inspection"],
    )

    runner._capture_memory_error_diagnostics(task_id=task.task_id, node_id=node.node_id, exc=MemoryError())

    saved = dict(store.get_node(node.node_id).metadata.get("memory_error_diagnostics") or {})
    frame = log_service.read_runtime_frame(task.task_id, node.node_id)

    assert saved["exception_type"] == "MemoryError"
    assert saved["phase"] == "before_model"
    assert saved["await_marker"] == "react_loop.run"
    assert saved["latest_runtime_actual_request_ref"] == "artifact:artifact:abc123"
    assert saved["latest_runtime_actual_request_message_count"] == 29
    assert saved["memory_error_probe"]["stage"] == "load_tool_context_direct_read_violations"
    assert saved["memory_error_probe"]["requested_tool_ids"] == ["content_open", "content_search"]
    assert frame["memory_error_diagnostics"]["exception_type"] == "MemoryError"
