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

from g3ku.runtime.frontdoor.skill_roster import clear_skill_roster_ledgers
from g3ku.runtime.frontdoor.tool_contract import clear_pinned_contract_stores
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
    clear_skill_roster_ledgers()
    clear_pinned_contract_stores()
    yield
    clear_skill_roster_ledgers()
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
        "hydrated_executor_names": [],
        "lightweight_tool_ids": [],
        "trace": {},
    }


def test_supplier_feeds_each_hop_while_head_stays_frozen() -> None:
    """第二跳 supplier 多出 beta：注入块点名 beta，头部字节不动。"""
    loop = _loop()
    task_id, node_id = "task-live-roster", "node-live-roster"
    scope = f"node:{task_id}:{node_id}"
    node = _node(task_id, node_id)
    hops = [[_item("alpha")], [_item("alpha"), _item("beta")]]
    sampled: list[int] = []

    def supplier() -> list[dict[str, str]]:
        index = min(len(sampled), len(hops) - 1)
        sampled.append(index)
        return list(hops[index])

    runtime_context = {"live_skill_roster_supplier": supplier}
    base_messages = [{"role": "system", "content": "node system"}, {"role": "user", "content": '{"prompt":"demo"}'}]

    # 第一跳：头部钉住 alpha，注入块空（alpha 已被头部覆盖）。
    items_first = loop._node_skill_roster_items(
        runtime_context=runtime_context, task_id=task_id, node_id=node_id
    )
    head_first = loop._apply_node_skill_roster_head(
        list(base_messages), scope_key=scope, skill_items=items_first
    )
    contract_first = loop._build_node_dynamic_contract(
        node=node,
        message_history=list(base_messages),
        tool_schema_selection=_tool_schema_selection(),
        stage_gate={},
        runtime_context=runtime_context,
        skill_roster_items=items_first,
    )
    assert [item["skill_id"] for item in items_first] == ["alpha"]
    assert "`alpha`" in head_first[0]["content"]
    assert contract_first.new_readable_skill_items == []

    # 第二跳：supplier 返回 [alpha, beta]；头部 epoch 未变 → 复用已钉原文。
    items_second = loop._node_skill_roster_items(
        runtime_context=runtime_context, task_id=task_id, node_id=node_id
    )
    head_second = loop._apply_node_skill_roster_head(
        list(base_messages), scope_key=scope, skill_items=items_second
    )
    contract_second = loop._build_node_dynamic_contract(
        node=node,
        message_history=list(base_messages),
        tool_schema_selection=_tool_schema_selection(),
        stage_gate={},
        runtime_context=runtime_context,
        skill_roster_items=items_second,
    )

    assert [item["skill_id"] for item in items_second] == ["alpha", "beta"]
    # 头部：逐字节不动，且不含 beta
    assert head_second[0]["content"] == head_first[0]["content"]
    assert "`beta`" not in head_second[0]["content"]
    # 注入块：只点名新变为可读的 beta（id + 名称 + 描述）
    assert [item["skill_id"] for item in contract_second.new_readable_skill_items] == ["beta"]
    rendered = contract_second.to_message()["content"]
    assert "new_readable_skills (" in rendered
    assert "- `beta` | Beta | beta 的描述。" in rendered
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
