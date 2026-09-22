"""分发屏障冻结孤儿子节点事故回归（task:4c9e6546d2fe，2026-09-15）。

事故链：用户补充消息触发子树分发 epoch → 工具看门狗轮询在 spawn 长等待内部
抛 DistributionHoldError → 被兜底捕获转成模型可见工具错误、看门狗取消在飞
spawn 协程 → spawn entries 被盖 error、恢复帧被批后帧写清空 → 同 id 重放通道
被毁、重试闸门拒绝新 id → 子节点冻结后释放复活静默失败 → 暂停/恢复只救活根
节点，子节点成永久幽灵 in_progress。

本文件锁定修复契约：
- B1：在飞 spawn 遇 hold/节点暂停不再转工具错误（控制异常直通）；
- B2：hold 期间的看门狗取消不再把 spawn entries 盖成 error；
- B3 配套：cancel_nodes 对被 hold 冻结的 entry 强制解析 future（不死锁）；
- B4：冻结帧保留恢复入口，释放后以原 tool_call_id 重放 spawn 并重挂原子节点；
- A1：陈旧 epoch meta（已 completed/cancelled/查无）不再冻结；failed 仍冻结；
- A3：释放后校验清扫对卡死节点再 resume 一次；
- C：run_task 入口对派生树孤儿 in_progress 节点做决断（可重放则重派发，否则收尸）；
- D：轮次投影子节点计数以绑定节点真实状态优先。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import g3ku.runtime.tool_watchdog as tool_watchdog_module
import main.runtime.react_loop as react_loop_module
import main.runtime.task_actor_service as task_actor_module
from g3ku.providers.base import LLMResponse, ToolCallRequest
from g3ku.runtime.tool_watchdog import ToolWatchdogConfig
from main.errors import DistributionHoldError
from main.models import NodeFinalResult, SpawnChildSpec
from main.monitoring.log_service import TaskLogService
from main.runtime.subtree_hold import (
    make_epoch_state_lookup,
    resolve_subtree_hold_epoch_id,
)
from main.runtime.task_actor_service import TaskActorService
from main.service.runtime_service import MainRuntimeService


@pytest.fixture(autouse=True)
def _default_node_send_preflight_context_window(monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.fixture
def _fast_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    """看门狗轮询加速到 0.2s：hold/pause 检查点必须能在 spawn 等待窗口内触发。"""

    def _fast_config(runtime_context):
        return ToolWatchdogConfig(
            enabled=True,
            poll_interval_seconds=0.2,
            handoff_after_seconds=600.0,
            stop_grace_seconds=0.05,
        )

    monkeypatch.setattr(tool_watchdog_module, "resolve_tool_watchdog_config", _fast_config)


# ---------------------------------------------------------------------------
# 响应构造
# ---------------------------------------------------------------------------


def _final_response(call_id: str, summary: str = "done") -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCallRequest(
                id=call_id,
                name="submit_final_result",
                arguments={
                    "status": "success",
                    "delivery_status": "final",
                    "summary": summary,
                    "answer": summary,
                    "evidence": [],
                    "remaining_work": [],
                    "blocking_reason": "",
                },
            )
        ],
        finish_reason="tool_calls",
        usage={"input_tokens": 8, "output_tokens": 4},
    )


def _spawn_response(call_id: str, *, goal: str, prompt: str) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCallRequest(
                id=call_id,
                name="spawn_child_nodes",
                arguments={
                    "children": [
                        {
                            "goal": goal,
                            "prompt": prompt,
                            "execution_policy": {"mode": "focus"},
                        }
                    ],
                },
            )
        ],
        finish_reason="tool_calls",
        usage={"input_tokens": 8, "output_tokens": 4},
    )


def _review_allow_response(call_id: str) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCallRequest(
                id=call_id,
                name="review_spawn_candidates",
                arguments={"allowed_indexes": [0], "blocked_specs": []},
            )
        ],
        finish_reason="tool_calls",
        usage={"input_tokens": 8, "output_tokens": 2},
    )


async def _decision_skip_children(kwargs) -> LLMResponse:
    """控制回合决策：对每个活子节点显式 skip（校验要求逐一覆盖），通知留根本地。"""
    live_child_ids: list[str] = []
    for message in kwargs.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        for item in list(payload.get("live_children") or []):
            if isinstance(item, dict):
                child_id = str(item.get("node_id") or "").strip()
                if child_id and child_id not in live_child_ids:
                    live_child_ids.append(child_id)
    children = [
        {
            "target_node_id": child_id,
            "should_distribute": False,
            "action": "skip",
            "reason": "test: notice irrelevant to this child",
        }
        for child_id in live_child_ids
    ]
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCallRequest(
                id="call:decision-dyn",
                name="submit_message_distribution",
                arguments={"children": children, "notes": "root keeps the notice locally"},
            )
        ],
        finish_reason="tool_calls",
        usage={"input_tokens": 8, "output_tokens": 2},
    )


class _ScriptedBackend:
    """全局顺序脚本后端：审查/分发控制回合按工具名路由（确定性），
    其余按预定顺序消费。脚本耗尽即断言失败（意外多出一轮 = 契约被破坏）。"""

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, object]] = []

    async def chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        tools = kwargs.get("tools") or []
        names = set()
        for item in tools:
            if isinstance(item, dict):
                names.add(str((item.get("function") or {}).get("name") or ""))
        if not self._script:
            raise AssertionError(f"unexpected model call (script exhausted): tools={sorted(names)}")
        item = self._script.pop(0)
        if callable(item):
            return await item(kwargs)
        return item


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be used in this test: {kwargs!r}")


def _build_service(tmp_path: Path, backend) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=backend,
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )

    async def _noop_async(*args, **kwargs):
        _ = args, kwargs
        return None

    service.global_scheduler.enqueue_task = _noop_async
    service.global_scheduler.cancel_task = _noop_async
    service.global_scheduler.wait = _noop_async
    # 分发驱动器手动驱动，避免后台波次与断言竞态。
    service.task_actor_service.ensure_scoped_epoch_driver = lambda task_id: None
    return service


async def _wait_until(predicate, *, timeout: float = 8.0, message: str = "condition") -> None:
    deadline = time.perf_counter() + max(0.1, float(timeout))
    while time.perf_counter() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {message}")


def _set_spawn_operations(service: MainRuntimeService, *, root_node_id: str, payload: dict[str, object]) -> None:
    def _mutate(metadata: dict[str, object]) -> dict[str, object]:
        metadata["spawn_operations"] = payload
        return metadata

    service.log_service.update_node_metadata(root_node_id, _mutate)


def _entry_frozen(service: MainRuntimeService, task_id: str, node_id: str) -> bool:
    """冻结形态：entry 存在、future pending、entry task 已停。"""
    dispatcher = service.task_actor_service._dispatchers.get(task_id)
    if dispatcher is None:
        return False
    entry = dispatcher._entries.get(node_id)
    return (
        entry is not None
        and not entry.future.done()
        and entry.task is not None
        and entry.task.done()
    )


def _success_result(node_id: str) -> NodeFinalResult:
    text = f"{node_id} complete"
    return NodeFinalResult(
        status="success",
        delivery_status="final",
        summary=text,
        answer=text,
        evidence=[],
        remaining_work=[],
        blocking_reason="",
    )


# ---------------------------------------------------------------------------
# B1+B2+B4：在飞 spawn 遇分发屏障——干净冻结、释放后同 id 重放、重挂原子节点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inflight_spawn_freezes_on_hold_and_replays_same_child_after_release(
    tmp_path: Path,
    _fast_watchdog,
) -> None:
    child_gate = asyncio.Event()
    child_chat_entered = asyncio.Event()

    async def _gated_child_final(kwargs):
        child_chat_entered.set()
        await asyncio.wait_for(child_gate.wait(), timeout=15)
        return _final_response("call:child-final-1", summary="child work done")

    backend = _ScriptedBackend(
        [
            # 1) 根首轮：派生一个子节点
            _spawn_response("call:spawn-1", goal="child goal", prompt="child prompt"),
            # 2) spawn 治理审查：放行
            _review_allow_response("call:review-1"),
            # 3) 子节点首轮（被 gate 挡住制造在飞窗口；放行后工具经宽限执行、
            #    节点在 run_node 回环后 hold 检查点冻结，final 结果按设计丢弃）
            _gated_child_final,
            # 4) 根的分发控制回合：逐子 skip，通知留在本地
            _decision_skip_children,
            # 5) 子节点复活轮：冻结时帧的 pending 已清空（批已完成），需重新提交 final
            _final_response("call:child-final-2", summary="child work done (resumed)"),
            # 6) 根收尾轮（spawn 同 id 重放拿到子结果之后）
            _final_response("call:root-final", summary="root done"),
        ]
    )
    service = _build_service(tmp_path, backend)
    try:
        record = await service.create_task("root lane request", session_id="web:ceo-demo")
        task_id = record.task_id
        runner = asyncio.create_task(service.task_actor_service.run_task(task_id))

        # 等子节点物化（spawn 轮已建、子节点已绑定）
        child_node = None

        def _child_materialized():
            nonlocal child_node
            kids = [
                item
                for item in service.store.list_children(record.root_node_id)
                if str(getattr(item, "node_kind", "")).strip().lower() == "execution"
            ]
            if kids:
                child_node = kids[0]
                return True
            return False

        await _wait_until(_child_materialized, timeout=15, message="child materialized")
        # 等子节点真正进入首轮模型调用（被 gate 挡住）再播种通知，
        # 固定消费顺序：root-spawn → review → child-gated → control → root-final。
        await asyncio.wait_for(child_chat_entered.wait(), timeout=15)

        # 用户补充消息 → 子树分发屏障（meta 发布 barrier_requested，root/child 都在 blocked 内）
        task = service.get_task(task_id)
        assert task is not None
        await service.task_append_notice(
            task_ids=[task_id],
            node_ids=[],
            message="补充要求：冻结窗口开始",
            session_id=task.session_id,
        )

        # 根在 spawn 等待中被看门狗检查点冻结（B1：不再转工具错误）
        await _wait_until(
            lambda: _entry_frozen(service, task_id, record.root_node_id),
            timeout=15,
            message="root frozen by hold",
        )

        # B2：spawn entries 未被盖 error，轮保持未完成、绑定仍在
        root_mid = service.store.get_node(record.root_node_id)
        ops_mid = dict((root_mid.metadata or {}).get("spawn_operations") or {})
        round_mid = dict(ops_mid.get("call:spawn-1") or {})
        entries_mid = [dict(item) for item in list(round_mid.get("entries") or []) if isinstance(item, dict)]
        assert round_mid, "spawn 轮必须存在"
        assert not round_mid.get("completed"), "冻结期间轮不得被标记完成"
        assert entries_mid and entries_mid[0].get("child_node_id") == child_node.node_id
        assert entries_mid[0].get("status") != "error", "B2: hold 取消不得盖 error 记账"

        # B1/B4：根的恢复帧未被工具错误污染，且保留重放入口
        frame_mid = service.log_service.read_runtime_frame(task_id, record.root_node_id) or {}
        messages_blob = json.dumps(frame_mid.get("messages") or [], ensure_ascii=False)
        assert "Error executing spawn_child_nodes" not in messages_blob, "B1: hold 不得转工具错误"
        pending_ids = {
            str(item.get("id") or "").strip()
            for item in list(frame_mid.get("pending_tool_calls") or [])
            if isinstance(item, dict)
        }
        assert str(frame_mid.get("phase") or "") == "waiting_children" or "call:spawn-1" in pending_ids, (
            "B4: 冻结帧必须保留同 id 重放入口"
        )

        # 放行子节点首轮：其响应返回后在工具检查点被 hold 冻结（queued 工具调用保留）
        child_gate.set()
        await _wait_until(
            lambda: _entry_frozen(service, task_id, child_node.node_id),
            timeout=15,
            message="child frozen by hold",
        )

        # 手动驱动 epoch 波次到完成：完成后释放并复活 root+child
        outcome = "idle"
        for _ in range(16):
            outcome = await service.task_actor_service._run_distribution_epoch(task_id)
            if outcome in {"completed", "failed", "idle"}:
                break
            await asyncio.sleep(0.05)
        assert outcome == "completed", f"epoch 应完成，实际 {outcome}"

        # 释放后：根同 id 重放 spawn、重挂原子节点；子节点复活跑完；根收尾
        await asyncio.wait_for(runner, timeout=30)

        root_after = service.store.get_node(record.root_node_id)
        child_after = service.store.get_node(child_node.node_id)
        assert root_after is not None and child_after is not None
        assert str(root_after.status) == "success"
        assert str(child_after.status) == "success"

        ops_after = dict((root_after.metadata or {}).get("spawn_operations") or {})
        round_after = dict(ops_after.get("call:spawn-1") or {})
        entries_after = [dict(item) for item in list(round_after.get("entries") or []) if isinstance(item, dict)]
        assert round_after.get("completed") is True, "重放后轮应正常完成"
        assert [str(item.get("status")) for item in entries_after] == ["success"]
        assert entries_after[0].get("child_node_id") == child_node.node_id, "B4: 必须重挂原子节点"

        execution_children = [
            item
            for item in service.store.list_children(record.root_node_id)
            if str(getattr(item, "node_kind", "")).strip().lower() == "execution"
        ]
        assert len(execution_children) == 1, "不得重复派生子节点"

        frame_after = service.log_service.read_runtime_frame(task_id, record.root_node_id) or {}
        assert "Error executing spawn_child_nodes" not in json.dumps(frame_after.get("messages") or [], ensure_ascii=False)
        assert service.store.list_task_error_logs(task_id) == []
    finally:
        child_gate.set()
        await service.close()


# ---------------------------------------------------------------------------
# B1（NodePausedError 同款）：在飞 spawn 遇节点暂停不转工具错误
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inflight_spawn_node_pause_freezes_without_tool_error(
    tmp_path: Path,
    _fast_watchdog,
) -> None:
    child_gate = asyncio.Event()
    child_chat_entered = asyncio.Event()

    async def _gated_child_final(kwargs):
        child_chat_entered.set()
        await asyncio.wait_for(child_gate.wait(), timeout=15)
        return _final_response("call:child-final-p", summary="child done")

    backend = _ScriptedBackend(
        [
            _spawn_response("call:spawn-p", goal="child goal", prompt="child prompt"),
            _review_allow_response("call:review-p"),
            _gated_child_final,
        ]
    )
    service = _build_service(tmp_path, backend)
    try:
        record = await service.create_task("pause lane request", session_id="web:ceo-demo")
        task_id = record.task_id
        runner = asyncio.create_task(service.task_actor_service.run_task(task_id))

        child_node = None

        def _child_materialized():
            nonlocal child_node
            kids = [
                item
                for item in service.store.list_children(record.root_node_id)
                if str(getattr(item, "node_kind", "")).strip().lower() == "execution"
            ]
            if kids:
                child_node = kids[0]
                return True
            return False

        await _wait_until(_child_materialized, timeout=15, message="child materialized")
        await asyncio.wait_for(child_chat_entered.wait(), timeout=15)

        # 操作员暂停根节点：看门狗轮询检查点抛 NodePausedError
        service.log_service.set_node_pause_state(
            task_id,
            record.root_node_id,
            pause_requested=True,
            is_paused=True,
            pause_reason="manual",
            remark="test pause",
        )

        # run_task 以节点暂停收场（control_only_return），不得把任务打成 failed
        await asyncio.wait_for(runner, timeout=20)

        root_after = service.store.get_node(record.root_node_id)
        assert root_after is not None
        assert bool(root_after.is_paused) or bool(root_after.pause_requested)
        task_after = service.get_task(task_id)
        assert task_after is not None
        assert str(task_after.status) != "failed"

        frame = service.log_service.read_runtime_frame(task_id, record.root_node_id) or {}
        assert "Error executing spawn_child_nodes" not in json.dumps(frame.get("messages") or [], ensure_ascii=False), (
            "B1: 节点暂停同样不得转工具错误"
        )
    finally:
        child_gate.set()
        await service.close()


# ---------------------------------------------------------------------------
# B3 配套：cancel_nodes 对被 hold 冻结（取消转 hold）的 entry 不死锁
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_nodes_resolves_held_entry_without_deadlock(tmp_path: Path) -> None:
    service = _build_service(tmp_path, _DummyChatBackend())
    try:
        record = await service.create_task("cancel hold task", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=SpawnChildSpec(goal="g", prompt="p", execution_policy={"mode": "focus"}),
        )

        async def _hold_converting_run_node(task_id: str, node_id: str) -> NodeFinalResult:
            # 模拟 B3 后的 run_node：无标志取消 + 活动 hold → 转冻结（future 保持 pending）
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise DistributionHoldError(task_id, node_id, "epoch:x")
            raise AssertionError("should have been cancelled")

        service.node_runner.run_node = _hold_converting_run_node
        dispatcher = service.task_actor_service._create_dispatcher(record.task_id)
        service.task_actor_service._dispatchers[record.task_id] = dispatcher
        try:
            waiter = asyncio.create_task(dispatcher.execute_node(record.task_id, child.node_id))
            await _wait_until(
                lambda: dispatcher._entries.get(child.node_id) is not None
                and dispatcher._entries[child.node_id].task is not None
                and not dispatcher._entries[child.node_id].task.done(),
                timeout=5,
                message="child entry running",
            )

            fail_calls: list[tuple[str, str]] = []
            real_fail = service.node_runner.fail_paused_node

            def _tracking_fail(task_id: str, node_id: str, reason: str = "") -> NodeFinalResult:
                fail_calls.append((node_id, reason))
                return real_fail(task_id, node_id, reason)

            service.node_runner.fail_paused_node = _tracking_fail

            # 修复前：cancel 后 run_node 转 hold、future 保持 pending，
            # 旧实现 shield-await future 永远等不到 → 死锁。
            await asyncio.wait_for(dispatcher.cancel_nodes([child.node_id]), timeout=3)
            result = await asyncio.wait_for(waiter, timeout=3)
            assert result.status == "failed"
            assert fail_calls and fail_calls[0][0] == child.node_id
        finally:
            await dispatcher.close()
            service.task_actor_service._dispatchers.pop(record.task_id, None)
    finally:
        await service.close()


# ---------------------------------------------------------------------------
# A1：陈旧 epoch meta 不再冻结；failed 仍冻结；校验通道故障保守维持
# ---------------------------------------------------------------------------


def test_hold_predicate_rejects_stale_epoch_and_keeps_failed() -> None:
    nodes = {
        "child": SimpleNamespace(parent_node_id="target"),
        "target": SimpleNamespace(parent_node_id=""),
    }
    base = {
        "active_epoch_id": "epoch:1",
        "state": "barrier_draining",
        "target_node_ids": ["target"],
        "blocked_node_ids": ["child"],
        "frontier_node_ids": [],
    }
    stale_events: list[tuple[str, str, str]] = []

    def _resolve(**kwargs):
        return resolve_subtree_hold_epoch_id(
            distribution=base,
            get_node=nodes.get,
            node_id="child",
            on_stale_hold=lambda node_id, epoch_id, db_state: stale_events.append((node_id, epoch_id, db_state)),
            **kwargs,
        )

    # 已完成 epoch 的陈旧 meta：不冻结 + 告警
    assert _resolve(get_epoch_state=lambda epoch_id: "completed") == ""
    assert stale_events == [("child", "epoch:1", "completed")]
    # 查无 epoch：不冻结
    assert _resolve(get_epoch_state=lambda epoch_id: "none") == ""
    # failed：按设计保持冻结
    assert _resolve(get_epoch_state=lambda epoch_id: "failed") == "epoch:1"
    # 活跃态：保持冻结
    assert _resolve(get_epoch_state=lambda epoch_id: "barrier_draining") == "epoch:1"

    # 校验通道自身故障：保守维持 hold（fail-safe）
    def _boom(epoch_id: str) -> str:
        raise RuntimeError("store down")

    assert _resolve(get_epoch_state=_boom) == "epoch:1"
    # 未注入校验（旧调用方形态）：行为不变
    assert _resolve() == "epoch:1"

    # 告警回调抛异常不得影响判定
    def _bad_callback(node_id: str, epoch_id: str, db_state: str) -> None:
        raise RuntimeError("logger down")

    assert (
        resolve_subtree_hold_epoch_id(
            distribution=base,
            get_node=nodes.get,
            node_id="child",
            get_epoch_state=lambda epoch_id: "completed",
            on_stale_hold=_bad_callback,
        )
        == ""
    )


def test_epoch_state_lookup_active_fallback() -> None:
    class _Store:
        def __init__(self, epoch=None, actives=None):
            self._epoch = epoch
            self._actives = list(actives or [])

        def get_task_message_distribution_epoch(self, task_id: str, epoch_id: str):
            return self._epoch

        def list_active_task_message_distribution_epochs(self, task_id: str):
            return list(self._actives)

    lookup = make_epoch_state_lookup(_Store(epoch=None), "task:1")
    assert lookup("epoch:1") == "none"
    lookup = make_epoch_state_lookup(_Store(epoch=SimpleNamespace(state="completed")), "task:1")
    assert lookup("epoch:1") == "completed"
    # meta 未记 id 的 'active' 兜底：无非终态 epoch → none
    lookup = make_epoch_state_lookup(_Store(actives=[]), "task:1")
    assert lookup("active") == "none"
    # 混合时取最早的非终态者
    lookup = make_epoch_state_lookup(
        _Store(actives=[SimpleNamespace(state="completed"), SimpleNamespace(state="barrier_draining")]),
        "task:1",
    )
    assert lookup("active") == "barrier_draining"


# ---------------------------------------------------------------------------
# A3：释放后校验清扫——卡死节点再 resume 一次，仍卡死落 ERROR
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_verification_sweep_re_resumes_wedged_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_actor_module, "_RELEASE_VERIFICATION_DELAY_SECONDS", 0.02)
    service = object.__new__(TaskActorService)
    service._release_sweeps = {}

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    done_task = asyncio.create_task(asyncio.sleep(0))
    await done_task
    entry = SimpleNamespace(
        node_id="node:wedged",
        future=future,
        task=done_task,
        role="execution",
        interrupt_result=None,
        queued_counted=False,
        running_counted=False,
    )
    resumed: list[str] = []

    async def _resume(node_id: str) -> None:
        resumed.append(node_id)

    dispatcher = SimpleNamespace(_entries={"node:wedged": entry}, resume_node=_resume)
    service._dispatchers = {"task:w": dispatcher}
    service._store = SimpleNamespace(
        get_node=lambda node_id: SimpleNamespace(status="in_progress", is_paused=False, pause_requested=False),
    )
    service._node_runner = SimpleNamespace(_subtree_hold_epoch_id=lambda *, task_id, node_id: "")
    service._node_operator_paused = lambda node: False

    service._schedule_release_verification("task:w", ["node:wedged"])
    sweep = service._release_sweeps.get("task:w")
    assert sweep is not None
    await asyncio.wait_for(sweep, timeout=3)
    # 第一段：再 resume 一次；第二段：仍卡死只落 ERROR（不再重复 resume）
    assert resumed == ["node:wedged"]
    assert not future.done(), "清扫只复活，不伪造结果"


# ---------------------------------------------------------------------------
# B：resume 命令道与释放道共用同一份 entry 判读——清标志必须留下活执行器
# ---------------------------------------------------------------------------


def _resume_triage_service(
    entry,
    *,
    node_status: str = "in_progress",
):
    """装配一个只带 entry 判读所需依赖的 TaskActorService，并记录 resume 调用。"""
    service = object.__new__(TaskActorService)
    service._release_sweeps = {}
    service._dispatchers = {}
    service._store = SimpleNamespace(
        get_node=lambda node_id: SimpleNamespace(
            status=node_status, is_paused=False, pause_requested=False
        ),
    )
    service._node_operator_paused = lambda node: False
    calls: list[str] = []

    async def _resume(node_id: str) -> None:
        calls.append(node_id)

    entries = {} if entry is None else {entry.node_id: entry}
    dispatcher = SimpleNamespace(_entries=entries, resume_node=_resume)
    service._dispatchers["task:r"] = dispatcher
    return service, dispatcher, calls


async def _triage_entry(*, future_resolved: bool, task_finished: bool):
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    if future_resolved:
        future.set_result("stale")
    gate = asyncio.Event()

    async def _coroutine() -> None:
        if not task_finished:
            await gate.wait()

    task = asyncio.create_task(_coroutine())
    if task_finished:
        await task
    return SimpleNamespace(
        node_id="node:child",
        future=future,
        task=task,
        gate=gate,
        role="execution",
        interrupt_result=None,
        queued_counted=False,
        running_counted=False,
    )


@pytest.mark.asyncio
async def test_resume_command_lane_rebuilds_entry_whose_future_already_resolved() -> None:
    """事故形态：future 已解析 + 协程已停 + 节点非终态 → 弹残骸重建，绝不静默返回。"""
    entry = await _triage_entry(future_resolved=True, task_finished=True)
    service, dispatcher, calls = _resume_triage_service(entry)

    outcome = await service.resume_node_entry("task:r", "node:child")

    assert outcome == "resumed"
    assert dispatcher._entries == {}, "残骸 entry 必须弹出，否则新 future 无人 await"
    assert calls == ["node:child"]
    assert service._release_sweeps.get("task:r") is not None, "命令道也要进延迟校验兜底"
    # 清扫本身由 test_release_verification_sweep_re_resumes_wedged_entry 覆盖，这里只验装填。
    service._release_sweeps.pop("task:r").cancel()


@pytest.mark.asyncio
async def test_resume_command_lane_defers_without_double_running_a_live_coroutine() -> None:
    entry = await _triage_entry(future_resolved=True, task_finished=False)
    service, dispatcher, calls = _resume_triage_service(entry)
    try:
        outcome = await service.resume_node_entry("task:r", "node:child")

        assert outcome == "deferred"
        assert calls == [], "协程还在跑就不再 resume（防双跑）"
        assert dispatcher._entries.get("node:child") is entry
        assert service._release_sweeps.get("task:r") is not None
    finally:
        entry.gate.set()
        await entry.task


@pytest.mark.asyncio
async def test_resume_command_lane_relaunches_paused_child_with_pending_future() -> None:
    """常规形态：NodePausedError 让 future 保持 pending，resume 应直接在原 future 上重跑。"""
    entry = await _triage_entry(future_resolved=False, task_finished=True)
    service, dispatcher, calls = _resume_triage_service(entry)

    outcome = await service.resume_node_entry("task:r", "node:child")

    assert outcome == "resumed"
    assert calls == ["node:child"]
    assert dispatcher._entries.get("node:child") is entry, "pending future 不重建"
    assert not entry.future.done()
    service._release_sweeps.pop("task:r").cancel()


@pytest.mark.asyncio
async def test_release_lane_still_ignores_nodes_without_an_entry() -> None:
    """共用判读不得改动释放道语义：无 entry 的节点仍然不介入、不新建。"""
    service, dispatcher, calls = _resume_triage_service(None)
    armed: list[list[str]] = []

    def _arm(task_id: str, node_ids) -> None:
        armed.append(list(node_ids))

    service._schedule_release_verification = _arm

    await service._release_scoped_epoch_holds("task:r", ["node:child"])

    assert calls == []
    assert armed == []


@pytest.mark.asyncio
async def test_release_lane_and_command_lane_agree_on_entry_triage() -> None:
    """同一条 done-future 残骸，两条道必须走同一段判读。"""
    entry = await _triage_entry(future_resolved=True, task_finished=True)
    service, dispatcher, calls = _resume_triage_service(entry)
    service._schedule_release_verification = lambda task_id, node_ids: None

    await service._release_scoped_epoch_holds("task:r", ["node:child"])

    assert calls == ["node:child"]
    assert dispatcher._entries == {}


# ---------------------------------------------------------------------------
# C：run_task 入口的孤儿决断——收尸 / 重派发
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_spawn_child_reaped_at_run_task_entry(tmp_path: Path) -> None:
    service = _build_service(tmp_path, _DummyChatBackend())
    try:
        record = await service.create_task("reap orphan task", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=SpawnChildSpec(goal="orphan goal", prompt="orphan prompt", execution_policy={"mode": "focus"}),
            owner_round_id="round-dead",
            owner_entry_index=0,
        )
        # 轮已"完成"（被放弃/结算）：绑定关系不再构成重放路径
        _set_spawn_operations(
            service,
            root_node_id=root.node_id,
            payload={
                "round-dead": {
                    "specs": [],
                    "entries": [
                        {"index": 0, "goal": "orphan goal", "child_node_id": child.node_id, "status": "error"}
                    ],
                    "completed": True,
                }
            },
        )

        async def _fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
            assert node_id == record.root_node_id, "被收尸的孤儿不得再被派发执行"
            service.log_service.update_node_status(task_id, node_id, status="success", final_output="root done")
            return _success_result(node_id)

        service.node_runner.run_node = _fake_run_node
        await service.task_actor_service.run_task(record.task_id)

        child_after = service.store.get_node(child.node_id)
        assert child_after is not None
        assert str(child_after.status) == "failed", "孤儿必须被收尸，不得幽灵 in_progress"
        logs = service.store.list_task_error_logs(record.task_id)
        assert any("orphan reaped" in str(getattr(item, "error_text", "") or "") for item in logs), (
            "收尸必须留错误日志"
        )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_orphan_spawn_child_redispatched_when_parent_replay_intact(tmp_path: Path) -> None:
    service = _build_service(tmp_path, _DummyChatBackend())
    try:
        record = await service.create_task("redispatch orphan task", session_id="web:ceo-demo")
        task = service.get_task(record.task_id)
        root = service.store.get_node(record.root_node_id)
        assert task is not None and root is not None
        child = service.node_runner._create_execution_child(
            task=task,
            parent=root,
            spec=SpawnChildSpec(goal="live goal", prompt="live prompt", execution_policy={"mode": "focus"}),
            owner_round_id="round-live",
            owner_entry_index=0,
        )
        # 轮未完成且绑定该子节点；根帧保留 waiting_children 重放意图
        _set_spawn_operations(
            service,
            root_node_id=root.node_id,
            payload={
                "round-live": {
                    "specs": [],
                    "entries": [
                        {"index": 0, "goal": "live goal", "child_node_id": child.node_id, "status": "running"}
                    ],
                    "completed": False,
                }
            },
        )
        service.log_service.upsert_frame(
            record.task_id,
            {
                "node_id": root.node_id,
                "depth": root.depth,
                "node_kind": root.node_kind,
                "phase": "waiting_children",
                "messages": [{"role": "user", "content": "seed"}],
            },
        )

        child_done = asyncio.Event()

        async def _fake_run_node(task_id: str, node_id: str) -> NodeFinalResult:
            if node_id == child.node_id:
                service.log_service.update_node_status(task_id, node_id, status="success", final_output="child done")
                child_done.set()
                return _success_result(node_id)
            assert node_id == record.root_node_id
            await asyncio.wait_for(child_done.wait(), timeout=5)
            service.log_service.update_node_status(task_id, node_id, status="success", final_output="root done")
            return _success_result(node_id)

        service.node_runner.run_node = _fake_run_node
        await service.task_actor_service.run_task(record.task_id)

        assert child_done.is_set(), "可重放的孤儿子节点必须被重派发而不是收尸"
        child_after = service.store.get_node(child.node_id)
        assert child_after is not None
        assert str(child_after.status) == "success"
        logs = service.store.list_task_error_logs(record.task_id)
        assert not any("orphan reaped" in str(getattr(item, "error_text", "") or "") for item in logs)
    finally:
        await service.close()


# ---------------------------------------------------------------------------
# D：轮次投影计数以绑定节点真实状态优先
# ---------------------------------------------------------------------------


def test_projection_entry_effective_status_prefers_bound_node() -> None:
    nodes = {
        "alive": SimpleNamespace(status="in_progress"),
        "dead": SimpleNamespace(status="failed"),
        "won": SimpleNamespace(status="success"),
    }
    svc = object.__new__(TaskLogService)
    svc._store = SimpleNamespace(get_node=lambda node_id: nodes.get(node_id))
    effective = TaskLogService._projection_entry_effective_status
    # 事故形态：entry 被误盖 error 而节点仍活着 → 不算 failed
    assert effective(svc, {"status": "error", "child_node_id": "alive"}) == "running"
    # 反向漂移：entry 还挂 running 而节点已终态 → 以节点为准
    assert effective(svc, {"status": "running", "child_node_id": "dead"}) == "failed"
    assert effective(svc, {"status": "running", "child_node_id": "won"}) == "success"
    # 正常形态保持原义
    assert effective(svc, {"status": "queued", "child_node_id": "alive"}) == "queued"
    assert effective(svc, {"status": "success", "child_node_id": "won"}) == "success"
    # 绑定不可解析：回退 entry 记账
    assert effective(svc, {"status": "error", "child_node_id": "gone"}) == "error"
    assert effective(svc, {"status": "error"}) == "error"


# ---------------------------------------------------------------------------
# 要点 5：未物化 spawn 批次不得被 hold 在物化前中止
# （2026-09-18 task:d596a609bbb3：epoch 永久卡 barrier_draining 且静默）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inflight_spawn_review_defers_hold_until_materialized(
    tmp_path: Path,
    _fast_watchdog,
) -> None:
    """hold 在 spawn review 在飞时到达：批次先物化，再在安全相位冻结。

    历史缺陷：看门狗 poll 在 review 内命中 hold → 取消在飞协程（CancelledError
    不被 _review_spawn_batch 的 except Exception 吞掉）→ entries 停在 queued、
    轮没有子节点；而屏障 drain 正等这些子节点物化，物化又只能由这个已被取消的
    协程产出 → 互等死锁。本用例锁定：review 在飞期间的 poll 必须延后，
    放行后批次照常物化，随后才冻结（轮仍可同 id 重放）。
    """
    review_gate = asyncio.Event()
    review_entered = asyncio.Event()

    async def _gated_review(kwargs):
        review_entered.set()
        await asyncio.wait_for(review_gate.wait(), timeout=15)
        return _review_allow_response("call:review-1")

    backend = _ScriptedBackend(
        [
            # 1) 根首轮：派生一个子节点
            _spawn_response("call:spawn-1", goal="child goal", prompt="child prompt"),
            # 2) spawn 治理审查：被 gate 挡住，制造「review 在飞」窗口
            _gated_review,
            # 3) 根的分发控制回合：逐子 skip，通知留在本地
            _decision_skip_children,
            # 4) 释放后子节点复活轮
            _final_response("call:child-final-1", summary="child work done (resumed)"),
            # 5) 根收尾轮（同 id 重放拿到子结果之后）
            _final_response("call:root-final", summary="root done"),
        ]
    )
    service = _build_service(tmp_path, backend)
    try:
        record = await service.create_task("review window notice", session_id="web:ceo-demo")
        task_id = record.task_id
        runner = asyncio.create_task(service.task_actor_service.run_task(task_id))
        await asyncio.wait_for(review_entered.wait(), timeout=15)

        # review 在飞时播种通知 → 屏障生效（快照期 root 在 blocked 内）
        task = service.get_task(task_id)
        assert task is not None
        await service.task_append_notice(
            task_ids=[task_id],
            node_ids=[],
            message="补充要求：统一渲染目录与输出路径",
            session_id=task.session_id,
        )

        # 跨过多个 poll 周期（_fast_watchdog=0.2s）：批次必须仍在飞、未被中止
        await asyncio.sleep(0.7)
        assert not _entry_frozen(service, task_id, record.root_node_id), "物化前不得被 hold 冻结"
        root_mid = service.store.get_node(record.root_node_id)
        ops_mid = dict((root_mid.metadata or {}).get("spawn_operations") or {})
        round_mid = dict(ops_mid.get("call:spawn-1") or {})
        assert not round_mid.get("completed"), "review 未放行前轮不得被标记完成"

        def _execution_children() -> list:
            return [
                item
                for item in service.store.list_children(record.root_node_id)
                if str(getattr(item, "node_kind", "")).strip().lower() == "execution"
            ]

        # 放行 review：批次照常物化出子节点（本修复的核心保证）
        review_gate.set()
        await _wait_until(lambda: bool(_execution_children()), timeout=15, message="child materialized under hold")
        child_node = _execution_children()[0]

        # 物化后：在安全相位冻结；轮未完成、entry 未被盖 error、绑定仍在
        await _wait_until(
            lambda: _entry_frozen(service, task_id, record.root_node_id),
            timeout=15,
            message="root frozen after materialization",
        )
        root_after = service.store.get_node(record.root_node_id)
        ops_after = dict((root_after.metadata or {}).get("spawn_operations") or {})
        round_after = dict(ops_after.get("call:spawn-1") or {})
        entries_after = [dict(item) for item in list(round_after.get("entries") or []) if isinstance(item, dict)]
        assert not round_after.get("completed")
        assert entries_after and entries_after[0].get("child_node_id") == child_node.node_id
        assert entries_after[0].get("status") != "error", "hold 取消不得盖 error 记账"

        # 死锁被解开的地方：drain 的待物化条件已可满足
        assert (
            service.task_actor_service._barrier_materialize_pending_entries(
                task_id=task_id,
                barrier_node_ids=[record.root_node_id],
            )
            == []
        )

        # 驱动 epoch 到完成 → 释放 → 根同 id 重放、重挂原子节点、收尾
        outcome = "idle"
        for _ in range(16):
            outcome = await service.task_actor_service._run_distribution_epoch(task_id)
            if outcome in {"completed", "failed", "idle"}:
                break
            await asyncio.sleep(0.05)
        assert outcome == "completed", f"epoch 不得卡在 barrier_draining，实际 {outcome}"
        await asyncio.wait_for(runner, timeout=30)

        root_final = service.store.get_node(record.root_node_id)
        child_final = service.store.get_node(child_node.node_id)
        assert str(root_final.status) == "success"
        assert str(child_final.status) == "success"
        assert len(_execution_children()) == 1, "不得重复派生子节点"
        assert service.store.list_task_error_logs(task_id) == []
    finally:
        review_gate.set()
        await service.close()


def _stall_spawn_round(
    service: MainRuntimeService,
    *,
    root,
    round_id: str,
    goal: str = "late child",
) -> None:
    spec = SpawnChildSpec(goal=goal, prompt=f"{goal} prompt", execution_policy={"mode": "focus"})
    _set_spawn_operations(
        service,
        root_node_id=root.node_id,
        payload={
            round_id: {
                "specs": [spec.model_dump(mode="json")],
                "entries": [service.node_runner._normalize_spawn_entry(index=0, spec=spec, entry={})],
                "completed": False,
            }
        },
    )


@pytest.mark.asyncio
async def test_barrier_drain_kicks_stalled_spawn_round_parent(tmp_path: Path) -> None:
    """drain 自愈：持有未物化轮、已被停摆的父节点会被踢起来完成物化。

    不踢的话 drain 等的是「只有释放屏障才能产生」的结果（互等死锁）。
    踢一次即记账（drain_kick_rounds），避免每秒重复 resume 同一个轮。
    """
    service = _build_service(tmp_path, _DummyChatBackend())
    try:
        record = await service.create_task("stalled spawn round", session_id="web:ceo-demo")
        task_id = record.task_id
        root = service.get_node(record.root_node_id)
        assert root is not None
        _stall_spawn_round(service, root=root, round_id="round-stalled")
        # 停摆形态：帧保留重放入口，但没有存活 entry
        service.log_service.update_frame(
            task_id,
            root.node_id,
            lambda frame: {**frame, "phase": "waiting_children"},
        )
        dispatcher = service.task_actor_service._create_dispatcher(task_id)
        service.task_actor_service._dispatchers[task_id] = dispatcher
        kicks: list[str] = []

        async def _record_resume(node_id: str) -> None:
            kicks.append(str(node_id))

        dispatcher.resume_node = _record_resume  # type: ignore[assignment]

        await service.task_append_notice(
            task_ids=[task_id],
            node_ids=[],
            message="收紧渲染目录约定",
            session_id=record.session_id,
        )
        outcome = await service.task_actor_service._run_distribution_epoch(task_id)
        assert outcome == "draining"
        epoch = service.store.list_active_task_message_distribution_epochs(task_id)[0]
        assert epoch.state == "barrier_draining"
        assert root.node_id in list(epoch.payload.get("drain_pending_node_ids") or [])
        assert kicks == [root.node_id], "停摆父节点必须被踢起"
        ledger = dict(epoch.payload.get("drain_kick_rounds") or {})
        assert list(ledger) == [f"{root.node_id}::round-stalled"], "踢起必须记账（键=父节点+轮）"
        assert str(ledger[f"{root.node_id}::round-stalled"]).strip(), "记账必须带上次踢起时刻（冷却重试用）"

        # 冷却期内不重复踢
        await service.task_actor_service._run_distribution_epoch(task_id)
        assert kicks == [root.node_id]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_barrier_drain_kick_requires_replay_intent(tmp_path: Path) -> None:
    """无重放入口（帧里没有该轮）时不踢：重放不成立，踢了只会让模型发新轮。"""
    service = _build_service(tmp_path, _DummyChatBackend())
    try:
        record = await service.create_task("no replay intent", session_id="web:ceo-demo")
        task_id = record.task_id
        root = service.get_node(record.root_node_id)
        assert root is not None
        _stall_spawn_round(service, root=root, round_id="round-noframe")
        dispatcher = service.task_actor_service._create_dispatcher(task_id)
        service.task_actor_service._dispatchers[task_id] = dispatcher
        kicks: list[str] = []

        async def _record_resume(node_id: str) -> None:
            kicks.append(str(node_id))

        dispatcher.resume_node = _record_resume  # type: ignore[assignment]

        await service.task_append_notice(
            task_ids=[task_id],
            node_ids=[],
            message="收紧渲染目录约定",
            session_id=record.session_id,
        )
        outcome = await service.task_actor_service._run_distribution_epoch(task_id)
        assert outcome == "draining"
        assert kicks == []
        epoch = service.store.list_active_task_message_distribution_epochs(task_id)[0]
        assert dict(epoch.payload.get("drain_kick_rounds") or {}) == {}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_barrier_drain_kick_retries_when_previous_kick_made_no_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """踢了但没进展的轮必须能再踢：一次失败不能永久卡住 drain。

    冷却窗口（`_DRAIN_KICK_RETRY_SECONDS`）只限制频率；早期列表形态的记账（无时刻）
    按冷却已过处理，无需迁移即可重试。
    """
    monkeypatch.setattr(task_actor_module, "_DRAIN_KICK_RETRY_SECONDS", 0.0)
    service = _build_service(tmp_path, _DummyChatBackend())
    try:
        record = await service.create_task("kick retry", session_id="web:ceo-demo")
        task_id = record.task_id
        root = service.get_node(record.root_node_id)
        assert root is not None
        _stall_spawn_round(service, root=root, round_id="round-retry")
        service.log_service.update_frame(
            task_id,
            root.node_id,
            lambda frame: {**frame, "phase": "waiting_children"},
        )
        dispatcher = service.task_actor_service._create_dispatcher(task_id)
        service.task_actor_service._dispatchers[task_id] = dispatcher
        kicks: list[str] = []

        async def _record_resume(node_id: str) -> None:
            kicks.append(str(node_id))

        dispatcher.resume_node = _record_resume  # type: ignore[assignment]

        await service.task_append_notice(
            task_ids=[task_id],
            node_ids=[],
            message="收紧渲染目录约定",
            session_id=record.session_id,
        )
        # 早期列表形态：预置一次「已踢过」的记账（无时刻）→ 冷却视为已过
        epoch = service.store.list_active_task_message_distribution_epochs(task_id)[0]
        service.store.upsert_task_message_distribution_epoch(
            epoch.model_copy(
                update={
                    "payload": {
                        **dict(epoch.payload or {}),
                        "drain_kick_rounds": [f"{root.node_id}::round-retry"],
                    }
                }
            )
        )

        outcome = await service.task_actor_service._run_distribution_epoch(task_id)
        assert outcome == "draining"
        assert kicks == [root.node_id], "无进展的轮在冷却过后必须被重新踢起"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_kicked_stalled_parent_materializes_despite_entry_hold_check(
    tmp_path: Path,
    _fast_watchdog,
) -> None:
    """自愈踢起已停摆父节点后，run_node 入口的 hold 检查也必须让位于未物化豁免。

    这条路径与「通知在节点运行中途到达」不同：节点已停摆、重新进入 run_node，
    入口检查先于 react_loop 的安全检查点执行。若入口不放行，节点会在被踢起后
    立刻再次冻结（实测 42ms），drain 依旧等不到物化。
    """
    backend = _ScriptedBackend(
        [
            # 重放 spawn 轮时唯一需要的一轮模型调用：spawn 治理审查放行
            _review_allow_response("call:review-1"),
        ]
    )
    service = _build_service(tmp_path, backend)
    try:
        record = await service.create_task("kicked stalled parent", session_id="web:ceo-demo")
        task_id = record.task_id
        root = service.get_node(record.root_node_id)
        assert root is not None
        _stall_spawn_round(service, root=root, round_id="round-stalled", goal="late child")
        # 停摆形态：帧保留重放入口（waiting_children → 同 id 重放）
        service.log_service.update_frame(
            task_id,
            root.node_id,
            lambda frame: {**frame, "phase": "waiting_children"},
        )
        dispatcher = service.task_actor_service._create_dispatcher(task_id)
        service.task_actor_service._dispatchers[task_id] = dispatcher

        await service.task_append_notice(
            task_ids=[task_id],
            node_ids=[],
            message="统一渲染目录与输出路径",
            session_id=record.session_id,
        )

        # 真实驱动（不 stub resume）：drain 自愈踢起父节点 → 入口放行 → 同 id 重放 → 物化
        outcome = await service.task_actor_service._run_distribution_epoch(task_id)
        assert outcome == "draining"

        def _round_entries() -> list:
            current = service.store.get_node(root.node_id)
            ops = dict((current.metadata or {}).get("spawn_operations") or {})
            return [dict(item) for item in list((ops.get("round-stalled") or {}).get("entries") or [])]

        await _wait_until(
            lambda: bool(_round_entries()) and bool(_round_entries()[0].get("child_node_id")),
            timeout=15,
            message="kicked parent materialized its spawn round",
        )
        entries = _round_entries()
        child_id = str(entries[0].get("child_node_id") or "").strip()
        child = service.store.get_node(child_id)
        assert child is not None, "重放必须物化出子节点"
        assert str(child.metadata.get("spawn_owner_round_id") or "") == "round-stalled"

        # drain 的待物化条件已可满足（死锁解开）
        assert (
            service.task_actor_service._barrier_materialize_pending_entries(
                task_id=task_id,
                barrier_node_ids=[root.node_id],
            )
            == []
        )
        # 且豁免不越界：物化完成后节点仍会在安全相位被冻结
        await _wait_until(
            lambda: _entry_frozen(service, task_id, root.node_id),
            timeout=15,
            message="parent frozen after materialization",
        )
    finally:
        await service.close()
