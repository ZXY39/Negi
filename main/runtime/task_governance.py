from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from main.protocol import now_iso

GOVERNANCE_PATCH_EVENT_TYPE = "task.governance.patch"
GOVERNANCE_LIMIT_REJECTION_TEXT = "[当前已达到最大深度，不允许派生子节点，请自行执行!]"


def normalize_task_governance_history_entry(value: Any) -> dict[str, Any]:
    source = dict(value or {}) if isinstance(value, dict) else {}
    trigger_snapshot = source.get("trigger_snapshot")
    snapshot = dict(trigger_snapshot) if isinstance(trigger_snapshot, dict) else {}
    try:
        max_depth = max(0, int(snapshot.get("max_depth") or 0))
    except (TypeError, ValueError):
        max_depth = 0
    try:
        total_nodes = max(0, int(snapshot.get("total_nodes") or 0))
    except (TypeError, ValueError):
        total_nodes = 0
    decision = str(source.get("decision") or "").strip().lower()
    if decision not in {"allow", "cap_current_depth"}:
        decision = "allow"
    try:
        limited_depth = max(0, int(source.get("limited_depth") or 0))
    except (TypeError, ValueError):
        limited_depth = 0
    evidence = [
        str(item or "").strip()
        for item in list(source.get("evidence") or [])
        if str(item or "").strip()
    ]
    return {
        "triggered_at": str(source.get("triggered_at") or "").strip(),
        "trigger_reason": str(source.get("trigger_reason") or "").strip(),
        "trigger_snapshot": {
            "max_depth": max_depth,
            "total_nodes": total_nodes,
        },
        "decision": decision,
        "decision_reason": str(source.get("decision_reason") or "").strip(),
        "limited_depth": limited_depth,
        "evidence": evidence,
    }


def normalize_task_governance_state(value: Any) -> dict[str, Any]:
    source = dict(value or {}) if isinstance(value, dict) else {}
    try:
        depth_baseline = max(1, int(source.get("depth_baseline") or 1))
    except (TypeError, ValueError):
        depth_baseline = 1
    try:
        node_count_baseline = max(0, int(source.get("node_count_baseline") or 0))
    except (TypeError, ValueError):
        node_count_baseline = 0
    try:
        hard_limited_depth = max(0, int(source.get("hard_limited_depth") or 0))
    except (TypeError, ValueError):
        hard_limited_depth = 0
    history = [
        normalize_task_governance_history_entry(item)
        for item in list(source.get("history") or [])
        if isinstance(item, dict)
    ]
    return {
        "enabled": source.get("enabled") is not False,
        "frozen": bool(source.get("frozen")),
        "review_inflight": bool(source.get("review_inflight")),
        "depth_baseline": depth_baseline,
        "node_count_baseline": node_count_baseline,
        "hard_limited_depth": hard_limited_depth,
        "supervision_disabled_after_limit": bool(source.get("supervision_disabled_after_limit")),
        "last_trigger_reason": str(source.get("last_trigger_reason") or "").strip(),
        "last_decision": str(source.get("last_decision") or "").strip().lower(),
        "history": history,
    }


