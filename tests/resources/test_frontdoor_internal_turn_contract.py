"""内部轮（心跳 / 定时）在冻结宽面下的收窄语义复核（交接文档「待实现项 2」）。

复核结论落成断言：

1. **车道切换不重印**：`_frontdoor_bundle_recommit_boundary` 只看压缩 / 手动压缩标记，
   内部轮（`heartbeat_internal` / `cron_internal`）不是重印理由——外部轮钉住的
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
from g3ku.runtime.frontdoor.tool_contract import (
    clear_pinned_contract_stores,
    skill_roster_epoch_for,
)
from g3ku.runtime.session_agent import (
    PENDING_PROVIDER_BUNDLE_RECOMMIT_ATTR,
)
from g3ku.runtime.skill_roster_notice import clear_skill_roster_notice_stores
from main.service.runtime_service import MainRuntimeService


@pytest.fixture(autouse=True)
def _isolate_process_stores():
    clear_pinned_contract_stores()
    clear_skill_roster_notice_stores()
    yield
    clear_pinned_contract_stores()
    clear_skill_roster_notice_stores()


def _ops() -> CeoFrontDoorRuntimeOps:
    return CeoFrontDoorRuntimeOps(loop=SimpleNamespace())


def test_lane_switch_is_not_a_recommit_boundary() -> None:
    """心跳/定时内部轮不是重印理由：epoch 不动，冻结的头部与 tools[] 继续复用。"""
    ops = _ops()
    session = SimpleNamespace()
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
        "provider_tool_names": ["exec", "web_fetch", "filesystem_write", "filesystem_stat"],
        "tool_names": ["exec", "web_fetch", "filesystem_write", "filesystem_stat"],
        # 治理可见集：当轮只授权了这三个（web_fetch 被收回）
        "rbac_visible_tool_names": ["exec", "filesystem_write", "filesystem_stat"],
        "hydrated_tool_names": [],
        "messages": [],
    }


def test_internal_turn_keeps_frozen_declaration_and_narrows_dispatch() -> None:
    """内部轮：声明面冻结，派发面按治理可见集收窄（看得见但调不动）。"""
    ops = _ops()
    state = _internal_turn_state()
    frozen_declaration = list(state["provider_tool_names"])

    dispatch = ops._frontdoor_dispatch_tool_names(state)

    assert dispatch == ["exec", "filesystem_write", "filesystem_stat"]
    assert "web_fetch" not in dispatch
    # 声明面一个名字没少：tools[] 的字节不因这一跳的权限收窄而变
    assert state["provider_tool_names"] == frozen_declaration


def test_internal_turn_keeps_fixed_builtin_tools_callable() -> None:
    """内部轮继承固定内置集合：不需要任何前置动作就能收尾或静默。"""
    ops = _ops()
    state = _internal_turn_state()

    callable_names = ops._frontdoor_callable_tool_names_for_state(state)

    assert "silent" in callable_names


def test_stale_compression_reason_is_not_a_recommit_boundary() -> None:
    """跨轮残留的 `token_compression` 不再被当重印判据，也不递增 skill roster epoch。

    实盘（10-19 18:21:28 / 18:22:08 / 18:24:21 / 18:24:29）：一条会话压缩过一次之后，
    连续四个用户轮每轮都按当轮 RBAC 改 `tools[]` 名册。判据读的是会话级持久值
    `_frontdoor_history_shrink_reason`，而它没有消费点——一次压缩把"只有边界能改名册"
    稀释成了"每轮都能改"。现在重印只认消耗式标记。
    """
    ops = _ops()
    session = SimpleNamespace()
    session_key = "web:shared"
    setattr(session, "_frontdoor_history_shrink_reason", "token_compression")
    epoch_before = skill_roster_epoch_for(session, session_key=session_key)
    state = {"session_key": session_key}

    assert ops._frontdoor_bundle_recommit_boundary(session=session, state=state) is False
    assert skill_roster_epoch_for(session, session_key=session_key) == epoch_before

    frozen = CeoFrontDoorRuntimeOps._refresh_frontdoor_provider_tool_bundle(
        prior_provider_tool_names=["exec", "web_fetch"],
        desired_provider_tool_names=["exec"],
        prior_history_shrink_reason="token_compression",
        recommit_boundary=False,
    )
    # 权限收窄只关执行准入：名字继续带参数表，等真正的重印边界一次性吸收
    assert frozen["provider_tool_names"] == ["exec", "web_fetch"]
    assert frozen["provider_tool_bundle_mode"] == "pinned_frozen"

    recommit = CeoFrontDoorRuntimeOps._refresh_frontdoor_provider_tool_bundle(
        prior_provider_tool_names=["exec", "web_fetch"],
        desired_provider_tool_names=["exec"],
        prior_history_shrink_reason="token_compression",
        recommit_boundary=True,
    )
    assert recommit["provider_tool_names"] == ["exec"]
    assert recommit["provider_tool_bundle_mode"] == "pinned_recommitted"


def test_inline_compression_raises_the_one_shot_recommit_marker() -> None:
    """内联压缩挂的是一次性标记，被下一跳消费后就没了——重印只发生一次。"""
    ops = _ops()
    session = SimpleNamespace()
    session_key = "web:shared"
    state = {"session_key": session_key}
    setattr(session, PENDING_PROVIDER_BUNDLE_RECOMMIT_ATTR, True)

    assert ops._frontdoor_bundle_recommit_boundary(session=session, state=state) is True
    assert bool(getattr(session, PENDING_PROVIDER_BUNDLE_RECOMMIT_ATTR, False)) is False
    assert ops._frontdoor_bundle_recommit_boundary(session=session, state=state) is False


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
