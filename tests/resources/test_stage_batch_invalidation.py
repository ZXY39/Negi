"""阶段切换失败时，同批普通调用要不要一起作废——判据是"阶段账本被动过没有"。

背景（`docs/FIX_PLAN_stage_transition_interlock_batch_block.md`，实盘 task:c3c29284fb4e）：
一批里 `submit_next_stage` 被拒后，同批的普通调用全部被写成
`Error: submit_next_stage failed earlier in this turn`，实测 85,773 条工具结果里 14 批 / 22 条
属于这一类，且**全部**是"改账本之前就被判拒"（参数契约、drop↔summary 配对、keep_* 闸门、
无实质进度），没有一例是改到一半失败。账本一字未动时，活动阶段还是模型上一跳看过的那份，
作废整批只是白烧一轮。

两条不变量都要守住，所以用例成对：
1) 提交前被拒 ⇒ 普通调用照常执行（单工具自己的阶段闸门仍然把关）；
2) 阶段调用动过账本后才失败、或账本读不到 ⇒ 普通调用照旧作废（fail-closed）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.providers.base import ToolCallRequest
from main.runtime.internal_tools import STAGE_TOOL_NAME
from main.runtime.react_loop import ReActToolLoop

TASK_ID = "task-batch-invalidation"
NODE_ID = "node-batch-invalidation"


def _stages(*, rounds_used: int = 1) -> dict:
    return {
        "active_stage_id": "stage:1",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "stage:1",
                "stage_index": 1,
                "stage_kind": "normal",
                "mode": "自主执行",
                "status": "进行中",
                "stage_goal": "跑实验",
                "completed_stage_summary": "",
                "tool_round_budget": 6,
                "tool_rounds_used": rounds_used,
                "key_refs": [],
                "rounds": [],
                "created_at": "2026-10-09T06:36:27+08:00",
                "finished_at": "",
            }
        ],
    }


class _Logs:
    """最小账本替身：`_store.get_node` 是阶段态与签名读数唯一的来源。"""

    def __init__(self, stages: dict) -> None:
        self.node = SimpleNamespace(
            node_id=NODE_ID,
            task_id=TASK_ID,
            node_kind="execution",
            status="in_progress",
            metadata={"execution_stages": stages},
        )
        self._store = _Store(self.node)
        self._content_store = None
        self._frames: dict[tuple[str, str], dict] = {}

    def set_pause_state(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def update_node_input(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def upsert_frame(self, task_id, payload, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        self._frames[(str(task_id), str(payload.get("node_id") or ""))] = dict(payload or {})

    def update_frame(self, task_id, node_id, mutate, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        key = (str(task_id), str(node_id))
        self._frames[key] = dict(mutate(dict(self._frames.get(key) or {})) or {})

    def read_runtime_frame(self, task_id, node_id):
        return dict(self._frames.get((str(task_id), str(node_id))) or {})

    def append_node_output(self, *args, **kwargs) -> None:
        _ = args, kwargs

    def record_execution_stage_round(self, *args, **kwargs) -> None:
        # 普通调用真被执行后才会走到这里（作废路径不会），所以替身只要吞掉。
        _ = args, kwargs

    def execution_stage_gate_snapshot(self, task_id, node_id) -> dict:
        stages = self.node.metadata.get("execution_stages") or {}
        active = next(
            (s for s in list(stages.get("stages") or []) if str(s.get("stage_id") or "") == str(stages.get("active_stage_id") or "")),
            None,
        )
        return {
            "enabled": True,
            "has_active_stage": active is not None,
            "transition_required": bool(stages.get("transition_required")),
            "active_stage": active,
        }


class _Store:
    def __init__(self, node: SimpleNamespace) -> None:
        self._node = node
        self._task = SimpleNamespace(cancel_requested=False, pause_requested=False)

    def get_task(self, task_id: str):
        _ = task_id
        return self._task

    def get_node(self, node_id: str):
        return self._node if node_id == NODE_ID else None

    def get_node_pause_flags(self, node_id: str):
        return {"pause_requested": False, "is_paused": False, "pause_reason": ""}


class _ProbeTool(Tool):
    """被作废与否一眼可见：执行过就返回 PROBE-RAN，被闸门拦下则拿到 failed earlier 文案。"""

    @property
    def name(self) -> str:
        return "probe_tool"

    @property
    def description(self) -> str:
        return "probe"

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        _ = kwargs
        return "PROBE-RAN"


class _StageTool(Tool):
    def __init__(self, *, behaviour: str, logs: _Logs) -> None:
        self._behaviour = behaviour
        self._logs = logs

    @property
    def name(self) -> str:
        return STAGE_TOOL_NAME

    @property
    def description(self) -> str:
        return "stage"

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        _ = kwargs
        if self._behaviour == "reject_before_apply":
            # 参数契约/闸门类拒绝：账本没动
            raise ValueError("drop_completed_stage_tool_detail requires a non-empty completed_stage_summary in the same call")
        if self._behaviour == "mutate_then_fail":
            stages = self._logs.node.metadata["execution_stages"]
            stages["active_stage_id"] = "stage:2"
            stages["stages"].append(
                {
                    "stage_id": "stage:2",
                    "stage_index": 2,
                    "stage_kind": "normal",
                    "mode": "自主执行",
                    "status": "进行中",
                    "stage_goal": "新阶段",
                    "completed_stage_summary": "",
                    "tool_round_budget": 4,
                    "tool_rounds_used": 0,
                    "key_refs": [],
                    "rounds": [],
                    "created_at": "2026-10-09T06:37:00+08:00",
                    "finished_at": "",
                }
            )
            raise RuntimeError("archive write exploded after the transition was applied")
        return json.dumps({"ok": True})


async def _run_batch(behaviour: str) -> tuple[list[str], _Logs]:
    logs = _Logs(_stages())
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=logs, max_iterations=2)
    results = await loop._execute_tool_calls(
        task=SimpleNamespace(task_id=TASK_ID),
        node=SimpleNamespace(node_id=NODE_ID, depth=0, node_kind="execution"),
        response_tool_calls=[
            ToolCallRequest(id="call-stage", name=STAGE_TOOL_NAME, arguments={}),
            ToolCallRequest(id="call-probe", name="probe_tool", arguments={}),
        ],
        tools={STAGE_TOOL_NAME: _StageTool(behaviour=behaviour, logs=logs), "probe_tool": _ProbeTool()},
        allowed_content_refs=[],
        runtime_context={"task_id": TASK_ID, "node_id": NODE_ID, "actor_role": "execution"},
    )
    contents = [str((item.get("tool_message") or {}).get("content") or "") for item in results]
    return contents, logs


@pytest.mark.asyncio
async def test_rejected_before_apply_does_not_void_the_batch() -> None:
    contents, _logs = await _run_batch("reject_before_apply")
    assert contents[0].startswith("Error")  # 阶段调用本身仍是错误回执
    assert "PROBE-RAN" in contents[1]
    assert "failed earlier in this turn" not in contents[1]


@pytest.mark.asyncio
async def test_ledger_move_still_voids_the_batch() -> None:
    contents, _logs = await _run_batch("mutate_then_fail")
    assert contents[0].startswith("Error")
    assert "failed earlier in this turn" in contents[1]
    assert "PROBE-RAN" not in contents[1]
