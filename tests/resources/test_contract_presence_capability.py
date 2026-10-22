"""刀一：契约在场即能力 —— 在场判据、撤销回写台账、重复读守卫同判据。

不变量：一个工具在某一跳能不能被调用，取决于它的 toolskill 契约**标记块**在这一跳是不是
在模型可见上下文里。判据只有一个函数（`g3ku/runtime/tool_context_presence.py`），载体也
只有这一个：请求视图消息里的 `[TOOL_CONTRACT:<tool_id>:<fingerprint>]` 块。

> 阶段台账的 `keep_tools` / `keep_skills` 通道已整条删除：正文不在模型眼前就不算在场，
> 裁撤即离场、下次调用由那一次调用自己把契约带回来。

覆盖 `docs/FIX_PLAN_contract-presence-capability.md` §4 的 1–6 项（刀一能覆盖的部分）。
撤销**必须回写台账**（不只是收窄视图）：只收窄视图的话，该名字既不在 callable、又被
candidate 的「排除已提升」规则挡在门外，模型 load 它只会拿到 `already_hydrated`，
那是文档禁止的第 4 态。

注：`test_contract_presence_*` 三条只测新模块自己的算术，在未修改的树上同样是绿的，
不算回归覆盖；真正的撤销/在场覆盖从 `test_node_*` 起。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.registry import ToolRegistry
from g3ku.runtime.context.execution_tool_selection import build_execution_tool_selection
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.state_models import initial_persistent_state
from g3ku.runtime.tool_context_presence import (
    contract_presence,
    contract_presence_index,
    loader_contract_index,
    partition_contract_presence,
)
from g3ku.runtime.tool_contract_block import render_tool_contract_block
from g3ku.runtime.tool_error_guidance import availability_hint
from g3ku.runtime.web_ceo_sessions import _normalized_completed_continuity_snapshot
from main.monitoring.log_service import TaskLogService
from main.monitoring.models import TaskProjectionRuntimeFrameRecord
from main.runtime.tool_names import SILENT_TOOL_NAME
from main.runtime.react_loop import ReActToolLoop
from main.service.runtime_service import MainRuntimeService

TOOL_ID = "filesystem_write"
FINGERPRINT = "tcf:" + "a" * 16
OTHER_ID = "content_open"


class _FakeTool(Tool):
    """只服务 schema/实例解析断言的极简工具：声明面与准入面分源时不碰真实 handler。"""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} fixture tool"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: object) -> str:
        return ""


def _loader_message(tool_id: str = TOOL_ID, fingerprint: str = FINGERPRINT, *, ok: bool = True) -> dict:
    """投递形态与真车道同形：JSON 信封的 `content` 字段里装着契约块。

    失败载荷不含块——判据据此判不在场。
    """
    if not ok:
        payload = {"ok": False, "tool_id": tool_id, "error": "Tool not visible"}
    else:
        payload = {
            "ok": True,
            "tool_id": tool_id,
            "tool_context_fingerprint": fingerprint,
            "content": render_tool_contract_block(tool_id, fingerprint, f"# {tool_id}\n契约正文"),
        }
    return {"role": "tool", "name": "load_tool_context", "content": json.dumps(payload, ensure_ascii=False)}


def _kept_entry(tool_id: str = TOOL_ID, fingerprint: str = FINGERPRINT) -> dict:
    return {
        "tool_id": tool_id,
        "tool_context_fingerprint": fingerprint,
        "body": "# filesystem_write\n契约正文",
    }


def _kept_stages(tool_id: str = TOOL_ID, fingerprint: str = FINGERPRINT) -> list[dict]:
    return [{"stage_id": "stage-1", "kept_tool_contexts": [_kept_entry(tool_id, fingerprint)]}]


# ---------------------------------------------------------------------------
# §2.1 共享判据：只有一个载体（块），失败载荷不算，skill 与台账正文都不参与
# ---------------------------------------------------------------------------


def test_contract_presence_accepts_the_block_carrier_only() -> None:
    present, fingerprint = contract_presence(TOOL_ID, request_messages=[_loader_message()])
    assert present is True
    assert fingerprint == FINGERPRINT

    # 阶段台账里的保留正文不再算在场（D9）：判据不再接受那个参数，签名里都没有它。
    present, fingerprint = contract_presence(TOOL_ID, request_messages=[])
    assert present is False
    assert fingerprint == ""


def test_contract_presence_index_skips_failures_and_ledger_entries() -> None:
    index = contract_presence_index(
        request_messages=[_loader_message(ok=False), _loader_message(OTHER_ID, "tcf:" + "b" * 16)],
    )
    assert OTHER_ID in index
    assert TOOL_ID not in index
    assert loader_contract_index([_loader_message(ok=False)]) == {}

    present, absent = partition_contract_presence([OTHER_ID, "skill_backed_tool", "filesystem_delete"], index=index)
    assert present == [OTHER_ID]
    assert absent == ["skill_backed_tool", "filesystem_delete"]


def test_skill_contexts_do_not_participate_in_presence() -> None:
    """skill 不水合、没有 callable 可摘，所以一段 skill 正文不构成在场证据。"""
    stages = [{"stage_id": "s1", "kept_skill_contexts": [{"skill_id": TOOL_ID, "body": "正文"}]}]
    assert contract_presence(TOOL_ID, request_messages=[{"content": json.dumps(stages)}])[0] is False


# ---------------------------------------------------------------------------
# 节点车道：契约在场即能力 + candidate 复位 + 存量台账数据不得复活
# ---------------------------------------------------------------------------


class _NodeLogService:
    """带真实归一化白名单的假账本：字段漏白名单必须能被这些用例抓到。"""

    def __init__(self) -> None:
        self._frames: dict[tuple[str, str], dict] = {}

    def upsert_frame(self, task_id: str, payload: dict, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        node_id = str((payload or {}).get("node_id") or "").strip()
        self._frames[(str(task_id), node_id)] = TaskLogService._sanitize_runtime_frame(dict(payload or {}))

    def update_frame(self, task_id: str, node_id: str, mutate, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        key = (str(task_id), str(node_id))
        current = self._frames.get(key) or {"node_id": node_id}
        mutated = mutate(TaskLogService._sanitize_runtime_frame(dict(current)))
        self._frames[key] = TaskLogService._sanitize_runtime_frame(dict(mutated or {}))

    def read_runtime_frame(self, task_id: str, node_id: str) -> dict:
        stored = self._frames.get((str(task_id), str(node_id)))
        if stored is None:
            return {}
        record = TaskProjectionRuntimeFrameRecord(task_id=str(task_id), node_id=str(node_id), payload=stored)
        return TaskLogService._hydrate_runtime_frame_record(object.__new__(TaskLogService), record)


def _node_service(
    *,
    legacy_ledger: list[str] | None = None,
    stages: dict | None = None,
    visible: list[str] | None = None,
) -> tuple[MainRuntimeService, _NodeLogService]:
    """`legacy_ledger` 只用来播种**存量**台账字段：这些用例要钉的是"旧数据不再撑起能力"。"""
    service = object.__new__(MainRuntimeService)
    log_service = _NodeLogService()
    service.log_service = log_service
    effective_visible = list(
        visible
        if visible is not None
        else [TOOL_ID, OTHER_ID, "filesystem_edit", "exec", SILENT_TOOL_NAME, "submit_final_result"]
    )
    service.list_effective_tool_names = lambda *, actor_role, session_id: list(effective_visible)
    service.list_visible_tools = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id=tool_id)
        for tool_id in (TOOL_ID, "filesystem_edit", OTHER_ID)
    ]
    log_service.upsert_frame(
        "task-cp",
        {
            "node_id": "node-cp",
            "node_kind": "execution",
            "hydrated_executor_state": list(legacy_ledger or []),
            "hydrated_executor_names": list(legacy_ledger or []),
            "execution_stages": dict(stages or {}),
            "messages": [],
        },
    )
    return service, log_service


def _node_obj() -> SimpleNamespace:
    return SimpleNamespace(node_id="node-cp", task_id="task-cp", node_kind="execution", can_spawn_children=False)


def _task_obj() -> SimpleNamespace:
    return SimpleNamespace(task_id="task-cp", session_id="web:shared", metadata={})


def test_node_contract_absent_tool_is_not_callable() -> None:
    """块不在请求视图里 ⇒ 不在 callable，且没有任何跨跳状态能把它捞回来。"""
    service, _log = _node_service(legacy_ledger=[TOOL_ID])
    present = service.contract_present_tool_names_for_node(
        actor_role="execution",
        session_id="web:shared",
        request_messages=[],
    )
    assert present == []
    assert TOOL_ID not in service._callable_tool_names_for_node(
        task=_task_obj(),
        node=_node_obj(),
        request_messages=[],
    )


def test_node_contract_present_tool_is_callable() -> None:
    service, _log = _node_service()
    present = service.contract_present_tool_names_for_node(
        actor_role="execution",
        session_id="web:shared",
        request_messages=[_loader_message()],
    )
    assert present == [TOOL_ID]
    assert TOOL_ID in service._callable_tool_names_for_node(
        task=_task_obj(),
        node=_node_obj(),
        request_messages=[_loader_message()],
    )


def test_node_presence_requires_request_view() -> None:
    """调用方没有当次请求视图时按空集处理：宁可不放行，不可无契约放行。"""
    service, _log = _node_service(legacy_ledger=[TOOL_ID])
    assert service.contract_present_tool_names_for_node(
        actor_role="execution",
        session_id="web:shared",
        request_messages=None,
    ) == []


def test_node_presence_follows_granted_order_and_drops_ungranted_blocks() -> None:
    """在场索引里可能有上一跳留下的名字，治理已收回的不得进 callable。"""
    service, _log = _node_service(visible=[SILENT_TOOL_NAME, "submit_final_result"])
    present = service.contract_present_tool_names_for_node(
        actor_role="execution",
        session_id="web:shared",
        request_messages=[_loader_message(OTHER_ID), _loader_message()],
    )
    assert present == []


def test_node_stage_ledger_alone_does_not_make_tool_callable() -> None:
    """存量阶段账本里留着保留正文、请求里没有块 ⇒ 不可调用（旧行为是这一条就能撑起 callable）。"""
    service, _log = _node_service()
    node = _node_obj()
    node.metadata = {"execution_stages": {"stages": _kept_stages()}}
    assert TOOL_ID not in service._callable_tool_names_for_node(
        task=_task_obj(),
        node=node,
        request_messages=[],
    )
    with_block = service._callable_tool_names_for_node(
        task=_task_obj(),
        node=node,
        request_messages=[_loader_message()],
    )
    assert TOOL_ID in with_block


def test_absent_tool_returns_to_candidates() -> None:
    """candidate = 已授权 − callable：不在场的名字必须回到候选，否则既不可调也不可读（第 4 态）。"""
    service, _log = _node_service()
    visible = service.list_effective_tool_names(actor_role="execution", session_id="web:shared")
    fixed = [
        name
        for name in visible
        if name in {SILENT_TOOL_NAME, "submit_final_result", "exec"}
    ]
    selection = build_execution_tool_selection(
        prompt="p",
        goal="g",
        core_requirement="g",
        visible_tool_items=[],
        visible_tool_names=list(visible),
        always_callable_tool_names=list(fixed),
        contract_present_tool_names=[],
        schema_size_by_executor={name: 100 for name in visible},
    )
    assert TOOL_ID in list(selection.candidate_tool_names or [])
    assert TOOL_ID not in list(selection.callable_tool_names or [])

    present_selection = build_execution_tool_selection(
        prompt="p",
        goal="g",
        core_requirement="g",
        visible_tool_items=[],
        visible_tool_names=list(visible),
        always_callable_tool_names=list(fixed),
        contract_present_tool_names=[TOOL_ID],
        schema_size_by_executor={name: 100 for name in visible},
    )
    assert TOOL_ID in list(present_selection.callable_tool_names or [])
    assert TOOL_ID not in list(present_selection.candidate_tool_names or [])


def test_legacy_ledger_keys_leave_the_frame_whitelist() -> None:
    """五颗台账键都必须从帧白名单里消失：存量 payload 带着它们进来，归一化后不得复活。"""
    legacy_keys = (
        "hydrated_executor_state",
        "hydrated_executor_names",
        "hydration_revoked_executor_names",
        "hydration_evicted_executor_names",
    )
    payload = TaskLogService._sanitize_runtime_frame(
        {"node_id": "n", **{key: ["a"] for key in legacy_keys}, "callable_tool_names": [TOOL_ID]}
    )
    for key in legacy_keys:
        assert key not in payload, key
    assert payload["callable_tool_names"] == [TOOL_ID]

    record = TaskProjectionRuntimeFrameRecord(task_id="t", node_id="n", payload=payload)
    hydrated = TaskLogService._hydrate_runtime_frame_record(object.__new__(TaskLogService), record)
    for key in legacy_keys:
        assert key not in hydrated, key

    snapshot = TaskLogService._sanitize_callable_tool_snapshot({key: ["a"] for key in legacy_keys})
    for key in legacy_keys:
        assert key not in snapshot, key

    default_frame = TaskLogService._default_frame(node_id="n")
    for key in legacy_keys:
        assert key not in default_frame, key


# ---------------------------------------------------------------------------
# 前门车道：三个算点必须给出同一份 callable
# ---------------------------------------------------------------------------


def _frontdoor_state(
    *,
    pool: list[str] | None = None,
    messages: list[dict],
    stages: list[dict] | None = None,
    legacy_hydrated: list[str] | None = None,
) -> dict:
    """`pool` 是已授权池（治理可见 ∪ 本轮声明），不是"可调用来"：可调用来只由块在场决定。

    `legacy_hydrated` 只用来播种**已删除**的台账键，钉的是"存量状态带着它们也不影响判据"。
    """
    granted = ["exec", *(pool if pool is not None else [TOOL_ID])]
    state = initial_persistent_state(user_input={"content": "q"})
    state["tool_names"] = list(granted)
    state["provider_tool_names"] = list(granted)
    state["candidate_tool_names"] = []
    state["rbac_visible_tool_names"] = list(granted)
    state["messages"] = list(messages)
    if legacy_hydrated is not None:
        state["hydrated_tool_names"] = list(legacy_hydrated)
    # 真实图状态始终带 model_refs（`state_models.CeoPersistentState.model_refs`），
    # `_refresh_prompt_cache_state` 只在状态缺它时才回落到读磁盘配置取模型。测试里没有
    # `.g3ku/config.json`，不钉住这一份就会在算点二上 FileNotFoundError——那是取模型，
    # 不是在场判据，别让环境问题冒充判据失败。
    state["model_refs"] = ["contract-presence-test-model"]
    state["frontdoor_stage_state"] = {
        "active_stage_id": "s1",
        "stages": [{"stage_id": "s1", "status": "active"}],
    }
    if stages:
        state["frontdoor_stage_state"]["stages"].extend(stages)
    return state


def _ops() -> CeoFrontDoorRuntimeOps:
    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)
    ops._loop = SimpleNamespace(tools={}, app_config=None, main_task_service=None)
    return ops


def test_frontdoor_callable_requires_the_contract_block() -> None:
    ops = _ops()
    assert TOOL_ID not in ops._frontdoor_callable_tool_names_for_state(
        _frontdoor_state(messages=[])
    )
    assert TOOL_ID in ops._frontdoor_callable_tool_names_for_state(
        _frontdoor_state(messages=[_loader_message()])
    )


def test_frontdoor_legacy_ledger_state_does_not_count_as_present() -> None:
    """存量状态留着 `hydrated_tool_names` 与保留正文、请求里没有块 ⇒ 判不在场。

    两条通道都已整条删除，但历史会话还带着这些键；这条钉的是"判据只认块"，
    存量数据不会被读成第二载体。
    """
    ops = _ops()
    state = _frontdoor_state(
        messages=[],
        stages=_kept_stages(),
        legacy_hydrated=[TOOL_ID],
    )
    assert ops._frontdoor_contract_present_names(state) == []
    assert TOOL_ID not in ops._frontdoor_callable_tool_names_for_state(state)


def test_frontdoor_three_compute_points_agree() -> None:
    """§4.3 合同分裂回归：装配路 / 缓存刷新 / 发送预检三处必须同判据。

    只补不删的 provider `tools[]` 不在本判据范围内，因此这里比对的是模型面 callable。
    """
    ops = _ops()
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    state = _frontdoor_state(messages=[])

    # 算点一：装配路 —— message_builder 用经判据筛过的在场名组 callable。
    assembly_names = CeoMessageBuilder._callable_tool_names(
        visible_tool_names=list(state["rbac_visible_tool_names"]),
        contract_present_tool_names=ops._frontdoor_contract_present_names(state),
    )

    # 算点二：缓存刷新（动态尾部合同的 callable 行由同一个 helper 得出）。
    refreshed = runner._refresh_prompt_cache_state(state=dict(state))
    refresh_names = runner._frontdoor_callable_tool_names_for_state(refreshed)

    # 算点三：发送预检。
    preflight_names = ops._frontdoor_callable_tool_names_for_state(dict(state))

    assert TOOL_ID not in assembly_names
    assert TOOL_ID not in refresh_names
    assert TOOL_ID not in preflight_names
    assert set(refresh_names) == set(preflight_names)


def test_frontdoor_internal_rounds_apply_same_filter() -> None:
    """§4.4 内部轮继承的 callable 也必须过同一在场判据。"""
    ops = _ops()
    for flag in ("heartbeat_internal", "cron_internal"):
        assert TOOL_ID not in ops._frontdoor_callable_tool_names_for_state(
            {**_frontdoor_state(messages=[]), flag: True}
        ), flag
        assert TOOL_ID in ops._frontdoor_callable_tool_names_for_state(
            {**_frontdoor_state(messages=[_loader_message()]), flag: True}
        ), flag


def test_frontdoor_absent_tool_stays_in_the_candidate_view() -> None:
    """不在场的名字必须同时满足：不进 callable、仍在候选里可见（第 4 态禁止）。"""
    ops = _ops()
    state = _frontdoor_state(messages=[])
    state["candidate_tool_items"] = [{"tool_id": TOOL_ID, "display_name": TOOL_ID, "description": TOOL_ID}]
    after = ops._frontdoor_tool_state_after_tool_results(state=state)
    assert TOOL_ID not in ops._frontdoor_callable_tool_names_for_state(after)
    assert TOOL_ID in ops._frontdoor_candidate_tool_view(after)
    assert TOOL_ID in [str(item.get("tool_id") or "") for item in list(after["candidate_tool_items"] or [])]


def test_frontdoor_writes_no_ledger_state_key() -> None:
    """工具态回写不得再产出任何台账键：写侧一旦复活，判据就有了第二份账。"""
    ops = _ops()
    state = _frontdoor_state(messages=[], legacy_hydrated=[TOOL_ID])
    after = ops._frontdoor_tool_state_after_tool_results(state=state)
    for key in ("hydrated_tool_names", "hydration_revoked_executor_names", "hydration_evicted_executor_names"):
        assert key not in after, key


def test_frontdoor_successful_load_needs_no_promotion_bookkeeping() -> None:
    """load 的契约就在同批工具结果里：调用方把结果消息一并交进来，在场判据当跳就能转真。"""
    ops = _ops()
    state = _frontdoor_state(messages=[])
    state["candidate_tool_names"] = [TOOL_ID]
    hop_state = {**state, "messages": list(state["messages"]) + [_loader_message()]}
    after = ops._frontdoor_tool_state_after_tool_results(state=hop_state)
    # 回写返回的是**局部更新**（tool_names / candidate_*），生产侧把它并进整份 state 再读；
    # 判据要读消息视图，所以这里并的是带结果的那一份。
    merged = {**hop_state, **after}
    assert TOOL_ID in ops._frontdoor_callable_tool_names_for_state(merged)


def test_frontdoor_legacy_field_leaves_session_normalization() -> None:
    """会话归一化与前门持久状态都不得再携带台账键（存量键进来即被丢）。"""
    snapshot = _normalized_completed_continuity_snapshot(
        {
            "hydrated_tool_names": ["a"],
            "hydration_revoked_executor_names": ["b"],
            "visible_tool_ids": ["exec"],
            "updated_at": "t",
        }
    )
    assert "hydrated_tool_names" not in snapshot
    assert "hydration_revoked_executor_names" not in snapshot
    assert snapshot["visible_tool_ids"] == ["exec"]

    state = initial_persistent_state(user_input={"content": "q"})
    assert "hydrated_tool_names" not in state
    assert "hydration_revoked_executor_names" not in state


# ---------------------------------------------------------------------------
# 重复读守卫：块内保留正文算作在场 ⇒ 不许放行重读
# ---------------------------------------------------------------------------


def test_duplicate_guards_share_the_block_presence_index() -> None:
    """§4.2 后半：两条车道的重读守卫与 callable 收窄共用同一个在场判据（只认块）。

    同判据才会拒重读；判据口径不同就会放行、同一份正文在上下文里出现两次。台账里保留的
    正文不再算在场（D9），守卫也就不会因为账本里有正文而拒读一次"模型其实读不到"的重读。
    """
    loader_history = [_loader_message()]

    from_loader = ReActToolLoop._latest_contract_fingerprints_by_tool_id(loader_history)
    assert from_loader[TOOL_ID] == FINGERPRINT
    assert CeoFrontDoorRuntimeOps._frontdoor_contract_fingerprint_index(loader_history)[TOOL_ID] == FINGERPRINT

    unrelated = [{"role": "tool", "name": "load_tool_context", "content": json.dumps({"ok": True, "tool_id": "z"})}]
    assert ReActToolLoop._latest_contract_fingerprints_by_tool_id(unrelated) == {}
    assert CeoFrontDoorRuntimeOps._frontdoor_contract_fingerprint_index(unrelated) == {}

    # 对照条：只有账本正文、没有块 ⇒ 守卫认不出 TOOL_ID。
    kept = _kept_stages()
    ledger_only = [{"role": "tool", "name": "submit_next_stage", "content": json.dumps({"kept_tool_contexts": kept})}]
    assert ReActToolLoop._latest_contract_fingerprints_by_tool_id(ledger_only) == {}
    assert CeoFrontDoorRuntimeOps._frontdoor_contract_fingerprint_index(ledger_only) == {}


def test_frontdoor_candidate_view_is_granted_minus_callable() -> None:
    """candidate = 治理可见 −（callable ∪ 待修复）：契约缺席的名字当跳就回到候选（第 4 态禁止）。

    台账时代靠 `revive_contract_absent_candidates` 把撤销名并回候选；现在候选直接从
    同一个判据派生，不需要"并回"这一步，也就不存在"只减不并"造成的回合内死区。
    """
    ops = _ops()
    state = _frontdoor_state(messages=[_loader_message(OTHER_ID, "tcf:" + "b" * 16)])
    state["tool_names"] = ["exec", TOOL_ID, OTHER_ID]
    state["provider_tool_names"] = ["exec", TOOL_ID, OTHER_ID]
    state["rbac_visible_tool_names"] = ["exec", TOOL_ID, OTHER_ID, "perf_inspect"]
    state["repair_required_tool_items"] = [{"tool_id": "perf_inspect", "display_name": "perf_inspect"}]

    callable_names = ops._frontdoor_callable_tool_names_for_state(state)
    assert OTHER_ID in callable_names
    assert TOOL_ID not in callable_names

    view = ops._frontdoor_candidate_tool_view(state)
    assert TOOL_ID in view
    # 待修复那一档 builder 侧本来就摘出去，候选里也不该出现。
    assert "perf_inspect" not in view
    assert ops._frontdoor_candidate_tool_view(None) == []


def _evicted_stage_state() -> dict:
    stage = {
        "stage_id": "s1",
        "stage_index": 1,
        "stage_kind": "normal",
        "status": "completed",
        "context_evicted": True,
        "rounds": [{"round_id": "r1", "tool_call_ids": ["call-load-1"], "tools": []}],
    }
    return {"stages": [stage], "active_stage_id": "", "transition_required": False}


def _loader_messages() -> list[dict]:
    """一条**真在场**的 loader 行：JSON 信封的 `content` 里装着契约块。

    只带 `tool_context_fingerprint` 键而没有块，在新判据下等于"不在场"，那会让下面这批
    用例假绿（裁撤对照、钉住派发都依赖这一行本来是在场的）。
    """
    return [
        {
            "role": "tool",
            "name": "load_tool_context",
            "tool_call_id": "call-load-1",
            "content": json.dumps(
                {
                    "ok": True,
                    "tool_id": TOOL_ID,
                    "tool_context_fingerprint": "tcf:fixture-presence",
                    "content": render_tool_contract_block(
                        TOOL_ID, "tcf:fixture-presence", "# fixture toolskill body"
                    ),
                },
                ensure_ascii=False,
            ),
        }
    ]






def test_dispatch_stays_pinned_while_executable_set_carries_present_tool() -> None:
    """`tools[]` 全程钉死：派发名单（schema 面）不并在场名；可执行名单（执行准入面）
    并入契约在场的名——mid-turn 注册的工具 load 落账后即可调用，但参数表要等
    声明在重印边界吸收它才进 `tools[]`。"""

    ops = _ops()
    state = {
        "messages": _loader_messages(),
        "provider_tool_names": ["exec"],
        "rbac_visible_tool_names": ["exec"],
        "tool_names": ["exec", TOOL_ID],
    }
    assert ops._frontdoor_dispatch_tool_names(state) == ["exec"]
    assert ops._frontdoor_executable_tool_names(state) == ["exec", TOOL_ID]


# ---------------------------------------------------------------------------
# 准入面 = 本轮 callable：声明面（tools[]）宽于准入面，不 load 就不许调
# ---------------------------------------------------------------------------


def test_executable_rejects_declared_tool_that_was_never_loaded() -> None:
    """声明里有参数表 ≠ 本轮能调：`tools[]` 是可加载面，`callable_tools` 才是准入面。

    实盘形态（web:ceo-670a38bb01b3）：`windows_mcp` 从首跳就在 `tools[]` 里、从没
    load 过，却成功调用了 4 次。这条钉住修正后的边界：声明面照旧（钉死不缩水），
    准入面只认 callable。
    """

    ops = _ops()
    state = {
        "messages": [],
        "provider_tool_names": ["exec", OTHER_ID],
        "rbac_visible_tool_names": ["exec", OTHER_ID],
        "tool_names": ["exec"],
    }
    assert ops._frontdoor_dispatch_tool_names(state) == ["exec", OTHER_ID]
    assert ops._frontdoor_executable_tool_names(state) == ["exec"]


def test_executable_keeps_fixed_builtins_without_a_contract() -> None:
    """常驻内置没有 toolskill，也就从不受"先有契约块"约束。"""

    ops = _ops()
    state = {
        "messages": [],
        "provider_tool_names": ["exec", "load_tool_context"],
        "rbac_visible_tool_names": ["exec", "load_tool_context"],
        "tool_names": ["exec", "load_tool_context"],
    }
    assert ops._frontdoor_executable_tool_names(state) == ["exec", "load_tool_context"]


def test_executable_drops_declared_present_tool_revoked_by_rbac() -> None:
    """RBAC 收回当跳生效：名字还挂在钉住的 `tools[]` 上（清单滞后），准入立刻关。"""

    ops = _ops()
    state = {
        "messages": _loader_messages(),
        "provider_tool_names": ["exec", TOOL_ID],
        "rbac_visible_tool_names": ["exec"],
        "tool_names": ["exec", TOOL_ID],
    }
    assert ops._frontdoor_executable_tool_names(state) == ["exec"]


def test_executable_keeps_mid_turn_tool_absent_from_stale_snapshot() -> None:
    """mid-turn 注册的工具本来就不在回合初治理快照里，按快照误拒会让 load 变成死出口。"""

    ops = _ops()
    state = {
        "messages": _loader_messages(),
        "provider_tool_names": ["exec"],
        "rbac_visible_tool_names": ["exec"],
        "tool_names": ["exec", TOOL_ID],
    }
    assert TOOL_ID not in set(ops._frontdoor_dispatch_tool_names(state))
    assert ops._frontdoor_executable_tool_names(state) == ["exec", TOOL_ID]


def test_executable_collapses_to_fixed_builtins_without_callable_view() -> None:
    """旁路/老快照没有 `tool_names` 键时**不**退回声明面。

    台账时代退回声明面是为了"别整束塌成空"；在场判据接管后这条退路等于在无契约放行，
    而塌成空不是死局：模型直接调用那一个工具，冲动路径就把契约送回来，下一跳自然可调。
    """

    ops = _ops()
    state = {
        "messages": [],
        "provider_tool_names": ["exec", OTHER_ID],
        "rbac_visible_tool_names": ["exec", OTHER_ID],
    }
    assert ops._frontdoor_executable_tool_names(state) == ["exec"]


def test_execution_bundle_keeps_declared_surface_wider_than_admission() -> None:
    """`tools[]` 不随 callable 收窄：声明面单独取实例（`declared_tools`），执行束只放
    本轮能调的名字。两者混用会让钉死语义当场失效（schema 从 32 掉到 12）。"""

    registry = ToolRegistry()
    for name in ("exec", OTHER_ID):
        registry.register(_FakeTool(name))
    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)
    ops._loop = SimpleNamespace(tools=registry, app_config=None)
    ops._build_tool_runtime_context = lambda **_: {}
    state = dict(initial_persistent_state(user_input={"content": "x", "metadata": {}}))
    state.update(
        {
            "messages": [],
            "provider_tool_names": ["exec", OTHER_ID],
            "rbac_visible_tool_names": ["exec", OTHER_ID],
            "tool_names": ["exec"],
        }
    )
    bundle = ops._frontdoor_execution_bundle(
        state=state,
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    assert set(bundle.declared_tools) >= {"exec", OTHER_ID, SILENT_TOOL_NAME}
    assert OTHER_ID not in bundle.visible_tools
    assert "exec" in bundle.visible_tools
    schema_names = [item["function"]["name"] for item in ops._frontdoor_tool_schemas_for_state(state=state, runtime=SimpleNamespace(context=SimpleNamespace()))]
    assert OTHER_ID in schema_names


def test_rejection_hint_guides_load_for_declared_unloaded_tool() -> None:
    """语义 top-k 没选中的声明名也要给 load 指引，不能落成"未知名字"。"""

    hint = availability_hint(
        requested=OTHER_ID,
        callable_names=["exec"],
        candidate_names=[],
        denied_names=[],
        declared_names=["exec", OTHER_ID],
    )
    assert OTHER_ID in hint
    assert "load_tool_context" in hint


def test_rejection_hint_prefers_denied_over_declared() -> None:
    """无权限优先于"没加载"：这两句给的动作完全不同。"""

    hint = availability_hint(
        requested=OTHER_ID,
        callable_names=["exec"],
        candidate_names=[],
        denied_names=[OTHER_ID],
        declared_names=["exec", OTHER_ID],
    )
    assert "无权限" in hint
    assert "load_tool_context" not in hint






def _surviving_tool_call_ids(result: dict) -> set[str]:
    return {
        str(item.get("tool_call_id") or "")
        for item in list(result.get("messages") or [])
        if str(item.get("role") or "") == "tool"
    }




# ---------------------------------------------------------------------------
# 节点车道：在场判据必须在派发字典与选择之前生效，且不被任何跨跳视图复活
# ---------------------------------------------------------------------------


def test_node_presence_reads_only_the_request_view() -> None:
    """判据只读传进来的请求视图：既不水合整帧，也不按 node_id 重读存储。

    台账时代这条要读帧里的 `hydrated_executor_state`（每跳还要防着 promoted 视图把它复活），
    实盘 task:92ad2fe69148 的"callable 仍含已裁工具"就来自那份撤销前的视图。现在没有跨跳
    状态可读，读口只剩一个 ⇒ 两类复活、两类白付的读法一起消失。
    """

    class _ExplodingLogService:
        def read_runtime_frame(self, *_args, **_kwargs):
            raise AssertionError("presence must not hydrate a frame")

        def read_runtime_frame_payload(self, *_args, **_kwargs):
            raise AssertionError("presence must not hydrate a frame")

    service, _log = _node_service()
    service.log_service = _ExplodingLogService()
    reads: list[str] = []
    service.store = SimpleNamespace(get_node=lambda node_id: reads.append(str(node_id)) or _node_obj())

    assert service.contract_present_tool_names_for_node(
        actor_role="execution",
        session_id="web:shared",
        request_messages=[],
    ) == []
    assert service.contract_present_tool_names_for_node(
        actor_role="execution",
        session_id="web:shared",
        request_messages=[_loader_message()],
    ) == [TOOL_ID]
    assert reads == []


def test_node_presence_never_reads_the_stage_ledger_again() -> None:
    """账本内容不参与在场：带着保留正文的存量元数据也不改变结论。

    旧用例（实盘 task:91b09176d70a）钉的是"读传进来的 node 快照会把已落库的正文读成空"。
    判据不再读那本账 ⇒ 撤就撤到底，模型下一次调用重新拿契约；这条把口径钉死。
    """
    service, _log = _node_service()
    node = _node_obj()
    node.metadata = {"execution_stages": {"stages": _kept_stages()}}
    assert TOOL_ID not in service._callable_tool_names_for_node(
        task=_task_obj(),
        node=node,
        request_messages=[],
    )
    assert TOOL_ID in service._callable_tool_names_for_node(
        task=_task_obj(),
        node=node,
        request_messages=[_loader_message()],
    )


def test_path_anchor_contract_renders_from_one_source_across_lanes():
    """落点规则只允许一个来源：两条车道必须同句、同 token 串，否则就是分叉实现。"""
    from g3ku.runtime.frontdoor.tool_contract import _render_path_anchor, render_pinned_contract_text
    from g3ku.utils.helpers import PATH_ANCHOR_RULE_TEXT, path_anchor_policy, path_anchor_tokens

    frontdoor_lines = _render_path_anchor()
    assert frontdoor_lines == [
        f'path_anchor: {path_anchor_tokens()}',
        f'path_anchor_rule: {PATH_ANCHOR_RULE_TEXT}',
    ]

    node_policy = path_anchor_policy()
    assert node_policy['path_anchor_rule'] == PATH_ANCHOR_RULE_TEXT
    assert node_policy['path_anchor_tokens'] == path_anchor_tokens()

    pinned = render_pinned_contract_text(
        skill_items=[{'skill_id': 'alpha', 'display_name': 'alpha', 'description': ''}],
        exec_runtime_policy=None,
        session_temp_dir='C:/proj/temp/ceo/web_ceo-x',
    )
    for line in frontdoor_lines:
        assert line in pinned

    # `{data}` 无条件列出：数据根与项目根重合时它是同一个目录，但仍是唯一的交付锚名。
    # 按分离与否增减 token，就等于让操作员第一次设置数据目录那一跳重付一遍头部前缀。
    assert '{data}' in node_policy['path_anchor_tokens']


def test_contract_presence_reads_the_versioned_legacy_block() -> None:
    """存量请求体里的 `[TOOL_CONTRACT_V1:…]` 块仍算在场；写侧只出新拼写。"""
    rendered = render_tool_contract_block("filesystem_stat", "tcf:current", "body")
    legacy = {
        "role": "tool",
        "content": "[TOOL_CONTRACT_V1:filesystem_stat:tcf:legacy]\nbody\n[/TOOL_CONTRACT_V1]",
    }
    current = {"role": "tool", "content": rendered}

    assert rendered.startswith("[TOOL_CONTRACT:filesystem_stat:tcf:current]")
    assert rendered.endswith("[/TOOL_CONTRACT]")
    assert "[TOOL_CONTRACT_V1" not in rendered
    assert contract_presence_index(request_messages=[legacy]) == {"filesystem_stat": "tcf:legacy"}
    assert contract_presence_index(request_messages=[current]) == {"filesystem_stat": "tcf:current"}
    assert contract_presence("filesystem_stat", request_messages=[legacy]) == (True, "tcf:legacy")
