"""节点道 skill 名单的**每跳** live 检测合同测试。

验收口径（`work/skill-roster-context-plan.md` / 交接文档「待实现项 1」）：

- supplier 存在时每跳现算：mid-turn 新变为可读的 skill 下一跳就进注入块；
- 头部名单在两次重印之间逐字节不动（新名字不进头部，只在注入块点名）；
- supplier 抛错时回落回合初 fresh payload，不炸回合；
- supplier 由 NodeRunner 递进 `runtime_context`，react_loop 自己不需要 service 引用。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor.tool_contract import clear_pinned_contract_stores
from g3ku.runtime.skill_roster_notice import clear_skill_roster_notice_stores
from main.runtime.node_runner import NodeRunner
from main.runtime.react_loop import ReActToolLoop
from main.service.runtime_service import MainRuntimeService


class _FakeLogService:
    def __init__(self) -> None:
        self._frames: dict[tuple[str, str], dict] = {}

    def read_runtime_frame(self, task_id: str, node_id: str):
        return self._frames.get((task_id, node_id))

    def upsert_frame(self, task_id: str, frame: dict, publish_snapshot: bool = False) -> None:
        self._frames[(task_id, str(frame.get("node_id") or ""))] = dict(frame)


def _item(skill_id: str, description: str = "") -> dict[str, str]:
    return {
        "skill_id": skill_id,
        "display_name": skill_id.title(),
        "description": description or f"{skill_id} 的描述。",
    }


@pytest.fixture(autouse=True)
def _isolate_process_stores():
    clear_skill_roster_notice_stores()
    clear_pinned_contract_stores()
    yield
    clear_skill_roster_notice_stores()
    clear_pinned_contract_stores()


def _loop() -> ReActToolLoop:
    return ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)


def _node(task_id: str, node_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        task_id=task_id,
        node_id=node_id,
        depth=0,
        node_kind="execution",
        can_spawn_children=False,
    )


def _tool_schema_selection() -> dict:
    return {
        "tool_names": ["submit_next_stage"],
        "candidate_tool_names": [],
        "lightweight_tool_ids": [],
        "trace": {},
    }


def test_supplier_feeds_each_hop_while_head_stays_frozen() -> None:
    """第二跳 supplier 多出 beta：提示落进历史一次，头部字节不动。"""
    loop = _loop()
    task_id, node_id = "task-live-roster", "node-live-roster"
    scope = f"node:{task_id}:{node_id}"
    node = SimpleNamespace(
        task_id=task_id, node_id=node_id, depth=0, node_kind="execution",
        can_spawn_children=False, metadata={},
    )
    hops = [[_item("alpha")], [_item("alpha"), _item("beta")]]
    sampled: list[int] = []

    def supplier() -> list[dict[str, str]]:
        index = min(len(sampled), len(hops) - 1)
        sampled.append(index)
        return list(hops[index])

    runtime_context = {"live_skill_roster_supplier": supplier}
    base_messages = [{"role": "system", "content": "node system"}, {"role": "user", "content": '{"prompt":"demo"}'}]

    # 第一跳：头部钉住 alpha，没有新名字 ⇒ 不投提示。
    items_first = loop._node_skill_roster_items(
        runtime_context=runtime_context, task_id=task_id, node_id=node_id
    )
    head_first = loop._apply_node_skill_roster_head(
        list(base_messages), scope_key=scope, skill_items=items_first
    )
    history_first = loop._apply_node_skill_roster_notice(
        list(base_messages), node=node, skill_roster_items=items_first
    )
    assert [item["skill_id"] for item in items_first] == ["alpha"]
    assert "`alpha`" in head_first[0]["content"]
    assert not any(
        str(item.get("content") or "").startswith("[SKILL_ROSTER_NOTICE_V1]") for item in history_first
    )

    # 第二跳：supplier 返回 [alpha, beta]；头部 epoch 未变 → 复用已钉原文。
    items_second = loop._node_skill_roster_items(
        runtime_context=runtime_context, task_id=task_id, node_id=node_id
    )
    head_second = loop._apply_node_skill_roster_head(
        list(base_messages), scope_key=scope, skill_items=items_second
    )
    history_second = loop._apply_node_skill_roster_notice(
        list(base_messages), node=node, skill_roster_items=items_second
    )

    assert [item["skill_id"] for item in items_second] == ["alpha", "beta"]
    # 头部：逐字节不动，且不含 beta
    assert head_second[0]["content"] == head_first[0]["content"]
    assert "`beta`" not in head_second[0]["content"]
    # 提示：恰好一条，只点名新变为可读的 beta（id + 名称 + 描述）
    injected = [
        str(item.get("content") or "")
        for item in history_second
        if str(item.get("content") or "").startswith("[SKILL_ROSTER_NOTICE_V1]")
    ]
    assert len(injected) == 1
    assert "- `beta` | Beta | beta 的描述。" in injected[0]
    # 第三跳：同一个 beta 不再投第二份
    history_third = loop._apply_node_skill_roster_notice(
        list(history_second), node=node, skill_roster_items=items_second
    )
    assert len([
        item for item in history_third
        if str(item.get("content") or "").startswith("[SKILL_ROSTER_NOTICE_V1]")
    ]) == 1
    # 每跳各问一次 supplier（不是回合初快照复用）
    assert len(sampled) == 2


def test_supplier_failure_falls_back_to_fresh_payload() -> None:
    """supplier 抛错 → 回落回合初 fresh payload，不炸回合。"""
    loop = _loop()

    def supplier() -> list[dict[str, str]]:
        raise RuntimeError("registry unavailable")

    runtime_context = {
        "live_skill_roster_supplier": supplier,
        "fresh_node_dynamic_contract_payload": {
            "candidate_skills": [{"skill_id": "fallback", "description": "fallback 的描述。"}],
        },
    }
    items = loop._node_skill_roster_items(
        runtime_context=runtime_context, task_id="task-fallback", node_id="node-fallback"
    )
    assert [item["skill_id"] for item in items] == ["fallback"]


def test_supplier_returning_empty_list_is_authoritative() -> None:
    """supplier 说"现在一个都不可读"就是权威结论，不回落陈旧快照。"""
    loop = _loop()
    runtime_context = {
        "live_skill_roster_supplier": lambda: [],
        "fresh_node_dynamic_contract_payload": {
            "candidate_skills": [{"skill_id": "stale", "description": "stale 的描述。"}],
        },
    }
    assert (
        loop._node_skill_roster_items(
            runtime_context=runtime_context, task_id="task-empty", node_id="node-empty"
        )
        == []
    )


def test_node_runner_exposes_supplier_in_runtime_context() -> None:
    """接线：NodeRunner 把注入的 supplier 以无参闭包递进 runtime_context。"""
    runner = NodeRunner(
        store=SimpleNamespace(),
        log_service=_FakeLogService(),
        react_loop=SimpleNamespace(_adaptive_tool_budget_controller=None),
        tool_provider=None,
        execution_model_refs=["execution"],
        acceptance_model_refs=["inspection"],
    )
    seen: list[tuple[str, str]] = []

    def _supplier(*, task, node) -> list[dict[str, str]]:
        seen.append((task.task_id, node.node_id))
        return [_item("alpha")]

    runner._skill_roster_supplier = _supplier
    task = SimpleNamespace(task_id="task-wire", session_id="web:shared", metadata={})
    node = _node("task-wire", "node-wire")

    runtime_context = runner._runtime_context(task=task, node=node)
    supplier = runtime_context.get("live_skill_roster_supplier")
    assert callable(supplier)
    assert [item["skill_id"] for item in supplier()] == ["alpha"]
    assert seen == [("task-wire", "node-wire")]

    # 没接线时（老调用方/测试桩）不递空壳：键存在但值为 None，调用侧回落快照。
    runner._skill_roster_supplier = None
    assert runner._runtime_context(task=task, node=node)["live_skill_roster_supplier"] is None


def test_runtime_service_supplier_reads_governed_visible_set() -> None:
    """supplier 本体：直接取治理实时可见集，归一成 roster item（描述保留原文）。"""
    records = [
        SimpleNamespace(skill_id="alpha", display_name="Alpha", description="第一句。第二句。"),
        SimpleNamespace(skill_id="beta", display_name="", description=""),
        SimpleNamespace(skill_id="", display_name="broken", description="no id"),
    ]
    stub = SimpleNamespace(
        list_contract_visible_skill_resources=lambda *, actor_role, session_id: records,
        _actor_role_for_node=lambda node: MainRuntimeService._actor_role_for_node(None, node),
    )
    task = SimpleNamespace(session_id="web:shared")
    node = _node("task-supplier", "node-supplier")

    items = MainRuntimeService._node_skill_roster_supplier(stub, task=task, node=node)

    assert items == [
        {"skill_id": "alpha", "display_name": "Alpha", "description": "第一句。第二句。"},
        {"skill_id": "beta", "display_name": "beta", "description": ""},
    ]


def test_pinned_head_rides_the_frame_trace_and_is_read_back_after_the_store_empties() -> None:
    """钉头原文与 roster epoch 装进已整份持久化的 selection trace；进程载体空了（重启或 LRU
    顶掉）从帧读回，而不是按当轮可见集重渲——头部是首条消息，重渲一行就顶掉身后全部前缀。
    """
    loop = _loop()
    task_id, node_id = "task-head-durable", "node-head-durable"
    scope = f"node:{task_id}:{node_id}"
    base_messages = [{"role": "system", "content": "node system"}, {"role": "user", "content": "demo"}]

    head_first = loop._apply_node_skill_roster_head(
        list(base_messages), scope_key=scope, skill_items=[_item("alpha"), _item("beta")]
    )
    carrier = loop._node_pinned_head_carrier(scope)
    assert carrier["skill_ids"] == ["alpha", "beta"]
    assert carrier["text"] and carrier["revision"]
    assert carrier["epoch"] == 0

    loop._log_service.upsert_frame(
        task_id,
        {"node_id": node_id, "model_visible_tool_selection_trace": {"pinned_head": carrier}},
    )

    # 进程重启 / LRU 顶掉：两份进程载体都空了。
    clear_pinned_contract_stores()
    assert loop._node_pinned_head_carrier(scope) == {}

    loop._restore_node_pinned_head_from_frame(task_id=task_id, node_id=node_id, scope_key=scope)
    assert loop._node_pinned_head_carrier(scope) == carrier

    # 停机期间删掉 beta：可见集只剩 alpha，头部仍逐字节复用钉住的那份。
    head_after = loop._apply_node_skill_roster_head(
        list(base_messages), scope_key=scope, skill_items=[_item("alpha")]
    )
    assert head_after[0]["content"] == head_first[0]["content"]
    assert "`beta`" in head_after[0]["content"]


def test_restore_node_pinned_head_never_overwrites_a_live_pin() -> None:
    """帧是兜底源：进程里已经钉着（本回合刚跨过重印边界）时不得被旧原文拉回去。"""
    loop = _loop()
    task_id, node_id = "task-head-live", "node-head-live"
    scope = f"node:{task_id}:{node_id}"
    base_messages = [{"role": "system", "content": "node system"}, {"role": "user", "content": "demo"}]

    head_live = loop._apply_node_skill_roster_head(
        list(base_messages), scope_key=scope, skill_items=[_item("alpha")]
    )
    stale = {
        "text": "## Runtime Contract (pinned)\nkind: frontdoor_runtime_pinned_contract\n- `stale` | Stale | 旧",
        "revision": "pc:stale",
        "skill_ids": ["stale"],
        "epoch": 7,
    }
    loop._log_service.upsert_frame(
        task_id, {"node_id": node_id, "model_visible_tool_selection_trace": {"pinned_head": stale}}
    )

    loop._restore_node_pinned_head_from_frame(task_id=task_id, node_id=node_id, scope_key=scope)

    assert loop._node_pinned_head_carrier(scope)["skill_ids"] == ["alpha"]
    head_after = loop._apply_node_skill_roster_head(
        list(base_messages), scope_key=scope, skill_items=[_item("alpha")]
    )
    assert head_after[0]["content"] == head_live[0]["content"]
    assert "stale" not in head_after[0]["content"]