class TaskGovernanceManager:
    def __init__(
        self,
        *,
        service: Any,
        reviewer: Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._service = service
        self._reviewer = reviewer or self._default_review
        self._review_tasks: dict[str, asyncio.Task[None]] = {}

    def is_task_frozen(self, task_id: str) -> bool:
        state = self.read_state(task_id)
        return bool(state.get("frozen")) or bool(state.get("review_inflight"))

    def read_state(self, task_id: str) -> dict[str, Any]:
        runtime_meta = self._service.log_service.read_task_runtime_meta(task_id) or {}
        return normalize_task_governance_state(runtime_meta.get("governance"))

    def on_execution_child_created(self, *, task_id: str, child_node: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = self._service.get_task(task_id)
        child_depth = int(getattr(child_node, "depth", 0) or 0)
        if task is None or str(getattr(task, "status", "") or "").strip().lower() != "in_progress":
            return
        current_state = self.read_state(task_id)
        if (
            not bool(current_state.get("enabled"))
            or bool(current_state.get("frozen"))
            or bool(current_state.get("review_inflight"))
            or bool(current_state.get("supervision_disabled_after_limit"))
        ):
            return
        snapshot = self._tree_snapshot(task_id)
        reasons: list[str] = []
        if int(snapshot["max_depth"]) >= int(current_state["depth_baseline"]) + 1:
            reasons.append("depth+1")
        node_baseline = max(0, int(current_state.get("node_count_baseline") or 0))
        if int(snapshot["total_nodes"]) >= 16 and node_baseline > 0 and int(snapshot["total_nodes"]) >= node_baseline * 2:
            reasons.append("node_count_double")
        if not reasons:
            return
        trigger_reason = ",".join(reasons)
        next_state = {
            **current_state,
            "frozen": True,
            "review_inflight": True,
            "last_trigger_reason": trigger_reason,
        }
        self._service.log_service.upsert_task_governance(task_id, next_state)
        if task_id in self._review_tasks and not self._review_tasks[task_id].done():
            return
        review_task = loop.create_task(
            self._run_review(
                task_id=task_id,
                trigger_reason=trigger_reason,
                trigger_snapshot=snapshot,
                child_depth=child_depth,
            ),
            name=f"task-governance-review:{task_id}",
        )
        self._review_tasks[task_id] = review_task
        review_task.add_done_callback(lambda done_task, stored_task_id=task_id: self._clear_review_task(stored_task_id, done_task))

    def spawn_refusal_message(self, *, task_id: str, parent_depth: int) -> str:
        task = self._service.get_task(task_id)
        if task is None:
            return ""
        state = self.read_state(task_id)
        if not bool(state.get("supervision_disabled_after_limit")):
            return ""
        next_depth = int(parent_depth or 0) + 1
        if next_depth > int(task.max_depth or 0):
            return GOVERNANCE_LIMIT_REJECTION_TEXT
        return ""

    async def close(self) -> None:
        tasks = list(self._review_tasks.values())
        self._review_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _clear_review_task(self, task_id: str, done_task: asyncio.Task[None]) -> None:
        current = self._review_tasks.get(task_id)
        if current is done_task:
            self._review_tasks.pop(task_id, None)

    def _tree_snapshot(self, task_id: str) -> dict[str, int]:
        nodes = list(self._service.store.list_task_nodes(task_id) or [])
        max_depth = 0
        for item in nodes:
            try:
                max_depth = max(max_depth, int(getattr(item, "depth", 0) or 0))
            except Exception:
                continue
        return {
            "max_depth": max_depth,
            "total_nodes": len(nodes),
        }

    async def _run_review(
        self,
        *,
        task_id: str,
        trigger_reason: str,
        trigger_snapshot: dict[str, int],
        child_depth: int,
    ) -> None:
        task = self._service.get_task(task_id)
        if task is None:
            return
        root = self._service.get_node(task.root_node_id)
        review_context = {
            "task_id": task.task_id,
            "task_title": str(getattr(task, "title", "") or ""),
            "user_request": str(getattr(task, "user_request", "") or ""),
            "core_requirement": str((dict(getattr(task, "metadata", {}) or {})).get("core_requirement") or ""),
            "root_prompt": str(getattr(root, "prompt", "") or ""),
            "trigger_reason": trigger_reason,
            "trigger_snapshot": dict(trigger_snapshot),
            "child_depth": int(child_depth or 0),
            "task_progress_text": self._service.view_progress(task_id, mark_read=False),
        }
        review_result = await self._call_reviewer(review_context)
        current_snapshot = self._tree_snapshot(task_id)
        current_state = self.read_state(task_id)
        decision = str(review_result.get("decision") or "").strip().lower()
        if decision not in {"allow", "cap_current_depth"}:
            decision = "allow"
        decision_reason = str(review_result.get("reason") or "").strip()
        evidence = [
            str(item or "").strip()
            for item in list(review_result.get("evidence") or [])
            if str(item or "").strip()
        ]
        next_state = {
            **current_state,
            "frozen": False,
            "review_inflight": False,
            "last_decision": decision,
            "history": [
                *list(current_state.get("history") or []),
                normalize_task_governance_history_entry(
                    {
                        "triggered_at": now_iso(),
                        "trigger_reason": trigger_reason,
                        "trigger_snapshot": trigger_snapshot,
                        "decision": decision,
                        "decision_reason": decision_reason,
                        "limited_depth": int(current_snapshot["max_depth"]) if decision == "cap_current_depth" else 0,
                        "evidence": evidence,
                    }
                ),
            ],
        }
        if decision == "cap_current_depth":
            limited_depth = int(current_snapshot["max_depth"])
            self._service.log_service.update_task_max_depth(task_id, limited_depth)
            next_state.update(
                {
                    "hard_limited_depth": limited_depth,
                    "supervision_disabled_after_limit": True,
                }
            )
        else:
            next_state.update(
                {
                    "depth_baseline": max(1, int(current_snapshot["max_depth"])),
                    "node_count_baseline": max(0, int(current_snapshot["total_nodes"])),
                }
            )
        self._service.log_service.upsert_task_governance(task_id, next_state)
        controller = getattr(self._service, "node_turn_controller", None)
        if controller is not None:
            controller.poke()

    async def _call_reviewer(self, context: dict[str, Any]) -> dict[str, Any]:
        result = self._reviewer(context)
        if asyncio.iscoroutine(result):
            return dict(await result)
        return dict(result or {})

    @staticmethod
    async def _default_review(context: dict[str, Any]) -> dict[str, Any]:
        trigger_reason = str(context.get("trigger_reason") or "").strip().lower()
        if "depth+1" in trigger_reason:
            return {
                "decision": "cap_current_depth",
                "reason": "任务树深度已超过监管基线，判定为过度派生，限制到当前深度。",
                "evidence": [f"trigger={trigger_reason}"],
            }
        return {
            "decision": "allow",
            "reason": "节点数量扩张但未继续增加深度，本次允许继续执行。",
            "evidence": [f"trigger={trigger_reason}"],
        }
