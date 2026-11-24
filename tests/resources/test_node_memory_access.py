"""节点接入长期记忆：种类门、头部冻结、取数失败降级、角色迁移守卫、复核隔离。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor.tool_contract import (
    clear_pinned_contract_stores,
    restore_pinned_contract_state,
)
from main.governance.models import ToolRecord
from main.governance.resource_registry import (
    MEMORY_TOOL_NODE_ROLE_WIDENING_META_KEY,
    MainRuntimeResourceRegistry,
)
from main.runtime.continuous_node import CONTINUOUS_METADATA_KEY
from main.runtime.react_loop import NODE_MEMORY_HEAD_HEADING, ReActToolLoop
from main.service.runtime_service import MainRuntimeService


@pytest.fixture(autouse=True)
def _isolate_pinned_contract_stores():
    clear_pinned_contract_stores()
    yield
    clear_pinned_contract_stores()


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend must not run in these tests: {kwargs!r}")


async def _noop_enqueue_task(_task_id: str) -> None:
    return None


def _service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task
    return service


def _node(*, continuous: bool = True, parent: str = "", kind: str = "execution") -> SimpleNamespace:
    metadata = {CONTINUOUS_METADATA_KEY: True} if continuous else {}
    return SimpleNamespace(metadata=metadata, parent_node_id=parent, node_kind=kind)


def _tool(tool_id: str, roles: list[str]) -> ToolRecord:
    return ToolRecord(
        tool_id=tool_id,
        display_name=tool_id,
        description=tool_id,
        source_path=f"tools/{tool_id}",
        allowed_roles=roles,
    )


def test_memory_tools_only_visible_to_continuous_root(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service._node_memory_tools_allowed(_node()) is True
    assert service._node_memory_tools_allowed(_node(continuous=False)) is False
    assert service._node_memory_tools_allowed(_node(parent="node:parent")) is False
    assert service._node_memory_tools_allowed(_node(kind="acceptance")) is False


def test_visible_tool_names_drop_memory_tools_for_other_nodes(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        service,
        "list_effective_tool_names",
        lambda **kwargs: ["exec", "memory_update", "memory_note", "submit_final_result"],
    )
    root = service._node_visible_tool_names(node=_node(), actor_role="execution", session_key="web:x")
    child = service._node_visible_tool_names(
        node=_node(continuous=False), actor_role="execution", session_key="web:x"
    )
    assert "memory_update" in root and "memory_note" in root
    assert "memory_update" not in child and "memory_note" not in child
    assert "exec" in child


def test_snapshot_supplier_returns_empty_for_other_nodes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = SimpleNamespace(session_id="web:x")
    assert service._node_memory_snapshot_supplier(task=task, node=_node(continuous=False)) == ""
    assert service._node_memory_snapshot_supplier(task=task, node=_node(parent="node:p")) == ""


def test_snapshot_supplier_reads_scope_view_and_reports_failure(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    task = SimpleNamespace(session_id="web:x")
    view = SimpleNamespace(snapshot_text=lambda: "  260101:一条记忆  ")
    router = SimpleNamespace(view_for_session=lambda key: view)
    monkeypatch.setattr(
        "g3ku.runtime.memory_scope_router.active_memory_scope_router", lambda: router
    )
    assert service._node_memory_snapshot_supplier(task=task, node=_node()) == "260101:一条记忆"

    def _explode(key):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(
        "g3ku.runtime.memory_scope_router.active_memory_scope_router",
        lambda: SimpleNamespace(view_for_session=_explode),
    )
    # None 专指取数失败：调用侧据此保留上一份冻结值，而不是把头部整块抹掉。
    assert service._node_memory_snapshot_supplier(task=task, node=_node()) is None


def _loop() -> ReActToolLoop:
    return object.__new__(ReActToolLoop)


def test_memory_head_freezes_between_reprint_epochs() -> None:
    loop = _loop()
    calls: list[int] = []

    def supplier():
        calls.append(len(calls))
        return f"260101:第 {len(calls)} 版"

    first = loop._node_memory_head_text(scope_key="node:t:n", supplier=supplier, epoch=1)
    again = loop._node_memory_head_text(scope_key="node:t:n", supplier=supplier, epoch=1)
    assert first == again == "260101:第 1 版"
    assert len(calls) == 1

    bumped = loop._node_memory_head_text(scope_key="node:t:n", supplier=supplier, epoch=2)
    assert bumped == "260101:第 2 版"
    assert len(calls) == 2


def test_memory_head_keeps_last_frozen_value_when_read_fails() -> None:
    loop = _loop()
    state = {"fail": False}

    def supplier():
        if state["fail"]:
            return None
        return "260101:冻结正文"

    assert loop._node_memory_head_text(scope_key="node:t:n", supplier=supplier, epoch=1) == "260101:冻结正文"
    state["fail"] = True
    assert loop._node_memory_head_text(scope_key="node:t:n", supplier=supplier, epoch=2) == "260101:冻结正文"


def test_memory_head_restores_from_carrier_without_refetch() -> None:
    loop = _loop()
    restore_pinned_contract_state(
        None,
        session_key="node:t:n",
        text=f"## Runtime Contract (pinned)\n名单\n\n{NODE_MEMORY_HEAD_HEADING}\n260101:恢复正文",
        revision=1,
        skill_ids=[],
        epoch=7,
    )
    calls: list[int] = []

    def supplier():
        calls.append(1)
        return "260101:新正文"

    text = loop._node_memory_head_text(scope_key="node:t:n", supplier=supplier, epoch=7)
    assert text == "260101:恢复正文"
    assert calls == []


def test_memory_head_absent_when_node_has_no_memory() -> None:
    loop = _loop()
    assert loop._node_memory_head_text(scope_key="node:t:n", supplier=lambda: "", epoch=1) == ""
    assert loop._node_memory_head_text(scope_key="node:t:n", supplier=None, epoch=1) == ""


class _FakeStore:
    def __init__(self, *, touched: set[str] | None = None) -> None:
        self.meta: dict[str, bool] = {}
        self.touched = set(touched or ())
        self.actions: list[dict[str, object]] = []

    def get_bool_meta(self, key: str, *, default: bool = False) -> bool:
        return self.meta.get(key, default)

    def set_bool_meta(self, key: str, value: bool) -> None:
        self.meta[key] = bool(value)

    def has_tool_policy_operator_action(self, tool_id: str) -> bool:
        return tool_id in self.touched

    def append_operator_action(self, **kwargs) -> dict[str, object]:
        self.actions.append(dict(kwargs))
        return dict(kwargs)


def _registry(store: _FakeStore) -> MainRuntimeResourceRegistry:
    return MainRuntimeResourceRegistry(workspace_root=Path("."), store=store, resource_manager=None)


def test_role_widening_adopts_discovered_roles_for_legacy_ceo_only() -> None:
    store = _FakeStore()
    registry = _registry(store)
    merged = registry._merge_tools(
        existing_tools={"memory_update": _tool("memory_update", ["ceo"])},
        discovered=[_tool("memory_update", ["ceo", "execution"])],
        apply_memory_node_role_widening=True,
    )
    assert merged[0].allowed_roles == ["ceo", "execution"]


def test_role_widening_skips_tools_the_operator_touched() -> None:
    store = _FakeStore(touched={"memory_update"})
    registry = _registry(store)
    merged = registry._merge_tools(
        existing_tools={"memory_update": _tool("memory_update", ["ceo"])},
        discovered=[_tool("memory_update", ["ceo", "execution"])],
        apply_memory_node_role_widening=True,
    )
    assert merged[0].allowed_roles == ["ceo"]


def test_role_widening_never_overrides_a_different_persisted_choice() -> None:
    store = _FakeStore()
    registry = _registry(store)
    merged = registry._merge_tools(
        existing_tools={"memory_update": _tool("memory_update", ["ceo", "inspection"])},
        discovered=[_tool("memory_update", ["ceo", "execution"])],
        apply_memory_node_role_widening=True,
    )
    assert merged[0].allowed_roles == ["ceo", "inspection"]


def test_role_widening_is_flag_gated_and_idempotent() -> None:
    store = _FakeStore()
    registry = _registry(store)
    assert registry._should_widen_memory_tool_node_roles() is True
    without_flag = registry._merge_tools(
        existing_tools={"memory_update": _tool("memory_update", ["ceo"])},
        discovered=[_tool("memory_update", ["ceo", "execution"])],
    )
    assert without_flag[0].allowed_roles == ["ceo"]
    registry._mark_memory_tool_node_roles_widened()
    assert store.meta[MEMORY_TOOL_NODE_ROLE_WIDENING_META_KEY] is True
    assert registry._should_widen_memory_tool_node_roles() is False


def test_widening_report_leaves_an_audit_trail_outside_the_operator_category() -> None:
    store = _FakeStore()
    registry = _registry(store)
    registry._report_memory_tool_node_role_widening(
        existing_tools={"memory_update": _tool("memory_update", ["ceo"])},
        merged=[_tool("memory_update", ["ceo", "execution"])],
    )
    assert len(store.actions) == 1
    # 迁移自己的留痕不许被读成"操作员改过策略"，否则下一次守卫会误判。
    assert store.actions[0]["category"] == "tool_migration"
    assert store.has_tool_policy_operator_action("memory_update") is False


def test_node_lane_has_no_review_window_entrypoint() -> None:
    """节点结构上不可能产生复核队列行：取数口只主动提交，不接复核窗口。"""
    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in (root / "main").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "record_turn_for_review" in text or "flush_review_window" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
