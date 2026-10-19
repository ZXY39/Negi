"""内部轮（心跳 / 定时）在冻结宽面下的收窄语义复核（交接文档「待实现项 2」）。

复核结论落成断言：

1. **车道切换不重印**：`_frontdoor_bundle_recommit_boundary` 只看压缩 / 手动压缩标记 /
   空闲边界，内部轮（`heartbeat_internal` / `cron_internal`）不是重印理由——外部轮钉住的
   `tools[]` 与 skill 全量名单跨车道共享，一次车道切换不得顶掉身后前缀。
2. **收窄落在执行侧**：内部轮 state 里的 `provider_tool_names` 保持冻结声明面不动，而
   `_frontdoor_dispatch_tool_names`（派发/准入的名字源）按当轮治理可见集收窄——
   "看得见但调不动"是内部轮的预期形态，不是 bug。
3. **skill 同理**：头部名单冻结（看得见），`load_skill_context_v2` 每次调用按治理实时
   可见集裁决（调不动 → 三态回执），不读内部轮继承下来的 `rbac_visible_skill_ids` 快照。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.frontdoor.skill_roster import clear_skill_roster_ledgers
from g3ku.runtime.frontdoor.tool_contract import (
    clear_pinned_contract_stores,
    skill_roster_epoch_for,
)
from g3ku.runtime.session_agent import (
    FRONTDOOR_LAST_SEND_AT_ATTR,
    PENDING_PROVIDER_BUNDLE_RECOMMIT_ATTR,
)
from main.protocol import now_iso
from main.service.runtime_service import MainRuntimeService


@pytest.fixture(autouse=True)
def _isolate_process_stores():
    clear_pinned_contract_stores()
    clear_skill_roster_ledgers()
    yield
    clear_pinned_contract_stores()
    clear_skill_roster_ledgers()


def _ops() -> CeoFrontDoorRuntimeOps:
    return CeoFrontDoorRuntimeOps(loop=SimpleNamespace())


def test_lane_switch_is_not_a_recommit_boundary() -> None:
    """心跳/定时内部轮不是重印理由：epoch 不动，冻结的头部与 tools[] 继续复用。"""
    ops = _ops()
    session = SimpleNamespace()
    setattr(session, FRONTDOOR_LAST_SEND_AT_ATTR, now_iso())
    session_key = "web:shared"
    epoch_before = skill_roster_epoch_for(session, session_key=session_key)

    for lane_flags in (
        {"heartbeat_internal": True},
        {"cron_internal": True},
        {"heartbeat_internal": True, "cron_internal": True},
    ):
        state = {"session_key": session_key, **lane_flags}
        assert ops._frontdoor_bundle_recommit_boundary(session=session, state=state) is False
        assert skill_roster_epoch_for(session, session_key=session_key) == epoch_before

    # 对照：压缩挂的待重印标记仍然成立，且与 epoch 同源递增
    setattr(session, PENDING_PROVIDER_BUNDLE_RECOMMIT_ATTR, True)
    assert ops._frontdoor_bundle_recommit_boundary(
        session=session, state={"session_key": session_key, "heartbeat_internal": True}
    ) is True
    assert skill_roster_epoch_for(session, session_key=session_key) == epoch_before + 1
    # 标记是一次性的：同回合再问不再重印
    assert ops._frontdoor_bundle_recommit_boundary(
        session=session, state={"session_key": session_key, "heartbeat_internal": True}
    ) is False


def _internal_turn_state() -> dict:
    return {
        "session_key": "web:shared",
        "heartbeat_internal": True,
        # 声明面：外部轮钉住的那份，跨车道原样继承
        "provider_tool_names": ["exec", "web_fetch", "filesystem_write", "submit_next_stage"],
        "tool_names": ["exec", "web_fetch", "filesystem_write", "submit_next_stage"],
        # 治理可见集：当轮只授权了这三个（web_fetch 被收回）
        "rbac_visible_tool_names": ["exec", "filesystem_write", "submit_next_stage"],
        "hydrated_tool_names": [],
        "messages": [],
        "frontdoor_stage_state": {},
    }


def test_internal_turn_keeps_frozen_declaration_and_narrows_dispatch() -> None:
    """内部轮：声明面冻结，派发面按治理可见集收窄（看得见但调不动）。"""
    ops = _ops()
    state = _internal_turn_state()
    frozen_declaration = list(state["provider_tool_names"])

    dispatch = ops._frontdoor_dispatch_tool_names(state)

    assert dispatch == ["exec", "filesystem_write", "submit_next_stage"]
    assert "web_fetch" not in dispatch
    # 声明面一个名字没少：tools[] 的字节不因这一跳的权限收窄而变
    assert state["provider_tool_names"] == frozen_declaration


def test_internal_turn_keeps_stage_tool_callable_without_active_stage() -> None:
    """内部轮没有活动阶段时，阶段工具仍可调用（否则整轮一个工具都用不了）。"""
    ops = _ops()
    state = _internal_turn_state()

    callable_names = ops._frontdoor_callable_tool_names_for_state(state)

    assert "submit_next_stage" in callable_names


def test_internal_turn_skill_read_is_narrowed_by_live_governed_set() -> None:
    """头部名单冻结（看得见），load 按治理实时可见集裁决（调不动 → skill_not_permitted）。"""
    record = SimpleNamespace(skill_id="frozen-in-head", skill_doc_path="", display_name="Frozen")
    registry = SimpleNamespace(get_skill_resource=lambda skill_id: record)
    stub = SimpleNamespace(
        # 内部轮继承的 rbac_visible_skill_ids 里还有它，但治理实时可见集已经没有了
        list_contract_visible_skill_resources=lambda *, actor_role, session_id: [],
        resource_registry=registry,
    )

    payload = MainRuntimeService.load_skill_context_v2(
        stub,
        actor_role="ceo",
        session_id="web:shared",
        skill_id="frozen-in-head",
    )

    assert payload["ok"] is False
    assert payload["error"] == "skill_not_permitted"
